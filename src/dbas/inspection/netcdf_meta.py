"""NetCDF 文件头解析（inspection）。

原则：
- 只读元数据（netCDF4 的 header/dimension/attribute 访问不触发数据块加载）。
- 唯一例外是时间坐标：为得到时间范围必须读取时间维数据，但只读该一维数组，
  并显式控制读取量（上限采样），不碰任何数据变量。
- 任何解析不了的项一律记为 None（未知），绝不推断填充；错误原样返回给调用方。

返回 ParseResult，无法解析的文件抛 NetcdfParseError（由扫描器按文件粒度处理，
不影响整批任务）。
"""
from __future__ import annotations

import cftime  # noqa: F401  netCDF4 依赖，保证日历解码可用
from dataclasses import dataclass, field
from datetime import datetime, timezone

import netCDF4
import numpy as np

from dbas.core.exceptions import ScanError

# 时间坐标最多读取的样本数（防止异常超长时间维）
MAX_TIME_SAMPLES = 200_000

LAT_NAMES = {"lat", "latitude", "y"}
LON_NAMES = {"lon", "longitude", "x"}
TIME_NAMES = {"time", "t", "valid_time"}


class NetcdfParseError(ScanError):
    """NetCDF 头解析失败。"""


@dataclass
class ParseResult:
    format: str                          # nc3/nc4/hdf5/unknown
    dimensions: list[dict] = field(default_factory=list)
    variables: list[dict] = field(default_factory=list)
    time_min: str | None = None
    time_max: str | None = None
    lat: tuple[float, float] | None = None
    lon: tuple[float, float] | None = None
    global_attrs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def fmt_of(ds) -> str:
    f = getattr(ds, "file_format", "") or ""
    if "NETCDF4" in f and "CLASSIC" not in f:
        return "nc4"
    if "NETCDF4_CLASSIC" in f:
        return "nc4"
    if "NETCDF3" in f:
        return "nc3"
    if "hdf5" in f.lower():
        return "hdf5"
    return "unknown"


def _decode_scalar(value) -> str | None:
    """属性值转文本（可能是 bytes / numpy 标量 / 数组）。"""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.ndarray):
        return ", ".join(_decode_scalar(v) for v in value.flat[:8])
    if isinstance(value, (np.floating, np.integer)):
        return value.item().__str__()
    return str(value)


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_netcdf_header(path) -> ParseResult:
    """解析单个 NetCDF 文件的头部元数据。失败抛 NetcdfParseError。"""
    path = str(path)
    result = ParseResult(format="unknown")
    try:
        ds = netCDF4.Dataset(path, "r")
    except FileNotFoundError as e:
        raise NetcdfParseError(f"文件不存在或卷离线: {e}") from e
    except PermissionError as e:
        raise NetcdfParseError(f"无读取权限（文件可能被占用）: {e}") from e
    except OSError as e:
        raise NetcdfParseError(f"NetCDF 打开失败（可能损坏或格式未知）: {e}") from e

    try:
        result.format = fmt_of(ds)
        result.global_attrs = {
            k: _decode_scalar(ds.getncattr(k)) for k in ds.ncattrs()}
        result.dimensions = [
            {"name": n, "size": len(d)}
            for n, d in ds.dimensions.items()]
        if not ds.variables:
            result.warnings.append("文件不含任何变量（可能为空壳文件）")

        for name, var in ds.variables.items():
            attrs = {a: _decode_scalar(getattr(var, a, None)) for a in var.ncattrs()}
            result.variables.append({
                "name": name,
                "dims": ",".join(var.dimensions),
                "dtype": str(var.dtype) if var.dtype is not None else None,
                "units": attrs.get("units"),
                "standard_name": attrs.get("standard_name"),
                "long_name": attrs.get("long_name"),
                # 缺测语义可能由多个属性表达，全部保留，交由 QC/人工判断
                "fill_value": attrs.get("_FillValue")
                              or attrs.get("missing_value"),
                # int16 打包参数：分析层据此判断数值是否已被 netCDF4 自动解码
                "scale_factor": _to_float(getattr(var, "scale_factor", None)),
                "add_offset": _to_float(getattr(var, "add_offset", None)),
            })

        _extract_time(ds, result)
        _extract_spatial(ds, result)
        return result
    finally:
        ds.close()


def _extract_time(ds, result: ParseResult) -> None:
    tname = next((n for n in ds.variables if n.lower() in TIME_NAMES), None)
    if tname is None:
        result.warnings.append("未找到时间坐标变量，时间范围记为未知")
        return
    var = ds.variables[tname]
    n = len(var)
    if n == 0:
        result.warnings.append("时间维长度为 0")
        return
    try:
        # 只读一维时间数组；超长时采样首尾（范围仍准确，中间步长细节留给 QC）
        if n > MAX_TIME_SAMPLES:
            idx = np.linspace(0, n - 1, MAX_TIME_SAMPLES, dtype=np.int64)
            vals = var[idx]
            result.warnings.append(
                f"时间维过长({n})，按采样 {MAX_TIME_SAMPLES} 点解码")
        else:
            vals = var[:]
        if np.ma.isMaskedArray(vals) and vals.mask.all():
            result.warnings.append("时间坐标全部缺测，时间范围记为未知")
            return
        times = netCDF4.num2date(
            np.asarray(vals.compressed() if np.ma.isMaskedArray(vals) else vals),
            units=var.units if hasattr(var, "units") else "",
            calendar=getattr(var, "calendar", "standard"),
            only_use_cftime_datetimes=False,
            only_use_python_datetimes=True)
        if len(times) == 0:
            result.warnings.append("时间坐标解码为空")
            return
        result.time_min = times[0].replace(tzinfo=timezone.utc).isoformat()
        result.time_max = times[-1].replace(tzinfo=timezone.utc).isoformat()
    except Exception as e:  # noqa: BLE001 单位/日历问题种类多，统一记警告
        result.warnings.append(f"时间解码失败（units/calendar 问题）：{type(e).__name__}: {e}")


def _extract_spatial(ds, result: ParseResult) -> None:
    for names, target in ((LAT_NAMES, "lat"), (LON_NAMES, "lon")):
        cname = next((n for n in ds.variables if n.lower() in names), None)
        if cname is None:
            result.warnings.append(f"未找到 {target} 坐标变量")
            continue
        var = ds.variables[cname]
        try:
            if var.ndim != 1:
                # 2D 曲线网格（如 rotate_lat_lon）：范围需全网格计算，此处不读
                result.warnings.append(
                    f"{cname} 为 {var.ndim} 维曲线网格，范围记为未知")
                continue
            # 一维坐标数组很小（全球 0.25° 也才 1440 个值），必须全量读取：
            # 只采样前 N 个会把降序/全球轴的范围错记成一小段（裁切相交判断会依赖它）
            n = len(var)
            if n > MAX_TIME_SAMPLES:  # 病态超大坐标轴：全量 + 首尾兜底采样
                idx = np.unique(np.linspace(0, n - 1, 4096, dtype=np.int64))
                vals = var[idx]
                result.warnings.append(
                    f"{cname} 轴长度异常({n})，按 4096 点采样估计范围")
            else:
                vals = var[:]
            vals = np.asarray(vals, dtype=float)
            vals = vals[np.isfinite(vals)]
            if vals.size == 0:
                result.warnings.append(f"{cname} 坐标无有效值")
                continue
            setattr(result, target,
                    (float(vals.min()), float(vals.max())))
        except Exception as e:  # noqa: BLE001
            result.warnings.append(f"{cname} 坐标读取失败：{type(e).__name__}: {e}")
