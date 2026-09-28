"""周期行业特化第一阶段：来源门禁、逐槽回退与成本反推。"""

import unittest

from research import cyclical_slots as slots


def evidence(slot, **changes):
    item = {
        "value": 12.3, "unit": slots.PIG_UNITS[slot],
        "scope": slots.PIG_SCOPES[slot],
        "period_start": "2025-01-01", "period_end": "2025-12-31",
        "source_id": "finalpage/2026-04-28/report.pdf",
        "source_type": "audited_report", "basis": "已售商品猪同口径",
    }
    item.update(changes)
    return item


def cost_input(value, unit, metric, *, period_end="2025-12-31", scope="company_commodity_hog"):
    return {"value": value, "unit": unit, "metric": metric, "scope": scope,
            "period_start": "2025-01-01", "period_end": period_end,
            "source_id": "official/2025-report", "source_type": "audited_report"}


def cost_records():
    return (cost_input(1500, "CNY", "pig_revenue"),
            cost_input(100, "CNY", "pig_gross_profit"),
            cost_input(100, "kg", "pig_sold_live_weight"))


def cost_evidence():
    return slots.infer_pig_cost(*cost_records(), profit_basis="gross_profit")


class TestPigEvidenceGate(unittest.TestCase):
    def test_audited_cost_replaces_only_its_own_slot(self):
        generic = {"unit_cost": {"value": 14.0, "basis": "财务代理"},
                   "supply_pressure": {"value": 0.4, "basis": "通用代理"}}
        resolved = slots.resolve_slots(generic, {"unit_cost": cost_evidence()})
        self.assertEqual(resolved["unit_cost"]["selection"], "pig")
        self.assertFalse(resolved["unit_cost"]["fallback_used"])
        self.assertEqual(resolved["supply_pressure"]["selection"], "generic")
        self.assertEqual(resolved["product_price"]["selection"], "missing")

    def test_company_claim_is_not_a_verified_cost(self):
        claim = {**cost_evidence(), "source_type": "company_statement",
                 "cost_basis": "normal_farms_reported"}
        result = slots.resolve_slots({"unit_cost": {"value": 13.0}},
                                     {"unit_cost": claim})["unit_cost"]
        self.assertEqual(result["selection"], "generic")
        self.assertTrue(result["fallback_used"])
        self.assertIn("来源", result["reason"])

    def test_product_and_unit_mismatch_cannot_take_over(self):
        for change in ({"scope": "all_hogs"}, {"unit": "CNY/head"},
                       {"value": float("nan")}, {"period_end": "2024-01-01"}):
            with self.subTest(change=change):
                accepted, _ = slots.validate_pig_evidence(
                    "unit_cost", {**cost_evidence(), **change})
                self.assertFalse(accepted)

    def test_cost_inputs_must_reconcile_and_share_period_and_scope(self):
        for key, mutation in (("value", 10.0), ("cost_basis", "company_claim")):
            with self.subTest(key=key):
                candidate = {**cost_evidence(), key: mutation}
                self.assertFalse(slots.validate_pig_evidence("unit_cost", candidate)[0])
        for field, mutation in (("period_end", "2024-12-31"),
                                ("scope", "all_businesses"), ("source_id", "")):
            with self.subTest(field=field):
                candidate = cost_evidence()
                candidate["inputs"]["sold_kg"] = {
                    **candidate["inputs"]["sold_kg"], field: mutation}
                self.assertFalse(slots.validate_pig_evidence("unit_cost", candidate)[0])
        candidate = cost_evidence()
        candidate["inputs"]["profit"] = {
            **candidate["inputs"]["profit"], "source_id": "different/report"}
        self.assertFalse(slots.validate_pig_evidence("unit_cost", candidate)[0])

    def test_third_party_market_data_requires_methodology(self):
        market = evidence("product_price", source_type="market_data")
        self.assertFalse(slots.validate_pig_evidence("product_price", market)[0])
        market["methodology"] = "全国外三元日度报价，按月均值"
        self.assertTrue(slots.validate_pig_evidence("product_price", market)[0])

    def test_planned_capacity_is_not_effective_capacity(self):
        item = evidence("capacity_growth", value=0.8)
        self.assertFalse(slots.validate_pig_evidence("capacity_growth", item)[0])
        item["capacity_basis"] = "effective_capacity"
        self.assertTrue(slots.validate_pig_evidence("capacity_growth", item)[0])


