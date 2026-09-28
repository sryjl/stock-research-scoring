"""周期行业指标槽位与证据门禁（不计算投资评分）。

同一个槽位只选一个来源：可信的行业数据优先，缺失或不合口径时回退到
通用代理。调用方必须保留 selection/reason，不能把回退值展示成猪业实测值。
"""

from datetime import date
import math


# 业务能力与行业周期分开；通用财务生存和估值仍由原评分模块负责。
SLOTS = (
    "product_price", "company_sale_price", "unit_cost", "unit_margin",
    "supply_pressure", "capacity_growth", "industry_profitability",
)
PIG_UNITS = {
    "product_price": "CNY/kg",
    "company_sale_price": "CNY/kg",
    "unit_cost": "CNY/kg",
    "unit_margin": "CNY/kg",
    "supply_pressure": "index",
    "capacity_growth": "ratio",
    "industry_profitability": "CNY/kg",
}
PIG_SCOPES = {
    "product_price": "national_live_hog",
    "company_sale_price": "company_commodity_hog",
    "unit_cost": "company_commodity_hog",
    "unit_margin": "company_commodity_hog",
    "supply_pressure": "national_live_hog",
    "capacity_growth": "company_commodity_hog",
    "industry_profitability": "national_live_hog",
}
OFFICIAL_SOURCES = {"audited_report", "official_disclosure"}
ACCEPTED_SOURCES = OFFICIAL_SOURCES | {"derived_official", "market_data"}


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _cost_inputs_reason(item):
    inputs = item.get("inputs")
    if not isinstance(inputs, dict):
        return "缺少收入、利润、已售活重的逐项来源"
    records = [inputs.get(k) for k in ("revenue", "profit", "sold_kg")]
    if not all(isinstance(r, dict) for r in records):
        return "成本输入未逐项注明来源"
    revenue, profit, weight = records
    if revenue.get("metric") != "pig_revenue" or weight.get("metric") != "pig_sold_live_weight":
        return "成本输入不是猪业务收入与已售活重"
    if (revenue.get("source_type") != "audited_report"
            or profit.get("source_type") != "audited_report"):
        return "收入与利润必须来自审计财报；月报只能辅助核对重量"
    if revenue.get("source_id") != profit.get("source_id"):
        return "收入与利润不是同一版审计财报"
    for record, unit in zip(records, ("CNY", "CNY", "kg")):
        if record.get("unit") != unit or not _finite_number(record.get("value")):
            return "成本输入的单位或数值无效"
        if record.get("scope") != item.get("scope"):
            return "收入、利润、重量的产品范围不一致"
        if (record.get("period_start"), record.get("period_end")) != (
                item.get("period_start"), item.get("period_end")):
            return "收入、利润、重量的报告期不一致"
        if not record.get("source_id") or record.get("source_type") not in OFFICIAL_SOURCES:
            return "成本输入缺少官方来源"
    if revenue["value"] <= 0 or weight["value"] <= 0:
        return "收入和已售活重必须为正"
    if inputs.get("profit_basis") not in {"gross_profit", "adjusted_operating_profit"}:
        return "利润口径不允许用于反推养殖成本"
    expected_profit_metric = ("pig_gross_profit" if inputs["profit_basis"] == "gross_profit"
                              else "pig_adjusted_operating_profit")
    if profit.get("metric") != expected_profit_metric:
        return "利润指标身份与声明口径不符"
    if (inputs["profit_basis"] == "adjusted_operating_profit"
            and not profit.get("expense_allocation")):
        return "经营利润缺少期间费用分摊方法"
    expected_basis = ("audited_cogs_per_sold_kg" if inputs["profit_basis"] == "gross_profit"
                      else "adjusted_operating_cost_per_sold_kg")
    if item.get("cost_basis") != expected_basis:
        return "成本口径与利润口径不符"
    expected = (revenue["value"] - profit["value"]) / weight["value"]
    if expected < 0 or not math.isclose(item["value"], expected, rel_tol=1e-9):
        return "成本与财报输入无法勾稽"
    return None


def _margin_inputs_reason(item):
    inputs = item.get("inputs")
    if not isinstance(inputs, dict):
        return "单位价差缺少售价和成本证据"
    price, cost = inputs.get("company_sale_price"), inputs.get("unit_cost")
    if not isinstance(price, dict) or not isinstance(cost, dict):
        return "单位价差缺少售价和成本证据"
    for slot, record in (("company_sale_price", price), ("unit_cost", cost)):
        accepted, reason = validate_pig_evidence(slot, record)
        if not accepted:
            return f"{slot}: {reason}"
        if (record["period_start"], record["period_end"]) != (
                item["period_start"], item["period_end"]):
            return "售价和成本的期间不一致"
    if not math.isclose(item["value"], price["value"] - cost["value"], rel_tol=1e-9,
                        abs_tol=1e-9):
        return "单位价差与售价、成本无法勾稽"
    return None


