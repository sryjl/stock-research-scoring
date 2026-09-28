# -*- coding: utf-8 -*-
"""core.py — 纯计算逻辑，无任何 I/O，便于单元测试。

金额计算内部一律保留完整精度；只有需要展示为 A 股价位时才调用
round_half_up 四舍五入，避免连续计算累积误差。
"""
from decimal import Decimal, ROUND_HALF_UP

# 三笔买入档位，全部相对「最初的观察基准价 P」计算。
# 关键规则：每一档都相对 P，绝不链式（第二档不是第一档再乘 0.95）。
TIER_MULTIPLIERS = (1.0, 0.95, 0.90)

# 每笔独立止盈目标相对其「实际成交价」计算。
TAKE_PROFIT_5 = 0.05
TAKE_PROFIT_10 = 0.10

# A股 1 手 = 100 股；数量以「手」为单位，盈亏计算需乘回股数。
SHARES_PER_HAND = 100

# 股票类型
STOCK_TYPE_TRADE = "trade"   # 交易型 / 烟蒂票
STOCK_TYPE_HOLD = "hold"     # 长持型
STOCK_TYPES = (STOCK_TYPE_TRADE, STOCK_TYPE_HOLD)

STOCK_TYPE_LABELS = {
    STOCK_TYPE_TRADE: "交易型",
    STOCK_TYPE_HOLD: "长持型",
}

# 每一笔 Lot 的状态（第一版只做提醒与状态显示，不做自动交易）
LOT_STATUSES = ("未触发", "计划买入", "已买入", "达到 +5%", "达到 +10%", "部分卖出", "已卖出")

# 持仓状态：只有这些状态代表「已买入、持有中」，才计算浮盈浮亏。
# 「未触发」「计划买入」= 还没买，不计算；「已卖出」= 已清仓，不计算。
HOLDING_STATUSES = ("已买入", "达到 +5%", "达到 +10%", "部分卖出")


def is_holding(status):
    """判断某笔状态是否代表「已买入、持有中」。"""
    return status in HOLDING_STATUSES


def round_half_up(value, decimals=2):
    """四舍五入（HALF_UP）到指定小数位，返回 float；None 原样返回。"""
    if value is None:
        return None
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def tier_prices(base_price):
    """根据观察基准价 P 计算三档观察价：[P, P×0.95, P×0.90]（完整精度）。"""
    if base_price is None:
        return [None, None, None]
    return [base_price * m for m in TIER_MULTIPLIERS]


def up_prices(base_price):
    """相对观察基准价 P 的 +5% / +10% 目标价：[P×1.05, P×1.10]（完整精度）。"""
    if base_price is None:
        return [None, None]
    return [base_price * (1 + TAKE_PROFIT_5), base_price * (1 + TAKE_PROFIT_10)]


def target_prices(actual_price):
    """根据某笔实际成交价计算 (+5% 目标价, +10% 目标价)（完整精度）。

    未成交（actual_price 为 None）时返回 (None, None)。
    """
    if actual_price is None:
        return (None, None)
    return (actual_price * (1 + TAKE_PROFIT_5), actual_price * (1 + TAKE_PROFIT_10))


def distance_pct(current_price, base_price):
    """距离观察价百分比 = (当前价 - 观察基准价) / 观察基准价 × 100。"""
    if current_price is None or not base_price:
        return None
    return (current_price - base_price) / base_price * 100.0


def lot_return_pct(current_price, actual_price):
    """某笔已买入 Lot 的当前收益率（百分比数值）。"""
    if current_price is None or not actual_price:
        return None
    return (current_price - actual_price) / actual_price * 100.0


def tp_hint(current_price, actual_price):
    """根据当前价判断某笔已成交 Lot 是否达到 +5% 目标（仅提示，不改状态）。"""
    if current_price is None or not actual_price:
        return None
    if current_price / actual_price >= 1 + TAKE_PROFIT_5:
        return "达到 +5%"
    return None
