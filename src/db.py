"""SQLite 连接与初始化。"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import DDL, SCHEMA_VERSION


def connect(database_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """打开一个开启外键与 WAL 的连接。"""
    path = Path(database_path or os.environ.get("DATABASE_PATH", "data/app.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """建表并写入版本号。"""
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO service_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SCHEMA_VERSION,),
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即开启写事务，配合条件 UPDATE 实现并发不超卖。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
