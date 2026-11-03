"""Offline analysis of DBAS/mitmproxy capture metadata.

只读分析抓包产生的事件流，输出 Markdown 报告与 summary.json。
不发起网络请求，也不改动任何抓包数据。
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

CLOUD_WATER = {
    "clwc", "ciwc", "total_cloud_cover", "cloud_cover", "tp",
    "precipitation", "evaporation", "boundary_layer_dissipation",
}

# CDS 目录全名 → 短名（GRIB 参数名）别名；请求里记的是全名，覆盖表按短名核对。
CLOUD_WATER_ALIASES = {
    "specific_cloud_liquid_water_content": "clwc",
    "specific_cloud_ice_water_content": "ciwc",
    "total_precipitation": "tp",
}

#: 视为"请求字段"的键（小写比较）
_FIELD_KEYS = {
    "dataset", "variable", "variables", "year", "month", "day", "time",
    "area", "pressure_level", "product_type", "data_format",
}


def load_events(root: Path) -> list[dict]:
    path = root / "events.jsonl" if root.is_dir() else root
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _fields(event: dict) -> dict:
    fields = event.get("fields") or event.get("request") or {}
    if isinstance(fields, dict):
        return {str(k).lower(): v for k, v in fields.items()
                if str(k).lower() in _FIELD_KEYS}
    return {}


def summarize(root: str | Path) -> dict:
    root = Path(root)
    events = load_events(root)
    requests = [e for e in events
                if e.get("event") in {"request", "application_request"}]
    responses = [e for e in events
                 if e.get("event") in {"response", "application_response"}]

    statuses = Counter(str(e.get("status")) for e in responses)
    datasets: Counter = Counter()
    variables: Counter = Counter()
    templates: dict = defaultdict(int)
    sizes: list = []
    durations: list = []

    for e in requests:
        f = _fields(e)
        dataset = f.get("dataset", e.get("dataset", "unknown"))
        datasets[str(dataset)] += 1
        vals = f.get("variable", f.get("variables", []))
        vals = vals if isinstance(vals, list) else [vals]
        variables.update(str(v) for v in vals)
        templates[(str(dataset), e.get("method", ""), e.get("path", ""),
                   tuple(sorted(f)))] += 1
        if isinstance(e.get("duration_ms"), (int, float)):
            durations.append(e["duration_ms"])

    for e in responses:
        body = e.get("body") or {}
        n = body.get("bytes", e.get("target_bytes"))
        if isinstance(n, (int, float)):
            sizes.append(n)

    failures = [e for e in responses
                if isinstance(e.get("status"), int) and e["status"] >= 400]
    failures += [e for e in events if e.get("event") == "error"]

    captured = {CLOUD_WATER_ALIASES.get(v, v) for v in variables}
    cloud = {
        name: {
            "captured": name in captured,
            "evidence": "proxy HTTP events" if name in captured
                        else "INSUFFICIENT_EVIDENCE",
        }
        for name in sorted(CLOUD_WATER)
    }

    return {
        "evidence": {
            "proxy_http": "PASS" if requests or responses
                          else "INSUFFICIENT_EVIDENCE",
            "application": "INSUFFICIENT_EVIDENCE",
            "pcap": "INSUFFICIENT_EVIDENCE",
        },
        "events": len(events),
        "requests": len(requests),
        "responses": len(responses),
        "status_counts": dict(statuses),
        "datasets": dict(datasets),
        "variables": dict(variables),
        "failed_events": len(failures),
        "failure_statuses": dict(Counter(
            str(e.get("status", e.get("error", "unknown"))) for e in failures)),
        "response_bytes": sum(sizes),
        "response_count_with_size": len(sizes),
        "duration_ms": {
            "count": len(durations),
            "mean": statistics.mean(durations) if durations else None,
            "max": max(durations) if durations else None,
        },
        "cloud_water_coverage": cloud,
        "templates": [
            {"dataset": d, "method": m, "path": p,
             "fields": list(fields), "count": n}
            for (d, m, p, fields), n in sorted(templates.items())
        ],
    }


def render_markdown(summary: dict) -> str:
    ev = summary["evidence"]
    lines = [
        "# CDS Capture Report",
        "",
        f"- Evidence: proxy={ev['proxy_http']}, "
        f"application={ev['application']}, pcap={ev['pcap']}",
        f"- Events: {summary['events']}; requests: {summary['requests']}; "
        f"responses: {summary['responses']}",
        f"- Response bytes: {summary['response_bytes']}",
        "",
        "## Status",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]
    lines += [f"| {k} | {v} |" for k, v in sorted(summary["status_counts"].items())]

    lines += ["", "## Request templates", "",
              "| Dataset | Method | Path | Fields | Count |",
              "|---|---|---|---|---:|"]
    lines += [
        f"| {x['dataset']} | {x['method']} | {x['path']} | "
        f"{', '.join(x['fields'])} | {x['count']} |"
        for x in summary["templates"]
    ]

    lines += ["", "## Cloud-water coverage", "",
              "| Candidate | Captured | Evidence |", "|---|---|---|"]
    lines += [
        f"| {k} | {v['captured']} | {v['evidence']} |"
        for k, v in summary["cloud_water_coverage"].items()
    ]
    return "\n".join(lines) + "\n"


def write_report(capture_dir: str | Path,
                 output: str | Path) -> tuple[Path, Path]:
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(capture_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    md = out / f"cds_capture_{stamp}.md"
    js = out / "summary.json"
    md.write_text(render_markdown(summary), encoding="utf-8")
    js.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    return md, js


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("capture_dir")
    p.add_argument("--output", default="data/reports")
    a = p.parse_args(argv)
    md, js = write_report(a.capture_dir, a.output)
    print(json.dumps({"report": str(md), "summary": str(js)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
