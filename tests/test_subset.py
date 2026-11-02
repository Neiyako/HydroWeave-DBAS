"""subset 模块测试：坐标轴归一化、bbox 裁切正确性、幂等、任务 handler 链路。
全部使用合成数据。"""
from __future__ import annotations

import numpy as np
import xarray as xr
from netCDF4 import Dataset

from dbas.analysis.regions import Bbox, get_region, overlaps_file_grid
from dbas.analysis.subset import (ensure_analysis_volume, subset_dataset,
                                  subset_one)
from dbas.core.config import Config
from dbas.core.exceptions import SubsetError

QHP = Bbox(25, 40, 70, 105)


def era_file(path, *, lat_desc=False, lon360=False, nt=3, var_names=("a",),
             lat_step=10.0, lon_step=10.0):
    """确定性小网格：值 = lat + lon（lon 已折算到 [-180,180)）+ 时间序号。"""
    lat = (np.arange(90.0, -91.0, -lat_step) if lat_desc
           else np.arange(-90.0, 91.0, lat_step))
    lon = (np.arange(0.0, 360.0, lon_step) if lon360
           else np.arange(-180.0, 180.0, lon_step))
    lon_true = ((lon + 180.0) % 360.0) - 180.0
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = Dataset(path, "w")
    ds.createDimension("time", nt)
    ds.createDimension("latitude", lat.size)
    ds.createDimension("longitude", lon.size)
    t = ds.createVariable("time", "f8", ("time",))
    t.units = "hours since 2010-01-01 00:00:00"
    t[:] = np.arange(nt) * 24.0
    ds.createVariable("latitude", "f4", ("latitude",))[:] = lat
    ds.createVariable("longitude", "f4", ("longitude",))[:] = lon
    for j, name in enumerate(var_names):
        v = ds.createVariable(name, "f4", ("time", "latitude", "longitude"))
        v.units = "1"
        base = (lat[:, None] + lon_true[None, :]) + j * 100.0
        v[:] = (base[None, :, :] + np.arange(nt)[:, None, None]).astype("f4")
    ds.close()
    return path


def expected_grid(nt, j=0, latv=(30, 40), lonv=(70, 80, 90, 100)):
    lat = np.array(latv, dtype=float)
    lon = np.array(lonv, dtype=float)
    return (lat[:, None] + lon[None, :] + j * 100.0)[None, :, :] \
        + np.arange(nt)[:, None, None]


def test_subset_basic(tmp_path):
    src = era_file(tmp_path / "src.nc")
    dst = tmp_path / "out/qhp_main/src.nc"
    stats = subset_dataset(src, dst, QHP)
    assert not stats["skipped"]
    assert stats["dims"]["latitude"] == 2 and stats["dims"]["longitude"] == 4
    out = xr.open_dataset(dst)
    np.testing.assert_allclose(out.latitude.values, [30, 40])
    np.testing.assert_allclose(out.longitude.values, [70, 80, 90, 100])
    np.testing.assert_allclose(out["a"].values, expected_grid(3), atol=1e-4)


def test_subset_lon0360_matches(tmp_path):
    a = subset_dataset(era_file(tmp_path / "e180.nc", lon360=False),
                       tmp_path / "o1.nc", QHP)
    b = subset_dataset(era_file(tmp_path / "e360.nc", lon360=True),
                       tmp_path / "o2.nc", QHP)
    assert "归一化" in b["notes"][0]
    d1, d2 = xr.open_dataset(a["out"]), xr.open_dataset(b["out"])
    np.testing.assert_allclose(d1["a"].values, d2["a"].values, atol=1e-4)


def test_subset_descending_lat_matches(tmp_path):
    subset_dataset(era_file(tmp_path / "asc.nc"), tmp_path / "o1.nc", QHP)
    subset_dataset(era_file(tmp_path / "desc.nc", lat_desc=True),
                   tmp_path / "o2.nc", QHP)
    d1, d2 = xr.open_dataset(tmp_path / "o1.nc"), xr.open_dataset(tmp_path / "o2.nc")
    np.testing.assert_allclose(d1["a"].values, d2["a"].values, atol=1e-4)


def test_subset_variables_allowlist(tmp_path):
    src = era_file(tmp_path / "src.nc", var_names=("a", "b", "c"))
    stats = subset_dataset(src, tmp_path / "o.nc", QHP,
                           variables=["b", "nonexistent"])
    assert stats["vars"] == ["b"]
    assert "nonexistent" in stats["notes"][-1]
    out = xr.open_dataset(tmp_path / "o.nc")
    np.testing.assert_allclose(out["b"].values, expected_grid(3, j=1), atol=1e-4)


def test_subset_idempotent_and_force(tmp_path):
    src = era_file(tmp_path / "src.nc")
    dst = tmp_path / "o.nc"
    s1 = subset_dataset(src, dst, QHP)
    s2 = subset_dataset(src, dst, QHP)
    s3 = subset_dataset(src, dst, QHP, force=True)
    assert not s1["skipped"] and s2["skipped"] and not s3["skipped"]


def test_subset_no_intersection_raises(tmp_path):
    src = era_file(tmp_path / "src.nc")
    try:
        subset_dataset(src, tmp_path / "o.nc", Bbox(61, 65, 71, 75))
        assert False, "应抛 SubsetError"
    except SubsetError as e:
        assert "无交点" in str(e)
    assert not (tmp_path / "o.nc").exists()
    assert not list(tmp_path.glob("o.nc.part"))


def test_overlaps_file_grid_conventions():
    # 0-360 轴的全球文件、-180-180 轴的全球文件都应判为相交
    assert overlaps_file_grid(QHP, (-90, 90), (0, 359)) is True
    assert overlaps_file_grid(QHP, (-90, 90), (-180, 179)) is True
    # 完全不相交（南美）
    assert overlaps_file_grid(QHP, (-60, -30), (-80, -50)) is False
    # 范围未知
    assert overlaps_file_grid(QHP, None, None) is None


def test_subset_one_handler_registers(tmp_path, cat):
    cfg = Config(root=tmp_path, data={})
    src_vol = tmp_path / "vol"
    era_file(src_vol / "2010/era5_daily_201001.nc", var_names=("t2m", "tp"))
    cat.register_volume("src", src_vol)
    file_id, _ = cat.upsert_file("src", "2010/era5_daily_201001.nc", 100, 1.0)
    avol = ensure_analysis_volume(cat, cfg)

    params = {"file_id": file_id, "volume_id": "src",
              "rel_path": "2010/era5_daily_201001.nc",
              "region": "qhp_main", "variables": ["t2m", "tp"]}
    stats = subset_one(params, cat, None, cfg=cfg)
    out = cfg.analysis_dir / "qhp_main" / "2010/era5_daily_201001.nc"
    assert out.exists() and not stats["skipped"]
    assert stats["vars"] == ["t2m", "tp"]
    # 产物已登记进分析卷，且入队了元数据解析
    rows = list(cat.iter_files(volume_id=avol))
    assert len(rows) == 1 and rows[0]["file_id"] == stats["artifact_file_id"]
    tasks = [t for t in cat.list_tasks(limit=50) if t["type"] == "scan_metadata"]
    assert tasks


def test_get_region():
    assert get_region("qhp_main") == Bbox(25, 40, 70, 105)
