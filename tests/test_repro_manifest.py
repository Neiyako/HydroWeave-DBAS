"""运行清单（dbas.repro.manifest）的单元测试：哈希稳定、指纹、原子写。"""
from __future__ import annotations

import json
import subprocess

import pytest

from dbas.repro.manifest import (
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    config_hash,
    file_sha256,
    git_revision,
    input_fingerprint,
    load_json,
    new_manifest,
    package_versions,
    update_manifest,
)


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert config_hash({"b": 1, "a": 2}) == config_hash({"a": 2, "b": 1})
    assert config_hash({"a": 1}) != config_hash({"a": 2})
    assert len(config_hash({"a": 1})) == 16


def test_input_fingerprint_ignores_row_order_but_tracks_content():
    rows = [
        {"volume_id": "v", "rel_path": "b.nc", "size_bytes": 2, "mtime": 2.0},
        {"volume_id": "v", "rel_path": "a.nc", "size_bytes": 1, "mtime": 1.0},
    ]
    fp = input_fingerprint(rows)
    assert fp == input_fingerprint(list(reversed(rows)))
    changed_size = [dict(rows[0], size_bytes=99), rows[1]]
    assert input_fingerprint(changed_size) != fp
    changed_mtime = [dict(rows[0], mtime=99.0), rows[1]]
    assert input_fingerprint(changed_mtime) != fp
    assert input_fingerprint([]) == input_fingerprint([])


def test_file_sha256_matches_hashlib(tmp_path):
    import hashlib
    p = tmp_path / "x.bin"
    payload = b"dbas" * 1000
    p.write_bytes(payload)
    assert file_sha256(p) == hashlib.sha256(payload).hexdigest()
    assert file_sha256(p, chunk_size=7) == hashlib.sha256(payload).hexdigest()


def test_atomic_write_leaves_no_part_file(tmp_path):
    target = tmp_path / "manifest.json"
    atomic_write_json(target, {"b": 1, "a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2, "b": 1}
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob(".*.part"))


def test_atomic_write_failure_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    target.write_text("{}", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(target, {"a": 1})
    # 原文件保持可读，临时文件已清理
    assert load_json(target) == {}
    assert not list(tmp_path.glob(".*.part"))


def test_update_manifest_merges_and_timestamps(tmp_path):
    path = tmp_path / "m.json"
    atomic_write_json(path, {"completed_years": [2010], "status": "running"})
    updated = update_manifest(path, completed_years=[2010, 2011], status="completed")
    assert updated["completed_years"] == [2010, 2011]
    assert updated["status"] == "completed"
    assert "updated_at" in updated
    assert load_json(path)["status"] == "completed"


def test_new_manifest_snapshot(tmp_path):
    manifest = new_manifest(project_root=tmp_path, config={"years": [2010]},
                            input_fingerprint_value="abc", run_id="r1",
                            packages=("numpy",))
    assert manifest["schema_version"] == 1
    assert manifest["status"] == "planned"
    assert manifest["config_hash"] == config_hash({"years": [2010]})
    assert manifest["input_fingerprint"] == "abc"
    assert set(manifest["completed_years"]) == set()
    assert manifest["packages"]["numpy"] != "not-installed"
    assert len(manifest["git_revision"]) in (7, 40) or manifest["git_revision"] == "unknown"


def test_package_versions_marks_missing():
    versions = package_versions(("numpy", "definitely-not-installed-xyz"))
    assert versions["numpy"] != "not-installed"
    assert versions["definitely-not-installed-xyz"] == "not-installed"


def test_git_revision_unknown_outside_repo(tmp_path):
    if (tmp_path / ".git").exists():
        pytest.skip("tmp_path 意外是 git 仓库")
    assert git_revision(tmp_path) == "unknown"
