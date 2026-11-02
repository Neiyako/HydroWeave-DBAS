"""扫描模式实现：inventory / incremental / verify / checksum / metadata。

共同原则：
- 目录遍历用 os.scandir 生成器流式处理，不把文件列表整体载入内存。
- 每个文件处理完立即提交数据库（小事务），进程被杀最多丢一个文件的状态。
- 卷不可访问（VolumeOffline）向上抛出，由调用方决定整体行为。
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from dbas_core.catalog.repository import Catalog, VolumeOffline
from dbas_core.exceptions import ScanError
from dbas_core.logging import get_logger
from dbas_core.inspection.netcdf_meta import NetcdfParseError, parse_netcdf_header

log = get_logger("scanner")

CHECKSUM_CHUNK = 1024 * 1024


@dataclass
class ScanStats:
    seen: int = 0
    ok: int = 0
    failed: int = 0
    new: int = 0
    changed: int = 0
    missing_pending: int = 0
    errors: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def elapsed_s(self) -> float:
        return time.time() - self.started


def _iter_files(root: Path, extensions: list[str],
                exclude_dirs: list[str] = ()):
    """流式遍历目录树，产出 (rel_path, full_path, st)。

    extensions 含 "*" 时收录全部文件；exclude_dirs 按目录名剪枝。
    """
    exts = {e.lower() for e in extensions}
    take_all = "*" in exts
    skip_dirs = set(exclude_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for fn in sorted(filenames):
            suffix = Path(fn).suffix.lower()
            if not take_all and suffix not in exts:
                continue
            full = Path(dirpath) / fn
            try:
                st = full.stat()
            except OSError as e:
                log.warning("无法 stat %s: %s", full, e)
                continue
            yield full.relative_to(root).as_posix(), full, st


# --------------------------------------------------------------------------
# inventory / incremental
# --------------------------------------------------------------------------

def scan_inventory(catalog: Catalog, volume_id: str, extensions: list[str],
                   *, enqueue_metadata: bool = False,
                   incremental: bool = False,
                   netcdf_extensions: list[str] | None = None,
                   exclude_dirs: list[str] = ()) -> ScanStats:
    """盘点卷内文件。incremental=True 时只把新增/变化的文件送去解析。

    extensions 可为 "*"（全量清单化）；此时只有后缀落在 netcdf_extensions 内的
    文件才会入队解析，其余标记为 not_netcdf 并记录 format=后缀，作为可检索的
    清单条目而非解析积压。

    missing_pending 语义：只有在「卷确认在线 + walk 完整跑完无异常」时才会
    把本次未发现的既有文件标记为待确认缺失；任何中断都不会触发该标记。
    """
    root = catalog.resolve_volume_root(volume_id)
    exts = {e.lower() for e in extensions}
    take_all = "*" in exts
    nc_exts = {e.lower() for e in (netcdf_extensions or extensions)}
    stats = ScanStats()
    seen_rel_paths: set[str] = set()
    for rel, full, st in _iter_files(root, extensions, exclude_dirs):
        seen_rel_paths.add(rel)
        stats.seen += 1
        file_id, changed = catalog.upsert_file(
            volume_id, rel, st.st_size, st.st_mtime)
        if changed:
            stats.changed += 1
        if incremental and not changed:
            continue
        if changed:
            stats.new += 1
        if Path(rel).suffix.lower() not in nc_exts:
            catalog.mark_not_netcdf(file_id, Path(rel).suffix.lstrip(".").lower()
                                    or "no_extension")
            continue
        if enqueue_metadata and changed:
            catalog.enqueue_task(
                "scan_metadata",
                {"file_id": file_id, "volume_id": volume_id, "rel_path": rel},
                # dedup_key 含完整 mtime：文件更新后允许重新入队解析
                dedup_key=f"scan_metadata:{file_id}:{st.st_mtime:.6f}")
    # walk 完整结束才允许标记缺失
    if not incremental:
        # 全量清单模式纳入所有已知文件；普通模式只纳入本次扫描负责的扩展名
        known = {f["rel_path"] for f in catalog.iter_files(volume_id=volume_id)
                 if take_all or Path(f["rel_path"]).suffix.lower() in exts}
        missing = list(known - seen_rel_paths)
        stats.missing_pending = catalog.mark_missing_pending(volume_id, missing)
        if missing:
            log.warning("%d 个既有文件本次未发现，已标记 missing_pending（待确认）",
                        len(missing))
    return stats


# --------------------------------------------------------------------------
# metadata（由任务队列逐文件调度）
# --------------------------------------------------------------------------

def scan_metadata_one(params: dict, catalog: Catalog, ctx=None) -> dict:
    """解析单个文件的 NetCDF 元数据并写库。返回结果摘要。"""
    if ctx: ctx.heartbeat()
    root = catalog.resolve_volume_root(params["volume_id"])
    path = root / params["rel_path"]
    try:
        parsed = parse_netcdf_header(path)
    except NetcdfParseError as e:
        catalog.mark_parse_failed(params["file_id"], str(e))
        raise ScanError(str(e)) from e
    catalog.update_parse_result(
        params["file_id"], fmt=parsed.format,
        time_min=parsed.time_min, time_max=parsed.time_max,
        lat=parsed.lat, lon=parsed.lon,
        variables=parsed.variables, dimensions=parsed.dimensions)
    return {
        "format": parsed.format,
        "n_vars": len(parsed.variables),
        "time_min": parsed.time_min,
        "time_max": parsed.time_max,
        "warnings": parsed.warnings,
    }


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def verify_one(params: dict, catalog: Catalog, ctx=None) -> dict:
    """验证文件当前可读性（打开并读取首块）。只读，不改内容。"""
    if ctx: ctx.heartbeat()
    root = catalog.resolve_volume_root(params["volume_id"])
    path = root / params["rel_path"]
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        ok = len(head) > 0
        catalog.set_check_result(params["file_id"],
                                 "ok" if ok else "failed",
                                 None if ok else "空文件")
        return {"readable": ok}
    except OSError as e:
        catalog.set_check_result(params["file_id"], "failed", str(e)[:300])
        raise ScanError(f"读取失败: {e}") from e


# --------------------------------------------------------------------------
# checksum
# --------------------------------------------------------------------------

def checksum_one(params: dict, catalog: Catalog, ctx=None) -> dict:
    """流式计算 sha256（分块读取，内存占用恒定 ≈ 1MB）。"""
    if ctx: ctx.heartbeat()
    root = catalog.resolve_volume_root(params["volume_id"])
    path = root / params["rel_path"]
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHECKSUM_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as e:
        catalog.set_check_result(params["file_id"], "failed", str(e)[:300])
        raise ScanError(f"校验和计算失败: {e}") from e
    catalog.set_fingerprint(params["file_id"], "sha256", h.hexdigest())
    return {"sha256": h.hexdigest()}


__all__ = ["ScanStats", "scan_inventory", "scan_metadata_one", "verify_one",
           "checksum_one", "VolumeOffline"]
