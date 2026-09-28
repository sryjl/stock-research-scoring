# -*- coding: utf-8 -*-
"""MODEL_ROUTER_V1 的测试。

覆盖 spec §26 的十条要求与用户点名的边界值表。这里刻意**不写死任何真实股票的
预期模型**：断言的都是「结构性质」（fit 在 0~100、缺数据不给分、行业先验不能单独
决定模型……），真实股票跑出什么就是什么。
"""
import copy
import dis
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import db as rdb
from research import engine
from research import router, rules
from tests.asset_fixtures import fake_provider


# --------------------------------------------------------------------------- #
# 合成 metrics
# --------------------------------------------------------------------------- #
def _names(fn):
    """函数体里真正被访问的名字，**不含文档字符串**。

    「不许调用 X」这类断言一律走这里，别按源码文本数：文档字符串里正需要写出
    被禁的写法来说明为什么不许这么写（``simulate_price`` 就写着旧写法），
    按文本数会在说明文字上误报。
    """
    return set(fn.__code__.co_names)


def _attr_accesses(fn, attr):
    """函数体里访问 ``.attr`` 的次数，逐条字节码数，同样不看文档字符串。"""
    return sum(1 for i in dis.get_instructions(fn)
               if i.opname == "LOAD_ATTR" and i.argval == attr)


_BAL_FIELDS = ("total_assets", "total_liabilities", "total_equity",
               "interest_bearing_debt", "monetary_funds")


def _series(vals, base_year=2019):
    return [(f"{base_year + i}-12-31", v) for i, v in enumerate(vals)]


def _rows(m):
    """把各年序列拼成 engine 口径的 annual 行（升序）。"""
    keys = ("revenue", "net_profit", "deduct_profit", "operating_cashflow",
            "free_cashflow", "capex")
    maps = {k: dict(m.get(k) or []) for k in keys}
    periods = sorted({p for k in keys for p in maps[k]})
    out = []
    for p in periods:
        row = {"report_period": p}
        row.update({k: maps[k].get(p) for k in keys})
        _fill_balance(row, m.get("_balance") or {})
        out.append(row)
    return out


def _fill_balance(row, bal):
    for f in _BAL_FIELDS:
        v = bal.get(f)
        row[f] = v


def _m(**kw):
    """合成一份 build_metrics 形状的 metrics。"""
    m = {
        "revenue": _series([1000, 1100, 1200, 1300, 1400]),
        "net_profit": _series([100, 110, 120, 130, 140]),
        "deduct_profit": _series([95, 105, 115, 125, 135]),
        "roe": _series([10, 10.5, 11, 11.5, 12]),
        "gross_margin": _series([30, 30.5, 31, 30.8, 31.2]),
        "operating_cashflow": _series([110, 120, 130, 140, 150]),
        "free_cashflow": _series([60, 65, 70, 75, 80]),
        "capex": _series([40, 45, 50, 55, 60]),
        "roic": [("2023-12-31", 0.11)],
        "current": {"price": 20.0, "total_market_cap": 20e9, "pe_ttm": 12.0, "pb": 1.5,
                    "dividend_yield": 0.02, "net_cash_ratio": 0.0,
                    "liquidation_ratio": 0.5, "asset_value_ratio": 0.6},
        "balance": {},
        "dividends": [],
        "industry": "",
        "_balance": {"total_equity": 1000, "interest_bearing_debt": 200,
                     "total_assets": 2000, "total_liabilities": 1000,
                     "monetary_funds": 100},
    }
    m.update(kw)
    if "annual" not in kw:
        m["annual"] = _rows(m)
    if "assets" not in kw:
        _sync_assets(m)
    return m


def _attrs(**kw):
    """8 个画像分。默认全 0（像「有结论但很弱」），可按需覆盖。"""
    base = {k: {"score": 0.0} for k in
            ("growth", "quality", "value", "dividend",
             "cigar_butt", "asset_value", "cyclical", "turnaround")}
    for k, v in kw.items():
        base[k] = v if isinstance(v, dict) else {"score": float(v)}
    return base


def _attrs_missing():
    """8 个画像全算不出来（新股票、财务数据完全缺失）。"""
    return _attrs(**{k: {"score": None} for k in
                     ("growth", "quality", "value", "dividend",
                      "cigar_butt", "asset_value", "cyclical", "turnaround")})


def _m_empty(**kw):
    """连财务序列都没有的 metrics。"""
    return _m(revenue=[], net_profit=[], deduct_profit=[], roe=[], gross_margin=[],
              operating_cashflow=[], free_cashflow=[], capex=[], roic=[], annual=[],
              current={}, **kw)


def _score(attrs):
    return {k: v["score"] for k, v in attrs.items()}


def _sync_assets(m):
    """按 current 里的资产比率重建 m["assets"]。

    这几只合成股票用 ``current`` 描述「资产端长什么样」，读起来最直观，所以先写
    current 再同步过来。**但只改 current 是不生效的**——Router 和画像的资产类
    分量只认 ``m["assets"]``，这正是本轮迁移要达到的效果。
    """
    c = m["current"]
    m["assets"] = fake_provider(
        market_cap=c.get("total_market_cap") or 20e9,
        net_cash_ratio=c.get("net_cash_ratio"),
        near_cash_ratio=c.get("near_cash_ratio", 0.30),
        interest_debt_cover=c.get("interest_debt_cover", 2.0),
        liquidation_ratio=c.get("liquidation_ratio"),
        asset_value_ratio=c.get("asset_value_ratio"),
        liquid_asset_ratio=c.get("liquid_asset_ratio"),
    )
    return m


