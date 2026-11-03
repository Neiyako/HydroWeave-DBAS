"""区域裁切（L1 crop）：源文件 → 研究区 ARD 文件。

设计原则：
- 源卷永远只读；输出写入独立分析卷（默认 data/analysis/<region>/<原相对路径>）。
- xarray + dask 惰性计算；single-threaded 调度器 + 小目标块（chunk_mb），
  在 8GB 内存机器上峰值可控。
- 幂等：输出已存在则跳过（--force 覆盖）；.part 临时文件 + 原子改名，
  中断不留半截文件。
- 坐标轴先归一化再裁切：降序纬度 → 升序；0–360 经度 → [-180,180) 并排序。
"""
from __future__ import annotations

import os
from pathlib import Path

import dask
import numpy as np
import xarray as xr

from dbas_core.analysis.regions import Bbox
from dbas_core.catalog.repository import Catalog
from dbas_core.exceptions import SubsetError
from dbas_core.logging import get_logger

log = get_logger("analysis.subset")

LAT_NAMES = {"lat", "latitude", "y"}
LON_NAMES = {"lon", "longitude", "x"}
TIME_NAMES = {"time", "t", "valid_time"}


def _coord_name(ds, candidates: set[str], what: str) -> str:
    for c in ds.coords:
        if str(c).lower() in candidates:
            return str(c)
    # 坐标可能不是 coord（老文件 time 只是维度）：退回变量表找
    for v in ds.variables:
        if str(v).lower() in candidates:
            return str(v)
    raise SubsetError(f"未找到{what}坐标（候选: {sorted(candidates)}）")


def _ascending(ds, name: str):
    """降序轴翻转为升序（xarray slice 选择要求升序）。"""
    vals = np.asarray(ds[name].values, dtype=float)
    if vals.size >= 2 and vals[0] > vals[-1]:
        return ds.isel({name: slice(None, None, -1)})
    return ds


def normalize_axes(ds) -> tuple:
    """归一化坐标轴约定，返回 (ds, notes)。"""
    notes: list[str] = []
    lat = _coord_name(ds, LAT_NAMES, "纬度")
    lon = _coord_name(ds, LON_NAMES, "经度")
    if ds[lat].ndim != 1 or ds[lon].ndim != 1:
        raise SubsetError(f"暂不支持曲线网格（{lat}/{lon} 非一维坐标）")
    latvals = np.asarray(ds[lat].values, dtype=float)
    if latvals.size >= 2 and latvals[0] > latvals[-1]:
        # 翻转必须留痕：产物的纬度顺序被改过，调用方与后续 QC 都要能看到
        notes.append("纬度轴降序已翻转为升序")
    ds = _ascending(ds, lat)
    lonvals = np.asarray(ds[lon].values, dtype=float)
    if lonvals.size and float(lonvals.max()) > 180.0:
        new = (lonvals + 180.0) % 360.0 - 180.0
        order = np.argsort(new, kind="stable")
        ds = ds.assign_coords({lon: new}).isel({lon: order})
        notes.append("经度轴 0–360 已归一化到 [-180,180)")
    return ds, notes


