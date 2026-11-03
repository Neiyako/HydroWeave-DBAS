"""Zarr store 的内容清单与清单哈希。

为什么需要这一层：Zarr 把一个“数据集”摊成成百上千个小文件（chunk），
它是对数组友好、对“比特级证据”最不友好的存储。DBAS 的既有契约是
“每个资产都有 sha256 指纹”，Zarr store 若照搬逐文件 sha256，指纹会退化成
上千条噪声记录，且无法回答“这个 store 相比上次构建是否变了”。

本模块用**清单哈希**（manifest digest）填补这个缺口：

    digest = sha256( 每一行 "相对路径\tsize\tmtime_ns\tsha256\n" 的顺序拼接 )

- 逐文件 sha256 保留，因此仍能定位到底是哪个 chunk 变了；
- 汇总成一个 digest 写进 files.fingerprint，沿用 DBAS 现有指纹语义；
- 行先按相对路径排序，因此与文件系统遍历顺序无关（可复现）。

只读：本模块从不写 store。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

# chunk 通常很小；给 1MB 缓冲足够且内存恒定
_CHUNK = 1 << 20


@dataclass
class ManifestEntry:
    rel_path: str
    size_bytes: int
    mtime_ns: int
    sha256: str

    def as_line(self) -> str:
        return f"{self.rel_path}\t{self.size_bytes}\t{self.mtime_ns}\t{self.sha256}\n"


@dataclass
class StoreManifest:
    root: Path
    entries: list[ManifestEntry] = field(default_factory=list)
    n_arrays: int = 0
    n_chunks: int = 0
    total_bytes: int = 0

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        for e in sorted(self.entries, key=lambda x: x.rel_path):
            h.update(e.as_line().encode("utf-8"))
        return h.hexdigest()

    def summary(self) -> dict:
        return {
            "root": str(self.root),
            "files": len(self.entries),
            "arrays": self.n_arrays,
            "chunks": self.n_chunks,
            "total_bytes": self.total_bytes,
            "manifest_digest": self.digest,
        }

    def to_json(self) -> str:
        return json.dumps(
            {"summary": self.summary(),
             "entries": [e.__dict__ for e in
                         sorted(self.entries, key=lambda x: x.rel_path)]},
            ensure_ascii=False, indent=1, sort_keys=True)


def _is_chunk(rel: str) -> bool:
    """zarr v3：<array>/c/<索引...>；v2：<array>/<索引...>（不含 . 前缀的纯数字串）。"""
    parts = rel.split("/")
    if len(parts) >= 2 and "c" in parts:
        return True
    last = parts[-1]
    if not last or last.startswith("."):
        return False
    return all(p.isdigit() for p in parts[1:]) and len(parts) >= 2


def scan_store(root: Path, *, with_sha256: bool = True) -> StoreManifest:
    """遍历 store，生成清单。with_sha256=False 时只记录 size/mtime（快速对账）。"""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"store 目录不存在: {root}")
    man = StoreManifest(root=root)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        st = p.stat()
        digest = ""
        if with_sha256:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                while True:
                    b = f.read(_CHUNK)
                    if not b:
                        break
                    h.update(b)
            digest = h.hexdigest()
        man.entries.append(ManifestEntry(rel, st.st_size, st.st_mtime_ns, digest))
        man.total_bytes += st.st_size
        if rel.endswith("zarr.json") or rel.endswith(".zarray"):
            man.n_arrays += 1
        elif _is_chunk(rel):
            man.n_chunks += 1
    return man


def compare_manifests(old: StoreManifest, new: StoreManifest) -> dict:
    """对比两次清单：新增/删除/变更的文件。用于回答“store 是否被悄悄改过”。"""
    o = {e.rel_path: e for e in old.entries}
    n = {e.rel_path: e for e in new.entries}
    added = sorted(set(n) - set(o))
    removed = sorted(set(o) - set(n))
    changed = sorted(
        k for k in set(o) & set(n)
        if o[k].sha256 != n[k].sha256 or o[k].size_bytes != n[k].size_bytes)
    return {
        "added": added, "removed": removed, "changed": changed,
        "added_count": len(added), "removed_count": len(removed),
        "changed_count": len(changed),
        "identical": not (added or removed or changed),
        "old_digest": old.digest, "new_digest": new.digest,
    }


__all__ = ["ManifestEntry", "StoreManifest", "scan_store", "compare_manifests"]