# --------------------------------------------------------------------------- #
# 三只合成股票
# --------------------------------------------------------------------------- #
def _stock_cyclical():
    """强周期：行业=养殖，利润巨幅波动、盈亏切换、重资本开支。"""
    m = _m(
        revenue=_series([1000, 1500, 1100, 1800, 900]),
        net_profit=_series([500, 1200, -300, 1800, -600]),
        deduct_profit=_series([480, 1150, -320, 1750, -620]),
        gross_margin=_series([20, 45, 10, 50, 8]),
        operating_cashflow=_series([400, 900, -100, 1400, -300]),
        free_cashflow=_series([150, 600, -400, 1000, -600]),
        capex=_series([180, 320, 150, 400, 90]),
        industry="养殖业",
    )
    m["current"].update({"pb": 1.8, "net_cash_ratio": -0.15, "liquidation_ratio": 0.4,
                         "asset_value_ratio": 0.5})
    _sync_assets(m)
    return m, _attrs(cyclical=90, growth=55, value=30, cigar_butt=25,
                     asset_value=30, quality=35, dividend=15, turnaround=60)


def _stock_cigar():
    """烟蒂：行业=服装（弱周期），利润平稳，高净现金、低 PB、清算价值高。"""
    m = _m(
        revenue=_series([1000, 1010, 1005, 1020, 1015]),
        net_profit=_series([300, 310, 290, 320, 305]),
        deduct_profit=_series([295, 305, 285, 315, 300]),
        gross_margin=_series([30, 31, 29, 30, 31]),
        capex=_series([30, 32, 28, 31, 29]),
        industry="服装家纺",
    )
    m["current"].update({"pb": 0.55, "net_cash_ratio": 0.50, "liquidation_ratio": 1.20,
                         "asset_value_ratio": 1.35, "dividend_yield": 0.045})
    _sync_assets(m)
    return m, _attrs(cigar_butt=80, asset_value=85, value=70, cyclical=20,
                     quality=55, dividend=70, growth=10, turnaround=20)


def _stock_hybrid():
    """既像周期又像烟蒂：钢铁股，折价明显且利润剧烈波动。"""
    m, attrs = _stock_cyclical()
    m["industry"] = "钢铁"
    m["current"].update({"pb": 0.70, "net_cash_ratio": 0.10, "liquidation_ratio": 1.10,
                         "asset_value_ratio": 1.25})
    _sync_assets(m)
    attrs = _attrs(cyclical=65, cigar_butt=85, asset_value=90, value=80,
                   quality=45, growth=20, dividend=35, turnaround=55)
    return m, attrs


def _stock_boring():
    """平庸：弱周期行业、什么都一般，任何专属模型都够不到门槛。"""
    m = _m(industry="食品饮料")
    m["current"].update({"pb": 2.2, "net_cash_ratio": -0.05, "liquidation_ratio": 0.30,
                         "asset_value_ratio": 0.45})
    _sync_assets(m)
    return m, _attrs(cyclical=25, quality=35, value=30, growth=30,
                     dividend=25, cigar_butt=20, asset_value=25, turnaround=20)


def _stock_ambiguous():
    """中间地带：强周期行业，但周期证据只到「有点意思」，够不到 65。"""
    m = _m(
        net_profit=_series([300, 400, -100, 350, 420]),
        deduct_profit=_series([290, 390, -110, 340, 410]),
        gross_margin=_series([30, 33, 28, 32, 30]),
        industry="化工",
    )
    m["current"].update({"pb": 1.5, "net_cash_ratio": 0.0, "liquidation_ratio": 0.5,
                         "asset_value_ratio": 0.6})
    _sync_assets(m)
    return m, _attrs(cyclical=55, cigar_butt=30, asset_value=35, value=25,
                     quality=40, growth=35, dividend=25, turnaround=30)


class RouterTestCase(unittest.TestCase):
    def route(self, stock):
        m, attrs = stock
        return router.route(m, attrs)