def subset_dataset(src: Path, dst: Path, bbox: Bbox, *,
                   variables: list[str] | None = None,
                   complevel: int = 4, time_chunk: int = 31,
                   chunk_mb: int = 32, force: bool = False) -> dict:
    """裁切单个文件。返回统计 dict（含 skipped 标记）。失败抛 SubsetError。"""
    src, dst = Path(src), Path(dst)
    if dst.exists() and not force:
        return {"skipped": True, "out": str(dst)}
    with dask.config.set({"array.chunk-size": f"{chunk_mb}MiB",
                         "scheduler": "single-threaded"}):
        try:
            ds = xr.open_dataset(src, chunks="auto", decode_times=True)
        except Exception as e:  # noqa: BLE001 xarray 打开错误种类多，统一包装
            raise SubsetError(f"打开失败: {type(e).__name__}: {e}") from e
        try:
            ds, notes = normalize_axes(ds)

            if variables:
                want = set(variables)
                have = {str(v) for v in ds.data_vars}
                missing = sorted(want - have)
                keep = [str(v) for v in ds.data_vars if str(v) in want]
                if not keep:
                    raise SubsetError(
                        f"变量白名单 {sorted(want)} 与文件变量 {sorted(have)} 无交集")
                ds = ds[keep]
                notes.append(f"保留变量 {keep}"
                             + (f"；文件中不存在: {missing}" if missing else ""))

            lat = _coord_name(ds, LAT_NAMES, "纬度")
            lon = _coord_name(ds, LON_NAMES, "经度")
            sub = ds.sel({lat: slice(bbox.lat_min, bbox.lat_max),
                          lon: slice(bbox.lon_min, bbox.lon_max)})
            if sub[lat].size == 0 or sub[lon].size == 0:
                raise SubsetError(
                    f"bbox({bbox.label}) 与文件网格无交点，未产出文件")

            encoding = {}
            for name, var in sub.data_vars.items():
                if not var.dims:
                    continue
                cs = [min(sz, time_chunk) if str(d).lower() in TIME_NAMES else sz
                      for d, sz in zip(var.dims, var.shape, strict=True)]
                encoding[str(name)] = {"zlib": True, "complevel": complevel,
                                       "chunksizes": cs}

            tname = next((str(c) for c in sub.coords
                          if str(c).lower() in TIME_NAMES), None)
            t_min = t_max = None
            if tname and sub[tname].size:
                tv = np.asarray(sub[tname].values[[0, -1]])
                t_min, t_max = str(tv[0]), str(tv[1])

            dst.parent.mkdir(parents=True, exist_ok=True)
            part = dst.with_name(dst.name + ".part")
            try:
                sub.to_netcdf(part, mode="w", format="NETCDF4",
                              encoding=encoding)
            except SubsetError:
                part.unlink(missing_ok=True)
                raise
            except Exception as e:  # noqa: BLE001
                part.unlink(missing_ok=True)
                raise SubsetError(f"写出失败: {type(e).__name__}: {e}") from e
            os.replace(part, dst)
            return {
                "skipped": False,
                "out": str(dst),
                "size_bytes": dst.stat().st_size,
                "vars": sorted(str(v) for v in sub.data_vars),
                "dims": {str(k): int(v) for k, v in sub.sizes.items()},
                "time_min": t_min, "time_max": t_max,
                "notes": notes,
            }
        finally:
            ds.close()


def subset_one(params: dict, catalog: Catalog, ctx=None, *, cfg) -> dict:
    """任务队列 handler（type=subset_nc）：裁切并登记产物进分析卷。"""
    if ctx:
        ctx.heartbeat()
    acfg = cfg.section("analysis")
    src_root = catalog.resolve_volume_root(params["volume_id"])
    src = src_root / params["rel_path"]

    if params.get("bbox"):
        bbox = Bbox(*params["bbox"])
    else:
        from dbas_core.analysis.regions import get_region
        bbox = get_region(params["region"])

    out_rel = f"{params.get('region', 'custom')}/{params['rel_path']}"
    dst = cfg.analysis_dir / out_rel

    stats = subset_dataset(
        src, dst, bbox,
        variables=params.get("variables"),
        complevel=acfg["complevel"], time_chunk=acfg["time_chunk"],
        chunk_mb=acfg["chunk_mb"], force=bool(params.get("force")))

    if not stats["skipped"]:
        # 产物登记进 catalog（分析卷），并入队元数据解析，保持可盘点
        avol = acfg["volume_id"]
        st = dst.stat()
        file_id, _ = catalog.upsert_file(avol, out_rel, st.st_size, st.st_mtime)
        catalog.enqueue_task(
            "scan_metadata",
            {"file_id": file_id, "volume_id": avol, "rel_path": out_rel},
            dedup_key=f"scan_metadata:{file_id}:{st.st_mtime:.6f}")
        stats["artifact_file_id"] = file_id
    if ctx:
        ctx.heartbeat()
    return stats


def ensure_analysis_volume(catalog: Catalog, cfg) -> str:
    """创建分析输出目录并注册为数据卷。返回卷 ID。"""
    acfg = cfg.section("analysis")
    cfg.analysis_dir.mkdir(parents=True, exist_ok=True)
    catalog.register_volume(acfg["volume_id"], cfg.analysis_dir,
                            label="分析产物卷（subset/ARD）",
                            notes="由 dbas_core subset 创建，源卷只读、产物可再生")
    return acfg["volume_id"]
