"""dbas 统一命令行入口。运行 `python -m dbas --help` 查看全部命令。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dbas_core.analysis.datasets import detect_dataset, ensure_builtin_datasets
from dbas_core.analysis.profile import profile_one
from dbas_core.catalog.db import current_version
from dbas_core.catalog.repository import Catalog, VolumeOffline
from dbas_core.config import ensure_dirs, load_config
from dbas_core.qc.rules import run_dataset_checks
from dbas.reports.profile_qc import profile_qc_report
from dbas.storage.backup import backup_database
from dbas_core.exceptions import DbasError
from dbas_core.logging import get_logger, setup_logging
from dbas.resource_guard.guard import ResourceGuard
from dbas.scheduler.runner import Handler, Runner
from dbas.scanner.scan import (ScanStats, checksum_one, scan_inventory,
                               scan_metadata_one, verify_one)

log = get_logger("cli")


def _open_catalog(cfg, read_only: bool = False) -> Catalog:
    # 只读命令也先确保 schema 迁移完成；迁移本身是写操作，只在需要时发生
    if read_only and cfg.db_path.exists():
        with Catalog(cfg.db_path):
            pass
    cat = Catalog(cfg.db_path, read_only=read_only)
    if not read_only:
        ensure_dirs(cfg)
    return cat


def _nc_extensions(cfg) -> set[str]:
    return {str(e).lower() for e in cfg.section("scan")["nc_extensions"]}


def _is_netcdf_path(rel_path: str, extensions: set[str]) -> bool:
    return Path(rel_path).suffix.lower() in extensions


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
    by_ds = s.get("files_by_dataset") or {}
    if by_ds:
        shown = {k: v for k, v in by_ds.items() if k}
        if shown:
            print(f"  数据集文件: {shown}")
        if by_ds.get(None):
            print(f"  未分组文件: {by_ds[None]}")
    print(f"  已画像文件: {s.get('profiled_files', 0)}")
    if s.get("quality_by_severity"):
        print(f"  QC 发现: {s['quality_by_severity']}")
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
    nc_extensions = scan_cfg["nc_extensions"]
    extensions = scan_cfg.get("inventory_extensions") or nc_extensions
    scan_kwargs = {"netcdf_extensions": nc_extensions,
                   "exclude_dirs": scan_cfg.get("exclude_dirs") or []}
    if not args.volume:
        print("错误: 需要 --volume（先用 `volume register` 注册）", file=sys.stderr)
        return 2
    with _open_catalog(cfg) as cat:
        try:
            if args.mode == "inventory":
                run_id = cat.start_scan_run("inventory", {"volume": args.volume})
                stats = scan_inventory(cat, args.volume, extensions,
                                       enqueue_metadata=args.enqueue_metadata,
                                       **scan_kwargs)
                cat.finish_scan_run(run_id, "done", stats.seen, stats.ok or stats.seen,
                                    0, f"new/changed={stats.new} missing_pending={stats.missing_pending}")
                _print_scan_stats(stats)
            elif args.mode == "incremental":
                run_id = cat.start_scan_run("incremental", {"volume": args.volume})
                stats = scan_inventory(cat, args.volume, extensions,
                                       enqueue_metadata=True, incremental=True,
                                       **scan_kwargs)
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


# --------------------------------------------------------------------------
# 数据集 / 内容画像 / QC
# --------------------------------------------------------------------------

def cmd_dataset(args, cfg) -> int:
    if args.action == "list":
        if not cfg.db_path.exists():
            print("目录数据库尚未创建。先执行 volume register + scan。")
            return 0
        with _open_catalog(cfg, read_only=True) as cat:
            rows = cat.list_datasets()
        if not rows:
            print("（尚未注册数据集）")
            return 0
        print(f"{'数据集':<20} {'文件':>6} {'其中.nc':>8} {'已解析':>6} "
              f"{'已画像':>6}  标签")
        for r in rows:
            print(f"{r['dataset_id']:<20} {r['files']:>6} "
                  f"{r.get('nc_files', 0):>8} {r['parsed']:>6} "
                  f"{r['profiled']:>6}  {r['label']}")
        return 0
    if args.action == "assign":
        exts = _nc_extensions(cfg)
        with _open_catalog(cfg) as cat:
            ensure_builtin_datasets(cat)
            counts: dict[str, int] = {}
            files = [f for f in cat.iter_files(
                volume_id=args.volume, status="active")
                if _is_netcdf_path(f["rel_path"], exts)]
            for f in files:
                ds = detect_dataset(f["rel_path"], f["file_name"])
                cat.set_file_dataset(f["file_id"], ds)
                counts[ds] = counts.get(ds, 0) + 1
            n = len(files)
        print(f"已为 {n} 个文件分配数据集: {counts}")
        return 0
    print(f"未知 dataset 操作: {args.action}", file=sys.stderr)
    return 2


def cmd_profile(args, cfg) -> int:
    pcfg = cfg.section("profile")
    with _open_catalog(cfg) as cat:
        ensure_builtin_datasets(cat)
        exts = _nc_extensions(cfg)
        files = [f for f in cat.iter_files(
            volume_id=args.volume, status="active")
            if _is_netcdf_path(f["rel_path"], exts)]
        if not files:
            print(f"卷 {args.volume} 中没有 active NetCDF 文件"
                  f"（先 scan --mode inventory）", file=sys.stderr)
            return 2

        ds_counts: dict[str, int] = {}
        for f in files:
            ds = detect_dataset(f["rel_path"], f["file_name"])
            if f.get("dataset_id") != ds:
                cat.set_file_dataset(f["file_id"], ds)
            ds_counts[ds] = ds_counts.get(ds, 0) + 1

        candidates = [f for f in files if f.get("parse_status") != "failed"]
        if args.limit is not None:
            candidates = candidates[:args.limit]
        created = 0
        for f in candidates:
            mtime = int(f["mtime"]) if f["mtime"] else 0
            _, ok = cat.enqueue_task(
                "profile_nc",
                {"file_id": f["file_id"], "volume_id": f["volume_id"],
                 "rel_path": f["rel_path"]},
                dedup_key=f"profile_nc:{f['file_id']}:{mtime}")
            created += ok
        print(f"[画像] 卷 {args.volume}: active {len(files)} 个，"
              f"本次候选 {len(candidates)} 个，新入队 {created} 个；"
              f"数据集分布 {ds_counts}")

        res = cfg.section("resource")
        sched = cfg.section("scheduler")
        runner = Runner(
            cat, {"profile_nc": profile_one},
            budget_s=args.budget if args.budget is not None
            else pcfg["budget_s"],
            batch_limit=args.batch_size if args.batch_size is not None
            else pcfg["batch_size"],
            guard=ResourceGuard(res["max_rss_mb"], res["min_available_mb"]),
            retry_delay_s=sched["retry_delay_s"],
            stale_running_s=sched["stale_running_s"], resume=True)
        summary = runner.run(types=["profile_nc"])
        _print_summary(summary)

        dataset_ids = sorted(ds_counts)
        run_dataset_checks(cat, dataset_ids)
        out = profile_qc_report(cat, cfg.reports_dir, volume_id=args.volume)
        print(f"报告: {out}")
    return 0


def cmd_qc(args, cfg) -> int:
    if args.action == "run":
        return cmd_profile(args, cfg)
    if args.action == "report":
        if not cfg.db_path.exists():
            print("目录数据库尚未创建。先执行 scan 与 profile。",
                  file=sys.stderr)
            return 2
        with _open_catalog(cfg) as cat:
            if args.dataset:
                dataset_ids = [args.dataset]
            elif args.volume:
                dataset_ids = cat.dataset_ids_for_volume(args.volume)
            else:
                dataset_ids = [d["dataset_id"] for d in cat.list_datasets()
                               if d.get("nc_files")]
            run_dataset_checks(cat, dataset_ids)
            out = profile_qc_report(cat, cfg.reports_dir,
                                    volume_id=args.volume,
                                    dataset_id=args.dataset)
        print(f"报告: {out}")
        return 0
    print(f"未知 qc 操作: {args.action}", file=sys.stderr)
    return 2


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
                    guard=guard, retry_delay_s=sched["retry_delay_s"],
                    stale_running_s=sched["stale_running_s"], resume=True)
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
    with _open_catalog(cfg, read_only=args.action not in ("prune", "cancel")) as cat:
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
        elif args.action == "cancel":
            if cat.cancel_task(args.task_id):
                print(f"任务 {args.task_id} 已取消")
            else:
                print(f"无法取消（任务不存在或已成功）: {args.task_id}",
                      file=sys.stderr)
                return 2
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
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = cfg.home / "backups" / f"catalog_{ts}.db"
        backup_database(cfg.db_path, dst)
        print(f"已备份并校验到 {dst}")
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


# --------------------------------------------------------------------------
# subset：区域裁切（L1）
# --------------------------------------------------------------------------

def cmd_subset(args, cfg) -> int:
    from dbas_core.analysis.regions import get_region, overlaps_file_grid
    from dbas_core.analysis.subset import ensure_analysis_volume, subset_one

    acfg = cfg.section("analysis")
    region_name = getattr(args, "region", None) or acfg["region"]

    if args.action == "plan":
        bbox = get_region(region_name)
        with _open_catalog(cfg, read_only=True) as cat:
            files = list(cat.iter_files(volume_id=args.volume, status="active"))
        if not files:
            print(f"卷 {args.volume} 中没有 active 文件"
                  f"（先 scan --mode inventory）", file=sys.stderr)
            return 2
        hit = miss = unknown = 0
        print(f"区域 {region_name}: {bbox.label}")
        for f in files:
            lr = (f["lat_min"], f["lat_max"]) if f["lat_min"] is not None else None
            gr = (f["lon_min"], f["lon_max"]) if f["lon_min"] is not None else None
            ov = overlaps_file_grid(bbox, lr, gr)
            tag = "?" if ov is None else ("✔" if ov else "×")
            if ov is None:
                unknown += 1
            elif ov:
                hit += 1
            else:
                miss += 1
            tr = (f"{(f['time_min'] or '?')[:10]}..{(f['time_max'] or '?')[:10]}")
            print(f"  [{tag}] {f['rel_path']}  {tr}  "
                  f"lat{lr and tuple(round(x,1) for x in lr)} "
                  f"lon{gr and tuple(round(x,1) for x in gr)}")
        print(f"合计 {len(files)} 个文件 | 相交 {hit} | 不相交 {miss} | 范围未知 {unknown}"
              f"（未解析的先跑 scan --mode metadata）")
        if hit or unknown:
            print(f"入队: python -m dbas subset enqueue --volume {args.volume} "
                  f"--region {region_name}")
        return 0

    if args.action == "enqueue":
        bbox = get_region(region_name)
        variables = ([v.strip() for v in args.variables.split(",") if v.strip()]
                     if args.variables else None)
        var_sig = ",".join(sorted(variables)) if variables else "all"
        with _open_catalog(cfg) as cat:
            ensure_analysis_volume(cat, cfg)
            files = list(cat.iter_files(volume_id=args.volume, status="active"))
            created = skipped = 0
            for f in files:
                lr = (f["lat_min"], f["lat_max"]) if f["lat_min"] is not None else None
                gr = (f["lon_min"], f["lon_max"]) if f["lon_min"] is not None else None
                if overlaps_file_grid(bbox, lr, gr) is False:
                    continue  # 明确不相交的不入队；未知的仍尝试（解析后可判）
                mtime = int(f["mtime"]) if f["mtime"] else 0
                _, ok = cat.enqueue_task(
                    "subset_nc",
                    {"file_id": f["file_id"], "volume_id": f["volume_id"],
                     "rel_path": f["rel_path"], "region": region_name,
                     "variables": variables, "force": bool(args.force)},
                    dedup_key=f"subset_nc:{f['file_id']}:{mtime}:{region_name}"
                              f":{var_sig}:{int(bool(args.force))}")
                created += ok
                skipped += not ok
        print(f"入队裁切任务 {created} 个，跳过重复 {skipped} 个 → "
              f"输出卷 {acfg['volume_id']}（{cfg.analysis_dir}）")
        print("执行: python -m dbas subset run")
        return 0

    if args.action == "run":
        with _open_catalog(cfg) as cat:
            def handler(params, catalog, ctx):
                return subset_one(params, catalog, ctx, cfg=cfg)

            sched = cfg.section("scheduler")
            res = cfg.section("resource")
            runner = Runner(cat, {"subset_nc": handler},
                            budget_s=args.budget if args.budget is not None
                            else acfg["budget_s"],
                            batch_limit=args.batch_size if args.batch_size is not None
                            else acfg["batch_size"],
                            guard=ResourceGuard(res["max_rss_mb"],
                                                res["min_available_mb"]),
                            retry_delay_s=sched["retry_delay_s"],
                            stale_running_s=sched["stale_running_s"], resume=True)
            summary = runner.run(types=["subset_nc"])
            _print_summary(summary)
        return 0

    print(f"未知 subset 操作: {args.action}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# zarr：数组层派生（源 .nc → Zarr store，登记回 catalog）
# --------------------------------------------------------------------------

def _ensure_array_volume(cat, cfg) -> str:
    """创建数组层目录并注册为数据卷，返回卷 ID。"""
    from dbas.zarr_export.config_ext import install_zarr_config
    install_zarr_config()
    zcfg = cfg.section("zarr")
    cfg.array_dir.mkdir(parents=True, exist_ok=True)
    cat.register_volume(zcfg["volume_id"], cfg.array_dir,
                        label="数组层卷（Zarr 派生）",
                        notes="由 zarr_export 创建；源卷只读、store 可再生")
    return zcfg["volume_id"]


def cmd_zarr(args, cfg) -> int:
    from dbas.zarr_export.config_ext import available_targets, install_zarr_config
    from dbas.zarr_export.store import estimate_chunk_bytes, plan_chunks
    from dbas.zarr_export.tasks import reconcile_store, zarr_export_one

    install_zarr_config()
    zcfg = cfg.section("zarr")

    if args.action == "plan":
        import xarray as xr
        source = Path(args.source).expanduser()
        if not source.exists():
            print(f"源文件不存在: {source}", file=sys.stderr)
            return 2
        ds = xr.open_dataset(source, chunks=None)
        try:
            print(f"源: {source}")
            print(f"维度: {dict(ds.sizes)}")
            print(f"变量: {sorted(str(v) for v in ds.data_vars)}")
            print()
            for t in ([args.target] if args.target else available_targets()):
                p = plan_chunks(ds, t, chunk_elements=int(zcfg["chunk_elements"]))
                est = estimate_chunk_bytes(ds, p)
                biggest = max(est.values()) if est else 0
                print(f"[{t}] {p.rationale}")
                print(f"     分块 {p.chunks} | 最大单块 {biggest/1e6:.1f} MB"
                      f" | 预估文件数 ~{_estimate_files(ds, p)}")
        finally:
            ds.close()
        return 0

    if args.action == "enqueue":
        with _open_catalog(cfg) as cat:
            from dbas_core.analysis.subset import ensure_analysis_volume
            ensure_analysis_volume(cat, cfg)
            vol = _ensure_array_volume(cat, cfg)
            targets = (args.target.split(",") if args.target
                       else [zcfg["default_target"]])
            variables = ([v.strip() for v in args.variables.split(",") if v.strip()]
                         if args.variables else None)
            var_sig = ",".join(sorted(variables)) if variables else "all"
            files = list(cat.iter_files(volume_id=args.volume, status="active"))
            created = skipped = 0
            for f in files:
                if Path(f["rel_path"]).suffix.lower() not in _nc_extensions(cfg):
                    continue
                mtime = int(f["mtime"]) if f["mtime"] else 0
                for t in targets:
                    _, ok = cat.enqueue_task(
                        "zarr_export",
                        {"file_id": f["file_id"], "volume_id": f["volume_id"],
                         "rel_path": f["rel_path"], "target": t,
                         "region": args.region, "variables": variables,
                         "shards": args.shards, "force": bool(args.force)},
                        dedup_key=(f"zarr_export:{f['file_id']}:{mtime}:{t}:"
                                   f"{args.region or '-'}:{var_sig}:"
                                   f"{args.shards or '-'}:{int(bool(args.force))}"))
                    created += ok
                    skipped += not ok
            print(f"入队数组导出任务 {created} 个，跳过重复 {skipped} 个 → "
                  f"数组层卷 {vol}（{cfg.array_dir}）")
            print(f"分块策略: {', '.join(targets)}")
            print("执行: python -m dbas zarr run")
        return 0

    if args.action == "run":
        with _open_catalog(cfg) as cat:
            def handler(params, catalog, ctx):
                return zarr_export_one(params, catalog, ctx, cfg=cfg)

            sched = cfg.section("scheduler")
            res = cfg.section("resource")
            runner = Runner(cat, {"zarr_export": handler},
                            budget_s=args.budget if args.budget is not None
                            else zcfg["budget_s"],
                            batch_limit=args.batch_size if args.batch_size is not None
                            else zcfg["batch_size"],
                            guard=ResourceGuard(res["max_rss_mb"],
                                                res["min_available_mb"]),
                            retry_delay_s=sched["retry_delay_s"],
                            stale_running_s=sched["stale_running_s"], resume=True)
            summary = runner.run(types=["zarr_export"])
            _print_summary(summary)
        return 0

    if args.action == "verify":
        rel = args.store
        root = (Path(rel) if Path(rel).is_absolute()
                else Path(cfg.array_dir) / rel)
        if not root.is_dir():
            print(f"store 不存在: {root}", file=sys.stderr)
            return 2
        with _open_catalog(cfg) as cat:
            file_id = None
            for f in cat.iter_files(volume_id=zcfg["volume_id"]):
                if f["rel_path"] == rel:
                    file_id = f["file_id"]
                    break
            res = reconcile_store({"rel_path": rel, "file_id": file_id},
                                  cat, None, cfg=cfg)
        s = res["summary"]
        print(f"store: {rel}")
        print(f"文件 {s['files']} | 数组 {s['arrays']} | chunk {s['chunks']} "
              f"| 总大小 {s['total_bytes']/1e6:.1f} MB")
        print(f"登记指纹: {res['recorded_digest'] or '（未登记）'}")
        print(f"当前指纹: {res['current_digest']}")
        print("结论: " + ("✔ 一致（store 未被改动）" if res["match"]
                          else "✘ 不一致（store 与登记时不符）"))
        sidecar = Path(cfg.reports_dir) / "zarr" / f"{file_id}.manifest.json"
        if not res["match"] and sidecar.exists():
            from dbas.zarr_export.manifest import (ManifestEntry, StoreManifest,
                                                   compare_manifests, scan_store)
            import json as _json
            old = _json.loads(sidecar.read_text(encoding="utf-8"))
            om = StoreManifest(root=root,
                               entries=[ManifestEntry(**e) for e in old["entries"]])
            diff = compare_manifests(om, scan_store(root))
            print(f"  相对登记时：新增 {diff['added_count']} / "
                  f"删除 {diff['removed_count']} / 变更 {diff['changed_count']}")
            for k in ("added", "removed", "changed"):
                for p in diff[k][:5]:
                    print(f"    {k}: {p}")
        return 0

    if args.action == "info":
        with _open_catalog(cfg, read_only=True) as cat:
            rows = list(cat.iter_files(volume_id=zcfg["volume_id"]))
        if not rows:
            print(f"数组层卷 {zcfg['volume_id']} 中暂无条目")
            return 0
        print(f"数组层卷 {zcfg['volume_id']}（{cfg.array_dir}）")
        for f in rows:
            fp = (f.get("fingerprint") or "-")[:16]
            print(f"  {f['rel_path']}")
            print(f"    大小 {f['size_bytes']/1e6:.1f} MB | "
                  f"{f.get('fingerprint_algo') or '-'}:{fp}")
        return 0

    print(f"未知 zarr 操作: {args.action}", file=sys.stderr)
    return 2


def _estimate_files(ds, plan) -> int:
    """粗估分块后的 chunk 文件数（不含坐标数组与元数据）。"""
    n = 0
    for _name, var in ds.data_vars.items():
        if not var.dims:
            continue
        c = 1
        for i, d in enumerate(var.dims):
            size = int(var.shape[i])
            ch = int(min(plan.chunks[str(d)], size))
            c *= -(-size // max(ch, 1))
        n += c
    return n


def cmd_fetch(args, cfg) -> int:
    from dbas.fetch.cds import FetchConfig, download_one, enqueue_fetch_tasks, ensure_download_volume
    if args.action == "capture":
        from dbas.fetch.capture import CaptureConfig
        fcfg = FetchConfig.from_yaml(Path(args.config).expanduser())
        output = Path(args.output).expanduser()
        output.mkdir(parents=True, exist_ok=True)
        capture = CaptureConfig(output, max_body_bytes=args.max_body_bytes)
        manifest = {
            "capture_id": capture.capture_id,
            "output": str(output),
            "dataset": fcfg.dataset,
            "request_count": len(fcfg.build_requests()),
            "proxy": {"host": args.host, "port": args.port},
            "network_started": False,
            "credentials": "resolved by cdsapi at runtime; not recorded",
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False))
        return 0
    if args.action == "report":
        from dbas.fetch.capture_report import write_report
        md, summary = write_report(Path(args.capture_dir).expanduser(),
                                   Path(args.output).expanduser())
        print(json.dumps({"report": str(md), "summary": str(summary)}, ensure_ascii=False))
        return 0
    if args.action == "plan":
        fcfg = FetchConfig.from_yaml(Path(args.config).expanduser())
        reqs = fcfg.build_requests()
        print(f"数据集: {fcfg.dataset}")
        print(f"变量: {fcfg.variables}")
        print(f"计划请求数: {len(reqs)}"
              + (f"（split_by={fcfg.split_by}，每块 {fcfg.chunk_days} 天）"
                 if fcfg.split_by else "（每年×每月一个请求）"))
        for r in reqs:
            tag = f"[{r['suffix']}] " if r.get("suffix") else ""
            print(f"  - {r['year']}-{r['month']} {tag}"
                  f"{json.dumps(r['request'], ensure_ascii=False)[:110]}")
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
                return download_one(catalog, params, cfg.root,
                                    capture_dir=getattr(args, "capture_dir", None),
                                    task_id=str(getattr(ctx, "_task_id", "") or "")
                                    if getattr(args, "capture_dir", None) else None)

            sched = cfg.section("scheduler")
            res = cfg.section("resource")
            runner = Runner(cat, {"download": handler},
                            budget_s=args.budget if args.budget is not None else 3600,
                            batch_limit=args.batch_size if args.batch_size is not None else 1000,
                            guard=ResourceGuard(res["max_rss_mb"], res["min_available_mb"]),
                            retry_delay_s=sched["retry_delay_s"],
                            stale_running_s=sched["stale_running_s"], resume=True)
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
    tsub.add_parser("cancel", help="取消未完成任务（废弃写错参数的历史任务）"
                    ).add_argument("task_id")
    tsub.add_parser("prune", help="清理已完成任务（防队列膨胀）")

    sp = sub.add_parser("report", help="生成报告")
    sp.add_argument("kind", nargs="?", default="inventory", choices=["inventory"])

    sp = sub.add_parser("db", help="数据库管理")
    dsub = sp.add_subparsers(dest="action", required=True)
    dsub.add_parser("check")
    dsub.add_parser("backup")
    dsub.add_parser("rebuild", help="显示重建数据库步骤")

    sp = sub.add_parser("subset", help="区域裁切（主高原 ARD 产出）")
    ssub = sp.add_subparsers(dest="action", required=True)
    pl = ssub.add_parser("plan", help="预览相交情况（不读数据，不写库）")
    pl.add_argument("--volume", required=True)
    pl.add_argument("--region", default=None)
    en = ssub.add_parser("enqueue", help="把裁切任务入队")
    en.add_argument("--volume", required=True)
    en.add_argument("--region", default=None)
    en.add_argument("--variables", default=None,
                    help="逗号分隔变量白名单（默认保留全部）")
    en.add_argument("--force", action="store_true", help="已存在的产物也重新生成")
    rn = ssub.add_parser("run", help="执行裁切任务（分批/断点续跑）")
    rn.add_argument("--budget", type=float, default=None)
    rn.add_argument("--batch-size", type=int, default=None)

    sp = sub.add_parser("zarr", help="数组层派生（.nc → Zarr store，登记回 catalog）")
    zsub = sp.add_subparsers(dest="action", required=True)
    zp = zsub.add_parser("plan", help="预览分块方案（不写数据，不写库）")
    zp.add_argument("--source", required=True, help="源 .nc 文件路径")
    zp.add_argument("--target", default=None,
                    help="只看某一种查询模式；省略则列出全部")
    ze = zsub.add_parser("enqueue", help="把数组导出任务入队")
    ze.add_argument("--volume", required=True)
    ze.add_argument("--target", default=None,
                    help="查询模式，逗号分隔（默认取配置 zarr.default_target）")
    ze.add_argument("--region", default=None, help="先按区域裁切再导出（可选）")
    ze.add_argument("--variables", default=None, help="逗号分隔变量白名单")
    ze.add_argument("--shards", type=int, default=None,
                    help="分片倍数：N 个 chunk 合并成一个分片文件（默认不分片）")
    ze.add_argument("--force", action="store_true", help="已存在的 store 也重建")
    zr = zsub.add_parser("run", help="执行数组导出任务（分批/断点续跑）")
    zr.add_argument("--budget", type=float, default=None)
    zr.add_argument("--batch-size", type=int, default=None)
    zv = zsub.add_parser("verify", help="对账 store 的清单哈希是否与登记一致")
    zv.add_argument("--store", required=True,
                    help="数组层卷内相对路径或绝对路径")
    zsub.add_parser("info", help="列出数组层卷内的 store 条目")

    sp = sub.add_parser("dataset", help="数据集分组与识别")
    dsub = sp.add_subparsers(dest="action", required=True)
    dsub.add_parser("list", help="列出数据集及文件/解析/画像计数")
    da = dsub.add_parser("assign", help="按路径规则给卷内文件分配数据集")
    da.add_argument("--volume", required=True)

    sp = sub.add_parser("profile", help="内容画像 + QC（抽样读数据）")
    sp.add_argument("--volume", required=True)
    sp.add_argument("--limit", type=int, default=None,
                    help="本次最多入队/处理文件数（默认全部入队）")
    sp.add_argument("--batch-size", type=int, default=None)
    sp.add_argument("--budget", type=float, default=None,
                    help="单次运行时间预算（秒）")

    sp = sub.add_parser("qc", help="质量检查与报告")
    qsub = sp.add_subparsers(dest="action", required=True)
    qrun = qsub.add_parser("run", help="执行内容画像 + QC 任务")
    qrun.add_argument("--volume", required=True)
    qrun.add_argument("--limit", type=int, default=None)
    qrun.add_argument("--batch-size", type=int, default=None)
    qrun.add_argument("--budget", type=float, default=None)
    qrep = qsub.add_parser("report", help="生成内容画像 + QC 报告")
    qrep.add_argument("--volume", default=None)
    qrep.add_argument("--dataset", default=None)

    sp = sub.add_parser("fetch", help="ERA5 CDS API 下载管理")
    fsub = sp.add_subparsers(dest="action", required=True)
    for name, help_ in (("plan", "预览下载计划（不联网）"),
                        ("enqueue", "把下载计划入队"),
                        ("run", "执行下载任务")):
        f = fsub.add_parser(name, help=help_)
        f.add_argument("--config", required=True, help="fetch 配置 YAML 路径")
        f.add_argument("--budget", type=float, default=None)
        f.add_argument("--batch-size", type=int, default=None)
        if name == "run":
            f.add_argument("--capture-dir", default=None,
                           help="应用层捕获输出目录（可选）")
    cap = fsub.add_parser("capture", help="创建脱敏抓包会话清单（不联网）")
    cap.add_argument("--config", required=True, help="fetch 配置 YAML 路径")
    cap.add_argument("--output", required=True, help="捕获输出目录")
    cap.add_argument("--host", default="cds.climate.copernicus.eu")
    cap.add_argument("--port", type=int, default=443)
    cap.add_argument("--max-body-bytes", type=int, default=16384)
    rep = fsub.add_parser("report", help="离线分析捕获事件")
    rep.add_argument("--capture-dir", required=True)
    rep.add_argument("--output", default="data/reports")
    return p


_DISPATCH = {
    "status": cmd_status, "volume": None, "scan": cmd_scan,
    "verify": cmd_verify, "checksum": cmd_checksum, "tasks": cmd_tasks,
    "report": cmd_report, "db": cmd_db, "fetch": cmd_fetch,
    "subset": cmd_subset, "dataset": cmd_dataset, "profile": cmd_profile,
    "qc": cmd_qc, "zarr": cmd_zarr,
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
