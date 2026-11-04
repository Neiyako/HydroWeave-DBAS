"""ResearchDataHub（RDH）交付导入驱动（fetch）。

为什么要有这一层：RDH 负责「从 CDS 采集 + 完整性校验 + 出交付清单」，
DBAS 负责「登记资产 + 入队元数据解析」。两者都是 CLI 工具，最稳的接缝是
子进程 + 交付清单 JSON，而不是把 RDH 当库导入——RDH 的 console script 已坏，
只有 ``python -m researchdatahub.cli.main`` 可用，本模块的所有命令都走这个入口。

链路：

    rdh plan create <request.yaml>   # 联网查目录，生成计划（不下载）
    rdh task run <plan_id>           # 真正下载 + 校验
    rdh report generate <request_id> # 写 <storage_root>/deliveries/<delivery_id>.json
    → 定位最新交付 JSON（按 mtime，且 request_id 必须匹配）
    → 校验 complete 为 True 且 files 非空
    → 逐文件 SHA-256 + size 核对（path 相对 storage_root）
    → 原子 copy（.part → os.replace）到 target_dir/<dataset_id>/<文件名>
    → catalog: register_volume + upsert_file + ensure_dataset + set_file_dataset
               + enqueue scan_metadata（dedup_key 含 file_id + mtime）
    → capture_dir/events.jsonl（脱敏，复用 capture.JsonlWriter）

边界：

- 绝不修改/移动 RDH 原始交付文件（它们 0444 只读，且是 RDH 的审计凭证）；
  只读源、写副本。
- 事件里只记相对路径与哈希，不记绝对家目录路径（便于随仓库归档）。
- 不读、不打印 CDS 凭据：凭据由 RDH 自己从 ``~/.cdsapirc`` 解析；DBAS 只透传
  环境变量（含 ``PYTHONPATH=src``，让 ``python -m researchdatahub...`` 能找到包）。
- 幂等：同一交付重跑 → 目标文件 SHA-256 相同则跳过拷贝与重复入队；同名不同内容
  → 明确报错而不是覆盖（覆盖会静默改掉已登记资产的身份）。
- 单个文件失败时，之前已导入的文件保持已登记状态：重跑会跳过它们继续补齐，
  不会产生半截文件（每个文件都是 .part + os.replace 原子落盘）。

测试注入点：``runner: Callable[[list[str], float], str]``，返回命令 stdout；
默认实现走 ``subprocess.run``（cwd=rdh_repo，env 继承 + PYTHONPATH=src）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dbas.fetch.capture import CaptureConfig, JsonlWriter, redact
from dbas_core.catalog.repository import Catalog
from dbas_core.exceptions import ConfigError, FetchError
from dbas_core.logging import get_logger

log = get_logger("fetch.rdh")

# 子进程执行器：入参 (argv, timeout_s)，返回 stdout；非零退出必须抛 FetchError。
Runner = Callable[[list[str], float], str]

_MODULE = "researchdatahub.cli.main"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# plan create 的 plan_id 在 rich 表格标题「采集计划：<plan_id>」里；
# 兼容「下一步：rdh task run <plan_id>」这种提示行，避免 rich 改版后直接解析失败。
_PLAN_ID_PATTERNS = (
    re.compile(r"采集计划[：:]\s*([A-Za-z0-9][A-Za-z0-9._:-]*)"),
    re.compile(r"rdh\s+task\s+run\s+([A-Za-z0-9][A-Za-z0-9._:-]*)"),
)
_REQUEST_ID_JSON = re.compile(r'"request_id"\s*:\s*"([^"]+)"')
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class RdhFetchConfig:
    """rdh fetch 配置（字段与 configs/fetch/rdh_sample.yaml 对应）。"""

    rdh_repo: Path
    request_file: Path
    rdh_python: str = ""                  # 空 = <rdh_repo>/.venv/bin/python
    storage_root: Path = Path("~/.rdh")
    target_dir: str = "data/downloads"    # 相对项目根
    volume_id: str = "downloads"
    dataset_id: str = "era5_cloud_water"
    dataset_label: str = ""               # 空 = dataset_id
    dataset_description: str = ""
    capture_dir: Path | None = None
    timeout_s: float = 5400.0
    plan_id: str | None = None            # 显式指定 = 复用已有计划，跳过 plan create

    @classmethod
    def from_yaml(cls, path: Path) -> RdhFetchConfig:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"解析 {path} 失败: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} 顶层必须是映射（key: value）")
        repo = raw.get("rdh_repo")
        if not repo:
            raise ConfigError("rdh fetch 配置缺少必填字段: rdh_repo")
        request_file = raw.get("request_file")
        if not request_file:
            raise ConfigError("rdh fetch 配置缺少必填字段: request_file")
        capture = raw.get("capture_dir")
        plan = raw.get("plan_id")
        return cls(
            rdh_repo=Path(str(repo)).expanduser(),
            request_file=Path(str(request_file)).expanduser(),
            rdh_python=str(raw.get("rdh_python") or ""),
            storage_root=Path(str(raw.get("storage_root") or "~/.rdh")).expanduser(),
            target_dir=str(raw.get("target_dir") or "data/downloads"),
            volume_id=str(raw.get("volume_id") or "downloads"),
            dataset_id=str(raw.get("dataset_id") or "era5_cloud_water"),
            dataset_label=str(raw.get("dataset_label") or ""),
            dataset_description=str(raw.get("dataset_description") or ""),
            capture_dir=Path(str(capture)).expanduser() if capture else None,
            timeout_s=float(raw.get("timeout_s", 5400) or 5400),
            plan_id=str(plan).strip() if plan else None)

    def resolved_target_dir(self, project_root: Path) -> Path:
        target = Path(self.target_dir).expanduser()
        return target if target.is_absolute() else project_root / target


# --------------------------------------------------------------------------
# 路径 / 子进程
# --------------------------------------------------------------------------

def _resolve_path(path: Path, project_root: Path) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else (project_root / p)


def _resolve_request_file(path: Path, project_root: Path, rdh_repo: Path) -> Path:
    """需求文件解析顺序：绝对路径 → 项目根相对 → RDH 仓相对。

    RDH 的示例需求文件放在 RDH 自己的 ``examples/`` 下，配置里写
    ``examples/xxx.yaml`` 更自然，所以项目根找不到时回落到 rdh_repo。
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    under_project = project_root / p
    if under_project.is_file():
        return under_project
    return rdh_repo / p


