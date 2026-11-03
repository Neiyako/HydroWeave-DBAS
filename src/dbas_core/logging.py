"""统一日志。简洁格式，文件日志按需追加到 data/logs/。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", logfile: Path | None = None) -> None:
    root = logging.getLogger("dbas_core")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if root.handlers:                      # 幂等：重复调用不叠加 handler
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(h)
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"dbas_core.{name}")
