"""内容画像 + QC 报告（只读 catalog，不读取原始数据）。"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from dbas.analysis.datasets import get_dataset_spec
from dbas.catalog.repository import Catalog
from dbas.reports.report import atomic_write_text

_SEV_ORDER = {"error": 0, "warn": 1, "info": 2}


def _fmt_bytes(n: Any) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def _dataset_stats(catalog: Catalog, dataset_id: str,
                   volume_id: str | None = None) -> dict[str, Any]:
    sql = """SELECT COUNT(DISTINCT f.file_id) AS files,
                    SUM(CASE WHEN f.parse_status='parsed' THEN 1 ELSE 0 END)
                      AS parsed,
                    COUNT(DISTINCT p.file_id) AS profiled,
                    COALESCE(SUM(CASE WHEN lower(f.rel_path) LIKE '%.nc'
                                        OR lower(f.rel_path) LIKE '%.nc4'
                                        OR lower(f.rel_path) LIKE '%.cdf'
                                      THEN 1 ELSE 0 END), 0) AS nc_files,
                    MIN(COALESCE(f.time_min, p.time_min)) AS time_min,
                    MAX(COALESCE(f.time_max, p.time_max)) AS time_max,
                    COALESCE(SUM(f.size_bytes), 0) AS size_bytes
             FROM files f
             LEFT JOIN file_profiles p ON p.file_id=f.file_id
             WHERE f.dataset_id=? AND f.status='active'"""
    args: list[Any] = [dataset_id]
    if volume_id:
        sql += " AND f.volume_id=?"
        args.append(volume_id)
    row = catalog.conn.execute(sql, args).fetchone()
    return dict(row) if row else {}


def _var_rows(catalog: Catalog, dataset_id: str,
              volume_id: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT vp.var_name, vp.units, COUNT(*) AS files,
                    SUM(vp.n_samples) AS samples,
                    SUM(vp.n_masked) AS masked,
                    MIN(vp.min_value) AS vmin, MAX(vp.max_value) AS vmax,
                    SUM(vp.mean_value * vp.n_samples)
                      / NULLIF(SUM(vp.n_samples), 0) AS vmean,
                    SUM(vp.negative_count) AS negatives,
                    SUM(vp.constant_field) AS constant_fields,
                    SUM(vp.tail_constant) AS tail_constants,
                    MIN(vp.scale_factor) AS scale_min,
                    MAX(vp.scale_factor) AS scale_max
             FROM variable_profiles vp
             JOIN files f ON f.file_id=vp.file_id
             WHERE f.dataset_id=? AND f.status='active'"""
    args: list[Any] = [dataset_id]
    if volume_id:
        sql += " AND f.volume_id=?"
        args.append(volume_id)
    sql += " GROUP BY vp.var_name, vp.units ORDER BY vp.var_name"
    return [dict(r) for r in catalog.conn.execute(sql, args).fetchall()]


def _coverage_rows(catalog: Catalog, dataset_id: str,
                   volume_id: str | None = None):
    spec = get_dataset_spec(dataset_id)
    if not spec.filename_pattern:
        return None
    sql = """SELECT file_name FROM files
             WHERE dataset_id=? AND status='active'"""
    args: list[Any] = [dataset_id]
    if volume_id:
        sql += " AND volume_id=?"
        args.append(volume_id)
    names = [r[0] for r in catalog.conn.execute(sql, args).fetchall()]
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    dates: dict[str, set[str]] = defaultdict(set)
    for name in names:
        d, var = spec.parse_filename(name)
        if d and var:
            ym = f"{d[:4]}-{d[4:6]}"
            counts[ym][var] += 1
            dates[ym].add(d)
    columns = sorted(spec.variables)
    rows = []
    for ym in sorted(counts):
        row = {"ym": ym, "days": len(dates[ym])}
        for col in columns:
            row[col] = counts[ym].get(col, 0)
        rows.append(row)
    return columns, rows


