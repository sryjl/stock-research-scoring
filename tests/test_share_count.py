# -*- coding: utf-8 -*-
"""tests/test_share_count.py — 总股本的口径（BATCH 3.1 §七 / §八）。

要修的根因不是「某个数算错了」，是**股本的默认口径选错了**：
``净资产 / BPS`` 被当成「真实总股本」的最后兜底，于是模拟价格那条路径先用它
反推股本、再用股本造一个市值，两个数都是编的。BPS 的定义本身就是
「净资产 ÷ 股本」，拿它反推是循环论证；更糟的是它**不会报错**——派息率照样
显示「有个数」。

线上实测：28 只里 **22 只**的落库股本与「市值 / 价格」口径不一致，比值
0.954 ~ 2.573；而「市值 / 价格」算出来的正是真实股本（招商银行 252.19 亿股、
中国建筑 413.20 亿股、TCL中环 40.43 亿股、中国平安 181.08 亿股，逐只对得上）。
行情商（东财 f84）对这批股票**一律返回** ``total_shares: None``，所以优先级 1
今天根本不会命中——真正在生效的是优先级 2。

这一层钉四件事：

1. **三级优先级的次序**，以及三级都取不到时返回 ``(None, None)`` 而不是编一个数。
2. **容差住在 ``RULES_V1``**：改规则就改行为（改到 0 和改到 0.05 结论相反）。
3. **``shares_source`` 必须能报出来**，且**不许进 ``m["current"]``**——那一整块进
   ``research_snapshots.valuation_metrics`` 并参与 ``result_hash``，多一个键等于
   给每只股票白加一行分数没变的旧快照。
4. **``simulate_price(当前价)`` 与正常分析逐位一致**，600502 的派息率不再有
   2.573 倍偏差（§八点名的那一条）。
"""
import copy
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.db as research_db  # noqa: E402
from research import engine, rules  # noqa: E402
from tests.test_audit_gate import _stock_row  # noqa: E402
from tests.test_research import _synth_fin  # noqa: E402

# --------------------------------------------------------------------------- #
# 600502 安徽建工的真实数字（2026-09-27 线上实测）
# --------------------------------------------------------------------------- #
PRICE = 4.16
MARKET_CAP = 7.141e9
#: 市值 / 价格 —— 这一条与分红记录里的股本一致到 0.003%，是真实股本。
SHARES_FROM_MARKET_CAP = MARKET_CAP / PRICE            # 1_716_586_538.46
#: 分红记录里的股本（``analyze`` 派息率真正用的那个数）。
SHARES_FROM_DIVIDEND = 1716533938
#: 净资产 / BPS —— 落库的旧值，错的那个。
SHARES_FROM_EQUITY = 4417000000.0
BPS = 5.0
TOTAL_EQUITY = SHARES_FROM_EQUITY * BPS                # 2.2085e10
DIVIDEND_PER_SHARE = 0.27
NET_PROFIT = 1.526e9

#: 分红记录股本与市值股本的比值 = 派息率被放大的倍数。**§八 点名的那个数。**
LEGACY_PAYOUT_INFLATION = SHARES_FROM_EQUITY / SHARES_FROM_DIVIDEND


class _Boom(Exception):
    """探针异常：谁拿到它就证明代码走到了那一步。"""


class FakeProvider(object):
    """只够跑分析路径的 Provider。

    ``total_shares`` **刻意不给**：线上行情商对这批股票真的一律返回 ``None``
    （东财 f84），只给价格与总市值。给一个显式股本会让测试测的是另一条分支。
    """

    def __init__(self, price=PRICE, market_cap=MARKET_CAP, name="安徽建工"):
        self.price, self.market_cap, self.name = price, market_cap, name

    def get_quote(self, code):
        return {"code": code, "name": self.name, "price": self.price,
                "total_market_cap": self.market_cap, "total_shares": None}

    def get_latest_report_period(self, code):
        return None


def _fin():
    """合成财报：让 ``净资产 / BPS`` 与「市值 / 价格」差 2.573 倍。

    结构照抄 ``tests.test_research._synth_fin``（评分层认得的那一份），只把
    三个数改成 600502 的真实值：BPS/净资产（错的股本来源）、分红记录里的股本、
    最新一年的净利润（派息率的分母）。
    """
    fin = _synth_fin("建筑装饰")
    fin["cache_version"] = engine.FIN_CACHE_VERSION
    for row in fin["balance"]:
        row["total_equity"] = TOTAL_EQUITY
    for row in fin["indicators"]:
        row["bps"] = BPS
    for row in fin["indicators"] + fin["income"]:
        if row["report_period"].startswith("2026"):
            row["net_profit"] = NET_PROFIT
            row["deduct_profit"] = NET_PROFIT
    fin["dividends"] = [{"year": 2026, "dividend_per_share": DIVIDEND_PER_SHARE,
                         "total_shares": SHARES_FROM_DIVIDEND}]
    return fin


