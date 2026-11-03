#!/usr/bin/env python3
"""兼容入口：转发到 :mod:`experiments.tp_cloud_water.plot`。

旧命令 ``python scripts/analysis/tp_cloud_water_plot.py [--years ...]`` 仍可用；新代码在
``experiments/tp_cloud_water/plot.py``，按 ``--run-id`` 读取对应运行目录。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "src", ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from experiments.tp_cloud_water.plot import main  # noqa: E402

if __name__ == "__main__":
    main()
