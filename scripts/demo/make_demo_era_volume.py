"""生成"类 ERA5"合成数据卷：全球网格、逐月多日、单多层级变量，
并刻意包含三种真实世界会遇到的坐标轴约定，供 subset 链路演示与自检：

  文件0: 纬度升序、经度 -180..180（新 CDS NetCDF 常见）
  文件1: 纬度降序、经度 -180..180（ERA5 grid 原始排布）
  文件2: 纬度降序、经度 0..359（GRIB 传统约定）

用法: .venv/bin/python scripts/demo/make_demo_era_volume.py [目标目录] [月数]
"""
from __future__ import annotations

import sys
from pathlib import Path

import netCDF4
import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

FILL = np.float32(9.96921e36)

# (变量, 单位, 基准值函数 seed)
VARS = [
    ("t2m", "K", 260.0),
    ("sp", "Pa", 70000.0),
    ("tcc", "(0 - 1)", 0.4),
    ("tp", "m", 0.002),
]


def _field(seed: float, lat, lon, nt):
    """确定性场：随纬度、经度、时间平滑变化，便于肉眼/断言核对裁切结果。"""
    return (seed + 20.0 * np.cos(np.deg2rad(lat))[None, :, None]
            + 0.02 * lon[None, None, :]
            + 0.1 * np.arange(nt)[:, None, None]).astype(np.float32)


def make_file(path: Path, *, month: int, nt: int, lat_desc: bool,
              lon_360: bool):
    lat = np.arange(-90.0, 90.5, 1.0) if not lat_desc \
        else np.arange(90.0, -90.5, -1.0)
    lon = np.arange(-180.0, 180.0, 1.0) if not lon_360 \
        else np.arange(0.0, 360.0, 1.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = netCDF4.Dataset(path, "w")
    ds.Conventions = "CF-1.8"
    ds.source = "synthetic era5-like demo"
    ds.createDimension("time", nt)
    ds.createDimension("latitude", lat.size)
    ds.createDimension("longitude", lon.size)
    t = ds.createVariable("time", "f8", ("time",))
    t.units = f"hours since {2010 + (month - 1) // 12:04d}-" \
              f"{(month - 1) % 12 + 1:02d}-01 00:00:00"
    t.calendar = "standard"
    t[:] = np.arange(nt) * 24.0          # 逐日
    la = ds.createVariable("latitude", "f4", ("latitude",))
    la.units = "degrees_north"
    la[:] = lat
    lo = ds.createVariable("longitude", "f4", ("longitude",))
    lo.units = "degrees_east"
    lo[:] = lon
    for name, units, seed in VARS:
        v = ds.createVariable(name, "f4", ("time", "latitude", "longitude"),
                              fill_value=FILL, zlib=True, complevel=1)
        v.units = units
        v.standard_name = name
        data = _field(seed, lat.astype(np.float32),
                      lon.astype(np.float32), nt)
        data[0, 5, 5] = FILL             # 留一个缺测点给 QC 演示
        v[:] = data
    ds.close()
    return path


def main() -> int:
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__.strip())
        return 0
    # 以 "--" 开头的参数一律拒绝：否则会被当成目录名，在当前目录建出 ./--help
    for a in args:
        if a.startswith("-"):
            print(f"未知参数: {a}\n\n{__doc__.strip()}", file=sys.stderr)
            return 2
    root = Path(args[0]) if len(args) > 0 else Path("data/era_like_volume")
    try:
        n_months = int(args[1]) if len(args) > 1 else 3
    except ValueError:
        print(f"月份数必须是整数: {args[1]!r}", file=sys.stderr)
        return 2
    specs = [dict(lat_desc=False, lon_360=False),
             dict(lat_desc=True, lon_360=False),
             dict(lat_desc=True, lon_360=True)]
    for i in range(n_months):
        sp = specs[i % len(specs)]
        month = 1 + i
        p = make_file(root / "2010" / f"era5_daily_2010{month:02d}.nc",
                      month=month, nt=6, **sp)
        print(f"  {p}  (lat {'降' if sp['lat_desc'] else '升'}序, "
              f"lon {'0-360' if sp['lon_360'] else '-180-180'})")
    print(f"已生成 {n_months} 个类 ERA5 文件于 {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
