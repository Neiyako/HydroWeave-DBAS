#!/usr/bin/env python3
"""生成一套自包含的「青藏高原云水代理 EDA」合成试验台。

真实 ERA5 卷有 2TB 且依赖外接盘，无法在 CI 或离机环境里跑通链路。本脚本用
确定性合成数据复刻实验代码实际依赖的全部结构：

* ``<dest>/volume/<year>/ERA5_025dg_<YYYYMMDD>_{specific_humidity,temperature}.nc``
  —— 含 37 个气压层、逐小时时次、纬度降序的 ERA5 风格文件；地形以下各层按
  地面值填充，用来复现 ``sub_ground_mask`` 的连段检测；
* ``<dest>/catalog.db`` —— 只含 EDA 读取的 volumes/files 两表，卷 ID 与数据集 ID
  可配置；
* ``<dest>/glacier_mask_025.npz`` —— 与裁切后网格同形的冰川掩膜；
* ``<dest>/config.yaml`` —— 指向以上绝对路径、``basemap_dir`` 为空的实验配置。

用法::

    .venv/bin/python scripts/demo/make_tp_cloud_water_fixture.py [dest] [--days 3]
    PYTHONPATH=src:. .venv/bin/python -m experiments.tp_cloud_water.eda \
        --config <dest>/config.yaml --run-id fixture
    PYTHONPATH=src:. .venv/bin/python -m experiments.tp_cloud_water.plot \
        --config <dest>/config.yaml --run-id fixture
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import netCDF4
import numpy as np
import yaml

LEVELS = (1, 2, 3, 5, 7, 10, 20, 30, 50, 70, 100, 125, 150, 175, 200, 225, 250,
          300, 350, 400, 450, 500, 550, 600, 650, 700, 750, 775, 800, 825, 850,
          875, 900, 925, 950, 975, 1000)
HOURS = 4                                   # 00/06/12/18 UTC，与实验抽样一致
SURFACE_P = 650.0                           # 合成「地面气压」：其下各层为填充值


def _fill_levels(levels: np.ndarray) -> int:
    """返回地表层下标（压力 >= SURFACE_P 的最顶层），其下按填充值处理。"""
    below = np.where(levels >= SURFACE_P)[0]
    return int(below[0]) if below.size else levels.size - 1


def _temperature(levels, lat, lon, day):
    """标准大气量级的温度场 (K)，随纬度、经度与日期小幅变化。"""
    p = levels[:, None, None].astype("float64")
    la = lat[None, :, None].astype("float64")
    lo = lon[None, None, :].astype("float64")
    return (288.0 * (p / 1000.0) ** 0.19 + 4.0 * np.cos(np.deg2rad(la))
            - 2.0 * np.sin(np.deg2rad(lo)) + 0.02 * day)


def _relative_humidity(levels, lat, lon, day):
    """目标相对湿度 (0–1)：中层 400–500 hPa 附近达到饱和峰，便于演示 CFRAC。"""
    p = levels[:, None, None].astype("float64")
    la = lat[None, :, None].astype("float64")
    lo = lon[None, None, :].astype("float64")
    ridge = 0.35 * np.exp(-(((p - 450.0) / 250.0) ** 2))
    field = 0.50 + ridge + 0.05 * np.cos(np.deg2rad(la)) \
        + 0.02 * np.sin(np.deg2rad(lo)) + 0.005 * day
    return np.clip(field, 0.05, 0.98)


def _profile(levels, lat, lon, day, var):
    """确定性三维场：先给出 T 与目标 RH，再由 RH 反算 q（保证物理合理）。"""
    temp = _temperature(levels, lat, lon, day)
    if var == "t":
        return temp
    p_pa = levels[:, None, None].astype("float64") * 100.0
    tc = temp - 273.15
    es = 611.2 * np.exp(17.67 * tc / (tc + 243.5))
    vapour = _relative_humidity(levels, lat, lon, day) * es
    return 0.622 * vapour / (p_pa - vapour)


def write_day(volume: Path, year: int, month: int, day: int) -> list[Path]:
    lat = np.arange(70.0, -10.01, -1.0, dtype="float32")       # 降序，模拟 ERA5 原始排布
    lon = np.arange(50.0, 160.01, 1.0, dtype="float32")
    levels = np.asarray(LEVELS)
    stamp = f"{year:04d}{month:02d}{day:02d}"
    paths = []
    for var, name in (("q", "specific_humidity"), ("t", "temperature")):
        path = volume / str(year) / f"ERA5_025dg_{stamp}_{name}.nc"
        path.parent.mkdir(parents=True, exist_ok=True)
        ds = netCDF4.Dataset(path, "w")
        ds.Conventions = "CF-1.6"
        ds.source = "synthetic tp_cloud_water fixture"
        ds.createDimension("time", HOURS)
        ds.createDimension("level", levels.size)
        ds.createDimension("latitude", lat.size)
        ds.createDimension("longitude", lon.size)
        t = ds.createVariable("time", "f8", ("time",))
        t.units = f"hours since {year:04d}-{month:02d}-{day:02d} 00:00:00"
        t.calendar = "standard"
        t[:] = np.arange(HOURS) * 6.0
        lev = ds.createVariable("level", "i4", ("level",))
        lev.units = "millibars"
        lev[:] = levels
        la = ds.createVariable("latitude", "f4", ("latitude",))
        la.units = "degrees_north"
        la[:] = lat
        lo = ds.createVariable("longitude", "f4", ("longitude",))
        lo.units = "degrees_east"
        lo[:] = lon
        field = _profile(levels, lat, lon, day, var)
        ground = _fill_levels(levels)
        field[ground:, :, :] = field[ground, :, :]              # 地形以下填充为地面值
        data = np.broadcast_to(field, (HOURS, *field.shape)).astype("float32").copy()
        hours = np.arange(HOURS)[:, None, None, None]
        if var == "q":                     # 给时次一点变化，但保持填充层内部等值
            data *= 1.0 + 0.002 * hours
        else:
            data += 0.01 * hours
        v = ds.createVariable(var, "f4", ("time", "level", "latitude", "longitude"),
                              zlib=True, complevel=1)
        v.units = "kg kg**-1" if var == "q" else "K"
        v[:] = data
        ds.close()
        paths.append(path)
    return paths


def make_catalog(dest: Path, volume: Path, volume_id: str, dataset_id: str) -> Path:
    db_path = dest / "catalog.db"
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE volumes (
            volume_id TEXT PRIMARY KEY, label TEXT, root_path TEXT NOT NULL,
            last_mount_path TEXT, status TEXT NOT NULL DEFAULT 'unknown',
            first_seen TEXT NOT NULL, last_seen TEXT, notes TEXT);
        CREATE TABLE files (
            file_id TEXT PRIMARY KEY, volume_id TEXT NOT NULL, rel_path TEXT NOT NULL,
            file_name TEXT NOT NULL, size_bytes INTEGER, mtime REAL, format TEXT,
            status TEXT NOT NULL DEFAULT 'new', parse_status TEXT NOT NULL DEFAULT 'unparsed',
            dataset_id TEXT, UNIQUE(volume_id, rel_path));
        """)
    db.execute(
        "INSERT INTO volumes VALUES (?,?,?,?,?,?,?,?)",
        (volume_id, "synthetic fixture", str(volume), str(volume), "online",
         "2026-01-01T00:00:00", "2026-01-01T00:00:00", "fixture"))
    for path in sorted(volume.rglob("*.nc")):
        rel = path.relative_to(volume).as_posix()
        stat = path.stat()
        db.execute(
            "INSERT INTO files (file_id,volume_id,rel_path,file_name,size_bytes,mtime,"
            "format,status,parse_status,dataset_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{volume_id}/{rel}", volume_id, rel, path.name, stat.st_size,
             stat.st_mtime, "nc4", "active", "parsed", dataset_id))
    db.commit()
    db.close()
    return db_path


