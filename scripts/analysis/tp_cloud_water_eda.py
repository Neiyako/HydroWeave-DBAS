#!/usr/bin/env python3
"""兼容入口：转发到 :mod:`experiments.tp_cloud_water.eda`。

旧命令 ``python scripts/analysis/tp_cloud_water_eda.py [--years ...] [--force]`` 仍可用；
新代码在 ``experiments/tp_cloud_water/eda.py``，配置在
``experiments/tp_cloud_water/config.yaml``，产物按 ``--run-id`` 分目录。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "src", ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from experiments.tp_cloud_water.eda import main  # noqa: E402

if __name__ == "__main__":
    main()
