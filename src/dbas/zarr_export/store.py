"""NetCDF → Zarr 派生（L2 数组层）。

设计立场（与 subset 层一致的“原始卷只读 + 派生可再生”）：

- 输入：分析卷里的裁切产物（subset 的 .nc），或任何已登记的 .nc 文件；
- 输出：**一个 Zarr store 目录**，写进独立数组层根目录，登记为分析卷条目；
- 原始 .nc 一个字节都不动，QC/checksum 口径不变。

和 subset 的关键区别在于**分块是查询模式的函数**，不是文件大小的函数。
这一点是照抄 arco-era5 会踩的坑：它的
`{time:1, latitude:721, longitude:1440, level:37}`（单变量 154MB/chunk）
为“整场单时刻”优化，对“单点长时间序列”是最坏情况——读一个格点要打开
154MB 的 chunk。因此本模块要求显式声明 target，并按 target 反推 chunk。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dbas_core.exceptions import AnalysisError
from dbas_core.logging import get_logger

log = get_logger("zarr_export")


class ZarrExportError(AnalysisError):
    """Zarr 派生执行错误。"""


LAT_NAMES = {"lat", "latitude", "y"}
LON_NAMES = {"lon", "longitude", "x"}
TIME_NAMES = {"time", "valid_time", "t"}
LEVEL_NAMES = {"level", "pressure_level", "plev", "hybrid", "isobaricInhPa"}


def _norm(name: str) -> str:
    return str(name).strip().lower()


def _find_dim(ds, candidates: set[str]) -> str | None:
    """优先找同名的维度坐标，再退到任意维度。"""
    for d in ds.dims:
        if _norm(d) in candidates:
            return str(d)
    for c in ds.coords:
        if _norm(c) in candidates and str(c) in ds.dims:
            return str(c)
    return None


# --------------------------------------------------------------------------
# 查询模式 → 分块策略
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ChunkPlan:
    target: str
    chunks: dict[str, int]          # 维度名 → 单块长度
    rationale: str

    def as_dict(self) -> dict:
        return {"target": self.target, "chunks": dict(self.chunks),
                "rationale": self.rationale}


#: 各 target 的分块取向。数值是“目标块元素数上限”，不是字节数——
#: 因为不同变量 dtype 不同（f4/f8/i2），按字节算会在 37 层变量上爆掉。
TARGET_RULES: dict[str, dict] = {
    # 一批完整场（一个时刻全球/区域场）→ 空间整块、时间单片
    "field_series": {
        "desc": "整场单时刻为主（默认，对齐 arco 的 AR 版本）",
        "space": "full", "time": 1, "level": "full",
    },
    # 少数站点/单柱的长时间序列 → 空间小块、时间整段
    "point_series": {
        "desc": "单点/少数站点长时间序列",
        "space": 8, "time": "full", "level": "full",
    },
    # 区域平均/时间平均等统计 → 空间中等、时间中等
    "area_stats": {
        "desc": "区域平均与时间统计",
        "space": 32, "time": 128, "level": "full",
    },
    # 按层切片（等压面剖面分析）→ 层单独一片
    "level_profile": {
        "desc": "按气压层剖面检索",
        "space": "full", "time": 24, "level": 1,
    },
}

#: 目标单 chunk 元素上限（bytes = 元素数 × itemsize，压缩前）
TARGET_CHUNK_ELEMENTS = 8_000_000


def plan_chunks(ds, target: str = "field_series",
                *, chunk_elements: int = TARGET_CHUNK_ELEMENTS) -> ChunkPlan:
    """按查询模式反推每个变量的 chunk 形状。返回统一维度分块方案。

    这里返回的是**按维度**的分块，应用到所有数值变量上——代价是不同变量
    的 chunk 字节数会不同（37 层变量约为单层变量的 37 倍），换取 store 内
    所有数组对齐、任意变量可一起 `open_zarr` 并做运算。若逐变量独立分块，
    xarray 合并时会退化成极小分块的外层循环。
    """
    if target not in TARGET_RULES:
        raise ZarrExportError(
            f"未知 target '{target}'，可选: {', '.join(sorted(TARGET_RULES))}")
    rule = TARGET_RULES[target]

    lat = _find_dim(ds, LAT_NAMES)
    lon = _find_dim(ds, LON_NAMES)
    tim = _find_dim(ds, TIME_NAMES)
    lev = _find_dim(ds, LEVEL_NAMES)

    def size(d):
        return int(ds.sizes[d]) if d else None

    def pick(kind: str, dim: str | None, spatial_ref: int | None = None):
        """把规则里的 "full"/int 解析成实际长度，并受元素预算约束。"""
        if dim is None:
            return None
        n = size(dim)
        want = rule[kind]
        if want == "full":
            return n
        return min(int(want), n)

    chunks: dict[str, int] = {}
    notes: list[str] = []

    # 空间：lat/lon 同规则（"full" 或同一个数）
    for d, kind in ((lat, "space"), (lon, "space")):
        if d:
            chunks[d] = pick(kind, d)

    if lev:
        chunks[lev] = pick("level", lev)
    if tim:
        chunks[tim] = pick("time", tim)

    # 元素预算：按“最大变量”估算（含 level 维），超了依次从
    # 时间维 → level 维 → 空间维退让。顺序有讲究：时间维退让对各类查询
    # 都只是变慢，而空间维退让会破坏“整场”语义，所以放到最后。
    level_full = size(lev) or 1
    lat_n = size(lat) or 1
    lon_n = size(lon) or 1

    def _elems() -> int:
        t_c = chunks.get(tim, 1) if tim else 1
        l_c = chunks.get(lev, level_full) if lev else 1
        s_c = chunks.get(lat, lat_n) * chunks.get(lon, lon_n)
        return t_c * l_c * s_c

    # 1) 时间维优先退让
    if tim:
        while _elems() > chunk_elements and chunks[tim] > 1:
            chunks[tim] = max(1, chunks[tim] // 2)
            notes.append(f"超出元素预算，时间块减半为 {chunks[tim]}")
    # 2) level 维退让
    if lev and _elems() > chunk_elements:
        while _elems() > chunk_elements and chunks[lev] > 1:
            chunks[lev] = max(1, chunks[lev] // 2)
            notes.append(f"元素预算不足，层块减半为 {chunks[lev]}")
    # 3) 空间维最后退让（会破坏整场语义，仅在极端情况下发生）
    if _elems() > chunk_elements:
        for dim in (lat, lon):
            if not dim or _elems() <= chunk_elements:
                continue
            while _elems() > chunk_elements and chunks[dim] > 1:
                chunks[dim] = max(1, chunks[dim] // 2)
                notes.append(f"元素预算不足，空间块 {dim} 减半为 {chunks[dim]}")

    parts = [f"target={target}（{rule['desc']}）"]
    for d in (tim, lev, lat, lon):
        if d:
            parts.append(f"{d}={chunks[d]}/{size(d)}")
    if notes:
        parts.append("；".join(notes))
    return ChunkPlan(target=target, chunks=chunks, rationale="，".join(parts))


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------

def _codec_list(complevel: int):
    """zarr v3 压缩器。v3 只接受 zarr.codecs 下的 codec，不接受 numcodecs 对象。"""
    import zarr.codecs as zc
    try:
        return [zc.BloscCodec(cname="zstd", clevel=int(complevel), shuffle="shuffle")]
    except Exception:  # noqa: BLE001 不同 zarr 版本签名差异
        return [zc.ZstdCodec(level=int(complevel))]


def export_store(ds, dst: Path, plan: ChunkPlan, *,
                 shards: int | None = None,
                 complevel: int = 4,
                 consolidate: bool = True,
                 force: bool = False,
                 zarr_format: int | None = None,
                 attrs: dict | None = None) -> dict:
    """把已打开的数据集写成 Zarr store。返回统计 dict（含 skipped 标记）。

    shards: 少于该数量的 chunk 合并成一个分片文件；None/1 表示不分片。
            分片能把“一个变量上万个小 chunk 文件”压成几十个，显著降低
            移动硬盘 / 网盘同步 / rglob 校验的开销。
    """
    dst = Path(dst)
    if dst.exists():
        if not force:
            return {"skipped": True, "out": str(dst)}
        if dst.is_dir() and not (dst / "zarr.json").exists() and not (dst / ".zgroup").exists():
            raise ZarrExportError(
                f"目标目录 {dst} 已存在但不是 Zarr store；拒绝覆盖以免误删")
        shutil.rmtree(dst)

    work = dst.with_name(dst.name + ".part")
    if work.exists():
        shutil.rmtree(work)

    encoding: dict[str, dict] = {}
    for name, var in ds.data_vars.items():
        if not var.dims:
            continue
        cs = tuple(int(plan.chunks[str(d)]) for d in var.dims)
        enc: dict = {"chunks": cs, "compressors": _codec_list(complevel)}
        if shards and shards > 1:
            enc["shards"] = tuple(
                int(min(c * shards, var.shape[i])) for i, c in enumerate(cs))
        encoding[str(name)] = enc

    if attrs:
        ds = ds.assign_attrs({**dict(ds.attrs), **attrs})

    kwargs: dict = {}
    if zarr_format is not None:
        kwargs["zarr_format"] = zarr_format
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        ds.to_zarr(work, mode="w", encoding=encoding,
                   consolidated=consolidate, compute=True, **kwargs)
    except Exception as e:  # noqa: BLE001 xarray/zarr 错误种类多，统一包装
        shutil.rmtree(work, ignore_errors=True)
        raise ZarrExportError(f"写入 Zarr 失败: {type(e).__name__}: {e}") from e
    os.replace(work, dst) if not work.is_dir() else work.rename(dst)

    n_files = sum(1 for p in dst.rglob("*") if p.is_file())
    return {
        "skipped": False,
        "out": str(dst),
        "size_bytes": sum(p.stat().st_size for p in dst.rglob("*") if p.is_file()),
        "files": n_files,
        "dims": {str(k): int(v) for k, v in ds.sizes.items()},
        "vars": sorted(str(v) for v in ds.data_vars),
        "chunks": dict(plan.chunks),
        "target": plan.target,
        "shards": shards,
    }


def estimate_chunk_bytes(ds, plan: ChunkPlan) -> dict[str, int]:
    """预估每个变量的单 chunk 字节数（压缩前），用于在写入前暴露“巨块”风险。"""
    out = {}
    for name, var in ds.data_vars.items():
        if not var.dims:
            continue
        n = 1
        for d in var.dims:
            n *= int(min(plan.chunks[str(d)], var.shape[list(var.dims).index(d)]))
        out[str(name)] = n * int(np.dtype(var.dtype).itemsize)
    return out


__all__ = ["ChunkPlan", "TARGET_RULES", "TARGET_CHUNK_ELEMENTS", "ZarrExportError",
           "plan_chunks", "export_store", "estimate_chunk_bytes"]
