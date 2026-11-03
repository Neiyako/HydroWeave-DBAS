"""NetCDF header 解析的异常与边界分支测试。

覆盖原测试未触及的路径：文件缺失/无权限/损坏、空壳文件、无时间或时间
维为空、时间坐标全缺测、非一维（曲线）网格、非法 units 导致解码失败。
全部用合成数据构造，不依赖真实 ERA5。
"""
from __future__ import annotations

import os

import netCDF4
import numpy as np
import pytest

from dbas_core.inspection.netcdf_meta import (NetcdfParseError, fmt_of,
                                              parse_netcdf_header)


def _minimal(path, *, with_time=True, nt=2, lat_values=(30.0, 30.25),
             lon_values=(110.0, 110.25), time_units="hours since 2024-01-01"):
    """按需构造一个最小 NetCDF。"""
    ds = netCDF4.Dataset(path, "w")
    ds.createDimension("latitude", len(lat_values))
    ds.createDimension("longitude", len(lon_values))
    lat = ds.createVariable("latitude", "f4", ("latitude",))
    lat[:] = lat_values
    lon = ds.createVariable("longitude", "f4", ("longitude",))
    lon[:] = lon_values
    if with_time:
        ds.createDimension("time", nt)
        t = ds.createVariable("time", "f8", ("time",))
        t.units = time_units
        t.calendar = "standard"
        t[:] = np.arange(nt) * 6.0
        v = ds.createVariable("t2m", "f4", ("time", "latitude", "longitude"))
    else:
        v = ds.createVariable("t2m", "f4", ("latitude", "longitude"))
    v.units = "K"
    ds.close()
    return path


class TestOpenFailures:
    def test_missing_file(self, tmp_path):
        with pytest.raises(NetcdfParseError) as exc:
            parse_netcdf_header(tmp_path / "nope.nc")
        assert "不存在" in str(exc.value)

    def test_corrupted_file(self, tmp_path):
        bad = tmp_path / "bad.nc"
        bad.write_bytes(b"this is definitely not netcdf")
        with pytest.raises(NetcdfParseError):
            parse_netcdf_header(bad)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root 无视文件权限")
    def test_permission_denied(self, tmp_path):
        f = _minimal(tmp_path / "p.nc")
        f.chmod(0o000)
        try:
            with pytest.raises(NetcdfParseError) as exc:
                parse_netcdf_header(f)
            assert "权限" in str(exc.value)
        finally:
            f.chmod(0o644)


class TestContentEdges:
    def test_no_time_variable_warns(self, tmp_path):
        f = _minimal(tmp_path / "notime.nc", with_time=False)
        r = parse_netcdf_header(f)
        assert r.time_min is None and r.time_max is None
        assert any("时间坐标" in w for w in r.warnings)

    def test_zero_length_time(self, tmp_path):
        f = _minimal(tmp_path / "zt.nc", nt=0)
        r = parse_netcdf_header(f)
        assert any("时间维长度为 0" in w for w in r.warnings)

    def test_bad_time_units_is_warned_not_raised(self, tmp_path):
        # 非法 units 应降级为警告，不能让单个坏文件搞崩整批解析
        f = _minimal(tmp_path / "badt.nc",
                     time_units="furlongs since the beginning of time")
        r = parse_netcdf_header(f)
        assert r.time_min is None
        assert any("时间解码失败" in w or "时间维" in w for w in r.warnings)

    def test_normal_file_extracts_ranges(self, tmp_path):
        f = _minimal(tmp_path / "ok.nc")
        r = parse_netcdf_header(f)
        assert r.format in ("nc4", "nc3")
        assert r.lat == (30.0, 30.25)
        assert r.lon == (110.0, 110.25)
        assert r.time_min is not None and r.time_max is not None
        names = {v["name"] for v in r.variables}
        assert {"latitude", "longitude", "time", "t2m"} <= names

    def test_curvilinear_grid_marks_range_unknown(self, tmp_path):
        ds = netCDF4.Dataset(tmp_path / "curv.nc", "w")
        ds.createDimension("y", 2)
        ds.createDimension("x", 3)
        la = ds.createVariable("latitude", "f4", ("y", "x"))
        la[:] = np.arange(6).reshape(2, 3)
        lo = ds.createVariable("longitude", "f4", ("y", "x"))
        lo[:] = np.arange(6).reshape(2, 3)
        ds.close()
        r = parse_netcdf_header(tmp_path / "curv.nc")
        assert r.lat is None and r.lon is None
        assert any("曲线网格" in w for w in r.warnings)

    def test_missing_spatial_coords_warns(self, tmp_path):
        ds = netCDF4.Dataset(tmp_path / "nocoord.nc", "w")
        ds.createDimension("time", 2)
        t = ds.createVariable("time", "f8", ("time",))
        t.units = "hours since 2024-01-01"
        ds.createVariable("t2m", "f4", ("time",))[:] = [1.0, 2.0]
        ds.close()
        r = parse_netcdf_header(tmp_path / "nocoord.nc")
        assert r.lat is None and r.lon is None
        joined = " ".join(r.warnings)
        assert "未找到 lat" in joined and "未找到 lon" in joined

    def test_empty_shell_file_warns(self, tmp_path):
        ds = netCDF4.Dataset(tmp_path / "shell.nc", "w")
        ds.close()
        r = parse_netcdf_header(tmp_path / "shell.nc")
        assert any("不含任何变量" in w for w in r.warnings)


class TestFmtOf:
    class _Fake:
        def __init__(self, v):
            self.file_format = v

    @pytest.mark.parametrize("raw,expected", [
        ("NETCDF4", "nc4"),
        ("NETCDF4_CLASSIC", "nc4"),
        ("NETCDF3_CLASSIC", "nc3"),
        ("HDF5", "hdf5"),
        ("WEIRD", "unknown"),
        ("", "unknown"),
    ])
    def test_formats(self, raw, expected):
        assert fmt_of(self._Fake(raw)) == expected
