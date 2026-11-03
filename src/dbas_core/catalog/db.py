"""SQLite 目录数据库：连接管理、版本化 schema 与迁移。

设计说明：
- schema_migrations 表记录已应用版本，migrations 列表按序执行；新版本只需追加函数。
- 连接统一走 connect()：开启外键、WAL、busy_timeout，降低并发与中断风险。
- 文件本体（原始 .nc）与元数据（本数据库）严格分离；数据库可随时由原始文件重建。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from dbas_core.exceptions import CatalogError

SCHEMA_VERSION = 2

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volumes (
    volume_id       TEXT PRIMARY KEY,          -- 用户指定的稳定逻辑 ID
    label           TEXT,
    root_path       TEXT NOT NULL,             -- 首次注册时的路径（仅作参考）
    last_mount_path TEXT,                      -- 最近一次确认在线时的挂载路径
    status          TEXT NOT NULL DEFAULT 'unknown',  -- online/offline/unknown
    first_seen      TEXT NOT NULL,
    last_seen       TEXT,                      -- 最近一次确认在线时间
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS files (
    file_id           TEXT PRIMARY KEY,        -- sha1(volume_id + '/' + rel_path)
    volume_id         TEXT NOT NULL REFERENCES volumes(volume_id),
    rel_path          TEXT NOT NULL,           -- 卷内相对路径（POSIX 风格）
    file_name         TEXT NOT NULL,
    size_bytes        INTEGER,
    mtime             REAL,
    format            TEXT,                    -- nc3/nc4/hdf5/unknown，解析后填充
    status            TEXT NOT NULL DEFAULT 'new',
                      -- new/active/missing_pending/offline/removed
    parse_status      TEXT NOT NULL DEFAULT 'unparsed',
                      -- unparsed/parsed/failed/not_netcdf/empty
    parse_error       TEXT,
    time_min          TEXT, time_max TEXT,     -- ISO 时间范围，无法解析为 NULL
    lat_min REAL, lat_max REAL, lon_min REAL, lon_max REAL,
    fingerprint_algo  TEXT,                    -- sha256
    fingerprint       TEXT,                    -- NULL = 尚未计算（≠ 空内容）
    fingerprint_at    TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT,                    -- 最近一次扫描中被再次发现
    last_check        TEXT,
    last_check_result TEXT,                    -- ok/failed/offline/not_checked
    check_error       TEXT,
    UNIQUE(volume_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_volume ON files(volume_id, status);
CREATE INDEX IF NOT EXISTS idx_files_parse ON files(parse_status);

CREATE TABLE IF NOT EXISTS variables (
    file_id      TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    dims         TEXT,                         -- 逗号分隔维度名
    dtype        TEXT,
    units        TEXT,
    standard_name TEXT,
    long_name    TEXT,
    fill_value   TEXT,                         -- 以文本存，保留原语义
    PRIMARY KEY (file_id, name)
);

CREATE TABLE IF NOT EXISTS dimensions (
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    size    INTEGER NOT NULL,
    PRIMARY KEY (file_id, name)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id          TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
                    -- running/done/budget_exhausted/aborted
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    params_json     TEXT,
    files_seen      INTEGER DEFAULT 0,
    files_ok        INTEGER DEFAULT 0,
    files_failed    INTEGER DEFAULT 0,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    type         TEXT NOT NULL,                -- scan_metadata/verify/checksum/download
    params_json  TEXT NOT NULL,
    dedup_key    TEXT UNIQUE,                  -- 类型+参数摘要，防重复入队
    status       TEXT NOT NULL DEFAULT 'pending',
                 -- pending/running/succeeded/failed/retry_wait/cancelled/blocked
    priority     INTEGER NOT NULL DEFAULT 100,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    claimed_by   TEXT,
    claimed_at   TEXT,
    heartbeat_at TEXT,
    last_error   TEXT,
    result_json  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority);

CREATE TABLE IF NOT EXISTS task_attempts (
    attempt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,                 -- running/succeeded/failed
    error       TEXT,
    stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id     TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,                 -- report/download/backup
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    checksum    TEXT
);

CREATE TABLE IF NOT EXISTS quality_reports (
    report_id   TEXT PRIMARY KEY,
    target      TEXT NOT NULL,
    level       TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    severity    TEXT NOT NULL,
    evidence_json TEXT,
    needs_human INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
"""),
    (2, """
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id  TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT,
    source      TEXT,
    created_at  TEXT NOT NULL
);
INSERT OR IGNORE INTO datasets(dataset_id, label, description, source, created_at) VALUES
 ('era5_pressure', 'ERA5 0.25° 气压层日文件',
  't/q/u/v 四个气压层变量，37 层，逐小时，按日分文件', 'ERA5 / CDS', datetime('now')),
 ('modis_air_quality', 'MODIS 经纬度网格大气成分产品',
  'CO/NO2/O3/PM1/PM2.5/PM10/SO2 等网格产品', 'MODIS / ECHAP', datetime('now')),
 ('eraint_wind', 'ERA-Interim 10m 风场',
  'u10/v10/si10 全球网格', 'ERA-Interim', datetime('now')),
 ('unknown', '未识别数据集', '未匹配到内置识别规则的数据集', '', datetime('now'));

ALTER TABLE files ADD COLUMN dataset_id TEXT REFERENCES datasets(dataset_id);
CREATE INDEX IF NOT EXISTS idx_files_dataset ON files(dataset_id);

ALTER TABLE variables ADD COLUMN scale_factor REAL;
ALTER TABLE variables ADD COLUMN add_offset REAL;
ALTER TABLE quality_reports ADD COLUMN dataset_id TEXT;
ALTER TABLE quality_reports ADD COLUMN volume_id TEXT;
CREATE INDEX IF NOT EXISTS idx_qr_target ON quality_reports(target);
CREATE INDEX IF NOT EXISTS idx_qr_dataset ON quality_reports(dataset_id);
CREATE INDEX IF NOT EXISTS idx_qr_volume ON quality_reports(volume_id);

CREATE TABLE IF NOT EXISTS file_profiles (
    file_id       TEXT PRIMARY KEY REFERENCES files(file_id) ON DELETE CASCADE,
    dataset_id    TEXT,
    profiled_at   TEXT NOT NULL,
    format        TEXT,
    size_bytes    INTEGER,
    expected_bytes INTEGER,                 -- nc3 可按维度/类型推算，用于截断检测
    n_vars        INTEGER,
    time_n        INTEGER,
    time_min      TEXT,
    time_max      TEXT,
    time_units    TEXT,
    time_calendar TEXT,
    time_non_increasing INTEGER NOT NULL DEFAULT 0,
    time_zero_count     INTEGER NOT NULL DEFAULT 0,
    lat_sig       TEXT,
    lon_sig       TEXT,
    level_sig     TEXT,
    lat_min REAL, lat_max REAL, lon_min REAL, lon_max REAL,
    level_n INTEGER, level_min REAL, level_max REAL,
    tail_suspect  INTEGER NOT NULL DEFAULT 0, -- 末时次为常数而首时次不是
    warnings_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_profiles_dataset ON file_profiles(dataset_id);

CREATE TABLE IF NOT EXISTS variable_profiles (
    file_id      TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    var_name     TEXT NOT NULL,
    units        TEXT,
    dtype        TEXT,
    scale_factor REAL,
    add_offset   REAL,
    n_total      INTEGER,
    n_samples    INTEGER,
    n_masked     INTEGER,
    min_value    REAL,
    max_value    REAL,
    mean_value   REAL,
    std_value    REAL,
    negative_count INTEGER NOT NULL DEFAULT 0,
    constant_field INTEGER NOT NULL DEFAULT 0,
    tail_constant  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_id, var_name)
);
CREATE INDEX IF NOT EXISTS idx_var_profiles_name ON variable_profiles(var_name);
"""),
]


def connect(db_path: Path, read_only: bool = False) -> sqlite3.Connection:
    """打开数据库连接。所有调用必须经由本函数以获得一致 PRAGMA。"""
    try:
        if read_only:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        raise CatalogError(f"无法打开数据库 {db_path}: {e}") from e


def migrate(conn: sqlite3.Connection) -> int:
    """按序应用未执行的迁移，返回最终 schema 版本。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        with conn:  # 单个迁移一个事务
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?,?)",
                (version, _now()))
    conn.commit()
    return current_version(conn)


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()
        return row[0] or 0
    except sqlite3.Error:
        return 0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