# --------------------------------------------------------------------------- #
# §26-3 fit 范围 0~100
# --------------------------------------------------------------------------- #
class TestIntegrationStatuses(RouterTestCase):
    """五只合成股票端到端锁死状态语义。

    2026-09-23 六个模型全开后有两只的落点变了，都是真输出、不是回归：
    ``_stock_cigar`` 由 CLEAR 变 HYBRID——利润平稳、毛利不抖、CFO 常年高于利润，
    质量复利适配度 71.6 与价值烟蒂 79.6 咬在 12 以内，两套模型确实都说得通；
    ``_stock_boring`` 由 FALLBACK 变 AMBIGUOUS——质量复利 56.6 越过 55 的
    「勉强像」下界但够不到 65 的可用门槛。两者都落回 ``GENERAL_VALUE_V2``，
    差别只在解释那一句。FALLBACK 的端到端覆盖仍在
    ``TestInsufficientData.test_zero_profiles_with_data_is_fallback_not_insufficient``。
    """

    def test_statuses(self):
        cases = (
            (_stock_cyclical(), "CLEAR", router.MODEL_CYCLICAL),
            (_stock_cigar(), "HYBRID", router.MODEL_VALUE_CIGAR),
            (_stock_hybrid(), "HYBRID", router.MODEL_CYCLICAL),
            (_stock_ambiguous(), "AMBIGUOUS", router.FALLBACK_MODEL),
            (_stock_boring(), "AMBIGUOUS", router.FALLBACK_MODEL),
        )
        for stock, status, primary in cases:
            r = self.route(stock)
            self.assertEqual(r["route_status"], status)
            self.assertEqual(r["primary_model"], primary)

    def test_hybrid_names_both_models(self):
        r = self.route(_stock_hybrid())
        self.assertIsNotNone(r["secondary_model"])
        self.assertIsNotNone(r["secondary_fit"])
        self.assertLess(r["primary_fit"] - r["secondary_fit"], router.HYBRID_GAP)

    def test_clear_has_no_secondary(self):
        r = self.route(_stock_cyclical())
        self.assertIsNone(r["secondary_model"])
        self.assertIsNone(r["secondary_fit"])

    def test_ambiguous_reports_no_fit_for_fallback(self):
        # 兜底模型没有「适配度」可言，最高候选分留在 candidates 里
        r = self.route(_stock_ambiguous())
        self.assertIsNone(r["primary_fit"])
        self.assertEqual(r["candidates"][0]["model"], router.MODEL_CYCLICAL)

    def test_tie_breaker_prefers_cyclical_in_strong_cyclical_industry(self):
        # §18：周期与烟蒂咬合时，强周期行业 + 周期适配度 >= 75 -> 周期核心
        r = self.route(_stock_hybrid())
        self.assertGreaterEqual(r["fit_scores"][router.MODEL_CYCLICAL], 75.0)
        self.assertEqual(r["primary_model"], router.MODEL_CYCLICAL)
        self.assertTrue(any("周期" in x and "烟蒂" in x for x in r["reasons"]),
                        r["reasons"])


class TestFitRange(RouterTestCase):
    def test_all_fits_in_range(self):
        for stock in (_stock_cyclical(), _stock_cigar(), _stock_hybrid(),
                      _stock_boring(), _stock_ambiguous()):
            r = self.route(stock)
            for model, fit in r["fit_scores"].items():
                if fit is None:
                    continue
                self.assertGreaterEqual(fit, 0.0, model)
                self.assertLessEqual(fit, 100.0, model)

    def test_profile_scores_in_range(self):
        for stock in (_stock_cyclical(), _stock_cigar()):
            r = self.route(stock)
            for name, v in r["profile_scores"].items():
                self.assertTrue(name.endswith("_profile"))
                if v is not None:
                    self.assertGreaterEqual(v, 0.0)
                    self.assertLessEqual(v, 100.0)

    def test_weights_sum_to_100(self):
        for model, spec in router.MODEL_SPECS.items():
            self.assertAlmostEqual(sum(c[2] for c in spec["components"]), 100.0, places=6, msg=model)


# --------------------------------------------------------------------------- #
# §26-4 profile score 不等于 final score
# --------------------------------------------------------------------------- #
class TestProfileIsNotFinalScore(RouterTestCase):
    def test_profile_scores_are_the_module_scores_unchanged(self):
        # Router 不重算画像，只是换名字转发；画像分与最终投资分是两套东西。
        m, attrs = _stock_cyclical()
        r = router.route(m, attrs)
        self.assertEqual(r["profile_scores"]["cyclical_profile"], 90.0)
        self.assertEqual(r["profile_scores"]["cigar_profile"], 25.0)

    def test_route_output_has_no_final_score(self):
        r = self.route(_stock_cyclical())
        for banned in ("total_score", "final_score", "score", "final"):
            self.assertNotIn(banned, r)

    def test_primary_fit_is_not_the_investment_score(self):
        # 适配度回答的是「该不该用这套模型看它」，不是「它值多少分」。
        m, attrs = _stock_cyclical()
        attrs["cyclical"]["score"] = 40.0   # 画像分很低
        r = router.route(m, attrs)
        # 画像分掉下来会把 fit 也压低，但 fit 从来不是那 40 分本身
        self.assertNotEqual(r["primary_fit"], 40.0)


