# -*- coding: utf-8 -*-
"""「行业盈利状态」证据层与打分档位。

这一格替换的是 ``comps.append(("行业盈利状态", "正常", 10, 20))``——一个**常量**。
所以本文件一半的力气花在**反向对照**上：证明没证据时它确实与改动前逐位相同，
证明确有证据时才按档给分，以及证明它**从不变 missing**（一旦变 missing，
``_assemble`` 的分母会从 75 掉到 55，把同模板下与本次改动无关的股票也拖着动）。
"""
import unittest
from unittest.mock import patch

from research import engine, industry_margin as im, rules
from tests.test_research import _SYNTH_QUOTE, _synth_fin

PIG = "002714"          # 牧原，在组合里
NON_PIG = "002129"      # TCL中环，走同一个模板但不在组合里


def _item(code, period_end, revenue, cogs, *, scope="company_live_hog_all",
          status="extracted", period=None):
    """造一份 ``inspect_cached_pig_report`` 形状的报告条目。"""
    if status != "extracted":
        return {"stock_code": code, "status": status, "reason": "x"}
    period = period or im._quoted(period_end)
    common = {"value": None, "scope": scope, "period_start": period_end[:5] + "01-01",
              "period_end": period_end, "source_page": 17, "source_id": f"{code}.pdf",
              "document_hash": "a" * 64, "product_label": "生猪"}
    return {"stock_code": code, "status": "extracted", "report_period": period,
            "revenue": {**common, "value": revenue},
            "cogs": {**common, "value": cogs}}


def _by_code(mapping):
    """把 ``{code: [entries]}`` 变成 ``inspect_cached_pig_report`` 的替身。"""
    def fake(code, **_kw):
        return mapping.get(code, [])
    return fake


def _provider(**kw):
    """造一个已覆盖的 provider。默认：2 家有证据、2 家没有（与生产现状一致）。"""
    members = kw.pop("members", [
        {"stock_code": "002714", "name": "牧原", "margin": -0.0511, "weight": 0.5},
        {"stock_code": "001201", "name": "东瑞", "margin": -0.0401, "weight": 0.5},
    ])
    payload = {"cohort": "pig", "period": kw.pop("period", "2026H1"),
               "period_end": kw.pop("period_end", "2026-06-30"),
               "weighted_margin": kw.pop("weighted_margin", -0.0456),
               "members": members, "failures": kw.pop("failures", {}),
               "coverage": kw.pop("coverage", 0.5),
               "top_weight": kw.pop("top_weight", 0.5)}
    payload.update(kw)
    return im.IndustryMarginProvider(payload, cohort="pig")


def _score(m, provider):
    """跑一遍周期位置模块，返回 (模块结果, 行业盈利状态分量)。"""
    if provider is not None:
        m = {**m, "industry_margin": provider}
    res = rules.score_cyclical_position(m, m)
    comp = next(c for c in res["components"] if c["name"] == "行业盈利状态")
    return res, comp


class TestBands(unittest.TestCase):
    """档位表就是用户给的那五档，方向是「越惨分越高」。"""

    def setUp(self):
        self.bands = rules.RULES_V1["cyclical_position"]["industry_margin_bands"]

    def test_band_scores_are_exactly_the_five_given(self):
        self.assertEqual([s for _, _, s in self.bands], [20, 15, 10, 5, 0])

    def test_boundaries_are_left_closed(self):
        """5% 归「微利」、10% 归「正常」……用户原话就是「5%～10%」这种左闭右开。"""
        cases = [(-0.20, "危险边缘"), (0.0, "危险边缘"), (0.0499, "危险边缘"),
                 (0.05, "微利"), (0.0999, "微利"),
                 (0.10, "正常"), (0.1499, "正常"),
                 (0.15, "景气"), (0.1999, "景气"),
                 (0.20, "高景气"), (0.50, "高景气")]
        for value, label in cases:
            self.assertEqual(rules._industry_margin_band(value, self.bands)[0], label,
                             f"{value} 应该落在 {label}")

    def test_normal_band_equals_the_old_hardcoded_value(self):
        """「正常 = 10」必须等于替换掉的那个常量。

        不等于的话，「没建证据」和「算出来是正常」在数值上就分开了——而它们是
        有意做成语义不同、数值相同的。
        """
        self.assertEqual(rules._industry_margin_band(0.12, self.bands), ("正常", 10))

    def test_negative_margin_folds_into_the_worst_band(self):
        for value in (-0.0001, -0.0456, -0.5):
            self.assertEqual(rules._industry_margin_band(value, self.bands),
                             ("危险边缘", 20))