def _findings(catalog: Catalog, dataset_ids: list[str],
              volume_id: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ds in dataset_ids:
        rows.extend(catalog.list_quality_reports(
            dataset_id=ds, volume_id=volume_id, limit=1000))
    rows.sort(key=lambda r: (_SEV_ORDER.get(r["severity"], 9),
                             r["rule_id"], r["target"]))
    return rows


def profile_qc_report(catalog: Catalog, out_dir: Path, *,
                      volume_id: str | None = None,
                      dataset_id: str | None = None) -> Path:
    if dataset_id:
        dataset_ids = [dataset_id]
    elif volume_id:
        dataset_ids = catalog.dataset_ids_for_volume(volume_id)
    else:
        dataset_ids = [d["dataset_id"] for d in catalog.list_datasets()
                       if d.get("nc_files")]

    stats = {ds: _dataset_stats(catalog, ds, volume_id) for ds in dataset_ids}
    stats = {ds: s for ds, s in stats.items()
             if (s.get("nc_files") or s.get("profiled")) or ds == dataset_id}
    dataset_ids = [ds for ds in dataset_ids if ds in stats]
    total_profiled = sum(int(s.get("profiled") or 0) for s in stats.values())
    findings = _findings(catalog, dataset_ids, volume_id)
    sev_counts = defaultdict(int)
    for f in findings:
        sev_counts[f["severity"]] += 1

    lines = ["# DBAS 内容画像与 QC 报告", "",
             f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    if volume_id:
        lines.append(f"- 数据卷: {volume_id}")
    if dataset_id:
        lines.append(f"- 数据集: {dataset_id}")
    lines.append(f"- 数据集数: {len(dataset_ids)}")
    lines.append(f"- 已画像文件: {total_profiled}")
    lines.append("- QC 发现: " + (", ".join(
        f"{k}={sev_counts.get(k, 0)}" for k in ("error", "warn", "info"))
        or "无"))
    lines.append("")
    lines.append("> 画像为抽样统计（首/末时次 × 空间跨步），不是全量逐格统计；"
                 "文件级覆盖率基于文件名规则，未画像文件不参与内容统计。")
    lines.append("")

    lines.append("## 数据集")
    lines.append("")
    lines.append("| 数据集 | 标签 | 文件 | 已解析 | 已画像 | 时间范围 | 体积 |")
    lines.append("|---|---|---|---|---|---|---|")
    for ds in dataset_ids:
        s = stats[ds]
        label = get_dataset_spec(ds).label
        tr = f"{s.get('time_min') or '?'} .. {s.get('time_max') or '?'}"
        lines.append(f"| {ds} | {label} | {s.get('files') or 0} | "
                     f"{s.get('parsed') or 0} | {s.get('profiled') or 0} | "
                     f"{tr} | {_fmt_bytes(s.get('size_bytes'))} |")
    lines.append("")

    lines.append("## 变量内容画像")
    lines.append("")
    lines.append("| 数据集 | 变量 | 单位 | 文件 | 样本数 | 缺测率 | 最小值 | 最大值 | 加权均值 | 负值 | 常数场 | 尾部常数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    any_var = False
    for ds in dataset_ids:
        for r in _var_rows(catalog, ds, volume_id):
            any_var = True
            total = (r["samples"] or 0) + (r["masked"] or 0)
            fill = (r["masked"] or 0) / total if total else 0
            def num(v, fmt):
                return fmt.format(v) if v is not None else "?"
            lines.append(
                f"| {ds} | {r['var_name']} | {r['units'] or ''} | "
                f"{r['files']} | {r['samples'] or 0} | {fill:.2%} | "
                f"{num(r['vmin'], '{:.6g}')} | {num(r['vmax'], '{:.6g}')} | "
                f"{num(r['vmean'], '{:.6g}')} | {r['negatives'] or 0} | "
                f"{r['constant_fields'] or 0} | {r['tail_constants'] or 0} |")
    if not any_var:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.append("")

    lines.append("## 时间覆盖（文件名级）")
    lines.append("")
    any_cov = False
    for ds in dataset_ids:
        cov = _coverage_rows(catalog, ds, volume_id)
        if cov is None:
            continue
        columns, rows = cov
        if not rows:
            continue
        any_cov = True
        lines.append(f"### {ds}")
        lines.append("")
        lines.append("| 年-月 | 日期数 | " + " | ".join(columns) + " |")
        lines.append("|---" * (len(columns) + 2) + "|")
        for row in rows:
            lines.append(f"| {row['ym']} | {row['days']} | "
                         + " | ".join(str(row[c]) for c in columns) + " |")
        lines.append("")
    if not any_cov:
        lines.append("（该数据集没有可用的文件名日期-变量规则，或尚未画像）")
        lines.append("")

    lines.append("## QC 发现")
    lines.append("")
    if not findings:
        lines.append("未发现规则命中。")
    else:
        lines.append(f"- error: {sev_counts.get('error', 0)}")
        lines.append(f"- warn: {sev_counts.get('warn', 0)}")
        lines.append(f"- info: {sev_counts.get('info', 0)}")
        lines.append("")
        lines.append("| 级别 | 规则 | 对象 | 说明 | 需人工 |")
        lines.append("|---|---|---|---|---|")
        for f in findings[:200]:
            try:
                ev = json.loads(f["evidence_json"] or "{}")
            except json.JSONDecodeError:
                ev = {}
            msg = str(ev.get("message", "")).replace("|", "\\|")
            target = str(f["target"]).replace("|", "\\|")
            lines.append(f"| {f['severity']} | {f['rule_id']} | {target} | "
                         f"{msg} | {'是' if f['needs_human'] else ''} |")
        if len(findings) > 200:
            lines.append(f"| ... | ... | ... | 另有 {len(findings) - 200} 条，"
                         f"请查看 quality_reports 表 | ... |")
    lines.append("")

    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000:06d}"
    out = out_dir / f"profile_qc_{stamp}.md"
    atomic_write_text(out, "\n".join(lines) + "\n")
    return out


__all__ = ["profile_qc_report"]