# --------------------------------------------------------------------------- #
# §26-5 缺失数据不会默认给 fit 分
# --------------------------------------------------------------------------- #
class TestMissingDataNotScored(RouterTestCase):
    def test_missing_component_is_none_not_zero(self):
        m, attrs = _stock_cyclical()
        for row in m["annual"]:
            row["capex"] = None
        r = router.route(m, attrs)
        ev = next(e for e in r["evidence"] if e["model"] == router.MODEL_CYCLICAL)
        capex = next(c for c in ev["components"] if c["key"] == "capex_cycle")
        self.assertIsNone(capex["score"])
        self.assertFalse(capex["available"])
        # 缺失只压低覆盖率，不当成 0 分拉低 fit
        self.assertLess(ev["coverage"], 1.0)

    def test_no_component_is_silently_broken(self):
        """数据齐全时，任何一个分量都不该以「计算异常」缺席。

        缺失数据的兜底逻辑很宽容：分量抛异常只会被记成 available=False，
        覆盖率悄悄掉一点，fit 照算，界面上看不出问题。毛利率波动就这样带着
        `_tail()` 参数漏传活了一版。所以这里反过来卡住：造一份每个分量都拿得到
        的样本，覆盖率必须是 100%。
        """
        m, attrs = _stock_cyclical()
        r = router.route(m, attrs)
        for model in (router.MODEL_CYCLICAL, router.MODEL_VALUE_CIGAR):
            ev = next(e for e in r["evidence"] if e["model"] == model)
            missing = [(c["key"], c["note"]) for c in ev["components"] if not c["available"]]
            self.assertEqual(missing, [], f"{model} 有分量缺席：{missing}")
            self.assertEqual(ev["coverage"], 1.0, f"{model} 覆盖率不是 100%")

    def test_rebalance_never_exceeds_available(self):
        # 缺分量时 fit 是可用分量的加权平均，不会因为分母变小而虚高到 100 以外
        m, attrs = _stock_cyclical()
        m["industry"] = ""
        r = router.route(m, attrs)
        ev = next(e for e in r["evidence"] if e["model"] == router.MODEL_CYCLICAL)
        self.assertIsNone(ev["components"][1]["score"])   # 行业先验缺失
        if ev["fit"] is not None:
            self.assertLessEqual(ev["fit"], 100.0)


# --------------------------------------------------------------------------- #
# §26-6 coverage 不足时不能强行路由
# --------------------------------------------------------------------------- #
class TestInsufficientData(RouterTestCase):
    def test_empty_everything_is_insufficient(self):
        r = router.route(_m_empty(), _attrs_missing())
        self.assertEqual(r["route_status"], "INSUFFICIENT_DATA")
        self.assertEqual(r["primary_model"], router.FALLBACK_MODEL)
        self.assertIsNone(r["primary_fit"])
        self.assertLess(r["coverage"], router.MIN_ROUTER_COVERAGE)

    def test_zero_profiles_with_data_is_fallback_not_insufficient(self):
        # 画像分是 0（有结论、只是都很弱）与画像分拿不到（没数据）不是一回事：
        # 前者覆盖率够，应该老实落到 FALLBACK，而不是谎报数据不足。
        r = router.route(_m_empty(), _attrs())
        self.assertEqual(r["route_status"], "FALLBACK")
        self.assertGreaterEqual(r["coverage"], router.MIN_ROUTER_COVERAGE)

    def test_below_coverage_threshold_never_becomes_primary(self):
        m, attrs = _stock_cyclical()
        # 只留行业先验 + 周期画像，其余全砍：覆盖率 55% < 60%
        m["net_profit"] = []
        m["gross_margin"] = []
        m["annual"] = []
        r = router.route(m, attrs)
        for ev in r["evidence"]:
            if ev["coverage"] < router.MIN_ROUTER_COVERAGE:
                self.assertNotEqual(r["primary_model"], ev["model"])


# --------------------------------------------------------------------------- #
# §26-7 / 用户点名的边界值表
# --------------------------------------------------------------------------- #
class TestStatusBoundaries(unittest.TestCase):
    def test_user_specified_table(self):
        d = router._decide_status
        self.assertEqual(d(64.9, 59), "AMBIGUOUS")   # 差 0.1 就是不够门槛
        self.assertEqual(d(65, 59), "CLEAR")         # 次席 < 60，没形成竞争
        self.assertEqual(d(65, 60), "HYBRID")        # 刚好咬住
        self.assertEqual(d(69, 59), "CLEAR")
        self.assertEqual(d(69, 60), "HYBRID")
        self.assertEqual(d(70, 59), "CLEAR")         # 70 不再是 CLEAR 的绝对门槛
        self.assertEqual(d(70, 65), "HYBRID")

    def test_gap_boundary(self):
        d = router._decide_status
        # gap 11.9 -> 咬合；gap 12.0 -> 明确
        self.assertEqual(d(76.9, 65.0), "HYBRID")
        self.assertEqual(d(77.0, 65.0), "CLEAR")

    def test_ambiguous_and_fallback(self):
        d = router._decide_status
        self.assertEqual(d(55.0, 10), "AMBIGUOUS")   # 含下界
        self.assertEqual(d(54.9, 10), "FALLBACK")
        self.assertEqual(d(0.0, None), "FALLBACK")

    def test_single_candidate_is_clear(self):
        self.assertEqual(router._decide_status(65.0, None), "CLEAR")
        self.assertEqual(router._decide_status(100.0, None), "CLEAR")

    def test_statuses_are_exhaustive_and_mutually_exclusive(self):
        seen = set()
        for top1 in (0, 54.9, 55, 64.9, 65, 69, 70, 100):
            for top2 in (None, 0, 55, 59.9, 60, 65, 88, 100):
                s = router._decide_status(top1, top2)
                self.assertIn(s, router.ROUTE_STATUSES)
                seen.add(s)
        self.assertEqual(seen, {"CLEAR", "HYBRID", "AMBIGUOUS", "FALLBACK"})


