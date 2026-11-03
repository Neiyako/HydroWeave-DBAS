"""配置扩展：在 dbas_core 的 Config 之上补数组层（Zarr）所需的字段。

为什么不在 dbas_core 里改：core 是外置依赖且被 commit 钉死，改它会让
“升级 core = 悄悄改科研口径”的历史承诺失效。本仓用 monkeypatch 的方式
在导入时补齐默认段，行为等价于 core 原生支持，且字段全部可被
configs/system.yaml 覆盖（沿用 core 的 section 合并语义）。
"""
from __future__ import annotations

from pathlib import Path

from dbas_core.config import DEFAULTS, Config
from dbas.zarr_export.store import TARGET_CHUNK_ELEMENTS, TARGET_RULES

#: 数组层默认配置。字段与 analysis 段并列，便于在 system.yaml 里分组阅读。
ZARR_DEFAULTS: dict = {
    # store 根目录（分析数组层产物卷），相对项目根
    "array_dir": "data/arrays",
    # 数组层登记进 catalog 的卷 ID；与 analysis 分开，避免 .nc 产物与
    # store 目录混在同一卷里污染 subset 的 plan 统计
    "volume_id": "arrays",
    # 未指定 target 时用哪种分块策略
    "default_target": "field_series",
    # 单 chunk 元素上限（压缩前）；8M 元素 × f4 ≈ 32MB，对齐 analysis.chunk_mb
    "chunk_elements": TARGET_CHUNK_ELEMENTS,
    # 分片倍数：1=不分片；>1 时把 N 个 chunk 合并进一个分片文件
    "shards": 1,
    # zarr 压缩级别（zstd clevel）
    "complevel": 4,
    # 是否写合并元数据（consolidated）
    "consolidate": True,
    # 批量导出：单次最多文件数与时间预算，沿用 Runner 的边界语义
    "budget_s": 900,
    "batch_size": 5,
}

_installed = False


def install_zarr_config() -> None:
    """幂等地安装数组层配置段，并给 Config 补 array_dir 属性。"""
    global _installed
    if _installed:
        return
    DEFAULTS.setdefault("zarr", {}).update(
        {k: v for k, v in ZARR_DEFAULTS.items() if k not in DEFAULTS.get("zarr", {})})

    def _array_dir(self) -> Path:
        z = dict(ZARR_DEFAULTS)
        z.update(self.data.get("zarr", {}) or {})
        return self._path(z["array_dir"])

    if not hasattr(Config, "array_dir"):
        Config.array_dir = property(_array_dir)  # type: ignore[attr-defined]
    _installed = True


def available_targets() -> list[str]:
    return sorted(TARGET_RULES)


__all__ = ["ZARR_DEFAULTS", "install_zarr_config", "available_targets"]
