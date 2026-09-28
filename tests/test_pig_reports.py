import copy
import unittest

from research.cyclical_slots import infer_pig_cost
from research.pig_reports import extract_pig_product_financials
from research.reports import _dedupe


META = {
    "stock_code": "001201", "report_period": "2026H1", "report_type": "H1",
    "source": "cninfo", "source_id": "finalpage/example.PDF",
    "document_hash": "a" * 64, "pdf_url": "https://example.invalid/report.pdf",
}


def row(page, *texts):
    return {"page": page, "cells": [{"x": i * 100, "text": text}
                                     for i, text in enumerate(texts)]}


def report_rows(amounts=("990,210,584.88", "1,062,255,788.11"), margin="-7.28%"):
    return [row(16, "单位：元"), row(17, "营业收入", "营业成本", "毛利率"),
            row(17, "分行业"), row(17, "养殖行业", *amounts, margin),
            row(17, "分产品"), row(17, "生猪", *amounts, margin),
            row(17, "分地区")]


class PigReportExtractionTests(unittest.TestCase):
    def test_dongrui_h1_extracts_segment_but_not_audited_cost(self):
        result = extract_pig_product_financials(META, report_rows())
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["revenue"]["value"], 990210584.88)
        self.assertEqual(result["cogs"]["value"], 1062255788.11)
        self.assertEqual(result["gross_profit"]["value"], -72045203.23)
        self.assertEqual(result["revenue"]["scope"], "company_live_hog_all")
        self.assertEqual(result["revenue"]["source_page"], 17)
        self.assertEqual(result["audit_status"], "not_covered_by_audit_opinion")
        with self.assertRaises(ValueError):
            infer_pig_cost(result["revenue"], result["gross_profit"],
                           {"value": 1, "unit": "kg"}, profit_basis="gross_profit")

    def test_muyuan_joined_pdf_money_cells(self):
        rows = report_rows(("52,430,296,318.8754,795,342,077.43",), "-4.51%")
        result = extract_pig_product_financials(META, rows)
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["cogs"]["value"], 54795342077.43)

    def test_pig_industry_has_broader_scope_than_live_hogs(self):
        rows = report_rows(("28,528,575,460.41", "26,201,274,738.57"), "8.16%")
        rows[5]["cells"][0]["text"] = "猪产业"
        result = extract_pig_product_financials(META, rows)
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["revenue"]["scope"], "company_pig_industry")

    def test_rejects_wrong_margin_or_missing_headers(self):
        rows = report_rows(margin="7.28%")
        self.assertEqual(extract_pig_product_financials(META, rows)["status"], "not_found")
        rows = report_rows()[3:]
        self.assertEqual(extract_pig_product_financials(META, rows)["status"], "not_found")

    def test_rejects_ambiguous_product_rows(self):
        rows = report_rows()
        rows.extend([row(17, "分产品"), rows[5]])
        self.assertEqual(extract_pig_product_financials(META, rows)["status"], "ambiguous")

    def test_annual_management_table_is_not_audited_even_with_audit_opinion(self):
        meta = {**META, "report_period": "2025A", "report_type": "A"}
        result = extract_pig_product_financials(meta, report_rows())
        self.assertEqual(result["audit_status"], "not_covered_by_audit_opinion")
        meta["audit_verified"] = True
        result = extract_pig_product_financials(meta, report_rows())
        self.assertEqual(result["audit_status"], "not_covered_by_audit_opinion")
        self.assertEqual(result["revenue"]["period_end"], "2025-12-31")

    def test_rejects_nonofficial_or_quarterly(self):
        meta = copy.copy(META)
        meta["source"] = "unknown"
        self.assertEqual(extract_pig_product_financials(meta, report_rows())["status"],
                         "unsupported_report")

    def test_chinese_report_preferred_over_later_english_translation(self):
        selected = _dedupe([
            {"report_period": "2025A", "title": "2025年年度报告",
             "publish_date": "2026-04-29"},
            {"report_period": "2025A", "title": "2025年年度报告（英文版）",
             "publish_date": "2026-07-22"},
        ])
        self.assertEqual(selected[0]["title"], "2025年年度报告")
        meta = {**META, "report_period": "2026Q1"}
        self.assertEqual(extract_pig_product_financials(meta, report_rows())["status"],
                         "unsupported_report")


if __name__ == "__main__":
    unittest.main()
