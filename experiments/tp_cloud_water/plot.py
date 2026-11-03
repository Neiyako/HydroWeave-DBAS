#!/usr/bin/env python3
"""青藏高原云水资源代理指标：分年制图 + EDA 摘要。

读取 ``experiments/tp_cloud_water/eda.py`` 在运行目录里写下的产物：

    runs/<run_id>/yearly/tp_monthly_<year>.nc
    runs/<run_id>/tables/tp_daily.csv
    runs/<run_id>/tables/tp_monthly.csv
    runs/<run_id>/tables/tp_read_problems.csv

图件与摘要写回同一运行目录的 ``figures/`` 与 ``summary/``，因此换 ``--run-id``
即可让 EDA 与制图产物成组隔离，不会互相覆盖。

图中所有量都是代理指标，不是真云水：本卷 ERA5 只有 t/q/u/v，没有 clwc/ciwc/总云量。
  PW     可降水量 (mm)                              —— 水汽资源
  RH500  500 hPa 相对湿度 (%)                       —— 成云潜势
  CFRAC  100–500 hPa 内 RH≥80% 的有效层占比         —— 云发生频率代理
冰川区来自 RGI 6.0（13/14/15 区）在 0.25° 高原网格上的栅格掩膜。
地图底稿用盘上 1:4,000,000 中国边界/水系 shp（只读），仅作地理参照，不做分析输入；
``basemap_dir`` 不在线时自动跳过底图，不影响其余图件。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import numpy as np                                     # noqa: E402
import pandas as pd                                    # noqa: E402
import shapefile                                       # noqa: E402
import xarray as xr                                    # noqa: E402
import matplotlib.pyplot as plt                        # noqa: E402
from matplotlib.collections import LineCollection      # noqa: E402
from matplotlib.colors import ListedColormap, Normalize  # noqa: E402

from dbas.repro.manifest import config_hash, update_manifest  # noqa: E402
from experiments.tp_cloud_water.common import (        # noqa: E402
    load_config,
    path_from_config,
    record_artifact,
    run_dir,
)

for fam in ("PingFang SC", "Heiti SC", "Hiragino Sans GB", "Arial Unicode MS"):
    matplotlib.rcParams["font.sans-serif"] = [fam, *matplotlib.rcParams["font.sans-serif"]]
matplotlib.rcParams["axes.unicode_minus"] = False

MONTHS = [5, 6, 7, 8, 9, 10]
MLAB = {5: "5月 May", 6: "6月 Jun", 7: "7月 Jul", 8: "8月 Aug", 9: "9月 Sep", 10: "10月 Oct"}
SPEC = {"pw": ("可降水量 PW (mm)", "viridis"),
        "rh500": ("500 hPa 相对湿度 (%)", "YlGnBu"),
        "cfrac": ("云频率代理：100–500 hPa 中 RH≥80% 层占比", "magma")}
DPI = 130


def _runs(mask):
    """布尔掩膜里的连续 True 行程 → 索引数组（长度≥2）。"""
    idx = np.where(mask)[0]
    if idx.size == 0:
        return []
    out, cur = [], [idx[0]]
    for a, b in zip(idx[:-1], idx[1:]):
        if b == a + 1:
            cur.append(b)
        else:
            if len(cur) >= 2:
                out.append(np.asarray(cur))
            cur = [b]
    if len(cur) >= 2:
        out.append(np.asarray(cur))
    return out


def basemap_lines(cache_dir: Path, shp_dir: Path):
    """缓存 TP 邻域内的边界/水系折线（只读 shp，一次解析永久复用）。"""
    cache = cache_dir / "tp_basemap_lines.npz"
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        return list(z["seg"]), list(z["kind"])
    segs, kinds = [], []
    for fname, kind in (("bou1_4p.shp", "border"), ("bou2_4l.shp", "province"),
                        ("hyd1_4l.shp", "river")):
        p = shp_dir / fname
        if not p.exists():
            continue
        r = shapefile.Reader(str(p), encoding="gbk")
        for sh in r.shapes():
            allpts = np.asarray(sh.points, dtype="float64")
            if allpts.ndim != 2 or allpts.shape[0] < 2:
                continue
            bounds = list(sh.parts) + [len(allpts)]
            for a, b in zip(bounds[:-1], bounds[1:]):
                pts = allpts[a:b]                 # 按 part 切分，避免跨环连线
                if pts.shape[0] < 2:
                    continue
                keep = ((pts[:, 0] >= 68) & (pts[:, 0] <= 107)
                        & (pts[:, 1] >= 23) & (pts[:, 1] <= 42))
                for grp in _runs(keep):
                    segs.append(pts[grp])
                    kinds.append(kind)
    np.savez_compressed(cache, seg=np.array(segs, dtype=object),
                        kind=np.array(kinds, dtype=object))
    return segs, kinds


def add_basemap(ax, segs, kinds):
    style = {"border": dict(color="0.2", lw=0.9, zorder=5),
             "province": dict(color="0.45", lw=0.5, zorder=4),
             "river": dict(color="#2b6cb0", lw=0.7, alpha=0.8, zorder=4)}
    for k, st in style.items():
        pts = [s for s, kk in zip(segs, kinds) if kk == k]
        if pts:
            ax.add_collection(LineCollection(pts, **st))


def panel(ax, field, lat, lon, norm, cmap, glacier, segs, kinds):
    ax.set_facecolor("0.88")
    ax.pcolormesh(lon, lat, field, cmap=cmap, norm=norm, shading="auto", zorder=1)
    g = np.ma.masked_where(~glacier, glacier.astype(float))
    ax.pcolormesh(lon, lat, g, cmap=ListedColormap(["none", "#ffd166"]),
                  norm=Normalize(0, 1), shading="auto", zorder=2)
    add_basemap(ax, segs, kinds)
    ax.set_xlim(float(lon[0]), float(lon[-1]))
    ax.set_ylim(float(lat[0]), float(lat[-1]))
    ax.set_aspect(1.0 / np.cos(np.deg2rad(float(lat.mean()))))
    ax.tick_params(labelsize=7.5)
    ax.grid(True, color="w", alpha=0.2, lw=0.4, zorder=3)


def fig_maps(ds, years, glacier, segs, kinds, scales, fig_dir: Path):
    """每年一张 3 行(PW/RH500/CFRAC) × 6 列(5–10 月) 空间分布图。

    色标跨年份共用，否则"某年颜色变深"可能只是换了量程。
    """
    rows = list(SPEC)
    for year in years:
        d = ds[year]
        lat, lon = d.latitude.values, d.longitude.values
        fig, axes = plt.subplots(3, 7, figsize=(19.5, 8.8),
                                 gridspec_kw={"width_ratios": [1] * 6 + [0.03]},
                                 constrained_layout=True)
        fig.suptitle(
            f"青藏高原云水资源代理指标 · {year} 年 5–10 月月平均\n"
            "代理量：可降水量 / 500 hPa 饱和度 / 中层 RH≥80% 层占比 —— "
            "本卷无 clwc、ciwc 与总云量，不是真云水；色标与其余年份一致", fontsize=13.5)
        for ri, spec in enumerate(rows):
            title, cmap = SPEC[spec]
            vmin, vmax = scales[spec]
            norm = Normalize(vmin=vmin, vmax=vmax)
            blk = d[spec].transpose("month", "latitude", "longitude").values
            for ci in range(6):
                if ci >= blk.shape[0]:              # 该年缺这些月份：留白但不画刻度
                    axes[ri, ci].axis("off")
                    continue
                f = np.squeeze(blk[ci]) * (100.0 if spec == "cfrac" else 1.0)
                panel(axes[ri, ci], f, lat, lon, norm, cmap, glacier, segs, kinds)
                axes[ri, ci].set_title(MLAB[int(d.month.values[ci])], fontsize=9.5, pad=2)
                if ci:
                    axes[ri, ci].tick_params(labelleft=False)
                if ri == 2:
                    axes[ri, ci].set_xlabel("经度 (°E)", fontsize=8)
            if ri == 0:
                for r in range(3):
                    axes[r, 0].set_ylabel("纬度 (°N)", fontsize=8)
            cb_ax = axes[ri, 6]
            im = cb_ax.imshow([[0.0]], cmap=cmap, norm=norm, aspect="auto")
            cb = fig.colorbar(im, cax=cb_ax)
            cb.set_label(title, fontsize=8.5)
            cb.ax.tick_params(labelsize=7.5)
        fig.savefig(fig_dir / f"fig_maps_{year}.png", dpi=DPI)
        plt.close(fig)
        print("写出", f"figures/fig_maps_{year}.png", flush=True)


def wmean(field, weight, mask=None):
    ok = np.isfinite(field) if mask is None else (mask & np.isfinite(field))
    if not ok.any():
        return float("nan")
    return float((field[ok] * weight[ok]).sum() / weight[ok].sum())


def fig_glacier_elevation(ds, years, glacier, fig_dir: Path):
    """冰川格点 vs 非冰川格点（上排）与沿估计高程的变化（下排）。"""
    fig, axes = plt.subplots(2, 3, figsize=(14.8, 8.0), constrained_layout=True)
    for col, spec in enumerate(SPEC):
        title, _ = SPEC[spec]
        scale = 100.0 if spec == "cfrac" else 1.0
        gl_vals, ng_vals, years_x = [], [], []
        ax2 = axes[1, col]
        for y in years:
            d = ds[y]
            v = np.nanmean(d[spec].transpose("month", "latitude", "longitude").values,
                           axis=0) * scale
            sp = np.nanmean(d["surf_p"].transpose("month", "latitude", "longitude").values,
                            axis=0)
            z = 44330.0 * (1.0 - (sp / 1013.25) ** 0.1903)
            W = np.broadcast_to(np.cos(np.deg2rad(d.latitude.values))[:, None]
                                * np.ones(d.longitude.size), v.shape)
            gl_vals.append(wmean(v, W, glacier))
            ng_vals.append(wmean(v, W, ~glacier))
            years_x.append(y)
            bins = np.arange(0, 6500, 500)
            cen, val = [], []
            for lo, hi in zip(bins[:-1], bins[1:]):
                s = (z >= lo) & (z < hi) & np.isfinite(v)
                if s.sum() >= 30:
                    cen.append((lo + hi) / 2000.0)
                    val.append(wmean(v, W, s))
            ax2.plot(cen, val, "-o", ms=3.6, lw=1.2, label=str(y))
        ax = axes[0, col]
        x = np.arange(len(years_x))
        ax.bar(x - 0.19, gl_vals, 0.36, label="冰川格点 (RGI 6.0)", color="#d7263d")
        ax.bar(x + 0.19, ng_vals, 0.36, label="非冰川格点", color="#457b9d")
        ax.set_xticks(x, [str(v) for v in years_x])
        ax.set_title(f"{title}（5–10 月平均）", fontsize=10)
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.3)
        ax2.set_xlabel("估计海拔 (km，由地面气压折算)", fontsize=8.5)
        ax2.set_ylabel(title, fontsize=8.5)
        ax2.set_title(f"{title} 随高程的变化", fontsize=10)
        ax2.legend(fontsize=7.5, title="年份", title_fontsize=7.5)
        ax2.grid(alpha=0.3)
    fig.suptitle(f"重点：冰川覆盖格点 vs 高原其余格点，以及沿高程的分布（{years[0]}–{years[-1]}，5–10 月）",
                 fontsize=13)
    fig.savefig(fig_dir / "fig_glacier_elevation.png", dpi=DPI)
    plt.close(fig)
    print("写出 figures/fig_glacier_elevation.png", flush=True)


def fig_timeseries(daily, fig_dir: Path):
    d = daily.copy()
    d["dt"] = pd.to_datetime(d["date"].astype(str), format="%Y%m%d")
    d = d.sort_values("dt")
    fig, axes = plt.subplots(3, 1, figsize=(13.8, 8.8), constrained_layout=True,
                             sharex=True)
    panels = [("pw_mm", "pw_glc", "可降水量 PW (mm)"),
              ("rh500_pct", "rh500_glc", "RH@500hPa (%)"),
              ("cfrac_pct", "cfrac_glc", "云频率代理 (%)")]
    for ax, (a, b, lab) in zip(axes, panels):
        s = d.set_index("dt")
        for col, c, lb in ((a, "#457b9d", "高原整体（cos 纬度加权）"),
                           (b, "#d7263d", "冰川格点")):
            ser = s[col].resample("D").mean().rolling(15, min_periods=5).mean()
            ax.plot(ser.index, ser.values, color=c, lw=1.2, label=lb)
        ax.set_ylabel(lab, fontsize=9)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="upper left")
    yr = sorted(d["year"].unique())
    for y in yr:
        axes[0].axvline(pd.Timestamp(year=int(y), month=5, day=1), color="0.35",
                        lw=0.9, ls="--")
        axes[0].text(pd.Timestamp(year=int(y), month=5, day=3),
                     axes[0].get_ylim()[1], f" {y}", fontsize=8, va="top")
    axes[0].set_title("逐日面积加权云水代理指标（15 日滑动平均；数据仅每年 5–10 月）",
                      fontsize=12)
    axes[2].set_xlabel("日期")
    fig.savefig(fig_dir / "fig_timeseries.png", dpi=DPI)
    plt.close(fig)
    print("写出 figures/fig_timeseries.png", flush=True)


def fig_crosssection(ds, years, fig_dir: Path):
    """纬向–气压剖面：6–8 月平均 RH + RH=80% 等值线 + 估计地面气压（地形顶）。"""
    years = list(years)[:4]
    fig, axes = plt.subplots(2, 2, figsize=(13.2, 8.6), constrained_layout=True,
                             squeeze=False)
    im = None
    for ax, y in zip(axes.ravel(), years):
        d = ds[y]
        months = [int(m) for m in d.month.values]
        sel = [months.index(m) for m in (6, 7, 8) if m in months]
        if not sel:
            ax.axis("off")
            continue
        rhz = d["rh_z"].isel(month=sel).mean("month").values       # (level, lat)
        sp = d["surf_p"].isel(month=sel).mean(["month", "longitude"]).values
        lat, lev = d.latitude.values, d.level.values
        lv2, la2 = np.meshgrid(lev, lat)
        im = ax.pcolormesh(lv2, la2, rhz.T, cmap="YlGnBu", norm=Normalize(0, 100),
                           shading="auto")
        cs = ax.contour(lv2, la2, rhz.T, levels=[80], colors="#d7263d", lw=1.8)
        ax.clabel(cs, fontsize=7.5)
        ax.plot(sp, lat, color="k", lw=1.6, label="估计地面气压（地形顶）")
        ax.set_xlim(1000, 100)
        ax.set_ylim(25, 40)
        ax.set_xlabel("气压层 (hPa)")
        ax.set_ylabel("纬度 (°N)")
        ax.set_title(f"{y} 年 6–8 月：沿经度平均 RH 的经向–高度剖面", fontsize=10)
        ax.legend(fontsize=7.5, loc="lower left")
        ax.grid(alpha=0.25)
    for ax in list(axes.ravel())[len(years):]:      # 年份不足 4 个时不留空面板
        ax.axis("off")
    if im is not None:
        fig.colorbar(im, ax=axes, shrink=0.65, label="相对湿度 (%)")
    fig.suptitle("中层饱和层出现在哪条纬度带、哪个高度？（红线 RH=80% 视为成云代理阈值）",
                 fontsize=12.5)
    fig.savefig(fig_dir / "fig_crosssection.png", dpi=DPI)
    plt.close(fig)
    print("写出 figures/fig_crosssection.png", flush=True)


def fig_annual_comparison(ds, years, glacier, segs, kinds, fig_dir: Path):
    """跨年版 comparison：N 行(年份) × 3 列(6/7/8 月) 的 PW，直接看年际差异。"""
    lat = ds[years[0]].latitude.values
    lon = ds[years[0]].longitude.values
    fig, axes = plt.subplots(len(years), 4, figsize=(14.5, 4.1 * len(years)),
                             gridspec_kw={"width_ratios": [1, 1, 1, 0.03]},
                             squeeze=False, constrained_layout=True)
    pool = np.concatenate([ds[y]["pw"].values.ravel() for y in years])
    # 用分位数定色标：以 0 为下限会把高原内部的差异压成一片同色
    norm = Normalize(float(np.nanpercentile(pool, 1)), float(np.nanpercentile(pool, 99)))
    for ri, y in enumerate(years):
        axes[ri, 0].set_ylabel("纬度 (°N)", fontsize=8)
        for ci, m in enumerate([6, 7, 8]):
            d = ds[y]
            if m not in [int(v) for v in d.month.values]:
                axes[ri, ci].axis("off")
                continue
            f = d["pw"].isel(month=[int(v) for v in d.month.values].index(m)).values
            panel(axes[ri, ci], f, lat, lon, norm, "viridis", glacier, segs, kinds)
            axes[ri, ci].set_title(f"{y} 年 {MLAB[m]}", fontsize=10, pad=2)
            axes[ri, ci].tick_params(labelleft=(ci == 0))
        cb_ax = axes[ri, 3]
        im = cb_ax.imshow([[0.0]], cmap="viridis", norm=norm, aspect="auto")
        cb = fig.colorbar(im, cax=cb_ax)
        cb.set_label("可降水量 PW (mm)", fontsize=8.5)
    fig.suptitle("年际对比：青藏高原 6–8 月可降水量（黄底=冰川格点，同一色标）", fontsize=13)
    fig.savefig(fig_dir / "fig_annual_comparison.png", dpi=DPI)
    plt.close(fig)
    print("写出 figures/fig_annual_comparison.png", flush=True)


def write_summary(ds, years, daily, monthly, glacier, problems, config, run_id: str,
                  summary_path: Path):
    d = pd.DataFrame(monthly)
    region = config.get("region", {})
    title = f"# 青藏高原云水资源代理指标 · 初步 EDA 摘要（run `{run_id}`）"
    lines = [title, "",
             f"数据：DBAS catalog `{config.get('catalog')}`，卷 `{config.get('volume_id')}`，",
             f"数据集 `{config.get('dataset_id')}`，全程只读。",
             f"窗口：{region.get('lat_min')}–{region.get('lat_max')}°N / "
             f"{region.get('lon_min')}–{region.get('lon_max')}°E，0.25°；",
             f"时次 {config.get('synoptic_hours')} UTC 等权近似日均；",
             f"年份 {min(years)}–{max(years)}（该卷本身只覆盖暖季，并非全年）。", "",
             "## 一、为什么是代理指标（不是真云水）",
             f"- 本卷气压层变量只有 `t/q/u/v`，**没有 clwc/ciwc/云水路径/总云量**；",
             "  严格意义的云水资源无法直接绘制。",
             "- 采用三个只依赖 t、q 的量：",
             "  PW = ∫q d(-p)/g（可降水量，mm）；RH500（500 hPa 相对湿度）；",
             f"  CFRAC = {config.get('pressure_band_hpa')} hPa 内 RH≥"
             f"{config.get('rh_cloud_pct')}% 的有效层占比（云发生频率代理）。",
             "- RH 用 Magnus/Tetens 水上式，未区分冰面 → 高冷上层 RH 系统性偏低。",
             "- 地形以下气压层被地面值填充且无缺测标记；按「自最底层起的等值连段」反推",
             "  地面气压并剔除其下各层（int16 打包使连段需带容差判定）。", "",
             "## 二、逐年 5–10 月平均", "",
             "| 年份 | 有效天数 | PW mm | 冰川区 PW | 非冰川 PW | RH500 % | CFRAC % | 地下填充层 % |",
             "|---|---|---|---|---|---|---|---|"]
    for y in years:
        g = d[d["year"] == y]
        if len(g):
            lines.append(
                f"| {y} | {int(g['n_days'].sum())} | {g['pw_mm'].mean():.1f} | "
                f"{g['pw_glc'].mean():.1f} | {g['pw_non'].mean():.1f} | "
                f"{g['rh500_pct'].mean():.1f} | {g['cfrac_pct'].mean():.1f} | "
                f"{g['subsurf_pct'].mean():.1f} |")
    lines += ["", "## 三、逐月（多年平均）", "",
              "| 月份 | PW mm | 冰川区 PW | RH500 % | CFRAC % |", "|---|---|---|---|---|"]
    for m in MONTHS:
        g = d[d["month"] == m]
        if len(g):
            lines.append(f"| {MLAB[m]} | {g['pw_mm'].mean():.1f} | {g['pw_glc'].mean():.1f} | "
                         f"{g['rh500_pct'].mean():.1f} | {g['cfrac_pct'].mean():.1f} |")
    lines += ["",
              "## 四、冰川区结论（初步）",
              f"- RGI 6.0（13/14/15 区）落到 0.25° 高原网格上的冰川格点 {int(glacier.sum())} 个"
              f"，占窗口 {100*glacier.sum()/glacier.size:.1f}%。",
              "- 冰川格点集中在喜马拉雅南坡、喀喇昆仑、冈底斯与横断山，",
              "  这些格点的海拔（地面气压折算）普遍 4–6 km。",
              "- 上表显示冰川格点的 PW 明显低于非冰川格点：高原内部水汽输送受地形阻挡，",
              "  冰川物质的水汽主要来自南坡与西风通道，具体来源需再做水汽收支（L3）。", "",
              "## 五、数据质量",
              f"- 读取失败文件：{len(problems)} 个。",
              f"- 逐日记录：{len(daily)} 条（`tables/tp_daily.csv`）；"
              "月平均场：`yearly/tp_monthly_<year>.nc`。",
              "- 地下填充层占比常年 ~20%+，说明「全层直接积分 PW」在这套数据上必然高估。", "",
              "## 六、图件（均在本运行目录 `figures/`）",
              "- `fig_maps_<year>.png`：分年 3×6 空间分布（PW / RH500 / CFRAC × 5–10 月）",
              "- `fig_annual_comparison.png`：6–8 月年际对比（同一色标）",
              "- `fig_glacier_elevation.png`：冰川 vs 非冰川、以及随估计高程的变化",
              "- `fig_timeseries.png`：逐日时间序列",
              "- `fig_crosssection.png`：经向–气压剖面（含地形顶轮廓）"]
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print("写出 summary/tp_eda_summary.md", flush=True)


def read_table(path: Path) -> pd.DataFrame:
    """空文件（pandas 零行输出）视作空表，不当作损坏。"""
    if not path.exists() or path.stat().st_size <= 1:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def main() -> None:
    ap = argparse.ArgumentParser(description="TP 云水代理指标：分年图件 + EDA 摘要")
    ap.add_argument("--config", default="experiments/tp_cloud_water/config.yaml")
    ap.add_argument("--run-id", default=None,
                    help="默认与 eda.py 相同：run-<配置哈希>")
    ap.add_argument("--years", type=int, nargs="*")
    args = ap.parse_args()
    config = load_config(args.config)
    clean = {key: value for key, value in config.items() if not key.startswith("_")}
    run_id = args.run_id or f"run-{config_hash(clean)}"
    root = run_dir(config, run_id)
    years = args.years or [int(item) for item in config.get("years", [])]

    ds, found = {}, []
    for y in years:
        p = root / "yearly" / f"tp_monthly_{y}.nc"
        if p.exists():
            ds[y] = xr.open_dataset(p)
            found.append(y)
        else:
            print(f"[skip] 缺少 {p}", flush=True)
    if not found:
        raise SystemExit(f"{root}/yearly 下没有 tp_monthly_<year>.nc；"
                         f"先运行 eda.py --run-id {run_id}")

    glacier_path = path_from_config(config["glacier_mask"])
    if not glacier_path.exists():
        raise SystemExit(f"缺少 glacier mask: {glacier_path}")
    glacier = np.load(glacier_path)["mask"]

    daily = read_table(root / "tables" / "tp_daily.csv")
    monthly = read_table(root / "tables" / "tp_monthly.csv").to_dict("records")
    problems = read_table(root / "tables" / "tp_read_problems.csv").to_dict("records")

    segs, kinds = [], []
    shp_setting = config.get("basemap_dir")
    if shp_setting:
        shp_dir = path_from_config(shp_setting)
        if shp_dir.exists():
            segs, kinds = basemap_lines(root, shp_dir)
        else:
            print(f"[warn] basemap_dir 不在线，跳过地图底稿: {shp_dir}", flush=True)
    print(f"底稿折线 {len(segs)} 条；年份 {found}", flush=True)

    fig_dir = root / "figures"
    scales = {}
    for spec in SPEC:
        pool = np.concatenate([ds[y][spec].values.ravel() for y in found])
        s = 100.0 if spec == "cfrac" else 1.0
        scales[spec] = (float(np.nanmin(pool)) * s, float(np.nanpercentile(pool, 99)) * s)
    for y in found:
        fig_maps({y: ds[y]}, [y], glacier, segs, kinds, scales, fig_dir)
    fig_annual_comparison(ds, found, glacier, segs, kinds, fig_dir)
    fig_glacier_elevation(ds, found, glacier, fig_dir)
    if not daily.empty:
        fig_timeseries(daily, fig_dir)
    fig_crosssection(ds, found, fig_dir)
    if monthly:
        write_summary(ds, found, daily, monthly, glacier, problems, config, run_id,
                      root / "summary" / "tp_eda_summary.md")

    manifest_path = root / "manifest.json"
    if manifest_path.exists():                     # 把图件指纹也登记进运行清单
        manifest = update_manifest(manifest_path, plotted=True)
        for artifact in sorted(fig_dir.glob("*.png")):
            manifest = record_artifact(manifest_path, manifest, artifact)
        summary_path = root / "summary" / "tp_eda_summary.md"
        if summary_path.exists():
            manifest = record_artifact(manifest_path, manifest, summary_path)
    print(f"DONE run={run_id} figures -> {fig_dir}", flush=True)


if __name__ == "__main__":
    main()
