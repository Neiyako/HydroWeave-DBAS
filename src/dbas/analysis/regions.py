"""分析区域注册表。

约定：bbox 一律用 [-180, 180) 经度体系、lat_min < lat_max。
裁切代码在归一化坐标轴之后按此体系选点。
"""
from __future__ import annotations

from dataclasses import dataclass

from dbas.core.exceptions import AnalysisError


@dataclass(frozen=True)
class Bbox:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def __post_init__(self):
        if not (self.lat_min < self.lat_max and self.lon_min < self.lon_max):
            raise AnalysisError(f"非法 bbox: {self}")
        if not (-90 <= self.lat_min and self.lat_max <= 90):
            raise AnalysisError(f"纬度超出 [-90,90]: {self}")
        if not (-180 <= self.lon_min and self.lon_max <= 180):
            raise AnalysisError(f"经度须用 [-180,180] 体系: {self}")

    @property
    def label(self) -> str:
        return (f"{self.lat_min:.1f}–{self.lat_max:.1f}°N, "
                f"{self.lon_min:.1f}–{self.lon_max:.1f}°E")

    def contains_point(self, lat: float, lon: float) -> bool:
        lon = ((lon + 180.0) % 360.0) - 180.0
        return (self.lat_min <= lat <= self.lat_max
                and self.lon_min <= lon <= self.lon_max)


# 预定义研究区域
REGIONS: dict[str, Bbox] = {
    # 青藏高原主体
    "qhp_main": Bbox(25, 40, 70, 105),
    # 高原 + 上游水汽源区（印度洋侧），做水汽收支时用
    "qhp_wide": Bbox(20, 55, 50, 120),
}


def get_region(name: str) -> Bbox:
    try:
        return REGIONS[name]
    except KeyError:
        raise AnalysisError(
            f"未知区域 '{name}'，已注册: {', '.join(sorted(REGIONS))}") from None


def overlaps_file_grid(bbox: Bbox, lat_range: tuple | None,
                       lon_range: tuple | None) -> bool | None:
    """判断 bbox 与文件存储网格范围是否可能相交（供 plan 用，不读数据）。

    None = 文件范围未知，无法判断。0–360 经度轴的文件自动折算比较。
    """
    if lat_range is not None:
        if not (lat_range[1] >= bbox.lat_min and lat_range[0] <= bbox.lat_max):
            return False
    if lon_range is None:
        return None
    lo, hi = lon_range
    b_lo, b_hi = bbox.lon_min, bbox.lon_max
    if hi > 180.0:  # 0–360 轴：把 bbox 折算到同一空间
        b_lo, b_hi = b_lo % 360.0, b_hi % 360.0
        if b_lo <= b_hi:
            hit = lo <= b_hi and hi >= b_lo
        else:  # bbox 跨 0°经线折算后分裂，两段分别判
            hit = lo <= b_hi or hi >= b_lo
    else:
        hit = lo <= b_hi and hi >= b_lo
    if lat_range is None:
        return None
    return hit
