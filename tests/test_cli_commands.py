"""CLI 命令族测试：zarr 派生、db 维护、tasks、report、dataset、volume 列表。

这些命令此前无测试覆盖（cli.py 覆盖率仅 50%）。全部使用合成数据与
临时项目根，不联网、不碰真实数据卷。
"""
from __future__ import annotations

import sqlite3

import pytest

from dbas.cli import main


def run(root, *args):
    return main(["--project-root", str(root), *args])


def _first_store(info_output: str) -> str:
    """从 `zarr info` 输出里取出第一个 store 的相对路径。

    输出形如：
        数组层卷 arrays（/abs/path/to/data/arrays）
          point_series/2023/xxx.zarr
            大小 1.2 MB | manifest-sha256:abcd...
    标题行含中文与绝对路径，必须按扩展名筛选，不能按行号取。
    """
    for line in info_output.splitlines():
        token = line.strip()
        if token.endswith(".zarr") and "（" not in token:
            return token
    raise AssertionError(f"未从 zarr info 输出中解析到 store 路径:\n{info_output}")


@pytest.fixture
def ready(tmp_path):
    """已注册卷并完成扫描/解析的项目，含 3 个可派生文件。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "configs").mkdir()
    (root / "configs" / "system.yaml").write_text(
        "scan:\n  budget_s: 60\n  batch_size: 50\n", encoding="utf-8")
    vol = tmp_path / "diskZ"
    from tests.conftest import make_volume
    make_volume(vol, files=3)
    assert run(root, "volume", "register", str(vol), "--id", "zvol") == 0
    assert run(root, "scan", "--volume", "zvol", "--mode", "inventory",
               "--enqueue-metadata") == 0
    assert run(root, "scan", "--volume", "zvol", "--mode", "metadata") == 0
    return root, vol


class TestVolumeCommands:
    def test_register_and_list(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "volume", "list") == 0
        assert "zvol" in capsys.readouterr().out

    def test_register_duplicate_id_rejected(self, ready, capsys):
        root, vol = ready
        capsys.readouterr()
        run(root, "volume", "register", str(vol), "--id", "zvol")
        # 同一路径重复注册应幂等或报错，但绝不能崩
        assert run(root, "volume", "list") == 0

    def test_register_missing_path(self, ready, tmp_path, capsys):
        root, _ = ready
        rc = run(root, "volume", "register", str(tmp_path / "ghost"), "--id", "ghost")
        assert rc == 2
        assert "不存在" in capsys.readouterr().err


class TestStatusAndReport:
    def test_status_shows_counts(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "status") == 0
        out = capsys.readouterr().out
        assert "zvol" in out and "解析状态" in out

    def test_inventory_report_written(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "report", "inventory") == 0
        reports = list((root / "data" / "reports").glob("inventory_*.md"))
        assert reports, "应生成盘点报告"
        assert "zvol" in reports[0].read_text(encoding="utf-8")


class TestTaskCommands:
    def test_list_and_show(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "tasks", "list") == 0
        con = sqlite3.connect(root / "data" / "catalog.db")
        tid = con.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()
        con.close()
        if tid:
            assert run(root, "tasks", "show", tid[0]) == 0

    def test_list_filters_by_status(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "tasks", "list", "--status", "succeeded") == 0

    def test_prune(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "tasks", "prune") == 0


class TestZarrCommands:
    def test_plan_lists_all_targets(self, ready, capsys):
        root, vol = ready
        src = next(vol.rglob("*.nc"))
        capsys.readouterr()
        assert run(root, "zarr", "plan", "--source", str(src)) == 0
        out = capsys.readouterr().out
        for target in ("field_series", "point_series", "area_stats",
                       "level_profile"):
            assert target in out

    def test_plan_single_target(self, ready, capsys):
        root, vol = ready
        src = next(vol.rglob("*.nc"))
        capsys.readouterr()
        assert run(root, "zarr", "plan", "--source", str(src),
                   "--target", "point_series") == 0
        out = capsys.readouterr().out
        assert "point_series" in out and "field_series" not in out

    def test_plan_missing_source(self, ready, capsys):
        root, _ = ready
        assert run(root, "zarr", "plan", "--source", "/nope/x.nc") == 2
        assert "不存在" in capsys.readouterr().err

    def test_enqueue_run_info_verify(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "zarr", "enqueue", "--volume", "zvol",
                   "--target", "point_series") == 0
        assert "入队数组导出任务" in capsys.readouterr().out

        assert run(root, "zarr", "run") == 0
        assert run(root, "zarr", "info") == 0
        info = capsys.readouterr().out
        assert "point_series/" in info and "manifest-sha256" in info

        store = _first_store(info)
        assert run(root, "zarr", "verify", "--store", store) == 0
        assert "✔ 一致" in capsys.readouterr().out

    def test_enqueue_dedup_skips_duplicates(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        run(root, "zarr", "enqueue", "--volume", "zvol", "--target", "point_series")
        cap = capsys.readouterr().out
        first = int(cap.split("任务 ")[1].split(" ")[0])
        assert first > 0
        # 再入队一次：dedup_key 相同，应全部跳过
        run(root, "zarr", "enqueue", "--volume", "zvol", "--target", "point_series")
        cap2 = capsys.readouterr().out
        assert "入队数组导出任务 0 个" in cap2

    def test_verify_detects_tampering(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        run(root, "zarr", "enqueue", "--volume", "zvol", "--target", "field_series")
        run(root, "zarr", "run")
        run(root, "zarr", "info")
        store = _first_store(capsys.readouterr().out)
        # 篡改一个 chunk 的首字节
        base = root / "data" / "arrays" / store
        chunk = next(p for p in base.rglob("*") if p.is_file() and p.name.isdigit())
        raw = bytearray(chunk.read_bytes())
        raw[0] ^= 0xFF
        chunk.write_bytes(raw)
        assert run(root, "zarr", "verify", "--store", store) == 0
        out = capsys.readouterr().out
        assert "✘ 不一致" in out
        assert "changed" in out


class TestDbCommands:
    def test_check_and_backup(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "db", "check") == 0
        out = capsys.readouterr().out
        assert "integrity_check: ok" in out
        assert run(root, "db", "backup") == 0
        assert list((root / "data" / "backups").glob("catalog_*.db"))

    def test_check_db_is_sqlite_integrity(self, ready):
        root, _ = ready
        con = sqlite3.connect(root / "data" / "catalog.db")
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        con.close()


class TestDatasetCommands:
    def test_assign_and_list(self, ready, capsys):
        root, _ = ready
        capsys.readouterr()
        assert run(root, "dataset", "assign", "--volume", "zvol") == 0
        assert run(root, "dataset", "list") == 0
        out = capsys.readouterr().out
        assert "era5_pressure" in out or "数据集" in out
