# -*- coding: utf-8 -*-
import hashlib
import math
import os
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import rules
from research import db as research_db
from research import engine
from research import providers
from research import series as fin_series
from tests.asset_fixtures import fake_provider as _fake_assets


def _series(vals, base_year=2018):
    return [(f"{base_year + i}-12-31", v) for i, v in enumerate(vals)]


_ANNUAL_KEYS = ("revenue", "net_profit", "deduct_profit",
                "operating_cashflow", "free_cashflow")

# 逐年资产负债数据：只给最近 3 个完整年度（更早的详细报表视作取不到），
# 逐年「资产负债率降、有息负债降、货币资金升、净现金升」。
_BALANCE_HISTORY = {
    "2022": {"monetary_funds": 4.0e9, "trading_finasset": 0.5e9,
             "short_loan": 1.4e9, "long_loan": 2.6e9,
             "total_assets": 30e9, "total_liabilities": 14.0e9, "total_equity": 16.0e9},
    "2023": {"monetary_funds": 4.5e9, "trading_finasset": 0.6e9,
             "short_loan": 1.2e9, "long_loan": 2.3e9,
             "total_assets": 30e9, "total_liabilities": 13.0e9, "total_equity": 17.0e9},
    "2024": {"monetary_funds": 5.0e9, "trading_finasset": 0.7e9,
             "short_loan": 1.0e9, "long_loan": 2.0e9,
             "total_assets": 30e9, "total_liabilities": 12.0e9, "total_equity": 18.0e9},
}

_BALANCE_FIELDS = ("monetary_funds", "trading_finasset", "short_loan", "long_loan",
                   "total_assets", "total_liabilities", "total_equity")


def _build_annual(m):
    """按 m 的年度序列拼出统一历史序列，形状与 engine 的 m["annual"] 一致。"""
    maps = {k: dict(m.get(k) or []) for k in _ANNUAL_KEYS}
    periods = sorted({p for k in _ANNUAL_KEYS for p in maps[k]})
    rows = []
    for p in periods:
        row = {"report_period": p, "report_type": "年报",
               "publish_date": f"{int(p[:4]) + 1}-04-30", "source": "test"}
        row.update({k: maps[k].get(p) for k in _ANNUAL_KEYS})
        hist = _BALANCE_HISTORY.get(p[:4])
        row.update(hist if hist else {f: None for f in _BALANCE_FIELDS})
        rows.append(row)
    return fin_series.from_rows(rows)


def _metrics(**kw):
    m = {
        "revenue": _series([10, 12, 15, 18, 22, 26, 30]),
        "net_profit": _series([1, 1.2, 1.5, 1.8, 2.2, 2.6, 3.0]),
        "deduct_profit": _series([0.9, 1.1, 1.4, 1.7, 2.0, 2.4, 2.8]),
        "roe": _series([10, 11, 12, 13, 14, 15, 16]),
        "gross_margin": _series([30, 31, 32, 33, 34, 35, 36]),
        "operating_cashflow": _series([1.1, 1.3, 1.6, 1.9, 2.3, 2.8, 3.2]),
        "free_cashflow": _series([0.5, 0.7, 0.9, 1.1, 1.4, 1.7, 2.0]),
        "roic": [("2024-12-31", 0.13)],
        "current": {"price": 20.0, "total_market_cap": 20e9, "pe_ttm": 12, "pb": 1.5,
                    "dividend_yield": 0.03, "fcf_yield": 0.06, "net_cash_ratio": 0.10,
                    "book_ratio": 0.66, "short_debt_cover": 2.0, "debt_asset_ratio": 40,
                    "accounts_receivable_growth": 10, "inventory_growth": 8, "revenue_growth": 15},
        "balance": {"monetary_funds": 5e9, "trading_finasset": 1e9, "accounts_receivable": 2e9,
                    "inventory": 3e9, "fixed_asset": 10e9, "goodwill": 0.5e9,
                    "total_assets": 30e9, "total_liabilities": 12e9, "total_equity": 18e9,
                    "short_loan": 1e9, "long_loan": 2e9},
        "dividends": [{"year": 2024, "dividend_per_share": 0.6, "amount": 1e9,
                       "report_date": "2024-12-31"}],
        "industry": "电子", "is_financial": False,
        "receivable_growth_history": [], "inventory_growth_history": [], "revenue_growth_history": [],
    }
    m.update(kw)
    if "annual" not in kw:
        m["annual"] = _build_annual(m)
    # 画像层取资产指标只有一条路：m["assets"]。默认给一份合成 provider，
    # 数值与旧的 current["net_cash_ratio"] / ["short_debt_cover"] 对齐，
    # 这样不关心资产口径的用例行为不变。要测资产相关的分支就传 assets=。
    if "assets" not in kw:
        m["assets"] = _fake_assets()
    return m


class TestPiecewise(unittest.TestCase):
    def test_interp(self):
        pts = [(0, 0), (5, 5), (10, 10)]
        self.assertEqual(rules.piecewise(0, pts), 0)
        self.assertEqual(rules.piecewise(2.5, pts), 2.5)
        self.assertEqual(rules.piecewise(7.5, pts), 7.5)
        self.assertEqual(rules.piecewise(20, pts), 10)  # 夹紧到上限

    def test_none(self):
        self.assertIsNone(rules.piecewise(None, [(0, 0), (5, 5)]))


class TestHelpers(unittest.TestCase):
    def test_cagr(self):
        self.assertAlmostEqual(rules.cagr(10, 20, 5), 2 ** 0.2 - 1, places=6)

    def test_cv(self):
        self.assertAlmostEqual(rules.cv([10, 10, 10]), 0.0, places=6)
        self.assertGreater(rules.cv([1, 5, 1, 5, 1]), 0.5)


class TestCyclicalDetection(unittest.TestCase):
    def test_pig_farm_not_misjudged_as_growth(self):
        # 牧原式：营收稳定高增长，但利润剧烈波动（周期），行业=养殖
        m = _metrics(
            net_profit=_series([20, -5, 60, -10, 80, -20, 30]),
            deduct_profit=_series([18, -6, 55, -12, 75, -22, 28]),
            gross_margin=_series([35, 10, 40, 5, 45, 8, 30]),
            industry="养殖业",
        )
        res = rules.analyze(m)
        self.assertEqual(res["type"]["primary"], "cyclical")


class TestAssetValue(unittest.TestCase):
    def test_asset_value_beats_growth(self):
        # 三角轮胎式：低PB、高净资产、净现金多、成长低
        m = _metrics(
            revenue=_series([10, 10.2, 10.1, 10.3, 10.2, 10.4, 10.3]),
            net_profit=_series([1.0, 0.9, 1.1, 1.0, 1.2, 1.1, 1.15]),
            deduct_profit=_series([0.9, 0.8, 1.0, 0.9, 1.1, 1.0, 1.05]),
            industry="轮胎",
            assets=_fake_assets(net_cash_ratio=0.5),
        )
        m["current"]["pb"] = 0.7
        res = rules.analyze(m)
        a = res["attributes"]
        self.assertGreater(a["asset_value"]["score"], a["growth"]["score"])


class TestValueChangesWithPrice(unittest.TestCase):
    def test_value_score_changes(self):
        m1 = _metrics()
        m1["current"]["price"] = 20.0
        m1["current"]["total_market_cap"] = 20e9
        m1["current"]["pb"] = 1.5
        s1 = rules.score_value(m1, m1)["score"]

        m2 = _metrics()
        m2["current"]["price"] = 10.0
        m2["current"]["total_market_cap"] = 10e9
        m2["current"]["pb"] = 0.75
        s2 = rules.score_value(m2, m2)["score"]

        self.assertNotEqual(s1, s2)
        self.assertGreater(s2, s1)  # 更便宜 -> 价值分更高


class TestRiskDetection(unittest.TestCase):
    def test_receivable_risk_multi_period(self):
        m = _metrics(
            receivable_growth_history=[("2025-12-31", 60), ("2024-12-31", 55), ("2023-12-31", 50)],
            revenue_growth_history=[("2025-12-31", 10), ("2024-12-31", 10), ("2023-12-31", 10)],
        )
        r = rules.detect_risk(m)
        self.assertIn("应收账款", [f["type"] for f in r["flags"]])
        # 连续3期 -> ORANGE
        f = [f for f in r["flags"] if f["type"] == "应收账款"][0]
        self.assertEqual(f["level"], "ORANGE")

    def test_single_period_anomaly_is_yellow(self):
        m = _metrics(
            receivable_growth_history=[("2025-12-31", 40), ("2024-12-31", 5), ("2023-12-31", 5)],
            revenue_growth_history=[("2025-12-31", 10), ("2024-12-31", 5), ("2023-12-31", 5)],
        )
        r = rules.detect_risk(m)
        f = [f for f in r["flags"] if f["type"] == "应收账款"]
        self.assertTrue(f)
        self.assertEqual(f[0]["level"], "YELLOW")  # 单期异常不直接 ORANGE

    def test_livestock_skips_inventory(self):
        m = _metrics(
            industry="养殖业",
            inventory_growth_history=[("2025-12-31", 80), ("2024-12-31", 70)],
            revenue_growth_history=[("2025-12-31", 5), ("2024-12-31", 5)],
        )
        r = rules.detect_risk(m)
        self.assertNotIn("存货", [f["type"] for f in r["flags"]])  # 养殖业不套制造业存货规则

    def test_short_debt_risk(self):
        # 债务风险看的是类现金 / 全部有息负债，来自资产语义层（不再是
        # current["short_debt_cover"] 那个只看短借的旧口径）
        m = _metrics(assets=_fake_assets(interest_debt_cover=0.3))
        r = rules.detect_risk(m)
        self.assertIn("短债", [f["type"] for f in r["flags"]])


class TestNormalization(unittest.TestCase):
    def test_missing_pe_normalization(self):
        m = _metrics()
        m["current"]["pe_ttm"] = None
        s = rules.score_value(m, m)
        self.assertIn("earned", s)
        self.assertIn("max_available", s)
        self.assertIn("total_max", s)
        self.assertLess(s["max_available"], s["total_max"])  # 缺 PE 后有效满分 < 理论满分
        self.assertLess(s["completeness"], 1.0)  # 覆盖率 < 100%
        self.assertIsNotNone(s["score"])


class TestMissingData(unittest.TestCase):
    def _comp(self, comps, name):
        return next(c for c in comps if c["name"] == name)

    def test_roic_missing(self):
        m = _metrics()
        m["roic"] = []
        s = rules.score_quality(m, m)
        c = self._comp(s["components"], "ROIC")
        self.assertFalse(c["eligible"])
        self.assertIsNone(c["score"])

    def test_roic_zero_is_valid(self):
        m = _metrics()
        m["roic"] = [("2024-12-31", 0.0)]
        s = rules.score_quality(m, m)
        c = self._comp(s["components"], "ROIC")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["score"], 0)

    def test_pe_not_applicable(self):
        m = _metrics()
        m["current"]["pe_ttm"] = -5.0
        s = rules.score_value(m, m)
        c = self._comp(s["components"], "PE")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "not_applicable")
        # 不适用 ≠ 缺数据：亏损是已知事实，PE 的输入是齐的
        self.assertEqual(c["coverage"], 1.0)

    def test_missing_coverage_is_zero(self):
        m = _metrics()
        m["current"]["pb"] = None
        s = rules.score_value(m, m)
        c = self._comp(s["components"], "PB")
        self.assertEqual(c["status"], "missing_data")
        self.assertEqual(c["coverage"], 0.0)

    def test_goodwill_zero_is_valid(self):
        m = _metrics()
        m["balance"]["goodwill"] = 0
        s = rules.score_quality(m, m)
        c = self._comp(s["components"], "商誉/净资产")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["value"], 0.0)

    def test_goodwill_missing(self):
        m = _metrics()
        m["balance"]["goodwill"] = None
        s = rules.score_quality(m, m)
        c = self._comp(s["components"], "商誉/净资产")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "missing_data")

    def test_dividend_zero_is_valid(self):
        m = _metrics()
        m["dividends"] = []  # 确认无分红
        s = rules.score_dividend(m, m)
        c = self._comp(s["components"], "分红现金覆盖")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["score"], 0)

    def test_dividend_missing(self):
        m = _metrics()
        m["dividends"] = None  # 分红数据缺失
        s = rules.score_dividend(m, m)
        c = self._comp(s["components"], "连续分红年数")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "missing_data")

    def test_no_score_for_missing(self):
        # 审计：8 个模块里，所有缺失/不适用项 score 必须为 None
        m = _metrics()
        m["roic"] = []
        m["dividends"] = None
        m["balance"]["goodwill"] = None
        m["balance"]["short_loan"] = None
        m["balance"]["long_loan"] = None
        for scorer in [rules.score_growth, rules.score_quality, rules.score_value,
                       rules.score_dividend, rules.score_cigar_butt, rules.score_asset_value,
                       rules.score_cyclical, rules.score_turnaround, rules.score_cyclical_position]:
            s = scorer(m, m)
            for c in s["components"]:
                if not c["eligible"]:
                    self.assertIsNone(c["score"], f"{scorer.__name__} 的 {c['name']} 缺失但仍有分数 {c['score']}")


