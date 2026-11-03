"""轻量、可审计的实验运行清单。

清单只记录配置、环境和输入逻辑指纹；不复制原始数据，也不对 2TB 卷做
隐式全量哈希。精确比特级完整性仍由 ``dbas checksum`` 负责。
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


def _json_default(value: Any) -> str:
    return str(value)


def canonical_json(value: Any) -> str:
    """以稳定顺序序列化 JSON，供哈希和清单保存使用。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=_json_default)


def config_hash(config: Mapping[str, Any]) -> str:
    """返回配置内容的 SHA-256 前 16 位。"""
    return hashlib.sha256(canonical_json(dict(config)).encode("utf-8")).hexdigest()[:16]


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件 SHA-256，内存占用与文件大小无关。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def input_fingerprint(rows: Iterable[Mapping[str, Any]]) -> str:
    """对 catalog 输入记录做稳定的逻辑指纹。

    使用卷 ID、相对路径、大小和 mtime；排序后再哈希，因此 SQL 返回顺序不会
    影响结果。调用方若需要防范静默内容损坏，应另行运行 ``dbas checksum``。
    """
    normalized = []
    for row in rows:
        normalized.append({
            "volume_id": str(row.get("volume_id", "")),
            "rel_path": str(row.get("rel_path", "")),
            "size_bytes": int(row.get("size_bytes", 0) or 0),
            "mtime": float(row.get("mtime", 0) or 0),
        })
    normalized.sort(key=lambda item: (item["volume_id"], item["rel_path"]))
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()[:16]


def git_revision(root: str | Path) -> str:
    """读取当前 Git revision；非 Git 目录返回 ``unknown``。"""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(root), capture_output=True,
            text=True, check=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def package_versions(names: Iterable[str]) -> dict[str, str]:
    """读取指定包的已安装版本，不因可选包缺失而失败。"""
    versions: dict[str, str] = {}
    for name in sorted(set(names)):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_text(path: str | Path, text: str) -> Path:
    """同目录临时文件 + 原子改名，避免消费者看到半截清单。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part",
                                    dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def atomic_write_json(path: str | Path, data: Mapping[str, Any]) -> Path:
    return atomic_write_text(
        path, json.dumps(dict(data), ensure_ascii=False, indent=2, sort_keys=True,
                         default=_json_default) + "\n")


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"运行清单必须是对象: {path}")
    return value


def new_manifest(*, project_root: str | Path, config: Mapping[str, Any],
                 input_fingerprint_value: str, run_id: str,
                 packages: Iterable[str] = ()) -> dict[str, Any]:
    """构造一个可序列化的运行清单。"""
    root = Path(project_root)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "planned",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "config_hash": config_hash(config),
        "config": dict(config),
        "input_fingerprint": input_fingerprint_value,
        "git_revision": git_revision(root),
        "python": sys.version,
        "platform": platform.platform(),
        "packages": package_versions(packages),
        "completed_years": [],
        "failed_dates": [],
        "artifacts": [],
    }


def update_manifest(path: str | Path, **updates: Any) -> dict[str, Any]:
    """合并更新清单并原子保存，返回更新后的对象。"""
    target = Path(path)
    current = load_json(target) if target.exists() else {}
    current.update(updates)
    current["updated_at"] = utc_now()
    atomic_write_json(target, current)
    return current
