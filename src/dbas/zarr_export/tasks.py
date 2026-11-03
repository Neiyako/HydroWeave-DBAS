"""Zarr 派生任务（type=zarr_export）：源 .nc → 数组层 store → 登记进 catalog。

任务参数（task.params）：
    file_id / volume_id / rel_path   源文件（来自 catalog）
    target       查询模式：field_series / point_series / area_stats / level_profile
    shards       分片倍数（1=不分片）
    region       若给定，先按该区域裁切再导出；None 表示整幅
    variables    变量白名单；None 表示全部
    force        覆盖已存在的 store
    array_volume 数组层卷 ID（默认 analysis）

登记语义（与 subset_one 一致，并补上一处 Zarr 特有的证据）：
    1) upsert_file 把 **store 目录** 作为一个条目登记进数组层卷；
    2) set_fingerprint 写入**清单哈希**（manifest digest），不是单文件 sha256；
    3) 清单全文写成 sidecar JSON 落进 reports 目录，可随时对账；
    4) 入队 scan_metadata —— store 目录不是 netCDF，解析会明确记为非 netCDF，
       不会污染变量索引。
"""
from __future__ import annotations

from pathlib import Path

import dask
import xarray as xr

from dbas_core.catalog.repository import Catalog
from dbas_core.exceptions import AnalysisError
from dbas_core.logging import get_logger
from dbas.zarr_export.config_ext import install_zarr_config
from dbas.zarr_export.manifest import scan_store
from dbas.zarr_export.store import (ChunkPlan, estimate_chunk_bytes, export_store,
                                    plan_chunks)

install_zarr_config()  # 保证 cfg.array_dir / cfg.section("zarr") 可用

log = get_logger("zarr_export")


def store_rel_path(rel_path: str, target: str) -> str:
    """数组层里的稳定路径：<target>/<原相对路径>.zarr。

    放 target 子目录的理由：同一源文件可以并存多种分块方案（field_series
    与 point_series 的物理数据相同但布局不同），互不覆盖、可对比实测。
    """
    p = Path(rel_path)
    return f"{target}/{p.with_suffix('').as_posix()}.zarr"


def _open_source(src: Path, *, chunk_mb: int, region=None, variables=None):
    with dask.config.set({"array.chunk-size": f"{chunk_mb}MiB",
                          "scheduler": "single-threaded"}):
        ds = xr.open_dataset(src, chunks="auto", decode_times=True)
    if variables:
        want = {str(v) for v in variables}
        keep = [str(v) for v in ds.data_vars if str(v) in want]
        if not keep:
            ds.close()
            raise AnalysisError(
                f"变量白名单 {sorted(want)} 与文件变量 "
                f"{sorted(str(v) for v in ds.data_vars)} 无交集")
        ds = ds[keep]
    if region is not None:
        from dbas_core.analysis.subset import normalize_axes, _coord_name
        ds, _notes = normalize_axes(ds)
        lat = _coord_name(ds, {"lat", "latitude", "y"}, "纬度")
        lon = _coord_name(ds, {"lon", "longitude", "x"}, "经度")
        ds = ds.sel({lat: slice(region.lat_min, region.lat_max),
                     lon: slice(region.lon_min, region.lon_max)})
        if ds.sizes.get(lat, 0) == 0 or ds.sizes.get(lon, 0) == 0:
            ds.close()
            raise AnalysisError(f"region({region.label}) 与网格无交点")
    return ds


def ensure_array_volume(catalog: Catalog, cfg) -> str:
    """幂等创建数组层目录并注册为数据卷。返回卷 ID。

    handler 自身也要调用它：任务可能被 CLI 以外的方式入队（脚本、
    另一个进程、重放旧任务），不能假设 enqueue 阶段已经注册过卷，
    否则 upsert_file 会撞 FOREIGN KEY 约束。
    """
    zcfg = cfg.section("zarr")
    cfg.array_dir.mkdir(parents=True, exist_ok=True)
    catalog.register_volume(zcfg["volume_id"], cfg.array_dir,
                            label="数组层卷（Zarr 派生）",
                            notes="由 zarr_export 创建；源卷只读、store 可再生")
    return zcfg["volume_id"]