class TestDividendUnits(unittest.TestCase):
    """东财 PRETAX_BONUS_RMB 是「每 10 股」派息，必须换算成每股。"""

    def _row(self, per_ten, shares=1_000_000_000):
        return {"PRETAX_BONUS_RMB": per_ten, "TOTAL_SHARES": shares,
                "EX_DIVIDEND_DATE": "2026-07-28 00:00:00", "REPORT_DATE": "2025-12-31 00:00:00",
                "DIVIDENT_RATIO": 0.05}

    def test_per_ten_shares_converted_to_per_share(self):
        # 华域式「10派10.00元」-> 每股 1.00 元，而不是 10 元
        d = providers.parse_dividend_rows([self._row(10)])[0]
        self.assertAlmostEqual(d["dividend_per_share"], 1.0)

    def test_dividend_yield_not_inflated(self):
        # 每股 1 元 / 股价 20 元 = 5%，不是 50%
        d = providers.parse_dividend_rows([self._row(10)])[0]
        self.assertAlmostEqual(d["dividend_per_share"] / 20.0, 0.05)

    def test_total_amount_in_cash_units(self):
        # 1 元/股 × 10 亿股 = 10 亿元
        d = providers.parse_dividend_rows([self._row(10, shares=1_000_000_000)])[0]
        self.assertAlmostEqual(d["amount"], 1e9)

    def test_dividend_ratio_is_yield_not_payout(self):
        # 东财 DIVIDENT_RATIO 是股息率，不是派息率，字段名不能叫 payout_ratio
        parsed = providers.parse_dividend_rows([self._row(10.0)])[0]
        self.assertAlmostEqual(parsed["dividend_yield"], 0.05)
        self.assertNotIn("payout_ratio", parsed)
        self.assertEqual(parsed["dividend_per_share"], 1.0)

    def test_missing_fields_stay_missing(self):
        d = providers.parse_dividend_rows([self._row(None, shares=None)])[0]
        self.assertIsNone(d["dividend_per_share"])
        self.assertIsNone(d["amount"])


class TestTencentFallback(unittest.TestCase):
    """东财 push2 挂掉时，腾讯行情要能顶上 PE/PB/市值。

    字段位是硬约定（39/44/45/46），这里用真实抓取的报文把它钉住。
    """

    @staticmethod
    def _payload(fields):
        p = [""] * 52
        p[1], p[2], p[3] = "华域汽车", "600741", "14.88"
        for idx, val in fields.items():
            p[idx] = val
        return 'v_sh600741="' + "~".join(p) + '";\n'

    def _parse(self, fields):
        return providers.parse_tencent_quote("600741", self._payload(fields), "sh600741")

    def test_parses_valuation_fields(self):
        q = self._parse({39: "6.74", 44: "469.13", 45: "469.13", 46: "0.71"})
        self.assertEqual(q["pe_ttm"], 6.74)
        self.assertEqual(q["pb"], 0.71)
        # 亿 -> 元，与东财口径一致
        self.assertAlmostEqual(q["total_market_cap"], 4.6913e10)
        self.assertAlmostEqual(q["float_market_cap"], 4.6913e10)

    def test_total_and_float_differ(self):
        # 牧原：流通 1356.49亿 / 总 2387.13亿，不能取错
        q = self._parse({44: "1356.49", 45: "2387.13"})
        self.assertAlmostEqual(q["float_market_cap"], 1.35649e11)
        self.assertAlmostEqual(q["total_market_cap"], 2.38713e11)

    def test_negative_pe_preserved(self):
        # 亏损股 PE 为负，必须原样传给评分规则判「不适用」，不能吞掉
        q = self._parse({39: "-212.98"})
        self.assertEqual(q["pe_ttm"], -212.98)

    def test_zero_and_empty_are_missing(self):
        q = self._parse({39: "0.00", 44: "", 45: "0.00", 46: ""})
        for k in ("pe_ttm", "pb", "total_market_cap", "float_market_cap"):
            self.assertIsNone(q[k], f"{k} 应视为缺失")

    def test_short_payload_still_reads_price(self):
        raw = 'v_sh600741="1~华域汽车~600741~14.88";'
        q = providers.parse_tencent_quote("600741", raw, "sh600741")
        self.assertEqual(q["price"], 14.88)
        self.assertIsNone(q["pe_ttm"])

    def test_missing_symbol_returns_none(self):
        self.assertIsNone(providers.parse_tencent_quote("600741", "v_sh000001=\"1~x\"", "sh600741"))


class TestBalanceSheetNulls(unittest.TestCase):
    """东财对「公司没有这条科目」返回 null，那是真实的 0，不是缺失。"""

    def test_null_goodwill_is_zero(self):
        row = {"REPORT_DATE": "2025-12-31 00:00:00", "GOODWILL": None, "TOTAL_ASSETS": 171741086702.08}
        self.assertEqual(providers.balance_line(row, "GOODWILL", zero_if_null=True), 0)

    def test_absent_key_is_missing(self):
        row = {"REPORT_DATE": "2025-12-31 00:00:00"}
        self.assertIsNone(providers.balance_line(row, "GOODWILL", zero_if_null=True))

    def test_real_value_untouched(self):
        row = {"GOODWILL": 500000000.0}
        self.assertEqual(providers.balance_line(row, "GOODWILL", zero_if_null=True), 500000000.0)

    def test_other_lines_keep_missing_semantics(self):
        # 只有商誉确认过 null==无此科目，其余科目仍按缺失处理
        row = {"SHORT_LOAN": None}
        self.assertIsNone(providers.balance_line(row, "SHORT_LOAN"))


class TestDividendCover(unittest.TestCase):
    def _comp(self, comps, name):
        return next(c for c in comps if c["name"] == name)

    def _divs(self, amounts, base_year=2022):
        # 接口按除权日倒序返回：最新在前；report_date 是分红所属财报年度
        return [{"year": base_year + len(amounts) - 1 - i, "dividend_per_share": 0.5,
                 "amount": a, "report_date": f"{base_year + len(amounts) - 1 - i}-12-31"}
                for i, a in enumerate(amounts)]

    def test_cover_computed_from_amount(self):
        # 3年FCF合计 1.4+1.7+2.0 = 5.1，同期分红合计 6 -> 覆盖 0.85
        m = _metrics(dividends=self._divs([2, 2, 2]))
        c = self._comp(rules.score_dividend(m, m)["components"], "分红现金覆盖")
        self.assertTrue(c["eligible"])
        self.assertAlmostEqual(c["value"], 5.1 / 6)

    def test_ignores_dividends_outside_window(self):
        # 2021 及更早的分红不属于最近 3 个完整年度，不能计入 3 年累计分母
        divs = self._divs([2, 2, 2]) + [
            {"year": 2021, "dividend_per_share": 0.5, "amount": 1e6, "report_date": "2021-12-31"}]
        m = _metrics(dividends=divs)
        c = self._comp(rules.score_dividend(m, m)["components"], "分红现金覆盖")
        self.assertAlmostEqual(c["value"], 5.1 / 6)

    def test_dividend_outside_window_only_is_real_zero(self):
        # 窗口内确实没有分红记录 -> 真 0，而不是「缺失」
        divs = [{"year": 2019, "dividend_per_share": 0.5, "amount": 1e8,
                 "report_date": "2019-12-31"}]
        m = _metrics(dividends=divs)
        c = self._comp(rules.score_dividend(m, m)["components"], "分红现金覆盖")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["score"], 0)
        self.assertEqual(c["value"], 0)

    def test_fcf_and_dividend_share_one_window(self):
        # FCF 与分红必须落在同一个 3 年窗口，组件里要能看到窗口年份
        m = _metrics(dividends=self._divs([2, 2, 2]))
        c = self._comp(rules.score_dividend(m, m)["components"], "分红现金覆盖")
        self.assertEqual(c["window"], ["2022", "2023", "2024"])
        self.assertAlmostEqual(c["fcf_3y"], 5.1)
        self.assertAlmostEqual(c["dividend_3y"], 6)

    def test_missing_amount_is_missing_data(self):
        divs = self._divs([None, None])
        m = _metrics(dividends=divs)
        c = self._comp(rules.score_dividend(m, m)["components"], "分红现金覆盖")
        self.assertFalse(c["eligible"])
        self.assertIsNone(c["score"])
        self.assertEqual(c["status"], "missing_data")

    def test_missing_fcf_is_missing_data(self):
        # FCF 由「经营现金流 - 资本开支」推导，经营现金流拿不到时 FCF 也就无从谈起
        m = _metrics(dividends=self._divs([2]), operating_cashflow=[], free_cashflow=[])
        c = self._comp(rules.score_dividend(m, m)["components"], "分红现金覆盖")
        self.assertFalse(c["eligible"])
        self.assertIsNone(c["score"])
        self.assertEqual(c["status"], "missing_data")

    def test_no_dividend_is_real_zero(self):
        m = _metrics(dividends=[])
        c = self._comp(rules.score_dividend(m, m)["components"], "分红现金覆盖")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["score"], 0)


class TestOverviewMetrics(unittest.TestCase):
    """概览「关键指标」直接读 valuation(= m["current"])，所以这两项必须真的落进去。

    之前 m["current"] 里既没有 roic 也没有 gross_margin —— 值算出来了，
    只是没往外暴露，于是概览永远显示「数据缺失」。
    """

    @staticmethod
    def _fin():
        return {
            "indicators": [
                {"report_period": "2026-06-30", "report_type": "中报", "publish_date": "2026-08-20",
                 "source": "test", "gross_margin": 11.91, "debt_asset_ratio": 64.1, "roe": 3.87,
                 "revenue": 1.0e11, "net_profit": 3.0e9, "deduct_profit": 2.8e9},
                {"report_period": "2025-12-31", "report_type": "年报", "publish_date": "2026-03-31",
                 "source": "test", "gross_margin": 12.30, "debt_asset_ratio": 63.0, "roe": 8.0,
                 "revenue": 1.8e11, "net_profit": 7.0e9, "deduct_profit": 6.4e9},
            ],
            "income": [{"report_period": "2025-12-31", "report_type": "年报",
                        "publish_date": "2026-03-31", "source": "test",
                        "revenue": 1.8e11, "net_profit": 7.0e9, "deduct_profit": 6.4e9}],
            "cashflow": [{"report_period": "2025-12-31", "report_type": "年报",
                          "publish_date": "2026-03-31", "source": "test",
                          "operating_cashflow": 9.0e9, "capex": 4.0e9}],
            "balance": [{"report_period": "2025-12-31", "report_type": "年报",
                         "publish_date": "2026-03-31", "source": "test",
                         "total_assets": 2.0e11, "total_liabilities": 1.26e11,
                         "total_equity": 7.4e10, "monetary_funds": 3.0e10,
                         "short_loan": 1.0e10, "long_loan": 5.0e9}],
            "dividends": [], "industry": "汽车零部件",
        }

    def _valuation(self):
        quote = {"price": 20.0, "total_market_cap": 6.3e10, "pe_ttm": 9.0, "pb": 0.85}
        m, _, _ = engine.build_metrics("600741", quote, self._fin())
        return m["current"]

    def test_overview_keys_present(self):
        v = self._valuation()
        for k in ("roic", "gross_margin", "roe", "debt_asset_ratio"):
            self.assertIn(k, v, f"概览要读 {k}，但 valuation 里没有这个字段")
            self.assertIsNotNone(v[k])

    def test_gross_margin_uses_latest_report_period(self):
        # 与资产负债率同口径：最近财报期（这里是中报），单位是百分点
        self.assertAlmostEqual(self._valuation()["gross_margin"], 11.91)

    def test_roic_exposed_as_fraction(self):
        # 7.0e9 / (7.4e10 净资产 + 1.5e10 有息负债) = 0.0787，前端 ratio() 再乘 100
        self.assertAlmostEqual(self._valuation()["roic"], 7.0e9 / 8.9e10)

    def test_roic_uses_inferred_zero_debt_when_sheet_is_valid(self):
        # 有息负债科目全为 null，但整张表有效 -> 按规则 2 推断为 0，
        # ROIC 照常计算，只是来源标成 inferred_zero
        fin = self._fin()
        for k in ("short_loan", "long_loan"):
            fin["balance"][0][k] = None
        quote = {"price": 20.0, "total_market_cap": 6.3e10, "pe_ttm": 9.0, "pb": 0.85}
        m, _, _ = engine.build_metrics("600741", quote, fin)
        self.assertAlmostEqual(m["current"]["roic"], 7.0e9 / 7.4e10)
        self.assertEqual(m["balance"]["interest_bearing_debt"], 0.0)
        self.assertEqual(m["balance"]["value_origin"]["interest_bearing_debt"],
                         fin_series.INFERRED_ZERO)

    def test_roic_missing_when_debt_and_sheet_both_missing(self):
        # 有息负债全空、核心科目也缺 -> 规则 3，绝不推断 0，ROIC 保持 None
        fin = self._fin()
        for k in ("short_loan", "long_loan", "total_assets", "total_liabilities",
                  "total_equity", "monetary_funds", "trading_finasset"):
            fin["balance"][0][k] = None
        quote = {"price": 20.0, "total_market_cap": 6.3e10, "pe_ttm": 9.0, "pb": 0.85}
        m, _, _ = engine.build_metrics("600741", quote, fin)
        self.assertIsNone(m["current"]["roic"])
        self.assertIsNone(m["balance"]["interest_bearing_debt"])


