"""QC 规则（L2）：文件级内容检查 + 数据集级结构检查。

规则只基于画像结果，不重复读原始数据；所有结论都带证据，未知不推断。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from dbas_core.analysis.datasets import DatasetSpec, get_dataset_spec


@dataclass
class Finding:
    rule_id: str
    severity: str                    # error / warn / info
    level: str                       # file / variable / dataset
    target: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    needs_human: bool = False
    dataset_id: str | None = None
    volume_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        ev = dict(self.evidence)
        ev.setdefault("message", self.message)
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "level": self.level,
            "target": self.target,
            "evidence": ev,
            "needs_human": self.needs_human,
            "dataset_id": self.dataset_id,
            "volume_id": self.volume_id,
        }


def _is_q(var_name: str) -> bool:
    return var_name in ("q", "specific_humidity")


def _is_t(var_name: str) -> bool:
    return var_name in ("t", "temperature", "t2m")


def _is_wind(var_name: str) -> bool:
    return var_name in ("u", "v", "u10", "v10")


def check_file_profile(file_profile: dict[str, Any],
                       var_profiles: list[dict[str, Any]],
                       spec: DatasetSpec,
                       *, target: str, dataset_id: str,
                       volume_id: str | None = None) -> list[Finding]:
    """对单个文件的画像结果执行规则，返回发现列表。"""
    out: list[Finding] = []
    fp = file_profile

    size = fp.get("size_bytes")
    expected = fp.get("expected_bytes")
    if fp.get("format") == "nc3" and size and expected:
        ratio = size / expected
        if ratio < 0.98:
            msg = (f"文件物理大小 {size} 字节 < 按维度/类型推算的 {expected} 字节"
                   f"（{ratio:.1%}），疑似截断或尾部未写入")
            out.append(Finding(
                "R001_FILE_SIZE", "error", "file", target, msg,
                {"expected_bytes": expected, "size_bytes": size,
                 "ratio": round(ratio, 4)}, dataset_id=dataset_id))

    tails = [v["var_name"] for v in var_profiles if v.get("tail_constant")]
    if fp.get("tail_suspect") or tails:
        msg = ("末时次抽样场为常数而首时次不是，疑似尾部数据未写入："
               + ", ".join(tails or ["(未定位变量)"]))
        out.append(Finding(
            "R002_TAIL_CONSTANT", "error", "file", target, msg,
            {"variables": tails}, dataset_id=dataset_id))

    noninc = int(fp.get("time_non_increasing") or 0)
    tmin, tmax = fp.get("time_min"), fp.get("time_max")
    if noninc or (tmin and tmax and tmin > tmax):
        msg = (f"时间轴非单调（{noninc} 处倒退），范围 {tmin} .. {tmax}；"
               f"疑似未写入的时间记录被解码成 1900 或早期时间")
        out.append(Finding(
            "R006_TIME_AXIS", "error", "file", target, msg,
            {"time_non_increasing": noninc, "time_min": tmin,
             "time_max": tmax,
             "time_zero_count": fp.get("time_zero_count")},
            dataset_id=dataset_id))

    if spec.time_resolution and fp.get("time_n") is None:
        out.append(Finding(
            "R008_TIME_MISSING", "warn", "file", target,
            f"数据集定义为 {spec.time_resolution}，但文件未找到可解码的时间轴",
            {"time_n": fp.get("time_n")}, dataset_id=dataset_id))
    if fp.get("lat_sig") is None or fp.get("lon_sig") is None:
        out.append(Finding(
            "R008_SPATIAL_MISSING", "info", "file", target,
            "未找到一维/可识别经纬坐标，空间范围记为未知",
            {"lat_sig": fp.get("lat_sig"), "lon_sig": fp.get("lon_sig")},
            dataset_id=dataset_id))

    for vp in var_profiles:
        name = str(vp.get("var_name") or "")
        total = int(vp.get("n_total") or 0)
        masked = int(vp.get("n_masked") or 0)
        if total > 0 and masked > 0:
            ratio = masked / total
            if ratio >= 0.01:
                sev = "error" if ratio >= 0.5 else "warn"
                out.append(Finding(
                    "R003_FILL_RATIO", sev, "variable", target,
                    f"{name} 缺测/非法值 {masked}/{total}（{ratio:.1%}）",
                    {"variable": name, "masked": masked, "total": total,
                     "ratio": round(ratio, 4)}, dataset_id=dataset_id))

        vmin, vmax = vp.get("min_value"), vp.get("max_value")
        if vmin is not None and vmax is not None:
            if _is_t(name):
                if vmin < 150 or vmax > 350:
                    out.append(Finding(
                        "R004_RANGE", "warn", "variable", target,
                        f"{name} 温度范围 {vmin:.2f}..{vmax:.2f} K 超出常识区间",
                        {"variable": name, "min": vmin, "max": vmax},
                        needs_human=True, dataset_id=dataset_id))
            elif _is_q(name):
                if vmax > 0.06:
                    out.append(Finding(
                        "R004_RANGE", "warn", "variable", target,
                        f"{name} 比湿最大值 {vmax:.4g} kg/kg 偏高",
                        {"variable": name, "max": vmax},
                        needs_human=True, dataset_id=dataset_id))
            elif _is_wind(name):
                if max(abs(vmin), abs(vmax)) > 150:
                    out.append(Finding(
                        "R004_RANGE", "warn", "variable", target,
                        f"{name} 风速范围 {vmin:.1f}..{vmax:.1f} m/s 异常",
                        {"variable": name, "min": vmin, "max": vmax},
                        needs_human=True, dataset_id=dataset_id))

        neg = int(vp.get("negative_count") or 0)
        if vmin is not None and vmin <= -900 and neg > 0:
            out.append(Finding(
                "R009_SENTINEL", "warn", "variable", target,
                f"{name} 最小值 {vmin:.4g} 且有 {neg} 个负值，"
                f"疑似 -999 等缺测哨兵未声明 _FillValue",
                {"variable": name, "min": vmin, "negative_count": neg},
                needs_human=True, dataset_id=dataset_id))

        if _is_q(name) and neg > 0:
            out.append(Finding(
                "R005_NEGATIVE_Q", "warn", "variable", target,
                f"{name} 出现 {neg} 个负值（物理上应为非负）",
                {"variable": name, "negative_count": neg},
                needs_human=True, dataset_id=dataset_id))

        if vp.get("constant_field"):
            out.append(Finding(
                "R007_CONSTANT_FIELD", "warn", "variable", target,
                f"{name} 抽样场为常数，需确认是物理真实还是缺失/填充",
                {"variable": name}, needs_human=True,
                dataset_id=dataset_id))

    for f in out:
        f.volume_id = volume_id
    return out


def check_dataset(catalog, dataset_id: str) -> list[Finding]:
    """数据集级检查：轴一致性、文件名覆盖率、日期连续性、重复文件。"""
    spec = get_dataset_spec(dataset_id)
    target = f"dataset:{dataset_id}"
    out: list[Finding] = []

    rows = catalog.conn.execute(
        """SELECT lat_sig, lon_sig, level_sig FROM file_profiles
           WHERE dataset_id=?""", (dataset_id,)).fetchall()
    for idx, axis in enumerate(("lat_sig", "lon_sig", "level_sig")):
        sigs = Counter(r[idx] for r in rows if r[idx])
        if len(sigs) > 1:
            out.append(Finding(
                "R101_AXIS_MISMATCH", "warn", "dataset", target,
                f"数据集内 {axis} 存在 {len(sigs)} 种签名，网格/层轴可能不一致",
                {"axis": axis, "signatures": dict(sigs)},
                needs_human=True, dataset_id=dataset_id))

    if spec.filename_pattern and spec.variables:
        files = catalog.conn.execute(
            """SELECT file_name FROM files
               WHERE dataset_id=? AND status='active'""",
            (dataset_id,)).fetchall()
        date_vars: dict[str, set[str]] = defaultdict(set)
        pair_counts: Counter = Counter()
        for (name,) in files:
            d, var = spec.parse_filename(name)
            if d and var:
                date_vars[d].add(var)
                pair_counts[(d, var)] += 1

        missing_pairs: list[tuple[str, list[str]]] = []
        for d, have in sorted(date_vars.items()):
            miss = sorted(spec.variables - have)
            if miss:
                missing_pairs.append((d, miss))
        if missing_pairs:
            out.append(Finding(
                "R102_COVERAGE_VAR", "warn", "dataset", target,
                f"{len(missing_pairs)} 个日期缺少部分变量（示例："
                + "; ".join(f"{d}:{','.join(v)}" for d, v in missing_pairs[:5])
                + "）",
                {"missing": missing_pairs[:100], "count": len(missing_pairs)},
                needs_human=True, dataset_id=dataset_id))

        by_month: dict[str, set[int]] = defaultdict(set)
        for d in date_vars:
            by_month[d[:6]].add(int(d[6:8]))
        gaps: list[tuple[str, list[int]]] = []
        for ym, days in sorted(by_month.items()):
            missing = [d for d in range(min(days), max(days) + 1)
                       if d not in days]
            if missing:
                gaps.append((ym, missing))
        if gaps:
            out.append(Finding(
                "R103_DATE_GAP", "warn", "dataset", target,
                f"{len(gaps)} 个年月在观测区间内存在整日缺失（示例："
                + "; ".join(f"{ym}:{m[:5]}" for ym, m in gaps[:5]) + "）",
                {"gaps": gaps[:100], "count": len(gaps)},
                needs_human=True, dataset_id=dataset_id))

        dups = {f"{d}:{v}": n for (d, v), n in pair_counts.items() if n > 1}
        if dups:
            out.append(Finding(
                "R104_DUPLICATE", "info", "dataset", target,
                f"存在 {len(dups)} 个重复的日期+变量组合",
                {"duplicates": dict(list(dups.items())[:100])},
                dataset_id=dataset_id))

    elif spec.variables:
        have_rows = catalog.conn.execute(
            """SELECT DISTINCT v.name FROM variables v
               JOIN files f ON f.file_id=v.file_id
               WHERE f.dataset_id=? AND f.parse_status='parsed'""",
            (dataset_id,)).fetchall()
        have = {r[0] for r in have_rows}
        if have:
            missing = sorted(spec.variables - have)
            if missing:
                out.append(Finding(
                    "R105_VARIABLE_MISSING", "warn", "dataset", target,
                    f"数据集定义变量中未见：{', '.join(missing)}",
                    {"missing": missing, "observed": sorted(have)},
                    needs_human=True, dataset_id=dataset_id))

    return out


def run_dataset_checks(catalog, dataset_ids: list[str]) -> None:
    for dataset_id in dataset_ids:
        target = f"dataset:{dataset_id}"
        findings = check_dataset(catalog, dataset_id)
        catalog.replace_quality_reports(
            target, [f.to_dict() for f in findings])


__all__ = ["Finding", "check_file_profile", "check_dataset",
           "run_dataset_checks"]