def build(dest: Path, *, years=(2010,), days: int = 3, volume_id: str = "fixture_hd",
          dataset_id: str = "era5_pressure") -> Path:
    dest = dest.expanduser().resolve()
    volume = dest / "volume"
    for year in years:
        for index in range(days):
            write_day(volume, year, 5, 1 + index)
    make_catalog(dest, volume, volume_id, dataset_id)

    # 与 EDA 的裁切网格同形：region 25–40N / 70–105E，1° 网格
    lat = np.arange(40.0, 24.99, -1.0)
    lon = np.arange(70.0, 105.01, 1.0)
    yy, xx = np.meshgrid(lat, lon, indexing="ij")
    mask = ((yy > 28) & (yy < 36) & (xx > 78) & (xx < 92)).astype("uint8")
    np.savez_compressed(dest / "glacier_mask_025.npz", mask=mask)

    config = {
        "experiment": "tp_cloud_water",
        "catalog": str(dest / "catalog.db"),
        "volume_id": volume_id,
        "dataset_id": dataset_id,
        "years": list(years),
        "region": {"lat_min": 25.0, "lat_max": 40.0, "lon_min": 70.0, "lon_max": 105.0},
        "synoptic_hours": [0, 6, 12, 18],
        "pressure_band_hpa": [100.0, 500.0],
        "rh_cloud_pct": 80.0,
        "batch_days": 2,
        "workers": 2,
        "output_dir": str(dest / "analysis"),
        "glacier_mask": str(dest / "glacier_mask_025.npz"),
        "basemap_dir": None,
        "seed": 0,
    }
    config_path = dest / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
    return config_path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 TP 云水 EDA 合成试验台")
    parser.add_argument("dest", nargs="?", default="data/tmp/tp_cloud_water_fixture")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--years", type=int, nargs="*", default=[2010])
    parser.add_argument("--volume-id", default="fixture_hd")
    args = parser.parse_args()
    config_path = build(Path(args.dest), years=tuple(args.years), days=args.days,
                        volume_id=args.volume_id)
    print(f"fixture ok: {config_path}")


if __name__ == "__main__":
    main()