def _annual_from(**fields):
    """构造统一历史序列。fields 形如 net_profit=[1, 2, 3]，长度即年数。

    走 ``series.from_rows`` 补派生科目，形状与 engine 的 m["annual"] 一致。
    """
    n = max(len(v) for v in fields.values()) if fields else 0
    rows = []
    for i in range(n):
        row = {"report_period": f"{2018 + i}-12-31", "report_type": "年报",
               "publish_date": f"{2019 + i}-04-30", "source": "test"}
        for k, vals in fields.items():
            row[k] = vals[i] if i < len(vals) else None
        rows.append(row)
    return fin_series.from_rows(rows)


class TestUnifiedHistorySeries(unittest.TestCase):
    """趋势类指标统一走同一条历史序列，缺数时状态要分得清。"""

    def _comp(self, comps, name):
        return next(c for c in comps if c["name"] == name)

    def _growth(self, m):
        return self._comp(rules.score_growth(m, m)["components"], "现金流匹配")

    def _quality(self, m):
        return self._comp(rules.score_quality(m, m)["components"], rules.METRIC_CFO_NET_PROFIT_3Y)

    def _turn(self, m, name):
        return self._comp(rules.score_turnaround(m, m)["components"], name)

    # ---- CFO/净利润：1年 / 3年累计 / 5年累计 ----
    def test_scores_on_three_year_cumulative(self):
        m = _metrics()
        c = self._quality(m)
        self.assertTrue(c["eligible"])
        self.assertEqual(c["score_years"], 3)
        # OCF 2.3+2.8+3.2=8.3；NP 2.2+2.6+3.0=7.8
        self.assertAlmostEqual(c["value"], 8.3 / 7.8)

    def test_exposes_all_three_windows(self):
        c = self._quality(_metrics())
        self.assertAlmostEqual(c["ratios"]["1Y"], 3.2 / 3.0)
        self.assertAlmostEqual(c["ratios"]["3Y"], 8.3 / 7.8)
        # 5 年窗口：OCF 1.9+2.3+2.8+3.2 只有 4 年，用完整 5 年 1.6/1.9/2.3/2.8/3.2
        self.assertAlmostEqual(
            c["ratios"]["5Y"],
            sum([1.6, 1.9, 2.3, 2.8, 3.2]) / sum([1.5, 1.8, 2.2, 2.6, 3.0]))
        self.assertEqual(c["coverage"], 1.0)

    def test_cashflow_match_shares_the_same_numbers(self):
        # 成长模块的「现金流匹配」与质量模块的「CFO/净利润」必须同源同口径
        m = _metrics()
        self.assertAlmostEqual(self._growth(m)["value"], self._quality(m)["value"])
        self.assertAlmostEqual(self._growth(m)["ratios"]["3Y"], self._quality(m)["ratios"]["3Y"])

    def test_negative_cumulative_profit_is_not_applicable(self):
        # 3 年累计归母净利润 <= 0 -> 不适用，而不是「缺失」
        m = _metrics(net_profit=_series([-1, -2, -3, -4, -5, -6, -7]))
        c = self._quality(m)
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "not_applicable")
        self.assertIn("<= 0", c["reason"])
        self.assertEqual(c["coverage"], 1.0)  # 数据是全的，只是结论不适用

    def test_insufficient_history_is_distinct_from_not_applicable(self):
        # 只有 2 个完整年度 -> 真的取不到数，不能标成「不适用」
        m = _metrics(operating_cashflow=_series([1.0, 2.0]),
                     net_profit=_series([1.0, 2.0]),
                     annual=_annual_from(operating_cashflow=[1.0, 2.0], net_profit=[1.0, 2.0]))
        c = self._quality(m)
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "insufficient_history")
        self.assertLess(c["coverage"], 1.0)

    # ---- 资产负债改善 ----
    def test_balance_improving(self):
        c = self._turn(_metrics(), "资产负债改善")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["state"], "IMPROVING")
        self.assertEqual(c["score"], 15)
        self.assertGreaterEqual(len(c["sub_indicators"]), 2)

    def test_balance_deteriorating(self):
        m = _metrics(annual=_annual_from(
            total_assets=[30e9, 30e9, 30e9],
            total_liabilities=[10e9, 14e9, 18e9],
            monetary_funds=[6e9, 4e9, 2e9],
            short_loan=[0.5e9, 1.5e9, 3e9]))
        c = self._turn(m, "资产负债改善")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["state"], "DETERIORATING")
        self.assertEqual(c["score"], 0)

    def test_balance_flat_is_stable(self):
        m = _metrics(annual=_annual_from(
            total_assets=[30e9, 30e9, 30e9],
            total_liabilities=[12e9, 12e9, 12e9],
            monetary_funds=[5e9, 5e9, 5e9],
            short_loan=[1e9, 1e9, 1e9]))
        c = self._turn(m, "资产负债改善")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["state"], "STABLE")
        self.assertEqual(c["score"], 8)

    def test_balance_partial_fields_still_scores(self):
        # 6 个子指标里只有 5 个拿得到 -> 照样判定，coverage 如实反映
        m = _metrics(annual=_annual_from(
            total_assets=[30e9, 30e9, 30e9],
            total_liabilities=[14e9, 13e9, 12e9],
            monetary_funds=[4e9, 4.5e9, 5e9],
            short_loan=[1.4e9, 1.2e9, 1.0e9]))
        c = self._turn(m, "资产负债改善")
        self.assertTrue(c["eligible"])
        self.assertAlmostEqual(c["coverage"], round(5 / 6, 4))
        self.assertEqual([d["name"] for d in c["sub_indicators"] if d["available"]][-1], "净现金")

    def test_balance_too_few_sub_indicators(self):
        # 只拿得到资产负债率一项 -> 不足以判定趋势
        m = _metrics(annual=_annual_from(
            total_assets=[30e9, 30e9, 30e9],
            total_liabilities=[14e9, 13e9, 12e9]))
        c = self._turn(m, "资产负债改善")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "insufficient_history")
        self.assertEqual(c["state"], None)

    def test_balance_short_history(self):
        m = _metrics(annual=_annual_from(
            total_assets=[30e9, 30e9], total_liabilities=[14e9, 12e9],
            monetary_funds=[4e9, 5e9], short_loan=[1.4e9, 1.0e9]))
        c = self._turn(m, "资产负债改善")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "insufficient_history")

    # ---- 利润反转 / 现金流改善 ----
    def test_profit_turnaround_uses_deduct_profit(self):
        m = _metrics(deduct_profit=_series([-1, -1, -1, -1, -1, -0.5, 1.5]))
        c = self._turn(m, "利润反转")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["state"], "转正")
        self.assertEqual(c["score"], 30)
        self.assertEqual(c["value"], 1.5)

    def test_profit_turnaround_to_loss(self):
        m = _metrics(deduct_profit=_series([1, 1, 1, 1, 1, 2, -3]))
        c = self._turn(m, "利润反转")
        self.assertEqual(c["state"], "转亏")
        self.assertEqual(c["score"], 0)

    def test_profit_cagr_sign_switch_is_not_applicable(self):
        # 扣非利润首尾正负切换 -> 不适用，不是「缺数据」
        m = _metrics(deduct_profit=_series([3, 3, 3, 3, 3, 1, -1]))
        c = self._comp(rules.score_growth(m, m)["components"], "扣非利润CAGR")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "not_applicable")
        self.assertIn("正负切换", c["reason"])

    def test_profit_cagr_negative_start_is_not_applicable(self):
        m = _metrics(deduct_profit=_series([-3, -2, -1, 1, 2, 3, 4]))
        c = self._comp(rules.score_growth(m, m)["components"], "扣非利润CAGR")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "not_applicable")

    def test_profit_cagr_short_history_is_missing(self):
        m = _metrics(deduct_profit=_series([1.0, 2.0]))
        c = self._comp(rules.score_growth(m, m)["components"], "扣非利润CAGR")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "missing_data")

    def test_profit_turnaround_needs_two_years(self):
        m = _metrics(deduct_profit=_series([2.0]))
        c = self._turn(m, "利润反转")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "missing_data")

    def test_cashflow_improvement_turns_positive(self):
        m = _metrics(operating_cashflow=_series([1, 1, 1, 1, 1, -2, 3]))
        c = self._turn(m, "现金流改善")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["state"], "转正")
        self.assertEqual(c["score"], 20)
        self.assertEqual(c["value"], 3)

    def test_cashflow_improvement_keeps_negative(self):
        m = _metrics(operating_cashflow=_series([1, 1, 1, 1, 1, -2, -3]))
        c = self._turn(m, "现金流改善")
        self.assertEqual(c["state"], "持续为负")
        self.assertEqual(c["score"], 0)

    def test_cashflow_component_reports_fcf_and_ratio(self):
        c = self._turn(_metrics(), "现金流改善")
        self.assertEqual(c["state"], "改善")
        self.assertEqual(sorted(c["cfo"]), ["2022", "2023", "2024"])
        self.assertEqual(sorted(c["fcf"]), ["2022", "2023", "2024"])
        self.assertAlmostEqual(c["cfo_netprofit_1y"], 3.2 / 3.0)

    # ---- 派息率 ----
    def test_payout_not_applicable_when_loss(self):
        m = _metrics(net_profit=_series([1, 1, 1, 1, 1, 1, -2]))
        m["current"]["payout_ratio"] = None
        c = self._comp(rules.score_dividend(m, m)["components"], "派息率")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "not_applicable")

    def test_payout_zero_when_no_dividend(self):
        m = _metrics(dividends=[])
        m["current"]["payout_ratio"] = None
        c = self._comp(rules.score_dividend(m, m)["components"], "派息率")
        self.assertTrue(c["eligible"])
        self.assertEqual(c["value"], 0.0)

    def test_payout_missing_without_dividend_data(self):
        m = _metrics(dividends=None)
        m["current"]["payout_ratio"] = None
        c = self._comp(rules.score_dividend(m, m)["components"], "派息率")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["status"], "missing_data")

    # ---- 审计：每个细则都要带 coverage ----
    def test_every_component_reports_coverage(self):
        for scorer in [rules.score_growth, rules.score_quality, rules.score_value,
                       rules.score_dividend, rules.score_cigar_butt, rules.score_asset_value,
                       rules.score_cyclical, rules.score_turnaround, rules.score_cyclical_position]:
            s = scorer(_metrics(), _metrics())
            for c in s["components"]:
                self.assertIn("coverage", c, f"{scorer.__name__} 的 {c['name']} 没有 coverage")
                self.assertIsNotNone(c["coverage"])

    def test_components_never_contain_nan_or_inf(self):
        _math = math
        for scorer in [rules.score_growth, rules.score_quality, rules.score_value,
                       rules.score_dividend, rules.score_cigar_butt, rules.score_asset_value,
                       rules.score_cyclical, rules.score_turnaround, rules.score_cyclical_position]:
            s = scorer(_metrics(), _metrics())
            for c in s["components"]:
                for k in ("value", "score", "coverage"):
                    v = c.get(k)
                    if isinstance(v, float):
                        self.assertFalse(_math.isnan(v) or _math.isinf(v),
                                         f"{scorer.__name__}/{c['name']}.{k} = {v}")


