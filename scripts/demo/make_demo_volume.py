"""生成一个合成 ERA5 风格的数据卷（用于演示与测试，不触碰真实数据）。

用法: .venv/bin/python scripts/make_demo_volume.py [目标目录] [文件数]
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from tests.conftest import make_nc  # noqa: E402


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/demo_volume")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    for i in range(n):
        year = 2023 + i // 3
        make_nc(root / f"{year}/era5_demo_{i:02d}.nc", n_time=4 + i,
                var_name="t2m" if i % 2 == 0 else "specific_humidity",
                units="K" if i % 2 == 0 else "kg kg**-1")
    print(f"已生成 {n} 个合成 NetCDF 文件于 {root}")


if __name__ == "__main__":
    main()
