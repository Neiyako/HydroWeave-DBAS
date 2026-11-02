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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from dbas_core.catalog.repository import Catalog
from dbas_core.exceptions import ConfigError, FetchError
from dbas_core.logging import get_logger

log = get_logger("fetch")

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

    @classmethod
    def from_yaml(cls, path: Path) -> "FetchConfig":
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
            n, w, s, e = (float(x) for x in area)
            # CDS 约定 north > south；反了会导致 MARS 返回空数据（MarsNoDataError）
            if n <= s:
                raise ConfigError(
                    f"area 顺序为 [北, 西, 南, 东]，但 北({n}) ≤ 南({s})；"
                    f"应写成 [{s}, {min(w, e)}, {n}, {max(w, e)}] 这类北>南的形式")
        return cls(
            dataset=str(raw["dataset"]),
            variables=[str(v) for v in raw["variables"]],
            years=[str(y) for y in raw["years"]],
            months=[str(m) for m in raw["months"]],
            area=raw.get("area"),
            target_dir=str(raw.get("target_dir", "data/downloads")),
            volume_id=str(raw.get("volume_id", "downloads")),
            product_type=raw.get("product_type"),
            extra_request=raw.get("extra_request") or {})

    def build_requests(self) -> list[dict[str, Any]]:
        """每个 (变量集 × 年 × 月) 一个最小请求，失败重试代价小。"""
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
                reqs.append({"year": year, "month": month, "request": req})
        return reqs


def request_dedup_key(cfg: FetchConfig, req_meta: dict[str, Any]) -> str:
    import hashlib
    import json
    payload = json.dumps({"dataset": cfg.dataset, **req_meta["request"]},
                         sort_keys=True, ensure_ascii=False)
    return "download:" + hashlib.sha1(payload.encode()).hexdigest()[:16]


def enqueue_fetch_tasks(catalog: Catalog, cfg: FetchConfig) -> tuple[int, int]:
    """把下载计划转成任务入队。返回 (新增数, 跳过数)。"""
    created = skipped = 0
    for req_meta in cfg.build_requests():
        dedup = request_dedup_key(cfg, req_meta)
        target_rel = (
            f"{cfg.dataset}/{req_meta['year']}{req_meta['month']}"
            f"_{cfg.variables[0]}.nc" if len(cfg.variables) == 1
            else f"{cfg.dataset}/{req_meta['year']}{req_meta['month']}_multi.nc")
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
        client = ObservedCdsClient(client, JsonlWriter(CaptureConfig(Path(capture_dir))), task_id=task_id)
    try:
        client.retrieve(params["dataset"], params["request"], str(part))
    except Exception as e:  # noqa: BLE001 cdsapi 异常类型多，统一转 FetchError
        part.unlink(missing_ok=True)
        raise FetchError(f"CDS 下载失败: {type(e).__name__}: {e}") from e

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
    return {"path": str(target), "size_bytes": st.st_size}