class TestInterestBearingDebtSemantics(unittest.TestCase):
    """有息负债的语义聚合，以及每个科目的取值来源。

    东财资产负债表固定返回全部字段、公司没有的科目给 null，
    所以「null → 0」不能全局执行，必须看整张表是否有效。
    """

    def _sheet(self, **kw):
        base = {"report_period": "2026-06-30", "source": "test",
                "total_assets": 3.0e11, "total_liabilities": 1.1e11,
                "total_equity": 1.9e11, "monetary_funds": 4.0e10,
                "trading_finasset": 2.0e9,
                "short_loan": None, "long_loan": None,
                "bond_payable": None, "noncurrent_liab_1year": None,
                "lease_liab": None}
        base.update(kw)
        return fin_series.enrich(base)

    # ---- 规则 1：表有效时，单个科目 null 按「该科目不存在」处理为 0 ----
    def test_rule1_single_null_account_counts_as_zero(self):
        e = self._sheet(short_loan=1.2e10)
        self.assertEqual(e["interest_bearing_debt"], 1.2e10)
        comps = fin_series.debt_aggregate(e, fin_series.DEBT_FIELDS_V1)["components"]
        self.assertEqual(comps["short_loan"]["value_origin"], fin_series.REPORTED)
        self.assertEqual(comps["long_loan"]["value_origin"], fin_series.INFERRED_ZERO)
        self.assertEqual(comps["long_loan"]["value"], 0.0)

    def test_rule1_mixed_accounts_sum_only_what_exists(self):
        e = self._sheet(short_loan=1.2e10, long_loan=3.0e9, bond_payable=5.0e8)
        self.assertEqual(e["interest_bearing_debt"], 1.55e10)
        self.assertEqual(e["short_debt"], 1.2e10)  # 短期借款 + 一年内到期

    # ---- 规则 2：全部 null 但核心科目正常 -> 推断为 0，并留痕 ----
    def test_rule2_all_null_with_valid_sheet_infers_zero(self):
        e = self._sheet()
        agg = fin_series.debt_aggregate(e, fin_series.DEBT_FIELDS_V1)
        self.assertEqual(agg["value"], 0.0)
        self.assertEqual(agg["status"], "ok")
        self.assertEqual(agg["value_origin"], fin_series.INFERRED_ZERO)
        self.assertEqual(agg["reason"], "Provider uses null for absent balance-sheet accounts")
        self.assertEqual(e["value_origin"]["interest_bearing_debt"], fin_series.INFERRED_ZERO)

    def test_rule2_inferred_zero_still_participates_in_calculation(self):
        # 推断出的 0 可以参与计算和评分，净现金 = 现金类资产 - 0
        e = self._sheet()
        self.assertEqual(e["net_cash"], 4.2e10)
        self.assertEqual(e["net_cash_full"], 4.2e10)

    # ---- 规则 3：全部 null 且核心科目也缺 -> 不允许推断 ----
    def test_rule3_missing_core_fields_never_infers_zero(self):
        e = self._sheet(total_assets=None, total_liabilities=None,
                        total_equity=None, monetary_funds=None)
        agg = fin_series.debt_aggregate(e, fin_series.DEBT_FIELDS_V1)
        self.assertIsNone(agg["value"])
        self.assertEqual(agg["status"], "missing_data")
        self.assertNotEqual(agg["value_origin"], fin_series.INFERRED_ZERO)
        self.assertIsNone(e["interest_bearing_debt"])
        self.assertIsNone(e["net_cash"])

    def test_rule3_detects_each_core_field(self):
        for missing in fin_series.CORE_BALANCE_FIELDS:
            with self.subTest(core=missing):
                e = self._sheet(**{missing: None})
                self.assertFalse(fin_series.is_valid_balance_sheet(e))
                self.assertIsNone(e["interest_bearing_debt"])

    # ---- 两种口径：V1 保持不变，FULL 加入租赁负债 ----
    def test_lease_liability_only_in_full_set(self):
        e = self._sheet(short_loan=1.0e10, lease_liab=8.0e8)
        self.assertEqual(e["interest_bearing_debt"], 1.0e10)          # V1：不含租赁负债
        self.assertEqual(e["interest_bearing_debt_full"], 1.08e10)    # 完整口径
        # 评分口径逐字节不变，避免静默改动 SCORING_V1
        self.assertEqual(fin_series.DEBT_FIELDS_V1,
                         ("short_loan", "long_loan", "bond_payable", "noncurrent_liab_1year"))

    # ---- 取值来源 ----
    def test_goodwill_null_is_inferred_zero(self):
        e = self._sheet(goodwill=None)
        self.assertEqual(e["goodwill"], 0.0)
        self.assertEqual(e["value_origin"]["goodwill"], fin_series.INFERRED_ZERO)

    def test_goodwill_reported_is_not_marked_inferred(self):
        e = self._sheet(goodwill=2.5e9)
        self.assertEqual(e["goodwill"], 2.5e9)
        self.assertEqual(e["value_origin"]["goodwill"], fin_series.REPORTED)

    def test_every_field_has_a_value_origin(self):
        e = self._sheet(short_loan=1.0e9, long_loan=2.0e9)
        for f in fin_series.FIELDS:
            self.assertIn(f, e["value_origin"], f"{f} 缺少取值来源标注")
        # 派生项必须标成 derived，不能冒充披露值
        for f in ("debt_asset_ratio", "cash_like", "net_cash", "interest_bearing_debt"):
            self.assertEqual(e["value_origin"][f], fin_series.DERIVED)

    def test_origin_vocabulary_is_closed(self):
        allowed = {fin_series.REPORTED, fin_series.DERIVED, fin_series.INFERRED_ZERO,
                   fin_series.FALLBACK, fin_series.ESTIMATED, fin_series.MISSING}
        e = self._sheet(short_loan=1.0e9, goodwill=None)
        self.assertTrue(set(e["value_origin"].values()) <= allowed)


def _period_rows(**periods):
    """按报告期构造统一序列。periods 形如 {"2026-06-30": {"revenue": 100}}。"""
    rows = []
    for p, fields in periods.items():
        row = {"report_period": p, "report_type": providers.report_type_of(p),
               "publish_date": "2026-08-30", "source": "test"}
        row.update(fields)
        rows.append(row)
    return fin_series.from_rows(rows)


class TestTTM(unittest.TestCase):
    """TTM 必须是真实滚动 12 个月，不能拿半年数 ×2 冒充。"""

    def _rows(self):
        return _period_rows(**{
            "2026-06-30": {"net_profit": 120.0, "revenue": 600.0},
            "2025-12-31": {"net_profit": 200.0, "revenue": 1000.0},
            "2025-06-30": {"net_profit": 100.0, "revenue": 500.0},
        })

    def test_ttm_is_current_plus_last_fy_minus_last_year_interim(self):
        # 2026H1 + 2025FY - 2025H1 = 120 + 200 - 100
        self.assertAlmostEqual(fin_series.ttm(self._rows(), "net_profit", "2026-06-30"), 220.0)

    def test_ttm_is_not_doubled_half_year(self):
        self.assertNotAlmostEqual(fin_series.ttm(self._rows(), "net_profit", "2026-06-30"), 240.0)

    def test_annual_report_is_its_own_ttm(self):
        self.assertAlmostEqual(fin_series.ttm(self._rows(), "net_profit", "2025-12-31"), 200.0)

    def test_missing_leg_returns_none_instead_of_guessing(self):
        rows = _period_rows(**{
            "2026-06-30": {"net_profit": 120.0},
            "2025-12-31": {"net_profit": 200.0},
        })  # 缺 2025-06-30 这条腿
        self.assertIsNone(fin_series.ttm(rows, "net_profit", "2026-06-30"))

    def test_missing_value_on_one_leg_returns_none(self):
        rows = _period_rows(**{
            "2026-06-30": {"net_profit": 120.0},
            "2025-12-31": {"net_profit": None},
            "2025-06-30": {"net_profit": 100.0},
        })
        self.assertIsNone(fin_series.ttm(rows, "net_profit", "2026-06-30"))

    def test_ttm_legs(self):
        self.assertEqual(fin_series.ttm_legs("2026-06-30"),
                         ["2026-06-30", "2025-12-31", "2025-06-30"])
        self.assertEqual(fin_series.ttm_legs("2026-12-31"), ["2026-12-31"])
        self.assertEqual(fin_series.ttm_legs(""), [])

    def test_ttm_open_period(self):
        self.assertEqual(fin_series.ttm_open_period("2026-06-30"), "2025-06-30")
        self.assertEqual(fin_series.ttm_open_period("2026-12-31"), "2025-12-31")

    def test_period_labels_separate_flow_from_point(self):
        # 同一个报告期，流量类说 Q2，时点类说 H1，两者不能混在同一个格子里
        self.assertEqual(fin_series.ttm_label("2026-06-30"), "2026Q2")
        self.assertEqual(fin_series.point_label("2026-06-30"), "2026H1")
        self.assertEqual(fin_series.point_label("2026-12-31"), "2026FY")

    def test_annual_only_drops_interims(self):
        rows = self._rows()
        self.assertEqual([e["report_period"] for e in fin_series.annual_only(rows)],
                         ["2025-12-31"])


