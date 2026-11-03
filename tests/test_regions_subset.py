"""区域定义、卷身份探测与 subset 裁切的测试。

这些是原测试未覆盖的部分：Bbox 校验、0–360 经度折算、跨 0° 经线的
相交判断、diskutil 不可用时的降级路径，以及 subset 的真实裁切与幂等。
全部使用合成数据，不依赖真实 ERA5、外接盘或网络。
"""
from __future__ import annotations

import subprocess

import numpy as np
import pytest
import xarray as xr

from dbas_core.analysis.regions import (Bbox, REGIONS, get_region,
                                        overlaps_file_grid)
from dbas_core.analysis.subset import normalize_axes, subset_dataset
from dbas_core.exceptions import AnalysisError, SubsetError
from dbas.storage.volumes import detect_volume_identity, suggest_volume_id


# --------------------------------------------------------------------------
# Bbox 校验
# --------------------------------------------------------------------------

class TestBbox:
    def test_valid_bbox(self):
        b = Bbox(25, 40, 70, 105)
        assert b.lat_min == 25 and b.lon_max == 105

    @pytest.mark.parametrize("args", [
        (40, 25, 70, 105),    # 纬度上下颠倒
        (25, 40, 105, 70),    # 经度左右颠倒
        (25, 25, 70, 105),    # 纬度为 0 高
    ])
    def test_rejects_inverted_or_empty(self, args):
        with pytest.raises(AnalysisError):
            Bbox(*args)

    @pytest.mark.parametrize("args", [
        (-91, 40, 70, 105),   # 纬度下限越界
        (25, 91, 70, 105),    # 纬度上限越界
        (25, 40, -181, 105),  # 经度下限越界
        (25, 40, 70, 181),    # 经度上限越界
    ])
    def test_rejects_out_of_range(self, args):
        with pytest.raises(AnalysisError):
            Bbox(*args)

    def test_label_format(self):
        assert Bbox(25, 40, 70, 105).label == "25.0–40.0°N, 70.0–105.0°E"

    def test_contains_point(self):
        b = Bbox(25, 40, 70, 105)
        assert b.contains_point(30, 80) is True
        assert b.contains_point(30, 120) is False
        # 0–360 写法应先折算：350° = −10°，在 bbox 之外
        assert b.contains_point(30, 350) is False

    def test_contains_point_wraps_longitude(self):
        b = Bbox(25, 40, -180, -170)
        # 190° (=−170°) 应落在 bbox 内
        assert b.contains_point(30, 190) is True


class TestRegions:
    def test_get_known_region(self):
        assert get_region("qhp_main").lat_min == 25

    def test_unknown_region_lists_available(self):
        with pytest.raises(AnalysisError) as exc:
            get_region("no_such_region")
        msg = str(exc.value)
        assert "no_such_region" in msg
        for name in REGIONS:
            assert name in msg

    def test_all_builtin_regions_are_valid(self):
        # 预置区域必须自身合法（构造时已校验，此处防止后续改错）
        for name, bbox in REGIONS.items():
            assert bbox.lat_min < bbox.lat_max, name
            assert bbox.lon_min < bbox.lon_max, name


class TestOverlapsFileGrid:
    bbox = Bbox(25, 40, 70, 105)

    def test_no_lat_overlap(self):
        assert overlaps_file_grid(self.bbox, (-10, 10), (60, 120)) is False

    def test_hit_plain_longitude(self):
        assert overlaps_file_grid(self.bbox, (20, 45), (60, 120)) is True

    def test_miss_plain_longitude(self):
        assert overlaps_file_grid(self.bbox, (20, 45), (120, 180)) is False

    def test_unknown_lon_returns_none(self):
        assert overlaps_file_grid(self.bbox, (20, 45), None) is None

    def test_unknown_lat_with_lon_given_returns_none(self):
        assert overlaps_file_grid(self.bbox, None, (60, 120)) is None

    def test_file_on_0_360_axis_is_converted(self):
        # 文件中经度以 0–360 存储（如 65..115），bbox 70–105 应判为相交
        assert overlaps_file_grid(self.bbox, (20, 45), (60.0, 120.0)) is True

    def test_bbox_crossing_0_meridian(self):
        # bbox 跨 0° 经线：[-180,-170] 折算到 0–360 后分裂成两段
        b = Bbox(25, 40, -180, -170)
        # 文件轴 180..340（对应 180..-20），应命中 [180,190] 段
        assert overlaps_file_grid(b, (25, 40), (180.0, 340.0)) is True
        # 完全不覆盖该段的文件轴
        assert overlaps_file_grid(b, (25, 40), (200.0, 340.0)) is False


# --------------------------------------------------------------------------
# 卷身份探测：非 macOS 或 diskutil 不可用时必须安全降级
# --------------------------------------------------------------------------

