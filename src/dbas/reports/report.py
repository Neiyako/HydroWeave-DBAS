"""报告生成：Markdown 报告 + 原子写入（tmp 文件 + os.replace）。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from dbas.catalog.repository import Catalog
from dbas.core.logging import get_logger

log = get_logger("reports")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)                # 半截文件不会被误认为完整报告


def inventory_report(catalog: Catalog, out_dir: Path) -> Path:
    """数据目录概览报告。只汇总 catalog 已有信息，不触碰原始文件。"""
    lines = ["# DBAS 数据目录报告", "",
             f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    summary = catalog.status_summary()
    lines.append(f"- schema 版本: {summary['schema_version']}")
    lines.append("")
    lines.append("## 数据卷")
    lines.append("")
    lines.append("| 卷 ID | 标签 | 状态 | 最近挂载路径 | 最近在线 |")
    lines.append("|---|---|---|---|---|")
    for v in summary["volumes"]:
        lines.append(f"| {v['volume_id']} | {v['label'] or ''} | {v['status']} "
                     f"| {v['last_mount_path'] or ''} | {v['last_seen'] or ''} |")
    lines.append("")
    lines.append("## 文件状态分布")
    lines.append("")
    for k, v in sorted(summary["files_by_status"].items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 元数据解析状态")
    lines.append("")
    for k, v in sorted(summary["files_by_parse"].items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 任务队列")
    lines.append("")
    for k, v in sorted(summary["tasks_by_status"].items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 最近扫描运行")
    lines.append("")
    for r in summary["recent_scan_runs"]:
        lines.append(f"- {r['run_id']} {r['mode']} {r['status']} "
                     f"seen={r['files_seen']} ok={r['files_ok']} failed={r['files_failed']}")
    out = out_dir / f"inventory_{time.strftime('%Y%m%d_%H%M%S')}.md"
    atomic_write_text(out, "\n".join(lines) + "\n")
    log.info("报告已生成: %s", out)
    return out