def zarr_export_one(params: dict, catalog: Catalog, ctx=None, *, cfg) -> dict:
    """任务队列 handler。任何异常都向上抛，由 Runner 按既有重试语义处理。"""
    if ctx:
        ctx.heartbeat()
    acfg = cfg.section("analysis")
    zcfg = cfg.section("zarr")

    src_root = catalog.resolve_volume_root(params["volume_id"])
    src = src_root / params["rel_path"]
    if not src.exists():
        raise AnalysisError(f"源文件不存在（卷可能离线）: {src}")

    target = params.get("target") or zcfg["default_target"]
    region = None
    if params.get("region"):
        from dbas_core.analysis.regions import get_region
        region = get_region(params["region"])

    ensure_array_volume(catalog, cfg)
    out_rel = store_rel_path(params["rel_path"], target)
    dst = Path(cfg.array_dir) / out_rel

    ds = _open_source(src, chunk_mb=acfg["chunk_mb"], region=region,
                      variables=params.get("variables"))
    try:
        plan: ChunkPlan = plan_chunks(
            ds, target, chunk_elements=int(zcfg["chunk_elements"]))
        est = estimate_chunk_bytes(ds, plan)
        stats = export_store(
            ds, dst, plan,
            shards=params.get("shards") or zcfg["shards"],
            complevel=int(zcfg["complevel"]),
            consolidate=bool(zcfg["consolidate"]),
            force=bool(params.get("force")),
            attrs={"dbas_source_file": f"{params['volume_id']}:{params['rel_path']}",
                   "dbas_target": target,
                   "dbas_plan": plan.rationale})
    finally:
        ds.close()

    stats["chunk_bytes_estimate"] = est
    stats["plan"] = plan.as_dict()
    stats["rel_path"] = out_rel

    if stats["skipped"]:
        return stats

    # -- 登记进 catalog：store 作为数组层的一个条目 -----------------------
    man = scan_store(dst)
    array_volume = params.get("array_volume") or zcfg["volume_id"]
    st = dst.stat()
    file_id, _ = catalog.upsert_file(array_volume, out_rel, st.st_size, st.st_mtime)
    catalog.set_fingerprint(file_id, "manifest-sha256", man.digest)

    # 清单落盘：随时可对账“store 是否被改过、改了哪几个 chunk”
    sidecar = Path(cfg.reports_dir) / "zarr" / f"{file_id}.manifest.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_suffix(".json.part")
    tmp.write_text(man.to_json(), encoding="utf-8")
    tmp.replace(sidecar)

    stats["artifact_file_id"] = file_id
    stats["manifest"] = man.summary()
    stats["manifest_path"] = str(sidecar)
    if ctx:
        ctx.heartbeat()
    return stats


def reconcile_store(params: dict, catalog: Catalog, ctx=None, *, cfg) -> dict:
    """对账任务（type=zarr_reconcile）：重扫 store，对比登记时的清单哈希。

    回答的问题：这个数组层条目是否被外部改动过（重新运行导出、手工删 chunk、
    网盘同步冲突）。不改 catalog 结论，只产出证据。
    """
    rel = params["rel_path"]
    root = Path(cfg.array_dir) / rel
    man = scan_store(root)
    file_id = params.get("file_id")
    rec = catalog.get_file(file_id) if file_id else None
    recorded = (rec or {}).get("fingerprint")
    return {
        "rel_path": rel,
        "recorded_digest": recorded,
        "current_digest": man.digest,
        "match": recorded == man.digest,
        "summary": man.summary(),
    }


__all__ = ["zarr_export_one", "reconcile_store", "store_rel_path"]
