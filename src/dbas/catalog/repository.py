"""目录数据库领域操作：卷、文件、任务。

所有写入均为幂等 upsert；重复扫描/重复入队不会产生冲突记录。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbas.catalog.db import connect, current_version, migrate
from dbas.core.exceptions import CatalogError

VOLUME_ONLINE = "online"
VOLUME_OFFLINE = "offline"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def make_file_id(volume_id: str, rel_path: str) -> str:
    return hashlib.sha1(f"{volume_id}/{rel_path}".encode()).hexdigest()


def _d(row) -> dict[str, Any]:
    """sqlite3.Row → dict（Row 不能直接 dict()）。"""
    return dict(zip(row.keys(), row))


# --------------------------------------------------------------------------
# Catalog 封装
# --------------------------------------------------------------------------

class Catalog:
    """目录数据库统一入口（一个实例持有一个连接）。"""

    def __init__(self, db_path: Path, read_only: bool = False):
        self.db_path = db_path
        self.conn = connect(db_path, read_only=read_only)
        if not read_only:
            self.schema_version = migrate(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- 卷 ----------------------------------------------------------------
    def register_volume(self, volume_id: str, root: Path, label: str = "",
                        notes: str = "") -> dict[str, Any]:
        """注册或更新数据卷。root 为该卷当前挂载点。"""
        if not root.is_dir():
            raise CatalogError(f"挂载点不存在或不是目录: {root}")
        now = _now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO volumes(volume_id, label, root_path,
                     last_mount_path, status, first_seen, last_seen, notes)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(volume_id) DO UPDATE SET
                     label=excluded.label,
                     last_mount_path=excluded.last_mount_path,
                     status='online',
                     last_seen=excluded.last_seen,
                     notes=excluded.notes""",
                (volume_id, label or volume_id, str(root), str(root),
                 VOLUME_ONLINE, now, now, notes))
        return self.get_volume(volume_id)

    def get_volume(self, volume_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM volumes WHERE volume_id=?", (volume_id,)).fetchone()
        return _d(row) if row else None

    def list_volumes(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM volumes ORDER BY volume_id")
        return [_d(r) for r in cur.fetchall()]

    def mark_volume(self, volume_id: str, status: str, mount_path: str | None = None) -> None:
        with self.conn:
            if mount_path is not None:
                self.conn.execute(
                    """UPDATE volumes SET status=?, last_mount_path=?,
                       last_seen=COALESCE(?, last_seen) WHERE volume_id=?""",
                    (status, mount_path, _now() if status == VOLUME_ONLINE else None,
                     volume_id))
            else:
                self.conn.execute(
                    "UPDATE volumes SET status=? WHERE volume_id=?",
                    (status, volume_id))

    def resolve_volume_root(self, volume_id: str) -> Path:
        """解析卷的当前可访问根路径。

        优先用注册的 root_path，其次 last_mount_path；都不可用则抛 VolumeOfflineError。
        绝不把某次挂载的路径当作永久有效——每次都现场验证。
        """
        vol = self.get_volume(volume_id)
        if vol is None:
            raise CatalogError(f"卷未注册: {volume_id}")
        candidates = [vol.get("last_mount_path"), vol.get("root_path")]
        for c in candidates:
            if c and Path(c).is_dir():
                return Path(c)
        self.mark_volume(volume_id, VOLUME_OFFLINE)
        raise VolumeOffline(volume_id)

    # -- 文件 ---------------------------------------------------------------
    def upsert_file(self, volume_id: str, rel_path: str, size_bytes: int,
                    mtime: float) -> tuple[str, bool]:
        """盘点发现文件。返回 (file_id, changed)。

        changed=True 表示文件是新增的，或 size/mtime 与库中记录不同——
        调用方应视为需要（重新）解析元数据；已有记录变化时 parse_status
        会被重置为 unparsed。
        """
        file_id = make_file_id(volume_id, rel_path)
        now = _now()
        with self.conn:
            old = self.conn.execute(
                "SELECT size_bytes, mtime FROM files WHERE file_id=?",
                (file_id,)).fetchone()
            if old is None:
                changed = True
                self.conn.execute(
                    """INSERT INTO files(file_id, volume_id, rel_path, file_name,
                         size_bytes, mtime, status, parse_status, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (file_id, volume_id, rel_path, Path(rel_path).name,
                     size_bytes, mtime, "active", "unparsed", now, now))
            else:
                changed = (old["size_bytes"] != size_bytes
                           or abs(old["mtime"] - mtime) > 1e-6)
                self.conn.execute(
                    """UPDATE files SET
                         size_bytes=?, mtime=?, last_seen=?,
                         status=CASE WHEN status IN ('missing_pending','removed')
                                     THEN 'active' ELSE status END,
                         parse_status=CASE WHEN ? THEN 'unparsed'
                                           ELSE parse_status END
                       WHERE file_id=?""",
                    (size_bytes, mtime, now, changed, file_id))
        return file_id, changed

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
        return _d(row) if row else None

    def iter_files(self, volume_id: str | None = None,
                   parse_status: str | None = None,
                   status: str | None = None) -> Any:
        """流式迭代文件记录（sqlite3 游标分批取，不整表载入内存）。"""
        sql = "SELECT * FROM files WHERE 1=1"
        args: list[Any] = []
        if volume_id:
            sql += " AND volume_id=?"
            args.append(volume_id)
        if parse_status:
            sql += " AND parse_status=?"
            args.append(parse_status)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY rel_path"
        cur = self.conn.execute(sql, args)
        while True:
            rows = cur.fetchmany(256)
            if not rows:
                return
            for r in rows:
                yield _d(r)

    def count_files(self, volume_id: str | None = None,
                    status: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM files WHERE 1=1"
        args: list[Any] = []
        if volume_id:
            sql += " AND volume_id=?"
            args.append(volume_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        return self.conn.execute(sql, args).fetchone()[0]

    def mark_missing_pending(self, volume_id: str, rel_paths: list[str]) -> int:
        """仅在「卷在线且扫描完整成功」后调用：把本次未发现的既有文件
        标记为 missing_pending（待确认缺失），绝不直接置为已删除。"""
        if not rel_paths:
            return 0
        now = _now()
        n = 0
        with self.conn:
            for rp in rel_paths:
                cur = self.conn.execute(
                    """UPDATE files SET status='missing_pending',
                       last_check_result='missing_in_last_scan', last_check=?
                       WHERE volume_id=? AND rel_path=? AND status='active'""",
                    (now, volume_id, rp))
                n += cur.rowcount
        return n

    def update_parse_result(self, file_id: str, *, fmt: str | None,
                            time_min: str | None, time_max: str | None,
                            lat: tuple[float, float] | None,
                            lon: tuple[float, float] | None,
                            variables: list[dict[str, Any]],
                            dimensions: list[dict[str, Any]]) -> None:
        now = _now()
        with self.conn:
            self.conn.execute(
                """UPDATE files SET parse_status='parsed', parse_error=NULL,
                   format=?, time_min=?, time_max=?,
                   lat_min=?, lat_max=?, lon_min=?, lon_max=?
                   WHERE file_id=?""",
                (fmt, time_min, time_max,
                 lat[0] if lat else None, lat[1] if lat else None,
                 lon[0] if lon else None, lon[1] if lon else None,
                 file_id))
            self.conn.execute("DELETE FROM variables WHERE file_id=?", (file_id,))
            for v in variables:
                self.conn.execute(
                    """INSERT OR REPLACE INTO variables
                       (file_id, name, dims, dtype, units, standard_name,
                        long_name, fill_value)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (file_id, v.get("name"), v.get("dims"), v.get("dtype"),
                     v.get("units"), v.get("standard_name"),
                     v.get("long_name"), v.get("fill_value")))
            self.conn.execute("DELETE FROM dimensions WHERE file_id=?", (file_id,))
            for d in dimensions:
                self.conn.execute(
                    "INSERT OR REPLACE INTO dimensions(file_id,name,size) VALUES (?,?,?)",
                    (file_id, d["name"], d["size"]))

    def mark_parse_failed(self, file_id: str, error: str, fmt: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE files SET parse_status='failed', parse_error=?,
                   format=COALESCE(?, format) WHERE file_id=?""",
                (error[:500], fmt, file_id))

    def set_fingerprint(self, file_id: str, algo: str, digest: str) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE files SET fingerprint_algo=?, fingerprint=?,
                   fingerprint_at=? WHERE file_id=?""",
                (algo, digest, _now(), file_id))

    def set_check_result(self, file_id: str, result: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE files SET last_check=?, last_check_result=?, check_error=?
                   WHERE file_id=?""", (_now(), result, error, file_id))

    def mark_volume_files_offline(self, volume_id: str) -> None:
        """卷离线时，把其下 active 文件标记为 offline（不是删除）。"""
        with self.conn:
            self.conn.execute(
                """UPDATE files SET status='offline'
                   WHERE volume_id=? AND status='active'""", (volume_id,))

    # -- 扫描运行记录 ---------------------------------------------------------
    def start_scan_run(self, mode: str, params: dict[str, Any]) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self.conn:
            self.conn.execute(
                """INSERT INTO scan_runs(run_id, mode, status, started_at, params_json)
                   VALUES (?,?,?,?,?)""",
                (run_id, mode, "running", _now(), json.dumps(params, ensure_ascii=False)))
        return run_id

    def finish_scan_run(self, run_id: str, status: str, files_seen: int = 0,
                        files_ok: int = 0, files_failed: int = 0,
                        notes: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE scan_runs SET status=?, finished_at=?, files_seen=?,
                   files_ok=?, files_failed=?, notes=? WHERE run_id=?""",
                (status, _now(), files_seen, files_ok, files_failed, notes, run_id))

    def get_scan_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM scan_runs WHERE run_id=?", (run_id,)).fetchone()
        return _d(row) if row else None

    def list_scan_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [_d(r) for r in cur.fetchall()]

    # -- 任务队列 --------------------------------------------------------------
    def enqueue_task(self, type_: str, params: dict[str, Any], *,
                     dedup_key: str | None = None,
                     priority: int = 100, max_attempts: int = 3) -> tuple[str, bool]:
        """入队任务。dedup_key 相同的任务已存在（未取消）则跳过。
        返回 (task_id, created)。"""
        now = _now()
        task_id = uuid.uuid4().hex[:12]
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO tasks(task_id, type, params_json, dedup_key,
                         priority, max_attempts, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (task_id, type_, json.dumps(params, ensure_ascii=False),
                     dedup_key, priority, max_attempts, "pending", now, now))
            return task_id, True
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "SELECT task_id FROM tasks WHERE dedup_key=?", (dedup_key,)).fetchone()
            return (row[0] if row else ""), False

    def claim_task(self, worker: str, types: list[str] | None = None) -> dict[str, Any] | None:
        """原子认领一个 pending/retry_wait 且到达重试时间的任务。"""
        now = _now()
        type_sql = ""
        select_args: list[Any] = [now]
        if types:
            type_sql = f" AND type IN ({','.join('?' * len(types))})"
            select_args += types
        with self.conn:
            row = self.conn.execute(
                f"""SELECT task_id FROM tasks
                    WHERE status IN ('pending','retry_wait')
                      AND updated_at <= ?            -- retry_wait 需延迟到期
                    {type_sql}
                    ORDER BY priority, created_at LIMIT 1""",
                select_args).fetchone()
            if row is None:
                return None
            task_id = row[0]
            cur = self.conn.execute(
                """UPDATE tasks SET status='running', claimed_by=?, claimed_at=?,
                   heartbeat_at=?, attempts=attempts+1, updated_at=?
                   WHERE task_id=? AND status IN ('pending','retry_wait')""",
                (worker, now, now, now, task_id))
            if cur.rowcount != 1:      # 并发下被别人抢走
                return None
        task = self.get_task(task_id)
        assert task is not None
        return task

    def heartbeat_task(self, task_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE task_id=?",
                (_now(), _now(), task_id))

    def finish_task(self, task_id: str, status: str, *,
                    error: str | None = None, result: dict[str, Any] | None = None,
                    attempt_id: int | None = None,
                    attempt_stats: dict[str, Any] | None = None) -> None:
        now = _now()
        with self.conn:
            self.conn.execute(
                """UPDATE tasks SET status=?, last_error=?, result_json=?,
                   updated_at=?, claimed_by=NULL, heartbeat_at=NULL WHERE task_id=?""",
                (status, error,
                 json.dumps(result, ensure_ascii=False) if result else None,
                 now, task_id))
            if attempt_id is not None:
                self.conn.execute(
                    """UPDATE task_attempts SET finished_at=?, status=?, error=?,
                       stats_json=? WHERE attempt_id=?""",
                    (now, status if status in ("succeeded", "failed") else "failed",
                     error, json.dumps(attempt_stats or {}, ensure_ascii=False),
                     attempt_id))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return _d(row) if row else None

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [_d(r) for r in self.conn.execute(sql, args)]

    def count_tasks_by_status(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status")
        return {r[0]: r[1] for r in cur.fetchall()}

    def start_attempt(self, task_id: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO task_attempts(task_id, started_at, status)
                   VALUES (?,?,'running')""", (task_id, _now()))
            return int(cur.lastrowid or 0)

    def reclaim_stale_tasks(self, stale_s: int) -> list[str]:
        """把心跳超时的 running 任务重置为 pending（进程被杀后的自愈）。"""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - stale_s))
        rows = self.conn.execute(
            """SELECT task_id FROM tasks WHERE status='running'
               AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
            (cutoff,)).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            with self.conn:
                self.conn.execute(
                    f"""UPDATE tasks SET status='pending', claimed_by=NULL,
                        heartbeat_at=NULL, updated_at=?
                        WHERE task_id IN ({','.join('?' * len(ids))})""",
                    [_now(), *ids])
        return ids

    def reset_running_tasks(self) -> list[str]:
        """启动时把所有 running 任务回收（单进程模式下安全）。"""
        with self.conn:
            rows = self.conn.execute(
                "SELECT task_id FROM tasks WHERE status='running'").fetchall()
            ids = [r[0] for r in rows]
            if ids:
                self.conn.execute(
                    f"""UPDATE tasks SET status='pending', claimed_by=NULL,
                        heartbeat_at=NULL, updated_at=?
                        WHERE task_id IN ({','.join('?' * len(ids))})""",
                    [_now(), *ids])
        return ids

    # -- 统计 -------------------------------------------------------------------
    def status_summary(self) -> dict[str, Any]:
        return {
            "schema_version": current_version(self.conn),
            "volumes": self.list_volumes(),
            "files_by_status": self._group_count("files", "status"),
            "files_by_parse": self._group_count("files", "parse_status"),
            "tasks_by_status": self.count_tasks_by_status(),
            "recent_scan_runs": self.list_scan_runs(5),
        }

    def _group_count(self, table: str, col: str) -> dict[str, int]:
        cur = self.conn.execute(
            f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col}")
        return {r[0]: r[1] for r in cur.fetchall()}


class VolumeOffline(CatalogError):
    """数据卷当前不可访问。"""

    def __init__(self, volume_id: str):
        self.volume_id = volume_id
        super().__init__(
            f"数据卷 {volume_id} 当前不可访问（未挂载或路径已变化）。"
            f"卷下文件不会被误标为已删除；挂载后重试即可。")
