"""scanner 集成测试：inventory / metadata / verify / checksum / incremental / 损坏文件。"""
from __future__ import annotations

import re
import time

import pytest

from dbas.catalog.repository import make_file_id
from dbas.core.exceptions import ScanError
from dbas.scanner.scan import (checksum_one, scan_inventory,
                               scan_metadata_one, verify_one)


def test_inventory_counts_and_registers(cat, volume_dir):
    cat.register_volume("v1", volume_dir)
    stats = scan_inventory(cat, "v1", [".nc"])
    assert stats.seen == 4                      # 3 正常 + 1 损坏
    assert cat.count_files(volume_id="v1") == 4


def test_inventory_all_files_keeps_non_netcdf_catalog_only(cat, volume_dir):
    """全量清单化：非 netCDF 登记为 not_netcdf 条目，不入解析队列。"""
    (volume_dir / "DATA").mkdir()
    (volume_dir / "DATA" / "readme.txt").write_text("x")
    (volume_dir / "DATA" / "field.tif").write_bytes(b"II*\x00")
    junk = volume_dir / "$RECYCLE.BIN"
    junk.mkdir()
    (junk / "ghost.nc").write_bytes(b"NOT A NETCDF FILE")

    cat.register_volume("v1", volume_dir)
    stats = scan_inventory(cat, "v1", ["*"], enqueue_metadata=True,
                           netcdf_extensions=[".nc"],
                           exclude_dirs=["$RECYCLE.BIN"])
    assert stats.seen == 6                       # 4 个 .nc + txt + tif，回收站剪枝
    catalog_only = list(cat.iter_files(volume_id="v1", parse_status="not_netcdf"))
    assert {f["format"] for f in catalog_only} == {"txt", "tif"}
    queued = [t["dedup_key"] for t in cat.list_tasks(status="pending", limit=50)]
    assert len(queued) == 4                      # 只有 netCDF 入队
    assert all(not k.endswith((".txt", ".tif")) for k in queued)


def test_inventory_missing_pending_only_after_complete_walk(cat, volume_dir):
    cat.register_volume("v1", volume_dir)
    scan_inventory(cat, "v1", [".nc"])
    # 删除一个文件后重扫
    (volume_dir / "2024/era5_00.nc").unlink()
    stats = scan_inventory(cat, "v1", [".nc"])
    assert stats.missing_pending == 1
    assert cat.count_files(volume_id="v1", status="missing_pending") == 1
    assert cat.count_files(volume_id="v1", status="active") == 3


def test_inventory_does_not_touch_known_non_netcdf_rows(cat, volume_dir):
    cat.register_volume("v1", volume_dir)
    cat.upsert_file("v1", "notes.txt", 10, 1.0)   # 其他工具写入的普通文件
    scan_inventory(cat, "v1", [".nc"])
    row = cat.get_file(make_file_id("v1", "notes.txt"))
    assert row["status"] == "active"


def test_metadata_parse_extracts_expected_fields(cat, volume_dir):
    cat.register_volume("v1", volume_dir)
    scan_inventory(cat, "v1", [".nc"])
    fid = make_file_id("v1", "2024/era5_00.nc")
    result = scan_metadata_one({"file_id": fid, "volume_id": "v1",
                                     "rel_path": "2024/era5_00.nc"}, cat)
    f = cat.get_file(fid)
    assert f["parse_status"] == "parsed"
    assert f["format"] == "nc4"
    assert f["time_min"].startswith("2024-01-01T00:00:00")
    assert f["time_max"].startswith("2024-01-01T06:00:00")
    assert f["lat_min"] == pytest.approx(30.0)
    assert f["lon_max"] == pytest.approx(110.5)
    vars_by_name = {v["name"]: v for v in cat.conn.execute(
        "SELECT * FROM variables WHERE file_id=?", (fid,)).fetchall()}
    assert "t2m" in vars_by_name
    assert vars_by_name["t2m"]["units"] == "K"
    assert vars_by_name["t2m"]["standard_name"] == "air_temperature"
    assert vars_by_name["t2m"]["fill_value"]     # 缺测标记已记录
    dims = {r[0]: r[1] for r in cat.conn.execute(
        "SELECT name, size FROM dimensions WHERE file_id=?", (fid,)).fetchall()}
    assert dims == {"time": 2, "latitude": 3, "longitude": 3}
    assert result["n_vars"] == 4


def test_corrupt_file_fails_alone_does_not_block_batch(cat, volume_dir):
    """核心故障测试：单个损坏文件只影响自己。"""
    cat.register_volume("v1", volume_dir)
    scan_inventory(cat, "v1", [".nc"])
    good_id = make_file_id("v1", "2024/era5_01.nc")
    bad_id = make_file_id("v1", "2024/corrupt.nc")
    scan_metadata_one({"file_id": good_id, "volume_id": "v1",
                            "rel_path": "2024/era5_01.nc"}, cat)
    with pytest.raises(ScanError):
        scan_metadata_one({"file_id": bad_id, "volume_id": "v1",
                                "rel_path": "2024/corrupt.nc"}, cat)
    assert cat.get_file(good_id)["parse_status"] == "parsed"
    bad = cat.get_file(bad_id)
    assert bad["parse_status"] == "failed"
    assert bad["parse_error"]                   # 诊断信息已保存


def test_verify_and_checksum(cat, volume_dir):
    cat.register_volume("v1", volume_dir)
    scan_inventory(cat, "v1", [".nc"])
    fid = make_file_id("v1", "2024/era5_02.nc")
    assert verify_one({"file_id": fid, "volume_id": "v1",
                            "rel_path": "2024/era5_02.nc"}, cat) == {"readable": True}
    f = cat.get_file(fid)
    assert f["last_check_result"] == "ok"
    result = checksum_one({"file_id": fid, "volume_id": "v1",
                                "rel_path": "2024/era5_02.nc"}, cat)
    assert re.fullmatch(r"[0-9a-f]{64}", result["sha256"])
    f = cat.get_file(fid)
    assert f["fingerprint_algo"] == "sha256"
    assert f["fingerprint"] == result["sha256"]


def test_incremental_only_enqueues_changed(cat, volume_dir):
    cat.register_volume("v1", volume_dir)
    scan_inventory(cat, "v1", [".nc"], enqueue_metadata=True)
    n_first = cat.count_tasks_by_status().get("pending", 0)
    assert n_first == 4
    # 不修改任何文件 → 无新任务
    stats = scan_inventory(cat, "v1", [".nc"], enqueue_metadata=True,
                           incremental=True)
    assert stats.changed == 0
    # 修改一个文件（重写内容改变 mtime）
    time.sleep(0.05)
    from tests.conftest import make_nc
    make_nc(volume_dir / "2024/era5_00.nc", n_time=9)
    stats = scan_inventory(cat, "v1", [".nc"], enqueue_metadata=True,
                           incremental=True)
    assert stats.changed == 1
    assert cat.count_tasks_by_status().get("pending", 0) == n_first + 1