class TestPigCostInference(unittest.TestCase):
    def test_gross_profit_reconstructs_cost_of_sales_not_full_cost(self):
        value = cost_evidence()
        self.assertEqual(value["value"], 14)
        self.assertEqual(value["cost_basis"], "audited_cogs_per_sold_kg")
        self.assertEqual(value["inputs"]["sold_kg"]["value"], 100)

    def test_operating_profit_is_a_different_cost_basis(self):
        allocated_profit = cost_input(100, "CNY", "pig_adjusted_operating_profit")
        allocated_profit["expense_allocation"] = "直接费用归属，其余按分部营收分摊"
        value = slots.infer_pig_cost(
            cost_input(1500, "CNY", "pig_revenue"), allocated_profit,
            cost_input(100, "kg", "pig_sold_live_weight"),
            profit_basis="adjusted_operating_profit")
        self.assertEqual(value["value"], 14)
        self.assertEqual(value["cost_basis"], "adjusted_operating_cost_per_sold_kg")

    def test_rejects_group_net_profit_headcount_and_wrong_scope(self):
        rev, profit, kg = cost_records()
        with self.assertRaises(ValueError):
            slots.infer_pig_cost(rev, profit, kg, profit_basis="group_net_profit")
        with self.assertRaises(ValueError):
            slots.infer_pig_cost(rev, {**profit, "metric": "group_net_profit"}, kg,
                                 profit_basis="gross_profit")
        with self.assertRaises(ValueError):
            slots.infer_pig_cost(rev, profit, cost_input(0, "kg", "pig_sold_live_weight"),
                                 profit_basis="gross_profit")
        with self.assertRaises(ValueError):
            slots.infer_pig_cost(rev, profit, cost_input(100, "kg", "pig_sold_live_weight",
                                                         scope="all_businesses"),
                                 profit_basis="gross_profit")

    def test_operating_profit_needs_expense_allocation(self):
        rev, _, kg = cost_records()
        profit = cost_input(100, "CNY", "pig_adjusted_operating_profit")
        with self.assertRaises(ValueError):
            slots.infer_pig_cost(rev, profit, kg,
                                 profit_basis="adjusted_operating_profit")
        profit["expense_allocation"] = "费用按直接归属+审计附注中的分部比例分摊"
        self.assertEqual(slots.infer_pig_cost(
            rev, profit, kg, profit_basis="adjusted_operating_profit")["value"], 14)

    def test_official_monthly_weight_is_marked_mixed_not_audited(self):
        rev, profit, kg = cost_records()
        kg["source_type"] = "official_disclosure"
        kg["source_id"] = "official/monthly-sales-2025"
        item = slots.infer_pig_cost(rev, profit, kg, profit_basis="gross_profit")
        self.assertEqual(item["quality"], "official_mixed")
        profit["source_type"] = "official_disclosure"
        with self.assertRaises(ValueError):
            slots.infer_pig_cost(rev, profit, kg, profit_basis="gross_profit")

    def test_unit_margin_requires_matching_period_and_reconciles(self):
        cost = cost_evidence()
        price = evidence("company_sale_price", value=15.0,
                         source_type="official_disclosure")
        result = slots.infer_unit_margin(price, cost)
        self.assertEqual(result["value"], 1.0)
        self.assertTrue(slots.validate_pig_evidence("unit_margin", result)[0])
        result["value"] = 8.0
        self.assertFalse(slots.validate_pig_evidence("unit_margin", result)[0])
        with self.assertRaises(ValueError):
            slots.infer_unit_margin(evidence("company_sale_price", value=15.0,
                                             period_end="2024-12-31"), cost)


if __name__ == "__main__":
    unittest.main()
