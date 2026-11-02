"""配置管理。

设计要点：
- 全部配置项都有内置默认值，YAML 只做覆盖（缺字段不报错）。
- 相对路径一律相对“项目根”（pyproject.toml 所在目录）解析，也支持 DBAS_HOME 环境变量显式指定。
- 机器专属路径（数据卷、CDS key）不写入 Git 跟踪的配置文件，见 README。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dbas.core.exceptions import ConfigError

# 限制底层 BLAS/数值库线程数：必须在 numpy 导入前设置才最可靠，
# 因此放在模块顶层，dbas 任何子模块 import 本包时即生效。
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

DEFAULTS: dict[str, Any] = {
    "project_name": "dbas",
    # 运行数据目录（数据库、报告、临时文件），默认在项目根下，不入 Git
    "home": "data",
    "database": {"path": "data/catalog.db", "busy_timeout_s": 30},
    "scan": {
        "batch_size": 20,
        "budget_s": 60,
        "nc_extensions": [".nc", ".nc4", ".cdf"],
        # inventory 收录范围；["*"] 为全量清单化（非 netCDF 只登记不解析）
        "inventory_extensions": [".nc", ".nc4", ".cdf"],
        # 按目录名剪枝（Windows 卷回收站/系统索引等噪声）
        "exclude_dirs": [],
    },
    "scheduler": {
        "max_attempts": 3,
        "retry_delay_s": 5,
        "stale_running_s": 600,      # running 状态心跳超过该秒数视为陈旧，可回收
        "heartbeat_s": 10,
    },
    "resource": {
        # 水位阈值是“尽量早停”的保护，不是绝对安全线（见 docs/operations.md）
        "max_rss_mb": 3072,          # 进程 RSS 警戒线（8GB 机器保守值）
        "min_available_mb": 1024,    # 系统可用内存警戒线
    },
    "profile": {
        "batch_size": 20,
        "budget_s": 120,
    },
    "analysis": {
        "output_dir": "data/analysis",   # ARD 产物卷根目录
        "volume_id": "analysis",         # 产物登记进 catalog 的卷 ID
        "region": "qhp_main",            # 默认研究区域（见 analysis/regions.py）
        "complevel": 4,                  # 输出 zlib 压缩级别
        "time_chunk": 31,                # 输出文件时间维分块上限
        "chunk_mb": 32,                  # dask 惰性读取的目标块大小（8GB 机器保守值）
        "budget_s": 600,
        "batch_size": 5,
    },
    "log_level": "INFO",
    "reports": {"dir": "data/reports"},
    "tmp": {"dir": "data/tmp"},
}


def find_project_root() -> Path:
    env = os.environ.get("DBAS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    cur = Path.cwd().resolve()
    for p in (cur, *cur.parents):
        if (p / "pyproject.toml").exists():
            return p
    return cur


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)

    # -- 常用路径 ---------------------------------------------------------
    @property
    def home(self) -> Path:
        return self._path(self.data.get("home", DEFAULTS["home"]))

    @property
    def db_path(self) -> Path:
        return self._path(self.data.get("database", {}).get(
            "path", DEFAULTS["database"]["path"]))

    @property
    def reports_dir(self) -> Path:
        return self._path(self.data.get("reports", {}).get(
            "dir", DEFAULTS["reports"]["dir"]))

    @property
    def tmp_dir(self) -> Path:
        return self._path(self.data.get("tmp", {}).get(
            "dir", DEFAULTS["tmp"]["dir"]))

    @property
    def analysis_dir(self) -> Path:
        return self._path(self.data.get("analysis", {}).get(
            "output_dir", DEFAULTS["analysis"]["output_dir"]))

    def section(self, name: str) -> dict[str, Any]:
        merged = dict(DEFAULTS.get(name, {}))
        merged.update(self.data.get(name, {}) or {})
        return merged

    def _path(self, p: str | Path) -> Path:
        path = Path(p).expanduser()
        return path if path.is_absolute() else (self.root / path)


def load_config(root: Path | None = None) -> Config:
    root = (root or find_project_root()).resolve()
    cfg_file = root / "configs" / "system.yaml"
    data: dict[str, Any] = {}
    if cfg_file.exists():
        try:
            loaded = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
            if loaded is not None:
                if not isinstance(loaded, dict):
                    raise ConfigError(f"{cfg_file} 顶层必须是映射（key: value）")
                data = loaded
        except yaml.YAMLError as e:
            raise ConfigError(f"解析 {cfg_file} 失败: {e}") from e
    return Config(root=root, data=data)


def ensure_dirs(cfg: Config) -> None:
    for d in (cfg.home, cfg.reports_dir, cfg.tmp_dir, cfg.db_path.parent):
        d.mkdir(parents=True, exist_ok=True)
