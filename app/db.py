"""SQLite 连接与 schema 初始化。"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    allergens  TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，如 ["gluten","milk"]
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_components (
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    ref_type  TEXT NOT NULL CHECK (ref_type IN ('ingredient', 'recipe')),
    ref_id    INTEGER NOT NULL,
    grams     REAL NOT NULL CHECK (grams > 0),
    PRIMARY KEY (recipe_id, seq)
);

CREATE TABLE IF NOT EXISTS labels (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id      INTEGER NOT NULL REFERENCES recipes(id),
    recipe_version INTEGER NOT NULL,
    snapshot       TEXT NOT NULL,            -- JSON：过敏原汇总 + 依赖版本
    created_at     TEXT NOT NULL
);

-- 标签不可变：禁止 UPDATE / DELETE
CREATE TRIGGER IF NOT EXISTS labels_no_update
BEFORE UPDATE ON labels
BEGIN
    SELECT RAISE(ABORT, 'labels are immutable');
END;

CREATE TRIGGER IF NOT EXISTS labels_no_delete
BEFORE DELETE ON labels
BEGIN
    SELECT RAISE(ABORT, 'labels are immutable');
END;
"""


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。autocommit 模式，事务由业务层显式 BEGIN/COMMIT 控制。"""
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path: str) -> None:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
