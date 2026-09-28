import unittest

from research.pig_segment_notes import extract_pig_industry_note


META = {"report_type": "A", "report_period": "2025A", "source": "cninfo",
        "source_id": "annual.pdf", "document_hash": "a" * 64}


def row(page, *texts):
    return {"page": page, "cells": [{"text": text} for text in texts]}


def note_rows():
    return [row(119, "审计意见类型", "标准的无保留意见"),
            row(274, "十八、其他重要事项"), row(274, "1、分部信息"),
            row(274, "以行业分布为基础确定报告的分部信息"),
            row(274, "项目", "营业收入", "营业成本", "资产总额", "负债总额"),
            row(274, "饲料", "70.00", "60.00", "30.00", "20.00"),
            row(274, "猪产业", "30.00", "25.00", "25.00", "15.00"),
            row(274, "合计", "100.00", "85.00", "100.00", "80.00")]


class PigSegmentNoteTests(unittest.TestCase):
    def test_extracts_note_and_keeps_exposure_indicative(self):
        result = extract_pig_industry_note(META, note_rows())
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["evidence"]["source_type"], "audited_report")
        self.assertAlmostEqual(result["exposure"]["revenue_share"], 0.3)
        self.assertAlmostEqual(result["exposure"]["gross_profit_share"], 1 / 3)
        self.assertEqual(result["exposure"]["status"], "indicative_not_final")

    def test_missing_audit_opinion_downgrades_source(self):
        result = extract_pig_industry_note(META, note_rows()[1:])
        self.assertEqual(result["evidence"]["source_type"], "official_disclosure")

    def test_rejects_bad_table_structure(self):
        rows = note_rows()
        rows[-2]["cells"][2]["text"] = "broken"
        self.assertEqual(extract_pig_industry_note(META, rows)["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
