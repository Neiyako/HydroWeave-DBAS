"""CLI 端到端测试：从注册卷到报告的完整流程（全部使用合成数据）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dbas.cli import main


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "configs").mkdir()
    (root / "configs" / "system.yaml").write_text(
        "scan:\n  budget_s: 60\n  batch_size: 50\n", encoding="utf-8")
    vol = tmp_path / "diskA"
    from tests.conftest import make_volume
    make_volume(vol, files=3, corrupt_one=True)
    return root, vol


def run(project_root, *args):
    rc = main(["--project-root", str(project_root), *args])
    return rc


def test_full_workflow(project, capsys):
    root, vol = project
    # 1. status（空库）
    assert run(root, "status") == 0
    # 2. 注册卷
    assert run(root, "volume", "register", str(vol), "--id", "testvol") == 0
    # 3. 盘点 + 入队元数据解析
    assert run(root, "scan", "--volume", "testvol", "--mode", "inventory",
               "--enqueue-metadata") == 0
    out = capsys.readouterr().out
    assert "发现 4 个文件" in out
    # 4. 执行元数据解析任务（损坏文件会失败重试，但不阻塞）
    assert run(root, "scan", "--volume", "testvol", "--mode", "metadata") == 0
    out = capsys.readouterr().out
    assert "成功 3" in out
    # 5. verify + checksum
    assert run(root, "verify", "--volume", "testvol") == 0
    assert run(root, "checksum", "--volume", "testvol") == 0
    # 6. 任务队列可见
    assert run(root, "tasks", "list") == 0
    capsys.readouterr()
    # 7. 报告生成且无半截文件
    assert run(root, "report", "inventory") == 0
    reports = list((root / "data" / "reports").glob("inventory_*.md"))
    assert len(reports) == 1
    assert not list((root / "data" / "reports").glob("*.tmp"))
    text = reports[0].read_text(encoding="utf-8")
    assert "testvol" in text and "parsed" in text
    # 8. 数据库检查与备份
    assert run(root, "db", "check") == 0
    assert run(root, "db", "backup") == 0
    backups = list((root / "data" / "backups").glob("catalog_*.db"))
    assert len(backups) == 1
    # 原始数据未被修改（损坏文件仍是原始字节）
    assert (vol / "2024" / "corrupt.nc").read_bytes() == b"NOT A NETCDF FILE" * 10


def test_budget_interrupt_and_resume(project, capsys):
    """预算耗尽后中断，重跑命令续跑。"""
    root, vol = project
    assert run(root, "volume", "register", str(vol), "--id", "testvol") == 0
    assert run(root, "scan", "--volume", "testvol", "--mode", "inventory",
               "--enqueue-metadata") == 0
    assert run(root, "scan", "--volume", "testvol", "--mode", "metadata",
               "--batch-size", "2") == 0
    out = capsys.readouterr().out
    assert "停止原因: batch" in out
    # 续跑：剩余任务全部处理完
    assert run(root, "scan", "--volume", "testvol", "--mode", "metadata") == 0
    out = capsys.readouterr().out
    assert "成功 2" in out
    assert "queue_empty" in out or "成功" in out


def test_profile_and_qc_cli(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "configs").mkdir()
    (root / "configs" / "system.yaml").write_text(
        "profile:\n  budget_s: 30\n  batch_size: 2\n", encoding="utf-8")
    vol = tmp_path / "diskE"
    from tests.conftest import make_layered_nc
    make_layered_nc(vol / "2010/ERA5_025dg_20100501_temperature.nc")
    make_layered_nc(vol / "2010/ERA5_025dg_20100502_temperature.nc",
                    tail_constant=True)

    assert run(root, "volume", "register", str(vol), "--id", "era5vol") == 0
    assert run(root, "scan", "--volume", "era5vol", "--mode", "inventory",
               "--enqueue-metadata") == 0
    assert run(root, "scan", "--volume", "era5vol", "--mode", "metadata") == 0
    capsys.readouterr()

    assert run(root, "dataset", "list") == 0
    out = capsys.readouterr().out
    assert "era5_pressure" in out

    assert run(root, "profile", "--volume", "era5vol", "--limit", "2",
               "--batch-size", "2") == 0
    out = capsys.readouterr().out
    assert "报告:" in out
    reports = list((root / "data" / "reports").glob("profile_qc_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "era5_pressure" in text
    assert "R002_TAIL_CONSTANT" in text

    assert run(root, "qc", "report", "--volume", "era5vol") == 0
    assert len(list((root / "data" / "reports").glob("profile_qc_*.md"))) == 2


def test_volume_offline_not_destructive(project, tmp_path, capsys):
    """硬盘拔出后扫描给出明确错误，索引不丢失。"""
    root, vol = project
    assert run(root, "volume", "register", str(vol), "--id", "testvol") == 0
    assert run(root, "scan", "--volume", "testvol", "--mode", "inventory") == 0
    vol.rename(vol.parent / "diskA_detached")   # 模拟拔盘（挂载点消失）
    rc = run(root, "scan", "--volume", "testvol", "--mode", "inventory")
    assert rc == 3
    out = capsys.readouterr().err
    assert "不可访问" in out
    # 索引仍在（用 db 直接确认，不经过文件系统）
    db = sqlite3.connect(root / "data" / "catalog.db")
    n = db.execute(
        "SELECT COUNT(*) FROM files WHERE volume_id='testvol'").fetchone()[0]
    db.close()
    assert n == 4