# --------------------------------------------------------------------------- #
# §七 三级口径：解析器本身
# --------------------------------------------------------------------------- #
class TestResolveTotalShares(unittest.TestCase):
    """``resolve_total_shares`` 的优先级与来源标注。"""

    def _resolve(self, quote, bps=None, equity=None):
        return engine.resolve_total_shares(quote, bps, equity)

    # ---- 优先级 2：市值 / 价格 -------------------------------------------
    def test_market_cap_over_price_is_used_when_no_explicit_field(self):
        """行情只给价格与市值时必须用它们相除——**这是线上唯一生效的那条**。"""
        self.assertEqual(self._resolve({"price": 4.16, "total_market_cap": 7.141e9}),
                         (SHARES_FROM_MARKET_CAP, "market_cap"))

    def test_the_legacy_equity_over_bps_is_not_used_while_market_cap_exists(self):
        """有市值口径时**轮不到**净产/BPS——2.573 倍就是这么来的。"""
        shares, source = self._resolve(
            {"price": PRICE, "total_market_cap": MARKET_CAP}, BPS, TOTAL_EQUITY)
        self.assertEqual(source, "market_cap")
        self.assertAlmostEqual(shares / SHARES_FROM_EQUITY, 1 / LEGACY_PAYOUT_INFLATION, places=4)

    # ---- 优先级 1：显式股本 ----------------------------------------------
    def test_an_explicit_field_that_agrees_with_the_market_cap_wins(self):
        """两口径自洽（舍入级差异）时保留行情商直接给的数。"""
        shares, source = self._resolve(
            {"price": PRICE, "total_market_cap": MARKET_CAP,
             "total_shares": SHARES_FROM_MARKET_CAP * 1.002})
        self.assertEqual(source, "explicit")
        self.assertAlmostEqual(shares, SHARES_FROM_MARKET_CAP * 1.002)

    def test_an_explicit_field_that_disagrees_loses_to_the_market_cap(self):
        """差得离谱就取市值口径：真正进分母的是市值，让两者打架必生口径分叉。"""
        shares, source = self._resolve(
            {"price": PRICE, "total_market_cap": MARKET_CAP,
             "total_shares": SHARES_FROM_EQUITY})
        self.assertEqual(source, "market_cap")
        self.assertEqual(shares, SHARES_FROM_MARKET_CAP)

    def test_without_a_price_the_explicit_field_is_all_we_have(self):
        shares, source = self._resolve({"total_shares": 5e8, "total_market_cap": 1e10})
        self.assertEqual((shares, source), (5e8, "explicit"))

    # ---- 优先级 3：净资产 / BPS（只配兜底）--------------------------------
    def test_equity_over_bps_is_the_last_resort_and_says_so(self):
        shares, source = self._resolve({}, BPS, TOTAL_EQUITY)
        self.assertEqual((shares, source), (SHARES_FROM_EQUITY, "equity_per_bps"))

    def test_nothing_available_returns_none_and_does_not_invent_a_number(self):
        self.assertEqual(self._resolve({}), (None, None))
        # 净资产为 0 / BPS 为 0 同样是「没有」，不许除出 0 或 inf 来
        self.assertEqual(self._resolve({}, 0.0, TOTAL_EQUITY), (None, None))
        self.assertEqual(self._resolve({}, BPS, 0), (None, None))
        # 价格或市值为 0 时不许拿它当分母
        self.assertEqual(self._resolve({"price": 0, "total_market_cap": 1e10}), (None, None))

    def test_a_non_positive_explicit_field_is_treated_as_missing(self):
        shares, source = self._resolve({"price": PRICE, "total_market_cap": MARKET_CAP,
                                       "total_shares": 0})
        self.assertEqual((shares, source), (SHARES_FROM_MARKET_CAP, "market_cap"))

    # ---- 容差住在 RULES_V1，改规则就改行为 --------------------------------
    def test_the_consistency_tolerance_is_read_from_rules_at_call_time(self):
        """§四 的同一条要求：阈值不许在模块里抄第二份，改 ``RULES_V1`` 必须真的改行为。

        样本：显式 100 股 vs 市值口径 102 股（差 1.96%）。
        默认容差 1% → 判为不一致 → 取市值口径；调到 5% → 判为自洽 → 保留显式值。
        """
        quote = {"total_shares": 100.0, "price": 10.0, "total_market_cap": 1020.0}
        self.assertEqual(engine.resolve_total_shares(quote, None, None), (102.0, "market_cap"))
        with mock.patch.dict(rules.RULES_V1, {"share_count": {"consistency_tolerance": 0.05}}):
            self.assertEqual(engine.resolve_total_shares(quote, None, None), (100.0, "explicit"))

    def test_the_threshold_lives_in_rules_v1(self):
        self.assertIn("consistency_tolerance", rules.RULES_V1["share_count"])
        self.assertGreater(rules.RULES_V1["share_count"]["consistency_tolerance"], 0)


