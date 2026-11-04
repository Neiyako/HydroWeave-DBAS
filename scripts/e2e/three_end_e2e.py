#!/usr/bin/env python3
"""三端端到端（e2e）编排：ResearchDataHub → DBAS → Hydroweave。

链路（每一步都有可复核的证据，不是“退出码 0”就算过）：

    ① RDH 采集：plan create → task run → report generate（真实 CDS 排队/下载/校验）
       产出  ~/.rdh/deliveries/<id>.json（逐文件 SHA-256）+ raw 文件
    ② DBAS 导入：dbas fetch rdh 读取交付清单 → SHA-256 复核 → 原子拷入
       data/downloads/<dataset>/ → catalog 登记 + scan_metadata 入队 + 脱敏事件
    ③ DBAS 解析：dbas scan --mode metadata 把 clwc/ciwc 头部/变量写进 catalog
    ④ Hydroweave 计算：读取 catalog 里的 clwc/ciwc → LWP/IWP/恒等式自检 → 图件

校验链（本脚本强制，任一不等即 FAIL）：
    RDH 交付清单 sha256 == DBAS 落盘文件 sha256 == Hydroweave inputs.json 里的 sha256

用法（在 DBAS 项目根）：

    ./bin/python scripts/e2e/three_end_e2e.py               # 全链路
    ./bin/python scripts/e2e/three_end_e2e.py --dry-run     # 只打印将执行的命令

产物：data/reports/e2e/<run-id>/{e2e.json,e2e.md}
退出码：0 = 全链路 PASS；1 = 有步骤 FAIL（报告仍会写出，失败项如实记录）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HW_REPO = Path("/Users/apple/Desktop/Hydroweave")
DEFAULT_RDH_STORAGE = Path.home() / ".rdh"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _last_json_object(text: str) -> dict | None:
    """从 CLI stdout 中取最后一个整行 JSON 对象（dbas fetch rdh 的 summary）。"""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


class Step:
    def __init__(self, name: str, status: str = "NOT_RUN", detail: str = "",
                 seconds: float = 0.0, evidence: dict | None = None) -> None:
        self.name = name
        self.status = status
        self.detail = detail
        self.seconds = seconds
        self.evidence = evidence or {}

    def as_dict(self) -> dict:
        return {
            "step": self.name,
            "status": self.status,
            "detail": self.detail,
            "seconds": round(self.seconds, 3),
            "evidence": self.evidence,
        }


class E2E:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        # 启动器 bin/dbas 会自己带 PYTHONPATH=src（src 布局，python -m dbas 直接跑不到）
        self.dbas_cli = str(PROJECT_ROOT / "bin" / "dbas")
        self.hw_repo = Path(args.hw_repo).expanduser().resolve()
        self.hw_python = str(self.hw_repo / ".venv" / "bin" / "python")
        self.rdh_config = (PROJECT_ROOT / args.rdh_config).resolve()
        self.hw_config = (self.hw_repo / args.hw_config).resolve()
        self.run_id = args.run_id
        self.report_dir = (PROJECT_ROOT / args.report_dir).resolve() / self.run_id
        self.steps: list[Step] = []
        self.delivery: dict = {}
        self.import_summary: dict = {}
        self.hw_run_dir = self.hw_repo / "data" / "analysis" / "cloud_water_strict" / "runs" / self.run_id
        self.env = dict(os.environ)

    # ---------------------------------------------------------------- 执行器
    def _run(self, argv: list[str], *, cwd: Path, env: dict | None = None,
             timeout: float = 7200.0) -> tuple[int, str, str, float]:
        started = time.perf_counter()
        proc = subprocess.run(argv, cwd=str(cwd), env=env, text=True,
                              capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr, time.perf_counter() - started

    def _step(self, name: str, status: str, detail: str, seconds: float,
              evidence: dict | None = None) -> Step:
        step = Step(name, status, detail, seconds, evidence)
        self.steps.append(step)
        print(f"[{status:>4}] {name}  {detail}")
        return step

    # ------------------------------------------------------------------ 步骤
    def step_acquire_and_import(self) -> Step:
        """① RDH 采集 + ② DBAS 导入，一条 CLI 命令（子进程内完成 plan/task/report/import）。"""
        argv = [self.dbas_cli, "fetch", "rdh", "--config", str(self.rdh_config)]
        if self.args.capture_dir:
            argv += ["--capture-dir", str(Path(self.args.capture_dir).expanduser())]
        if self.args.plan_id:
            argv += ["--plan-id", self.args.plan_id]
        rc, out, err, seconds = self._run(argv, cwd=PROJECT_ROOT)
        summary = _last_json_object(out)
        if rc != 0 or not summary or summary.get("dry_run"):
            return self._step("①+② RDH 采集 + DBAS 导入", "FAIL",
                              f"exit={rc}; stderr={_tail(err)}", seconds)
        self.import_summary = summary
        plan_id = summary.get("plan_id")
        delivery_name = summary.get("delivery")

        # 从 RDH 交付清单读 sha256，作为整条校验链的起点
        delivery_path = self._locate_delivery(delivery_name, summary.get("request_id"))
        if delivery_path is None:
            return self._step("①+② RDH 采集 + DBAS 导入", "FAIL",
                              f"找不到交付清单（delivery={delivery_name}）", seconds,
                              {"import": summary})
        payload = json.loads(delivery_path.read_text(encoding="utf-8"))
        delivery_files = [entry for entry in payload.get("files", []) if isinstance(entry, dict)]
        if not delivery_files:
            return self._step("①+② RDH 采集 + DBAS 导入", "FAIL",
                              "交付清单 files 为空", seconds)
        self.delivery = {
            "path": str(delivery_path),
            "delivery_id": payload.get("delivery_id"),
            "request_id": summary.get("request_id"),
            "plan_id": plan_id,
            "complete": payload.get("complete"),
            "files": delivery_files,
        }
        return self._step(
            "①+② RDH 采集 + DBAS 导入", "PASS",
            f"plan={plan_id} delivery={payload.get('delivery_id')} "
            f"files={len(delivery_files)} imported={summary.get('imported')} "
            f"skipped={summary.get('skipped')}", seconds,
            {"delivery": self.delivery, "import": summary})

    def _locate_delivery(self, name: str | None, request_id: str | None) -> Path | None:
        storage = Path(self.args.rdh_storage).expanduser()
        if name:
            candidate = storage / "deliveries" / name
            if candidate.is_file():
                return candidate
        if not (storage / "deliveries").is_dir():
            return None
        candidates = sorted((storage / "deliveries").glob("*.json"),
                            key=lambda item: item.stat().st_mtime, reverse=True)
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if request_id is None or payload.get("request_id") == request_id:
                return candidate
        return candidates[0] if candidates else None

    def step_parse_metadata(self) -> Step:
        """③ DBAS 解析：跑 scan_metadata 队列，把 clwc/ciwc 写进 catalog。"""
        argv = [self.dbas_cli, "scan", "--volume", "downloads",
                "--mode", "metadata", "--budget", "300"]
        rc, out, err, seconds = self._run(argv, cwd=PROJECT_ROOT)
        if rc != 0:
            return self._step("③ DBAS 解析元数据", "FAIL",
                              f"exit={rc}; {_tail(err)}", seconds)
        checks = []
        for record in self.import_summary.get("files", []):
            target = Path(self.import_summary["target_dir"]) / record["target"]
            if not target.is_file():
                return self._step("③ DBAS 解析元数据", "FAIL",
                                  f"导入文件不存在: {target}", seconds)
            actual = sha256_file(target)
            checks.append({
                "target": record["target"],
                "file_id": record.get("file_id"),
                "expected_sha256": record.get("sha256"),
                "actual_sha256": actual,
                "match": actual == record.get("sha256"),
            })
        variables = self._catalog_variables()
        saw_cloud = {"clwc", "ciwc"}.issubset({item.lower() for item in variables})
        ok = all(item["match"] for item in checks) and saw_cloud
        return self._step(
            "③ DBAS 解析元数据", "PASS" if ok else "FAIL",
            f"files={len(checks)} sha256_all_match={all(i['match'] for i in checks)} "
            f"catalog_variables={variables}", seconds,
            {"sha256": checks, "variables": variables})

    def _catalog_variables(self) -> list[str]:
        db_path = PROJECT_ROOT / "data" / "catalog.db"
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT DISTINCT v.name FROM variables v JOIN files f ON f.file_id = v.file_id "
                "WHERE f.dataset_id = ? AND f.volume_id = ?",
                ("era5_cloud_water", "downloads")).fetchall()
            return sorted({str(row[0]) for row in rows})
        finally:
            connection.close()

    def step_hydroweave(self) -> Step:
        """④ Hydroweave：LWP/IWP 计算 → 恒等式自检 → 图件，最后核对输入 sha256 链。"""
        env = dict(self.env)
        env["PYTHONPATH"] = f"src:{self.hw_repo}"
        seconds_total = 0.0
        for name, argv in (
            ("compute", [self.hw_python, "experiments/cloud_water_strict/compute.py",
                         "--config", str(self.hw_config), "--run-id", self.run_id]),
            ("verify", [self.hw_python, "experiments/cloud_water_strict/verify.py",
                        "--config", str(self.hw_config), "--run-id", self.run_id]),
            ("plot", [self.hw_python, "experiments/cloud_water_strict/plot.py",
                      "--config", str(self.hw_config), "--run-id", self.run_id]),
        ):
            rc, out, err, seconds = self._run(argv, cwd=self.hw_repo, env=env)
            seconds_total += seconds
            if rc != 0:
                return self._step("④ Hydroweave 云水模型验证", "FAIL",
                                  f"{name} exit={rc}; {_tail(err)}", seconds_total)
        inputs_path = self.hw_run_dir / "inputs.json"
        if not inputs_path.is_file():
            return self._step("④ Hydroweave 云水模型验证", "FAIL",
                              f"缺少 {inputs_path}", seconds_total)
        inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
        hw_hashes = {str(item.get("sha256")) for item in inputs.get("files", [])
                     if isinstance(item, dict)}
        import_hashes = {str(item.get("sha256")) for item in self.import_summary.get("files", [])}
        chain_ok = bool(hw_hashes & import_hashes) and hw_hashes.issubset(import_hashes)
        figures = sorted(str(path.relative_to(self.hw_run_dir))
                         for path in self.hw_run_dir.rglob("*.png"))
        stats = {}
        stats_path = self.hw_run_dir / "stats.json"
        if stats_path.is_file():
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        return self._step(
            "④ Hydroweave 云水模型验证", "PASS" if chain_ok and figures else "FAIL",
            f"input_sha256_chain={chain_ok} figures={len(figures)} run_dir={self.hw_run_dir}",
            seconds_total, {"hw_hashes": sorted(hw_hashes),
                            "import_hashes": sorted(import_hashes),
                            "figures": figures, "stats": stats})

    # ------------------------------------------------------------------ 报告
    def report(self, started_at: str) -> int:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        failed = [step for step in self.steps if step.status != "PASS"]
        verdict = "PASS" if self.steps and not failed else "FAIL"
        payload = {
            "kind": "three_end_e2e",
            "verdict": verdict,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "run_id": self.run_id,
            "chain": ["ResearchDataHub(CDS)", "DBAS(catalog)", "Hydroweave(model)"],
            "delivery": self.delivery,
            "import": self.import_summary,
            "steps": [step.as_dict() for step in self.steps],
        }
        (self.report_dir / "e2e.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            f"# 三端 e2e 报告（{self.run_id}）",
            "",
            f"- 判定：**{verdict}**",
            f"- 时间：{payload['started_at']} → {payload['finished_at']}",
            f"- 链路：{' → '.join(payload['chain'])}",
            "",
            "| 步骤 | 判定 | 说明 | 耗时(s) |",
            "|---|---|---|---|",
        ]
        for step in self.steps:
            lines.append(f"| {step.name} | {step.status} | {step.detail} | {step.seconds:.1f} |")
        lines += ["", "## 校验链（SHA-256）", ""]
        for step in self.steps:
            if step.name.startswith("③"):
                for item in step.evidence.get("sha256", []):
                    lines.append(f"- `{item['target']}`: {item['actual_sha256']} "
                                 f"（与交付清单一致={item['match']}）")
        lines += ["", "## 图件", ""]
        hw_step = next((s for s in self.steps if s.name.startswith("④")), None)
        if hw_step:
            for figure in hw_step.evidence.get("figures", []):
                lines.append(f"- {self.hw_run_dir / figure}")
        lines.append("")
        (self.report_dir / "e2e.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"\n[e2e] {verdict} → {self.report_dir / 'e2e.md'}")
        return 0 if verdict == "PASS" else 1

    # -------------------------------------------------------------------- 主流程
    def execute(self) -> int:
        started_at = _utc_now()
        if self.args.dry_run:
            print("[dry-run] 将执行：")
            print(f"  1) {self.dbas_cli} fetch rdh --config {self.rdh_config}")
            print(f"  2) {self.dbas_cli} scan --volume downloads --mode metadata")
            print(f"  3) {self.hw_python} experiments/cloud_water_strict/"
                  f"{{compute,verify,plot}}.py --config {self.hw_config} --run-id {self.run_id}")
            print(f"  报告目录：{self.report_dir}")
            return 0
        for step_fn in (self.step_acquire_and_import, self.step_parse_metadata,
                        self.step_hydroweave):
            step = step_fn()
            if step.status != "PASS":
                break
        return self.report(started_at)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="三端 e2e：ResearchDataHub → DBAS → Hydroweave")
    parser.add_argument("--rdh-config", default="configs/fetch/rdh_sample.yaml",
                        help="DBAS 的 rdh fetch 配置（相对 DBAS 项目根）")
    parser.add_argument("--rdh-storage", default=str(DEFAULT_RDH_STORAGE),
                        help="RDH storage.root（交付清单所在）")
    parser.add_argument("--hw-repo", default=str(DEFAULT_HW_REPO), help="Hydroweave 仓库路径")
    parser.add_argument("--hw-config", default="experiments/cloud_water_strict/config.yaml",
                        help="Hydroweave 实验配置（相对 Hydroweave 仓库）")
    parser.add_argument("--run-id", default=datetime.now().strftime("e2e-%Y%m%d-%H%M%S"),
                        help="本次 e2e 的 run-id（也用于 Hydroweave 运行目录）")
    parser.add_argument("--report-dir", default="data/reports/e2e", help="e2e 报告输出目录")
    parser.add_argument("--capture-dir", default=None, help="覆盖 DBAS 抓包事件输出目录")
    parser.add_argument("--plan-id", default=None, help="复用已存在的 RDH 计划")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return E2E(args).execute()


if __name__ == "__main__":
    sys.exit(main())