# --------------------------------------------------------------------------- #
# §26-8 行业 prior 不能单独决定模型
# --------------------------------------------------------------------------- #
class TestIndustryPriorCannotDecideAlone(unittest.TestCase):
    def test_prior_weight_below_entry_threshold(self):
        spec = router.MODEL_SPECS[router.MODEL_CYCLICAL]
        w = next(c[2] for c in spec["components"] if c[0] == "industry_prior")
        self.assertLess(w, router.FIT_ENTRY_THRESHOLD)

    def test_industry_only_stock_is_not_routed_to_cyclical(self):
        # 行业=养殖，但没有任何经营数据、画像分也拿不到 -> 连路由资格都没有
        r = router.route(_m_empty(industry="养殖业"), _attrs_missing())
        self.assertNotEqual(r["primary_model"], router.MODEL_CYCLICAL)
        self.assertEqual(r["route_status"], "INSUFFICIENT_DATA")

    def test_strong_industry_with_boring_numbers_is_not_cyclical(self):
        # 行业先验满分，但利润平稳、无盈亏切换、不重资本开支 -> 不像周期股
        m = _m(industry="养殖业")
        r = router.route(m, _attrs(cyclical=20))
        self.assertLess(r["fit_scores"][router.MODEL_CYCLICAL], router.FIT_ENTRY_THRESHOLD)

    def test_every_rule_cyclical_keyword_is_strong_tier(self):
        for kw in rules.RULES_V1["cyclical_industries"]:
            prior, tier = router._industry_prior(kw)
            self.assertEqual(tier, "强周期", f"{kw} 没有落在强周期档")
            self.assertEqual(prior, 100.0)

    def test_unknown_industry_has_no_prior(self):
        self.assertEqual(router._industry_prior(""), (None, None))
        self.assertEqual(router._industry_prior("某个不存在的行业"), (0.0, "无周期先验"))


# --------------------------------------------------------------------------- #
# §26-9 相同 profile、不同 route
# --------------------------------------------------------------------------- #
class TestSameProfileDifferentRoute(RouterTestCase):
    def test_identical_profiles_different_evidence_different_route(self):
        cycl_m, attrs = _stock_cyclical()
        bor_m, _ = _stock_boring()
        # 强行让两只股票的画像完全一致
        r_cyc = router.route(cycl_m, attrs)
        r_bor = router.route(bor_m, attrs)
        self.assertEqual(_score(attrs), _score(attrs))
        self.assertEqual(r_cyc["profile_scores"], r_bor["profile_scores"])
        self.assertNotEqual(r_cyc["fit_scores"][router.MODEL_CYCLICAL],
                            r_bor["fit_scores"][router.MODEL_CYCLICAL])
        self.assertNotEqual(r_cyc["primary_model"], r_bor["primary_model"])


# --------------------------------------------------------------------------- #
# §26-1 / §26-2 唯一权威：一次分析一个 route
# --------------------------------------------------------------------------- #
class TestSingleCanonicalRoute(RouterTestCase):
    def test_route_is_deterministic(self):
        m, attrs = _stock_cyclical()
        a = router.route(copy.deepcopy(m), copy.deepcopy(attrs))
        b = router.route(copy.deepcopy(m), copy.deepcopy(attrs))
        self.assertEqual(json.dumps(a, sort_keys=True, ensure_ascii=False),
                         json.dumps(b, sort_keys=True, ensure_ascii=False))

    def test_route_does_not_mutate_inputs(self):
        m, attrs = _stock_cyclical()
        m0, a0 = copy.deepcopy(m), copy.deepcopy(attrs)
        router.route(m, attrs)
        self.assertEqual(m, m0)
        self.assertEqual(attrs, a0)

    def test_engine_computes_route_exactly_once(self):
        # 界面、主记录、路由快照必须共用同一个 route 对象：算分编排里只允许出现
        # 一次 router.route 调用，别处不许再算。2026-09-24 起编排移进
        # engine._run_analysis（模板要跟随 primary_model，Router 必须插在
        # score_modules 与 finalize 之间），不变量本身不变、反而更硬：
        # 现在连 simulate_price 都不可能另算一套。
        self.assertEqual(_attr_accesses(engine._run_analysis, "route"), 1)
        self.assertEqual(_attr_accesses(engine.analyze, "route"), 0)

    def test_both_scoring_entries_go_through_the_same_orchestrator(self):
        # analyze 与 simulate_price 都必须走 _run_analysis：任何一处自己拼
        # 「先算分再选模板」，模拟出来的模板就会和库里那只不是同一个。
        for fn in (engine.analyze, engine.simulate_price):
            self.assertIn("_run_analysis", _names(fn), fn.__name__)
            self.assertNotIn("analyze", _names(fn), fn.__name__)
            self.assertNotIn("finalize", _names(fn), fn.__name__)

    def test_every_model_fit_has_a_detail_entry(self):
        r = self.route(_stock_cyclical())
        self.assertEqual(set(r["fit_scores"]), set(router.MODEL_REGISTRY))
        self.assertEqual({e["model"] for e in r["evidence"]}, set(router.MODEL_REGISTRY))

    def test_disabled_model_shows_in_candidates_and_the_reason(self):
        # 2026-09-23 起 6 个模型全开，所以这条只能临时关掉一个来验。机制仍然
        # 要留着：关掉模型时若不把它列进候选、不在理由里交代一句，界面上就会
        # 出现「主模型 质量复利 72，可适配度 80 的价值烟蒂整个消失」——看起来
        # 像 Router 判断错了。
        m, attrs = _stock_cigar()
        fits = router.route(m, attrs)["fit_scores"]
        top = max(fits, key=lambda k: -1.0 if fits[k] is None else fits[k])
        self.assertEqual(top, router.MODEL_VALUE_CIGAR, "这只合成股票的头名应当是它")
        patch = {"enabled": False, "implementation_status": "PLACEHOLDER"}
        with mock.patch.dict(router.MODEL_REGISTRY[top], patch):
            r = router.route(m, attrs)
            cand = next(c for c in r["candidates"] if c["model"] == top)
            self.assertFalse(cand["enabled"])
            # 注册表里的 implementation_status 要原样透传到候选，不能吞掉
            self.assertEqual(cand["implementation_status"], "PLACEHOLDER")
            self.assertNotEqual(r["primary_model"], top)
            self.assertTrue(router.is_enabled(r["primary_model"]))
            self.assertTrue(
                any("未启用" in x and router.MODEL_REGISTRY[top]["label"] in x
                    for x in r["reasons"]),
                r["reasons"])

    def test_disabled_model_is_never_primary(self):
        # 6 个模型全开后「主模型总是启用状态」成了废话，所以改成逐个临时关掉，
        # 直接验 enabled 这道闸门本身：关掉谁，谁就不许当主模型。
        for stock in (_stock_cyclical(), _stock_cigar(), _stock_hybrid(),
                      _stock_boring()):
            for mid in router.MODEL_REGISTRY:
                with mock.patch.dict(router.MODEL_REGISTRY[mid], {"enabled": False}):
                    r = self.route(stock)
                    self.assertNotEqual(r["primary_model"], mid,
                                        f"{mid} 未启用却被选为主模型")
                    self.assertTrue(router.is_enabled(r["primary_model"]))