# --------------------------------------------------------------------------- #
# §七 接线：两条路径共用同一个解析器，来源可见但不进快照
# --------------------------------------------------------------------------- #
class TestShareCountWiring(unittest.TestCase):
    def test_both_paths_go_through_the_same_resolver(self):
        """两处各写一份必然漂移——而这个数已经漂过一次了。"""
        for fn in (engine.build_metrics, engine.simulate_price):
            self.assertIn("resolve_total_shares", fn.__code__.co_names, fn.__name__)

    def test_the_source_is_reported_but_never_enters_the_snapshot_payload(self):
        """``shares_source`` 在顶层 ``m``，**不在** ``m["current"]``。

        ``m["current"]`` 整块进快照的 ``valuation_metrics`` 并参与 ``result_hash``，
        往里加键 = 给每只股票白加一行**分数没变**的旧快照。
        """
        m, _period, _industry = engine.build_metrics("600502",
                                                     FakeProvider().get_quote("600502"), _fin())
        self.assertEqual(m["shares_source"], "market_cap")
        self.assertNotIn("shares_source", m["current"])

    def test_build_metrics_now_reports_the_market_cap_share_count(self):
        m, _p, _i = engine.build_metrics("600502", FakeProvider().get_quote("600502"), _fin())
        self.assertAlmostEqual(m["current"]["total_shares"], SHARES_FROM_MARKET_CAP, places=4)


# --------------------------------------------------------------------------- #
# §八 端到端：模拟价格 == 正常分析，600502 不再有 2.573 倍派息率偏差
# --------------------------------------------------------------------------- #
class TestSimulatePriceMatchesAnalysis(unittest.TestCase):
    """``simulate_price(当前价)`` 必须与 ``analyze`` 在同一价格上给出同一套数。

    这条最容易被写坏的形态是：模拟自己拼一个 quote（旧写法就是拿 BPS 反推股本
    再乘价格造市值），于是同一只股票、同一个价格，模拟页与详情页显示两个派息率。
    """

    CODE = "600502"

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)
        self._patch = mock.patch.object(research_db, "DEFAULT_PATH", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._cleanup)

        self._seed()

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _seed(self):
        """把库里那只股票的现状摆好：**落库的股本还是旧的错误值**。

        刻意留着旧的 ``total_shares``：这正是线上 22 只的样子，也正是最容易让
        模拟路径「顺手拿来用」从而把 bug 请回来的形状。模拟必须不认它。
        """
        research_db.upsert_stock(self.conn, _stock_row(
            self.CODE, name="安徽建工", industry="建筑装饰", total_score=43.18,
            valuation_json=json.dumps({
                "price": PRICE, "total_market_cap": MARKET_CAP,
                "total_shares": SHARES_FROM_EQUITY,          # ← 旧的错值
                "payout_ratio": 0.3037548797393772})))
        research_db.set_financial_cache(self.conn, self.CODE, _fin(), "2026-06-30")
        self.conn.execute(
            "INSERT INTO asset_semantic_snapshot (stock_code, date, metric_version,"
            " report_period, total_assets, source_document)"
            " VALUES (?, '2026-09-01 00:00:00', 'ASSET_SEMANTIC_ENGINE_V1.0',"
            " '2026-06-30', 3e10, '测试')", (self.CODE,))
        self.conn.commit()

    # ---------------------------------------------------------------- #
    @staticmethod
    def _offline(test):
        """把两个路径**共有的**外部依赖关掉：联网取 PB 历史与 peer/MARKET 上下文。

        两边都关，所以比较的仍然是同一套输入；不关会让这条测试依赖网络。
        """
        for name, value in (("ensure", lambda *a, **k: None),
                            ("_research_context",
                             lambda *a, **k: {"peer": None, "market": None,
                                              "unmapped_industries": []})):
            p = mock.patch.object(engine.valuation_history if name == "ensure"
                                  else engine, name, value)
            p.start()
            test.addCleanup(p.stop)

    def _analyze(self):
        with mock.patch.object(engine, "get_provider", FakeProvider):
            return engine.analyze(self.CODE)

    def _stored_row(self):
        return research_db.get_stock(self.conn, self.CODE)

    def _stored_valuation(self):
        return json.loads(self._stored_row()["valuation_json"])

    # ---------------------------------------------------------------- #
    def test_simulate_price_at_the_current_price_matches_the_library(self):
        """同一个价格上，模拟与库里的总股本 / 派息率 / 总分必须逐位相等。"""
        self._offline(self)
        self._analyze()
        stored, stored_v = self._stored_row(), self._stored_valuation()

        sim = engine.simulate_price(self.CODE, PRICE)
        self.assertIsNotNone(sim)

        self.assertAlmostEqual(sim["total_score"], stored["total_score"], places=9)
        self.assertAlmostEqual(sim["valuation"]["payout_ratio"],
                               stored_v["payout_ratio"], places=9)
        self.assertAlmostEqual(sim["valuation"]["total_shares"],
                               stored_v["total_shares"], places=6)

    def test_the_simulated_quote_ignores_the_legacy_share_count_in_the_row(self):
        """落库那份股本来源不明（可能是 BPS 反推的），**不许**当显式值拿回来用。"""
        self._offline(self)
        self._analyze()
        before = self._stored_valuation()["total_shares"]
        sim = engine.simulate_price(self.CODE, PRICE)
        self.assertEqual(sim["shares_source"], "market_cap")
        self.assertNotAlmostEqual(sim["valuation"]["total_shares"], SHARES_FROM_EQUITY, places=0)
        self.assertAlmostEqual(sim["valuation"]["total_shares"], SHARES_FROM_MARKET_CAP, places=4)
        self.assertAlmostEqual(before, SHARES_FROM_MARKET_CAP, places=4)

    def test_payout_ratio_no_longer_carries_the_2573_inflation(self):
        """§八 点名的那个数：600502 的派息率不许再有 2.573 倍偏差。"""
        self._offline(self)
        sim = engine.simulate_price(self.CODE, PRICE)
        payout = sim["valuation"]["payout_ratio"]
        expected = (DIVIDEND_PER_SHARE * SHARES_FROM_DIVIDEND) / NET_PROFIT
        self.assertAlmostEqual(payout, expected, places=9)

        wrong = (DIVIDEND_PER_SHARE * SHARES_FROM_EQUITY) / NET_PROFIT
        self.assertAlmostEqual(wrong / payout, LEGACY_PAYOUT_INFLATION, places=3)
        self.assertAlmostEqual(LEGACY_PAYOUT_INFLATION, 2.573, places=3)
        self.assertLess(abs(payout - expected), abs(payout - wrong))

    def test_the_simulation_still_writes_nothing(self):
        """模拟是纯本地计算：不许写快照、不许动主记录。"""
        self._offline(self)
        self._analyze()
        before = (self.conn.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0],
                  self.conn.execute("SELECT COUNT(*) FROM model_route_snapshot").fetchone()[0],
                  self.conn.execute("SELECT COUNT(*) FROM research_stocks").fetchone()[0],
                  self._stored_valuation())
        engine.simulate_price(self.CODE, PRICE * 1.5)
        after = (self.conn.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0],
                 self.conn.execute("SELECT COUNT(*) FROM model_route_snapshot").fetchone()[0],
                 self.conn.execute("SELECT COUNT(*) FROM research_stocks").fetchone()[0],
                 self._stored_valuation())
        self.assertEqual(before, after, "价格模拟动了库")


