# -*- coding: utf-8 -*-
"""db.py — SQLite 数据访问层。

表结构可扩展：每一笔 Lot 都是独立记录，不做「三笔写死成几十个字段」。
"""
import os
import sqlite3
from datetime import datetime

import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "stocks.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    stock_type    TEXT NOT NULL DEFAULT 'trade',  -- trade=交易型/烟蒂, hold=长持型
    watch_price   REAL,                           -- 观察基准价 P
    current_price REAL,                           -- 当前价（手动维护）
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_tags (
    stock_id INTEGER NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (stock_id, tag_id)
);

-- 每一笔买入都是独立 Lot；actual_price 为空表示「尚未成交」。
CREATE TABLE IF NOT EXISTS lots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id     INTEGER NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
    tranche      INTEGER NOT NULL,          -- 第几笔：1/2/3
    plan_price   REAL,                      -- 计划买入价（观察价变化时只重算未成交的）
    actual_price REAL,                      -- 实际成交价（NULL = 尚未成交）
    quantity     INTEGER,                   -- 数量（股）
    buy_date     TEXT,                      -- 买入日期 YYYY-MM-DD
    status       TEXT NOT NULL DEFAULT '未触发',
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (stock_id, tranche)
);

CREATE TABLE IF NOT EXISTS sell_records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id     INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    sell_price REAL NOT NULL,
    quantity   INTEGER,
    sell_date  TEXT,
    buy_price  REAL,          -- 成本价快照（用于累计盈利）
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_TAGS = ("烟蒂", "长持", "红利", "核心仓", "周期", "观察")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    path = path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn):
    """对已存在的旧库做增量迁移（幂等）。"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(sell_records)")]
    if "buy_price" not in cols:
        conn.execute("ALTER TABLE sell_records ADD COLUMN buy_price REAL")
    # 一次性迁移：数量单位从「股」改为「手」（100 的倍数除以 100）
    if get_setting(conn, "units_hand") != "1":
        conn.execute(
            "UPDATE lots SET quantity = quantity / 100 WHERE quantity IS NOT NULL AND quantity % 100 = 0"
        )
        conn.execute(
            "UPDATE sell_records SET quantity = quantity / 100 WHERE quantity IS NOT NULL AND quantity % 100 = 0"
        )
        set_setting(conn, "units_hand", "1")


def init_db(path=None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    for name in DEFAULT_TAGS:
        conn.execute(
            "INSERT OR IGNORE INTO tags(name, created_at) VALUES (?, ?)", (name, _now())
        )
    conn.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES ('schema_version', '1')"
    )
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# 数值转换辅助
# --------------------------------------------------------------------------- #
def _num_or_none(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 标签
# --------------------------------------------------------------------------- #
def list_tags(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM tags ORDER BY name")]


def create_tag(conn, name):
    name = (name or "").strip()
    if not name:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO tags(name, created_at) VALUES (?, ?)", (name, _now())
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM tags WHERE name = ?", (name,)).fetchone())


def _set_stock_tags(conn, stock_id, tag_names):
    conn.execute("DELETE FROM stock_tags WHERE stock_id = ?", (stock_id,))
    for n in (tag_names or []):
        n = (n or "").strip()
        if not n:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO tags(name, created_at) VALUES (?, ?)", (n, _now())
        )
        tid = conn.execute("SELECT id FROM tags WHERE name = ?", (n,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO stock_tags(stock_id, tag_id) VALUES (?, ?)",
            (stock_id, tid),
        )


# --------------------------------------------------------------------------- #
# Lot
# --------------------------------------------------------------------------- #
def _create_default_lots(conn, stock_id, watch_price, stock_type):
    if stock_type == core.STOCK_TYPE_HOLD:
        return  # 长持型不生成三笔
    plans = core.tier_prices(watch_price)
    now = _now()
    for tranche in (1, 2, 3):
        conn.execute(
            "INSERT INTO lots(stock_id, tranche, plan_price, status, created_at, updated_at) "
            "VALUES (?, ?, ?, '未触发', ?, ?)",
            (stock_id, tranche, plans[tranche - 1], now, now),
        )


def _recompute_unfilled_plan_prices(conn, stock_id, old_watch, new_watch):
    """观察价变化时：只重算「尚未成交」的 Lot 计划价。

    - 已成交 Lot 的历史数据绝不改动。
    - 未成交 Lot：仅当计划价仍是「旧观察价的自动档位」时重算；
      用户手动改过的计划价保持不变。
    """
    if new_watch is None:
        return
    now = _now()
    rows = conn.execute(
        "SELECT id, tranche, actual_price, plan_price FROM lots WHERE stock_id = ? ORDER BY tranche",
        (stock_id,),
    ).fetchall()
    for r in rows:
        if r["actual_price"] is not None or not (1 <= r["tranche"] <= 3):
            continue
        old_auto = old_watch * core.TIER_MULTIPLIERS[r["tranche"] - 1] if old_watch else None
        if old_auto is not None and (r["plan_price"] is None or abs(r["plan_price"] - old_auto) < 1e-9):
            new_plan = new_watch * core.TIER_MULTIPLIERS[r["tranche"] - 1]
            conn.execute(
                "UPDATE lots SET plan_price = ?, updated_at = ? WHERE id = ?",
                (new_plan, now, r["id"]),
            )


def get_lot(conn, lot_id):
    r = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    return dict(r) if r else None


def update_lot(conn, lot_id, data):
    row = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    if row is None:
        return None
    fields = {}
    if "actual_price" in data:
        fields["actual_price"] = _num_or_none(data["actual_price"])
    if "plan_price" in data:
        fields["plan_price"] = _num_or_none(data["plan_price"])
    if "quantity" in data:
        fields["quantity"] = _int_or_none(data["quantity"])
    if "buy_date" in data:
        fields["buy_date"] = data["buy_date"] or None
    if "status" in data:
        fields["status"] = data["status"]
    if "note" in data:
        fields["note"] = data["note"] or ""
    if fields:
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [lot_id]
        conn.execute(f"UPDATE lots SET {sets} WHERE id = ?", vals)
    conn.commit()
    return get_lot(conn, lot_id)


# --------------------------------------------------------------------------- #
# 卖出记录
# --------------------------------------------------------------------------- #
def list_sell_records(conn, lot_id):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM sell_records WHERE lot_id = ? ORDER BY sell_date, id", (lot_id,)
        )
    ]


def add_sell_record(conn, lot_id, data):
    lot = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    if lot is None:
        return None
    cur = conn.execute(
        "INSERT INTO sell_records(lot_id, sell_price, quantity, sell_date, buy_price, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            lot_id,
            _num_or_none(data.get("sell_price")),
            _int_or_none(data.get("quantity")),
            data.get("sell_date") or None,
            lot["actual_price"],  # 成本价快照
            data.get("note") or "",
            _now(),
        ),
    )
    _maybe_archive_lot(conn, lot)
    conn.commit()
    return dict(conn.execute("SELECT * FROM sell_records WHERE id = ?", (cur.lastrowid,)).fetchone())


def _maybe_archive_lot(conn, lot):
    """卖出后判断：全部卖出则归档（重置为空白，保留计划价）；部分卖出则标记状态。"""
    sold = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS s FROM sell_records WHERE lot_id = ?", (lot["id"],)
    ).fetchone()["s"]
    if lot["actual_price"] is None:
        return
    if lot["quantity"] and sold >= lot["quantity"]:
        conn.execute(
            "UPDATE lots SET actual_price = NULL, quantity = NULL, buy_date = NULL, "
            "status = '未触发', updated_at = ? WHERE id = ?",
            (_now(), lot["id"]),
        )
    elif sold > 0:
        conn.execute(
            "UPDATE lots SET status = '部分卖出', updated_at = ? WHERE id = ?",
            (_now(), lot["id"]),
        )


def delete_sell_record(conn, sell_id):
    conn.execute("DELETE FROM sell_records WHERE id = ?", (sell_id,))
    conn.commit()
    return True


# --------------------------------------------------------------------------- #
# 股票
# --------------------------------------------------------------------------- #
def _stock_with_children(conn, row):
    s = dict(row)
    s["tags"] = [
        t["name"]
        for t in conn.execute(
            "SELECT t.name FROM tags t JOIN stock_tags st ON st.tag_id = t.id "
            "WHERE st.stock_id = ? ORDER BY t.name",
            (row["id"],),
        )
    ]
    lots = []
    for x in conn.execute("SELECT * FROM lots WHERE stock_id = ? ORDER BY tranche", (row["id"],)):
        lot = dict(x)
        lot["sells"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sell_records WHERE lot_id = ? ORDER BY sell_date, id", (lot["id"],)
            )
        ]
        lots.append(lot)
    s["lots"] = lots
    return s


def list_stocks(conn):
    return [
        _stock_with_children(conn, r)
        for r in conn.execute("SELECT * FROM stocks ORDER BY id")
    ]


def get_stock(conn, stock_id):
    r = conn.execute("SELECT * FROM stocks WHERE id = ?", (stock_id,)).fetchone()
    return _stock_with_children(conn, r) if r else None


def create_stock(conn, data):
    now = _now()
    stock_type = data.get("stock_type", core.STOCK_TYPE_TRADE)
    if stock_type not in core.STOCK_TYPES:
        stock_type = core.STOCK_TYPE_TRADE
    watch = _num_or_none(data.get("watch_price"))
    current = _num_or_none(data.get("current_price"))
    cur = conn.execute(
        "INSERT INTO stocks(code, name, stock_type, watch_price, current_price, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (data.get("code") or "").strip(),
            (data.get("name") or "").strip(),
            stock_type,
            watch,
            current,
            data.get("notes") or "",
            now,
            now,
        ),
    )
    stock_id = cur.lastrowid
    _set_stock_tags(conn, stock_id, data.get("tags") or [])
    _create_default_lots(conn, stock_id, watch, stock_type)
    conn.commit()
    return get_stock(conn, stock_id)


def update_stock(conn, stock_id, data):
    row = conn.execute("SELECT * FROM stocks WHERE id = ?", (stock_id,)).fetchone()
    if row is None:
        return None
    old_watch = row["watch_price"]
    old_type = row["stock_type"]

    new_watch = _num_or_none(data["watch_price"]) if "watch_price" in data else old_watch
    new_type = old_type
    if "stock_type" in data and data["stock_type"] in core.STOCK_TYPES:
        new_type = data["stock_type"]

    fields = {}
    if "name" in data:
        fields["name"] = (data["name"] or "").strip()
    if "stock_type" in data and data["stock_type"] in core.STOCK_TYPES:
        fields["stock_type"] = new_type
    if "watch_price" in data:
        fields["watch_price"] = new_watch
    if "current_price" in data:
        fields["current_price"] = _num_or_none(data["current_price"])
    if "notes" in data:
        fields["notes"] = data["notes"] or ""
    if fields:
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE stocks SET {sets} WHERE id = ?", list(fields.values()) + [stock_id])

    if "tags" in data:
        _set_stock_tags(conn, stock_id, data["tags"])

    # 观察价 / 类型变化后的 Lot 处理
    if new_type == core.STOCK_TYPE_TRADE:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM lots WHERE stock_id = ?", (stock_id,)).fetchone()["c"]
        if cnt == 0:
            _create_default_lots(conn, stock_id, new_watch, core.STOCK_TYPE_TRADE)
        elif new_watch != old_watch:
            _recompute_unfilled_plan_prices(conn, stock_id, old_watch, new_watch)

    conn.commit()
    return get_stock(conn, stock_id)


def delete_stock(conn, stock_id):
    conn.execute("DELETE FROM stocks WHERE id = ?", (stock_id,))
    conn.commit()
    return True


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
