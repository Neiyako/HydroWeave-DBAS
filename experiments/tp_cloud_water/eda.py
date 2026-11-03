#!/usr/bin/env python3
"""青藏高原云水资源代理指标 EDA（配置驱动、按年份断点续跑）。

本实验只读原始 ERA5 卷，输出写入 ``data/analysis/.../runs/<run_id>``。
严格云水需要 clwc/ciwc/云量；当前 t/q 结果只能解释为 PW、RH500、CFRAC 代理。
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

import dask
import numpy as np
import pandas as pd
import xarray as xr

from dbas.repro.manifest import atomic_write_text, config_hash, update_manifest
from experiments.tp_cloud_water.common import (
    ROOT,
    catalog_index,
    finish_manifest,
    load_config,
    mark_year,
    path_from_config,
    prepare_manifest,
    record_artifact,
    run_dir,
)

LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 25.0, 40.0, 70.0, 105.0
HOURS = (0, 6, 12, 18)
G = 9.80665
BAND = (100.0, 500.0)
RH_CLOUD = 80.0
BATCH = 10
WORKERS = 2
KEYS = ("pw_mm", "pw_glc", "pw_non", "rh500_pct", "rh500_glc", "rh500_non",
        "cfrac_pct", "cfrac_glc", "cfrac_non", "subsurf_pct", "surf_p_hpa")


def configure(config: dict[str, Any]) -> None:
    """把实验配置投影到计算函数使用的常量。"""
    global LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, HOURS, BAND, RH_CLOUD, BATCH, WORKERS
    region = config.get("region", {})
    LAT_MIN, LAT_MAX = float(region.get("lat_min", LAT_MIN)), float(region.get("lat_max", LAT_MAX))
    LON_MIN, LON_MAX = float(region.get("lon_min", LON_MIN)), float(region.get("lon_max", LON_MAX))
    HOURS = tuple(int(x) for x in config.get("synoptic_hours", HOURS))
    BAND = tuple(float(x) for x in config.get("pressure_band_hpa", BAND))
    RH_CLOUD = float(config.get("rh_cloud_pct", RH_CLOUD))
    BATCH = int(config.get("batch_days", BATCH))
    WORKERS = int(config.get("workers", WORKERS))


def crop_indices(ds):
    """TP 窗口在原网格上的整数切片 + 是否需要翻转成升序。"""
    lat, lon = ds.latitude.values, ds.longitude.values
    i0 = int(np.argmin(np.abs(lat - LAT_MAX)))
    i1 = int(np.argmin(np.abs(lat - LAT_MIN)))
    j0 = int(np.argmin(np.abs(lon - LON_MIN)))
    j1 = int(np.argmin(np.abs(lon - LON_MAX)))
    return slice(min(i0, i1), max(i0, i1) + 1), slice(min(j0, j1), max(j0, j1) + 1), (i0 < i1)


def lazy_day(qpath, tpath, grid):
    """单日 q/t 的高原窗口，保持 dask 惰性。

    返回 ``(q, t, q_dataset, t_dataset)``：调用方必须在 ``dask.compute`` 之后
    关闭后两个 Dataset，否则逐日打开的文件句柄会一直累积（4 年 ≈ 1500 个）。
    """
    lat_s, lon_s, flip = grid
    nlat = lat_s.stop - lat_s.start
    q_ds = xr.open_dataset(qpath, chunks={"time": 1, "level": 37,
                                         "latitude": max(nlat // 2, 30), "longitude": 60})
    t_ds = xr.open_dataset(tpath, chunks={"time": 1, "level": 37,
                                         "latitude": max(nlat // 2, 30), "longitude": 60})
    q = q_ds["q"].astype("float32")
    t = t_ds["t"].astype("float32")
    hsel = np.isin(pd.DatetimeIndex(q.time.values).hour, list(HOURS))
    q = q.isel(time=hsel, latitude=lat_s, longitude=lon_s)
    t = t.isel(time=hsel, latitude=lat_s, longitude=lon_s)
    if flip:
        q, t = q.isel(latitude=slice(None, None, -1)), t.isel(latitude=slice(None, None, -1))
    return q, t, q_ds, t_ds


def sub_ground_mask(qv, rtol=1e-3, atol=1e-6):
    """找出从最底层开始的等值连段（地形以下填充层）。"""
    n = qv.shape[1]
    eq = np.abs(qv[:, 1:] - qv[:, :-1]) <= (atol + rtol * np.abs(qv[:, :-1]))
    chain = np.logical_and.accumulate(eq[:, ::-1], axis=1)[:, ::-1]
    any_chain = chain.any(axis=1)
    first = np.argmax(chain, axis=1)
    surf = np.where(any_chain, first, n - 1)
    idx = np.arange(n)[None, :, None, None]
    return idx <= surf[:, None, :, :], surf


def trapezoid_weights(p_hpa):
    p = np.asarray(p_hpa, dtype="float64") * 100.0
    w = np.empty_like(p)
    w[0] = (p[1] - p[0]) / 2.0
    w[-1] = (p[-1] - p[-2]) / 2.0
    w[1:-1] = (p[2:] - p[:-2]) / 2.0
    return w


def compute_day(q, t):
    lv = q["level"].values.astype("float64")
    ok, surf_idx = sub_ground_mask(q.data)
    surf_p = lv[surf_idx]
    qv = np.where(ok, q.data, np.nan).astype("float64")
    tv = np.where(ok, t.data, np.nan).astype("float64")
    p = lv[None, :, None, None] * 100.0
    wq = np.where(np.isfinite(qv), qv, 0.0)
    pw = (wq * trapezoid_weights(lv)[None, :, None, None]).sum(1) / G
    xq = np.maximum(wq, 1e-12)
    ee = p * xq / (0.622 + xq)
    tc = np.where(np.isfinite(tv), tv - 273.15, -200.0)
    es = 611.2 * np.exp(17.67 * tc / (tc + 243.5))
    rh = np.clip(100.0 * ee / es, 0.0, 300.0)
    rh = np.where(np.isfinite(tv), rh, np.nan)
    i500 = int(np.argmin(np.abs(lv - 500.0)))
    rh500 = rh[:, i500, :, :]
    band = (lv >= BAND[0]) & (lv <= BAND[1])
    valid = np.isfinite(rh) & band[None, :, None, None]
    cfrac = np.where(valid.any(1), ((rh >= RH_CLOUD) & valid).sum(1)
                     / np.maximum(valid.sum(1), 1), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        rh_zon = np.nanmean(rh, axis=(0, 3))
    return {"pw": pw.mean(0).astype("float32"), "rh500": rh500.mean(0).astype("float32"),
            "cfrac": cfrac.mean(0).astype("float32"), "rh_z": rh_zon.astype("float32"),
            "surf_p": surf_p.mean(0).astype("float32"), "subsurf": float(1.0 - ok.mean()),
            "sp_mean": float(surf_p.mean())}


def write_frame(path: Path, frame: pd.DataFrame) -> None:
    atomic_write_text(path, frame.to_csv(index=False))


def write_year_dataset(ds: xr.Dataset, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        ds.to_netcdf(temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        ds.close()


def process_year(year: int, index: dict[str, dict[str, str]], glacier: np.ndarray,
                 config: dict[str, Any], root: Path, manifest_path: Path,
                 manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict], list[dict], list[dict]]:
    dates = sorted(d for d in index if d.startswith(str(year)) and {"q", "t"} <= set(index[d]))
    if not dates:
        return manifest, [], [], [{"year": year, "error": "无 q/t 配对文件"}]
    grid = lv = None
    daily_year: list[dict[str, Any]] = []
    monthly_year: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    acc: dict[int, dict[str, Any]] = {}
    for b0 in range(0, len(dates), BATCH):
        batch = dates[b0:b0 + BATCH]
        graphs, graph_dates, opened = [], [], []
        if grid is None:
            with xr.open_dataset(index[dates[0]]["q"]) as probe:
                grid = crop_indices(probe)
                lv = probe.level.values.astype("float64")
        for date in batch:
            try:
                q, t, q_ds, t_ds = lazy_day(index[date]["q"], index[date]["t"], grid)
                graphs.append((q, t))
                graph_dates.append(date)
                opened.extend((q_ds, t_ds))
            except Exception as exc:  # noqa: BLE001
                problems.append({"date": date, "error": f"open {type(exc).__name__}: {exc}"})
        if not graphs:
            continue
        try:
            result = dask.compute(*[item for pair in graphs for item in pair],
                                  scheduler="threads", num_workers=WORKERS)
        except Exception as exc:  # noqa: BLE001
            problems.extend({"date": date, "error": f"read {type(exc).__name__}: {exc}"}
                            for date in graph_dates)
            continue
        finally:
            for dataset in opened:
                dataset.close()
        for k, date in enumerate(graph_dates):
            q, t = result[2 * k], result[2 * k + 1]
            try:
                fields = compute_day(q, t)
                if glacier.shape != fields["pw"].shape:
                    raise ValueError(f"glacier mask shape {glacier.shape} != field {fields['pw'].shape}")
                cosw = np.cos(np.deg2rad(q.latitude.values))
                weights = cosw[:, None] * np.ones(q.longitude.size)
                weights = weights / weights.sum()
                non_glacier = ~glacier

                def wm(array, mask=None):
                    valid = np.isfinite(array) if mask is None else mask & np.isfinite(array)
                    return float((array[valid] * weights[valid]).sum() / weights[valid].sum()) if valid.any() else float("nan")

                daily_year.append({
                    "year": year, "date": date, "month": int(date[4:6]),
                    "n_hours": int(q.time.size), "n_levels": int(lv.size),
                    "pw_mm": wm(fields["pw"]), "pw_glc": wm(fields["pw"], glacier),
                    "pw_non": wm(fields["pw"], non_glacier), "rh500_pct": wm(fields["rh500"]),
                    "rh500_glc": wm(fields["rh500"], glacier), "rh500_non": wm(fields["rh500"], non_glacier),
                    "cfrac_pct": 100 * wm(fields["cfrac"]), "cfrac_glc": 100 * wm(fields["cfrac"], glacier),
                    "cfrac_non": 100 * wm(fields["cfrac"], non_glacier), "subsurf_pct": 100 * fields["subsurf"],
                    "surf_p_hpa": fields["sp_mean"],
                })
                month = int(date[4:6])
                bucket = acc.setdefault(month, dict(
                    n=0, s={key: np.zeros(fields[key].shape, "float64") for key in ("pw", "rh500", "cfrac", "surf_p")},
                    c={key: np.zeros(fields[key].shape, "float64") for key in ("pw", "rh500", "cfrac", "surf_p")},
                    z=np.zeros((lv.size, fields["pw"].shape[0]), "float64"),
                    zc=np.zeros((lv.size, fields["pw"].shape[0]), "float64")))
                bucket["n"] += 1
                for key in bucket["s"]:
                    valid = np.isfinite(fields[key])
                    bucket["s"][key] += np.where(valid, fields[key], 0.0)
                    bucket["c"][key] += valid
                bucket["z"] += np.where(np.isfinite(fields["rh_z"]), fields["rh_z"], 0.0)
                bucket["zc"] += np.isfinite(fields["rh_z"])
            except Exception as exc:  # noqa: BLE001
                problems.append({"date": date, "error": f"calc {type(exc).__name__}: {exc}"})
        if daily_year:
            done = min(b0 + BATCH, len(dates))
            print(f"  {year} {done}/{len(dates)}", flush=True)

    if not acc:
        return manifest, daily_year, monthly_year, problems or [{"year": year, "error": "全部读取失败"}]
    months = sorted(acc)
    nlat, nlon = acc[months[0]]["s"]["pw"].shape
    level = lv.astype("float32")
    fields3 = {key: np.full((len(months), nlat, nlon), np.nan, "float32")
               for key in ("pw", "rh500", "cfrac", "surf_p")}
    profile = np.full((len(months), level.size, nlat), np.nan, "float32")
    for mi, month in enumerate(months):
        bucket = acc[month]
        for key in fields3:
            fields3[key][mi] = np.where(bucket["c"][key] > 0,
                                        bucket["s"][key] / np.maximum(bucket["c"][key], 1.0), np.nan)
        profile[mi] = np.where(bucket["zc"] > 0,
                               bucket["z"] / np.maximum(bucket["zc"], 1.0), np.nan)
        records = [item for item in daily_year if item["month"] == month]
        monthly_year.append({"year": year, "month": month, "n_days": len(records),
                             **{key: float(np.nanmean([item[key] for item in records])) for key in KEYS}})
    dataset = xr.Dataset(
        {key: (("month", "latitude", "longitude"), fields3[key]) for key in fields3} |
        {"rh_z": (("month", "level", "latitude"), profile)},
        coords={"month": months, "latitude": np.linspace(LAT_MIN, LAT_MAX, nlat),
                "longitude": np.linspace(LON_MIN, LON_MAX, nlon), "level": level})
    dataset.attrs.update({"title": "Tibetan Plateau cloud-water PROXY monthly means",
                          "bbox": f"{LAT_MIN}-{LAT_MAX}N {LON_MIN}-{LON_MAX}E",
                          "synoptic_hours": str(list(HOURS)),
                          "pw_definition": "PW = layer-trapezoid(q dp)/g [mm]; sub-ground fill removed",
                          "rh_definition": "RH from q,T via Magnus over water; no ice phase",
                          "cfrac_definition": f"fraction of {BAND[0]:.0f}-{BAND[1]:.0f} hPa levels with RH>={RH_CLOUD:.0f}%",
                          "glacier_mask": "RGI 6.0 regions 13/14/15 rasterised on the 0.25deg TP grid",
                          "caveat": "NOT true cloud water: source volume needs clwc/ciwc for strict GCWR"})
    dataset["pw"].attrs = {"units": "mm", "long_name": "precipitable water",
                           "comment": "vapour resource proxy, NOT cloud water"}
    dataset["rh500"].attrs = {"units": "%", "long_name": "RH at 500 hPa",
                              "comment": "cloud-forming potential proxy"}
    dataset["cfrac"].attrs = {"units": "1",
                              "long_name": f"fraction of {BAND[0]:.0f}-{BAND[1]:.0f} hPa levels RH>={RH_CLOUD:.0f}%",
                              "comment": "cloud occurrence proxy"}
    dataset["surf_p"].attrs = {"units": "hPa",
                               "long_name": "estimated surface pressure from sub-ground fill run"}
    dataset["rh_z"].attrs = {"units": "%",
                             "long_name": "longitude- and time-mean RH on levels x latitude"}
    for key in dataset.data_vars:
        dataset[key].encoding = {"zlib": True, "complevel": int(config.get("complevel", 4)), "dtype": "float32"}
    year_dir = root / "yearly"
    table_dir = root / "tables"
    destination = year_dir / f"tp_monthly_{year}.nc"
    write_year_dataset(dataset, destination)
    write_frame(table_dir / f"tp_daily_{year}.csv", pd.DataFrame(daily_year))
    write_frame(table_dir / f"tp_monthly_{year}.csv", pd.DataFrame(monthly_year))
    write_frame(table_dir / f"tp_read_problems_{year}.csv", pd.DataFrame(problems))
    manifest = mark_year(manifest_path, manifest, year)
    for artifact in (destination, table_dir / f"tp_daily_{year}.csv",
                     table_dir / f"tp_monthly_{year}.csv",
                     table_dir / f"tp_read_problems_{year}.csv"):
        manifest = record_artifact(manifest_path, manifest, artifact)
    return manifest, daily_year, monthly_year, problems


def read_table(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() and path.stat().st_size > 1 else pd.DataFrame()


def main() -> None:
    parser = argparse.ArgumentParser(description="配置驱动、可断点续跑的 TP 云水代理 EDA")
    parser.add_argument("--config", default="experiments/tp_cloud_water/config.yaml")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--years", type=int, nargs="*")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    configure(config)
    clean_config = {key: value for key, value in config.items() if not key.startswith("_")}
    run_id = args.run_id or f"run-{config_hash(clean_config)}"
    index, rows, _ = catalog_index(config)
    manifest_path, manifest = prepare_manifest(config, run_id, rows, force=args.force)
    root = run_dir(config, run_id)
    glacier_path = path_from_config(config["glacier_mask"])
    if not glacier_path.exists():
        raise SystemExit(f"缺少 glacier mask: {glacier_path}")
    glacier = np.load(glacier_path)["mask"]
    years = args.years or [int(item) for item in config.get("years", [])]
    for year in years:
        if year in manifest.get("completed_years", []) and not args.force:
            print(f"[skip] {year} 已在 manifest 完成", flush=True)
            continue
        try:
            manifest, _, _, problems = process_year(
                year, index, glacier, config, root, manifest_path, manifest)
            if problems:
                print(f"[{year}] 完成但有 {len(problems)} 个问题", flush=True)
        except Exception as exc:  # noqa: BLE001
            manifest = update_manifest(manifest_path, status="failed",
                                       failed_year=year,
                                       error=f"{type(exc).__name__}: {exc}")
            print(f"[{year}] FAIL {type(exc).__name__}: {exc}", flush=True)
    completed = sorted(set(int(item) for item in manifest.get("completed_years", [])))
    daily = pd.concat([read_table(root / "tables" / f"tp_daily_{year}.csv") for year in completed],
                      ignore_index=True) if completed else pd.DataFrame()
    monthly = pd.concat([read_table(root / "tables" / f"tp_monthly_{year}.csv") for year in completed],
                        ignore_index=True) if completed else pd.DataFrame()
    problems = pd.concat([read_table(root / "tables" / f"tp_read_problems_{year}.csv") for year in completed],
                         ignore_index=True) if completed else pd.DataFrame()
    write_frame(root / "tables" / "tp_daily.csv", daily)
    write_frame(root / "tables" / "tp_monthly.csv", monthly)
    write_frame(root / "tables" / "tp_read_problems.csv", problems)
    for artifact in (root / "tables" / "tp_daily.csv", root / "tables" / "tp_monthly.csv",
                     root / "tables" / "tp_read_problems.csv"):
        manifest = record_artifact(manifest_path, manifest, artifact)
    manifest = finish_manifest(manifest_path, manifest, completed,
                               problems.to_dict("records") if not problems.empty else [])
    print(f"DONE run={run_id} years={completed} days={len(daily)} months={len(monthly)} "
          f"failed={len(problems)} -> {root}", flush=True)


if __name__ == "__main__":
    main()
