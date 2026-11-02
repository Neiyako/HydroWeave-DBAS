"""数据集识别与分组测试。"""
from __future__ import annotations

from dbas.analysis.datasets import detect_dataset, get_dataset_spec


def test_detect_dataset_rules():
    assert detect_dataset(
        "2010/ERA5_025dg_20100501_temperature.nc") == "era5_pressure"
    assert detect_dataset(
        "DATA/MODIS_jingwei/no2/2019.nc") == "modis_air_quality"
    assert detect_dataset(
        "DATA/MODIS_jingwei/co/ECHAP_CO_Y10K_2013_V1.nc") == "modis_air_quality"
    assert detect_dataset(
        "DATA/ERAINT_uvspeed10m_halfD.nc") == "eraint_wind"
    assert detect_dataset("2024/unknown_file.nc") == "unknown"


def test_era5_filename_parsing():
    spec = get_dataset_spec("era5_pressure")
    assert spec.parse_filename(
        "ERA5_025dg_20100501_temperature.nc") == ("20100501", "t")
    assert spec.parse_filename(
        "ERA5_025dg_20131031_specific_humidity.nc") == ("20131031", "q")
    assert spec.parse_filename("not_era5.nc") == (None, None)


def test_dataset_assignment_and_listing(cat, tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    cat.register_volume("v1", root)
    fid, _ = cat.upsert_file(
        "v1", "2010/ERA5_025dg_20100501_temperature.nc", 10, 1.0)
    cat.ensure_dataset("era5_pressure", "ERA5")
    cat.set_file_dataset(fid, "era5_pressure")
    rows = {r["dataset_id"]: r for r in cat.list_datasets()}
    assert rows["era5_pressure"]["files"] == 1
    assert rows["era5_pressure"]["label"] == "ERA5"
    assert cat.dataset_ids_for_volume("v1") == ["era5_pressure"]
