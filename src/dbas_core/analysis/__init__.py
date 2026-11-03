"""分析层：区域裁切（subset）与后续的 QC / 派生量。

subset 依赖 dask/xarray，而这两个依赖属于可选 extras `[eda]`。为了让
「基础安装」（只装 numpy/netCDF4 等必需项）也能 import 本包的其它模块，
这里对 subset 采用**惰性导入**：真正用到 subset_* 时才加载 dask。

这样 `pip install -e .` 后所有命令都能启动，只有实际执行区域裁切时才
提示需要额外安装 `[eda]`。
"""
from dbas_core.analysis.regions import Bbox, REGIONS, get_region

_SUBSET_NAMES = ("ensure_analysis_volume", "normalize_axes",
                 "subset_dataset", "subset_one")

__all__ = ["Bbox", "REGIONS", "get_region", *_SUBSET_NAMES]


def __getattr__(name: str):
    """PEP 562：仅在访问 subset 相关符号时才导入 subset 模块。"""
    if name in _SUBSET_NAMES:
        from dbas_core.analysis import subset as _subset
        return getattr(_subset, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
