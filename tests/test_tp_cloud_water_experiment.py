"""云水代理实验链路测试：合成 fixture → EDA → manifest → 断点续跑 → 制图。

不接触真实 ERA5 外接卷；合成数据由 scripts/demo/make_tp_cloud_water_fixture.py 生成。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    env["MPLBACKEND"] = "Agg"
    return env


def _run(args, cwd=ROOT):
    return subprocess.run([sys.executable, *args], cwd=cwd, env=_env(),
                          capture_output=True, text=True, timeout=300)


def test_fixture_eda_resume_plot(tmp_path):
    fixture = tmp_path / "fixture"
    build = _run([str(ROOT / "scripts/demo/make_tp_cloud_water_fixture.py"),
                  str(fixture), "--days", "3"])
    assert build.returncode == 0, build.stderr
    config = fixture / "config.yaml"
    assert config.exists()
    run_dir = fixture / "analysis" / "runs" / "fixture"

    eda = _run(["-m", "experiments.tp_cloud_water.eda",
                "--config", str(config), "--run-id", "fixture"])
    assert eda.returncode == 0, eda.stderr
    assert "DONE run=fixture" in eda.stdout
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["completed_years"] == [2010]
    assert manifest["failed_dates"] == []
    paths = {Path(item["path"]).name for item in manifest["artifacts"]}
    assert {"tp_monthly_2010.nc", "tp_daily_2010.csv",
            "tp_daily.csv", "tp_monthly.csv"} <= paths
    yearly = run_dir / "yearly" / "tp_monthly_2010.nc"
    assert yearly.exists() and yearly.stat().st_size > 0
    assert not list(run_dir.rglob("*.part"))

    # 同 run-id 再跑：跳过已完成年份，清单输入指纹不变
    again = _run(["-m", "experiments.tp_cloud_water.eda",
                  "--config", str(config), "--run-id", "fixture"])
    assert again.returncode == 0, again.stderr
    assert "[skip] 2010" in again.stdout
    resumed = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert resumed["input_fingerprint"] == manifest["input_fingerprint"]
    assert resumed["config_hash"] == manifest["config_hash"]

    # 制图：图件与摘要落到 run 目录，并把产物登记回 manifest
    plot = _run(["-m", "experiments.tp_cloud_water.plot",
                 "--config", str(config), "--run-id", "fixture"])
    assert plot.returncode == 0, plot.stderr
    figures = sorted(p.name for p in (run_dir / "figures").glob("*.png"))
    assert figures == ["fig_annual_comparison.png", "fig_crosssection.png",
                       "fig_glacier_elevation.png", "fig_maps_2010.png",
                       "fig_timeseries.png"]
    summary = (run_dir / "summary" / "tp_eda_summary.md").read_text(encoding="utf-8")
    assert "run `fixture`" in summary and "| 2010 | 3 |" in summary
    plotted = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert plotted["plotted"] is True
    plotted_names = {Path(item["path"]).name for item in plotted["artifacts"]}
    assert set(figures) <= plotted_names


def test_config_mismatch_requires_force(tmp_path):
    fixture = tmp_path / "fixture"
    build = _run([str(ROOT / "scripts/demo/make_tp_cloud_water_fixture.py"),
                  str(fixture), "--days", "2"])
    assert build.returncode == 0, build.stderr
    config = fixture / "config.yaml"
    run = _run(["-m", "experiments.tp_cloud_water.eda",
                "--config", str(config), "--run-id", "r"])
    assert run.returncode == 0, run.stderr

    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("rh_cloud_pct: 80.0", "rh_cloud_pct: 75.0"),
                      encoding="utf-8")
    mismatch = _run(["-m", "experiments.tp_cloud_water.eda",
                     "--config", str(config), "--run-id", "r"])
    assert mismatch.returncode != 0
    assert "不一致" in mismatch.stderr

    forced = _run(["-m", "experiments.tp_cloud_water.eda",
                   "--config", str(config), "--run-id", "r", "--force"])
    assert forced.returncode == 0, forced.stderr
    manifest = json.loads((fixture / "analysis/runs/r/manifest.json").read_text(
        encoding="utf-8"))
    assert manifest["config"]["rh_cloud_pct"] == 75.0


def test_missing_glacier_mask_fails_clearly(tmp_path):
    fixture = tmp_path / "fixture"
    build = _run([str(ROOT / "scripts/demo/make_tp_cloud_water_fixture.py"),
                  str(fixture), "--days", "1"])
    assert build.returncode == 0, build.stderr
    (fixture / "glacier_mask_025.npz").unlink()
    run = _run(["-m", "experiments.tp_cloud_water.eda",
                "--config", str(fixture / "config.yaml"), "--run-id", "r"])
    assert run.returncode != 0
    assert "glacier mask" in (run.stderr + run.stdout)
