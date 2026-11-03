"""ERA5 CDS API 下载器（fetch）。

设计：
- 一次下载 = 一个任务（类型 download），dedup_key = dataset+请求摘要，
  重复执行 `fetch run` 不会重复下载同一份数据。
- 下载先写 <target>.part，完成后原子 os.replace 成正式文件——临时文件
  永远不会与完整结果混淆；中断后重跑会重新下载该文件。
- 下载成功后立即把文件注册进 catalog（挂在一个专用数据卷上）并入队
  scan_metadata 任务，使小体量下载直接进入既有的管理/扫描测试流程。
- CDS 凭证读取顺序：环境变量 CDSAPI_URL/CDSAPI_KEY → ~/.cdsapirc（cdsapi 默认行为）。
- 测试时可替换模块级 CLIENT_FACTORY 注入假客户端（不联网）。

说明：CDS 服务器排队时间不可控，任务超时由 CDS 客户端自身重试机制处理；
本模块不对其设置硬超时，中断后重跑即可续传（已完成的请求会被 dedup 跳过）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

import yaml

from dbas_core.catalog.repository import Catalog
from dbas_core.exceptions import ConfigError, FetchError
from dbas_core.logging import get_logger

log = get_logger("fetch")


class FetchPermanentError(FetchError):
    """不可重试的下载错误：重试必然以同样方式失败。

    继承 `FetchError` 保持向后兼容——既有 `except FetchError` 仍然拦得住。

    为什么需要它：runner 对任何异常都会重试到 `max_attempts`（默认 6 次，
    间隔 60 秒）。但下列错误重试毫无意义，只会白等 6 分钟并占掉批次预算：
    - 请求成本超 CDS 上限：必须把请求拆小，重试同一个请求必然再挂；
    - 变量名/层级/数据集名写错：请求本身错了；
    - 未接受数据集使用条款：要人去网页点同意，程序无法自愈。
    这类错误一律快速失败，让人早点看到真正原因。
    """

    # runner 通过这个属性判断「不重试」。用属性而不是 isinstance，
    # 是为了让 scheduler 层不必反向依赖 fetch 层。
    permanent = True


# 命中即判定为永久失败。只用明确短语，避免把偶发网络抖动误判成永久失败。
_PERMANENT_PATTERNS = (
    re.compile(r"cost\s*limit|too\s+large|exceeds?\s+the\s+.*limit", re.I),
    re.compile(r"required_terms|accept the terms|licen[cs]e.*not.*accept", re.I),
    re.compile(r"unknown\s+(variable|param|dataset|level)|invalid\s+(variable|request|param)", re.I),
    re.compile(r"MarsNoDataError|no data available", re.I),
)


def is_permanent_fetch_error(message: str) -> bool:
    """错误信息是否属于「不该重试」的类型。"""
    return any(p.search(message) for p in _PERMANENT_PATTERNS)


CLIENT_FACTORY: Callable[[], Any] | None = None  # 测试注入点；None = 用 cdsapi.Client


@dataclass
class FetchConfig:
    dataset: str
    variables: list[str]
    years: list[str]
    months: list[str]
    area: list[float] | None = None          # [N, W, S, E]
    target_dir: str = "data/downloads"       # 相对项目根
    volume_id: str = "downloads"
    product_type: str | None = None
    extra_request: dict[str, Any] = field(default_factory=dict)
    split_by: str | None = None              # None | "day" | "time"：把月请求按该维度拆成小请求
    chunk_days: int = 1                      # split_by=day 时每批天数；按 CDS 成本上限取 5

    @classmethod
    def from_yaml(cls, path: Path) -> FetchConfig:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"解析 {path} 失败: {e}") from e
        for key in ("dataset", "variables", "years", "months"):
            if not raw.get(key):
                raise ConfigError(f"fetch 配置缺少必填字段: {key}")
        area = raw.get("area")
        if area is not None:
            if len(area) != 4:
                raise ConfigError(f"area 必须是 [北, 西, 南, 东] 四个值，当前: {area}")
            north, west, south, east = (float(x) for x in area)
            # CDS 约定 north > south；反了会导致 MARS 返回空数据（MarsNoDataError）
            if north <= south:
                raise ConfigError(
                    f"area 顺序为 [北, 西, 南, 东]，但 北({north}) ≤ 南({south})；"
                    f"应写成 [{south}, {min(west, east)}, {north}, "
                    f"{max(west, east)}] 这类北>南的形式")
        return cls(
            dataset=str(raw["dataset"]),
            variables=[str(v) for v in raw["variables"]],
            years=[str(y) for y in raw["years"]],
            months=[str(m) for m in raw["months"]],
            area=raw.get("area"),
            target_dir=str(raw.get("target_dir", "data/downloads")),
            volume_id=str(raw.get("volume_id", "downloads")),
            product_type=raw.get("product_type"),
            extra_request=raw.get("extra_request") or {},
            split_by=(str(raw["split_by"]).lower() if raw.get("split_by") else None),
            chunk_days=int(raw.get("chunk_days", 1) or 1))

    def build_requests(self) -> list[dict[str, Any]]:
        """每个 (变量集 × 年 × 月) 一个最小请求，失败重试代价小。

        `split_by: day` 时按 `chunk_days`（默认 1）把该月请求切成若干批：
        CDS 单请求有成本上限（`estimate_costs` 返回 `cost/limit`，超限即 403）。
        实测 clwc/ciwc × 37 层 × 24 时次：1 天 = 10656，5 天 = 53280（占限额 88.8%），
        6 天 = 63936 就超限。因此默认按 5 天打包，而不是逐日提交——
        请求数从 744 降到 150，队列压力降 80%，单批仍在安全水位内。
        """
        reqs = []
        for year in self.years:
            for month in self.months:
                req: dict[str, Any] = {
                    "variable": self.variables,
                    "year": year,
                    "month": month,
                    **self.extra_request,
                }
                if self.product_type:
                    req["product_type"] = self.product_type
                if self.area:
                    req["area"] = self.area
                if self.split_by == "day" and isinstance(req.get("day"), list) and len(req["day"]) > 1:
                    days = req["day"]
                    size = max(1, int(self.chunk_days or 1))
                    for start in range(0, len(days), size):
                        chunk = days[start:start + size]
                        part = dict(req, day=chunk)
                        suffix = f"d{chunk[0]}" if len(chunk) == 1 else f"d{chunk[0]}-{chunk[-1]}"
                        reqs.append({"year": year, "month": month,
                                     "day": str(chunk[0]) if len(chunk) == 1 else None,
                                     "suffix": suffix, "request": part})
                elif self.split_by == "time" and isinstance(req.get("time"), list) and len(req["time"]) > 1:
                    for t in req["time"]:
                        part = dict(req, time=[t])
                        reqs.append({"year": year, "month": month, "suffix": f"t{t.split(':')[0]}",
                                     "request": part})
                else:
                    reqs.append({"year": year, "month": month, "request": req})
        return reqs


# 语义上是「位置」的参数：顺序有物理含义，归一化时必须原样保留。
# area = [北, 西, 南, 东]，str 形式可能带前导零，都不能重排或重格式化。
_POSITIONAL_KEYS = frozenset({"area"})


def normalize_request(request: dict[str, Any]) -> dict[str, list[str]]:
    """把 CDS 请求归一化成「键 → 排序后的字符串列表」，用于摘要与比较。

    动机（借鉴 cdsswarm `_cds_utils.normalize_request`）：
    CDS 侧把 `"year": "2024"` 和 `"year": ["2024"]` 视为同一个请求，
    但直接 json.dumps 会得到两个完全不同的摘要，于是同一份数据被重复入队下载。
    同理 `["01", "02"]` 与 `["02", "01"]` 在服务端等价（集合语义），也应折叠。

    与 cdsswarm 的差异：它保留所有 list 顺序（为了安全地兼容位置参数），
    这里按参数名区分——只有 `area` 这类位置参数保序，集合型参数一律排序后去重。
    """
    out: dict[str, list[str]] = {}
    for key in sorted(request):
        value = request[key]
        if key in _POSITIONAL_KEYS:
            items = list(value) if isinstance(value, (list, tuple)) else [value]
            out[key] = [str(v) for v in items]
            continue
        items = list(value) if isinstance(value, (list, tuple)) else [value]
        # set 去重顺带吞掉重复值（如 day 列表里误写两遍 "01"）
        out[key] = sorted({str(v) for v in items})
    return out


def request_dedup_key(cfg: FetchConfig, req_meta: dict[str, Any]) -> str:
    """下载任务去重键：dataset + 归一化后的请求。

    注意不要把凭证、task_id 之类的易变字段塞进来：它们既不属于请求语义，
    又会把「同一份数据」拆成多个键，还会在摘要里留下可离线爆破的校验值。
    """
    import hashlib
    import json
    payload = json.dumps({"dataset": cfg.dataset,
                          "request": normalize_request(req_meta["request"])},
                         sort_keys=True, ensure_ascii=False)
    return "download:" + hashlib.sha1(payload.encode()).hexdigest()[:16]


def enqueue_fetch_tasks(catalog: Catalog, cfg: FetchConfig) -> tuple[int, int]:
    """把下载计划转成任务入队。返回 (新增数, 跳过数)。"""
    created = skipped = 0
    for req_meta in cfg.build_requests():
        dedup = request_dedup_key(cfg, req_meta)
        suffix = req_meta.get("suffix", "")
        stem = f"{req_meta['year']}{req_meta['month']}" + (f"_{suffix}" if suffix else "")
        target_rel = (
            f"{cfg.dataset}/{stem}_{cfg.variables[0]}.nc" if len(cfg.variables) == 1
            else f"{cfg.dataset}/{stem}_multi.nc")
        _, ok = catalog.enqueue_task(
            "download",
            {"dataset": cfg.dataset, "request": req_meta["request"],
             "target_rel": target_rel, "volume_id": cfg.volume_id},
            dedup_key=dedup)
        created += ok
        skipped += not ok
    return created, skipped


def ensure_download_volume(catalog: Catalog, cfg: FetchConfig, project_root: Path) -> None:
    target = Path(cfg.target_dir)
    if not target.is_absolute():
        target = project_root / target
    target.mkdir(parents=True, exist_ok=True)
    catalog.register_volume(
        cfg.volume_id, target,
        label="ERA5 API 下载目录", notes="由 dbas fetch 创建")


def download_one(catalog: Catalog, params: dict, project_root: Path,
                 capture_dir: Path | None = None, task_id: str | None = None) -> dict:
    """执行单个下载任务（由 Runner 调度）。"""
    try:
        import cdsapi
    except ImportError as e:
        raise FetchError("缺少 cdsapi，请安装: pip install 'dbas[fetch]'" ) from e

    root = catalog.resolve_volume_root(params["volume_id"])
    target = root / params["target_rel"]
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")

    if CLIENT_FACTORY is not None:
        client = CLIENT_FACTORY()
    else:
        client = cdsapi.Client(quiet=True, wait_until_complete=True, timeout=3600)
    if capture_dir is not None:
        from dbas.fetch.capture import CaptureConfig, JsonlWriter, ObservedCdsClient
        client = ObservedCdsClient(client, JsonlWriter(CaptureConfig(Path(capture_dir))),
                                  task_id=task_id)
    request_id: str | None = None
    try:
        client.retrieve(params["dataset"], params["request"], str(part))
        # 抓包包装会记下服务端 request_id：进程被杀后可用它直接取结果，
        # 不必重新排到 9000+ 人的队列尾部。
        request_id = getattr(client, "last_request_id", None)
    except Exception as e:  # noqa: BLE001 cdsapi 异常类型多，统一转成自己的错误类型
        part.unlink(missing_ok=True)
        msg = f"CDS 下载失败: {type(e).__name__}: {e}"
        # 永久失败不重试：重试只会白等 max_attempts 轮，真正原因反而被淹没。
        if is_permanent_fetch_error(msg):
            raise FetchPermanentError(
                f"{msg}（判定为不可重试：请检查请求是否超过成本上限、"
                f"变量名是否正确、或是否需要先在网页接受数据集条款）") from e
        raise FetchError(msg) from e

    if not part.exists() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise FetchError("下载产物为空，已删除 .part 文件")
    os.replace(part, target)             # 原子改名，正式产物不可能是半截文件

    st = target.stat()
    file_id, _ = catalog.upsert_file(
        params["volume_id"], params["target_rel"].replace("\\", "/"),
        st.st_size, st.st_mtime)
    catalog.enqueue_task(
        "scan_metadata",
        {"file_id": file_id, "volume_id": params["volume_id"],
         "rel_path": params["target_rel"]},
        dedup_key=f"scan_metadata:{file_id}:{int(st.st_mtime)}")
    result = {"path": str(target), "size_bytes": st.st_size}
    if request_id:
        # 回传给调用方（runner）落库，供下次复用与追溯
        result["cds_request_id"] = request_id
    return result
