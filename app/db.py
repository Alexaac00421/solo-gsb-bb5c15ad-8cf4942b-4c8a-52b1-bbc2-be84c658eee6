"""SQLite 连接与 schema 初始化。

设计要点：
- 原料 / 配方均为“草稿”，携带单调递增 version，用于乐观锁。
- 配方成分存于 recipe_components，每行要么引用原料，要么引用子配方。
- 标签（labels）一旦发布即不可变：用触发器在数据库层面禁止 UPDATE/DELETE。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ingredients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    allergens   TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，如 ["gluten","egg"]
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS recipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS recipe_components (
    recipe_id      INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,             -- 成分顺序，保证展开路径稳定
    ingredient_id  INTEGER REFERENCES ingredients(id),
    sub_recipe_id  INTEGER REFERENCES recipes(id),
    grams          REAL NOT NULL CHECK (grams > 0),
    PRIMARY KEY (recipe_id, seq),
    CHECK ((ingredient_id IS NULL) <> (sub_recipe_id IS NULL))
);

CREATE TABLE IF NOT EXISTS labels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       INTEGER NOT NULL REFERENCES recipes(id),
    recipe_version  INTEGER NOT NULL,            -- 发布时配方的版本
    total_grams     REAL NOT NULL,
    content         TEXT NOT NULL,               -- JSON：过敏原汇总（含来源路径）
    deps            TEXT NOT NULL,               -- JSON：发布时所用依赖版本快照
    published_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 标签不可变：数据库层面拒绝任何修改 / 删除
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
    """新建一个连接。每请求一个连接，配合 BEGIN IMMEDIATE 保证写串行化。"""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    """启动时初始化数据库（幂等）。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
