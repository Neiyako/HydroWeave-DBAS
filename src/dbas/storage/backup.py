"""SQLite 在线备份与备份后校验。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _check_connection(conn: sqlite3.Connection) -> tuple[str, list[tuple]]:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    return integrity, foreign_keys


def backup_database(source_path: str | Path, destination_path: str | Path) -> Path:
    """将在线 SQLite 数据库备份到 destination，并原子替换正式文件。

    在线备份 API 允许源库继续使用；备份期间先写同目录 ``.part``，校验通过后
    才改名。这样即使进程被杀，也不会留下一个看起来完整但实际截断的备份。
    """
    source = Path(source_path)
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)

    src_conn = sqlite3.connect(str(source), timeout=30)
    dst_conn = sqlite3.connect(str(temporary), timeout=30)
    try:
        src_conn.backup(dst_conn)
        dst_conn.commit()
        integrity, foreign_keys = _check_connection(dst_conn)
        if integrity != "ok" or foreign_keys:
            raise sqlite3.DatabaseError(
                f"备份校验失败: integrity={integrity}, foreign_keys={foreign_keys}")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        dst_conn.close()
        src_conn.close()

    os.replace(temporary, destination)
    return destination