class TestOverviewBasis(unittest.TestCase):
    """概览统一口径：流量类走 TTM，时点类走最新一期资产负债表，
    每项都必须自带 period/basis，不同口径不许挤在同一格。"""

    MCAP = 1.4e11

    def _fin(self):
        return {
            "cache_version": engine.FIN_CACHE_VERSION,
            "indicators": [{"report_period": "2026-06-30", "roe": 9.0,
                            "gross_margin": 12.4, "debt_asset_ratio": 36.0,
                            "bps": 12.8, "current_ratio": 2.0}],
            "industry": "汽车零部件", "dividends": [],
            "income": [
                {"report_period": "2026-06-30", "report_type": "中报",
                 "publish_date": "2026-08-28", "source": "test",
                 "revenue": 8.0e10, "operate_cost": 7.0e10, "net_profit": 6.0e9,
                 "deduct_profit": 5.5e9, "total_profit": 8.0e9,
                 "income_tax": 2.0e9, "finance_expense": 5.0e8},
                {"report_period": "2025-12-31", "report_type": "年报",
                 "publish_date": "2026-03-28", "source": "test",
                 "revenue": 1.6e11, "operate_cost": 1.4e11, "net_profit": 1.2e10,
                 "deduct_profit": 1.1e10, "total_profit": 1.6e10,
                 "income_tax": 4.0e9, "finance_expense": 1.0e9},
                {"report_period": "2025-06-30", "report_type": "中报",
                 "publish_date": "2025-08-28", "source": "test",
                 "revenue": 7.2e10, "operate_cost": 6.3e10, "net_profit": 5.0e9,
                 "deduct_profit": 4.6e9, "total_profit": 6.6e9,
                 "income_tax": 1.6e9, "finance_expense": 4.5e8},
                {"report_period": "2024-12-31", "report_type": "年报",
                 "publish_date": "2025-03-28", "source": "test",
                 "revenue": 1.4e11, "operate_cost": 1.22e11, "net_profit": 1.0e10,
                 "deduct_profit": 9.2e9, "total_profit": 1.35e10,
                 "income_tax": 3.4e9, "finance_expense": 9.0e8},
            ],
            "cashflow": [
                {"report_period": "2026-06-30", "report_type": "中报",
                 "publish_date": "2026-08-28", "source": "test",
                 "operating_cashflow": 9.0e9, "capex": 4.0e9, "invest_cashflow": -5.0e9},
                {"report_period": "2025-12-31", "report_type": "年报",
                 "publish_date": "2026-03-28", "source": "test",
                 "operating_cashflow": 1.8e10, "capex": 8.0e9, "invest_cashflow": -9.0e9},
                {"report_period": "2025-06-30", "report_type": "中报",
                 "publish_date": "2025-08-28", "source": "test",
                 "operating_cashflow": 8.0e9, "capex": 3.5e9, "invest_cashflow": -4.0e9},
                {"report_period": "2024-12-31", "report_type": "年报",
                 "publish_date": "2025-03-28", "source": "test",
                 "operating_cashflow": 1.5e10, "capex": 7.0e9, "invest_cashflow": -8.0e9},
            ],
            "balance": [
                {"report_period": "2026-06-30", "report_type": "中报",
                 "publish_date": "2026-08-28", "source": "test",
                 "total_assets": 2.0e11, "total_liabilities": 7.2e10,
                 "total_equity": 1.28e11, "parent_equity": 1.24e11,
                 "monetary_funds": 3.0e10, "trading_finasset": 2.0e9,
                 "short_loan": 8.0e9, "long_loan": 4.0e9, "bond_payable": 2.0e9,
                 "noncurrent_liab_1year": 1.0e9, "lease_liab": 5.0e8},
                {"report_period": "2025-12-31", "report_type": "年报",
                 "publish_date": "2026-03-28", "source": "test",
                 "total_assets": 2.0e11, "total_liabilities": 8.0e10,
                 "total_equity": 1.20e11, "parent_equity": 1.16e11,
                 "monetary_funds": 2.6e10, "trading_finasset": 1.5e9,
                 "short_loan": 9.0e9, "long_loan": 4.5e9, "bond_payable": 2.0e9,
                 "noncurrent_liab_1year": 1.0e9, "lease_liab": 6.0e8},
                {"report_period": "2025-06-30", "report_type": "中报",
                 "publish_date": "2025-08-28", "source": "test",
                 "total_assets": 2.0e11, "total_liabilities": 8.0e10,
                 "total_equity": 1.20e11, "parent_equity": 1.16e11,
                 "monetary_funds": 2.6e10, "trading_finasset": 1.5e9,
                 "short_loan": 9.0e9, "long_loan": 4.5e9, "bond_payable": 2.0e9,
                 "noncurrent_liab_1year": 1.0e9, "lease_liab": 6.0e8},
                {"report_period": "2024-12-31", "report_type": "年报",
                 "publish_date": "2025-03-28", "source": "test",
                 "total_assets": 1.9e11, "total_liabilities": 9.0e10,
                 "total_equity": 1.0e11, "parent_equity": 9.6e10,
                 "monetary_funds": 2.2e10, "trading_finasset": 1.0e9,
                 "short_loan": 1.0e10, "long_loan": 5.0e9, "bond_payable": 2.0e9,
                 "noncurrent_liab_1year": 1.0e9, "lease_liab": 7.0e8},
            ],
        }

    def _overview(self):
        quote = {"price": 20.0, "total_market_cap": self.MCAP,
                 "pe_ttm": 9.0, "pb": 0.85}
        m, _, _ = engine.build_metrics("600741", quote, self._fin())
        self.m = m
        return m["current"]["overview"]

    # ---- 口径标注 ----
    def test_every_metric_carries_period_and_basis(self):
        o = self._overview()
        flow = ("pe_ttm", "roe_ttm", "roic_ttm", "gross_margin_ttm", "fcf_yield_ttm")
        point = ("debt_asset_ratio", "net_cash", "net_cash_to_mcap", "book_value",
                 # 报表口径净现金：与上面 net_cash 同值，但名字分开，避免与
                 # 「调整后净现金/市值」同名异义（见 METRIC_REPORTED_*）
                 "reported_net_cash", "reported_net_cash_to_mcap")
        for k in flow:
            self.assertEqual(o[k]["basis"], "TTM · 2026Q2", f"{k} 应标 TTM 口径")
            self.assertEqual(o[k]["period"], "2026-06-30")
        for k in point:
            self.assertEqual(o[k]["basis"], "2026H1", f"{k} 应标时点口径")
            self.assertEqual(o[k]["period"], "2026-06-30")
        self.assertEqual(sorted(set(flow) | set(point)), sorted(
            k for k in o if not k.startswith("_")))

    def test_flow_metrics_are_never_labelled_as_point_in_time(self):
        o = self._overview()
        for k in ("roe_ttm", "roic_ttm", "gross_margin_ttm", "fcf_yield_ttm"):
            self.assertIsNotNone(o[k]["basis"])
            self.assertIn("TTM", o[k]["basis"])
            self.assertNotEqual(o[k]["basis"], o["debt_asset_ratio"]["basis"])

    # ---- TTM 数值 ----
    def test_roe_uses_ttm_profit_over_average_equity(self):
        o = self._overview()
        ttm_np = 6.0e9 + 1.2e10 - 5.0e9
        avg_eq = (1.24e11 + 1.16e11) / 2
        self.assertAlmostEqual(o["roe_ttm"]["value"], ttm_np / avg_eq)

    def test_roic_uses_ttm_nopat_over_average_capital(self):
        o = self._overview()
        ttm_nopat = (8.0e9 + 5.0e8 - 2.0e9) + (1.6e10 + 1.0e9 - 4.0e9) - (6.6e9 + 4.5e8 - 1.6e9)
        cap_now = 1.24e11 + 1.55e10   # 归母权益 + 含租赁负债的有息负债
        cap_open = 1.16e11 + 1.71e10
        self.assertAlmostEqual(o["roic_ttm"]["value"], ttm_nopat / ((cap_now + cap_open) / 2))

    def test_gross_margin_is_ttm_and_in_percentage_points(self):
        o = self._overview()
        ttm_rev = 8.0e10 + 1.6e11 - 7.2e10
        ttm_cost = 7.0e10 + 1.4e11 - 6.3e10
        self.assertAlmostEqual(o["gross_margin_ttm"]["value"],
                               (ttm_rev - ttm_cost) / ttm_rev * 100.0)
        self.assertAlmostEqual(o["gross_margin_ttm"]["value"], 12.5)

    def test_fcf_yield_uses_ttm_free_cashflow(self):
        o = self._overview()
        ttm_fcf = (9.0e9 - 4.0e9) + (1.8e10 - 8.0e9) - (8.0e9 - 3.5e9)
        self.assertAlmostEqual(o["fcf_yield_ttm"]["value"], ttm_fcf / self.MCAP)

    def test_ttm_is_not_the_interim_value(self):
        # 半年数直接当期值是最容易犯的错，这里明确挡住
        o = self._overview()
        self.assertNotAlmostEqual(o["roe_ttm"]["value"], 6.0e9 / 1.24e11)

    # ---- 时点数值 ----
    def test_point_metrics_use_latest_balance_sheet(self):
        o = self._overview()
        self.assertAlmostEqual(o["debt_asset_ratio"]["value"], 7.2e10 / 2.0e11 * 100.0)
        self.assertAlmostEqual(o["book_value"]["value"], 1.24e11)
        # 净现金用含租赁负债的完整口径：3.2e10 - (8+4+2+1+0.5)e9
        self.assertAlmostEqual(o["net_cash"]["value"], 3.2e10 - 1.55e10)
        self.assertAlmostEqual(o["net_cash_to_mcap"]["value"], (3.2e10 - 1.55e10) / self.MCAP)

    # ---- 评分口径不被中报污染 ----
    def test_scoring_series_stays_annual_only(self):
        self._overview()
        periods = [e["report_period"] for e in self.m["annual"]]
        self.assertEqual(periods, ["2024-12-31", "2025-12-31"])
        self.assertNotIn("2026-06-30", periods)
        # 完整序列（含中报）单独存放，只给概览 TTM 用
        self.assertIn("2026-06-30", [e["report_period"] for e in self.m["full"]])

    def test_meta_exposes_the_ttm_legs(self):
        o = self._overview()
        self.assertEqual(o["_meta"]["ttm_legs"],
                         ["2026-06-30", "2025-12-31", "2025-06-30"])

    def test_missing_leg_degrades_to_missing_not_a_half_year(self):
        fin = self._fin()
        fin["income"] = [r for r in fin["income"] if r["report_period"] != "2025-06-30"]
        quote = {"price": 20.0, "total_market_cap": self.MCAP, "pe_ttm": 9.0, "pb": 0.85}
        m, _, _ = engine.build_metrics("600741", quote, fin)
        o = m["current"]["overview"]
        self.assertIsNone(o["roe_ttm"]["value"])
        self.assertEqual(o["roe_ttm"]["status"], "missing_data")
        self.assertIsNotNone(o["roe_ttm"]["reason"])
        # 时点类不受影响，照常有值
        self.assertIsNotNone(o["debt_asset_ratio"]["value"])

    def test_pe_falls_back_to_market_cap_over_ttm_profit(self):
        fin = self._fin()
        quote = {"price": 20.0, "total_market_cap": self.MCAP, "pb": 0.85}  # 行情没给 PE
        m, _, _ = engine.build_metrics("600741", quote, fin)
        o = m["current"]["overview"]
        ttm_np = 6.0e9 + 1.2e10 - 5.0e9
        self.assertAlmostEqual(o["pe_ttm"]["value"], self.MCAP / ttm_np)
        self.assertEqual(o["pe_ttm"]["value_origin"], fin_series.DERIVED)

    def test_composite_origin_reflects_the_weakest_input(self):
        # NOPAT 凑不齐时退回归母净利润，ROIC 必须标成估计值而不是计算值
        fin = self._fin()
        for r in fin["income"]:
            r["finance_expense"] = None
        quote = {"price": 20.0, "total_market_cap": self.MCAP, "pe_ttm": 9.0, "pb": 0.85}
        m, _, _ = engine.build_metrics("600741", quote, fin)
        self.assertEqual(m["current"]["overview"]["roic_ttm"]["value_origin"],
                         fin_series.ESTIMATED)

    def test_missing_metric_never_claims_a_value_origin(self):
        fin = self._fin()
        fin["income"] = [r for r in fin["income"] if r["report_period"] != "2025-06-30"]
        quote = {"price": 20.0, "total_market_cap": self.MCAP, "pe_ttm": 9.0, "pb": 0.85}
        m, _, _ = engine.build_metrics("600741", quote, fin)
        o = m["current"]["overview"]
        self.assertIsNone(o["roe_ttm"]["value"])
        self.assertEqual(o["roe_ttm"]["value_origin"], fin_series.MISSING)

    def test_overview_never_contains_nan_or_inf(self):
        o = self._overview()
        for k, v in o.items():
            if k.startswith("_"):
                continue
            if v["value"] is not None:
                self.assertTrue(math.isfinite(v["value"]), f"{k} 不是有限数")


class _FakeEmweb(providers.EastmoneyProvider):
    """假 provider：记录每次请求带的 dates，并按真实接口的上限截断。

    实测 emweb 的 dates 参数一次最多 5 个，超出的从尾部静默丢弃。
    这里如实复现，用来挡住「补 TTM 的中报把最早的年报挤掉」这类回归。
    """

    # 服务端的真实上限（实测值），故意写死：providers 那边的常量若被调大，
    # 这里就会开始丢数据，测试随即失败。
    API_CAP = 5

    def __init__(self, rows_by_period):
        self.rows_by_period = rows_by_period
        self.requests = []

    def _get(self, url, referer="https://data.eastmoney.com/"):
        dates = url.split("dates=")[1].split("&")[0].split(",")
        self.requests.append(dates)
        out = []
        for p in dates[:self.API_CAP]:
            row = self.rows_by_period.get(p)
            if row is None:
                continue
            out.append({"REPORT_DATE": p + " 00:00:00", "NOTICE_DATE": "2026-08-30 00:00:00", **row})
        return {"data": out}


class TestEmwebDateBatching(unittest.TestCase):
    """接口一次只吃 5 个报告期，历史 + TTM 必须分批取，不能被截断。"""

    def _provider(self):
        rows = {f"{y}-12-31": {"TOTAL_ASSETS": float(y)} for y in range(2021, 2026)}
        rows["2026-06-30"] = {"TOTAL_ASSETS": 2026.0}
        rows["2025-06-30"] = {"TOTAL_ASSETS": 2025.5}
        return _FakeEmweb(rows)

    def test_annual_history_survives_the_extra_ttm_periods(self):
        p = self._provider()
        rows = p.get_balance_sheet_history("600741", years=5,
                                           extra_periods=providers.ttm_periods("2026-06-30"))
        periods = [r["report_period"] for r in rows]
        for y in range(2021, 2027):
            if y == 2026:
                continue
            self.assertIn(f"{y}-12-31", periods, f"{y} 年报被截断了")
        self.assertIn("2026-06-30", periods)
        self.assertIn("2025-06-30", periods)
        self.assertEqual(len(periods), 7)

    def test_requests_never_exceed_the_api_cap(self):
        p = self._provider()
        p.get_balance_sheet_history("600741", years=5,
                                    extra_periods=providers.ttm_periods("2026-06-30"))
        self.assertTrue(p.requests)
        for batch in p.requests:
            self.assertLessEqual(len(batch), _FakeEmweb.API_CAP)
        self.assertLessEqual(providers.EMWEB_MAX_DATES, _FakeEmweb.API_CAP)

    def test_annual_only_history_needs_a_single_request(self):
        p = self._provider()
        rows = p.get_balance_sheet_history("600741", years=5)
        self.assertEqual(len(p.requests), 1)
        self.assertEqual(len(rows), 5)

    def test_duplicate_periods_are_not_repeated(self):
        p = self._provider()
        rows = p.get_balance_sheet_history(
            "600741", years=5, extra_periods=providers.ttm_periods("2025-12-31"))
        periods = [r["report_period"] for r in rows]
        self.assertEqual(len(periods), len(set(periods)))