class TestEvidenceCollection(unittest.TestCase):
    """取数口径：显式成员、只收「生猪」、取最新够门槛的期间。"""

    def setUp(self):
        im._MEMO.update({"key": None, "cohort": None, "value": None})

    def test_membership_is_by_explicit_code_not_industry_name(self):
        """新希望 000876 的东财行业是「饲料」。按行业字串筛会漏掉它。"""
        self.assertEqual(im.cohort_of("000876"), "pig")
        self.assertEqual(sorted(im.COHORTS["pig"]["members"]),
                         ["000876", "001201", "002100", "002714"])

    def test_non_member_is_uncovered_and_untouched(self):
        p = im.load(NON_PIG)
        self.assertFalse(p.covered)
        self.assertFalse(p.available)
        self.assertEqual(p.to_dict()["covered"], False)

    def test_pig_industry_scope_is_not_mixed_in(self):
        """新希望的「猪产业」含屠宰，比「生猪」宽，不能并进组合。"""
        with patch("research.pig_reports.inspect_cached_pig_report",
                   _by_code({"002714": [_item("002714", "2026-06-30", 100.0, 90.0)],
                             "001201": [_item("001201", "2026-06-30", 50.0, 45.0)],
                             "000876": [_item("000876", "2026-06-30", 999.0, 1.0,
                                              scope="company_pig_industry")]})):
            p = im.load(PIG)
        self.assertTrue(p.available)
        self.assertEqual([m["stock_code"] for m in p.members],
                         ["002714", "001201"])
        self.assertIn("猪产业", p.failures["000876"])

    def test_picks_the_latest_period_that_reaches_the_floor(self):
        """2026H1 只有牧原一家 → 不够门槛，回退到两家都有的 2025A。

        每家必须留**全部**期间。只留最新的话这里会误报「证据不足」，而共同
        期间明明存在——这是本模块最容易犯且看不出来的错。
        """
        with patch("research.pig_reports.inspect_cached_pig_report", _by_code({
                "002714": [_item("002714", "2026-06-30", 100.0, 96.0),
                           _item("002714", "2025-12-31", 100.0, 80.0)],
                "001201": [_item("001201", "2025-12-31", 100.0, 90.0)]})):
            p = im.load(PIG)
        self.assertTrue(p.available)
        self.assertEqual(p.period, "2025A")
        self.assertAlmostEqual(p.weighted_margin, 0.15)   # 各占一半收入：(20%+10%)/2

    def test_latest_period_wins_when_both_have_it(self):
        with patch("research.pig_reports.inspect_cached_pig_report", _by_code({
                "002714": [_item("002714", "2026-06-30", 100.0, 95.0),
                           _item("002714", "2025-12-31", 100.0, 80.0)],
                "001201": [_item("001201", "2026-06-30", 100.0, 90.0),
                           _item("001201", "2025-12-31", 100.0, 70.0)]})):
            p = im.load(PIG)
        self.assertEqual(p.period, "2026H1")
        self.assertAlmostEqual(p.weighted_margin, 0.075)

    def test_weighting_is_by_revenue_not_by_head(self):
        """大的一家把小的那家压过去。组合毛利率不是简单平均。"""
        with patch("research.pig_reports.inspect_cached_pig_report", _by_code({
                "002714": [_item("002714", "2026-06-30", 990.0, 891.0)],   # 10%
                "001201": [_item("001201", "2026-06-30", 10.0, 0.0)]})):  # 100%
            p = im.load(PIG)
        self.assertAlmostEqual(p.weighted_margin, 0.109, places=4)
        self.assertAlmostEqual(p.top_weight, 0.99, places=4)

    def test_concentration_is_named_in_the_reason(self):
        with patch("research.pig_reports.inspect_cached_pig_report", _by_code({
                "002714": [_item("002714", "2026-06-30", 990.0, 940.0)],
                "001201": [_item("001201", "2026-06-30", 10.0, 9.0)]})):
            p = im.load(PIG)
        self.assertGreater(p.top_weight, im.CONCENTRATION_WARN)
        self.assertIn("牧原", p.describe())
        self.assertIn("约等于", p.describe())

    def test_each_failure_status_gets_its_own_reason(self):
        """「没有这张表」和「表被改过」不能长得一模一样，否则排查只能靠猜。"""
        reasons = set(im._FAIL_REASONS.values())
        self.assertEqual(len(reasons), len(im._FAIL_REASONS))
        for status in ("not_found", "ambiguous", "cache_incomplete",
                       "pdf_hash_mismatch", "rows_cache_stale",
                       "unsupported_report"):
            self.assertIn(status, im._FAIL_REASONS)

    def test_one_broken_member_does_not_take_down_the_cohort(self):
        def boom(code, **_kw):
            if code == "000876":
                raise RuntimeError("缓存目录炸了")
            return {"002714": [_item("002714", "2026-06-30", 100.0, 90.0)],
                    "001201": [_item("001201", "2026-06-30", 100.0, 90.0)]}[code]
        with patch("research.pig_reports.inspect_cached_pig_report", boom):
            p = im.load(PIG)
        self.assertTrue(p.available)
        self.assertIn("炸了", p.failures["000876"])

    def test_below_the_floor_is_unavailable_not_zero(self):
        with patch("research.pig_reports.inspect_cached_pig_report", _by_code({
                "002714": [_item("002714", "2026-06-30", 100.0, 90.0)]})):
            p = im.load(PIG)
        self.assertTrue(p.covered)
        self.assertFalse(p.available)
        self.assertIsNone(p.weighted_margin)
        self.assertIsNone(p.to_dict()["weighted_margin"])

    def test_result_is_memoized_per_cache_fingerprint(self):
        calls = []
        real = im._build

        def counted(cohort):
            calls.append(cohort)
            return real(cohort)
        with patch("research.pig_reports.inspect_cached_pig_report", _by_code({
                "002714": [_item("002714", "2026-06-30", 100.0, 90.0)],
                "001201": [_item("001201", "2026-06-30", 100.0, 90.0)]})), \
                patch.object(im, "_build", counted):
            im.load(PIG)
            im.load(PIG)
            im.load("001201")
        self.assertEqual(len(calls), 1, "同组合的重复调用不该重算")