class TestVolumeIdentity:
    def test_returns_none_when_diskutil_missing(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise FileNotFoundError("diskutil not found")
        monkeypatch.setattr(subprocess, "run", boom)
        assert detect_volume_identity(tmp_path) is None

    def test_returns_none_when_diskutil_fails(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise subprocess.CalledProcessError(1, "diskutil")
        monkeypatch.setattr(subprocess, "run", boom)
        assert detect_volume_identity(tmp_path) is None

    def test_suggest_falls_back_to_path_digest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dbas.storage.volumes.detect_volume_identity", lambda p: None)
        vid, why = suggest_volume_id(tmp_path)
        assert vid.startswith("vol-")
        assert "显式" in why   # 必须提示用户改用显式 ID

    def test_suggest_uses_uuid_when_available(self, tmp_path, monkeypatch):
        from dbas.storage.volumes import VolumeIdentity
        monkeypatch.setattr(
            "dbas.storage.volumes.detect_volume_identity",
            lambda p: VolumeIdentity("ABCD-UUID-XYZ123", "MyDisk", "disk1s2", "apfs"))
        vid, why = suggest_volume_id(tmp_path)
        assert vid == "vol-abcd-uuid-xy"   # 取 UUID 前 12 位
        assert "UUID" in why

    def test_suggest_uses_name_when_no_uuid(self, tmp_path, monkeypatch):
        from dbas.storage.volumes import VolumeIdentity
        monkeypatch.setattr(
            "dbas.storage.volumes.detect_volume_identity",
            lambda p: VolumeIdentity(None, "My External Disk", "disk1s2", "apfs"))
        vid, why = suggest_volume_id(tmp_path)
        assert vid == "vol-my-external-disk"
        assert "卷名" in why


# --------------------------------------------------------------------------
# subset：真实裁切
# --------------------------------------------------------------------------

@pytest.fixture()
def grid_ds(tmp_path):
    """2 时次 × 4 纬度 × 4 经度，可作为 subset 的输入源。"""
    lat = np.array([25.0, 26.0, 27.0, 28.0])
    lon = np.array([70.0, 71.0, 72.0, 73.0])
    data = np.arange(2 * 4 * 4, dtype="f4").reshape(2, 4, 4)
    ds = xr.Dataset(
        {"t2m": (("time", "latitude", "longitude"), data)},
        coords={"time": np.arange(2), "latitude": lat, "longitude": lon},
    )
    path = tmp_path / "src.nc"
    ds.to_netcdf(path)
    return path


class TestSubsetDataset:
    def test_crops_region_and_shrinks(self, grid_ds, tmp_path):
        out = tmp_path / "out.nc"
        res = subset_dataset(grid_ds, out, Bbox(25.5, 27.5, 70.5, 72.5))
        assert res.get("skipped") is not True
        assert out.exists()
        with xr.open_dataset(out) as sub:
            assert sub.sizes["latitude"] == 2
            assert sub.sizes["longitude"] == 2
            assert float(sub["latitude"].min()) == 26.0
            assert float(sub["longitude"].max()) == 72.0

    def test_idempotent_without_force(self, grid_ds, tmp_path):
        out = tmp_path / "out.nc"
        subset_dataset(grid_ds, out, Bbox(25, 28, 70, 73))
        again = subset_dataset(grid_ds, out, Bbox(25, 28, 70, 73))
        assert again["skipped"] is True

    def test_force_rebuilds(self, grid_ds, tmp_path):
        out = tmp_path / "out.nc"
        subset_dataset(grid_ds, out, Bbox(25, 28, 70, 73))
        rebuilt = subset_dataset(grid_ds, out, Bbox(25.5, 28, 70, 73), force=True)
        assert rebuilt.get("skipped") is False

    def test_disjoint_bbox_raises(self, grid_ds, tmp_path):
        # 文件网格在 25–28N/70–73E，请求欧洲区域应无交点
        with pytest.raises(SubsetError) as exc:
            subset_dataset(grid_ds, tmp_path / "x.nc", Bbox(40, 50, 0, 10))
        assert "无交点" in str(exc.value)

    def test_variable_whitelist(self, grid_ds, tmp_path):
        out = tmp_path / "v.nc"
        subset_dataset(grid_ds, out, Bbox(25, 28, 70, 73), variables=["t2m"])
        with xr.open_dataset(out) as sub:
            assert "t2m" in sub.data_vars


def _lonlat_ds(lats, lons):
    """构造带一维 lat/lon 的最小数据集（normalize_axes 要求两轴同时存在）。"""
    data = np.zeros((len(lats), len(lons)), dtype="f4")
    return xr.Dataset(
        {"t2m": (("latitude", "longitude"), data)},
        coords={"latitude": list(lats), "longitude": list(lons)})


class TestNormalizeAxes:
    def test_descending_lat_is_flipped(self):
        ds = _lonlat_ds([28.0, 27.0, 26.0, 25.0], [70.0, 71.0, 72.0, 73.0])
        out, notes = normalize_axes(ds)
        assert list(out["latitude"].values) == [25.0, 26.0, 27.0, 28.0]
        assert any("纬度" in n for n in notes)

    def test_0_360_longitude_is_converted(self):
        ds = _lonlat_ds([25.0, 26.0], [350.0, 355.0, 0.0, 5.0])
        out, notes = normalize_axes(ds)
        lons = list(out["longitude"].values)
        assert max(lons) <= 180.0 and min(lons) >= -180.0
        assert lons == sorted(lons)          # 折算后必须重新排序
        assert any("0–360" in n for n in notes)

    def test_already_standard_is_untouched(self):
        ds = _lonlat_ds([25.0, 26.0, 27.0], [70.0, 71.0, 72.0])
        out, notes = normalize_axes(ds)
        assert list(out["latitude"].values) == [25.0, 26.0, 27.0]
        assert notes == []                   # 无需归一化时不应产生提示

    def test_missing_longitude_raises(self):
        ds = xr.Dataset(
            {"t2m": (("latitude",), np.arange(3, dtype="f4"))},
            coords={"latitude": [25.0, 26.0, 27.0]})
        with pytest.raises(SubsetError) as exc:
            normalize_axes(ds)
        assert "经度" in str(exc.value)