# --------------------------------------------------------------------------- #
# 2026-09-23 六个模型全部启用
# --------------------------------------------------------------------------- #
class TestAllModelsEnabled(RouterTestCase):
    """锁住「全部启用」这个决定，以及它让哪些规则第一次变成活的。"""

    def test_registry_has_no_disabled_model(self):
        # 要停用某个模型，先想清楚：这条测试、注册表上方的注释、以及
        # /api/meta 里 router 指纹的 enabled_models 是同一个决定的三处体现。
        disabled = [m for m in router.MODEL_REGISTRY
                    if not router.MODEL_REGISTRY[m]["enabled"]]
        self.assertEqual(disabled, [], f"仍有停用模型：{disabled}")
        placeholders = [m for m in router.MODEL_REGISTRY
                        if router.MODEL_REGISTRY[m]["implementation_status"] != "ACTIVE"]
        self.assertEqual(placeholders, [],
                         "已参与路由的模型不该再标成未实现")

    def test_router_fingerprint_reports_all_six(self):
        fp = router.router_source_fingerprint()
        self.assertEqual(set(fp["enabled_models"]), set(router.MODEL_REGISTRY))
        self.assertEqual(fp["fallback_model"], router.FALLBACK_MODEL)

    def test_no_reason_claims_a_model_is_disabled(self):
        # 「未启用」这句话只在真的有关掉模型时才能出现——全开后它还冒出来，
        # 就说明 disabled_better 算错了。
        for stock in (_stock_cyclical(), _stock_cigar(), _stock_hybrid(),
                      _stock_ambiguous(), _stock_boring()):
            r = self.route(stock)
            self.assertFalse([x for x in r["reasons"] if "未启用" in x], r["reasons"])

    def test_anchor_missing_blocks_even_an_enabled_model(self):
        # 启用不等于能赢：require_all 的锚定画像拿不到时 fit 直接是 None，
        # 连候选列表都进不去。这是「全开」之后唯一还挡得住模型的闸门。
        m, attrs = _stock_cigar()
        attrs["dividend"] = {"score": None}       # 股息价值 V2 的锚定画像
        r = router.route(m, attrs)
        ev = next(e for e in r["evidence"] if e["model"] == router.MODEL_DIVIDEND)
        self.assertIsNone(ev["fit"])
        self.assertIn("锚定画像", ev["blocked"])
        self.assertNotIn(router.MODEL_DIVIDEND, {c["model"] for c in r["candidates"]})
        self.assertNotEqual(r["primary_model"], router.MODEL_DIVIDEND)

    def test_dividend_cigar_tie_breaker_only_now_becomes_reachable(self):
        # _apply_tie_breakers 里的「股息价值 × 价值烟蒂」这一对，在股息价值被
        # 停用的那段时间里谁也走不到。全开后它第一次是活的，所以要单独钉住：
        # 咬合（差距 < TIE_GAP）且价值烟蒂适配度 >= 75 -> 价值烟蒂优先。
        m, attrs = _stock_cigar()
        ctx = router._Ctx(m, attrs)
        ranked = [{"model": router.MODEL_DIVIDEND, "fit": 88.0, "coverage": 1.0},
                  {"model": router.MODEL_VALUE_CIGAR, "fit": 82.0, "coverage": 1.0}]
        self.assertLess(88.0 - 82.0, router.TIE_GAP)
        out, notes = router._apply_tie_breakers(ranked, ctx)
        self.assertEqual(out[0]["model"], router.MODEL_VALUE_CIGAR)
        self.assertTrue(any("咬合" in n for n in notes), notes)

    def test_tie_breaker_stays_out_of_it_when_the_gap_is_wide(self):
        m, attrs = _stock_cigar()
        ctx = router._Ctx(m, attrs)
        ranked = [{"model": router.MODEL_DIVIDEND, "fit": 95.0, "coverage": 1.0},
                  {"model": router.MODEL_VALUE_CIGAR, "fit": 82.0, "coverage": 1.0}]
        out, notes = router._apply_tie_breakers(ranked, ctx)
        self.assertEqual(out[0]["model"], router.MODEL_DIVIDEND)
        self.assertEqual(notes, [])