class TestScoringReverseControl(unittest.TestCase):
    """反向对照：**先写下预期增分，再看它是不是这个数**。"""

    def setUp(self):
        self.m, _, _ = engine.build_metrics(PIG, _SYNTH_QUOTE, _synth_fin("养殖业"))
        self.base = rules.analyze(self.m)
        self.assertEqual(self.base["final"]["template"], "周期价值型",
                         "合成数据没走到周期价值型，下面的对照全是空转")

    def _total(self, provider):
        m = {**self.m, "industry_margin": provider}
        return rules.analyze(m)["final"]["score"]

    def test_the_five_bands_move_the_total_exactly_as_predicted(self):
        """0.30 × (档位分 − 10) / 75 × 100，四舍五入到分。"""
        for margin, expect in ((-0.0456, +4.00), (0.07, +2.00), (0.12, 0.0),
                               (0.17, -2.00), (0.25, -4.00)):
            with self.subTest(margin=margin):
                got = self._total(_provider(weighted_margin=margin)) - self.base["final"]["score"]
                self.assertAlmostEqual(got, expect, delta=0.01)

    def test_uncovered_provider_is_byte_identical_to_no_provider(self):
        """非猪行业（或没建证据）必须**逐位不变**——这是回退分支的守门测试。"""
        with_provider = rules.score_cyclical_position(
            {**self.m, "industry_margin": im.load(NON_PIG)}, self.m)
        without = rules.score_cyclical_position(self.m, self.m)
        self.assertEqual(with_provider, without)

    def test_unavailable_provider_does_not_move_the_score(self):
        """建了证据但这期不够门槛：分数不动，但标签与理由要说实话。"""
        p = _provider(weighted_margin=None, period=None, period_end=None,
                      failures={"002100": im._FAIL_REASONS["not_found"]})
        self.assertTrue(p.covered)
        self.assertFalse(p.available)
        total = self._total(p)
        self.assertAlmostEqual(total, self.base["final"]["score"], delta=0.01)
        _, comp = _score(self.m, p)
        self.assertEqual(comp["value"], "证据不足")
        self.assertEqual(comp["score"], 10)

    def test_the_component_is_never_missing_in_any_branch(self):
        """**本次改动最硬的一条不变量。**

        这一格一旦变 missing，``_assemble`` 的有效满分就从 75 掉到 55，
        4 只猪企朝不同方向变，与本次改动毫无关系的 002129 / 002460 也会跟着
        动——那是最难解释的一种连带变化。所以三条分支都必须 eligible。
        """
        branches = {
            "已覆盖且证据齐备": _provider(weighted_margin=-0.0456),
            "已覆盖但证据不足": _provider(weighted_margin=None, period=None),
            "未覆盖": im.load(NON_PIG),
            "没注入": None,
        }
        for label, provider in branches.items():
            with self.subTest(branch=label):
                res, comp = _score(self.m, provider)
                self.assertEqual(comp["status"], "ok", label)
                self.assertFalse(comp["missing"], label)
                self.assertTrue(comp["eligible"], label)
                self.assertIsNotNone(comp["score"], label)
                self.assertEqual(res["max_available"], 75.0,
                                 f"{label}：分母变了，说明这一格掉出了归一化")
                self.assertEqual(res["total_max"], 100.0, label)

    def test_every_branch_states_what_it_knows(self):
        """三种「不知道」要在文字上分得开：没建 / 建了没数 / 有数是多少。"""
        _, covered = _score(self.m, _provider(weighted_margin=-0.0456))
        self.assertIn("2026H1", covered["value"])
        self.assertIn("危险边缘", covered["value"])
        self.assertIn("覆盖 2/4", covered["reason"])

        _, insufficient = _score(self.m, _provider(weighted_margin=None, period=None))
        self.assertEqual(insufficient["value"], "证据不足")

        _, uncovered = _score(self.m, im.load(NON_PIG))
        self.assertEqual(uncovered["value"], "未覆盖")
        self.assertIn("未建立", uncovered["reason"])

    def test_a_non_pig_stock_on_the_same_template_is_untouched(self):
        """002129 TCL中环走同一个模板但不是猪企：总分必须一分不动。"""
        m, _, _ = engine.build_metrics(NON_PIG, _SYNTH_QUOTE, _synth_fin("养殖业"))
        base = rules.analyze(m)["final"]["score"]
        m2 = {**m, "industry_margin": im.load(NON_PIG)}
        self.assertEqual(rules.analyze(m2)["final"]["score"], base)


