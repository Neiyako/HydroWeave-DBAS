"""在线备份（dbas.storage.backup）的单元测试：完整性、原子替换、失败清理。"""
from __future__ import annotations

import sqlite3

import pytest

from dbas.storage.backup import backup_database


def _make_db(path, rows=("a", "b")):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE item (name TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO item (name) VALUES (?)", [(name,) for name in rows])
    db.commit()
    db.close()
    return path


def _names(path):
    db = sqlite3.connect(path)
    try:
        return [row[0] for row in db.execute("SELECT name FROM item ORDER BY name")]
    finally:
        db.close()


def test_backup_copies_live_database(tmp_path):
    source = _make_db(tmp_path / "catalog.db")
    destination = tmp_path / "backups" / "catalog.db"
    backup_database(source, destination)
    assert destination.exists()
    assert _names(destination) == ["a", "b"]
    assert not (tmp_path / "backups" / "catalog.db.part").exists()


def test_backup_is_atomic_and_replaces_existing(tmp_path):
    source = _make_db(tmp_path / "catalog.db", rows=("new",))
    destination = tmp_path / "backups" / "catalog.db"
    _make_db(destination, rows=("old",))
    backup_database(source, destination)
    assert _names(destination) == ["new"]
    assert [p.name for p in destination.parent.iterdir()] == ["catalog.db"]


def test_backup_failure_keeps_previous_copy(tmp_path, monkeypatch):
    source = _make_db(tmp_path / "catalog.db")
    destination = tmp_path / "backups" / "catalog.db"
    _make_db(destination, rows=("old",))

    def broken_check(conn):
        return "corrupt", []

    monkeypatch.setattr("dbas.storage.backup._check_connection", broken_check)
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, destination)
    assert _names(destination) == ["old"]
    assert not (destination.parent / "catalog.db.part").exists()


def test_backup_rejects_foreign_key_violation(tmp_path, monkeypatch):
    source = _make_db(tmp_path / "catalog.db")
    destination = tmp_path / "backups" / "catalog.db"
    monkeypatch.setattr("dbas.storage.backup._check_connection",
                        lambda conn: ("ok", [("item", 0, "item", 0)]))
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, destination)
    assert not destination.exists()
    assert not (destination.parent / "catalog.db.part").exists()