class TestRateThresholdUnits(unittest.TestCase):
    """V1.1 修的 5 处「百分数当小数比阈值」。

    共同症状：该指标对输入完全不敏感，恒定满分或恒定零分。所以这里一律
    断言「两个明显不同的输入必须给出不同得分」，而不是断言具体插值数值。
    """

    @staticmethod
    def _comp(block, name):
        return [c for c in block["components"] if c["name"] == name][0]

    @staticmethod
    def _years(series, n):
        return [(f"{2026 - i}-12-31", series) for i in range(n)][::-1]

    def _quality(self, roe_pct=None, dr_pct=None):
        m = {"current": {}, "balance": {}, "is_financial": False,
             "roe": [], "roic": [], "net_profit": [], "annual": []}
        if roe_pct is not None:
            m["roe"] = self._years(roe_pct, 3)
        if dr_pct is not None:
            m["current"]["debt_asset_ratio"] = dr_pct
        return rules.score_quality(m, {})

    def test_roe_responds_to_the_input(self):
        low = self._comp(self._quality(roe_pct=5.0), "ROE")
        high = self._comp(self._quality(roe_pct=20.0), "ROE")
        self.assertLess(low["score"], high["score"])
        self.assertLess(low["score"], 20, "5% 的 ROE 不该拿满分（原先恒 20/20）")
        self.assertEqual(high["score"], 20)
        # 展示的 raw 仍是百分数，没被 /100
        self.assertAlmostEqual(high["raw"], 20.0)

    def test_debt_ratio_responds_to_the_input(self):
        light = self._comp(self._quality(dr_pct=20.0), "资产负债率")
        heavy = self._comp(self._quality(dr_pct=80.0), "资产负债率")
        self.assertGreater(light["score"], heavy["score"])
        self.assertGreater(light["score"], 0, "20% 负债率不该得 0 分（原先恒 0/15）")
        self.assertEqual(heavy["score"], 0)
        self.assertAlmostEqual(light["raw"], 20.0)

    def test_debt_ratio_fallback_matches_the_provider_unit(self):
        """自算回退值原是小数、Provider 值是百分数，两条路量纲必须一致。"""
        m = {"current": {}, "balance": {"total_liabilities": 30.0, "total_assets": 100.0},
             "is_financial": False, "roe": [], "roic": [], "net_profit": [], "annual": []}
        comp = self._comp(rules.score_quality(m, {}), "资产负债率")
        self.assertAlmostEqual(comp["raw"], 30.0)  # 百分数，不是 0.30
        self.assertGreater(comp["score"], 0)

    def _cyclical(self, gm_range):
        gms = [10.0, 10.0 + gm_range]
        m = {"net_profit": self._years(1.0, 5), "revenue": self._years(1.0, 5),
             "gross_margin": [(f"{2025 + i}-12-31", v) for i, v in enumerate(gms)],
             "industry": "汽车零部件", "current": {},
             "operating_cashflow": [], "annual": [], "dividend": []}
        return rules.score_cyclical(m, {})

    def test_gross_margin_range_responds_to_the_input(self):
        flat = self._comp(self._cyclical(1.0), "毛利率波动")   # 1 个百分点
        wild = self._comp(self._cyclical(30.0), "毛利率波动")  # 30 个百分点
        self.assertLess(flat["score"], wild["score"])
        self.assertLess(flat["score"], 20, "1 个点的波动不该拿满分（原先恒 20/20）")
        self.assertEqual(wild["score"], 20)

    def _turnaround(self, gm_recovery):
        gms = [(f"{2025 + i}-12-31", v) for i, v in enumerate([10.0, 10.0 + gm_recovery])]
        m = {"deduct_profit": [], "gross_margin": gms, "revenue": [], "operating_cashflow": [],
             "free_cashflow": [], "annual": [], "net_profit": [], "current": {}, "balance": []}
        return rules.score_turnaround(m, {})

    def test_gross_margin_recovery_responds_to_the_input(self):
        flat = self._comp(self._turnaround(0.5), "毛利率恢复")   # 回升 0.5 个点
        strong = self._comp(self._turnaround(20.0), "毛利率恢复")  # 回升 20 个点
        self.assertLess(flat["score"], strong["score"])
        self.assertLess(flat["score"], 20, "回升 0.5 个点不该拿满分（原先恒 20/20）")
        self.assertEqual(strong["score"], 20)

    def test_financial_safety_debt_branch_responds_to_the_input(self):
        """_score_financial_safety 里比较负债率的分支也是同一处量纲错。"""
        def block(dr_pct):
            m = {"current": {"debt_asset_ratio": dr_pct}, "is_financial": False,
                 "dividends": [], "annual": [], "operating_cashflow": []}
            return self._comp(rules.score_dividend(m, {}), "财务安全")
        light, heavy = block(20.0), block(80.0)
        self.assertGreater(light["score"], heavy["score"], "20% 与 80% 负债率应给出不同得分")

    def test_pct_helper_passes_none_through(self):
        self.assertIsNone(rules._pct(None))
        self.assertAlmostEqual(rules._pct(11.79), 0.1179)

    def test_rule_version_marks_the_break(self):
        # 评分体系处于探索期，只有一套当前生效规则，不再为每次修改递增版本号。
        # 版本标记本身仍然必须存在：它是读侧判定「旧实验结果」的唯一依据
        # （engine.is_legacy_rule_version），没有它跨规则时期的分数就混在一起了。
        self.assertEqual(rules.RULE_VERSION, "SCORING_EXPERIMENTAL")
        self.assertNotRegex(rules.RULE_VERSION, r"V\d+",
                            "又变回带 V1.x / V2.0 号的形式了？EXPERIMENTAL 下不再递增版本号")


class TestComponentUnits(unittest.TestCase):
    """细则「原始值」列的单位表必须覆盖所有评分项。

    raw 的形态在同一批指标里并不统一（ROIC 是小数、ROE 是百分数），
    单位靠名字猜曾经把 ROIC 显示成 0.08%。所以单位必须逐项声明，
    这里做静态扫描，防止以后新增评分项时漏登记。
    """

    #: 名字可以是字面量 `"现金/净利润"`，也可以是模块级常量 `METRIC_...`。
    #: 两者都要认——只认字面量的话，把名字抽成常量就等于从扫描里消失了，
    #: 单位表和分量名会在谁都没察觉的情况下各走各的。
    _NAME = r"(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))"
    COMPONENT_RES = (
        re.compile(r"comps\.append\(\s*\(\s*" + _NAME),             # 直接拼元组
        re.compile(r"comps\.append\(\s*_ratio_comp\(\s*" + _NAME),  # 走 _ratio_comp
        # 走 _asset_comp 的资产类分量：comps.append(_asset_comp(m, "attr", <名字>, ...))
        re.compile(r"comps\.append\(\s*_asset_comp\(\s*\w+\s*,\s*\"[^\"]+\"\s*,\s*" + _NAME),
        # 走 _percentile_comp / _pb_comp 的分位类分量（「周期位置」那三格）。名字仍
        # 写在调用点上、口径在 helper 里；扫描必须认得这种形态，否则把名字挪进
        # helper 就等于把这三格从单位表里悄悄摘掉——那正是本测试要拦的事。
        re.compile(r"comps\.append\(\s*_(?:percentile|pb)_comp\(\s*" + _NAME),
    )

    def _resolve(self, literal, ident):
        """把扫描到的名字还原成字符串：字面量直接用，标识符去 rules 里取值。"""
        if literal:
            return literal
        value = getattr(rules, ident, None)
        if not isinstance(value, str):
            self.fail(f"评分分量名引用了 rules.{ident}，但它不是字符串常量"
                      f"（拿到 {value!r}）——扫描无法确定分量叫什么名字")
        return value

    def _declared_names(self):
        src = pathlib.Path(rules.__file__).read_text(encoding="utf-8")
        names = set()
        for pattern in self.COMPONENT_RES:
            for literal, ident in pattern.findall(src):
                names.add(self._resolve(literal, ident))
        return sorted(names)

    def test_every_scored_component_declares_a_unit(self):
        names = self._declared_names()
        self.assertGreater(len(names), 20, "扫描没抓到评分项，正则可能失效了")
        undecided = [n for n in names if n not in rules.COMPONENT_UNITS]
        self.assertEqual(undecided, [], f"以下评分项没登记单位：{undecided}")

    def test_unit_table_has_no_dead_entries(self):
        names = set(self._declared_names())
        dead = sorted(k for k in rules.COMPONENT_UNITS if k not in names)
        self.assertEqual(dead, [], f"单位表里有已不存在的评分项：{dead}")

    def test_vocabulary_is_closed(self):
        allowed = {rules.MONEY, rules.MULTIPLE, rules.RATIO, rules.POINTS,
                   rules.COUNT, rules.TEXT, rules.PLAIN, rules.STATE}
        self.assertEqual(set(rules.COMPONENT_UNITS.values()) - allowed, set())

    def test_roic_is_a_fraction_not_a_percentage(self):
        """ROIC 的 raw 是小数，前端按 ratio 渲染；这里锁住这个口径。

        回归背景：ROIC 曾被当成百分数，0.0827 显示成 0.08%（差 100 倍）。
        """
        self.assertEqual(rules.COMPONENT_UNITS["ROIC"], rules.RATIO)
        self.assertEqual(rules.COMPONENT_UNITS["ROE"], rules.POINTS)

    def test_assembled_detail_carries_the_unit(self):
        block = rules._assemble([("ROIC", 0.0827, 8.0, 10)])
        self.assertEqual(block["components"][0]["unit"], rules.RATIO)
        self.assertEqual(block["components"][0]["raw"], 0.0827)

    def test_extra_unit_overrides_the_table(self):
        """复合指标用驱动值的单位，必须能盖过表里的默认值。"""
        block = rules._assemble([("负债安全", 2.48, 10.0, 10, "ok", None,
                                  {"unit": rules.MULTIPLE})])
        self.assertEqual(block["components"][0]["unit"], rules.MULTIPLE)

    def test_composite_components_expose_a_driver_value(self):
        """财务安全 / 财务风险 / 负债安全 原先 raw=None，显示成「数据缺失」。

        三个复合分量的驱动值现在都取自 AssetMetricProvider，
        所以这里直接注入一份合成资产指标。
        """
        m = {"current": {"debt_asset_ratio": 64.1},
             "assets": _fake_assets(net_cash_ratio=0.4512,
                                    interest_debt_cover=2.4775),
             "is_financial": False, "operating_cashflow": [],
             "balance": {}, "net_profit": [], "dividends": [], "annual": []}
        dividend = rules.score_dividend(m, {})
        safety = [c for c in dividend["components"] if c["name"] == "财务安全"][0]
        self.assertAlmostEqual(safety["raw"], 0.4512)
        cigar = rules.score_cigar_butt(m, {})
        risk = [c for c in cigar["components"] if c["name"] == "财务风险"][0]
        self.assertAlmostEqual(risk["raw"], 2.4775)
        asset = rules.score_asset_value(m, {})
        liability = [c for c in asset["components"] if c["name"] == "负债安全"][0]
        self.assertAlmostEqual(liability["raw"], 2.4775)

    def test_balance_trend_shows_the_state_not_the_vote_count(self):
        """资产负债改善原先展示 -6~+6 的票数，现在展示趋势状态。"""
        m = {"revenue": [], "net_profit": [], "gross_margin": [], "current": {},
             "balance": {}, "operating_cashflow": [], "deduct_profit": [],
             "free_cashflow": [], "is_financial": False}
        block = rules.score_turnaround(m, {})
        comp = [c for c in block["components"] if c["name"] == "资产负债改善"][0]
        if comp.get("missing"):
            self.assertIsNone(comp["raw"])
        else:
            self.assertIn(comp["raw"], ("改善", "稳定", "恶化"))
            self.assertEqual(comp["unit"], rules.STATE)

    def test_money_components_are_stamped_as_money(self):
        for name in ("利润反转", "现金流改善"):
            self.assertEqual(rules.COMPONENT_UNITS[name], rules.MONEY)


class TestMissingDataRebalance(unittest.TestCase):
    def test_missing_rebalances(self):
        # PE 缺失时，价值分按剩余项重平衡，而不是记 0 或满分
        m = _metrics()
        m["current"]["pe_ttm"] = None
        s = rules.score_value(m, m)
        self.assertIsNotNone(s["score"])
        self.assertLess(s["completeness"], 1.0)
        # 缺失项有标记
        pe_comp = next(c for c in s["components"] if c["name"] == "PE")
        self.assertTrue(pe_comp["missing"])


class TestResearchSnapshotStorage(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    @staticmethod
    def _snapshot(price=10.0):
        return {
            "stock_code": "600000", "date": "2026-09-21 10:00:00",
            "current_price": price, "report_period": "2026-06-30", "rule_version": "test",
            "type_scores": {"value": 80}, "financial_metrics": {"revenue": [1, 2]},
            "valuation_metrics": {"pb": 1.0}, "risk_flags": [],
            "category_scores": [{"key": "valuation", "score": 80}],
            "total_score": 80.0, "confidence": 1.0,
        }

    def test_duplicate_snapshot_is_not_stored(self):
        first = self._snapshot()
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, first))

        duplicate = self._snapshot()
        duplicate["date"] = "2026-09-21 10:05:00"
        self.assertFalse(research_db.add_snapshot_if_changed(self.conn, duplicate))
        self.assertEqual(len(research_db.list_snapshots(self.conn, "600000")), 1)

        changed = self._snapshot(price=10.01)
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, changed))
        self.assertEqual(len(research_db.list_snapshots(self.conn, "600000")), 2)


# ---------------------------------------------------------------------------
# 最终模板完整性：类型路由唯一性 + 量纲统一
# ---------------------------------------------------------------------------

# 一个能同时走到「周期」与「价值」两条路的合成财报：周期模块分 79，价值模块分
# 略高于它（80.06）。所以带 industry 时强制判 cyclical、不带时按排名判 value，
# 正好复现牧原那类 display/scoring 分叉。
_SYNTH_REVENUES = [1.0e10, 1.05e10, 1.10e10, 1.15e10, 1.20e10, 1.25e10, 1.30e10, 1.35e10]
_SYNTH_PROFITS = [-2e8, 1e8, -1e8, 2e8, -0.5e8, 3e8, 1e8, 4e8]
_SYNTH_MARGINS = [20.0, 30.0, 22.0, 32.0, 21.0, 31.0, 23.0, 33.0]
_SYNTH_QUOTE = {"price": 10.0, "total_market_cap": 1e10, "pe_ttm": 12.0,
                "pb": 0.9, "total_shares": 1e9}