# --------------------------------------------------------------------------- #
# §26-10 Router 不修改 SCORING_V1.1
# --------------------------------------------------------------------------- #
class TestRouterLeavesScoringAlone(RouterTestCase):
    def test_router_only_imports_pure_helpers_from_rules(self):
        import inspect
        src = inspect.getsource(router)
        line = next(l for l in src.splitlines() if l.startswith("from .rules import"))
        names = {n.strip() for n in line.split("import", 1)[1].split(",")}
        self.assertEqual(names, {"cagr", "cv", "mean", "piecewise"})

    def test_router_never_reads_a_final_score(self):
        # 查编译后的名字表，而不是源码文本：文档字符串里提到 final_score 是在
        # 说明「不碰它」，代码里则连这个名字都不该出现。
        import types
        names = set()

        def walk(obj):
            if isinstance(obj, types.FunctionType):
                names.update(obj.__code__.co_names)
                names.update(obj.__code__.co_freevars)
            elif isinstance(obj, type):
                for v in vars(obj).values():
                    walk(v)

        for obj in vars(router).values():
            walk(obj)
        for banned in ("total_score", "final_score", "score", "final"):
            self.assertNotIn(banned, names)

    def test_rule_version_untouched(self):
        before = rules.RULE_VERSION
        self.route(_stock_cyclical())
        self.assertEqual(rules.RULE_VERSION, before)


# --------------------------------------------------------------------------- #
# 2026-09-24 模型 -> 模板的桥：primary_model 是评分框架的唯一入口
# --------------------------------------------------------------------------- #
class TestModelToTemplateBridge(unittest.TestCase):
    """``rules.MODEL_TO_TEMPLATE`` 是 Router 与评分层之间**唯一**的桥。

    键集靠这里钉住：注册表加了模型却忘了配模板，路由到它就会静悄悄退回画像判型
    （分数照出，但用户看到的模板与主模型对不上）——那正是这个改动要根除的病。
    """

    def test_every_enabled_model_has_a_template(self):
        self.assertEqual(set(rules.MODEL_TO_TEMPLATE), set(router.MODEL_REGISTRY),
                         "桥的键集必须与模型注册表完全一致")

    def test_every_template_is_a_real_template(self):
        for model, tmpl in rules.MODEL_TO_TEMPLATE.items():
            self.assertIn(tmpl, rules.RULES_V1["templates"], model)

    def test_the_six_models_land_on_the_existing_five_templates(self):
        # 不新增模板：6 个模型铺满 5 套模板，没有哪套模板是没人走的死路。
        self.assertEqual(len(router.MODEL_REGISTRY), 6)
        self.assertEqual(set(rules.MODEL_TO_TEMPLATE.values()),
                         set(rules.RULES_V1["templates"]))

    def test_the_fallback_model_is_deliberately_not_in_the_bridge(self):
        # 通用价值表示「没有专属模型适配」，它要是出现在桥里，未路由就会被算成
        # 一次正式路由，兜底那条路再也走不到。
        self.assertNotIn(router.FALLBACK_MODEL, rules.MODEL_TO_TEMPLATE)

    def test_a_routed_model_wins_over_the_attribute_type(self):
        # 水泥：属性判型是 value，Router 却判 CYCLICAL_CORE —— 模板必须跟 Router。
        tmpl, source, decider = rules.template_for(
            {"primary_model": router.MODEL_CYCLICAL}, {"primary": "value"})
        self.assertEqual((tmpl, source, decider),
                         ("周期价值型", rules.TEMPLATE_SOURCE_ROUTE, router.MODEL_CYCLICAL))

    def test_no_route_falls_back_and_says_so(self):
        tmpl, source, decider = rules.template_for(None, {"primary": "dividend"})
        self.assertEqual((tmpl, source, decider),
                         ("高股息型", rules.TEMPLATE_SOURCE_ATTR, None))

    def test_the_fallback_model_falls_back_even_when_it_is_named(self):
        # 未路由时 route 里确实带着 primary_model == GENERAL_VALUE_V2，
        # 桥要认得出这是「没有专属模型」，而不是当成一个模型去查表。
        route = {"primary_model": router.FALLBACK_MODEL, "route_status": "AMBIGUOUS"}
        tmpl, source, _ = rules.template_for(route, {"primary": "cigar_butt"})
        self.assertEqual((tmpl, source),
                         ("烟蒂/资产价值型", rules.TEMPLATE_SOURCE_ATTR))

    def test_an_unknown_model_falls_back_rather_than_crashing(self):
        tmpl, source, _ = rules.template_for({"primary_model": "NOT_A_MODEL"},
                                             {"primary": "growth"})
        self.assertEqual((tmpl, source), ("成长型", rules.TEMPLATE_SOURCE_ATTR))

    def test_no_attribute_type_at_all_lands_on_the_generic_template(self):
        tmpl, source, _ = rules.template_for(None, None)
        self.assertEqual((tmpl, source), ("价值型", rules.TEMPLATE_SOURCE_ATTR))


