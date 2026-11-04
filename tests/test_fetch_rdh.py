"""rdh fetch 驱动测试：全离线，不联网、不碰真实 RDH 仓与 ~/.rdh。

两种替身：

1. ``FakeRdh``（注入 runner）——覆盖大多数分支：正常导入/幂等/交付缺失/
   清单不完整/校验不符/目标冲突/捕获事件；
2. ``fake_rdh_cli``（真子进程脚本）——覆盖默认 subprocess.run 执行器、
   ``plan create`` 的 plan_id 解析、以及 ``dbas fetch rdh`` 子命令端到端。

RDH 交付文件的真实结构：``<storage_root>/raw/...`` 只读原始文件 +
``<storage_root>/deliveries/<delivery_id>.json`` 清单（files[] 的 path 相对
storage_root，checksum 形如 ``sha256:<hex>``）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from dbas.cli import main
from dbas.fetch.rdh import RdhFetchConfig, run_rdh_fetch
from dbas_core.catalog.repository import Catalog
from dbas_core.exceptions import ConfigError, FetchError

REQUEST_ID = "era5-clwc-ciwc-tp-2024-01-01"
PLAN_ID = "plan-selftest-0001"
FILES = (
    ("era5_clwc_tp_20240101.nc", b"fake-netcdf-clwc" * 64),
    ("era5_ciwc_tp_20240101.nc", b"fake-netcdf-ciwc" * 48),
)


# --------------------------------------------------------------------------
# 假 RDH（注入式）
# --------------------------------------------------------------------------

class FakeRdh:
    """in-process 假 RDH：按 ``python -m researchdatahub.cli.main <group> <action>`` 应答。"""

    def __init__(self, storage_root: Path, *, request_id: str = REQUEST_ID,
                 plan_id: str = PLAN_ID, files=FILES, complete: bool = True,
                 write_manifest: bool = True, tamper_sha: bool = False,
                 fail_on: tuple[str, str] | None = None) -> None:
        self.storage_root = storage_root
        self.request_id = request_id
        self.plan_id = plan_id
        self.files = files
        self.complete = complete
        self.write_manifest = write_manifest
        self.tamper_sha = tamper_sha
        self.fail_on = fail_on
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout_s: float) -> str:
        self.calls.append(list(argv))
        group = argv[3] if len(argv) > 3 else ""
        action = argv[4] if len(argv) > 4 else ""
        if self.fail_on == (group, action):
            raise FetchError(f"RDH 命令退出码 7: 模拟 {group} {action} 失败")
        if (group, action) == ("plan", "create"):
            # rich 表格标题里带 plan_id；驱动必须能从这行解析出来
            return (f"╭──── 采集计划：{self.plan_id} ────╮\n"
                    f"│ 请求 │ {self.request_id} │\n╰────╯\n")
        if (group, action) == ("plan", "show"):
            return json.dumps({"plan_id": self.plan_id, "request_id": self.request_id})
        if (group, action) == ("task", "run"):
            return "入队 1 个任务\n"
        if (group, action) == ("report", "generate"):
            if self.write_manifest:
                self.write_delivery_json()
            return "报告：report-xxx.json\n交付清单：deliveries/delivery-selftest-1.json\n"
        raise AssertionError(f"未预期的 RDH 调用: {argv}")

    def write_delivery_json(self) -> Path:
        entries = []
        for i, (name, data) in enumerate(self.files):
            raw = self.storage_root / "raw" / "cds" / name
            raw.parent.mkdir(parents=True, exist_ok=True)
            if not raw.exists():
                raw.write_bytes(data)
                raw.chmod(0o444)          # 与 RDH 真实交付一致：只读原始文件
            digest = (hashlib.sha256(b"tampered").hexdigest() if self.tamper_sha
                      else hashlib.sha256(data).hexdigest())
            entries.append({
                "file_id": f"file-{i}", "task_id": f"task-{i}",
                "dataset_id": "era5-pressure-levels",
                "path": f"raw/cds/{name}", "path_escapes_base": False,
                "size_bytes": len(data), "checksum": f"sha256:{digest}",
                "validation_status": "passed",
            })
        payload = {
            "schema_version": "1.0", "delivery_id": "delivery-selftest-1",
            "request_id": self.request_id, "plan_id": self.plan_id,
            "created_at": "2024-01-01T00:00:00+00:00", "tool_version": "0.0-test",
            "complete": self.complete,
            "total_bytes": sum(len(d) for _, d in self.files),
            "notes": None if self.complete else "有文件未通过完整性校验",
            "relative_to": str(self.storage_root), "dataset_metadata": {},
            "files": entries, "integrity": [], "excluded": [], "sources": [],
            "verification": None,
        }
        out = self.storage_root / "deliveries" / "delivery-selftest-1.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return out


# --------------------------------------------------------------------------
# 环境 fixture
# --------------------------------------------------------------------------

@dataclass
class RdhEnv:
    tmp: Path
    project_root: Path
    rdh_repo: Path
    storage_root: Path
    request_file: Path
    config_path: Path
    capture_dir: Path

    def config(self, **overrides) -> RdhFetchConfig:
        data = {
            "rdh_repo": str(self.rdh_repo),
            "rdh_python": str(self.rdh_repo / ".venv" / "bin" / "python"),
            "request_file": "examples/era5-clwc-ciwc-tp-2024-01-01.yaml",
            "storage_root": str(self.storage_root),
            "target_dir": "data/downloads",
            "volume_id": "downloads",
            "dataset_id": "era5_cloud_water",
            "capture_dir": str(self.capture_dir),
            "timeout_s": 30,
        }
        data.update(overrides)
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True),
                                    encoding="utf-8")
        return RdhFetchConfig.from_yaml(self.config_path)


@pytest.fixture
def rdh_env(tmp_path) -> RdhEnv:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    rdh_repo = tmp_path / "rdh"
    (rdh_repo / "examples").mkdir(parents=True)
    request_file = rdh_repo / "examples" / "era5-clwc-ciwc-tp-2024-01-01.yaml"
    request_file.write_text(
        f"schema_version: '1.0'\nrequest_id: {REQUEST_ID}\ndataset_id: era5-pressure-levels\n",
        encoding="utf-8")
    storage_root = tmp_path / "rdhroot"
    storage_root.mkdir()
    return RdhEnv(tmp=tmp_path, project_root=project_root, rdh_repo=rdh_repo,
                  storage_root=storage_root, request_file=request_file,
                  config_path=tmp_path / "rdh.yaml",
                  capture_dir=project_root / "data" / "captures" / "rdh")


def _target_dir(env: RdhEnv) -> Path:
    return env.project_root / "data" / "downloads" / "era5_cloud_water"


def _task_rows(cat: Catalog) -> list[dict]:
    return list(cat.conn.execute("SELECT * FROM tasks").fetchall())


def _fake_cli_script(env: RdhEnv, *, exit_code: int | None = None) -> Path:
    """写一个「假装是 RDH venv python」的可执行脚本，供默认 runner 真跑子进程。"""
    script = env.rdh_repo / ".venv" / "bin" / "python"
    script.parent.mkdir(parents=True, exist_ok=True)
    if exit_code is not None:
        script.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "sys.stderr.write('模拟 RDH 内部错误：boom\\n')\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8")
        script.chmod(0o755)
        return script
    body = f"""#!{sys.executable}
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path({str(env.storage_root)!r})
args = sys.argv[1:]
if args[:2] == ["-m", "researchdatahub.cli.main"]:
    args = args[2:]