def _resolve_interpreter(raw: str, rdh_repo: Path, project_root: Path) -> str:
    text = raw.strip()
    if not text:
        return str(rdh_repo / ".venv" / "bin" / "python")
    p = Path(text).expanduser()
    # 带路径分隔符的按路径解析；纯命令名（如 python3）保持原样交给 PATH。
    if len(p.parts) == 1 and not text.startswith("."):
        return text
    return str(_resolve_path(p, project_root))


def _argv(python: str, *args: str) -> list[str]:
    return [python, "-m", _MODULE, *args]


def _default_runner(cfg: RdhFetchConfig, rdh_repo: Path) -> Runner:
    """真实执行器：cwd=rdh_repo，env 继承 + PYTHONPATH=src（相对 rdh_repo）。

    RDH 未安装成包（console script 已坏），只能靠 PYTHONPATH 指到它的 ``src/``
    才能 ``python -m researchdatahub.cli.main``；覆盖而不替换原 PYTHONPATH，
    以免宿主环境里已有的依赖路径失效。
    """
    env = os.environ.copy()
    prior = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = "src" if not prior else f"src{os.pathsep}{prior}"

    def run(argv: list[str], timeout_s: float) -> str:
        try:
            proc = subprocess.run(argv, cwd=str(rdh_repo), env=env,
                                  capture_output=True, text=True,
                                  timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FetchError(
                f"调用 RDH 失败: {type(exc).__name__}: {exc}"
                f"（cwd={rdh_repo}，命令={' '.join(argv[:3])}）") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise FetchError(
                f"RDH 命令退出码 {proc.returncode}: {' '.join(argv[3:])}\n"
                f"{redact(tail)}")
        return proc.stdout
    return run


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _parse_plan_id(output: str) -> str | None:
    text = _strip_ansi(output)
    for pattern in _PLAN_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).rstrip(".:：")
    return None


