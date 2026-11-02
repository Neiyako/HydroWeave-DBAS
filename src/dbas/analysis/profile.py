"""内容画像（L1）：抽样读取数据值 + 文件级结构信息。

与 inspection（只读 header）不同，本模块会读：
- 首/末时次的一个空间抽样场（纬/经方向跨步），层维全量；
- 时间/层/经纬坐标轴。
不读取完整数据变量，峰值样本量由 MAX_SAMPLES_PER_TIME 控制。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import timezone
from typing import Any

import netCDF4
import numpy as np

from dbas.analysis.datasets import detect_dataset, get_dataset_spec
from dbas.catalog.repository import Catalog
from dbas.core.exceptions import ScanError
from dbas.core.logging import get_logger
from dbas.inspection.netcdf_meta import fmt_of
from dbas.qc.rules import check_file_profile

log = get_logger("analysis.profile")

MAX_SAMPLES_PER_TIME = 100_000

LAT_NAMES = {"lat", "latitude", "y", "ydim"}
LON_NAMES = {"lon", "longitude", "x", "xdim"}
TIME_NAMES = {"time", "t", "valid_time"}
LEVEL_NAMES = {"level", "pressure_level", "plev", "isobaricinhpa"}


class ProfileError(ScanError):
    """内容画像读取失败。"""


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_axis(ds, names: set[str]) -> str | None:
    for n in ds.variables:
        if n.lower() in names:
            return n
    return None


def _find_time_axis(ds) -> str | None:
    for n in ds.variables:
        if n.lower() in ("time", "valid_time"):
            return n
    for n in ds.variables:
        v = ds.variables[n]
        if (n.lower() == "t" and v.ndim == 1
                and "since" in str(getattr(v, "units", "")).lower()):
            return n
    return None


def _signature(values: np.ndarray) -> str:
    arr = np.ascontiguousarray(values, dtype=np.float64)
    return hashlib.md5(arr.tobytes()).hexdigest()[:16]


def _read_axis(ds, names: set[str], *, target: int = 200_000) -> dict[str, Any] | None:
    name = _find_axis(ds, names)
    if name is None:
        return None
    var = ds.variables[name]
    try:
        size = int(np.prod(var.shape)) if var.shape else 1
        step = max(1, int(math.ceil(size / target)))
        if var.ndim == 1:
            raw = var[::step]
        elif var.ndim == 2:
            s2 = max(1, int(math.ceil(math.sqrt(size / target))))
            raw = var[::s2, ::s2]
        else:
            return None
        arr = np.ma.asarray(raw, dtype=np.float64)
        masked = int(np.ma.count_masked(arr))
        vals = np.ma.filled(arr, np.nan).ravel()
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            return {"name": name, "sig": None, "masked": masked,
                    "min": None, "max": None, "n": 0}
        return {"name": name, "sig": _signature(vals),
                "min": float(vals.min()), "max": float(vals.max()),
                "n": int(vals.size), "masked": masked}
    except Exception as e:  # noqa: BLE001 坐标异常不应阻断整文件画像
        log.warning("读取坐标 %s 失败: %s", name, e)
        return {"name": name, "sig": None, "masked": None,
                "min": None, "max": None, "n": 0}


def _time_info(ds) -> dict[str, Any]:
    info: dict[str, Any] = {
        "time_n": None, "time_min": None, "time_max": None,
        "time_units": None, "time_calendar": None,
        "time_non_increasing": 0, "time_zero_count": 0,
    }
    name = _find_time_axis(ds)
    if name is None:
        return info
    var = ds.variables[name]
    info["time_units"] = str(getattr(var, "units", "")) or None
    info["time_calendar"] = str(getattr(var, "calendar", "standard")) or None
    try:
        raw = np.ma.asarray(var[:], dtype=np.float64)
        info["time_n"] = int(raw.size)
        vals = raw.compressed()
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            return info
        info["time_zero_count"] = int((vals == 0).sum())
        if vals.size > 1:
            info["time_non_increasing"] = int((np.diff(vals) <= 0).sum())
        times = netCDF4.num2date(
            vals, units=getattr(var, "units", ""),
            calendar=getattr(var, "calendar", "standard"),
            only_use_cftime_datetimes=False,
            only_use_python_datetimes=True)
        if len(times):
            info["time_min"] = times[0].replace(
                tzinfo=timezone.utc).isoformat()
            info["time_max"] = times[-1].replace(
                tzinfo=timezone.utc).isoformat()
    except Exception as e:  # noqa: BLE001 非 ISO 日历/单位问题统一记日志
        log.warning("时间轴解码失败: %s", e)
    return info


def _expected_bytes(ds, fmt: str) -> int | None:
    if fmt != "nc3":
        return None
    total = 0
    for v in ds.variables.values():
        if v.dtype is None:
            continue
        n = 1
        for s in v.shape:
            n *= int(s)
        total += n * int(v.dtype.itemsize)
    return total


def _roles(var) -> dict[str, int]:
    roles: dict[str, int] = {}
    for i, d in enumerate(var.dimensions):
        dl = d.lower()
        if dl in TIME_NAMES and "time" not in roles:
            roles["time"] = i
        elif dl in LEVEL_NAMES:
            roles["level"] = i
        elif dl in LAT_NAMES:
            roles["lat"] = i
        elif dl in LON_NAMES:
            roles["lon"] = i
    return roles


def _is_coord_var(ds, name: str, var) -> bool:
    low = name.lower()
    if var.ndim == 1 and name in ds.dimensions:
        return True
    if low in LAT_NAMES | LON_NAMES:
        return True
    if low in {"time", "valid_time", "level", "pressure_level", "plev",
               "isobaricinhpa"}:
        return True
    return False


def _read_sample(var, roles: dict[str, int], pos: str) -> dict[str, Any]:
    spatial = [var.shape[roles[k]] for k in ("lat", "lon") if k in roles]
    n_spatial = 1
    for s in spatial:
        n_spatial *= int(s)
    n_levels = int(var.shape[roles["level"]]) if "level" in roles else 1
    target_spatial = max(1000, MAX_SAMPLES_PER_TIME // max(1, n_levels))
    step = max(1, int(math.ceil(math.sqrt(n_spatial / target_spatial))))
    idx: list[Any] = []
    for i in range(len(var.dimensions)):
        if i == roles.get("time"):
            idx.append(0 if pos == "first" else -1)
        elif i == roles.get("level"):
            idx.append(slice(None))
        elif i in (roles.get("lat"), roles.get("lon")):
            idx.append(slice(None, None, step))
        else:
            idx.append(0)
    arr = np.ma.asarray(var[tuple(idx)], dtype=np.float64)
    flat = np.ma.filled(arr, np.nan).ravel()
    finite = flat[np.isfinite(flat)]
    return {
        "values": finite,
        "n_total": int(flat.size),
        "n_samples": int(finite.size),
        "n_masked": int(flat.size - finite.size),
        "min": float(finite.min()) if finite.size else None,
        "max": float(finite.max()) if finite.size else None,
        "mean": float(finite.mean()) if finite.size else None,
        "std": float(finite.std()) if finite.size else None,
        "constant": bool(finite.size and finite.min() == finite.max()),
    }


def profile_one(params: dict, catalog: Catalog, ctx=None) -> dict:
    """单个文件的画像任务：抽样 + 统计 + 文件级 QC，结果整体替换写库。"""
    if ctx:
        ctx.heartbeat()
    root = catalog.resolve_volume_root(params["volume_id"])
    path = root / params["rel_path"]
    file_rec = catalog.get_file(params["file_id"]) or {}
    dataset_id = detect_dataset(params["rel_path"], file_rec.get("file_name"))
    spec = get_dataset_spec(dataset_id)
    catalog.ensure_dataset(dataset_id, spec.label, spec.description, spec.source)
    catalog.set_file_dataset(params["file_id"], dataset_id)
    target = f"file:{params['volume_id']}:{params['rel_path']}"

    try:
        ds = netCDF4.Dataset(str(path), "r")
    except Exception as e:  # noqa: BLE001 打开失败按文件隔离
        msg = f"NetCDF 打开失败: {type(e).__name__}: {e}"
        catalog.mark_parse_failed(params["file_id"], msg)
        raise ProfileError(msg) from e

    try:
        fmt = fmt_of(ds)
        size_bytes = path.stat().st_size
        expected_bytes = _expected_bytes(ds, fmt)
        tinfo = _time_info(ds)
        lat = _read_axis(ds, LAT_NAMES)
        lon = _read_axis(ds, LON_NAMES)
        lev = _read_axis(ds, LEVEL_NAMES)

        warnings: list[str] = []
        var_profiles: list[dict[str, Any]] = []
        for name, var in ds.variables.items():
            if _is_coord_var(ds, name, var) or var.ndim == 0:
                continue
            roles = _roles(var)
            if not roles:
                warnings.append(f"{name}: 维度无法识别 {var.dimensions}")
                continue
            try:
                first = _read_sample(var, roles, "first")
                samples = [first]
                last = None
                if "time" in roles:
                    last = _read_sample(var, roles, "last")
                    samples.append(last)
                arrays = [s["values"] for s in samples if s["values"].size]
                vals = (np.concatenate(arrays) if arrays
                        else np.array([], dtype=float))
                n_total = sum(s["n_total"] for s in samples)
                n_masked = sum(s["n_masked"] for s in samples)
                tail_constant = bool(
                    last is not None
                    and first["n_samples"] > 0 and not first["constant"]
                    and last["n_samples"] > 0 and last["constant"])
                var_profiles.append({
                    "var_name": name,
                    "units": getattr(var, "units", None),
                    "dtype": str(var.dtype) if var.dtype is not None else None,
                    "scale_factor": _to_float(getattr(var, "scale_factor", None)),
                    "add_offset": _to_float(getattr(var, "add_offset", None)),
                    "n_total": int(n_total),
                    "n_samples": int(vals.size),
                    "n_masked": int(n_masked),
                    "min_value": float(vals.min()) if vals.size else None,
                    "max_value": float(vals.max()) if vals.size else None,
                    "mean_value": float(vals.mean()) if vals.size else None,
                    "std_value": float(vals.std()) if vals.size else None,
                    "negative_count": int((vals < 0).sum()) if vals.size else 0,
                    "constant_field": bool(vals.size and vals.min() == vals.max()),
                    "tail_constant": tail_constant,
                })
                if ctx:
                    ctx.heartbeat()
            except Exception as e:  # noqa: BLE001 单变量失败只记警告
                warnings.append(f"{name}: 抽样失败 {type(e).__name__}: {e}")

        tail_suspect = any(vp["tail_constant"] for vp in var_profiles)
        file_profile = {
            "file_id": params["file_id"],
            "dataset_id": dataset_id,
            "profiled_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "format": fmt,
            "size_bytes": size_bytes,
            "expected_bytes": expected_bytes,
            "n_vars": len(var_profiles),
            "time_n": tinfo["time_n"],
            "time_min": tinfo["time_min"],
            "time_max": tinfo["time_max"],
            "time_units": tinfo["time_units"],
            "time_calendar": tinfo["time_calendar"],
            "time_non_increasing": tinfo["time_non_increasing"],
            "time_zero_count": tinfo["time_zero_count"],
            "lat_sig": lat["sig"] if lat else None,
            "lon_sig": lon["sig"] if lon else None,
            "level_sig": lev["sig"] if lev else None,
            "lat_min": lat["min"] if lat else None,
            "lat_max": lat["max"] if lat else None,
            "lon_min": lon["min"] if lon else None,
            "lon_max": lon["max"] if lon else None,
            "level_n": lev["n"] if lev else None,
            "level_min": lev["min"] if lev else None,
            "level_max": lev["max"] if lev else None,
            "tail_suspect": tail_suspect,
            "warnings_json": (json.dumps(warnings, ensure_ascii=False)
                              if warnings else None),
        }
        findings = check_file_profile(
            file_profile, var_profiles, spec, target=target,
            dataset_id=dataset_id, volume_id=params["volume_id"])
    finally:
        ds.close()

    catalog.save_profile(file_profile, var_profiles,
                         [f.to_dict() for f in findings], target=target)
    log.info("画像完成 %s: vars=%d warnings=%d findings=%d",
             params["rel_path"], len(var_profiles), len(warnings), len(findings))
    return {"n_vars": len(var_profiles), "format": fmt,
            "tail_suspect": tail_suspect, "findings": len(findings),
            "warnings": warnings}


__all__ = ["ProfileError", "profile_one"]
