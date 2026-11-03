"""Zarr 数组层：把 ERA5 派生数据从「文件」升维成「可切片数组」。

分工一句话：**catalog 记身份与证据，Zarr 管切片与并行**。
- 原始 .nc 保持只读，逐文件 sha256 / QC 规则口径不变；
- Zarr store 作为数组层条目登记，用清单哈希（manifest digest）做比特级证据；
- 分块方案由查询模式反推（见 store.plan_chunks），不照抄公开数据集。

入口：
    from dbas.zarr_export.tasks import zarr_export_one, reconcile_store
    from dbas.zarr_export.store import plan_chunks, export_store
    from dbas.zarr_export.manifest import scan_store, compare_manifests
"""
from __future__ import annotations

from dbas.zarr_export.manifest import compare_manifests, scan_store
from dbas.zarr_export.store import (ChunkPlan, TARGET_CHUNK_ELEMENTS,
                                    TARGET_RULES, ZarrExportError,
                                    estimate_chunk_bytes, export_store,
                                    plan_chunks)
from dbas.zarr_export.tasks import reconcile_store, store_rel_path, zarr_export_one

__all__ = [
    "ChunkPlan", "TARGET_RULES", "TARGET_CHUNK_ELEMENTS", "ZarrExportError",
    "plan_chunks", "export_store", "estimate_chunk_bytes",
    "scan_store", "compare_manifests",
    "zarr_export_one", "reconcile_store", "store_rel_path",
]