class TestSnapshotPayload(unittest.TestCase):
    """落库形态：**只在已覆盖的行业写**，且只写实测事实。"""

    def test_only_covered_industries_get_the_key(self):
        m, _, _ = engine.build_metrics("600741", _SYNTH_QUOTE, _synth_fin("汽车零部件"))
        self.assertNotIn("industry_margin", engine._financial_summary(m))

    def test_payload_carries_the_facts_and_no_score(self):
        m, _, _ = engine.build_metrics(PIG, _SYNTH_QUOTE, _synth_fin("养殖业"))
        m = {**m, "industry_margin": _provider(weighted_margin=-0.0456)}
        got = engine._financial_summary(m)["industry_margin"]
        self.assertTrue(got["covered"])
        self.assertAlmostEqual(got["weighted_margin"], -0.0456)
        self.assertEqual(got["cohort"], "pig")
        # 分数与档位是**评分层**的决定，改档位不该让证据看起来变了。
        self.assertNotIn("score", got)
        self.assertNotIn("label_band", got)
        self.assertNotIn("state", got)


class TestScoringWithPbHistoryPresent(unittest.TestCase):
    """**生产形状**：PB 分位那一格有真数据时的分母与档位差。

    上面那些用例的 ``m`` 里没有 pb_history（PB 分位恒 missing），分母是 75——那是
    「没有估值历史源」时代的分母。接上东财估值历史之后生产路径的分母是 **100**，
    于是一个档位的差从 ``0.30 × 10/75 × 100`` 变成 ``0.30 × 10/100 × 100``：
    ±4.00 / ±2.00 会变成 ±3.00 / ±1.50。这条把新分母钉住，免得哪天有人照着旧期望
    值去调权重——那会把一个算术问题改成规则问题。
    """

    @staticmethod
    def _pb_months(n=105, pb=2.0):
        """n 个月等值月末序列（末行是当期）。等值 → 分位恒为 1.0，与本条的档位差无关。"""
        out = []
        year, month = 2018, 1
        for _ in range(n):
            mon = "%04d-%02d" % (year, month)
            out.append({"month": mon, "trade_date": mon + "-28", "pb": pb})
            month += 1
            if month > 12:
                year, month = year + 1, 1
        return out

    def setUp(self):
        m, _, _ = engine.build_metrics(PIG, _SYNTH_QUOTE, _synth_fin("养殖业"))
        self.without = rules.analyze(m)["final"]["score"]
        self.m = {**m, "pb_history": self._pb_months()}
        res = rules.score_cyclical_position(self.m, self.m)
        self.assertEqual(res["max_available"], 100.0,
                         "PB 分位没进分母——那些「档位差 ±4.00」的旧期望值会重新成立")
        self.base = rules.analyze(self.m)["final"]["score"]

    def _total(self, provider):
        return rules.analyze({**self.m, "industry_margin": provider})["final"]["score"]

    def test_the_five_bands_move_the_total_by_the_new_denominator(self):
        """0.30 × (档位分 − 10) / 100 × 100，四舍五入到分。"""
        for margin, expect in ((-0.0456, +3.00), (0.07, +1.50), (0.12, 0.0),
                               (0.17, -1.50), (0.25, -3.00)):
            with self.subTest(margin=margin):
                got = self._total(_provider(weighted_margin=margin)) - self.base
                self.assertAlmostEqual(got, expect, delta=0.01)

    def test_the_pb_cell_is_eligible_and_scores_straight_off_the_series(self):
        """等值序列 → 当期等于历史最高 → 分位 1.0 → 这一格 0 分（不是缺失）。"""
        res = rules.score_cyclical_position(self.m, self.m)
        pb = next(c for c in res["components"] if c["name"] == "PB分位")
        self.assertEqual(pb["status"], "ok")
        self.assertEqual(pb["raw"], 1.0)
        self.assertEqual(pb["score"], 0.0)
        self.assertEqual(len([c for c in res["components"] if c["status"] == "ok"]), 4)
        self.assertAlmostEqual(res["score"],
                               sum(c["score"] for c in res["components"]), places=6)
        # 分母从 75 变 100 是本条的存在理由：同一个模块，PB 一进来分数就会动。
        self.assertNotAlmostEqual(self.base, self.without, places=2)


if __name__ == "__main__":
    unittest.main()