group = args[0] if args else ""
action = args[1] if len(args) > 1 else ""
if (group, action) == ("plan", "create"):
    print("采集计划：{PLAN_ID}")
elif (group, action) == ("task", "run"):
    print("入队 1 个任务")
elif (group, action) == ("report", "generate"):
    entries = []
    for i, (name, data) in enumerate({FILES!r}):
        raw = ROOT / "raw" / "cds" / name
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(data)
        raw.chmod(0o444)
        entries.append({{
            "file_id": f"f{{i}}", "path": f"raw/cds/{{name}}",
            "size_bytes": len(data), "path_escapes_base": False,
            "checksum": "sha256:" + hashlib.sha256(data).hexdigest(),
            "validation_status": "passed",
        }})
    payload = {{
        "delivery_id": "delivery-selftest-1", "request_id": {REQUEST_ID!r},
        "plan_id": "{PLAN_ID}", "complete": True,
        "total_bytes": sum(e["size_bytes"] for e in entries),
        "files": entries,
    }}
    out = ROOT / "deliveries" / "delivery-selftest-1.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    print("交付清单：" + str(out))
else:
    sys.stderr.write(f"unexpected args: {{args!r}}\\n")
    sys.exit(3)
"""
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def test_config_from_yaml_defaults_and_override(rdh_env):
    cfg = rdh_env.config()
    assert cfg.dataset_id == "era5_cloud_water"
    assert cfg.volume_id == "downloads"
    assert cfg.target_dir == "data/downloads"
    assert cfg.timeout_s == 30
    assert cfg.capture_dir == rdh_env.capture_dir
    # 相对 request_file 由驱动按 rdh_repo 解析
    assert cfg.request_file == Path("examples/era5-clwc-ciwc-tp-2024-01-01.yaml")


def test_config_requires_repo_and_request_file(rdh_env):
    rdh_env.config_path.write_text("rdh_python: /usr/bin/python3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="rdh_repo"):
        RdhFetchConfig.from_yaml(rdh_env.config_path)
    rdh_env.config_path.write_text("rdh_repo: /tmp/rdh\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="request_file"):
        RdhFetchConfig.from_yaml(rdh_env.config_path)


# --------------------------------------------------------------------------
# 正常导入
# --------------------------------------------------------------------------

def test_imports_delivery_and_registers_catalog(rdh_env, cat):
    cfg = rdh_env.config()
    fake = FakeRdh(rdh_env.storage_root)
    summary = run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)

    assert summary["imported"] == 2 and summary["skipped"] == 0
    assert summary["plan_id"] == PLAN_ID and summary["request_id"] == REQUEST_ID
    assert summary["dataset_id"] == "era5_cloud_water"
    # 文件内容与 RDH 原始交付逐字节一致（只拷副本）
    for name, data in FILES:
        assert (_target_dir(rdh_env) / name).read_bytes() == data
    # 原始 RDH 文件未被移动/改动
    for name, data in FILES:
        assert (rdh_env.storage_root / "raw" / "cds" / name).read_bytes() == data

    # catalog：卷 + 文件 + 数据集归属 + 哈希登记后需 scan_metadata 任务
    vol = cat.get_volume("downloads")
    assert vol is not None and vol["status"] == "online"
    rows = list(cat.iter_files(volume_id="downloads"))
    assert {r["rel_path"] for r in rows} == {f"era5_cloud_water/{n}" for n, _ in FILES}
    assert all(r["dataset_id"] == "era5_cloud_water" for r in rows)
    assert all(r["size_bytes"] == len(data) for r, (_, data) in zip(
        sorted(rows, key=lambda r: r["rel_path"]), sorted(FILES), strict=True))
    datasets = {d["dataset_id"]: d for d in cat.list_datasets()}
    assert datasets["era5_cloud_water"]["files"] == 2
    tasks = cat.list_tasks(status="pending")
    assert len(tasks) == 2
    assert all(t["type"] == "scan_metadata" for t in tasks)
    # 命令顺序：plan create → task run → report generate
    assert [call[3:5] for call in fake.calls] == [
        ["plan", "create"], ["task", "run"], ["report", "generate"]]


def test_idempotent_rerun_skips_and_does_not_duplicate_tasks(rdh_env, cat):
    cfg = rdh_env.config()
    fake = FakeRdh(rdh_env.storage_root)
    first = run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)
    assert first["imported"] == 2 and first["tasks_created"] == 2

    second = run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)
    assert second["imported"] == 0 and second["skipped"] == 2
    assert second["tasks_created"] == 0
    assert len(_task_rows(cat)) == 2              # 任务没有翻倍
    # 文件未被重写（mtime 不变），内容一致
    for name, data in FILES:
        assert (_target_dir(rdh_env) / name).read_bytes() == data


def test_plan_id_reuse_skips_plan_create(rdh_env, cat):
    cfg = rdh_env.config(plan_id=PLAN_ID)
    fake = FakeRdh(rdh_env.storage_root)
    summary = run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)
    assert summary["plan_id"] == PLAN_ID
    assert ["plan", "create"] not in [call[3:5] for call in fake.calls]


# --------------------------------------------------------------------------
# 失败路径
# --------------------------------------------------------------------------

def test_missing_delivery_raises(rdh_env, cat):
    cfg = rdh_env.config()
    fake = FakeRdh(rdh_env.storage_root, write_manifest=False)
    with pytest.raises(FetchError, match="交付清单目录不存在"):
        run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)
    assert cat.count_files(volume_id="downloads") == 0


def test_incomplete_delivery_rejected(rdh_env, cat):
    cfg = rdh_env.config()
    fake = FakeRdh(rdh_env.storage_root, complete=False)
    with pytest.raises(FetchError, match="交付清单不完整"):
        run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)
    assert not list(_target_dir(rdh_env).glob("*.nc"))


def test_sha256_mismatch_rejected(rdh_env, cat):
    cfg = rdh_env.config()
    fake = FakeRdh(rdh_env.storage_root, tamper_sha=True)
    with pytest.raises(FetchError, match="SHA-256 不符"):
        run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)
    # 校验失败必须先于拷贝：不落任何文件，也不登记 catalog
    assert not list(_target_dir(rdh_env).glob("*.nc"))
    assert cat.count_files(volume_id="downloads") == 0


def test_existing_target_with_different_content_refused(rdh_env, cat):
    cfg = rdh_env.config()
    _target_dir(rdh_env).mkdir(parents=True, exist_ok=True)
    existing = _target_dir(rdh_env) / FILES[0][0]
    existing.write_bytes("旧版本，绝不能被我覆盖".encode())
    fake = FakeRdh(rdh_env.storage_root)
    with pytest.raises(FetchError, match="拒绝覆盖"):
        run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)
    assert existing.read_bytes() == "旧版本，绝不能被我覆盖".encode()


def test_rdh_command_error_propagates(rdh_env, cat):
    cfg = rdh_env.config()
    fake = FakeRdh(rdh_env.storage_root, fail_on=("task", "run"))
    with pytest.raises(FetchError, match="退出码 7"):
        run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)


def test_default_runner_reports_nonzero_exit(rdh_env, cat):
    """默认 subprocess 执行器：RDH 命令非零退出 → FetchError（不吞 stderr）。"""
    script = _fake_cli_script(rdh_env, exit_code=2)
    cfg = rdh_env.config(rdh_python=str(script))
    with pytest.raises(FetchError, match="退出码 2"):
        run_rdh_fetch(cat, cfg, rdh_env.project_root)


# --------------------------------------------------------------------------
# 捕获事件
# --------------------------------------------------------------------------

def test_capture_events_redacted_and_complete(rdh_env, cat):
    cfg = rdh_env.config()
    fake = FakeRdh(rdh_env.storage_root)
    run_rdh_fetch(cat, cfg, rdh_env.project_root, runner=fake)

    path = rdh_env.capture_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == [
        "rdh_plan", "rdh_task", "rdh_delivery", "import", "import"]
    imports = [e for e in events if e["event"] == "import"]
    assert {e["source"] for e in imports} == {f"raw/cds/{n}" for n, _ in FILES}
    assert {e["target"] for e in imports} == {
        f"era5_cloud_water/{n}" for n, _ in FILES}
    assert {e["sha256"] for e in imports} == {
        hashlib.sha256(data).hexdigest() for _, data in FILES}
    assert all(e["action"] == "imported" for e in imports)
    # 事件里只留相对路径：不含项目根 / storage_root 的绝对路径
    text = path.read_text(encoding="utf-8")
    assert str(rdh_env.storage_root) not in text
    assert str(rdh_env.project_root) not in text


# --------------------------------------------------------------------------
# dry-run 与 CLI
# --------------------------------------------------------------------------

def test_dry_run_prints_commands_without_side_effects(rdh_env):
    cfg = rdh_env.config(rdh_python=sys.executable)   # dry-run 会校验解释器存在
    plan = run_rdh_fetch(None, cfg, rdh_env.project_root, dry_run=True)
    assert plan["dry_run"] is True
    assert plan["request_id"] == REQUEST_ID
    assert len(plan["commands"]) == 3                      # create → run → generate
    assert all(cmd[:3] == [cfg.rdh_python, "-m", "researchdatahub.cli.main"]
               for cmd in plan["commands"])
    assert not (rdh_env.project_root / "data").exists()
    assert not (rdh_env.storage_root / "deliveries").exists()


def test_cli_rdh_dry_run(rdh_env, capsys):
    rdh_env.config(rdh_python=sys.executable)              # 只需存在，不执行
    rc = main(["--project-root", str(rdh_env.project_root), "fetch", "rdh",
               "--config", str(rdh_env.config_path), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "researchdatahub.cli.main" in out
    assert not (rdh_env.project_root / "data" / "downloads").exists()


def test_cli_rdh_import_end_to_end(rdh_env):
    """真 subprocess 跑假 RDH CLI：覆盖默认 runner、plan_id 解析、CLI 装配。"""
    script = _fake_cli_script(rdh_env)
    rdh_env.config(rdh_python=str(script))
    rc = main(["--project-root", str(rdh_env.project_root), "fetch", "rdh",
               "--config", str(rdh_env.config_path)])
    assert rc == 0
    for name, data in FILES:
        assert (_target_dir(rdh_env) / name).read_bytes() == data
    with Catalog(rdh_env.project_root / "data" / "catalog.db") as db:
        files = list(db.iter_files(volume_id="downloads"))
        assert {f["rel_path"] for f in files} == {f"era5_cloud_water/{n}" for n, _ in FILES}
        assert db.count_tasks_by_status().get("pending", 0) == 2
