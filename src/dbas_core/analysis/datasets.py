"""数据集注册表与自动识别。

数据集（dataset）表达「同一来源、同一变量集、同一网格/时间约定」的一组文件，
与物理数据卷（volume）解耦：一个卷可以混放多个数据集，一个数据集也可以跨卷。

识别只依赖路径/文件名，不读文件内容；无法识别的归入 unknown。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    label: str
    description: str
    source: str
    variables: frozenset[str]
    time_resolution: str | None = None       # hourly/daily/...；None=无时间维
    filename_pattern: str | None = None      # 命名规范的数据集可做文件名级覆盖率
    variable_aliases: tuple[tuple[str, str], ...] = ()

    def alias_map(self) -> dict[str, str]:
        return dict(self.variable_aliases)

    def parse_filename(self, file_name: str) -> tuple[str | None, str | None]:
        """返回 (日期字符串, 规范变量名)；不匹配返回 (None, None)。"""
        if not self.filename_pattern:
            return None, None
        m = re.match(self.filename_pattern, file_name)
        if not m:
            return None, None
        gd = m.groupdict()
        date = gd.get("date") or (m.group(1) if m.groups() else None)
        raw_var = gd.get("var") or (m.group(2) if len(m.groups()) > 1 else None)
        if raw_var is None:
            return date, None
        return date, self.alias_map().get(raw_var, raw_var)


DATASETS: dict[str, DatasetSpec] = {
    "era5_pressure": DatasetSpec(
        dataset_id="era5_pressure",
        label="ERA5 0.25° 气压层日文件",
        description="t/q/u/v 四个气压层变量，37 层，逐小时，按日分文件",
        source="ERA5 / CDS",
        variables=frozenset({"t", "q", "u", "v"}),
        time_resolution="hourly",
        filename_pattern=r"^ERA5_025dg_(?P<date>\d{8})_(?P<var>.+)\.nc$",
        variable_aliases=(
            ("temperature", "t"),
            ("specific_humidity", "q"),
            ("u_component_of_wind", "u"),
            ("v_component_of_wind", "v"),
        ),
    ),
    "modis_air_quality": DatasetSpec(
        dataset_id="modis_air_quality",
        label="MODIS 经纬度网格大气成分产品",
        description="CO/NO2/O3/PM1/PM2.5/PM10/SO2 等网格产品",
        source="MODIS / ECHAP",
        variables=frozenset({"CO", "NO2", "O3", "PM1", "PM2.5", "PM10", "SO2"}),
        time_resolution=None,
    ),
    "eraint_wind": DatasetSpec(
        dataset_id="eraint_wind",
        label="ERA-Interim 10m 风场",
        description="u10/v10/si10 全球网格",
        source="ERA-Interim",
        variables=frozenset({"u10", "v10", "si10"}),
        time_resolution="half_daily",
    ),
    "unknown": DatasetSpec(
        dataset_id="unknown",
        label="未识别数据集",
        description="未匹配到内置识别规则的数据集",
        source="",
        variables=frozenset(),
        time_resolution=None,
    ),
}


def get_dataset_spec(dataset_id: str) -> DatasetSpec:
    return DATASETS.get(dataset_id, DATASETS["unknown"])


def detect_dataset(rel_path: str, file_name: str | None = None) -> str:
    """按卷内相对路径/文件名识别数据集。规则保持显式、可测试。"""
    name = file_name or Path(rel_path).name
    posix = rel_path.replace("\\", "/")
    upper_name = name.upper()

    if name.startswith("ERA5_025dg_"):
        return "era5_pressure"
    if "MODIS_JINGWEI" in posix.upper() or name.startswith("ECHAP_"):
        return "modis_air_quality"
    if "ERAINT" in upper_name:
        return "eraint_wind"
    return "unknown"


def ensure_builtin_datasets(catalog) -> None:
    """把内置数据集定义同步到 catalog（标签/描述可随代码升级）。"""
    for spec in DATASETS.values():
        catalog.ensure_dataset(spec.dataset_id, spec.label,
                               spec.description, spec.source)


__all__ = ["DatasetSpec", "DATASETS", "detect_dataset", "get_dataset_spec",
           "ensure_builtin_datasets"]
