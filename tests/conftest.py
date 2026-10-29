"""合成 NetCDF 数据与公共 fixture。不依赖任何真实 ERA5 数据。"""
from __future__ import annotations

import netCDF4
import numpy as np
import pytest

from dbas.catalog.repository import Catalog


def make_nc(path, *, n_time=4, n_lat=3, n_lon=3, var_name="t2m",
            units="K", calendar="standard",
            time_units="hours since 2024-01-01 00:00:00",
            fill=True, lat_start=30.0, lon_start=110.0):
    """写一个最小但结构完整的 NetCDF 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = netCDF4.Dataset(path, "w")
    ds.Conventions = "CF-1.8"
    ds.source = "synthetic test file"
    ds.createDimension("time", n_time)
    ds.createDimension("latitude", n_lat)
    ds.createDimension("longitude", n_lon)
    t = ds.createVariable("time", "f8", ("time",))
    t.units = time_units
    t.calendar = calendar
    t[:] = np.arange(n_time) * 6.0          # 每 6 小时一步
    lat = ds.createVariable("latitude", "f4", ("latitude",))
    lat.units = "degrees_north"
    lat[:] = lat_start + np.arange(n_lat) * 0.25
    lon = ds.createVariable("longitude", "f4", ("longitude",))
    lon.units = "degrees_east"
    lon[:] = lon_start + np.arange(n_lon) * 0.25
    v = ds.createVariable(var_name, "f4", ("time", "latitude", "longitude"),
                          fill_value=np.float32(9.96921e36))
    v.units = units
    v.standard_name = "air_temperature" if var_name == "t2m" else var_name
    data = 280.0 + np.random.default_rng(42).random(
        (n_time, n_lat, n_lon)).astype(np.float32) * 10
    if fill and n_time >= 2:                # 人为放两个缺测点
        data[0, 0, 0] = np.float32(9.96921e36)
        data[1, 2, 1] = np.float32(9.96921e36)
    elif fill:
        data[0, 0, 0] = np.float32(9.96921e36)
    v[:] = data
    ds.close()
    return path


def make_volume(root, files=3, corrupt_one=False):
    """生成一个含若干 .nc 的合成数据卷目录。corrupt_one 时附加一个坏文件。"""
    paths = []
    for i in range(files):
        paths.append(make_nc(root / f"2024/era5_{i:02d}.nc", n_time=2 + i))
    if corrupt_one:
        bad = root / "2024/corrupt.nc"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"NOT A NETCDF FILE" * 10)
        paths.append(bad)
    return paths


@pytest.fixture
def cat(tmp_path):
    """临时 Catalog（数据库位于 tmp_path）。"""
    c = Catalog(tmp_path / "catalog.db")
    yield c
    c.close()


@pytest.fixture
def volume_dir(tmp_path):
    """合成数据卷目录（含 3 个正常 + 1 个损坏文件）。"""
    root = tmp_path / "disk1"
    make_volume(root, files=3, corrupt_one=True)
    return root