def _request_id_from_request_file(path: Path) -> str | None:
    """从需求 YAML 取 request_id（RDH 的 DatasetRequest 把它列为必填 Identifier）。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if isinstance(raw, dict):
        value = raw.get("request_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _request_id_from_plan(runner: Runner, cfg: RdhFetchConfig, python: str,
                          plan_id: str) -> str | None:
    """兜底：从 ``plan show <plan_id> --json`` 取 request_id。"""
    output = runner(_argv(python, "plan", "show", plan_id, "--json"), cfg.timeout_s)
    text = _strip_ansi(output).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _REQUEST_ID_JSON.search(text)
        return match.group(1) if match else None
    if isinstance(payload, dict):
        value = payload.get("request_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# --------------------------------------------------------------------------
# 哈希 / 交付清单
# --------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_sha256(entry: dict[str, Any], rel_source: str) -> str:
    """交付清单的 ``checksum`` 形如 ``"sha256:<hex>"``；兼容纯 hex 与 dict 形式。"""
    raw = entry.get("checksum")
    if raw is None:
        raw = entry.get("sha256")
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("sha256")
    if not isinstance(raw, str) or not raw.strip():
        raise FetchError(f"交付清单缺少 checksum/sha256: {rel_source}")
    text = raw.strip()
    if ":" in text:
        algo, _, hexdigest = text.partition(":")
        if algo.strip().lower() != "sha256":
            raise FetchError(
                f"不支持的校验算法 {algo!r}: {rel_source}（本驱动只接受 sha256）")
        text = hexdigest
    text = text.strip().lower()
    if not _SHA256_HEX.match(text):
        raise FetchError(f"交付清单 sha256 格式非法: {rel_source}: {raw!r}")
    return text


def locate_delivery(storage_root: Path, request_id: str) -> tuple[Path, dict[str, Any]]:
    """定位该请求最新的交付清单（按 mtime 倒序，第一个 request_id 匹配者）。

    只按 mtime 取「最新」不够：同一个 storage_root 下可能有别的请求的交付，
    误导入会把不属于本请求的数据登记进来，所以必须校验 request_id。
    """
    delivery_dir = storage_root / "deliveries"
    if not delivery_dir.is_dir():
        raise FetchError(
            f"交付清单目录不存在: {delivery_dir}（rdh report generate 未产出交付清单？）")
    candidates = sorted(delivery_dir.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    skipped: list[str] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append(f"{path.name}: {type(exc).__name__}")
            continue
        if not isinstance(payload, dict):
            skipped.append(f"{path.name}: 顶层不是对象")
            continue
        rid = payload.get("request_id")
        if rid in (None, request_id):
            return path, payload
    detail = f"；跳过: {'; '.join(skipped[:3])}" if skipped else ""
    raise FetchError(
        f"未找到请求 {request_id} 的交付清单（{delivery_dir} 下 {len(candidates)} 个 JSON）"
        f"{detail}")


def _validate_delivery(payload: dict[str, Any]) -> list[Any]:
    """校验交付清单可导入：complete 必须为 True，files 非空。"""
    if payload.get("complete") is not True:
        notes = str(payload.get("notes") or "").strip() or "（无说明）"
        excluded = payload.get("excluded")
        n_excluded = len(excluded) if isinstance(excluded, list) else 0
        raise FetchError(
            f"交付清单不完整（complete={payload.get('complete')!r}）：{notes}；"
            f"排除文件 {n_excluded} 个。不导入未通过完整性校验的数据。")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise FetchError("交付清单 files 为空：没有可导入的文件")
    return files


def _write_event(writer: JsonlWriter | None, event: dict[str, Any]) -> None:
    if writer is not None:
        writer.write(event)


def _atomic_copy(source: Path, target: Path, expected_sha256: str) -> None:
    """先拷到 ``<name>.part``，核对副本哈希后再 os.replace，正式产物不可能是半截。"""
    part = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(source, part)
        actual = sha256_file(part)
        if actual != expected_sha256:
            raise FetchError(
                f"副本校验失败（写入不完整？）: {part} 期望 {expected_sha256}，实际 {actual}")
        os.replace(part, target)
    except OSError as exc:
        raise FetchError(f"拷贝交付文件失败: {source} -> {target}: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run_rdh_fetch(catalog: Catalog | None, cfg: RdhFetchConfig, project_root: Path,
                  *, runner: Runner | None = None,
                  capture_dir: Path | None = None,
                  dry_run: bool = False) -> dict[str, Any]:
    """跑完整 RDH 链路并把交付导入 DBAS；返回结构化 summary。

    :param catalog: 目录库；``dry_run=True`` 时可传 None。
    :param runner: 子进程执行器（测试注入点）；None = 默认 subprocess.run。
    :param capture_dir: 覆盖配置里的 capture_dir（CLI --capture-dir）。
    :param dry_run: 只校验配置并打印将执行的命令，不联网、不写库。
    """
    project_root = Path(project_root)
    rdh_repo = _resolve_path(cfg.rdh_repo, project_root)
    if not rdh_repo.is_dir():
        raise FetchError(f"RDH 仓库不存在或不是目录: {rdh_repo}")
    request_file = _resolve_request_file(cfg.request_file, project_root, rdh_repo)
    if not request_file.is_file():
        raise FetchError(f"RDH 需求文件不存在: {request_file}（rdh_repo={rdh_repo}）")
    python = _resolve_interpreter(cfg.rdh_python, rdh_repo, project_root)
    if runner is None and not (Path(python).is_file() or shutil.which(python)):
        raise FetchError(
            f"找不到 RDH 解释器: {python}（配置 rdh_python，或先建 RDH 的 .venv）")

    storage_root = _resolve_path(cfg.storage_root, project_root)
    target_root = cfg.resolved_target_dir(project_root)
    effective_capture = capture_dir if capture_dir is not None else cfg.capture_dir
    capture_path = (_resolve_path(Path(effective_capture), project_root)
                    if effective_capture is not None else None)

    request_id = _request_id_from_request_file(request_file)
    plan_id = (cfg.plan_id or "").strip() or None

    if dry_run:
        # dry-run 不建目录、不写事件、不碰 catalog
        commands = []
        if plan_id is None:
            commands.append(_argv(python, "plan", "create", str(request_file)))
        commands.append(_argv(python, "task", "run", plan_id or "<plan_id>"))
        commands.append(_argv(python, "report", "generate", request_id or "<request_id>"))
        return {
            "dry_run": True,
            "rdh_repo": str(rdh_repo),
            "rdh_python": python,
            "request_file": str(request_file),
            "request_id": request_id,
            "plan_id": plan_id,
            "storage_root": str(storage_root),
            "target_dir": str(target_root),
            "volume_id": cfg.volume_id,
            "dataset_id": cfg.dataset_id,
            "capture_dir": str(capture_path) if capture_path is not None else None,
            "commands": commands,
        }

    writer = JsonlWriter(CaptureConfig(capture_path)) if capture_path is not None else None
    if runner is None:
        runner = _default_runner(cfg, rdh_repo)

    # 1) 计划：复用（plan_id）或现建
    if plan_id is not None:
        plan_mode = "reuse"
    else:
        output = runner(_argv(python, "plan", "create", str(request_file)), cfg.timeout_s)
        plan_id = _parse_plan_id(output)
        if plan_id is None:
            tail = _strip_ansi(output).strip()[-300:]
            raise FetchError(f"无法从 rdh plan create 输出解析 plan_id；输出片段: {tail!r}")
        plan_mode = "create"
    if request_id is None:
        request_id = _request_id_from_plan(runner, cfg, python, plan_id)
    if request_id is None:
        raise FetchError(
            "无法确定 request_id：需求文件缺少 request_id，plan show 也没给出。"
            "请在需求 YAML 里写 request_id（RDH 的必填字段）。")
    _write_event(writer, {"event": "rdh_plan", "plan_id": plan_id,
                          "request_id": request_id,
                          "request_file": request_file.name, "mode": plan_mode})
    log.info("rdh plan %s（%s）request=%s", plan_id, plan_mode, request_id)

    # 2) 执行任务 + 生成报告（报告会写出交付清单 JSON）
    runner(_argv(python, "task", "run", plan_id), cfg.timeout_s)
    _write_event(writer, {"event": "rdh_task", "plan_id": plan_id,
                          "request_id": request_id, "command": "task run"})
    runner(_argv(python, "report", "generate", request_id), cfg.timeout_s)

    # 3) 定位并校验交付清单
    delivery_path, payload = locate_delivery(storage_root, request_id)
    files = _validate_delivery(payload)
    total_bytes = payload.get("total_bytes")
    _write_event(writer, {
        "event": "rdh_delivery", "delivery_id": payload.get("delivery_id"),
        "request_id": request_id, "complete": True,
        "files": len(files),
        "total_bytes": total_bytes if isinstance(total_bytes, int) else None,
        "delivery": delivery_path.name})
    log.info("交付清单 %s：%d 个文件", delivery_path.name, len(files))

    # 4) 目标卷/数据集先登记（幂等），再逐文件原子拷贝并登记
    if catalog is None:
        raise FetchError("导入需要 Catalog：dry_run 之外不能为空")
    dataset_dir = target_root / cfg.dataset_id
    dataset_dir.mkdir(parents=True, exist_ok=True)
    catalog.register_volume(cfg.volume_id, target_root,
                            label="ResearchDataHub 交付目录",
                            notes="由 dbas fetch rdh 创建")
    catalog.ensure_dataset(
        cfg.dataset_id,
        cfg.dataset_label or cfg.dataset_id,
        cfg.dataset_description or "由 ResearchDataHub 从 CDS 采集、SHA-256 校验后导入的交付文件",
        source="ResearchDataHub/CDS")

    root_resolved = storage_root.resolve()
    imported = skipped = tasks_created = 0
    records: list[dict[str, Any]] = []
    used_names: dict[str, str] = {}      # 目标文件名 → sha256（同名不同内容时加哈希后缀）
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise FetchError(f"交付清单 files[{index}] 不是对象")
        rel_source = entry.get("path")
        if not isinstance(rel_source, str) or not rel_source.strip():
            raise FetchError(f"交付清单 files[{index}] 缺少 path")
        if Path(rel_source).is_absolute():
            raise FetchError(f"交付清单 path 必须是相对路径: {rel_source}")
        if entry.get("path_escapes_base"):
            raise FetchError(f"交付清单 path 逃逸 storage_root: {rel_source}")
        source = (storage_root / rel_source).resolve()
        if not source.is_relative_to(root_resolved):
            raise FetchError(f"交付清单 path 逃逸 storage_root: {rel_source}")
        if not source.is_file():
            raise FetchError(f"交付文件不存在（清单已列出但磁盘缺失）: {source}")

        expected_sha = _expected_sha256(entry, rel_source)
        expected_size = entry.get("size_bytes")
        actual_size = source.stat().st_size
        if isinstance(expected_size, int) and expected_size != actual_size:
            raise FetchError(
                f"交付文件大小不符: {rel_source} 清单 {expected_size} 字节，实际 {actual_size} 字节")
        digest = sha256_file(source)
        if digest != expected_sha:
            raise FetchError(
                f"交付文件 SHA-256 不符: {rel_source} 清单 {expected_sha}，实际 {digest}")

        name = Path(rel_source).name
        rel_name = name
        if name in used_names and used_names[name] != digest:
            # 同一交付里重名但内容不同：用内容哈希加后缀，绝不互相覆盖
            stem, suffix = Path(name).stem, Path(name).suffix
            rel_name = f"{stem}.{digest[:8]}{suffix}"
        used_names[rel_name] = digest
        target = dataset_dir / rel_name
        rel_target = f"{cfg.dataset_id}/{rel_name}"

        if target.exists():
            existing = sha256_file(target)
            if existing != digest:
                raise FetchError(
                    f"目标文件已存在且内容不同，拒绝覆盖: {target}"
                    f"（已有 sha256={existing[:12]}…，交付 sha256={digest[:12]}…）。"
                    "请先人工确认要保留哪个版本。")
            action = "skipped"
            skipped += 1
        else:
            _atomic_copy(source, target, digest)
            action = "imported"
            imported += 1

        st = target.stat()
        file_id, changed = catalog.upsert_file(
            cfg.volume_id, rel_target, st.st_size, st.st_mtime)
        catalog.set_file_dataset(file_id, cfg.dataset_id)
        _, created = catalog.enqueue_task(
            "scan_metadata",
            {"file_id": file_id, "volume_id": cfg.volume_id, "rel_path": rel_target},
            dedup_key=f"scan_metadata:{file_id}:{int(st.st_mtime)}")
        tasks_created += created
        records.append({"source": rel_source, "target": rel_target,
                        "sha256": digest, "size_bytes": st.st_size,
                        "action": action, "file_id": file_id,
                        "changed": changed, "task_created": created})
        _write_event(writer, {"event": "import", "dataset_id": cfg.dataset_id,
                              "volume_id": cfg.volume_id, "source": rel_source,
                              "target": rel_target, "sha256": digest,
                              "size_bytes": st.st_size, "action": action,
                              "file_id": file_id, "task_created": created})

    summary = {
        "dry_run": False,
        "plan_id": plan_id,
        "request_id": request_id,
        "delivery_id": payload.get("delivery_id"),
        "delivery": delivery_path.name,
        "storage_root": str(storage_root),
        "target_dir": str(target_root),
        "dataset_dir": str(dataset_dir),
        "volume_id": cfg.volume_id,
        "dataset_id": cfg.dataset_id,
        "files_total": len(files),
        "imported": imported,
        "skipped": skipped,
        "tasks_created": tasks_created,
        "files": records,
    }
    log.info("RDH 导入完成：新增 %d，跳过 %d，入队 %d", imported, skipped, tasks_created)
    return summary