def validate_pig_evidence(slot, item):
    """返回 (可替换通用代理, 原因)。披露事实与可比评分资格是两回事。"""
    if slot not in PIG_UNITS:
        return False, "未知周期槽位"
    if not isinstance(item, dict):
        return False, "缺少猪业证据"
    value = item.get("value")
    if not _finite_number(value):
        return False, "数值缺失或无效"
    if slot in {"product_price", "company_sale_price", "unit_cost"} and value <= 0:
        return False, "价格或成本必须为正"
    if item.get("unit") != PIG_UNITS[slot]:
        return False, "单位不符"
    if item.get("scope") != PIG_SCOPES[slot]:
        return False, "业务或产品范围不符"
    try:
        start = date.fromisoformat(item["period_start"])
        end = date.fromisoformat(item["period_end"])
    except (KeyError, TypeError, ValueError):
        return False, "报告期缺失或无效"
    if start > end:
        return False, "报告期倒置"
    if not item.get("source_id") or not item.get("basis"):
        return False, "缺少来源或计算口径"
    source_type = item.get("source_type")
    if source_type not in ACCEPTED_SOURCES:
        return False, "来源尚未核验"
    if source_type == "derived_official" and slot not in {"unit_cost", "unit_margin"}:
        return False, "该指标缺少原始披露来源"
    if source_type == "market_data" and not item.get("methodology"):
        return False, "行情源统计方法未核验"
    # 「正常运营场线」或公司自己声明的完全成本，不等于全公司已售商品猪成本。
    if slot == "unit_cost":
        reason = _cost_inputs_reason(item)
        if reason:
            return False, reason
    if slot == "unit_margin":
        reason = _margin_inputs_reason(item)
        if reason:
            return False, reason
    if slot == "capacity_growth" and item.get("capacity_basis") != "effective_capacity":
        return False, "规划产能不能代替有效产能"
    return True, None


def resolve_slots(generic, pig):
    """逐槽替换，不叠加分数；不满足门禁的专业指标只留下回退说明。"""
    result = {}
    for slot in SLOTS:
        specialized = (pig or {}).get(slot)
        accepted, reason = validate_pig_evidence(slot, specialized)
        if accepted:
            result[slot] = {"selection": "pig", "evidence": specialized,
                            "fallback_used": False, "reason": None}
        elif (generic or {}).get(slot) is not None:
            result[slot] = {"selection": "generic", "evidence": generic[slot],
                            "fallback_used": True, "reason": reason}
        else:
            result[slot] = {"selection": "missing", "evidence": None,
                            "fallback_used": False, "reason": reason}
    return result


def infer_pig_cost(revenue, profit, sold_kg, *, profit_basis):
    """用同范围、同期的收入与利润反推单位成本，拒绝集团净利润。

    gross_profit -> 已售猪营业成本/公斤；adjusted_operating_profit ->
    经营保本成本/公斤。两者都不是公司自报的养殖场「完全成本」。
    """
    if profit_basis not in {"gross_profit", "adjusted_operating_profit"}:
        raise ValueError("只能用毛利或调整后经营利润；集团净利润不可反推养殖成本")
    inputs = {"revenue": revenue, "profit": profit, "sold_kg": sold_kg,
              "profit_basis": profit_basis}
    if not all(isinstance(r, dict) for r in (revenue, profit, sold_kg)):
        raise ValueError("收入、利润与已售活重必须各自带来源和口径")
    if not all(_finite_number(r.get("value")) for r in (revenue, profit, sold_kg)):
        raise ValueError("收入、利润与已售活重必须为有效数值")
    basis = ("audited_cogs_per_sold_kg" if profit_basis == "gross_profit"
             else "adjusted_operating_cost_per_sold_kg")
    evidence = {
        "value": ((revenue["value"] - profit["value"]) / sold_kg["value"]
                  if sold_kg["value"] else None),
        "unit": "CNY/kg", "scope": "company_commodity_hog",
        "period_start": revenue.get("period_start"), "period_end": revenue.get("period_end"),
        "source_type": "derived_official", "source_id": revenue.get("source_id"),
        "basis": f"同口径收入减{profit_basis}，再除以已售活重",
        "cost_basis": basis, "inputs": inputs,
        "quality": ("audited" if all(r.get("source_type") == "audited_report"
                                    for r in (revenue, profit, sold_kg)) else "official_mixed"),
    }
    accepted, reason = validate_pig_evidence("unit_cost", evidence)
    if not accepted:
        raise ValueError(reason)
    return evidence


def infer_unit_margin(company_sale_price, unit_cost):
    """售价减反推成本；绝不把不同期间或不同产品的两条线直接相减。"""
    for slot, record in (("company_sale_price", company_sale_price),
                         ("unit_cost", unit_cost)):
        accepted, reason = validate_pig_evidence(slot, record)
        if not accepted:
            raise ValueError(f"{slot}: {reason}")
    evidence = {
        "value": company_sale_price["value"] - unit_cost["value"],
        "unit": "CNY/kg", "scope": "company_commodity_hog",
        "period_start": company_sale_price["period_start"],
        "period_end": company_sale_price["period_end"],
        "source_type": "derived_official",
        "source_id": f"{company_sale_price['source_id']} | {unit_cost['source_id']}",
        "basis": "同期间商品猪销售均价－同口径反推成本",
        "inputs": {"company_sale_price": company_sale_price, "unit_cost": unit_cost},
    }
    accepted, reason = validate_pig_evidence("unit_margin", evidence)
    if not accepted:
        raise ValueError(reason)
    return evidence
