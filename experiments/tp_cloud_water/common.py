"""云水代理实验的配置、输入索引和运行目录共用代码。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from dbas.repro.manifest import (
    atomic_write_json,
    config_hash,
    file_sha256,
    input_fingerprint,
    load_json,
    new_manifest,
    package_versions,
    update_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACKAGES = ("dbas-core", "dask", "netCDF4", "numpy", "pandas", "xarray")


def load_config(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    if not source.is_absolute():
        source = ROOT / source
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"实验配置必须是映射: {source}")
    raw["_config_path"] = str(source)
    return raw


def path_from_config(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def run_dir(config: dict[str, Any], run_id: str) -> Path:
    output = path_from_config(config["output_dir"])
    target = output / "runs" / run_id
    for name in ("yearly", "tables", "figures", "summary"):
        (target / name).mkdir(parents=True, exist_ok=True)
    return target


def catalog_index(config: dict[str, Any]) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], Path]:
    """从 DBAS catalog 构造日期到 q/t 文件的索引及输入记录。"""
    db_path = path_from_config(config["catalog"])
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    volume_id = str(config["volume_id"])
    dataset_id = str(config["dataset_id"])
    volume = db.execute(
        "SELECT last_mount_path FROM volumes WHERE volume_id=?", (volume_id,)
    ).fetchone()
    if volume is None or not volume[0]:
        db.close()
        raise ValueError(f"catalog 中没有在线卷: {volume_id}")
    rows = [dict(row) for row in db.execute(
        "SELECT rel_path, size_bytes, mtime, volume_id FROM files "
        "WHERE dataset_id=? AND volume_id=? AND status='active'",
        (dataset_id, volume_id),
    ).fetchall()]
    db.close()
    root = Path(volume[0])
    filevar = {"specific_humidity": "q", "temperature": "t"}
    index: dict[str, dict[str, str]] = {}
    selected: list[dict[str, Any]] = []
    import re
    for row in rows:
        match = re.match(r"^ERA5_025dg_(\d{8})_(\w+)\.nc$", Path(row["rel_path"]).name)
        if not match or match.group(2) not in filevar:
            continue
        date, variable = match.group(1), filevar[match.group(2)]
        index.setdefault(date, {})[variable] = str(root / row["rel_path"])
        selected.append(row)
    return index, selected, root


def _clean(config: dict[str, Any]) -> dict[str, Any]:
    """去掉以 ``_`` 开头的运行时注记键，保证哈希只反映用户配置。"""
    return {key: value for key, value in config.items() if not key.startswith("_")}


def prepare_manifest(config: dict[str, Any], run_id: str, rows: list[dict[str, Any]],
                     *, force: bool = False) -> tuple[Path, dict[str, Any]]:
    target = run_dir(config, run_id) / "manifest.json"
    fp = input_fingerprint(rows)
    if target.exists() and not force:
        manifest = load_json(target)
        if manifest.get("config_hash") != config_hash(_clean(config)) or \
                manifest.get("input_fingerprint") != fp:
            raise ValueError("已有运行清单与当前配置或输入不一致；换 --run-id 或显式 --force")
        return target, manifest
    manifest = new_manifest(
        project_root=ROOT,
        config=_clean(config),
        input_fingerprint_value=fp,
        run_id=run_id,
        packages=DEFAULT_PACKAGES,
    )
    manifest["status"] = "running"
    atomic_write_json(target, manifest)
    return target, manifest


def record_artifact(manifest_path: Path, manifest: dict[str, Any], path: Path) -> dict[str, Any]:
    rel = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    artifacts = [item for item in manifest.get("artifacts", []) if item.get("path") != rel]
    artifacts.append({"path": rel, "sha256": file_sha256(path), "size_bytes": path.stat().st_size})
    return update_manifest(manifest_path, artifacts=artifacts)


def mark_year(manifest_path: Path, manifest: dict[str, Any], year: int) -> dict[str, Any]:
    years = sorted(set(int(item) for item in manifest.get("completed_years", [])) | {int(year)})
    return update_manifest(manifest_path, completed_years=years, status="running")


def finish_manifest(manifest_path: Path, manifest: dict[str, Any], years: list[int],
                    problems: list[dict[str, Any]]) -> dict[str, Any]:
    return update_manifest(
        manifest_path,
        completed_years=sorted(set(int(item) for item in manifest.get("completed_years", [])) | set(years)),
        failed_dates=problems,
        status="completed" if not problems else "completed_with_warnings",
    )