# --------------------------------------------------------------------------- #
# 不许白加快照行：同一个输入跑两遍只能有一行
# --------------------------------------------------------------------------- #
class TestNoSpuriousSnapshotRows(unittest.TestCase):
    """改了股本口径之后，快照去重必须照样生效。

    这是「加一个键到 ``m["current"]`` 就会白加 28 行」那条边界的守卫：跑两遍
    同一次分析，行数不许涨。分数没变的快照**不新增**是本轮 §一 的硬要求。
    """

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)
        self._patch = mock.patch.object(research_db, "DEFAULT_PATH", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._cleanup)
        research_db.upsert_stock(self.conn, _stock_row(
            "600502", name="安徽建工", industry="建筑装饰", total_score=43.18,
            valuation_json=json.dumps({"price": PRICE, "total_market_cap": MARKET_CAP})))
        research_db.set_financial_cache(self.conn, "600502", _fin(), "2026-06-30")
        self.conn.execute(
            "INSERT INTO asset_semantic_snapshot (stock_code, date, metric_version,"
            " report_period, total_assets, source_document)"
            " VALUES ('600502', '2026-09-01 00:00:00', 'ASSET_SEMANTIC_ENGINE_V1.0',"
            " '2026-06-30', 3e10, '测试')")
        self.conn.commit()

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_two_identical_analyses_add_no_second_snapshot(self):
        p1 = mock.patch.object(engine.valuation_history, "ensure", lambda *a, **k: None)
        p2 = mock.patch.object(engine, "_research_context",
                               lambda *a, **k: {"peer": None, "market": None,
                                                "unmapped_industries": []})
        with p1, p2, mock.patch.object(engine, "get_provider", FakeProvider):
            engine.analyze("600502")
            first = self.conn.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0]
            engine.analyze("600502")
            second = self.conn.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0]
        self.assertEqual(first, second, "同一个输入跑两遍白加了快照行")