# --------------------------------------------------------------------------- #
# §21 路由快照与主记录共用同一个 route
# --------------------------------------------------------------------------- #
class TestPersistedRoute(RouterTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = rdb.DEFAULT_PATH
        rdb.DEFAULT_PATH = os.path.join(self.tmp, "research.db")
        self.conn = rdb.init_db()

    def tearDown(self):
        self.conn.close()
        rdb.DEFAULT_PATH = self._orig

    def _fake_result(self, m, attrs):
        route = router.route(m, attrs)
        return {
            "type": {"primary": "cyclical", "confidence": 0.8},
            "final": {"score": 71.5, "template": "周期价值型",
                      "components": [], "completeness": 0.9},
            "risk": {"level": "中", "flags": []},
            "attributes": attrs,
            "route": route,
        }

    def test_upsert_and_snapshot_share_the_same_route(self):
        m, attrs = _stock_cyclical()
        result = self._fake_result(m, attrs)
        code = "002714"
        engine._persist(self.conn, code, {"name": "牧原股份", "price": 40.0},
                        m, result, "2023-12-31", "养殖业", False)

        row = rdb.get_stock(self.conn, code)
        route = result["route"]
        self.assertEqual(row["primary_model"], route["primary_model"])
        self.assertEqual(row["route_status"], route["route_status"])
        self.assertEqual(row["primary_fit"], route["primary_fit"])
        self.assertEqual(row["router_version"], router.ROUTER_VERSION)
        self.assertEqual(json.loads(row["route_json"])["primary_model"], route["primary_model"])
        # system_type 继续是老的画像类型，没有被 Router 改写
        self.assertEqual(row["system_type"], "cyclical")

        snaps = rdb.list_route_snapshots(self.conn, code)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["primary_model"], route["primary_model"])
        self.assertEqual(json.loads(snaps[0]["fit_scores"]), route["fit_scores"])

    def test_unchanged_route_does_not_add_snapshot(self):
        m, attrs = _stock_cyclical()
        result = self._fake_result(m, attrs)
        code = "002714"
        engine._persist(self.conn, code, {"name": "牧原股份"}, m, result,
                        "2023-12-31", "养殖业", False)
        engine._persist(self.conn, code, {"name": "牧原股份"}, m, result,
                        "2023-12-31", "养殖业", False)
        self.assertEqual(len(rdb.list_route_snapshots(self.conn, code)), 1)

    def test_route_change_adds_snapshot(self):
        m, attrs = _stock_cyclical()
        code = "002714"
        engine._persist(self.conn, code, {"name": "牧原股份"}, m,
                        self._fake_result(m, attrs), "2023-12-31", "养殖业", False)
        bor_m, bor_attrs = _stock_boring()
        engine._persist(self.conn, code, {"name": "牧原股份"}, bor_m,
                        self._fake_result(bor_m, bor_attrs), "2023-12-31", "养殖业", False)
        self.assertEqual(len(rdb.list_route_snapshots(self.conn, code)), 2)


class TestLegacyDbMigration(unittest.TestCase):
    def test_route_columns_added_to_old_table(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE research_stocks (
            code TEXT PRIMARY KEY, name TEXT, board TEXT, industry TEXT,
            system_type TEXT, user_type TEXT, type_confidence REAL, risk_level TEXT,
            total_score REAL, rule_version TEXT, latest_report_period TEXT,
            attr_scores_json TEXT, category_scores_json TEXT, valuation_json TEXT,
            financial_json TEXT, risk_json TEXT, data_completeness REAL,
            first_analyzed_at TEXT, last_updated_at TEXT, financial_updated_at TEXT)""")
        conn.execute("INSERT INTO research_stocks (code, name, total_score) VALUES ('600000','老数据',60.0)")
        conn.commit()
        conn.close()

        conn = rdb.init_db(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(research_stocks)")}
        self.assertTrue(rdb.ROUTE_COLUMNS.keys() <= cols)
        row = conn.execute("SELECT * FROM research_stocks WHERE code='600000'").fetchone()
        self.assertEqual(row["total_score"], 60.0)     # 老数据一个字节没动
        self.assertIsNone(row["primary_model"])       # 路由之前分析的，保持 NULL
        conn.close()


if __name__ == "__main__":
    unittest.main()
