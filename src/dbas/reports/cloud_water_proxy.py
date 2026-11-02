"""Auditable reporting helpers for the Tibetan Plateau proxy experiment."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import json

from dbas.analysis.cloud_water_proxy import formula_audit


def availability_report(variables: Mapping[str, Any]) -> dict[str, Any]:
    audit = formula_audit(variables)
    return {"variables": sorted(variables), "formulas": audit,
            "strict_gcwr": {"status": audit["GCWR"]["status"],
                            "message": "需要 clwc/ciwc、降水、蒸发和边界通量；当前数据不足"}}


def write_proxy_report(path: str | Path, *, metadata: Mapping[str, Any],
                       variables: Mapping[str, Any], metrics: Mapping[str, Any] | None = None,
                       caveats: list[str] | None = None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    report = availability_report(variables)
    report.update({"metadata": dict(metadata), "metrics": dict(metrics or {}),
                   "caveats": list(caveats or [])})
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)
    return p


def markdown_summary(report: Mapping[str, Any]) -> str:
    lines = ["# 青藏高原 ERA5 云水资源代理试验审计", "",
             "## 结论", "- 代理试验可在现有 t/q 数据上运行。",
             "- 严格 GCWR 当前为 INSUFFICIENT_EVIDENCE，不以 PW/RH 代理替代。", "",
             "## 公式可用性", "| 指标 | 状态 | 缺失变量 |", "|---|---|---|"]
    for name, item in report.get("formulas", {}).items():
        lines.append(f"| {name} | {item['status']} | {', '.join(item['missing']) or '—'} |")
    lines += ["", "## 限制"] + [f"- {x}" for x in report.get("caveats", [])]
    return "\n".join(lines) + "\n"
