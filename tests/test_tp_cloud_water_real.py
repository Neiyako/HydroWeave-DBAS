"""真实 ERA5 卷上的 EDA 测试（需要外接盘在线，默认无盘时自动 skip）。

运行方式：

    .venv/bin/python -m pytest -m real -q          # 只跑真实数据测试
    .venv/bin/python -m pytest -q                  # 全量；无外接盘时这些用例 skip

覆盖点：
1. catalog 索引完整性：q/t 配对、每年 5–10 月 184 天、文件确实在盘上；
2. 单日物理量合理区间：PW / RH500 / CFRAC / 估计地面气压 / 地下填充层占比；
3. 独立重算：直接读原始 NetCDF、手工算地形掩膜与梯形积分，与 EDA 结果对比；
4. 已完成的真实 run 目录回归：manifest、与旧扁平产物逐比特一致、图件与摘要。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import netCDF4
import numpy as np
import pytest
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.tp_cloud_water import eda  # noqa: E402
from experiments.tp_cloud_water.common import catalog_index, load_config  # noqa: E402

pytestmark = pytest.mark.real

CONFIG_PATH = ROOT / "experiments" / "tp_cloud_water" / "config.yaml"
RUN_ID = "real-2010"
RUN_DIR = ROOT / "data" / "analysis" / "tp_cloud_water" / "runs" / RUN_ID
LEGACY_DIR = ROOT / "data" / "analysis" / "tp_cloud_water"
YEARS = (2010, 2011, 2012, 2013)
DAYS_PER_YEAR = 184          # 5 月 1 日 – 10 月 31 日
FILES_PER_YEAR = DAYS_PER_YEAR * 2


def _real_config():
    if not CONFIG_PATH.exists():
        pytest.skip("缺少实验配置")
    cfg = load_config(CONFIG_PATH)
    catalog = Path(cfg["catalog"])
    if not catalog.is_absolute():
        catalog = ROOT / catalog
    if not catalog.exists():
        pytest.skip("catalog 不存在，先 dbas scan")
    try:
        index, rows, root = catalog_index(cfg)
    except ValueError as exc:
        pytest.skip(f"外接卷不在线: {exc}")
    if not index:
        pytest.skip("catalog 中没有 era5_pressure 的 q/t 文件")
    eda.configure(cfg)
    return cfg, index, rows, root


# ---------------------------------------------------------------------------
# 1. catalog / 文件系统一致性
# ---------------------------------------------------------------------------

def test_catalog_covers_full_season():
    _, index, rows, root = _real_config()
    assert root.exists(), f"卷挂载点不存在: {root}"
    assert len(rows) >= len(YEARS) * FILES_PER_YEAR
    dates = sorted(index)
    assert dates[0] == "20100501" and dates[-1] == "20131031"
    assert dates[0][:4] == "2010" and dates[-1][:4] == "2013"
    for year in YEARS:
        year_dates = [d for d in dates if d.startswith(str(year))]
        assert len(year_dates) == DAYS_PER_YEAR, (year, len(year_dates))
        assert year_dates[0] == f"{year}0501"
        assert year_dates[-1] == f"{year}1031"
        assert {d[4:6] for d in year_dates} == {"05", "06", "07", "08", "09", "10"}


def test_catalog_files_exist_on_disk_with_matching_size():
    _, index, rows, root = _real_config()
    missing = []
    size_mismatch = []
    for row in rows[:200]:                 # 抽查 200 个文件，避免每次全盘 stat
        path = root / row["rel_path"]
        if not path.exists():
            missing.append(str(path))
        elif path.stat().st_size != int(row["size_bytes"]):
            size_mismatch.append((path.name, path.stat().st_size, row["size_bytes"]))
    assert not missing, f"catalog 有记录但盘上缺文件: {missing[:3]}"
    assert not size_mismatch, f"文件大小与 catalog 不一致: {size_mismatch[:3]}"


# ---------------------------------------------------------------------------
# 2. 单日物理量合理区间
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_day():
    import dask
    _, index, _, _ = _real_config()
    date = "20100715"
    assert {"q", "t"} <= set(index[date]), f"{date} 无 q/t 配对文件"
    with xr.open_dataset(index[date]["q"]) as probe:
        grid = eda.crop_indices(probe)
    q, t, q_ds, t_ds = eda.lazy_day(index[date]["q"], index[date]["t"], grid)
    try:
        q_arr, t_arr = dask.compute(q, t, scheduler="threads", num_workers=2)
        fields = eda.compute_day(q_arr, t_arr)
    finally:
        q_ds.close()
        t_ds.close()
    return fields, q_arr, t_arr


def test_single_day_fields_are_physically_plausible(real_day):
    fields, q, _ = real_day
    pw = fields["pw"]
    lat = np.linspace(eda.LAT_MIN, eda.LAT_MAX, pw.shape[0])
    lon = np.linspace(eda.LON_MIN, eda.LON_MAX, pw.shape[1])
    assert pw.shape == (61, 141)
    assert np.isfinite(lat).all() and np.isfinite(lon).all()
    assert 0 < float(np.nanmean(pw)) < 80, "TP 夏季 PW 应在 0–80 mm"
    assert float(np.nanmax(pw)) < 150
    assert float(np.nanmean(fields["rh500"])) < 300.0     # 代码 clip 上限
    assert 0.0 <= float(np.nanmin(fields["rh500"]))
    cfrac = fields["cfrac"]
    assert 0.0 <= float(np.nanmin(cfrac)) and float(np.nanmax(cfrac)) <= 1.0
    assert 0.05 < float(np.nanmean(cfrac)) < 0.6, "单日 CFRAC 量级异常"
    surf_p = fields["surf_p"]
    # 最低格点约 5000 m（~540 hPa）；无填充连段的低海拔列取到最底层 1000 hPa。
    assert 400.0 < float(np.nanmin(surf_p)) <= 1000.0
    assert float(np.nanmax(surf_p)) <= 1000.0
    assert 0.1 < fields["subsurf"] < 0.4, "地下填充层占比应在 ~20% 量级"


# ---------------------------------------------------------------------------
# 3. 独立重算：原始 NetCDF → 手算地形掩膜 + 梯形积分
# ---------------------------------------------------------------------------

def _manual_pw_column(q_path: str, lat: float, lon: float) -> float:
    """不经过实验代码，直接从原始文件重算单柱 PW。"""
    ds = netCDF4.Dataset(q_path)
    try:
        lat_v = ds["latitude"][:]
        lon_v = ds["longitude"][:]
        if lat_v[0] < lat_v[-1]:                       # 升序也要能选到目标点
            i = int(np.argmin(np.abs(lat_v - lat)))
        else:
            i = int(np.argmin(np.abs(lat_v - lat)))
        j = int(np.argmin(np.abs(lon_v - lon)))
        level_hpa = ds["level"][:].astype("float64")
        tvals = netCDF4.num2date(ds["time"][:], ds["time"].units,
                                 ds["time"].calendar)
        hours = np.array([v.hour for v in tvals])
        sel = np.isin(hours, list(eda.HOURS))
        q = ds["q"][:][sel, :, i, j].astype("float64")   # (time, level)
    finally:
        ds.close()

    # 自底向上的等值连段 → 地形以下填充层
    eq = np.abs(np.diff(q, axis=1)) <= (1e-6 + 1e-3 * np.abs(q[:, :-1]))
    chain = np.flip(np.logical_and.accumulate(np.flip(eq, axis=1), axis=1), axis=1)
    # 连段顶（含）以上为可解析层（与 eda.sub_ground_mask 语义一致）
    first_true = np.where(chain.any(axis=1), np.argmax(chain, axis=1), q.shape[1] - 1)
    idx = np.arange(q.shape[1])[None, :]
    valid = idx <= first_true[:, None]

    p_pa = level_hpa * 100.0
    w = np.empty_like(p_pa)
    w[0] = (p_pa[1] - p_pa[0]) / 2.0
    w[-1] = (p_pa[-1] - p_pa[-2]) / 2.0
    w[1:-1] = (p_pa[2:] - p_pa[:-2]) / 2.0
    qv = np.where(valid, q, np.nan)
    pw = np.nansum(qv * w[None, :], axis=1) / eda.G
    return float(np.mean(pw))


def test_independent_pw_recomputation(real_day):
    fields, _, _ = real_day
    _, index, _, _ = _real_config()
    date = "20100715"
    # 实验网格：lat 25→40、lon 70→105 升序；取 35°N/75°E 对应的格点
    i = int(round((35.0 - eda.LAT_MIN) / 0.25))
    j = int(round((75.0 - eda.LON_MIN) / 0.25))
    expected = _manual_pw_column(index[date]["q"], 35.0, 75.0)
    actual = float(fields["pw"][i, j])
    assert abs(actual - expected) <= 0.01 * abs(expected) + 0.05, (
        f"独立重算 PW 不一致: pipeline={actual:.4f} manual={expected:.4f}")


# ---------------------------------------------------------------------------
# 4. 已完成真实 run 的产物回归（没有该 run 时跳过）
# ---------------------------------------------------------------------------

def _require_run():
    if not RUN_DIR.exists():
        pytest.skip(f"真实 run 产物不存在: {RUN_DIR}")
    return RUN_DIR


def test_real_run_manifest():
    run = _require_run()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert sorted(manifest["completed_years"]) == list(YEARS)
    assert manifest["failed_dates"] == []
    assert manifest["config_hash"] and manifest["input_fingerprint"]
    assert manifest["git_revision"] not in (None, "")
    names = {Path(a["path"]).name for a in manifest["artifacts"]}
    for year in YEARS:
        assert f"tp_monthly_{year}.nc" in names
        assert f"tp_daily_{year}.csv" in names
    assert manifest.get("plotted") is True
    for a in manifest["artifacts"]:
        path = Path(a["path"])
        if not path.is_absolute():
            path = ROOT / path
        assert path.exists(), f"manifest 登记的产物不存在: {path}"
        assert path.stat().st_size == a["size_bytes"]


def test_real_run_matches_legacy_outputs():
    run = _require_run()
    for year in YEARS:
        new = run / "yearly" / f"tp_monthly_{year}.nc"
        legacy = LEGACY_DIR / f"tp_monthly_{year}.nc"
        if not legacy.exists():
            pytest.skip(f"旧扁平产物不存在（已归档）: {legacy}")
        with xr.open_dataset(new) as a, xr.open_dataset(legacy) as b:
            for var in ("pw", "rh500", "cfrac", "surf_p"):
                diff = float(np.nanmax(np.abs(a[var].values - b[var].values)))
                assert diff == 0.0, f"{year} {var} 与旧产物不一致: max abs diff={diff}"


def test_real_run_figures_and_summary():
    run = _require_run()
    figs = sorted(p.name for p in (run / "figures").glob("*.png"))
    assert len(figs) == 8, figs
    summary = (run / "summary" / "tp_eda_summary.md").read_text(encoding="utf-8")
    for year in YEARS:
        assert f"| {year} | {DAYS_PER_YEAR} |" in summary
    assert "max abs diff" not in summary      # 摘要不应混入测试措辞
    assert "代理" in summary

def test_daily_table_has_no_gaps():
    run = _require_run()
    import csv
    rows = list(csv.DictReader((run / "tables" / "tp_daily.csv").open(encoding="utf-8")))
    assert len(rows) == len(YEARS) * DAYS_PER_YEAR
    for year in YEARS:
        dates = sorted(r["date"] for r in rows if r["year"] == str(year))
        assert dates[0] == f"{year}0501" and dates[-1] == f"{year}1031"
        assert len(dates) == len(set(dates)) == DAYS_PER_YEAR
        for day in range(1, 32):
            assert f"{year}05{day:02d}" in dates
            assert f"{year}10{day:02d}" in dates
    for row in rows:
        assert 0.0 <= float(row["pw_mm"]) < 150.0
        assert 0.0 <= float(row["rh500_pct"]) <= 300.0
        assert 0.0 <= float(row["cfrac_pct"]) <= 100.0
        assert 400.0 <= float(row["surf_p_hpa"]) <= 1000.0


def test_monthly_fields_consistent_with_daily_tables():
    """月平均场与逐日表的面积加权聚合必须一致（防止聚合口径分叉）。"""
    import csv

    run = _require_run()
    months = list(csv.DictReader((run / "tables" / "tp_monthly.csv").open(encoding="utf-8")))
    for year in YEARS:
        with xr.open_dataset(run / "yearly" / f"tp_monthly_{year}.nc") as ds:
            lat = ds["latitude"].values
            w = np.cos(np.deg2rad(lat))
            weight = np.broadcast_to(w[:, None], ds["pw"].shape[1:])
            for row in (r for r in months if r["year"] == str(year)):
                mi = list(ds["month"].values).index(int(row["month"]))
                for var, column, scale in (("pw", "pw_mm", 1.0),
                                           ("rh500", "rh500_pct", 1.0),
                                           ("cfrac", "cfrac_pct", 100.0)):
                    field = ds[var].isel(month=mi).values * scale
                    ok = np.isfinite(field)
                    got = float((field[ok] * weight[ok]).sum() / weight[ok].sum())
                    expected = float(row[column])
                    assert abs(got - expected) <= 1e-3 * abs(expected) + 1e-3, (
                        f"{year}-{row['month']} {var}: nc加权均值 {got:.6f} != 逐日表 {expected:.6f}")
                sp = ds["surf_p"].isel(month=mi).values
                got_sp = float(np.nanmean(sp))
                expected_sp = float(row["surf_p_hpa"])
                assert abs(got_sp - expected_sp) <= 1e-3, (
                    f"{year}-{row['month']} surf_p: {got_sp} != {expected_sp}")
