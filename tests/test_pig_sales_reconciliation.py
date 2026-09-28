import unittest

from research.pig_sales import annual_company_price_history, extract_sales_bulletin_rows
from research.pig_reconciliation import reconcile_annual_pig_evidence, shadow_pig_slots


URL = "https://static.cninfo.com.cn/finalpage/2026-01-08/example.PDF"


def row(page, *texts):
    return {"page": page, "cells": [{"text": text} for text in texts]}


def sample_rows(*, commodity=False):
    label = "商品猪" if commodity else "生猪"
    return [
        row(1, "证券代码：001201"), row(1, "2025年12月份生猪销售简报"),
        row(1, "2025年1-12月，公司共销售生猪2.00万头，生猪销售收入2.00亿元，商品"),
        row(1, "猪销售均价14.66元/公斤。向全资子公司销售生猪。"),
        row(1, f"{label}销量" if commodity else "生猪销售数量",
            f"{label}销售收入", "商品猪价格"),
        row(1, "2025年1-11月", "1.00", "1.00", "1.00", "1.00", "14.00"),
        row(2, "2025年12月", "1.00", "2.00", "1.00", "2.00", "14.50"),
    ]


def segment(scope="company_live_hog_all"):
    common = {"scope": scope, "period_start": "2025-01-01",
              "period_end": "2025-12-31", "source_type": "official_disclosure",
              "source_id": "annual.pdf", "unit": "CNY"}
    return {"status": "extracted", "revenue": {**common, "metric": "pig_revenue",
                                              "value": 198000000},
            "gross_profit": {**common, "metric": "pig_gross_profit",
                             "value": 20000000}}


class PigSalesTests(unittest.TestCase):
    def test_extracts_full_year_and_disclosed_annual_price(self):
        result = extract_sales_bulletin_rows(sample_rows(), stock_code="001201",
                                             year=2025, source_url=URL,
                                             document_hash="abc")
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["annual_sales_heads"]["value"], 20000)
        self.assertEqual(result["annual_sales_revenue"]["value"], 200000000)
        self.assertEqual(result["annual_commodity_price"]["value"], 14.66)
        self.assertEqual(result["annual_commodity_price"]["scope"],
                         "company_commodity_hog")

    def test_commodity_report_does_not_borrow_price_from_mismatched_prose(self):
        result = extract_sales_bulletin_rows(sample_rows(commodity=True),
                                             stock_code="001201", year=2025,
                                             source_url=URL, document_hash="abc")
        self.assertEqual(result["scope"], "company_commodity_hog")
        self.assertIsNone(result["annual_commodity_price"])
        self.assertIsNotNone(result["annual_commodity_sold_weight_estimate"])
        self.assertEqual(result["annual_commodity_sold_weight_estimate"]["source_type"],
                         "derived_official")

    def test_rejects_missing_month_and_wrong_company(self):
        rows = sample_rows()
        rows[-1]["cells"][0]["text"] = "2025年10月"
        self.assertEqual(extract_sales_bulletin_rows(rows, stock_code="001201",
                        year=2025, source_url=URL, document_hash="abc")["status"],
                         "incomplete_year")
        self.assertEqual(extract_sales_bulletin_rows(sample_rows(), stock_code="002714",
                        year=2025, source_url=URL, document_hash="abc")["status"],
                         "document_identity_mismatch")

    def test_rejects_cumulative_mismatch_and_untrusted_host(self):
        rows = sample_rows()
        rows[-1]["cells"][2]["text"] = "4.00"
        self.assertEqual(extract_sales_bulletin_rows(rows, stock_code="001201",
                        year=2025, source_url=URL, document_hash="abc")["status"],
                         "cumulative_mismatch")
        self.assertEqual(extract_sales_bulletin_rows(sample_rows(), stock_code="001201",
                        year=2025, source_url="https://example.com/a.pdf",
                        document_hash="abc")["status"], "untrusted_source")

    def test_reconciliation_never_uses_heads_as_weight(self):
        bulletin = extract_sales_bulletin_rows(sample_rows(), stock_code="001201",
                                               year=2025, source_url=URL,
                                               document_hash="abc")
        result = reconcile_annual_pig_evidence(segment(), bulletin)
        self.assertTrue(result["same_scope"])
        self.assertEqual(result["arithmetic_delta_cny"], -2000000)
        self.assertFalse(result["cost_ready"])
        self.assertTrue(any("已售活重" in x for x in result["blockers"]))
        shadow = shadow_pig_slots({}, result)
        self.assertEqual(shadow["company_sale_price"]["selection"], "pig")
        self.assertEqual(shadow["unit_cost"]["selection"], "missing")
        fallback = shadow_pig_slots({"unit_cost": {"value": 42}}, result)
        self.assertEqual(fallback["unit_cost"]["selection"], "generic")
        self.assertTrue(fallback["unit_cost"]["fallback_used"])

    def test_scope_mismatch_blocks_delta_interpretation(self):
        bulletin = extract_sales_bulletin_rows(sample_rows(commodity=True),
                                               stock_code="001201", year=2025,
                                               source_url=URL, document_hash="abc")
        result = reconcile_annual_pig_evidence(segment(), bulletin)
        self.assertFalse(result["same_scope"])
        self.assertIn("不可视为误差", result["delta_interpretation"])

    def test_three_year_company_prices_are_not_labeled_national_premium(self):
        bulletins = []
        for year, price in ((2023, 17.13), (2024, 18.16), (2025, 14.66)):
            bulletins.append({"status": "extracted", "stock_code": "001201",
                              "annual_commodity_price": {
                                  "value": price, "period_end": f"{year}-12-31",
                                  "source_id": f"{year}.pdf"}})
        result = annual_company_price_history(bulletins, end_year=2025)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([p["price_cny_kg"] for p in result["prices"]],
                         [17.13, 18.16, 14.66])
        self.assertEqual(result["national_premium_status"],
                         "missing_comparable_national_benchmark")


if __name__ == "__main__":
    unittest.main()
