"""SQLite 存储层：连接管理、PRAGMA 设置、迁移机制。

设计要点（见 spec/04-data-model.md §3）：
- WAL 模式支持一写多读；
- 外键约束开启，级联删除生效；
- 自建轻量迁移机制，不引入 Alembic，避免额外依赖；
- 迁移脚本只增不改，已发布的迁移永不修改。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .config import ROOT_DIR, settings

MIGRATIONS_DIR = ROOT_DIR / "migrations"

_local = threading.local()
_write_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(
        str(settings.db_path),
        timeout=15.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,          # 自动提交模式，事务显式控制
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def get_conn() -> sqlite3.Connection:
    """获取当前线程的连接（线程内复用）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def close_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(get_conn().execute(sql, tuple(params)).fetchall())


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    cur = get_conn().execute(sql, tuple(params))
    return cur.fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """执行写语句，返回 lastrowid。"""
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid


def execute_many(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with _write_lock:
        get_conn().executemany(sql, [tuple(p) for p in seq])


def transaction():
    """返回事务上下文管理器（写操作用）。"""
    return _Transaction()


class _Transaction:
    def __enter__(self):
        _write_lock.acquire()
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def __exit__(self, exc_type, exc, tb):
        conn = get_conn()
        try:
            if exc_type is None:
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
        finally:
            _write_lock.release()
        return False


# ------------------------------------------------------------------ 迁移


def _applied_versions() -> set[int]:
    try:
        rows = query("SELECT version FROM schema_version")
    except sqlite3.OperationalError:
        return set()
    return {int(r["version"]) for r in rows}


def run_migrations() -> list[int]:
    """按版本号顺序执行未应用的迁移，返回本次应用的版本列表。"""
    if not MIGRATIONS_DIR.exists():
        return []
    applied = _applied_versions()
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    executed: list[int] = []

    for path in files:
        version = int(path.name.split("_", 1)[0])
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        with _write_lock:
            conn = get_conn()
            # 注意：sqlite3 的 executescript 会先隐式提交已有事务，
            # 因此不能用显式 BEGIN/COMMIT 包裹。改为：先在事务外执行脚本，
            # 再单独写入版本记录。
            try:
                conn.executescript(sql)
            except Exception:
                raise
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )
        executed.append(version)
    return executed


def init_db() -> None:
    """初始化数据库：建表 + 迁移。"""
    settings.ensure_dirs()
    # schema_version 必须先于任何迁移存在
    with _write_lock:
        conn = get_conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version    INTEGER PRIMARY KEY,"
            "  applied_at REAL NOT NULL)"
        )
    run_migrations()
