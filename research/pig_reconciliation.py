"""猪企财报与销售简报勾稽、成本就绪检查及只读周期槽位预演。"""

from .cyclical_slots import infer_pig_cost, infer_unit_margin, resolve_slots


def reconcile_annual_pig_evidence(segment, bulletin, *, sold_live_weight=None,
                                  adjusted_operating_profit=None):
    """不把数值差额自动解释为审计调整，更不以头数或均价反推重量。"""
    if segment.get("status") != "extracted" or bulletin.get("status") != "extracted":
        return {"status": "insufficient_sources", "cost_ready": False,
                "blockers": ["年度分产品表或全年销售简报不可用"]}
    revenue = segment["revenue"]
    bulletin_revenue = bulletin["annual_sales_revenue"]
    same_period = ((revenue["period_start"], revenue["period_end"]) ==
                   (bulletin_revenue["period_start"], bulletin_revenue["period_end"]))
    same_scope = revenue["scope"] == bulletin_revenue["scope"]
    delta = revenue["value"] - bulletin_revenue["value"] if same_period else None
    delta_pct = (delta / bulletin_revenue["value"] * 100
                 if delta is not None and bulletin_revenue["value"] else None)
    blockers = []
    if not same_period:
        blockers.append("财报与公告报告期不一致")
    if not same_scope:
        blockers.append("财报产品范围与公告销量范围不同；差额不能解释为审计调整")
    if bulletin.get("includes_internal_sales"):
        blockers.append("简报包含向集团内部屠宰子公司销售，可能需合并抵销")
    if segment["revenue"].get("source_type") != "audited_report":
        blockers.append("经营分析分产品表不在审计意见覆盖范围")
    if not isinstance(sold_live_weight, dict):
        if bulletin.get("annual_commodity_sold_weight_estimate"):
            blockers.append("商品猪重量仅由公告收入/舍入均价推算，非独立称重；仍缺同范围成本")
        else:
            blockers.append("缺同期间、同产品范围的已售活重 kg；头数和销售均价不能替代")
    costs = {}
    if isinstance(sold_live_weight, dict):
        for name, profit, basis in (
            ("audited_cogs_per_sold_kg", segment["gross_profit"], "gross_profit"),
            ("adjusted_operating_cost_per_sold_kg", adjusted_operating_profit,
             "adjusted_operating_profit"),
        ):
            if profit is None:
                continue
            try:
                costs[name] = infer_pig_cost(revenue, profit, sold_live_weight,
                                              profit_basis=basis)
            except ValueError as exc:
                blockers.append(f"{name}: {exc}")
    return {
        "status": "reconciled" if same_period else "period_mismatch",
        "same_period": same_period, "same_scope": same_scope,
        "annual_report_revenue_cny": revenue["value"],
        "sales_bulletin_revenue_cny": bulletin_revenue["value"],
        "arithmetic_delta_cny": delta, "arithmetic_delta_pct": delta_pct,
        "delta_interpretation": ("口径一致但尚未核准内部销售、确认时点和四舍五入"
                                 if same_scope else "范围不同，差额不可视为误差"),
        "sales_heads": bulletin["annual_sales_heads"],
        "commodity_weight_estimate": bulletin.get("annual_commodity_sold_weight_estimate"),
        "annual_commodity_price": bulletin.get("annual_commodity_price"),
        "cost_ready": bool(costs), "costs": costs, "blockers": list(dict.fromkeys(blockers)),
    }


def shadow_pig_slots(generic, reconciliation, *, market_evidence=None):
    """与现行周期槽位做只读替换预演；不改变 router、rules 或快照。"""
    pig = dict(market_evidence or {})
    price = reconciliation.get("annual_commodity_price")
    if price is not None:
        pig["company_sale_price"] = price
    cost = reconciliation.get("costs", {}).get("audited_cogs_per_sold_kg")
    if cost is not None:
        pig["unit_cost"] = cost
        if price is not None:
            try:
                pig["unit_margin"] = infer_unit_margin(price, cost)
            except ValueError:
                pass
    return resolve_slots(generic, pig)
