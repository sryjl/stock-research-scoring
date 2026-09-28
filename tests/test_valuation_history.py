# -*- coding: utf-8 -*-
"""估值历史 Provider、月末序列口径，以及「周期位置」三个分位的新口径。

三组断言分别对应三件容易做错的事：

1. **月末桶取的是该月最后一个有效交易日**。东财那个接口默认降序返回（``sortTypes=-1``），
   按输入顺序覆盖的话每个桶会留下该月**第一个**交易日——本轮真的写出过这个 bug
   （牧原当期 PB 从 2.952 变成 3.092，分数看着还挺合理）。所以这里喂**降序**输入。
2. **失败不连打源、不删旧行**。缓存层的价值全在这里：源挂了要让 PB 分位那一格
   明确变 missing，而不是把整条分析主流程带下去，也不能每次都去撞源。
3. **历史不足给中性分、当期不入样本、分母恒为 100**。这一格一旦变 missing，
   ``_assemble`` 的有效满分从 100 掉到 75，同模板下与本次改动无关的股票也会跟着动。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from research import rules, valuation_history as vh


def _pb_rows(pbs, start=(2018, 1)):
    """按自然月升序造月末序列；``pbs`` 的最后一个是当期。"""
    out = []
    year, month = start
    for pb in pbs:
        mon = "%04d-%02d" % (year, month)
        out.append({"month": mon, "trade_date": mon + "-28", "pb": float(pb)})
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def _day(day, pb, **kw):
    """造一行 ``get_valuation_history`` 形状的日频数据。"""
    row = {"TRADE_DATE": day, "PB_MRQ": pb, "CLOSE_PRICE": 10.0, "PE_TTM": 12.0,
           "PS_TTM": 1.5, "PCF_OCF_TTM": 8.0, "TOTAL_MARKET_CAP": 1e10}
    row.update(kw)
    return row


class _FakeProvider:
    """``get_valuation_history`` 的替身：返回预设结果，并记下被调了几次。"""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def get_valuation_history(self, code):        # noqa: ARG002
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TestMonthEndSeries(unittest.TestCase):
    def test_the_bucket_keeps_the_last_valid_trading_day(self):
        """**降序**输入（接口的真实顺序）也要取月末那一天，不能取月初。"""
        raw = [_day("2026-03-31", 3.0), _day("2026-03-02", 2.0),
               _day("2026-02-27", 1.5), _day("2026-02-03", 1.4)]
        rows = vh.to_month_ends(raw)
        self.assertEqual([r["month"] for r in rows], ["2026-02", "2026-03"])
        self.assertEqual(rows[0]["trade_date"], "2026-02-27")
        self.assertEqual(rows[1]["trade_date"], "2026-03-31")
        self.assertEqual(rows[1]["pb"], 3.0)

    def test_an_invalid_last_day_falls_back_within_the_month(self):
        """该月最后一个交易日 PB 取不到 → 往前找，而不是整个月丢掉。"""
        raw = [_day("2026-03-31", None), _day("2026-03-20", -0.5),
               _day("2026-03-02", 2.0)]
        rows = vh.to_month_ends(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_date"], "2026-03-02")

    def test_a_month_with_no_valid_pb_produces_no_row(self):
        """负净资产（PB ≤ 0）不算「便宜」，整月无有效值时这一格就不存在。"""
        raw = [_day("2026-03-31", -1.0), _day("2026-03-02", None),
               _day("2026-02-27", 1.2)]
        rows = vh.to_month_ends(raw)
        self.assertEqual([r["month"] for r in rows], ["2026-02"])

    def test_daily_rows_collapse_to_one_row_per_month(self):
        raw = [_day("2026-01-0%d" % d, d) for d in range(1, 6)] + \
              [_day("2026-02-0%d" % d, 10 + d) for d in range(1, 4)]
        rows = vh.to_month_ends(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pb"], 5.0)

    def test_other_valuation_fields_ride_along(self):
        """PE/PS/PCF/市值 与 PB 同源同月存下来，以后接 PE 分位时不必再请求一次。"""
        rows = vh.to_month_ends([_day("2026-03-31", 3.0, PE_TTM=-20.0,
                                      PS_TTM=1.25, PCF_OCF_TTM=9.5,
                                      TOTAL_MARKET_CAP=2e11)])
        self.assertEqual(rows[0]["pe_ttm"], -20.0)
        self.assertEqual(rows[0]["ps_ttm"], 1.25)
        self.assertEqual(rows[0]["market_cap"], 2e11)
        self.assertEqual(rows[0]["source"], vh.SOURCE)

    def test_junk_rows_are_ignored_not_fatal(self):
        rows = vh.to_month_ends([None, "x", {}, {"TRADE_DATE": "2026-3-1"},
                                 _day("2026-03-31", 3.0)])
        self.assertEqual(len(rows), 1)


class TestFreshness(unittest.TestCase):
    def _meta(self, **kw):
        meta = {"policy_version": vh.POLICY_VERSION, "fetched_at": "2026-09-25",
                "status": "ok", "last_month": "2026-08"}
        meta.update(kw)
        return meta

    def test_last_complete_month_never_counts_the_running_one(self):
        self.assertEqual(vh.last_complete_month("2026-09-26"), "2026-08")
        self.assertEqual(vh.last_complete_month("2026-09-01"), "2026-08")
        self.assertEqual(vh.last_complete_month("2026-01-15"), "2025-12")
        self.assertEqual(vh.last_complete_month("2026-01-01"), "2025-12")

    def test_missing_meta_and_stale_policy_both_refetch(self):
        self.assertTrue(vh.needs_refresh(None, "2026-09-26"))
        self.assertTrue(vh.needs_refresh(self._meta(policy_version="OLD"), "2026-09-26"))

    def test_today_already_tried_never_hammers_the_source(self):
        """成功也好失败也好，同一天只试一次——失败隔天再来，不连打。"""
        today = self._meta(fetched_at="2026-09-26")
        self.assertFalse(vh.needs_refresh(today, "2026-09-26"))
        failed = self._meta(fetched_at="2026-09-26", status="error")
        self.assertFalse(vh.needs_refresh(failed, "2026-09-26"))

    def test_a_failure_is_retried_the_next_day(self):
        self.assertTrue(vh.needs_refresh(
            self._meta(fetched_at="2026-09-25", status="error"), "2026-09-26"))

    def test_a_new_complete_month_triggers_exactly_one_refetch(self):
        """上一个完整月已覆盖 → 不抓；新月份结束 → 抓一次。"""
        self.assertFalse(vh.needs_refresh(self._meta(last_month="2026-08"), "2026-09-26"))
        self.assertTrue(vh.needs_refresh(self._meta(last_month="2026-07"), "2026-09-26"))

    def test_an_unparseable_date_falls_back_to_today(self):
        self.assertEqual(vh.last_complete_month("不是日期"), vh.last_complete_month())


class TestCacheIsNeverFatal(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        from research import db
        self.conn = db.init_db(self.path)
        self.rows = vh.to_month_ends(
            [_day("2018-01-31", 5.0), _day("2018-02-28", 4.0), _day("2018-03-30", 3.0)])

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _ensure(self, provider, **kw):
        with patch("research.providers.get_provider", lambda: provider):
            return vh.ensure(self.conn, "002714", **kw)

    def test_a_dead_source_leaves_a_missing_branch_not_an_exception(self):
        """源返回 None（不可用）→ 不抛、返回空，并**留下一条失败状态**。"""
        p = _FakeProvider(None)
        self.assertEqual(self._ensure(p), [])
        meta = self.conn.execute("SELECT * FROM valuation_history_meta").fetchone()
        self.assertEqual(meta["status"], "error")
        self.assertIsNone(meta["last_month"])

    def test_a_raising_source_is_swallowed(self):
        self.assertEqual(self._ensure(_FakeProvider(RuntimeError("boom"))), [])
        meta = self.conn.execute("SELECT * FROM valuation_history_meta").fetchone()
        self.assertEqual(meta["status"], "error")
        self.assertIn("boom", meta["note"])

    def test_a_failure_never_deletes_what_was_already_cached(self):
        """先成功缓存，再让源挂掉：旧行必须原样留着。

        ``force`` 是必须的——不 force 的话当天根本不会去撞源（那正是另一条测试
        钉住的「不连打」），于是这条就变成在测短路，而不是在测「失败不删旧行」。
        """
        self._ensure(_FakeProvider([_day("2018-01-31", 5.0)]))
        before = vh.load(self.conn, "002714")
        self.assertEqual(len(before), 1)
        self.assertEqual(self._ensure(_FakeProvider(None), force=True), before,
                         "失败时应当退回已有缓存（而不是清空或抛异常）")
        self.assertEqual(vh.load(self.conn, "002714"), before, "旧行被清掉了")
        meta = self.conn.execute("SELECT * FROM valuation_history_meta").fetchone()
        self.assertEqual(meta["status"], "error", "失败状态没记下来")

    def test_the_second_call_the_same_day_does_not_touch_the_source(self):
        p = _FakeProvider([_day("2018-01-31", 5.0)])
        self._ensure(p)
        self._ensure(p)
        self.assertEqual(p.calls, 1, "同一天重复联网了")

    def test_rows_that_yield_nothing_usable_are_recorded_as_a_failure(self):
        """拿到一堆交易日但全是无效 PB：不能当成成功写一条空序列。"""
        self._ensure(_FakeProvider([_day("2018-01-31", None)]))
        meta = self.conn.execute("SELECT * FROM valuation_history_meta").fetchone()
        self.assertEqual(meta["status"], "error")

    def test_load_on_an_untouched_db_returns_empty(self):
        self.assertEqual(vh.load(self.conn, "000000"), [])
        self.assertEqual(vh.pb_series(self.conn, "000000"), [])

    def test_pb_series_hands_the_scoring_layer_only_three_keys(self):
        self._ensure(_FakeProvider([_day("2018-01-31", 5.0), _day("2018-02-28", 4.0)]))
        series = vh.pb_series(self.conn, "002714")
        self.assertEqual([r["month"] for r in series], ["2018-01", "2018-02"])
        self.assertEqual(set(series[0]), {"month", "trade_date", "pb"})
        self.assertEqual(series[-1]["pb"], 4.0, "末行必须是当前值")


#: 合成的年度序列：两格的样本都够（5 年 / 5 年），于是只有 PB 那一格随用例变化。
#: 少了这一层，PB 变正常时分母也到不了 100——那样断言的就不是「PB 进了分母」了。
_PROFITS = [1.0, 2.0, 3.0, 4.0, 5.0]
_MARGINS = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def _m(pbs=None, profits=_PROFITS, margins=_MARGINS):
    def annual(values):
        return [("%04d-12-31" % (2020 + i), v) for i, v in enumerate(values)]
    return {"pb_history": _pb_rows(pbs) if pbs else [],
            "net_profit": annual(profits), "gross_margin": annual(margins)}


class TestPbPercentileGates(unittest.TestCase):
    """三档样本：<36 中性分、36~47 置信度下降、>=48 正常。**三档都不许 missing。**"""

    def _module(self, n_months, current=5.0):
        # n_months 是**历史样本**月数（不含当期）
        pbs = [10.0] * (n_months // 2) + [1.0] * (n_months - n_months // 2) + [current]
        res = rules.score_cyclical_position(_m(pbs), _m(pbs))
        comp = next(c for c in res["components"] if c["name"] == "PB分位")
        return res, comp

    def test_the_three_tiers_all_stay_eligible(self):
        for months, label in ((35, "不足"), (40, "偏短"), (104, "正常")):
            with self.subTest(months=months):
                res, comp = self._module(months)
                self.assertEqual(comp["status"], "ok", label)
                self.assertFalse(comp["missing"], label)
                self.assertIsNotNone(comp["score"], label)
                self.assertEqual(res["max_available"], 100.0,
                                 f"{label}：分母变了，说明这一格掉出了归一化")

    def test_below_the_floor_gets_the_neutral_score_not_a_zero(self):
        """历史不足**不许**给 0 分：那是把「没历史」冒充成「历史上最差」。"""
        res, comp = self._module(35)
        neutral = rules.piecewise(rules.RULES_V1["cyclical_position"]["neutral_percentile"],
                                  rules.RULES_V1["cyclical_position"]["pb_percentile"])
        self.assertAlmostEqual(comp["score"], neutral, places=6)
        self.assertGreater(comp["score"], 0.0)
        self.assertIsNone(comp["raw"], "中性分不该假装有一个分位数")
        self.assertIn("不足门槛", comp["sample"])

    def test_the_short_tier_says_so_out_loud(self):
        _, short = self._module(40)
        _, normal = self._module(104)
        self.assertIn("置信度下降", short["sample"])
        self.assertNotIn("置信度下降", normal["sample"])
        self.assertIn("40 个月", short["sample"])

    def test_the_note_states_the_sample_span_and_the_current_value(self):
        _, comp = self._module(104)
        self.assertIn("104 个月", comp["sample"])
        self.assertIn("2018-01~2026-08", comp["sample"])
        self.assertIn("当期 PB 5.000", comp["sample"])

    def test_the_current_value_is_not_in_its_own_sample(self):
        """当期是历史最低 → 分位必须是 0.0；把它算进样本会得到 1/N 而不是 0。"""
        _, comp = self._module(104, current=0.5)
        self.assertEqual(comp["raw"], 0.0)
        self.assertEqual(comp["score"], 25.0)
        _, comp = self._module(104, current=99.0)
        self.assertEqual(comp["raw"], 1.0)

    def test_the_5y_window_is_only_shown_it_does_not_score(self):
        """近 60 个月分位与全史分位并排给出；打分用全史那一份。"""
        pbs = [1.0] * 40 + [10.0] * 60 + [5.0]
        comp = next(c for c in rules.score_cyclical_position(_m(pbs), _m(pbs))["components"]
                    if c["name"] == "PB分位")
        self.assertEqual(comp["raw"], 0.4, "全史分位 = 40/100")
        self.assertIn("近 60 个月分位 0.0%", comp["sample"])
        self.assertAlmostEqual(comp["score"], rules.piecewise(
            0.4, rules.RULES_V1["cyclical_position"]["pb_percentile"]), places=6)

    def test_a_stock_too_young_for_5y_says_so_instead_of_printing_a_number(self):
        _, comp = self._module(40)
        self.assertIn("未出 5Y 分位", comp["sample"])

    def test_no_history_at_all_is_the_only_missing_branch(self):
        res = rules.score_cyclical_position(_m(), _m())
        comp = next(c for c in res["components"] if c["name"] == "PB分位")
        self.assertEqual(comp["status"], "missing_data")
        self.assertIn("取不到估值历史", comp["reason"])
        self.assertEqual(res["max_available"], 75.0)

    def test_the_bare_percentile_rank_helper_is_unchanged(self):
        """公共函数的定义**没变**（历史里 ≤ value 的占比）；剔除当期是调用侧策略。"""
        self.assertEqual(rules.percentile_rank(5.0, [1.0, 5.0, 9.0]), 2 / 3)


class TestLegacyPercentilesGetTheSameTreatment(unittest.TestCase):
    """利润分位 / 毛利率分位：同样剔除当期，同样在样本不足时给中性分。"""

    def _comps(self, profits, margins):
        m = {"net_profit": [("20%02d-12-31" % (18 + i), v) for i, v in enumerate(profits)],
             "gross_margin": [("20%02d-12-31" % (18 + i), v) for i, v in enumerate(margins)],
             "pb_history": []}
        res = rules.score_cyclical_position(m, m)
        return res, {c["name"]: c for c in res["components"]}

    def test_the_current_year_is_not_in_its_own_sample(self):
        """当期是历史最高 → 分位 1.0；若把自己算进样本会得到 4/5 = 0.8。"""
        _, comps = self._comps([1.0, 2.0, 3.0, 4.0, 99.0], [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(comps["利润分位"]["raw"], 1.0)
        _, comps = self._comps([1.0, 2.0, 3.0, 4.0, 0.5], [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(comps["利润分位"]["raw"], 0.0)

    def test_a_short_profit_history_gets_the_neutral_score(self):
        cfg = rules.RULES_V1["cyclical_position"]
        _, comps = self._comps([1.0, 99.0], [1.0, 2.0, 3.0, 4.0, 5.0])
        comp = comps["利润分位"]
        self.assertEqual(comp["status"], "ok")
        self.assertIsNone(comp["raw"])
        self.assertAlmostEqual(
            comp["score"], rules.piecewise(cfg["neutral_percentile"], cfg["profit_percentile"]),
            places=6)

    def test_a_single_data_point_no_longer_scores_zero(self):
        """单点序列下 percentile_rank 会给 1.0（自己跟自己比）＝ 0 分，是最坏的那种错。"""
        _, comps = self._comps([7.0], [7.0])
        self.assertEqual(comps["利润分位"]["status"], "ok")
        self.assertGreater(comps["利润分位"]["score"], 0.0)
        self.assertGreater(comps["毛利率分位"]["score"], 0.0)

    def test_no_series_at_all_is_still_missing(self):
        _, comps = self._comps([], [])
        self.assertEqual(comps["利润分位"]["status"], "missing_data")
        self.assertEqual(comps["毛利率分位"]["status"], "missing_data")
        self.assertIn("缺年度净利润序列", comps["利润分位"]["reason"])

    def test_the_window_is_still_the_old_one(self):
        """8 年 / 6 年窗口一个字没动——本次只改「样本里有没有当期」。"""
        profits = [float(i) for i in range(1, 13)]
        _, comps = self._comps(profits, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        self.assertIn("7 年历史样本", comps["利润分位"]["reason"])
        self.assertIn("5 年历史样本", comps["毛利率分位"]["reason"])


if __name__ == "__main__":
    unittest.main()
