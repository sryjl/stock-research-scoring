# -*- coding: utf-8 -*-
"""views.py — 把数据库行转成带计算字段的展示结构（纯函数，无 I/O）。"""
import core


def lot_view(lot, current_price):
    # 每一笔只需一个 +5% 目标（相对该笔实际成交价）
    t5, _ = core.target_prices(lot["actual_price"])
    sells = lot.get("sells", [])
    sold_qty = sum((r["quantity"] or 0) for r in sells)
    quantity = lot["quantity"] or 0
    holding = core.is_holding(lot["status"])
    remaining = (quantity - sold_qty) if holding else 0
    realized = 0.0
    for r in sells:
        buy = r.get("buy_price")
        if buy is None:
            buy = lot["actual_price"]
        if buy and r["sell_price"] is not None and r["quantity"]:
            realized += (r["sell_price"] - buy) * r["quantity"] * core.SHARES_PER_HAND
    unrealized = 0.0
    if holding and lot["actual_price"] is not None and current_price is not None and remaining > 0:
        unrealized = (current_price - lot["actual_price"]) * remaining * core.SHARES_PER_HAND
    return {
        "id": lot["id"],
        "tranche": lot["tranche"],
        "plan_price": core.round_half_up(lot["plan_price"], 2),
        "actual_price": lot["actual_price"],
        "quantity": quantity,
        "buy_date": lot["buy_date"],
        "status": lot["status"],
        "note": lot["note"],
        "target_5": core.round_half_up(t5, 3),
        "return_pct": core.round_half_up(core.lot_return_pct(current_price, lot["actual_price"]), 2),
        "tp_hint": core.tp_hint(current_price, lot["actual_price"]) if holding else None,
        "filled": holding,
        "sold_qty": sold_qty,
        "remaining": remaining,
        "realized_pnl": core.round_half_up(realized, 2),
        "unrealized_pnl": core.round_half_up(unrealized, 2),
    }


def stock_view(s):
    is_trade = s["stock_type"] == core.STOCK_TYPE_TRADE
    watch = s["watch_price"]
    current = s["current_price"]

    up_5 = up_10 = None
    tier_views = []
    if is_trade and watch is not None:
        tier_labels = ("观察价", "-5%", "-10%")
        tiers = [core.round_half_up(t, 2) for t in core.tier_prices(watch)]
        tier_views = [
            {"tranche": i + 1, "label": tier_labels[i], "value": tiers[i]}
            for i in range(3)
        ]
        up_5, up_10 = [core.round_half_up(x, 2) for x in core.up_prices(watch)]

    lots = [lot_view(l, current) for l in s.get("lots", [])] if is_trade else []
    realized_pnl = core.round_half_up(sum(l["realized_pnl"] or 0 for l in lots), 2)
    unrealized_pnl = core.round_half_up(sum(l["unrealized_pnl"] or 0 for l in lots), 2)

    return {
        "id": s["id"],
        "code": s["code"],
        "name": s["name"],
        "stock_type": s["stock_type"],
        "stock_type_label": core.STOCK_TYPE_LABELS.get(s["stock_type"], s["stock_type"]),
        "watch_price": watch,
        "current_price": current,
        "notes": s["notes"],
        "tags": s["tags"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "distance_pct": core.round_half_up(core.distance_pct(current, watch), 2),
        "up_5": up_5,
        "up_10": up_10,
        "tiers": tier_views,
        "lots": lots,
        "filled_lots": sum(1 for l in lots if l["filled"]),
        "has_tp": any(l["tp_hint"] for l in lots),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
    }
