#!/usr/bin/env python3
"""青藏高原「云水资源」代理指标抽检 + EDA（原始卷只读，产物写 data/analysis）。

口径说明（重要）
----------------
本卷 ERA5 气压层只含 t/q/u/v，**没有云水混合比(clwc/ciwc)与总云量**，严格意义的云水
含量画不出来。这里用三个只依赖 t/q、物理含义明确的代理量，并且都不当真云水解释：
  PW     = ∫ q d(-p) / g            可降水量，水汽资源总量（mm ≡ kg/m²）
  RH500  = RH @ 500 hPa             中层饱和度，成云潜势
  CFRAC  = 100–500 hPa 之间 RH≥80% 的有效层占比   云发生频率代理
RH 由 q、T 反算，es 用 Magnus/Tetens 水上式 (a=17.67, b=243.5)，不区分冰面，
所以高冷上层的 RH 系统性偏保守（实际冰面饱和更低）。

地形以下怎么处理（关键点）
--------------------------
grib_to_netcdf 导出的 ERA5 气压层把地形以下的层用"地面值"填充（q 恒定、t 按固定直减率
外推），没有任何缺测标记。若直接全层积分，PW 会把伪造的地下层水汽算进去，高原上可低估
上百 mm。本脚本按"从底部开始的等值连段"反推地面气压层，连段顶（含）以上视为真实可解析
层，以下置 NaN；连段顶的气压即地面气压估计 surf_p，用 barometric 公式折算成海拔近似，
用于按高程分层统计。这是近似，精度约一层（25–50 hPa）。

分工：dask 负责分块惰性读取与并发 I/O（xarray chunks），numpy 负责逐柱地形掩膜与物理量。

抽样：每个日文件 24 个时次取 00/06/12/18 UTC 等权（近似日均）；默认 2010–2013 每年 5–10
月全部日期，逐文件隔离失败。

用法：PYTHONPATH=src .venv/bin/python scripts/tp_cloud_water_eda.py [--years ...] [--force]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from pathlib import Path

import dask
import numpy as np
import pandas as pd
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "analysis" / "tp_cloud_water"
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 25.0, 40.0, 70.0, 105.0   # 高原主体
HOURS = (0, 6, 12, 18)
G = 9.80665
BAND = (100.0, 500.0)      # 云发生频率代理使用的中上层段
RH_CLOUD = 80.0
BATCH = 10                 # 每次交给 dask 的天数
WORKERS = 2
FILEVAR = {"specific_humidity": "q", "temperature": "t"}
KEYS = ("pw_mm", "pw_glc", "pw_non", "rh500_pct", "rh500_glc", "rh500_non",
        "cfrac_pct", "cfrac_glc", "cfrac_non", "subsurf_pct", "surf_p_hpa")


def build_index(volume_root: Path) -> dict[str, dict[str, str]]:
    """从 DBAS 目录库取 era5_pressure 活跃文件 → {date: {'q':路径,'t':路径}}。"""
    db = sqlite3.connect(ROOT / "data" / "catalog.db")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT rel_path FROM files WHERE dataset_id='era5_pressure' "
        "AND volume_id='era5hd' AND status='active'").fetchall()
    db.close()
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        m = re.match(r"^ERA5_025dg_(\d{8})_(\w+)\.nc$", Path(r["rel_path"]).name)
        if m and FILEVAR.get(m.group(2)):
            out.setdefault(m.group(1), {})[FILEVAR[m.group(2)]] = \
                str(volume_root / r["rel_path"])
    return out


def crop_indices(ds):
    """TP 窗口在原（纬度降序）网格上的整数切片 + 是否需要翻转成升序。"""
    lat, lon = ds.latitude.values, ds.longitude.values
    i0 = int(np.where(lat == LAT_MAX)[0][0])
    i1 = int(np.where(lat == LAT_MIN)[0][0])
    j0 = int(np.where(lon == LON_MIN)[0][0])
    j1 = int(np.where(lon == LON_MAX)[0][0])
    return slice(min(i0, i1), max(i0, i1) + 1), slice(j0, j1 + 1), (i0 < i1)


def lazy_day(qpath, tpath, grid):
    """单日 q/t 的高原窗口，保持 dask 惰性（尚未触发 I/O）。

    chunk 尺寸要贴合作图窗口：NC3 里 time 是最外维，若 chunks 只按 time 切，
    每个 chunk 就是整幅 (37,321,441) 面元（10.5MB），裁切发生在读取之后。
    把 latitude/longitude 也切成小块，dask 才会只取窗口涉及的块（约 4MB/天）。
    """
    lat_s, lon_s, flip = grid
    nlat = (lat_s.stop - lat_s.start)
    q = xr.open_dataset(qpath, chunks={"time": 1, "level": 37,
                                       "latitude": max(nlat // 2, 30),
                                       "longitude": 60})["q"].astype("float32")
    t = xr.open_dataset(tpath, chunks={"time": 1, "level": 37,
                                       "latitude": max(nlat // 2, 30),
                                       "longitude": 60})["t"].astype("float32")
    hsel = np.isin(pd.DatetimeIndex(q.time.values).hour, list(HOURS))
    q = q.isel(time=hsel, latitude=lat_s, longitude=lon_s)
    t = t.isel(time=hsel, latitude=lat_s, longitude=lon_s)
    if flip:
        q, t = q.isel(latitude=slice(None, None, -1)), t.isel(latitude=slice(None, None, -1))
    return q, t


def sub_ground_mask(qv, rtol=1e-3, atol=1e-6):
    """找出"从最底层开始的等值连段"= 地形以下填充层，返回 True=可解析层。

    连段顶（含）保留：它是地面层的插值，物理上代表地表；其下所有层置为不可用。
    必须用容差比较：原始 int16 打包把地下常数层量化成相邻两个码（32°N/90°E 处
    -22108/-22107），严格 == 会漏判、导致 PW 把地下伪造层算进去。
    """
    n = qv.shape[1]
    eq = np.abs(qv[:, 1:] - qv[:, :-1]) <= (atol + rtol * np.abs(qv[:, :-1]))
    chain = np.logical_and.accumulate(eq[:, ::-1], axis=1)[:, ::-1]
    any_chain = chain.any(axis=1)
    first = np.argmax(chain, axis=1)                            # 连段顶的 level 下标
    surf = np.where(any_chain, first, n - 1)                    # 无连段→全层可信
    idx = np.arange(n)[None, :, None, None]
    return idx <= surf[:, None, :, :], surf


def trapezoid_weights(p_hpa):
    p = np.asarray(p_hpa, dtype="float64") * 100.0              # Pa，升序
    w = np.empty_like(p)
    w[0] = (p[1] - p[0]) / 2.0
    w[-1] = (p[-1] - p[-2]) / 2.0
    w[1:-1] = (p[2:] - p[:-2]) / 2.0
    return w


def compute_day(q, t):
    """单日 numpy 数组 → pw / rh500 / cfrac / 剖面 / 地下层占比 / 地面气压估计。"""
    lv = q["level"].values.astype("float64")
    ok, surf_idx = sub_ground_mask(q.data)
    surf_p = lv[surf_idx]
    qv = np.where(ok, q.data, np.nan).astype("float64")
    tv = np.where(ok, t.data, np.nan).astype("float64")
    p = lv[None, :, None, None] * 100.0                          # Pa
    wq = np.where(np.isfinite(qv), qv, 0.0)
    pw = (wq * trapezoid_weights(lv)[None, :, None, None]).sum(1) / G
    xq = np.maximum(wq, 1e-12)
    ee = p * xq / (0.622 + xq)                                   # 水汽压 Pa
    tc = np.where(np.isfinite(tv), tv - 273.15, -200.0)
    es = 611.2 * np.exp(17.67 * tc / (tc + 243.5))               # Magnus 水上
    rh = np.clip(100.0 * ee / es, 0.0, 300.0)
    rh = np.where(np.isfinite(tv), rh, np.nan)
    i500 = int(np.where(lv == 500.0)[0][0])
    rh500 = rh[:, i500, :, :]
    band = (lv >= BAND[0]) & (lv <= BAND[1])
    valid = np.isfinite(rh) & band[None, :, None, None]
    cfrac = np.where(valid.any(1), ((rh >= RH_CLOUD) & valid).sum(1)
                     / np.maximum(valid.sum(1), 1), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        rh_zon = np.nanmean(rh, axis=(0, 3))                    # (level, lat) 时次+经度平均
    subsurf = float(1.0 - ok.mean())
    return {"pw": pw.mean(0).astype("float32"), "rh500": rh500.mean(0).astype("float32"),
            "cfrac": cfrac.mean(0).astype("float32"), "rh_z": rh_zon.astype("float32"),
            "surf_p": surf_p.mean(0).astype("float32"), "subsurf": subsurf,
            "sp_mean": float(surf_p.mean())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="*", default=[2010, 2011, 2012, 2013])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ROOT / "data" / "catalog.db")
    volume_root = Path(db.execute(
        "SELECT last_mount_path FROM volumes WHERE volume_id='era5hd'").fetchone()[0])
    db.close()
    idx = build_index(volume_root)
    glacier = np.load(OUT / "glacier_mask_025.npz")["mask"]

    daily, monthly, problems = [], [], []
    grid = lv = None
    for year in args.years:
        dest = OUT / f"tp_monthly_{year}.nc"
        if dest.exists() and not args.force:
            print(f"[skip] {year} 已有 {dest.name}", flush=True)
            continue
        dates = sorted(d for d in idx if d.startswith(str(year))
                       and {"q", "t"} <= set(idx[d]))
        if not dates:
            print(f"[{year}] 无 q/t 配对文件", flush=True)
            continue
        if grid is None:
            probe = xr.open_dataset(idx[dates[0]]["q"])
            grid = crop_indices(probe)
            lv = probe.level.values.astype("float64")
            probe.close()
        t_year = time.time()
        acc: dict[int, dict] = {}
        for b0 in range(0, len(dates), BATCH):
            batch = dates[b0:b0 + BATCH]
            graphs = []
            for d in batch:
                try:
                    graphs.append(lazy_day(idx[d]["q"], idx[d]["t"], grid))
                except Exception as e:
                    problems.append({"date": d, "error": f"open {type(e).__name__}: {e}"})
            if not graphs:
                continue
            flat = [a for pair in graphs for a in pair]
            try:
                res = dask.compute(*flat, scheduler="threads", num_workers=WORKERS)
            except Exception as e:
                for pair, d in zip(graphs, batch):
                    problems.append({"date": d,
                                     "error": f"read {type(e).__name__}: {e}"})
                continue
            for k, d in enumerate(batch[:len(graphs)]):
                q, t = res[2 * k], res[2 * k + 1]
                try:
                    f = compute_day(q, t)
                except Exception as e:
                    problems.append({"date": d, "error": f"calc {type(e).__name__}: {e}"})
                    continue
                cosw = np.cos(np.deg2rad(q.latitude.values))
                W = (cosw[:, None] * np.ones(q.longitude.size))
                W = W / W.sum()
                ng = ~glacier

                def wm(a, m=None):
                    ok2 = np.isfinite(a) if m is None else (m & np.isfinite(a))
                    return float((a[ok2] * W[ok2]).sum() / W[ok2].sum()) if ok2.any() \
                        else float("nan")

                rec = {"year": year, "date": d, "month": int(d[4:6]),
                       "n_hours": int(q.time.size), "n_levels": int(lv.size),
                       "pw_mm": wm(f["pw"]), "pw_glc": wm(f["pw"], glacier),
                       "pw_non": wm(f["pw"], ng),
                       "rh500_pct": wm(f["rh500"]), "rh500_glc": wm(f["rh500"], glacier),
                       "rh500_non": wm(f["rh500"], ng),
                       "cfrac_pct": 100 * wm(f["cfrac"]),
                       "cfrac_glc": 100 * wm(f["cfrac"], glacier),
                       "cfrac_non": 100 * wm(f["cfrac"], ng),
                       "subsurf_pct": 100 * f["subsurf"],
                       "surf_p_hpa": f["sp_mean"]}
                daily.append(rec)
                m = int(d[4:6])
                a = acc.setdefault(m, dict(
                    n=0, s={k: np.zeros(f[k].shape, "float64")
                            for k in ("pw", "rh500", "cfrac", "surf_p")},
                    c={k: np.zeros(f[k].shape, "float64")
                       for k in ("pw", "rh500", "cfrac", "surf_p")},
                    z=np.zeros((lv.size, f["pw"].shape[0]), "float64"),
                    zc=np.zeros((lv.size, f["pw"].shape[0]), "float64")))
                a["n"] += 1
                for k in a["s"]:
                    ok2 = np.isfinite(f[k])
                    a["s"][k] += np.where(ok2, f[k], 0.0)
                    a["c"][k] += ok2
                a["z"] += np.where(np.isfinite(f["rh_z"]), f["rh_z"], 0.0)
                a["zc"] += np.isfinite(f["rh_z"])
            if (b0 // BATCH) % 3 == 0 or b0 + BATCH >= len(dates):
                el = time.time() - t_year
                done = min(b0 + BATCH, len(dates))
                print(f"  {year} {done}/{len(dates)} pw={daily[-1]['pw_mm']:.1f}mm "
                      f"rh500={daily[-1]['rh500_pct']:.0f}% cf="
                      f"{daily[-1]['cfrac_pct']:.0f}% subsurf={daily[-1]['subsurf_pct']:.0f}% "
                      f"[{el:.0f}s {el/done:.2f}s/day]", flush=True)

        if not acc:
            print(f"[{year}] 全部读取失败", flush=True)
            continue
        months = sorted(acc)
        nlat, nlon = acc[months[0]]["s"]["pw"].shape
        lvl = lv.astype("float32")
        g3 = {k: np.full((len(months), nlat, nlon), np.nan, "float32")
              for k in ("pw", "rh500", "cfrac", "surf_p")}
        zsec = np.full((len(months), lvl.size, nlat), np.nan, "float32")
        for mi, m in enumerate(months):
            a = acc[m]
            for k in g3:
                g3[k][mi] = np.where(a["c"][k] > 0,
                                     a["s"][k] / np.maximum(a["c"][k], 1.0), np.nan)
            zsec[mi] = np.where(a["zc"] > 0, a["z"] / np.maximum(a["zc"], 1.0), np.nan)
            dm = [x for x in daily if x["year"] == year and x["month"] == m]
            monthly.append({"year": year, "month": m, "n_days": len(dm),
                            **{k: float(np.nanmean([x[k] for x in dm])) for k in KEYS}})
        ds = xr.Dataset(
            {k: (("month", "latitude", "longitude"), g3[k])
             for k in ("pw", "rh500", "cfrac", "surf_p")} |
            {"rh_z": (("month", "level", "latitude"), zsec)},
            coords={"month": months,
                    "latitude": np.linspace(LAT_MIN, LAT_MAX, nlat),
                    "longitude": np.linspace(LON_MIN, LON_MAX, nlon), "level": lvl})
        ds["pw"].attrs = {"units": "mm", "long_name": "precipitable water",
                          "comment": "vapour resource proxy, NOT cloud water"}
        ds["rh500"].attrs = {"units": "%", "long_name": "RH at 500 hPa",
                             "comment": "cloud-forming potential proxy"}
        ds["cfrac"].attrs = {"units": "1",
                             "long_name": f"fraction of {BAND[0]:.0f}-{BAND[1]:.0f} hPa levels RH>={RH_CLOUD:.0f}%",
                             "comment": "cloud occurrence proxy"}
        ds["surf_p"].attrs = {"units": "hPa",
                              "long_name": "estimated surface pressure from sub-ground fill run"}
        ds["rh_z"].attrs = {"units": "%",
                            "long_name": "longitude- and time-mean RH on levels x latitude"}
        ds.attrs.update(
            title="Tibetan Plateau cloud-water PROXY monthly means (ERA5 pressure levels)",
            bbox=f"{LAT_MIN}-{LAT_MAX}N {LON_MIN}-{LON_MAX}E",
            synoptic_hours=str(list(HOURS)),
            pw_definition="PW = layer-trapezoid(q dp)/g [mm]; sub-ground fill removed",
            rh_definition="RH from q,T via Magnus over water (17.67/243.5), no ice phase",
            cfrac_definition=f"fraction of {BAND[0]:.0f}-{BAND[1]:.0f} hPa levels with RH>={RH_CLOUD:.0f}%",
            glacier_mask="RGI 6.0 regions 13/14/15 rasterised on the 0.25deg TP grid",
            caveat="NOT true cloud water: this volume has only t/q/u/v, no clwc/ciwc/cloud area",
            source="read-only external volume, listed by DBAS catalog (dataset=era5_pressure)")
        for k in ds.data_vars:
            ds[k].encoding = dict(zlib=True, complevel=4, dtype="float32")
        ds.to_netcdf(dest)
        print(f"[{year}] 写出 {dest.name} ({dest.stat().st_size/1e6:.1f} MB)", flush=True)

    pd.DataFrame(daily).to_csv(OUT / "tp_daily.csv", index=False)
    pd.DataFrame(monthly).to_csv(OUT / "tp_monthly.csv", index=False)
    pd.DataFrame(problems).to_csv(OUT / "tp_read_problems.csv", index=False)
    print(f"DONE days={len(daily)} months={len(monthly)} failed={len(problems)} -> {OUT}",
          flush=True)
    for p in problems[:10]:
        print("  FAIL", p, flush=True)


if __name__ == "__main__":
    main()
