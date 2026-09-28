# -*- coding: utf-8 -*-
"""seed.py — 首次启动时写入少量示例数据，便于验证功能。"""
import core
import db


def _add_stock(conn, code, name, stock_type, watch, current, tags, notes="", lots=None):
    s = db.create_stock(conn, {
        "code": code,
        "name": name,
        "stock_type": stock_type,
        "watch_price": watch,
        "current_price": current,
        "tags": tags,
        "notes": notes,
    })
    if lots and s["lots"]:
        # s["lots"] 按 tranche 升序，与 lots 列表顺序一致（第 1/2/3 笔）
        for lot, data in zip(s["lots"], lots):
            if data:
                db.update_lot(conn, lot["id"], data)
    return s


def seed_if_empty(conn):
    if db.list_stocks(conn):
        return False
    _add_stock(
        conn, "600741", "华域汽车", core.STOCK_TYPE_TRADE, 15.00, 14.96, ["烟蒂"],
        "等待回落至 15 元附近分批买入。",
        lots=[
            {"actual_price": 14.98, "quantity": 300, "buy_date": "2026-09-10",
             "status": "已买入", "note": "第一批"},
            None,  # 第二笔：计划 14.25，未成交
            None,  # 第三笔：计划 13.50，未成交
        ],
    )
    _add_stock(
        conn, "000002", "万科A", core.STOCK_TYPE_TRADE, 8.00, 7.68, ["烟蒂", "周期"],
        "地产困境反转，越低越买。",
    )
    _add_stock(
        conn, "600332", "白云山", core.STOCK_TYPE_TRADE, 25.00, 24.05, ["烟蒂", "观察"],
        "医药商业，估值偏低，继续观察。",
    )
    _add_stock(
        conn, "601318", "中国平安", core.STOCK_TYPE_HOLD, 40.00, 42.10, ["长持", "红利"],
        "长期持有，逢低加仓。",
    )
    _add_stock(
        conn, "601939", "建设银行", core.STOCK_TYPE_HOLD, 6.00, 6.32, ["红利", "核心仓"],
        "高股息，作为底仓。",
    )
    _add_stock(
        conn, "601088", "中国神华", core.STOCK_TYPE_HOLD, 35.00, 33.60, ["周期", "红利"],
        "煤炭高分红，等待更低价。",
    )
    return True
