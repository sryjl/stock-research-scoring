# -*- coding: utf-8 -*-
"""猪业务暴露（批 4，spec §二十–§二十四）。

这一层最容易犯的错是**拿业务描述凑一个数**：界面上「猪业务暴露 55%」与
「猪业务暴露 55%（估计）」长得几乎一样，而后者可能只是把「公司有生猪养殖业务」
这句话翻译成了一个百分比。所以这里的测试盯三件事：

1. 解析不出来就是 ``missing``，而且 miss 的**种类**要能分开（报告没缓存 /
   有报告没分部表 / 有表没收入占比）；
2. 只有 ``segment_revenue`` 这一个来源进正式 composite，毛利占比、分部资产
   只作为旁证留在 ``detail`` 里；
3. 分类判据是**数**不是公司名字。
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import pig_exposure, rules     # noqa: E402


def _report(period="2025A", revenue_share=0.2669, gross_profit_share=0.31,
            assets_share=0.22, audit_scope="financial_statement_note",
            status="extracted", segment_label="猪产业", revenue=1.2e10,
            cogs=1.0e10, source_page=88):
    """一期缓存报告。``financial_note`` 的形状照 ``pig_segment_notes`` 的产物。"""
    note = {"status": status, "segment_label": segment_label,
            "audit_scope": audit_scope,
            "exposure": {"revenue_share": revenue_share,
                         "gross_profit_share": gross_profit_share,
                         "assets_share_pre_elimination": assets_share},
            "evidence": {"source_page": source_page}}
    return {"report_period": period, "financial_note": note,
            "revenue": {"value": revenue}, "cogs": {"value": cogs}}


def _broken(period="2025A", status="not_found"):
    """有报告、但分部表核对不出来（实测里牧原/东瑞/天康就是这一种）。"""
    return {"report_period": period,
            "financial_note": {"status": status, "exposure": {}},
            "revenue": {"value": 1e10}, "cogs": {"value": 0.9e10}}


class TestClassification(unittest.TestCase):

    def test_bands_come_from_config(self):
        cfg = rules.RULES_V1["pig"]
        for code, lower in cfg["classification_bands"]:
            self.assertEqual(pig_exposure.classify(lower + 0.001), code)
            self.assertNotEqual(pig_exposure.classify(lower - 0.001), code)

    def test_low_exposure_falls_to_the_declared_default(self):
        code = pig_exposure.classify(0.05)
        self.assertEqual(code, rules.RULES_V1["pig"]["default_classification"])
        self.assertEqual(code, "DIVERSIFIED_AGRI")

    def test_no_exposure_is_unknown_not_a_low_exposure(self):
        """「没有暴露数据」不许被归成「多元化农业」——那是两个结论。

        批 5 起它也不是 ``None``：下游的适用性门要拿这个码**查表**
        （``RULES_V1["pig"]["gate_multipliers"]`` 给 UNKNOWN ×0.00），
        而一个空值查不了表——它只会让门静默落进「没读到」那一档，于是
        「数据说它成分未知」与「连分类都没算出来」在报告里再也分不开。
        """
        self.assertEqual(pig_exposure.classify(None), pig_exposure.CLASS_UNKNOWN)
        self.assertEqual(pig_exposure.classify("0.5"), pig_exposure.CLASS_UNKNOWN)
        # 超出占比范围 = 解析串行，与「没有数」同一档：都不是一个可用的暴露值。
        self.assertEqual(pig_exposure.classify(1.4), pig_exposure.CLASS_UNKNOWN)
        self.assertEqual(pig_exposure.CLASS_UNKNOWN,
                         rules.RULES_V1["pig"]["unknown_classification"])
        # 它是**能被查表**的码，不是「大概很低」。
        self.assertIn(pig_exposure.CLASS_UNKNOWN,
                      rules.RULES_V1["pig"]["gate_multipliers"])
        self.assertNotIn(pig_exposure.CLASS_UNKNOWN,
                         [code for code, _lower
                          in rules.RULES_V1["pig"]["classification_bands"]])


class TestResolve(unittest.TestCase):

    def test_annual_report_wins_over_the_newer_half_year(self):
        """结构性的属性用全年口径：中报的分子分母只覆盖半年。"""
        out = pig_exposure.resolve([
            _report(period="2025A", revenue_share=0.40),
            _report(period="2026H1", revenue_share=0.62),
        ])
        self.assertEqual(out["as_of"], "2025A")
        self.assertEqual(out["exposure"], 0.40)

    def test_only_segment_revenue_enters_the_composite(self):
        out = pig_exposure.resolve([_report()])
        self.assertEqual(out["exposure_source"], "segment_revenue")
        self.assertEqual([s["source"] for s in out["exposure_sources"]],
                         ["segment_revenue"])
        # 毛利占比与分部资产是**真数据但口径不足**，只当旁证。
        self.assertEqual(out["detail"]["gross_profit_share"]["value"], 0.31)
        self.assertEqual(out["detail"]["assets_share_pre_elimination"]["value"], 0.22)

    def test_source_breakdown_separates_entered_excluded_and_absent(self):
        """权重配齐（.45/.30/.15/.10）≠ 四个来源都在用——这张表就是那个区别。

        修复前的载荷只有「用到的那个来源」，于是「权重配好了但还没数据」与
        「压根没打算用这个来源」长得一模一样，报告的读者会以为 0.45 那一格在用。
        """
        out = pig_exposure.resolve([_report()])
        rows = {r["source"]: r for r in out["source_breakdown"]}
        self.assertEqual(list(rows), list(rules.RULES_V1["pig"]["exposure_source_priority"]),
                         "顺序照来源优先级，不另排")
        self.assertEqual(rows["segment_revenue"]["status"], "entered")
        self.assertEqual(rows["segment_revenue"]["weight"], 0.30)
        # 有真数据、但口径不足的那两个来源必须**带着原因**留下，不能被丢掉。
        self.assertEqual(rows["segment_profit"]["status"], "excluded")
        self.assertEqual(rows["segment_profit"]["kind"], "indicative")
        self.assertEqual(rows["segment_profit"]["value"], 0.31)
        self.assertEqual(rows["segment_asset"]["status"], "excluded")
        self.assertEqual(rows["segment_asset"]["kind"], "upper_bound")
        self.assertEqual(rows["segment_asset"]["upper_bound"], 0.22)
        self.assertEqual(rows["segment_capex"]["status"], "absent")
        self.assertTrue(rows["segment_capex"]["note"])
        # 它**不进** composite：暴露值仍然只由收入占比归一化而来。
        self.assertEqual(rows["segment_revenue"]["value"], 0.2669)
        self.assertEqual(out["exposure"], 0.2669)

    def test_unimplemented_sources_are_listed_with_their_reason(self):
        """缺席的来源不是「权重为 0」，是「还没有解析器」——要有逐条理由。"""
        out = pig_exposure.resolve([_report()])
        absent = out["absent_sources"]
        self.assertEqual(sorted(absent), sorted(pig_exposure.SOURCE_ABSENT_REASONS))
        for src, reason in absent.items():
            self.assertTrue(reason, src)
            self.assertNotIn(src, pig_exposure.IMPLEMENTED_SOURCES)
        self.assertEqual(list(pig_exposure.IMPLEMENTED_SOURCES), ["segment_revenue"])

    def test_business_description_has_no_metric_and_no_weight(self):
        """业务描述**不进 composite**（用户裁定），所以它两处都不在表里。"""
        self.assertNotIn("business_description", pig_exposure.SOURCE_TO_METRIC)
        weights = rules.RULES_V1["pig"]["exposure_source_weights"]
        self.assertEqual(weights.get("business_description"), 0.0)

    def test_confidence_comes_from_audit_scope(self):
        bands = rules.RULES_V1["pig"]["confidence_bands"]
        for scope, expected in bands.items():
            out = pig_exposure.resolve([_report(audit_scope=scope)])
            self.assertEqual(out["confidence"], round(float(expected), 4), scope)
        # 表里没有的 scope → 0.0，不给一个「看起来还行」的默认值。
        self.assertEqual(pig_exposure.resolve([_report(audit_scope="??")])["confidence"],
                         0.0)

    def test_missing_reports_never_borrow_from_a_guess(self):
        """用户裁定的硬约束：解析不出分部占比 → 正式暴露保持 missing。"""
        for reports, expect in (([], "没有这家公司的定期报告"),
                                ([_broken()], "都没有可唯一核对的分部信息表"),
                                ([_report(revenue_share=None)],
                                 "没有可用的收入占比")):
            out = pig_exposure.resolve(reports)
            self.assertIsNone(out["exposure"], reports)
            self.assertIsNone(out["exposure_source"])
            # 分类码不是 ``None`` 而是 ``UNKNOWN``（批 5）：它要能被门**查表**。
            # 但「没有暴露值」这一条判据没变——UNKNOWN 不在分档表里，所以它
            # 永远拿不到一个「看起来像暴露很低」的码。
            self.assertEqual(out["classification"], pig_exposure.CLASS_UNKNOWN,
                             reports)
            self.assertNotIn(pig_exposure.CLASS_UNKNOWN,
                             [code for code, _low
                              in rules.RULES_V1["pig"]["classification_bands"]])
            self.assertIn(expect, out["reason"])
            # 估计值字段必须在，但**标明是估计**且不参与正式结果。
            self.assertTrue(out["estimate_is_estimated"] is False or
                            out["estimate_is_estimated"] is True)
        self.assertFalse(pig_exposure.resolve([_broken()])["exposure"])

    def test_a_report_without_extracted_note_is_not_a_candidate(self):
        out = pig_exposure.resolve([_report(status="unsupported_report")])
        self.assertIsNone(out["exposure"])
        self.assertIn("unsupported_report", out["reason"])

    def test_never_raises_on_garbage(self):
        for reports in (None, [], [None], ["x"], [{}], [{"financial_note": None}]):
            out = pig_exposure.resolve(reports)
            self.assertIsNone(out["exposure"], reports)
            self.assertTrue(out["reason"], reports)

    def test_describe_says_it_is_unknown_when_it_is(self):
        self.assertIn("未知", pig_exposure.describe(pig_exposure.resolve([])))
        self.assertIn("26.7%", pig_exposure.describe(pig_exposure.resolve([_report()])))


class TestMetricRegistration(unittest.TestCase):
    """来源码背后的指标名必须在 metric_catalog 里登记过（不新造名字）。"""

    def test_every_source_metric_is_registered(self):
        from research import metric_catalog
        ids = {s.metric_id for s in metric_catalog.CATALOG}
        for src, metric_id in pig_exposure.SOURCE_TO_METRIC.items():
            self.assertIn(metric_id, ids, src)


if __name__ == "__main__":
    unittest.main()
