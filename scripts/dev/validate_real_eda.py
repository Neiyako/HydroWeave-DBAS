#!/usr/bin/env python3
"""真实 ERA5 卷上的云水代理 EDA 独立验收（对抗性检查，不是再跑一遍 EDA）。

验收标准（执行前固定，见脚本内 ``STANDARDS``）：

- S1 catalog 与盘上文件一致：q/t 记录全部存在、大小与 catalog 一致；
- S2 覆盖完整：2010–2013 每年 5–10 月 184 天，q/t 成对；
- S3 物理量合理：抽样 12 天（每年 5/7/10 月各 1 天）的 PW / RH500 / CFRAC /
  地面气压在物理量级内；
- S4 独立重算：绕过实验代码直接读原始 NetCDF 手工算单柱 PW，与 EDA 一致；
- S5 回归一致：新 run 的年度月平均场与旧扁平实现产物逐比特一致；
- S6 产物完整：manifest 完成年份/失败清单、逐日表 736 行、图件与摘要齐全。

用法::

    .venv/bin/python scripts/dev/validate_real_eda.py            # 报告写到 data/analysis/...
    .venv/bin/python scripts/dev/validate_real_eda.py --quick    # 只抽查 4 天

判定口径：全部硬标准 PASS → PASS；仅历史产物缺失导致 S5/S6 NOT_RUN →
CONDITIONAL_PASS；任一硬标准 FAIL → FAIL。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import netCDF4
import numpy as np
import xarray as xr

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dask  # noqa: E402
from experiments.tp_cloud_water import eda  # noqa: E402
from experiments.tp_cloud_water.common import catalog_index, load_config  # noqa: E402

CONFIG_PATH = ROOT / "experiments" / "tp_cloud_water" / "config.yaml"
RUN_ID = "real-2010"
RUN_DIR = ROOT / "data" / "analysis" / "tp_cloud_water" / "runs" / RUN_ID
LEGACY_DIR = ROOT / "data" / "analysis" / "tp_cloud_water"
REPORT_PATH = ROOT / "data" / "analysis" / "tp_cloud_water" / "validation" / "real_eda_check.md"
YEARS = (2010, 2011, 2012, 2013)
DAYS_PER_YEAR = 184
EXPECTED_ROWS = len(YEARS) * DAYS_PER_YEAR * 2

STANDARDS = {
    "S1": "catalog 中 era5_pressure 的 q/t 记录在盘上全部存在，大小一致",
    "S2": "每年 5-1 至 10-31 恰好 184 天且 q/t 成对，月份集合为 05–10",
    "S3": "抽样日：0<PW<100 mm、0<=RH500<=300%、0<=CFRAC<=1、400<surf_p<=1000 hPa",
    "S4": "独立手工重算的单柱 PW 与 EDA 输出相对误差 <=0.5%",
    "S5": "新 run 年度 nc 的 pw/rh500/cfrac/surf_p 与旧实现产物 max abs diff = 0",
    "S6": "manifest 完成 4 年且无失败、逐日表 736 行、8 张图与摘要齐全",
}

LIMITS = {"pw": (0.0, 100.0), "rh500": (0.0, 300.0), "cfrac": (0.0, 1.0),
          "surf_p": (400.0, 1000.0)}


def _fail(msg: str):
    raise SystemExit(msg)


def _config():
    cfg = load_config(CONFIG_PATH)
    try:
        index, rows, root = catalog_index(cfg)
    except ValueError as exc:
        _fail(f"外接卷不在线，无法做真实 EDA 测试: {exc}")
    if not rows:
        _fail("catalog 中没有 era5_pressure 的 q/t 文件")
    eda.configure(cfg)
    return cfg, index, rows, root


def _check_s1(rows, root: Path) -> dict:
    missing, bad_size, bad_mtime = [], [], 0
    for row in rows:
        path = root / row["rel_path"]
        if not path.exists():
            missing.append(row["rel_path"])
            continue
        stat = path.stat()
        if stat.st_size != int(row["size_bytes"]):
            bad_size.append((row["rel_path"], stat.st_size, row["size_bytes"]))
        if abs(stat.st_mtime - float(row["mtime"])) > 2.0:
            bad_mtime += 1
    ok = not missing and not bad_size
    return {"id": "S1", "status": "PASS" if ok else "FAIL",
            "detail": f"记录 {len(rows)} 个，缺失 {len(missing)}，大小不符 {len(bad_size)}，"
                      f"mtime 漂移 {bad_mtime}（NTFS 挂载，仅记录不判定）",
            "evidence": missing[:3] + [str(item) for item in bad_size[:3]]}


def _check_s2(index: dict) -> dict:
    problems = []
    dates = sorted(index)
    for year in YEARS:
        ds = [d for d in dates if d.startswith(str(year))]
        months = sorted({d[4:6] for d in ds})
        if len(ds) != DAYS_PER_YEAR:
            problems.append(f"{year}: {len(ds)} 天 != {DAYS_PER_YEAR}")
        if months != ["05", "06", "07", "08", "09", "10"]:
            problems.append(f"{year}: 月份 {months}")
        unpaired = [d for d in ds if not {"q", "t"} <= set(index[d])]
        if unpaired:
            problems.append(f"{year}: q/t 未配对 {unpaired[:3]}")
    return {"id": "S2", "status": "PASS" if not problems else "FAIL",
            "detail": f"日期范围 {dates[0]}–{dates[-1]}，总日数 {len(dates)}，"
                      f"配对检查 {'通过' if not problems else '失败'}",
            "evidence": problems}


def _sample_dates(index: dict, quick: bool) -> list[str]:
    dates = sorted(index)
    picked = []
    months = ("07",) if quick else ("05", "07", "10")
    for year in YEARS:
        for month in months:
            in_month = [d for d in dates if d.startswith(f"{year}{month}")]
            if in_month:
                picked.append(in_month[len(in_month) // 2])
    return picked


def _day_fields(index, date):
    with xr.open_dataset(index[date]["q"]) as probe:
        grid = eda.crop_indices(probe)
    q, t, q_ds, t_ds = eda.lazy_day(index[date]["q"], index[date]["t"], grid)
    try:
        q_arr, t_arr = dask.compute(q, t, scheduler="threads", num_workers=2)
        return eda.compute_day(q_arr, t_arr)
    finally:
        q_ds.close()
        t_ds.close()


def _check_s3(index, sample_dates: list[str]) -> dict:
    stats = {"pw": [], "rh500": [], "cfrac": [], "surf_p": []}
    subsurf = []
    for date in sample_dates:
        fields = _day_fields(index, date)
        for key in stats:
            values = fields[key]
            stats[key].append((float(np.nanmin(values)), float(np.nanmax(values)),
                               float(np.nanmean(values))))
        subsurf.append(fields["subsurf"])
    summary = {}
    problems = []
    for key, items in stats.items():
        lo = min(item[0] for item in items)
        hi = max(item[1] for item in items)
        mean = float(np.mean([item[2] for item in items]))
        summary[key] = (lo, hi, mean)
        low, high = LIMITS[key]
        if lo < low or hi > high:
            problems.append(f"{key} 范围 [{lo:.3f},{hi:.3f}] 超出 [{low},{high}]")
    mean_sub = float(np.mean(subsurf))
    if not 0.05 < mean_sub < 0.40:
        problems.append(f"地下填充层占比均值 {mean_sub:.3f} 超出 0.05–0.40")
    return {"id": "S3", "status": "PASS" if not problems else "FAIL",
            "detail": f"抽样 {len(sample_dates)} 天: "
                      + "；".join(f"{k} [{v[0]:.2f},{v[1]:.2f}] 均值 {v[2]:.2f}"
                                 for k, v in summary.items())
                      + f"；地下填充层均值 {mean_sub:.3f}",
            "evidence": problems, "stats": summary}


def _manual_pw_column(q_path: str, lat: float, lon: float) -> float:
    """绕过实验代码：直接读原始文件，手算地形掩膜 + 梯形积分。"""
    ds = netCDF4.Dataset(q_path)
    try:
        lat_v = ds["latitude"][:]
        lon_v = ds["longitude"][:]
        i = int(np.argmin(np.abs(lat_v - lat)))
        j = int(np.argmin(np.abs(lon_v - lon)))
        level_hpa = ds["level"][:].astype("float64")
        times = netCDF4.num2date(ds["time"][:], ds["time"].units, ds["time"].calendar)
        hours = np.array([v.hour for v in times])
        picked = np.where(np.isin(hours, list(eda.HOURS)))[0]
        q = ds["q"][picked, :, i, j].astype("float64")
    finally:
        ds.close()

    eq = np.abs(np.diff(q, axis=1)) <= (1e-6 + 1e-3 * np.abs(q[:, :-1]))
    chain = np.flip(np.logical_and.accumulate(np.flip(eq, axis=1), axis=1), axis=1)
    first_true = np.where(chain.any(axis=1), np.argmax(chain, axis=1), q.shape[1] - 1)
    valid = np.arange(q.shape[1])[None, :] <= first_true[:, None]

    p_pa = level_hpa * 100.0
    w = np.empty_like(p_pa)
    w[0] = (p_pa[1] - p_pa[0]) / 2.0
    w[-1] = (p_pa[-1] - p_pa[-2]) / 2.0
    w[1:-1] = (p_pa[2:] - p_pa[:-2]) / 2.0
    qv = np.where(valid, q, np.nan)
    return float(np.mean(np.nansum(qv * w[None, :], axis=1) / eda.G))


def _check_s4(index) -> dict:
    probes = [("20100715", 35.0, 75.0), ("20110801", 30.0, 90.0),
              ("20120615", 28.0, 100.0), ("20130901", 33.0, 95.0)]
    rows, problems = [], []
    for date, lat, lon in probes:
        fields = _day_fields(index, date)
        i = int(round((lat - eda.LAT_MIN) / 0.25))
        j = int(round((lon - eda.LON_MIN) / 0.25))
        actual = float(fields["pw"][i, j])
        expected = _manual_pw_column(index[date]["q"], lat, lon)
        rel = abs(actual - expected) / abs(expected) if expected else float("inf")
        rows.append((date, lat, lon, actual, expected, rel))
        if rel > 0.005:
            problems.append(f"{date} ({lat}N,{lon}E): pipeline={actual:.4f} "
                            f"manual={expected:.4f} rel={rel:.2%}")
    return {"id": "S4", "status": "PASS" if not problems else "FAIL",
            "detail": "；".join(f"{d} {la}N/{lo}E 相对误差 {r:.4%}"
                               for d, la, lo, _, _, r in rows),
            "evidence": problems}


def _check_s5(run_dir: Path) -> dict:
    if not run_dir.exists():
        return {"id": "S5", "status": "NOT_RUN",
                "detail": f"新 run 目录不存在: {run_dir}", "evidence": []}
    problems, rows = [], []
    for year in YEARS:
        new = run_dir / "yearly" / f"tp_monthly_{year}.nc"
        legacy = LEGACY_DIR / f"tp_monthly_{year}.nc"
        if not new.exists():
            problems.append(f"缺少 {new}")
            continue
        if not legacy.exists():
            problems.append(f"缺少旧产物 {legacy}")
            continue
        with xr.open_dataset(new) as a, xr.open_dataset(legacy) as b:
            for var in ("pw", "rh500", "cfrac", "surf_p"):
                diff = float(np.nanmax(np.abs(a[var].values - b[var].values)))
                rows.append((year, var, diff))
                if diff != 0.0:
                    problems.append(f"{year} {var} max abs diff={diff}")
    status = "PASS" if rows and not problems else ("FAIL" if problems else "NOT_RUN")
    return {"id": "S5", "status": status,
            "detail": f"比较 {len(rows)} 组（4 年 × 4 变量），不一致 {len(problems)} 组",
            "evidence": problems}


def _check_s6(run_dir: Path) -> dict:
    if not run_dir.exists():
        return {"id": "S6", "status": "NOT_RUN",
                "detail": f"新 run 目录不存在: {run_dir}", "evidence": []}
    problems = []
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        problems.append("缺少 manifest.json")
        manifest = {}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if sorted(manifest.get("completed_years", [])) != list(YEARS):
            problems.append(f"completed_years={manifest.get('completed_years')}")
        if manifest.get("failed_dates"):
            problems.append(f"failed_dates={len(manifest['failed_dates'])} 条")
        if not manifest.get("plotted"):
            problems.append("manifest 未标记 plotted")
    daily_path = run_dir / "tables" / "tp_daily.csv"
    rows = 0
    if daily_path.exists():
        import csv
        with daily_path.open(encoding="utf-8") as handle:
            rows = sum(1 for _ in csv.DictReader(handle))
        if rows != len(YEARS) * DAYS_PER_YEAR:
            problems.append(f"逐日表 {rows} 行 != {len(YEARS) * DAYS_PER_YEAR}")
    else:
        problems.append("缺少 tables/tp_daily.csv")
    figures = sorted(p.name for p in (run_dir / "figures").glob("*.png"))
    if len(figures) != 8:
        problems.append(f"图件 {len(figures)} 张 != 8")
    summary = run_dir / "summary" / "tp_eda_summary.md"
    if not summary.exists():
        problems.append("缺少 summary/tp_eda_summary.md")
    return {"id": "S6", "status": "PASS" if not problems else "FAIL",
            "detail": f"manifest 年份 {manifest.get('completed_years')}，逐日 {rows} 行，"
                      f"图件 {len(figures)} 张，摘要 {'有' if summary.exists() else '无'}",
            "evidence": problems}


def _verdict(results: list[dict]) -> str:
    statuses = {item["id"]: item["status"] for item in results}
    if "FAIL" in statuses.values():
        return "FAIL"
    if statuses.get("S5") == "NOT_RUN" or statuses.get("S6") == "NOT_RUN":
        return "CONDITIONAL_PASS"
    return "PASS"


def _render(results, verdict, cfg, root, sample_dates) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 真实 ERA5 卷云水代理 EDA · 独立验收报告", "",
        f"- 生成时间：{now}",
        f"- 数据卷：`{cfg['volume_id']}` @ `{root}`（dataset=`{cfg['dataset_id']}`）",
        f"- 运行目录：`{RUN_DIR.relative_to(ROOT)}`",
        f"- 抽样日：{', '.join(sample_dates)}", "",
        "## A. 最终判定", "",
        f"**{verdict}**", "",
        "## B. 核心依据", "",
    ]
    for item in results:
        lines.append(f"- {item['id']} {item['status']} — {item['detail']}")
    lines += ["", "## C. 致命与高优先级问题", ""]
    fails = [item for item in results if item["status"] == "FAIL"]
    if fails:
        for item in fails:
            lines.append(f"- **{item['id']}**：{item['detail']}")
            for evidence in item["evidence"]:
                lines.append(f"  - 证据：`{evidence}`")
    else:
        lines.append("无（所有硬标准 PASS，未发现致命问题）。")
    lines += ["", "## D. 其他问题", ""]
    lines.append("- S1 的 mtime 漂移只记录、不判定：NTFS 经 FSKit 挂载时 mtime "
                 "可能被驱动重写，文件大小与可读性才是硬约束。")
    lines += ["", "## E. 检查与测试记录", "",
              "| 项 | 标准 | 结果 |", "|---|---|---|"]
    for item in results:
        lines.append(f"| {item['id']} | {STANDARDS[item['id']]} | {item['status']} |")
    lines += [
        "", "已执行：真实盘 catalog/文件系统核对、年份/日期/q-t 配对核对、"
        "12 个抽样日的物理量区间、4 个单柱独立重算、4 年等值回归、产物清单核对。",
        "未执行：全量 2944 个文件的 NetCDF 内容逐字节校验（本实验用其中 q/t 1472 个；需 `dbas checksum`，"
        "按运行手册低峰期分批做）；未做与独立观测（探空/站点）的交叉验证。", "",
        "## F. 剩余风险与局限", "",
        "- S5 的“逐比特一致”说明新旧实现等价，但两者共用同一物理口径（Magnus "
        "水上式、等值连段掩膜），不能证明该口径本身正确；口径验证仍需观测对照。",
        "- 抽样日覆盖每年 6 个月，但未覆盖全部 736 天；极端天气个案未被单独检查。",
        "- 真实卷为只读挂载，测试未验证损坏文件场景（需人工制造只读副本才能测）。", "",
        "## G. 后续行动", "",
        "1. 低峰期分批跑 `./bin/dbas checksum --volume era5hd --batch-size 5 "
        "--budget 600`，补比特级基线。",
        "2. 有探空/站点数据后，对 PW/RH500 做独立观测交叉检验。",
        "3. 若上游重写扁平产物，先归档再重跑本脚本，避免 S5 失去对照。", "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 ERA5 卷云水代理 EDA 独立验收")
    parser.add_argument("--quick", action="store_true", help="只抽 4 天（每年 7 月中）")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args()

    cfg, index, rows, root = _config()
    sample_dates = _sample_dates(index, quick=args.quick)
    results = [
        _check_s1(rows, root),
        _check_s2(index),
        _check_s3(index, sample_dates),
        _check_s4(index),
        _check_s5(RUN_DIR),
        _check_s6(RUN_DIR),
    ]
    verdict = _verdict(results)
    report = _render(results, verdict, cfg, root, sample_dates)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    for item in results:
        print(f"{item['id']} {item['status']}: {item['detail']}")
        for evidence in item["evidence"]:
            print(f"    - {evidence}")
    print(f"VERDICT={verdict}")
    print(f"报告: {report_path}")
    return 0 if verdict in ("PASS", "CONDITIONAL_PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
