import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,              -- readings | swaps | supplement
    filename      TEXT,
    status        TEXT NOT NULL,              -- applied | rejected
    message       TEXT,
    rows_received INTEGER NOT NULL DEFAULT 0,
    rows_applied  INTEGER NOT NULL DEFAULT 0,
    order_id      INTEGER,                    -- 补抄单批次所属的补抄单
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    point     TEXT NOT NULL,
    ts        TEXT NOT NULL,                  -- 规范化的 UTC 时刻，定宽字符串，字典序即时间序
    value     TEXT NOT NULL,                  -- 十进制累计读数（kWh），以文本保存避免浮点误差
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    PRIMARY KEY (point, ts)
);

CREATE TABLE IF NOT EXISTS swaps (
    point     TEXT NOT NULL,
    ts        TEXT NOT NULL,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    PRIMARY KEY (point, ts)
);

-- 补抄单：同一点同一 UTC 日唯一；再次开单返回原单
CREATE TABLE IF NOT EXISTS supplement_orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    point      TEXT NOT NULL,
    day        TEXT NOT NULL,                 -- YYYY-MM-DD（UTC）
    created_at TEXT NOT NULL,
    UNIQUE (point, day)
);

-- 开单时快照下来的缺读数整点（该 UTC 日 00:00 ~ 23:00 中当时没有读数的时刻）
CREATE TABLE IF NOT EXISTS supplement_slots (
    order_id INTEGER NOT NULL REFERENCES supplement_orders(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,                   -- 规范化的整点 UTC 时刻
    PRIMARY KEY (order_id, ts)
);

-- 现场回报的核验记录：即使与已入库读数同值也登记
CREATE TABLE IF NOT EXISTS verifications (
    order_id   INTEGER NOT NULL REFERENCES supplement_orders(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    value      TEXT NOT NULL,                 -- 现场回报的十进制读数
    upload_id  INTEGER NOT NULL REFERENCES uploads(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (order_id, ts)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_uploads_order ON uploads(order_id);
CREATE INDEX IF NOT EXISTS idx_supplement_slots_order ON supplement_slots(order_id);
CREATE INDEX IF NOT EXISTS idx_verifications_order ON verifications(order_id);
"""


# 旧库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里按需 ALTER。
MIGRATIONS = [
    ("uploads", "order_id", "ALTER TABLE uploads ADD COLUMN order_id INTEGER"),
]


def _migrate(conn):
    for table, column, ddl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)



def get_db():
    """每个请求一个连接；autocommit 模式，事务由代码显式控制。"""
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # autocommit；BEGIN/COMMIT 显式管理
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        g.db = conn
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    conn = sqlite3.connect(app.config["DATABASE"])
    # WAL：读写互不阻塞，读事务始终看到一致的快照
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
    conn.commit()
    conn.close()