def _synth_fin(industry):
    years = list(range(2026 - len(_SYNTH_PROFITS) + 1, 2027))
    inds, income, cash, balance = [], [], [], []
    for y, p, r, gm in zip(years, _SYNTH_PROFITS, _SYNTH_REVENUES, _SYNTH_MARGINS):
        head = {"report_period": f"{y}-12-31", "report_type": "年报",
                "publish_date": f"{y + 1}-03-31", "source": "test"}
        inds.append({**head, "revenue": r, "net_profit": p, "deduct_profit": p,
                     "roe": 10.0, "gross_margin": gm, "debt_asset_ratio": 45.0, "bps": 5.0})
        income.append({**head, "revenue": r, "net_profit": p, "deduct_profit": p})
        cash.append({**head, "operating_cashflow": r * 0.15, "capex": r * 0.05})
        balance.append({**head, "total_assets": r * 2, "total_liabilities": r * 0.9,
                        "total_equity": r * 1.1, "monetary_funds": r * 0.3,
                        "short_loan": r * 0.05, "long_loan": r * 0.1})
    return {"indicators": inds, "income": income, "cashflow": cash,
            "balance": balance, "dividends": [], "industry": industry}


# 每个模板取一个能选中它的 primary 属性
_TEMPLATE_PRIMARIES = ("growth", "quality", "dividend", "cigar_butt", "cyclical")
# 八个属性模块 + 周期位置的理论满分（_assemble 归一化后都是 0~100）
_MODULE_KEYS = ("growth", "quality", "value", "dividend",
                "cigar_butt", "asset_value", "cyclical", "turnaround")


def _max_attrs():
    return {k: {"score": 100.0} for k in _MODULE_KEYS}


def _max_metrics():
    """全满 metrics：CFO/净利润 >= 1.1、类现金覆盖有息负债 >= 2、
    五年净利与近三年经营现金流全为正 —— 非模块类分量全部顶到各自满分。

    资产类分量全部来自 AssetMetricProvider。每项都取到它那条曲线的**上限点**，
    否则 test_D「声明满分 == 实测上限」会失败：
    净现金/市值 0.80、清算价值/市值 1.60、资产价值/市值 1.60、资产流动性 0.50。
    """
    years = [f"{2026 - i}-12-31" for i in range(5)][::-1]
    return {
        "operating_cashflow": [(p, 110.0) for p in years],
        "net_profit": [(p, 100.0) for p in years],
        "free_cashflow": [(p, 50.0) for p in years],
        "current": {"short_debt_cover": 3.0, "net_cash_ratio": 0.8},
        "assets": _fake_assets(net_cash_ratio=0.80, near_cash_ratio=1.00,
                               interest_debt_cover=3.0, liquidation_ratio=1.60,
                               asset_value_ratio=1.60, liquid_asset_ratio=0.50),
    }


def _empty_metrics():
    """取不到任何财报时的 metrics。形状必须与 engine.build_metrics 一致——
    m["current"] 一定存在（只是空 dict），不能直接给 {}。"""
    return {"current": {}, "operating_cashflow": [], "net_profit": [],
            "free_cashflow": [], "dividends": []}


class TestNormalizeComponentScore(unittest.TestCase):
    """§2 的统一归一化入口。"""

    def test_scales_any_range_to_0_100(self):
        self.assertAlmostEqual(rules.normalize_component_score(8, 10), 80.0)
        self.assertAlmostEqual(rules.normalize_component_score(10, 10), 100.0)
        self.assertAlmostEqual(rules.normalize_component_score(0, 10), 0.0)
        self.assertAlmostEqual(rules.normalize_component_score(100, 100), 100.0)
        self.assertAlmostEqual(rules.normalize_component_score(43.2, 100), 43.2)

    def test_missing_stays_missing(self):
        """缺失必须原样变 None 走出去，归一化这一步不许把它变成 0 分。"""
        self.assertIsNone(rules.normalize_component_score(None, 10))
        self.assertIsNone(rules.normalize_component_score(None, None))
        self.assertIsNone(rules.normalize_component_score(8, None))
        self.assertIsNone(rules.normalize_component_score(8, 0))


class TestFinalTemplateIntegrity(unittest.TestCase):
    """§3/§4 全量扫描最终模板：权重和、归一化区间、理论最大贡献。"""

    # --- B. 所有 final template 权重和 = 1.0 ---
    def test_B_template_weights_sum_to_one(self):
        for name, tmpl in rules.RULES_V1["templates"].items():
            self.assertAlmostEqual(sum(tmpl.values()), 1.0, places=9, msg=name)
            for key, w in tmpl.items():
                self.assertGreater(w, 0, f"{name}.{key} 权重必须为正")

    def test_B_every_template_is_reachable(self):
        """每个模板都要有能选中它的属性，否则等于死代码。"""
        reachable = {rules._ATTR_TO_TEMPLATE[k] for k in _MODULE_KEYS}
        self.assertEqual(reachable, set(rules.RULES_V1["templates"]))

    # --- C. 进入模板前每个 component 的范围都是 0~100 ---
    def test_C_components_enter_the_template_as_0_to_100(self):
        for primary in _TEMPLATE_PRIMARIES:
            res = rules.final_score(_max_attrs(), {"score": 100.0},
                                    _max_metrics(), {"primary": primary})
            for c in res["components"]:
                if c["missing"]:
                    continue
                self.assertGreaterEqual(c["score"], 0.0, f"{primary}.{c['key']}")
                self.assertLessEqual(c["score"], 100.0, f"{primary}.{c['key']}")

    # --- D. 每个 component：max contribution == weight × 100 ---
    def test_D_declared_max_is_the_real_max(self):
        """raw_max 必须真的是该分量能取到的最大值，否则「理论最大贡献」是假的。"""
        comps = rules.template_components(_max_attrs(), {"score": 100.0}, _max_metrics())
        for key, (raw, raw_max, cover, _status) in comps.items():
            self.assertIsNotNone(raw, f"{key} 在全满输入下仍取不到值")
            self.assertAlmostEqual(raw, raw_max, places=6,
                                   msg=f"{key} 声明满分 {raw_max}，实测上限 {raw}")
            self.assertAlmostEqual(rules.normalize_component_score(raw_max, raw_max), 100.0)
            self.assertEqual(cover, 1.0, f"{key} 全满输入下 coverage 应为 1")

    def test_D_every_component_is_covered_by_some_template(self):
        """有分量没进任何模板 = 白算；模板引用了没登记的分量 = 必然 KeyError。"""
        comps = set(rules.template_components(_max_attrs(), {"score": 100.0}, _max_metrics()))
        used = set().union(*rules.RULES_V1["templates"].values())
        self.assertEqual(comps - used, set(), "有分量没被任何模板使用")
        self.assertEqual(used - comps, set(), "模板引用了 template_components 没提供的分量")

    def test_D_max_contribution_equals_weight_times_100(self):
        """全满输入下走完整 final_score，每个分量的实际贡献必须正好是 权重×100。

        注意这里必须读 final_score 的输出，不能自己重算一遍
        normalize_component_score(raw_max, raw_max)*w —— 那样等于用定义证明定义，
        分量真的没被归一化时反而测不出来。
        """
        for primary in _TEMPLATE_PRIMARIES:
            res = rules.final_score(_max_attrs(), {"score": 100.0},
                                    _max_metrics(), {"primary": primary})
            total = 0.0
            for c in res["components"]:
                self.assertFalse(c["missing"], f"{primary}.{c['key']} 全满输入下不该缺失")
                contrib = c["score"] * c["weight"]
                self.assertAlmostEqual(
                    contrib, c["weight"] * 100, places=9,
                    msg=f"{primary}.{c['key']} 实际最大贡献 {contrib:.2f} "
                        f"!= 权重×100 {c['weight'] * 100:.2f}"
                        f"（raw {c['raw']} / raw_max {c['raw_max']}）")
                total += contrib
            self.assertAlmostEqual(total, 100.0, places=6, msg=primary)

    def test_D_cigar_butt_liability_safety_is_no_longer_diluted(self):
        """烟蒂模板原先把 0~10 的负债安全直接 ×0.20，理论最高只贡献 2 分而非 20。"""
        res = rules.final_score(_max_attrs(), {"score": 100.0},
                                _max_metrics(), {"primary": "cigar_butt"})
        c = next(x for x in res["components"] if x["key"] == "liability_safety")
        self.assertEqual(c["weight"], 0.20)
        self.assertEqual(c["raw_max"], 10.0)
        self.assertAlmostEqual(c["score"], 100.0)
        self.assertAlmostEqual(c["score"] * c["weight"], 20.0)

    # --- E. final score 理论范围 0~100 ---
    def test_E_all_max_inputs_score_exactly_100(self):
        for primary in _TEMPLATE_PRIMARIES:
            res = rules.final_score(_max_attrs(), {"score": 100.0},
                                    _max_metrics(), {"primary": primary})
            self.assertAlmostEqual(res["score"], 100.0, places=6, msg=primary)
            self.assertAlmostEqual(res["completeness"], 1.0, places=9, msg=primary)

    def test_E_score_never_leaves_0_to_100(self):
        """三个极端输入都不许越界：全满 / 全零 / 只有部分模块有分。

        分数为 None 是合法结论（有效分量一个都没有），不在本测试的射程内；
        这里只管「给了分数就必须落在 0~100」。
        """
        cases = [
            (_max_attrs(), {"score": 100.0}, _max_metrics()),
            ({k: {"score": 0.0} for k in _MODULE_KEYS}, {"score": 0.0}, _max_metrics()),
            ({"quality": {"score": 62.0}}, None, _empty_metrics()),
            ({k: {"score": 100.0} for k in _MODULE_KEYS}, {"score": 0.0}, _empty_metrics()),
        ]
        for attrs, cyc, metrics in cases:
            for primary in _TEMPLATE_PRIMARIES:
                res = rules.final_score(attrs, cyc, metrics, {"primary": primary})
                if res["score"] is None:
                    self.assertEqual(res["completeness"], 0.0, primary)
                    continue
                self.assertGreaterEqual(res["score"], 0.0, primary)
                self.assertLessEqual(res["score"], 100.0, primary)

    # --- F. 缺失按有效权重重平衡，不能因为 normalize 重新变成 0 分 ---
    def test_F_missing_components_rebalance_by_effective_weight(self):
        # 价值型 = quality .25 / valuation .30 / balance .20 / cashflow .15 / shareholder .10
        # 只有 quality(80) 与 cashflow(100) 有效 -> (80*.25 + 100*.15) / (0.25+0.15)
        res = rules.final_score({"quality": {"score": 80.0}}, None,
                                _max_metrics(), {"primary": "quality"})
        self.assertAlmostEqual(res["score"], (80 * 0.25 + 100 * 0.15) / 0.40, places=6)
        self.assertGreater(res["score"], 35.0, "缺失分量若被记 0 分会掉到 35 分")
        self.assertAlmostEqual(res["completeness"], 0.40, places=9)

        missing = sorted(c["key"] for c in res["components"] if c["missing"])
        self.assertEqual(missing, ["balance", "shareholder", "valuation"])
        for c in res["components"]:
            if c["missing"]:
                self.assertIsNone(c["score"], f"{c['key']} 缺失不能被折成 0 分")
                self.assertIsNone(c["raw"])
            else:
                self.assertGreater(c["score"], 0.0)

    def test_F_all_missing_yields_none_not_zero(self):
        # 价值型模板不含「存活」分量，所以全部缺失时必须整体为 None
        res = rules.final_score({}, None, _empty_metrics(), {"primary": "quality"})
        self.assertIsNone(res["score"])
        self.assertEqual(res["completeness"], 0.0)
        self.assertTrue(all(c["missing"] for c in res["components"]))

    def test_F_normalization_does_not_invent_a_zero(self):
        """raw 为 None 但 raw_max 有值（和 raw 有值但 raw_max 为 None）都必须是缺失。"""
        self.assertIsNone(rules.normalize_component_score(None, rules.LIABILITY_SAFETY_MAX))
        self.assertIsNone(rules.normalize_component_score(8.0, None))


def _survival_metrics(years, profits=(), cashflows=None):
    """``years`` = m["annual"] 的报告期行数；序列只放真的取到值的年度。

    engine._series 会把取不到值的年度从序列里丢掉，所以「历史短」和
    「有缺口」只能靠 annual 的行数区分，这里如实模拟这两种形状。
    """
    return {"annual": [{"report_period": f"{2020 + i}-12-31"} for i in range(years)],
            "net_profit": [(f"{2020 + i}-12-31", v) for i, v in enumerate(profits)],
            "operating_cashflow": [(f"{2020 + i}-12-31", v)
                                   for i, v in enumerate(profits if cashflows is None else cashflows)],
            "current": {}}


