"""catalog 单元测试：迁移、幂等、卷解析、缺失标记。"""
from __future__ import annotations

import pytest

from dbas.catalog.db import current_version, migrate
from dbas.catalog.repository import VolumeOffline, make_file_id
from dbas.core.exceptions import CatalogError


def test_migrate_creates_tables_and_is_idempotent(cat):
    assert current_version(cat.conn) == 1
    tables = {r[0] for r in cat.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("volumes", "files", "variables", "dimensions", "scan_runs",
              "tasks", "task_attempts", "artifacts", "schema_migrations"):
        assert t in tables, f"缺少表 {t}"
    # 重复迁移不报错、版本不变
    assert migrate(cat.conn) == 1


def test_register_volume_and_resolve_root(cat, tmp_path):
    root = tmp_path / "diskA"
    root.mkdir()
    vol = cat.register_volume("v1", root, label="测试盘")
    assert vol["status"] == "online"
    assert cat.resolve_volume_root("v1") == root


def test_volume_offline_detected_not_deleted(cat, tmp_path):
    root = tmp_path / "diskA"
    root.mkdir()
    cat.register_volume("v1", root)
    file_id, _ = cat.upsert_file("v1", "2024/a.nc", 100, 1.0)
    # 模拟硬盘拔出
    root.rmdir()
    with pytest.raises(VolumeOffline):
        cat.resolve_volume_root("v1")
    assert cat.get_volume("v1")["status"] == "offline"
    # 卷下文件仍是 active（offline 标记需显式调用）
    assert cat.get_file(file_id)["status"] == "active"
    cat.mark_volume_files_offline("v1")
    assert cat.get_file(file_id)["status"] == "offline"
    # 重新挂载（换一个路径，模拟路径变化）后文件恢复
    new_root = tmp_path / "mnt2"
    new_root.mkdir()
    cat.register_volume("v1", new_root)
    assert cat.resolve_volume_root("v1") == new_root


def test_register_volume_requires_existing_dir(cat, tmp_path):
    with pytest.raises(CatalogError):
        cat.register_volume("v1", tmp_path / "nope")


def test_upsert_file_idempotent_and_change_detection(cat, tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    cat.register_volume("v1", root)
    id1, changed1 = cat.upsert_file("v1", "a.nc", 100, 1000.0)   # 新文件
    id2, changed2 = cat.upsert_file("v1", "a.nc", 100, 1000.0)   # 无变化
    assert id1 == id2 and changed1 and not changed2
    assert cat.count_files(volume_id="v1") == 1
    # 修改 mtime/size → changed=True，parse_status 重置
    cat.update_parse_result(
        id1, fmt="nc4", time_min="2024-01-01T00:00:00", time_max="2024-01-01T18:00:00",
        lat=(30.0, 30.5), lon=(110.0, 110.5), variables=[], dimensions=[])
    id3, changed3 = cat.upsert_file("v1", "a.nc", 200, 2000.0)
    assert changed3
    assert cat.get_file(id3)["parse_status"] == "unparsed"


def test_missing_pending_flow(cat, tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    cat.register_volume("v1", root)
    cat.upsert_file("v1", "keep.nc", 1, 1.0)
    cat.upsert_file("v1", "gone.nc", 1, 1.0)
    assert cat.mark_missing_pending("v1", ["gone.nc"]) == 1
    assert cat.get_file(make_file_id("v1", "gone.nc"))["status"] == "missing_pending"
    # 重新发现后恢复 active
    cat.upsert_file("v1", "gone.nc", 1, 2.0)
    assert cat.get_file(make_file_id("v1", "gone.nc"))["status"] == "active"
