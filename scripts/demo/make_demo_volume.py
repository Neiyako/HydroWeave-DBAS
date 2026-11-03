"""生成一个合成 ERA5 风格的数据卷（用于演示与测试，不触碰真实数据）。

用法: .venv/bin/python scripts/demo/make_demo_volume.py [目标目录] [文件数]

不带参数时写入 data/demo_volume 下 5 个文件（README「30 秒」流程默认用法）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from tests.conftest import make_nc  # noqa: E402

_USAGE = __doc__


def main() -> int:
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(_USAGE.strip())
        return 0
    # 以 "--" 开头的参数一律拒绝：曾出现过把 --help 当成目录名、
    # 在当前目录建出 ./--help 并写入合成文件的事故。
    for a in args:
        if a.startswith("-"):
            print(f"未知参数: {a}\n\n{_USAGE.strip()}", file=sys.stderr)
            return 2

    root = Path(args[0]) if len(args) > 0 else Path("data/demo_volume")
    try:
        n = int(args[1]) if len(args) > 1 else 5
    except ValueError:
        print(f"文件数必须是整数: {args[1]!r}", file=sys.stderr)
        return 2
    if n <= 0:
        print(f"文件数必须为正: {n}", file=sys.stderr)
        return 2

    for i in range(n):
        year = 2023 + i // 3
        make_nc(root / f"{year}/era5_demo_{i:02d}.nc", n_time=4 + i,
                var_name="t2m" if i % 2 == 0 else "specific_humidity",
                units="K" if i % 2 == 0 else "kg kg**-1")
    print(f"已生成 {n} 个合成 NetCDF 文件于 {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
