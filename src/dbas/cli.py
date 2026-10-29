"""dbas 统一命令行入口。运行 `python -m dbas --help` 查看全部命令。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from dbas.catalog.db import current_version
from dbas.catalog.repository import Catalog, VolumeOffline
from dbas.core.config import ensure_dirs, load_config
from dbas.core.exceptions import DbasError
from dbas.core.logging import get_logger, setup_logging
from dbas.resource_guard.guard import ResourceGuard
from dbas.scheduler.runner import Handler, Runner, RunContext
from dbas.scanner import scan as scanner_mod
from dbas.scanner.scan import (ScanStats, checksum_one, scan_inventory,
                               scan_metadata_one, verify_one)

log = get_logger("cli")


def _open_catalog(cfg, read_only: bool = False) -> Catalog:
    cat = Catalog(cfg.db_path, read_only=read_only)
    if not read_only:
        ensure_dirs(cfg)
    return cat


def _print_summary(s, prefix: str = "运行结果") -> None:
    print(f"[{prefix}] 认领 {s.claimed} | 成功 {s.succeeded} | 失败 {s.failed} "
          f"| 等待重试 {s.skipped_retry} | 耗时 {s.elapsed_s:.1f}s "
          f"| RSS峰值 {s.peak_rss_mb:.0f}MB")
    for e in s.errors[:10]:
        print(f"  错误: {e}")
    if len(s.errors) > 10:
        print(f"  ... 及另外 {len(s.errors) - 10} 条错误")
    hint = s.resume_hint()
    if hint:
        print(f"停止原因: {s.stop_reason}。{hint}")


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------

def cmd_status(args, cfg) -> int:
    if not cfg.db_path.exists():
        print(f"DBAS 状态（项目根: {cfg.root}）")
        print("  目录数据库尚未创建。第一步：")
        print("    python -m dbas volume register <数据卷挂载点>")
        print("    python -m dbas scan --volume <卷ID> --mode inventory")
        return 0
    with _open_catalog(cfg, read_only=True) as cat:
        s = cat.status_summary()
    print(f"DBAS 状态（项目根: {cfg.root}）")
    print(f"  schema 版本: {s['schema_version']}")
    print(f"  数据库: {cfg.db_path} "
          f"({'存在' if cfg.db_path.exists() else '尚未创建，首次写操作时自动建立'})")
    print(f"  数据卷: {len(s['volumes'])}")
    for v in s["volumes"]:
        print(f"    - {v['volume_id']} [{v['status']}] {v['last_mount_path'] or v['root_path']}")
    print(f"  文件状态: {s['files_by_status'] or '（无记录）'}")
    print(f"  解析状态: {s['files_by_parse'] or '（无记录）'}")
    print(f"  任务队列: {s['tasks_by_status'] or '（空）'}")
    return 0


def cmd_volume_register(args, cfg) -> int:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"错误: 挂载点不存在或不是目录: {root}", file=sys.stderr)
        return 2
    volume_id = args.id
    from dbas.storage.volumes import suggest_volume_id
    note = ""
    if not volume_id:
        volume_id, note = suggest_volume_id(root)
        print(f"未指定 --id，建议使用: {volume_id}（{note}）")
    with _open_catalog(cfg) as cat:
        vol = cat.register_volume(volume_id, root, label=args.label, notes=note)
    print(f"已注册数据卷 {vol['volume_id']} -> {root}")
    print(f"下一步: python -m dbas scan --volume {volume_id} --mode inventory")
    return 0


def cmd_scan(args, cfg) -> int:
    scan_cfg = cfg.section("scan")
    extensions = scan_cfg["nc_extensions"]
    if not args.volume:
        print("错误: 需要 --volume（先用 `volume register` 注册）", file=sys.stderr)
        return 2
    with _open_catalog(cfg) as cat:
        try:
            if args.mode == "inventory":
                run_id = cat.start_scan_run("inventory", {"volume": args.volume})
                stats = scan_inventory(cat, args.volume, extensions,
                                       enqueue_metadata=args.enqueue_metadata)
                cat.finish_scan_run(run_id, "done", stats.seen, stats.ok or stats.seen,
                                    0, f"new/changed={stats.new} missing_pending={stats.missing_pending}")
                _print_scan_stats(stats)
            elif args.mode == "incremental":
                run_id = cat.start_scan_run("incremental", {"volume": args.volume})
                stats = scan_inventory(cat, args.volume, extensions,
                                       enqueue_metadata=True, incremental=True)
                cat.finish_scan_run(run_id, "done", stats.seen, stats.new,
                                    0, f"changed={stats.changed}")
                _print_scan_stats(stats)
                _run_task_queue(cat, cfg, ["scan_metadata"], args)
            elif args.mode == "metadata":
                _run_task_queue(cat, cfg, ["scan_metadata"], args)
            else:  # pragma: no cover
                print(f"未知扫描模式: {args.mode}", file=sys.stderr)
                return 2
        except VolumeOffline as e:
            print(f"错误: {e}", file=sys.stderr)
            return 3
    return 0


def _print_scan_stats(st: ScanStats) -> None:
    print(f"[扫描] 发现 {st.seen} 个文件 | 新增/变化 {st.new} "
          f"| 待确认缺失 {st.missing_pending} | 耗时 {st.elapsed_s():.1f}s")


def _run_task_queue(cat: Catalog, cfg, types: list[str], args) -> None:
    res = cfg.section("resource")
    guard = ResourceGuard(max_rss_mb=res["max_rss_mb"],
                          min_available_mb=res["min_available_mb"])
    sched = cfg.section("scheduler")
    handlers: dict[str, Handler] = {
        "scan_metadata": scan_metadata_one,
        "verify": verify_one,
        "checksum": checksum_one,
    }
    runner = Runner(cat, handlers,
                    budget_s=args.budget if args.budget is not None else scan_budget(cfg),
                    batch_limit=args.batch_size if args.batch_size is not None else scan_batch(cfg),
                    guard=guard, retry_delay_s=sched["retry_delay_s"], resume=True)
    summary = runner.run(types=types)
    _print_summary(summary)


def scan_budget(cfg) -> float:
    return cfg.section("scan")["budget_s"]


def scan_batch(cfg) -> int:
    return cfg.section("scan")["batch_size"]


def cmd_verify(args, cfg) -> int:
    with _open_catalog(cfg) as cat:
        _enqueue_file_tasks(cat, "verify", args.volume)
        _run_task_queue(cat, cfg, ["verify"], args)
    return 0


def cmd_checksum(args, cfg) -> int:
    with _open_catalog(cfg) as cat:
        _enqueue_file_tasks(cat, "checksum", args.volume)
        _run_task_queue(cat, cfg, ["checksum"], args)
    return 0


def _enqueue_file_tasks(cat: Catalog, type_: str, volume_id: str | None) -> None:
    """为卷内 active 文件补齐 verify/checksum 任务（幂等，已入队的不重复）。"""
    n = 0
    for f in cat.iter_files(volume_id=volume_id, status="active"):
        mtime = int(f["mtime"]) if f["mtime"] else 0
        _, created = cat.enqueue_task(
            type_,
            {"file_id": f["file_id"], "volume_id": f["volume_id"],
             "rel_path": f["rel_path"]},
            dedup_key=f"{type_}:{f['file_id']}:{mtime}")
        n += created
    if n:
        print(f"新增 {type_} 任务 {n} 个")


def cmd_tasks(args, cfg) -> int:
    with _open_catalog(cfg, read_only=(args.action != "prune")) as cat:
        if args.action == "list":
            rows = cat.list_tasks(status=args.status, limit=args.limit)
            if not rows:
                print("队列为空")
            for t in rows:
                print(f"{t['task_id']}  {t['type']:<14} {t['status']:<10} "
                      f"attempts={t['attempts']}/{t['max_attempts']} "
                      f"{t['last_error'] or ''}")
        elif args.action == "show":
            t = cat.get_task(args.task_id)
            if not t:
                print(f"任务不存在: {args.task_id}", file=sys.stderr)
                return 2
            for k in ("task_id", "type", "status", "attempts", "max_attempts",
                      "last_error", "claimed_by", "created_at", "updated_at"):
                print(f"{k}: {t[k]}")
            print(f"params: {t['params_json']}")
            print(f"result: {t['result_json']}")
        elif args.action == "prune":
            # 清理已完成任务，防止队列无限膨胀（历史保留在 task_attempts）
            cur = cat.conn.execute(
                "DELETE FROM tasks WHERE status IN ('succeeded','cancelled')")
            cat.conn.commit()
            print(f"已清理 {cur.rowcount} 个已完成任务")
    return 0


def cmd_report(args, cfg) -> int:
    if args.kind != "inventory":
        print(f"未知报告类型: {args.kind}", file=sys.stderr)
        return 2
    with _open_catalog(cfg, read_only=True) as cat:
        out = _report_inventory(cat, cfg)
    print(f"报告: {out}")
    return 0


def _report_inventory(cat, cfg) -> Path:
    from dbas.reports.report import inventory_report
    return inventory_report(cat, cfg.reports_dir)


def cmd_db(args, cfg) -> int:
    if args.action == "check":
        with _open_catalog(cfg, read_only=True) as cat:
            ver = current_version(cat.conn)
            integrity = cat.conn.execute("PRAGMA integrity_check").fetchone()[0]
            fk = cat.conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"schema 版本: {ver}")
        print(f"integrity_check: {integrity}")
        print(f"foreign_key_check: {'通过' if not fk else fk}")
        return 0 if integrity == "ok" and not fk else 1
    if args.action == "backup":
        ensure_dirs(cfg)
        with _open_catalog(cfg) as cat:
            ts = time.strftime("%Y%m%d_%H%M%S")
            dst = cfg.home / "backups" / f"catalog_{ts}.db"
            dst.parent.mkdir(parents=True, exist_ok=True)
            # SQLite 在线备份 API，可在使用中安全复制
            src = sqlite3.connect(str(cfg.db_path))
            try:
                src.backup(sqlite3.connect(str(dst)))
            finally:
                src.close()
            print(f"已备份到 {dst}")
        return 0
    if args.action == "rebuild":
        print("数据库重建：当前实现为「删除库文件后重新注册卷并扫描」。\n"
              "原始数据不受影响。请依次执行：\n"
              f"  1) 先备份: python -m dbas db backup\n"
              f"  2) 移走 {cfg.db_path}（不要删，先确认）\n"
              "  3) python -m dbas volume register ... 重新注册\n"
              "  4) python -m dbas scan --volume ... --mode inventory --enqueue-metadata")
        return 0
    print(f"未知 db 操作: {args.action}", file=sys.stderr)
    return 2


def cmd_fetch(args, cfg) -> int:
    from dbas.fetch.cds import FetchConfig, download_one, enqueue_fetch_tasks, ensure_download_volume
    if args.action == "plan":
        fcfg = FetchConfig.from_yaml(Path(args.config).expanduser())
        reqs = fcfg.build_requests()
        print(f"数据集: {fcfg.dataset}")
        print(f"变量: {fcfg.variables}")
        print(f"计划请求数: {len(reqs)}（每年×每月一个最小请求）")
        for r in reqs:
            print(f"  - {r['year']}-{r['month']} {json.dumps(r['request'], ensure_ascii=False)[:120]}")
        return 0
    if args.action == "enqueue":
        with _open_catalog(cfg) as cat:
            fcfg = FetchConfig.from_yaml(Path(args.config).expanduser())
            ensure_download_volume(cat, fcfg, cfg.root)
            created, skipped = enqueue_fetch_tasks(cat, fcfg)
        print(f"入队 {created} 个下载任务，跳过重复 {skipped} 个")
        print("执行下载: python -m dbas fetch run")
        return 0
    if args.action == "run":
        with _open_catalog(cfg) as cat:
            fcfg = FetchConfig.from_yaml(Path(args.config).expanduser())
            ensure_download_volume(cat, fcfg, cfg.root)

            def handler(params, catalog, ctx):
                ctx.heartbeat()
                return download_one(catalog, params, cfg.root)

            sched = cfg.section("scheduler")
            res = cfg.section("resource")
            runner = Runner(cat, {"download": handler},
                            budget_s=args.budget if args.budget is not None else 3600,
                            batch_limit=args.batch_size if args.batch_size is not None else 1000,
                            guard=ResourceGuard(res["max_rss_mb"], res["min_available_mb"]),
                            retry_delay_s=sched["retry_delay_s"], resume=True)
            summary = runner.run(types=["download"])
            _print_summary(summary)
        return 0
    print(f"未知 fetch 操作: {args.action}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# argparse 装配
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dbas", description="DBAS — ERA5 科研数据管理与分析系统")
    p.add_argument("--project-root", default=None,
                   help="项目根目录（默认自动定位 pyproject.toml）")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看系统状态概览")

    sp = sub.add_parser("volume", help="数据卷管理")
    vsub = sp.add_subparsers(dest="action", required=True)
    vr = vsub.add_parser("register", help="注册数据卷（挂载点）")
    vr.add_argument("root", help="当前挂载点路径")
    vr.add_argument("--id", default=None, help="稳定逻辑 ID（建议显式指定）")
    vr.add_argument("--label", default="")
    vr.add_argument("--notes", default="", help=argparse.SUPPRESS)
    vsub.add_parser("list", help="列出已注册数据卷")

    sp = sub.add_parser("scan", help="扫描数据卷")
    sp.add_argument("--volume", required=True)
    sp.add_argument("--mode", default="inventory",
                    choices=["inventory", "incremental", "metadata"],
                    help="inventory=盘点; incremental=增量; metadata=解析元数据任务")
    sp.add_argument("--enqueue-metadata", action="store_true",
                    help="inventory 后同时入队元数据解析任务")
    sp.add_argument("--batch-size", type=int, default=None)
    sp.add_argument("--budget", type=float, default=None,
                    help="单次运行时间预算（秒）")

    sp = sub.add_parser("verify", help="验证文件可读性")
    sp.add_argument("--volume", default=None)
    sp.add_argument("--batch-size", type=int, default=None)
    sp.add_argument("--budget", type=float, default=None)

    sp = sub.add_parser("checksum", help="计算 sha256 校验和（分批）")
    sp.add_argument("--volume", default=None)
    sp.add_argument("--batch-size", type=int, default=None)
    sp.add_argument("--budget", type=float, default=None)

    sp = sub.add_parser("tasks", help="任务队列管理")
    tsub = sp.add_subparsers(dest="action", required=True)
    tl = tsub.add_parser("list")
    tl.add_argument("--status", default=None)
    tl.add_argument("--limit", type=int, default=50)
    tsub.add_parser("show").add_argument("task_id")
    tsub.add_parser("prune", help="清理已完成任务（防队列膨胀）")

    sp = sub.add_parser("report", help="生成报告")
    sp.add_argument("kind", nargs="?", default="inventory", choices=["inventory"])

    sp = sub.add_parser("db", help="数据库管理")
    dsub = sp.add_subparsers(dest="action", required=True)
    dsub.add_parser("check")
    dsub.add_parser("backup")
    dsub.add_parser("rebuild", help="显示重建数据库步骤")

    sp = sub.add_parser("fetch", help="ERA5 CDS API 下载管理")
    fsub = sp.add_subparsers(dest="action", required=True)
    for name, help_ in (("plan", "预览下载计划（不联网）"),
                        ("enqueue", "把下载计划入队"),
                        ("run", "执行下载任务")):
        f = fsub.add_parser(name, help=help_)
        f.add_argument("--config", required=True, help="fetch 配置 YAML 路径")
        f.add_argument("--budget", type=float, default=None)
        f.add_argument("--batch-size", type=int, default=None)
    return p


_DISPATCH = {
    "status": cmd_status, "volume": None, "scan": cmd_scan,
    "verify": cmd_verify, "checksum": cmd_checksum, "tasks": cmd_tasks,
    "report": cmd_report, "db": cmd_db, "fetch": cmd_fetch,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(Path(args.project_root) if args.project_root else None)
    except DbasError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.data.get("log_level", "INFO"))

    # volume 子命令需要二级分发
    if args.command == "volume":
        if args.action == "register":
            return cmd_volume_register(args, cfg)
        if args.action == "list":
            with _open_catalog(cfg, read_only=True) as cat:
                vols = cat.list_volumes()
                if not vols:
                    print("（尚未注册数据卷）")
                for v in vols:
                    print(f"{v['volume_id']}  [{v['status']}]  {v['last_mount_path'] or v['root_path']}  {v['label'] or ''}")
            return 0

    handler = _DISPATCH.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args, cfg)
    except DbasError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n用户中断。已完成的文件状态均已提交；重跑同一命令可从断点继续。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