class TestSurvivalComponents(unittest.TestCase):
    """_cashflow_survival / _profit_survival 的四种数据状态。

    V1.1 之前这两项在序列为空时返回 0，把「没有数据」当成「0 年为正」，
    于是缺失被折成 0 分计入分母，把总分压低。
    """

    def test_empty_series_is_missing_not_zero(self):
        for fn, window in ((rules._cashflow_survival, 3), (rules._profit_survival, 5)):
            score, status, cover = fn(_empty_metrics())
            self.assertIsNone(score, f"{fn.__name__} 空序列必须返回 None，不能是 0")
            self.assertEqual(status, "missing_data")
            self.assertEqual(cover, 0.0)

    def test_real_history_with_zero_positive_years_scores_zero(self):
        """真实结论是「一年都没赚」，那就是 0 分，要占分母——不能和取不到数混同。"""
        m = _survival_metrics(5, profits=[-1.0, -2.0, -3.0, -1.0, -2.0])
        score, status, cover = rules._profit_survival(m)
        self.assertEqual(score, 0)
        self.assertEqual(status, "ok")
        self.assertEqual(cover, 1.0)

    def test_insufficient_history_is_not_scored(self):
        """报告期本身就不够 window 年 —— 不计分，也不进分母。"""
        m = _survival_metrics(3, profits=[1.0, 2.0, 3.0])
        score, status, cover = rules._profit_survival(m)
        self.assertIsNone(score)
        self.assertEqual(status, "insufficient_history")
        self.assertEqual(cover, 0.0)
        # 同一份数据对 3 年窗口是够的，所以现金流存活项照常计分
        cf_score, cf_status, _ = rules._cashflow_survival(m)
        self.assertEqual((cf_score, cf_status), (100, "ok"))

    def test_partial_data_is_scored_with_lowered_coverage(self):
        """报告期够长但科目有缺口：按有效年数算分，coverage 按比例降。"""
        m = _survival_metrics(8, profits=[1.0, 2.0, 3.0, 4.0])   # 8 个报告期，只 4 年有数
        score, status, cover = rules._profit_survival(m)
        self.assertEqual(score, 80)          # 4 年全正
        self.assertEqual(status, "partial")
        self.assertAlmostEqual(cover, 4 / 5)
        # 有分、不算缺失，但有效权重被压低
        res = rules.final_score({"asset_value": {"score": 100.0}}, None, m,
                                {"primary": "asset_value"})
        c = next(x for x in res["components"] if x["key"] == "profit_survival")
        self.assertFalse(c["missing"])
        self.assertAlmostEqual(c["weight"], 0.10 * 4 / 5)      # 有效权重
        self.assertAlmostEqual(c["template_weight"], 0.10)     # 模板原始权重
        self.assertLess(res["completeness"], 1.0)

    def test_ok_data_keeps_full_weight(self):
        m = _survival_metrics(5, profits=[1.0, 2.0, 3.0, 4.0, 5.0])
        res = rules.final_score({"asset_value": {"score": 100.0}}, None, m,
                                {"primary": "asset_value"})
        c = next(x for x in res["components"] if x["key"] == "profit_survival")
        self.assertEqual(c["score"], 100.0)
        self.assertEqual(c["weight"], c["template_weight"])

    def test_regression_missing_survival_does_not_dilute_the_score(self):
        """回归：asset_value=80 + 现金流/利润历史全空 -> 最终必须保持 80。

        修复前两个存活项各以 0 分计入分母，分数被压到 50.91。
        """
        res = rules.final_score({"asset_value": {"score": 80.0}}, None,
                                _empty_metrics(), {"primary": "asset_value"})
        self.assertEqual(res["score"], 80.0)
        self.assertAlmostEqual(res["completeness"], 0.35)   # 只剩 asset_value 一项
        for key in ("cashflow_survival", "profit_survival"):
            c = next(x for x in res["components"] if x["key"] == key)
            self.assertTrue(c["missing"], key)
            self.assertIsNone(c["score"], key)
            self.assertEqual(c["status"], "missing_data", key)

    def test_missing_survival_leaves_the_weight_to_the_others(self):
        """缺失项不进分母：剩下两项按有效权重重平衡到 100。"""
        m = _survival_metrics(2, profits=[1.0, 2.0])   # 两项都 insufficient_history
        res = rules.final_score({"asset_value": {"score": 60.0}}, None, m,
                                {"primary": "asset_value"})
        self.assertEqual(res["score"], 60.0)
        self.assertAlmostEqual(res["completeness"], 0.35)


class TestRuleFingerprint(unittest.TestCase):
    """启动时打印的规则指纹必须与磁盘上的 rules.py 对得上。

    它是「进程里加载的规则 == 磁盘上的规则」的唯一凭据，写死或算错都会让
    旧进程的评分被当成新口径，所以这里对着文件重算一遍。

    ``loaded_*`` 和 ``disk_*`` 是**两个**概念，曾经被合并成一个 ``sha256``，
    于是旧进程报出来的哈希和新文件一模一样——恰好把最需要发现的场景掩盖掉。
    loaded/disk/dirty 的三态行为见 tests/test_runtime_fingerprint.py。
    """

    def test_fingerprint_matches_the_file_on_disk(self):
        fp = rules.rule_source_fingerprint()
        data = pathlib.Path(rules.__file__).read_bytes()
        self.assertEqual(fp["rule_version"], rules.RULE_VERSION)
        loaded = hashlib.sha256(data).hexdigest()[:12]
        self.assertEqual(fp["loaded_rule_sha256"], loaded)
        self.assertEqual(fp["loaded_rule_bytes"], len(data))
        self.assertEqual(len(fp["loaded_rule_sha256"]), 12)
        # 测试进程里没人改过 rules.py，所以两份必须相等、且不能报脏。
        self.assertEqual(fp["disk_rule_sha256"], loaded)
        self.assertFalse(fp["rule_source_dirty"])

    def test_fingerprint_has_no_ambiguous_sha256_key(self):
        """裸 ``sha256`` 键就是那个「时而指磁盘、时而指内存」的键，不许回来。"""
        fp = rules.rule_source_fingerprint()
        self.assertNotIn("sha256", fp)
        self.assertNotIn("mtime", fp)
        self.assertIn("loaded_rule_sha256", fp)
        self.assertIn("disk_rule_sha256", fp)

    def test_fingerprint_line_contains_the_version(self):
        line = rules.format_rule_fingerprint()
        self.assertIn(rules.RULE_VERSION, line)
        self.assertIn("rules.py", line)


class TestTypeRoutingIsUnified(unittest.TestCase):
    """§1 类型判定只有一个权威结果。

    2026-09-24 起这个「唯一权威」是 Router 的 primary_model：它决定模板
    （MODEL_TO_TEMPLATE），system_type 退居兜底判型。带 ``A_`` 的几条钉的是
    **兜底路径**（route=None，等价于「这只股票没有路由结果」，分数与改动前
    逐位一致），带 ``C_`` 的钉生产路径（engine._run_analysis）。
    """

    def test_A_scoring_type_equals_display_type(self):
        for code, industry in (("002714", "养殖业"), ("600741", "汽车零部件"),
                               ("601163", "橡胶制品"), ("600000", "银行")):
            m, _, _ = engine.build_metrics(code, _SYNTH_QUOTE, _synth_fin(industry))
            res = rules.analyze(m)
            self.assertEqual(res["final"]["type"], res["type"]["primary"], industry)
            self.assertEqual(res["final"]["template"],
                             rules._ATTR_TO_TEMPLATE[res["type"]["primary"]], industry)

    def test_A_the_fixture_really_did_disagree_before(self):
        """先证明这段合成数据确实能触发分叉，否则上面那条断言是空转的。"""
        m, _, _ = engine.build_metrics("002714", _SYNTH_QUOTE, _synth_fin("养殖业"))
        res = rules.analyze(m)
        self.assertEqual(res["type"]["primary"], "cyclical")
        self.assertEqual(rules.determine_type(res["attributes"])["primary"], "value",
                         "不带行业的判定应该落到 value，这才是被修掉的那个分叉")

    def test_A_cyclical_industry_over_55_forces_cyclical(self):
        attrs = {k: {"score": 40.0} for k in _MODULE_KEYS}
        attrs["cyclical"] = {"score": 55.0}
        attrs["quality"] = {"score": 90.0}   # 排名第一，但不该赢
        self.assertEqual(rules.determine_type(attrs, "养殖业")["primary"], "cyclical")
        self.assertEqual(rules.determine_type(attrs, "汽车零部件")["primary"], "quality")
        self.assertEqual(rules.determine_type(attrs, "养殖业")["secondary"], "quality")

    def test_A_cyclical_industry_under_55_falls_back_to_ranking(self):
        attrs = {k: {"score": 40.0} for k in _MODULE_KEYS}
        attrs["cyclical"] = {"score": 54.99}
        attrs["quality"] = {"score": 90.0}
        self.assertEqual(rules.determine_type(attrs, "养殖业")["primary"], "quality")

    def test_A_final_score_uses_the_type_it_is_given(self):
        """final_score 不许自己再调一次 determine_type(attrs)。"""
        attrs = {k: {"score": 40.0} for k in _MODULE_KEYS}
        attrs["cyclical"] = {"score": 55.0}
        attrs["quality"] = {"score": 90.0}
        res = rules.final_score(attrs, {"score": 50.0}, _max_metrics(),
                                rules.determine_type(attrs, "养殖业"))
        self.assertEqual(res["type"], "cyclical")
        self.assertEqual(res["template"], "周期价值型")
        self.assertEqual(res["template_source"], "attr_fallback",
                         "没有 route 时模板只能由画像判型兜底")

    # ---- C 组：生产路径（模板跟随 primary_model）----

    def test_C_template_follows_the_primary_model(self):
        """算分模板必须由 Router 的主模型决定——这是本次唯一入口。"""
        for code, industry in (("002714", "养殖业"), ("600585", "水泥"),
                               ("600741", "汽车零部件"), ("600000", "银行")):
            m, _, _ = engine.build_metrics(code, _SYNTH_QUOTE, _synth_fin(industry))
            res = engine._run_analysis(m)
            route = res["route"]
            model = route["primary_model"]
            if model in rules.MODEL_TO_TEMPLATE:
                # 有专属主模型：模板只能来自映射表，且来源标成 route
                self.assertEqual(res["final"]["template"],
                                 rules.MODEL_TO_TEMPLATE[model], industry)
                self.assertEqual(res["final"]["template_source"], "route", industry)
                self.assertEqual(res["final"]["template_model"], model, industry)
            else:
                # 未路由 / 落到通用价值：退回画像判型，并如实标注
                self.assertEqual(res["final"]["template_source"], "attr_fallback", industry)
                self.assertIsNone(res["final"]["template_model"], industry)
                self.assertEqual(res["final"]["template"],
                                 rules._ATTR_TO_TEMPLATE[res["type"]["primary"]], industry)

    def test_C_both_branches_are_actually_exercised(self):
        """上面的断言不许空转：这批合成数据必须两种来源都有。"""
        seen = set()
        for code, industry in (("002714", "养殖业"), ("600585", "水泥"),
                               ("600741", "汽车零部件"), ("600000", "银行")):
            m, _, _ = engine.build_metrics(code, _SYNTH_QUOTE, _synth_fin(industry))
            seen.add(engine._run_analysis(m)["final"]["template_source"])
        self.assertEqual(seen, {"route", "attr_fallback"},
                         "合成数据只走了一条分支，上面那条测试等于没测")

    def test_C_the_router_can_override_the_attribute_type(self):
        """水泥这只：属性判型说 value，Router 说周期核心 —— 模板必须听 Router。

        这正是「两套周期判定」的现场（改动前它会按价值型算分）。
        """
        m, _, _ = engine.build_metrics("600585", _SYNTH_QUOTE, _synth_fin("水泥"))
        res = engine._run_analysis(m)
        self.assertNotEqual(res["type"]["primary"], "cyclical")
        self.assertEqual(res["route"]["primary_model"], "CYCLICAL_CORE_V2")
        self.assertEqual(res["final"]["template"], "周期价值型")
        self.assertEqual(res["final"]["template_source"], "route")
        # 而 system_type（画像判型）仍是 value：它是描述，不再决定算分
        self.assertEqual(res["final"]["type"], res["type"]["primary"])

    def test_C_simulate_price_uses_the_same_template_as_the_library(self):
        """模拟涨跌回传的 template 必须与库里那只的一致口径（都走 _run_analysis）。

        查编译后的名字表，不看源码文本：这个函数的文档字符串里正写着旧写法
        ``rules.analyze`` 来说明「为什么不许这么写」。
        """
        names = set(engine.simulate_price.__code__.co_names)
        self.assertIn("_run_analysis", names)
        self.assertNotIn("analyze", names)
        self.assertNotIn("finalize", names)


if __name__ == "__main__":
    unittest.main()
