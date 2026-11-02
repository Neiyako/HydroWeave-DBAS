"""内容画像与 QC 规则测试（合成 NetCDF，不依赖真实数据）。"""
from __future__ import annotations

import pytest

from dbas.analysis.datasets import get_dataset_spec
from dbas.analysis.profile import profile_one
from dbas.qc.rules import check_dataset, check_file_profile, run_dataset_checks
from tests.conftest import make_layered_nc


def _register(cat, root, rel):
    cat.register_volume("v1", root)
    path = root / rel
    fid, _ = cat.upsert_file("v1", rel, path.stat().st_size, 1.0)
    return fid


def _profile(cat, fid, rel):
    return profile_one({"file_id": fid, "volume_id": "v1",
                        "rel_path": rel}, cat)


def test_profile_good_file(cat, tmp_path):
    root = tmp_path / "disk"
    rel = "2010/ERA5_025dg_20100501_temperature.nc"
    make_layered_nc(root / rel)
    fid = _register(cat, root, rel)
    res = _profile(cat, fid, rel)
    assert res["n_vars"] == 1
    assert res["tail_suspect"] is False

    fp = cat.conn.execute(
        "SELECT * FROM file_profiles WHERE file_id=?", (fid,)).fetchone()
    assert fp["dataset_id"] == "era5_pressure"
    assert fp["time_n"] == 4
    assert fp["level_n"] == 3
    assert fp["lat_sig"] and fp["lon_sig"] and fp["level_sig"]
    vp = cat.conn.execute(
        "SELECT * FROM variable_profiles WHERE file_id=? AND var_name='t'",
        (fid,)).fetchone()
    assert vp["n_samples"] > 0
    assert vp["min_value"] == pytest.approx(280.0, abs=0.02)
    assert vp["max_value"] > vp["min_value"]
    assert cat.get_file(fid)["dataset_id"] == "era5_pressure"
    assert cat.list_quality_reports(dataset_id="era5_pressure") == []


def test_profile_detects_constant_tail(cat, tmp_path):
    root = tmp_path / "disk"
    rel = "2010/ERA5_025dg_20100502_temperature.nc"
    make_layered_nc(root / rel, tail_constant=True)
    fid = _register(cat, root, rel)
    res = _profile(cat, fid, rel)
    assert res["tail_suspect"] is True
    rules = {r["rule_id"] for r in cat.list_quality_reports(
        dataset_id="era5_pressure")}
    assert "R002_TAIL_CONSTANT" in rules


def test_profile_detects_time_axis_zeros(cat, tmp_path):
    root = tmp_path / "disk"
    rel = "2010/ERA5_025dg_20100503_temperature.nc"
    make_layered_nc(root / rel, time_values=[0, 6, 12, 0])
    fid = _register(cat, root, rel)
    _profile(cat, fid, rel)
    rules = {r["rule_id"] for r in cat.list_quality_reports(
        dataset_id="era5_pressure")}
    assert "R006_TIME_AXIS" in rules


def test_profile_detects_negative_humidity(cat, tmp_path):
    root = tmp_path / "disk"
    rel = "2010/ERA5_025dg_20100504_specific_humidity.nc"
    make_layered_nc(root / rel, var_name="q", units="kg kg**-1",
                    negative=True, base=0.005)
    fid = _register(cat, root, rel)
    _profile(cat, fid, rel)
    rules = {r["rule_id"] for r in cat.list_quality_reports(
        dataset_id="era5_pressure")}
    assert "R005_NEGATIVE_Q" in rules


def test_file_rules_truncation_fill_and_time():
    spec = get_dataset_spec("era5_pressure")
    fp = {"format": "nc3", "size_bytes": 100, "expected_bytes": 200,
          "time_n": 24, "time_min": "2020-01-01T00:00:00+00:00",
          "time_max": "2020-01-01T23:00:00+00:00",
          "time_non_increasing": 2, "time_zero_count": 1,
          "lat_sig": "a", "lon_sig": "b", "tail_suspect": True}
    vps = [{"var_name": "t", "n_total": 100, "n_masked": 10,
            "min_value": 200.0, "max_value": 300.0, "negative_count": 0,
            "constant_field": False, "tail_constant": True}]
    findings = check_file_profile(
        fp, vps, spec, target="file:v1:x", dataset_id="era5_pressure",
        volume_id="v1")
    rules = {f.rule_id for f in findings}
    assert {"R001_FILE_SIZE", "R002_TAIL_CONSTANT", "R003_FILL_RATIO",
            "R006_TIME_AXIS"} <= rules
    assert all(f.volume_id == "v1" for f in findings)


def test_rule_detects_undeclared_sentinel():
    spec = get_dataset_spec("modis_air_quality")
    fp = {"format": "nc3", "size_bytes": 100, "expected_bytes": 100,
          "time_n": None, "time_min": None, "time_max": None,
          "time_non_increasing": 0, "time_zero_count": 0,
          "lat_sig": "a", "lon_sig": "b", "tail_suspect": False}
    vps = [{"var_name": "CO", "n_total": 100, "n_masked": 0,
            "min_value": -999.0, "max_value": 5.0, "negative_count": 60,
            "constant_field": False, "tail_constant": False}]
    findings = check_file_profile(
        fp, vps, spec, target="file:v1:x", dataset_id="modis_air_quality",
        volume_id="v1")
    assert "R009_SENTINEL" in {f.rule_id for f in findings}


def test_dataset_coverage_from_filenames(cat, tmp_path):
    root = tmp_path / "disk"
    root.mkdir()
    cat.register_volume("v1", root)
    rels = [
        "2010/ERA5_025dg_20100501_temperature.nc",
        "2010/ERA5_025dg_20100501_specific_humidity.nc",
        "2010/ERA5_025dg_20100503_temperature.nc",
        "2010/ERA5_025dg_20100503_specific_humidity.nc",
        "2010/ERA5_025dg_20100503_u_component_of_wind.nc",
        "2010/ERA5_025dg_20100503_v_component_of_wind.nc",
    ]
    for rel in rels:
        fid, _ = cat.upsert_file("v1", rel, 1, 1.0)
        cat.set_file_dataset(fid, "era5_pressure")

    findings = check_dataset(cat, "era5_pressure")
    rules = {f.rule_id for f in findings}
    assert "R102_COVERAGE_VAR" in rules   # 5/1 缺 u/v
    assert "R103_DATE_GAP" in rules       # 5/2 整日缺失

    run_dataset_checks(cat, ["era5_pressure"])
    rows = cat.list_quality_reports(dataset_id="era5_pressure")
    assert rows and all(r["target"] == "dataset:era5_pressure" for r in rows)
