# -*- coding: utf-8 -*-
"""批 3 的**外部数据层**：peer 对照上下文、MARKET 读数层、以及它们接进 factor 层的那一跳。

这一批新引入的两件事——「同质同价」与「市场状态」——都依赖库外数据，
所以它们最危险的失效方式不是报错，而是**悄悄少一格**：少一格 factor 只是让
分母变一点，界面上看不出异常。这个文件就是钉住那些「少一格」的地方。

三条纪律贯穿本文件：

* **不联网**。需要缓存的地方就用真 schema 建内存库、把行插进去，让
  ``needs_refresh`` 判定为新鲜，于是取数路径根本不会被触发。
* **窗口不足就给 None**，不许缩短窗口算出个数来（用 30 根算的「60日涨跌幅」
  是报错口径，比 missing 糟得多）。
* **口径名与方向只有一处声明**，中途改名或加因子必须让这里的对账测试红。
"""
import sqlite3
import unittest
from datetime import date, timedelta
from unittest import mock

from research import dimensions as D
from research import engine, factors as F, market_series as MS, peer_groups as PG


TODAY = date.today().isoformat()
START = date(2025, 6, 1)


def _bars(n, close_at=None, amount_at=None, volume=1.0e6, turnover=1.0,
          high_pad=0.05):
    """``n`` 根人造日线（升序、日期互不相同）。默认平盘，方便断言「涨跌幅 == 0」。"""
    out = []
    for i in range(n):
        close = 10.0 if close_at is None else close_at(i)
        amount = 1.0e7 if amount_at is None else amount_at(i)
        day = (START + timedelta(days=i)).isoformat()
        out.append({
            "trade_date": day, "open": close, "close": close,
            "high": close * (1.0 + high_pad), "low": close * 0.98,
            "volume": volume, "amount": amount, "turnover": turnover,
            "source": MS.SOURCE_TENCENT,
        })
    return out


def _seeded_conn(code="002714", bars=None, float_cap=None, overhang=None):
    """真 schema + 直接插行 + 今天抓过的 meta → 取数路径判定新鲜，**不联网**。

    日线**与筹码压力都要**种今天的行：只种一边的话，另一边会被判成需要刷新，
    于是测试偷偷去联网（而且在没网的环境里变成一条时红时绿的测试）。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    MS.ensure_schema(conn)
    MS.ensure_overhang_schema(conn)
    bars = _bars(320) if bars is None else bars
    for b in bars:
        conn.execute(
            "INSERT INTO market_series (stock_code, trade_date, open, high, low,"
            " close, volume, amount, turnover, source, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (code, b["trade_date"], b["open"], b["high"], b["low"], b["close"],
             b["volume"], b["amount"], b["turnover"], b["source"], TODAY))
    conn.execute(
        "INSERT INTO market_series_meta (stock_code, fetched_at, policy_version,"
        " bar_count, first_date, last_date, source, basis, status, note,"
        " source_conflict, conflict_fields)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, TODAY, MS.POLICY_VERSION, len(bars), bars[0]["trade_date"],
         bars[-1]["trade_date"], MS.SOURCE_TENCENT, MS.PRICE_BASIS_QFQ, "ok",
         "测试用", 0, ""))
    row = {"stock_code": code, "fetched_at": TODAY, "status": "ok", "note": "",
           "policy_version": MS.OVERHANG_POLICY_VERSION,
           "float_market_cap": float_cap}
    row.update(overhang or {})
    MS._replace(conn, "market_overhang", code, row)
    conn.commit()
    return conn, bars


class TestIndexSymbolTrap(unittest.TestCase):
    """指数代码不能被 ``_market()`` 推市场。

    ``000300`` 是沪深300（上交所发布），按首位 ``0`` 会被推成 SZ，于是去请求
    ``sz000300``——**那不是报错**，是另一个东西的数据，序列看着完全正常。
    """

    def test_bare_index_code_would_be_misread(self):
        from research import providers as P
        self.assertEqual(P._market("000300"), "SZ")      # 这就是陷阱本身
        self.assertEqual(P._symbol_parts("000300")[1], "000300")

    def test_prefixed_index_code_wins(self):
        from research import providers as P
        self.assertEqual(P._symbol_parts("sh000300"), ("sh", "000300"))
        self.assertEqual(P._secid_for_kline("sh000300"), "1.000300")
        self.assertEqual(P._secid_for_kline("sz399006"), "0.399006")

    def test_stock_codes_are_untouched(self):
        from research import providers as P
        for code, want in (("600036", ("sh", "600036")),
                           ("002714", ("sz", "002714")),
                           ("300750", ("sz", "300750")),
                           ("830799", ("bj", "830799"))):
            self.assertEqual(P._symbol_parts(code), want, code)
        self.assertEqual(P._secid_for_kline("600036"), "1.600036")
        self.assertEqual(P._secid_for_kline("002714"), "0.002714")


class TestDerivationHelpers(unittest.TestCase):
    """读数层的两个纯函数。窗口不足必须给 None，**不许缩短窗口**。"""

    def test_pct_rank_handles_ties_and_range(self):
        self.assertEqual(MS._pct_rank([1, 2, 3, 4], 4), 0.875)
        self.assertEqual(MS._pct_rank([1, 2, 3, 4], 1), 0.125)
        self.assertEqual(MS._pct_rank([5, 5, 5], 5), 0.5)     # 全并列 → 中点
        self.assertEqual(MS._pct_rank([1, 2], 3), 1.0)
        self.assertIsNone(MS._pct_rank([], 1))
        self.assertIsNone(MS._pct_rank([1], None))

    def test_window_returns_refuses_a_short_window(self):
        bars = [{"close": 10.0 + i} for i in range(30)]
        self.assertIsNone(MS._window_returns(bars, 60),
                          "30 根算不出 60 日涨跌幅——缩窗口会得到一个错的数")
        self.assertIsNotNone(MS._window_returns(bars, 20))
        self.assertIsNone(MS._window_returns(bars, 0))

    def test_window_returns_computes_the_real_thing(self):
        bars = [{"close": 10.0}, {"close": 11.0}]
        self.assertAlmostEqual(MS._window_returns(bars, 1), 0.1)


class TestMarketContextOffline(unittest.TestCase):
    """用真 schema 插行，走完整读数路径，**不联网**。"""

    def setUp(self):
        self.conns = []

    def tearDown(self):
        for c in self.conns:
            c.close()

    def _conn(self, **kw):
        conn, bars = _seeded_conn(**kw)
        self.conns.append(conn)
        return conn, bars

    def test_readings_from_a_seeded_cache(self):
        # 单调上行 + 成交额单调上行（今天是最大值 → 分位接近 1）
        conn, _ = self._conn(bars=_bars(320, close_at=lambda i: 10.0 + 0.1 * i,
                                        amount_at=lambda i: 1.0e7 + 1.0e4 * i),
                             float_cap=1.0e10)
        ctx = MS.market_context(conn, "002714", 1.0e10)
        self.assertEqual(ctx["bar_count"], 320)
        self.assertEqual(ctx["source"], MS.SOURCE_TENCENT)
        self.assertEqual(ctx["status"], "ok")
        self.assertFalse(ctx["source_conflict"])
        # 单调上行 → 三档涨跌幅都是正的，且窗口越长涨得越多
        self.assertGreater(ctx["trend"]["trend_20d"], 0)
        self.assertGreater(ctx["trend"]["trend_120d"], ctx["trend"]["trend_60d"])
        # 今天是 20 日窗口里的最高成交额 → 分位是 (n-0.5)/n
        self.assertAlmostEqual(ctx["attention"]["amount_percentile_20d"],
                               19.5 / 20, places=6)
        self.assertAlmostEqual(ctx["attention"]["volume_ratio"], 1.0, places=6)
        # 成交额占流通市值 = 近 20 日均额 / 流通市值（均额随序列上行，不是首日的值）
        amounts = [1.0e7 + 1.0e4 * i for i in range(320)]
        self.assertAlmostEqual(ctx["liquidity"]["amount_to_float_cap_20d"],
                               (sum(amounts[-20:]) / 20) / 1.0e10, places=12)
        self.assertEqual(ctx["liquidity"]["free_float_market_cap"], 1.0e10)
        # 单调上行 → 今天就在最高点上（回到窗口内最高收盘价）
        self.assertGreater(ctx["trend"]["distance_from_120d_high"], -0.10)

    def test_short_history_writes_a_reason_and_leaves_the_gap_empty(self):
        conn, _ = self._conn(bars=_bars(30), float_cap=1.0e10)
        ctx = MS.market_context(conn, "002714", 1.0e10)
        self.assertEqual(ctx["bar_count"], 30)
        for fid, need in (("trend_60d", "60"), ("trend_120d", "120"),
                          ("distance_from_120d_high", "120")):
            self.assertNotIn(fid, ctx["trend"], fid)
            self.assertIn(need, ctx["reasons"][fid], fid)
        # 20 日窗口够，所以它必须有值——「短历史」不是把整块打死
        self.assertIn("trend_20d", ctx["trend"])
        self.assertNotIn("trend_20d", ctx["reasons"])

    def test_a_source_that_gives_no_amount_leaves_only_those_gaps(self):
        """新浪不给成交额与换手率——缺的只能是那两列，OHLC 那几格照常有值。"""
        bars = _bars(80)
        for b in bars:
            b["amount"] = None
            b["turnover"] = None
        conn, _ = self._conn(bars=bars, float_cap=1.0e10)
        ctx = MS.market_context(conn, "002714", 1.0e10)
        self.assertIn("trend_60d", ctx["trend"])
        self.assertNotIn("amount_percentile_20d", ctx["attention"])
        self.assertNotIn("turnover_percentile_20d", ctx["attention"])
        self.assertIn("成交额", ctx["reasons"]["amount_percentile_20d"])
        self.assertIn("换手率", ctx["reasons"]["turnover_percentile_20d"])

    def test_missing_float_cap_names_the_missing_denominator(self):
        conn, _ = self._conn(float_cap=None)
        ctx = MS.market_context(conn, "002714", None)
        self.assertIsNone(ctx["liquidity"]["free_float_market_cap"])
        self.assertIn("分母", ctx["reasons"]["amount_to_float_cap_20d"])
        self.assertIn("分母", ctx["reasons"]["unlock_ratio_12m"])

    def test_overhang_readings_land_in_the_right_group(self):
        conn, _ = self._conn(float_cap=3.114e10, overhang={
            "unlock_ratio_12m": 0.1021, "unlock_count": 1,
            "holder_num_change": -2.62, "holder_num_end_date": "2026-08-10",
            "reduction_count_12m": 1, "unlock_next_date": "2027-03-01"})
        ctx = MS.market_context(conn, "002714", 3.114e10)
        self.assertAlmostEqual(ctx["overhang"]["unlock_ratio_12m"], 0.1021)
        self.assertAlmostEqual(ctx["overhang"]["holder_num_change"], -2.62)
        self.assertEqual(ctx["overhang"]["holder_reduction_count_12m"], 1)
        self.assertEqual(ctx["unlock_next_date"], "2027-03-01")

    def test_zero_unlock_is_a_reading_not_a_gap(self):
        """接口明确说「未来一年没有解禁」= 零压力，不是「不知道」。"""
        conn, _ = self._conn(float_cap=1.0e10, overhang={
            "unlock_ratio_12m": 0.0, "unlock_count": 0,
            "holder_num_change": -2.0, "reduction_count_12m": 0})
        ctx = MS.market_context(conn, "002714", 1.0e10)
        self.assertEqual(ctx["overhang"]["unlock_ratio_12m"], 0.0)
        self.assertNotIn("unlock_ratio_12m", ctx["reasons"])

    def test_no_connection_is_an_error_not_a_crash(self):
        ctx = MS.market_context(None, "002714")
        self.assertEqual(ctx["status"], "error")
        self.assertTrue(ctx["reasons"])


class TestOverhangCacheInvalidation(unittest.TestCase):
    """分母变了，缓存的解禁占比就是错的（或永远空着）。"""

    def _meta(self, cap, status="ok", fetched=TODAY):
        return {"policy_version": MS.OVERHANG_POLICY_VERSION, "fetched_at": fetched,
                "status": status, "float_market_cap": cap,
                "holder_num_end_date": TODAY}

    def test_same_denominator_stays_fresh(self):
        self.assertFalse(MS.overhang_needs_refresh(self._meta(1.0e10), TODAY, 1.0e10))

    def test_changed_denominator_forces_a_refresh(self):
        self.assertTrue(MS.overhang_needs_refresh(self._meta(1.0e10), TODAY, 1.1e10))

    def test_small_drift_does_not_thrash_the_cache(self):
        """市值天天在动，精确比会让季报级慢变量天天重抓。"""
        self.assertFalse(MS.overhang_needs_refresh(self._meta(1.0e10), TODAY, 1.005e10))

    def test_a_row_that_never_had_a_denominator_is_refreshed(self):
        self.assertTrue(MS.overhang_needs_refresh(self._meta(None), TODAY, 1.0e10))

    def test_a_failing_cache_is_not_retried_all_day(self):
        """取数一直失败时，分母永远对不上——不能因此每次分析都去撞一遍源。"""
        m = self._meta(None, status="error")
        self.assertFalse(MS.overhang_needs_refresh(m, TODAY, 1.0e10))
        self.assertTrue(MS.overhang_needs_refresh(m, "2026-01-02", 1.0e10))


def _tire_view(**kw):
    group = PG.PeerGroup("TIRE", "轮胎", PG.BASIS_EXPLICIT,
                         (("601163", "三角轮胎"), ("601058", "赛轮轮胎")),
                         PG.SOURCE_CURATED, note="测试")
    rows = [
        {"stock_code": "601163", "name": "三角轮胎", "pe": 11.27, "pb": 0.70},
        {"stock_code": "601058", "name": "赛轮轮胎", "pe": 11.58, "pb": 1.27},
        {"stock_code": "002984", "name": "森麒麟", "pe": 12.0, "pb": 1.30},
        {"stock_code": "601966", "name": "玲珑轮胎", "pe": 13.0, "pb": 1.40},
        {"stock_code": "000589", "name": "贵州轮胎", "pe": 14.0, "pb": 1.50},
        {"stock_code": "002593", "name": "日上集团", "pe": 15.0, "pb": 1.60},
    ]
    fund = [
        {"stock_code": "601163", "fcf_yield": 0.079, "roe": 0.08},
        {"stock_code": "601058", "fcf_yield": -0.022, "roe": 0.06},
        {"stock_code": "002984", "fcf_yield": 0.02, "roe": 0.09},
        {"stock_code": "601966", "fcf_yield": 0.01, "roe": 0.05},
        {"stock_code": "000589", "fcf_yield": 0.03, "roe": 0.07},
        {"stock_code": "002593", "fcf_yield": 0.04, "roe": 0.04},
    ]
    return PG.PeerView(peer_group=group, rows=rows, own_code="601163",
                       as_of="2026-09-26", fundamental_rows=fund, **kw)


class TestPeerContextContract(unittest.TestCase):
    """``PeerView.context()`` 的键与取值形状是 ``factors._peer_result`` 的读法。

    两边任一方改名都会让某个 factor **静默**变 missing——所以这里逐字段对账。
    """

    def test_every_key_factors_reads_is_present(self):
        ctx = _tire_view().context()
        for key in ("available", "peer_group", "display_name", "member_count",
                    "basis", "as_of", "reason", "missing_names",
                    "financial_excluded", "valuation", "fundamental",
                    "quality_adjusted"):
            self.assertIn(key, ctx, key)
        # factors 读 valuation["pe"]/["pb"] 与 fundamental["fcf_yield"]
        for field in ("pe", "pb"):
            self.assertIn(field, ctx["valuation"], field)
        self.assertIn("fcf_yield", ctx["fundamental"])

    def test_the_values_factors_multiplies_by_100_are_percentiles(self):
        """``_peer_result`` 拿 ``peer_percentile * 100`` 当分数，所以它必须是 0~1。"""
        ctx = _tire_view().context()
        for field in ("pe", "pb"):
            pct = ctx["valuation"][field]["peer_percentile"]
            self.assertIsNotNone(pct, field)
            self.assertGreaterEqual(pct, 0.0, field)
            self.assertLessEqual(pct, 1.0, field)
        # 三角轮胎 PB 0.70 是 6 家里最低的，而 PB 越低越好 → 分位 1.0
        self.assertEqual(ctx["valuation"]["pb"]["peer_percentile"], 1.0)

    def test_valuation_relative_shape(self):
        pb = _tire_view().context()["valuation"]["pb"]
        self.assertEqual(pb["own"], 0.70)
        self.assertEqual(pb["confidence"], PG.CONF_HIGH)     # 6 家样本
        self.assertIn("peer_count", pb)
        self.assertIn("excluded", pb)
        # 中位数取**剔掉自己**的 5 家（1.27/1.30/1.40/1.50/1.60）→ 1.40。
        # 把自己算进中位数是这一类实现最容易犯的错，所以这个数写死。
        self.assertEqual(pb["peer_count"], 5)
        self.assertAlmostEqual(pb["peer_median"], 1.40)

    def test_quality_adjusted_confidence_is_a_number_not_a_word(self):
        """factor 层的 confidence 要和 ``LOW_CONFIDENCE_THRESHOLD`` 比大小。

        两个词表混用会直接 TypeError（不是算错），所以这条掐住它。
        """
        qa = _tire_view().context()["quality_adjusted"]
        self.assertIsInstance(qa["confidence"], float)
        self.assertGreaterEqual(qa["confidence"], 0.0)
        self.assertLessEqual(qa["confidence"], 1.0)
        self.assertIsNotNone(qa["valuation_gap"])

    def test_a_thin_sample_is_low_confidence_not_a_crash(self):
        """样本不足 → 值可以没有，但 ``reason`` 必须在，不能静默。"""
        rows = [{"stock_code": "601163", "name": "三角轮胎", "pe": 11.0, "pb": 0.70},
                {"stock_code": "601058", "name": "赛轮轮胎", "pe": 11.5, "pb": 1.27}]
        qa = PG.PeerView(peer_group=_tire_view().peer_group, rows=rows,
                         own_code="601163").context()["quality_adjusted"]
        self.assertIsNone(qa["valuation_gap"])
        self.assertTrue(qa["reason"])

    def test_financial_group_excludes_only_pe_and_fcf(self):
        group = PG.PeerGroup("BANK", "商业银行", PG.BASIS_EXPLICIT,
                             (("600036", "招商银行"),), PG.SOURCE_CURATED)
        v = PG.PeerView(peer_group=group, rows=[
            {"stock_code": "600036", "name": "招商银行", "pe": 6.76, "pb": 0.90}],
            own_code="600036")
        exc = v.context()["financial_excluded"]
        self.assertIn("peer_pe_relative", exc)
        self.assertIn("peer_fcf_yield_relative", exc)
        self.assertNotIn("peer_pb_relative", exc)
        self.assertNotIn("peer_quality_adjusted_valuation", exc)

    def test_non_financial_group_excludes_nothing(self):
        self.assertEqual(_tire_view().context()["financial_excluded"], {})

    def test_a_dead_group_still_returns_a_shape_factors_can_read(self):
        """组落空 → 整组 missing，但**载荷形状必须还在**（不能是 None）。"""
        ctx = PG.PeerView(reason="行业未建组").context()
        self.assertFalse(ctx["available"])
        self.assertIsNone(ctx["peer_group"])
        self.assertEqual(ctx["financial_excluded"], {})
        self.assertIn("pe", ctx["valuation"])
        self.assertIn("fcf_yield", ctx["fundamental"])
        self.assertIsNone(ctx["quality_adjusted"]["valuation_gap"])
        self.assertTrue(ctx["quality_adjusted"]["reason"])


class TestPeerFactorIdDrift(unittest.TestCase):
    """``PEER_FACTOR_IDS`` 与 factor 层的 ``peer_`` 因子必须逐字相等。

    漏一个的后果是那个因子对金融业**照常打分**——不报错、只是口径错了。
    """

    def test_the_two_lists_agree(self):
        actual = tuple(s.factor_id for s in F.FACTORS
                       if s.factor_id.startswith("peer_"))
        self.assertEqual(sorted(PG.PEER_FACTOR_IDS), sorted(actual))

    def test_every_peer_factor_lives_in_relative_value(self):
        for spec in F.FACTORS:
            if spec.factor_id.startswith("peer_"):
                self.assertEqual(spec.factor_group, F.GROUP_RELATIVE_VALUE,
                                 spec.factor_id)

    def test_financial_exclusion_covers_every_peer_factor(self):
        """每个 peer 因子都必须**明确**要么允许要么停用，没有中间地带。"""
        allowed = PG.FINANCIAL_ALLOWED_FACTORS
        for fid in PG.PEER_FACTOR_IDS:
            if fid in allowed:
                self.assertIsNone(PG.excluded_from_financial(fid), fid)
            else:
                self.assertTrue(PG.excluded_from_financial(fid), fid)


class TestKlineThresholdsLiveInRulesV1(unittest.TestCase):
    """跨源一致性检查的三个阈值**只有一份**，住在 ``RULES_V1["market"]``。

    曾经 ``market_series`` 自己抄了一份 ``KLINE_SOURCE_TOLERANCE`` 之类。后果不是
    「读起来重复」，而是改 ``RULES_V1`` 会让 ``/api/meta`` 报 ``rule_source_dirty``
    ——**看起来规则变了**——却一行行为都不改。脏标记与真实行为脱钩之后，
    「这次改动到底生效没有」就没有任何可判定的答案了。

    所以这里钉两层：模块里不许再有第二份常量，以及**改 ``RULES_V1`` 必须真的
    改变判定**（不是「读了就算」）。
    """

    def test_the_three_keys_exist_in_rules_v1(self):
        from research import rules
        market = rules.RULES_V1["market"]
        for key in ("kline_source_tolerance", "kline_return_tolerance",
                    "kline_conflict_ratio"):
            self.assertIn(key, market, key)
            self.assertGreater(market[key], 0, key)

    def test_market_series_holds_no_second_copy(self):
        for name in ("KLINE_SOURCE_TOLERANCE", "KLINE_RETURN_TOLERANCE",
                     "KLINE_CONFLICT_RATIO"):
            self.assertFalse(hasattr(MS, name),
                             f"{name} 是 RULES_V1 的死副本，改规则不会改行为")

    def test_thresholds_reads_the_rules(self):
        from research import rules
        market = rules.RULES_V1["market"]
        self.assertEqual(MS.thresholds(),
                         (market["kline_source_tolerance"],
                          market["kline_return_tolerance"],
                          market["kline_conflict_ratio"]))
        with mock.patch.dict(rules.RULES_V1, {"market": dict(market, kline_conflict_ratio=0.9)}):
            self.assertEqual(MS.thresholds()[2], 0.9)

    def _pair(self, differing_days=5, overlap=20):
        """重叠 ``overlap`` 天，其中 ``differing_days`` 天的成交量差 10%。"""
        primary, secondary = [], []
        for i in range(overlap):
            day = (START + timedelta(days=i)).isoformat()
            close = 10.0
            row = {"trade_date": day, "open": close, "high": close * 1.05,
                   "low": close * 0.98, "close": close,
                   "volume": 1.0e6, "amount": 1.0e7, "turnover": 1.0}
            primary.append(dict(row))
            other = dict(row)
            if i < differing_days:
                other["volume"] = 1.1e6                     # 差 10% ≫ 0.5% 容差
            secondary.append(other)
        return primary, secondary

    def test_the_default_ratio_flags_a_quarter_of_days_as_conflict(self):
        primary, secondary = self._pair()
        out = MS.cross_check(primary, secondary)
        self.assertEqual(out["overlap_days"], 20)
        self.assertIn("volume", out["conflict_fields"])
        self.assertTrue(out["conflict"])                    # 5/20 = 25% > 5%

    def test_raising_the_ratio_in_rules_v1_flips_the_verdict(self):
        """**同一样本**，只把 ``RULES_V1`` 里的占比上限调高 → 判定必须翻转。

        这条是 §四 的核心要求：阈值住在规则里，就要能靠改规则改行为。
        0.25 的差异占比在 0.05 的上限下是冲突，在 0.50 的上限下不是。
        """
        from research import rules
        primary, secondary = self._pair()
        self.assertTrue(MS.cross_check(primary, secondary)["conflict"])
        market = dict(rules.RULES_V1["market"], kline_conflict_ratio=0.50)
        with mock.patch.dict(rules.RULES_V1, {"market": market}):
            out = MS.cross_check(primary, secondary)
        self.assertFalse(out["conflict"])
        # 差异本身照旧被记下来——翻转的是「判定」，不是「看不见差异」
        self.assertEqual(out["conflict_fields"], [])
        self.assertEqual(len(out["differing_dates"]["volume"]), 5)

    def test_raising_the_level_tolerance_in_rules_v1_erases_the_difference(self):
        """容差同理：调到 50% 之后那 10% 的差异根本不算差异。"""
        from research import rules
        primary, secondary = self._pair()
        self.assertTrue(MS.cross_check(primary, secondary)["conflict_fields"])
        market = dict(rules.RULES_V1["market"], kline_source_tolerance=0.50)
        with mock.patch.dict(rules.RULES_V1, {"market": market}):
            out = MS.cross_check(primary, secondary)
        self.assertEqual(out["conflict_fields"], [])
        self.assertEqual(out["differing_dates"], {})
        self.assertFalse(out["conflict"])


class TestPeerSampleStatusSemantics(unittest.TestCase):
    """§六：PE 算不出分位时的三种原因，**语义不同、分母也不同**。

    ``_assemble`` 对 ``not_applicable`` 的 coverage 惩罚是 0（``setdefault 1.0``），
    对 ``missing_data`` 给 0.0。所以「整组都在亏」被判成「数据没抓到」的后果不只是
    报告里写错一句话——它会**把这一格记进分母**，等于因为「大家都亏」而扣这只股票
    的分。反过来把「没取到数」当 ``not_applicable`` 又会白白免掉 coverage 惩罚。
    """

    def _rows(self, own_pe, peer_pes):
        rows = [{"stock_code": "601163", "name": "三角轮胎", "pe": own_pe, "pb": 0.70}]
        for i, pe in enumerate(peer_pes):
            rows.append({"stock_code": f"00000{i}", "name": f"同行{i}", "pe": pe, "pb": 1.0})
        return rows

    def _view(self, own_pe, peer_pes):
        group = PG.PeerGroup("TIRE", "轮胎", PG.BASIS_EXPLICIT,
                             (("601163", "三角轮胎"),), PG.SOURCE_CURATED)
        return PG.PeerView(peer_group=group, rows=self._rows(own_pe, peer_pes),
                           own_code="601163")

    def _status(self, own_pe, peer_pes):
        return self._view(own_pe, peer_pes).valuation_relative("pe")["sample_status"]

    # ---------------------------------------------------------------- #
    def test_the_company_itself_is_losing(self):
        """自身亏损 → ``own_not_applicable``：口径此刻对它没有定义。"""
        self.assertEqual(self._status(-8.0, [11.5, 12.0, 13.0, 14.0]),
                         PG.SAMPLE_OWN_NOT_APPLICABLE)

    def test_the_whole_group_is_losing(self):
        """整组亏损 → ``all_peers_not_applicable``。**「大家都亏」不是「大家都便宜」。**"""
        self.assertEqual(self._status(10.0, [-1.0, -2.0, -3.0]),
                         PG.SAMPLE_ALL_PEERS_NOT_APPLICABLE)

    def test_a_mixed_group_with_too_few_profitable_peers(self):
        """只有 2 家正利润（< 3 家门槛）→ ``insufficient_peer_sample``。

        这里**不是**整组亏损（组里存在正利润），所以不许报 not_applicable——
        这一格是「有定义但样本不够」，要记 missing。
        """
        self.assertEqual(self._status(10.0, [11.5, 12.0, -3.0]),
                         PG.SAMPLE_INSUFFICIENT)

    def test_missing_numbers_are_not_nonpositive(self):
        """取不到数 → ``own_missing`` / 样本不足，**不许**被算成「亏损」。"""
        self.assertEqual(self._status(None, [11.5, 12.0, 13.0]),
                         PG.SAMPLE_OWN_MISSING)
        self.assertEqual(self._status(10.0, [None, None, None]),
                         PG.SAMPLE_INSUFFICIENT)

    def test_the_three_reasons_are_three_sentences(self):
        """三种原因三句话。共用一句等于没区分——报告里看不出该去补数还是该换口径。"""
        pe_own, pe_group, pe_thin = (
            self._view(-8.0, [11.5, 12.0, 13.0]).valuation_relative("pe")["reason"],
            self._view(10.0, [-1.0, -2.0, -3.0]).valuation_relative("pe")["reason"],
            self._view(10.0, [11.5, 12.0, -3.0]).valuation_relative("pe")["reason"])
        self.assertEqual(len({pe_own, pe_group, pe_thin}), 3)
        self.assertIn("自身", pe_own)
        self.assertIn("全部", pe_group)

    def test_pb_keeps_the_old_semantics(self):
        """PB ≤ 0（净资产为负）**不适用 PE 那套「口径不适用」**，本批不动它的语义。"""
        rows = [{"stock_code": "601163", "name": "三角轮胎", "pe": 10.0, "pb": -0.5},
                {"stock_code": "000001", "name": "同行0", "pe": 11.0, "pb": 1.0},
                {"stock_code": "000002", "name": "同行1", "pe": 12.0, "pb": 1.2},
                {"stock_code": "000003", "name": "同行2", "pe": 13.0, "pb": 1.3}]
        group = PG.PeerGroup("TIRE", "轮胎", PG.BASIS_EXPLICIT,
                             (("601163", "三角轮胎"),), PG.SOURCE_CURATED)
        out = PG.PeerView(peer_group=group, rows=rows, own_code="601163") \
            .valuation_relative("pb")
        self.assertEqual(out["sample_status"], PG.SAMPLE_OWN_UNUSABLE)
        self.assertNotEqual(out["sample_status"], PG.SAMPLE_OWN_NOT_APPLICABLE)
        # 严格口径只登记了 PE 一个字段，PB 不在其中
        self.assertEqual(PG.NONPOSITIVE_MEANS_NOT_APPLICABLE, frozenset({"pe"}))

    # ---------------------------------------------------------------- #
    # 状态码 → 计分口径的那一跳
    # ---------------------------------------------------------------- #
    def test_factors_turns_a_loss_into_not_applicable_without_a_coverage_penalty(self):
        spec = next(s for s in F.FACTORS if s.factor_id == "peer_pe_relative")
        for own_pe, peer_pes in ((-8.0, [11.5, 12.0, 13.0]),
                                 (10.0, [-1.0, -2.0, -3.0])):
            peer = self._view(own_pe, peer_pes).context()
            res = F._peer_result(spec, peer)
            self.assertEqual(res.status, "not_applicable", (own_pe, peer_pes))
            self.assertEqual(res.coverage, 1.0, "亏损被记进了分母")
            self.assertIsNone(res.score)
            self.assertTrue(res.reason)
            # 原因必须一路上浮到载荷里，否则报告只能看见一个 not_applicable
            self.assertIn("sample_status", res.components[0])

    def test_factors_turns_a_thin_sample_into_missing_data(self):
        spec = next(s for s in F.FACTORS if s.factor_id == "peer_pe_relative")
        peer = self._view(10.0, [11.5, 12.0, -3.0]).context()
        res = F._peer_result(spec, peer)
        self.assertEqual(res.status, "missing_data")
        self.assertEqual(res.coverage, 0.0)
        self.assertTrue(res.reason)

    def test_the_two_statuses_are_not_the_same_factor_result(self):
        """同一个 factor、同一个「算不出数」，两种语义必须产出**不同**的载荷。"""
        spec = next(s for s in F.FACTORS if s.factor_id == "peer_pe_relative")
        loss = F._peer_result(spec, self._view(-8.0, [11.5, 12.0, 13.0]).context())
        thin = F._peer_result(spec, self._view(10.0, [11.5, 12.0, -3.0]).context())
        self.assertNotEqual(loss.status, thin.status)
        self.assertNotEqual(loss.coverage, thin.coverage)
        self.assertNotEqual(loss.reason, thin.reason)


class TestPeerPeDistributionIsItsOwnSemantics(unittest.TestCase):
    """BATCH 4.1 §一–§五：**同行自己的 PE 分布**与「本公司在组内的位置」是两条语义。

    A（``valuation_relative``）：本公司 PE 在同组里处于什么位置；
    B（``peer_pe_distribution``）：同行当前可审计的 PE 分布是多少。

    两者**不能共用一个 early return**：本公司亏损时 A 该报 not_applicable，
    但那不代表同行没有 PE——三档锚里的 Base 与 Bull 只用得到 B。
    """

    def _rows(self, own_pe, peer_pes):
        rows = [{"stock_code": "601163", "name": "三角轮胎", "pe": own_pe, "pb": 0.70}]
        for i, pe in enumerate(peer_pes):
            rows.append({"stock_code": f"00000{i}", "name": f"同行{i}",
                         "pe": pe, "pb": 1.0})
        return rows

    def _view(self, own_pe, peer_pes):
        group = PG.PeerGroup("TIRE", "轮胎", PG.BASIS_EXPLICIT,
                             (("601163", "三角轮胎"),), PG.SOURCE_CURATED)
        return PG.PeerView(peer_group=group, rows=self._rows(own_pe, peer_pes),
                           own_code="601163")

    # ---------------------------------------------------------------- #
    def test_a_loss_maker_still_has_the_peer_distribution(self):
        """自身亏损 & 同行 ≥ 3 家正 PE：A 不适用，**B 照常可用**。"""
        view = self._view(-8.0, [11.5, 12.0, 13.0, 14.0])
        rel = view.valuation_relative("pe")
        self.assertEqual(rel["sample_status"], PG.SAMPLE_OWN_NOT_APPLICABLE)
        self.assertEqual(rel["reason_code"], PG.OWN_PE_NOT_MEANINGFUL)
        self.assertIsNone(rel["peer_percentile"])
        dist = view.peer_pe_distribution("pe")
        self.assertEqual(dist["sample_status"], PG.SAMPLE_OK)
        self.assertEqual(dist["peer_positive_pe_count"], 4)
        self.assertIsNotNone(dist["peer_pe_median"])
        self.assertIsNotNone(dist["peer_pe_p75"])
        self.assertEqual(dist["peer_pe_confidence"], PG.CONF_LOW)   # 4 家 → low

    def test_the_relative_factor_stays_not_applicable_for_a_loss_maker(self):
        """**同行样本存在不等于**这一格复活——``peer_pe_relative`` 仍不适用。"""
        spec = next(s for s in F.FACTORS if s.factor_id == "peer_pe_relative")
        res = F._peer_result(
            spec, self._view(-8.0, [11.5, 12.0, 13.0, 14.0]).context())
        self.assertEqual(res.status, "not_applicable")
        self.assertEqual(res.coverage, 1.0)
        self.assertIsNone(res.score)

    def test_fewer_than_three_positive_peers_is_insufficient(self):
        """§三：< 3 家 → ``insufficient_peer_sample``，分位一律不给。

        那 2 家的 PE **照实发**（真实数据，报告里看得见），只是不拿它们当
        「同行倍数」——2 个数的分位数没有分辨力。
        """
        dist = self._view(-8.0, [11.5, 12.0, -3.0]).peer_pe_distribution("pe")
        self.assertEqual(dist["sample_status"], PG.SAMPLE_INSUFFICIENT)
        self.assertEqual(dist["reason_code"], PG.INSUFFICIENT_SAMPLE)
        self.assertEqual(dist["peer_positive_pe_count"], 2)
        self.assertEqual(dist["peer_pe_values"], [11.5, 12.0])
        self.assertIsNone(dist["peer_pe_median"])
        self.assertIsNone(dist["peer_pe_p25"])
        self.assertIsNone(dist["peer_pe_p75"])
        self.assertEqual(dist["peer_pe_confidence"], PG.CONF_NONE)
        self.assertIn("不退回全市场基准", dist["reason"])

    def test_confidence_bands_are_five_and_three(self):
        """≥5 家 HIGH、3~4 家 LOW —— 与 ``confidence_of`` 同一套门槛。"""
        self.assertEqual(
            self._view(10.0, [11.0, 12.0, 13.0, 14.0, 15.0])
            .peer_pe_distribution()["peer_pe_confidence"], PG.CONF_HIGH)
        for pes in ([11.0, 12.0, 13.0], [11.0, 12.0, 13.0, 14.0]):
            self.assertEqual(
                self._view(10.0, pes).peer_pe_distribution()["peer_pe_confidence"],
                PG.CONF_LOW, pes)

    def test_an_all_losing_group_reports_why_without_inventing_a_multiple(self):
        """整组亏损：B 也没有值，且**必须说清是被剔掉的**，不是「没取到数」。"""
        dist = self._view(-8.0, [-1.0, -2.0, -3.0]).peer_pe_distribution("pe")
        self.assertEqual(dist["peer_positive_pe_count"], 0)
        self.assertIsNone(dist["peer_pe_median"])
        self.assertEqual(dist["excluded_nonpositive"], ["000000", "000001", "000002"])
        self.assertEqual(dist["excluded_missing"], [])

    def test_the_context_carries_both_semantics_side_by_side(self):
        ctx = self._view(-8.0, [11.5, 12.0, 13.0, 14.0]).context()
        self.assertIn("pe", ctx["valuation"])
        self.assertIn(PG.PE_DISTRIBUTION_KEY, ctx)
        # 两条语义各有各的状态：一条 not_applicable，另一条 ok。
        self.assertEqual(ctx["valuation"]["pe"]["sample_status"],
                         PG.SAMPLE_OWN_NOT_APPLICABLE)
        self.assertEqual(ctx[PG.PE_DISTRIBUTION_KEY]["sample_status"], PG.SAMPLE_OK)


class TestMarketResultStatusRules(unittest.TestCase):
    """factor 层对 MARKET 读数的三种处理：属性 / missing（带窗口数字）/ ok。"""

    def _market(self, **kw):
        base = {"trend": {}, "attention": {}, "liquidity": {}, "overhang": {},
                "reasons": {}, "bar_count": 320, "source": MS.SOURCE_TENCENT,
                "basis": MS.PRICE_BASIS_QFQ, "source_conflict": False,
                "conflict_fields": []}
        base.update(kw)
        return base

    def _spec(self, fid):
        return F.COMPUTED_FACTOR_INDEX[fid]

    def test_free_float_market_cap_is_always_display_only(self):
        """它有值也不计分：属性 factor 进了分母会让「partial」永远为真。"""
        for cap in (None, 1.0e10):
            r = F._market_result(self._spec("free_float_market_cap"),
                                 self._market(liquidity={"free_float_market_cap": cap}))
            self.assertEqual(r.status, F.STATUS_DISPLAY_ONLY, cap)
            self.assertFalse(r.eligible)

    def test_a_missing_reading_carries_the_window_numbers(self):
        r = F._market_result(self._spec("trend_60d"),
                             self._market(bar_count=30,
                                          reasons={"trend_60d": "日线只有 30 根"}))
        self.assertEqual(r.status, "missing_data")
        comp = r.components[0]
        self.assertEqual(comp["need_bars"], 60)
        self.assertEqual(comp["have_bars"], 30)
        self.assertFalse(comp["window_filled"])
        self.assertIn("30", r.reason)

    def test_a_present_reading_is_ok_with_full_coverage(self):
        r = F._market_result(self._spec("trend_60d"),
                             self._market(trend={"trend_60d": 0.25}))
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.coverage, 1.0)
        self.assertEqual(r.confidence, 1.0)
        self.assertTrue(r.components[0]["window_filled"])

    def test_source_conflict_halves_confidence(self):
        r = F._market_result(self._spec("trend_60d"),
                             self._market(trend={"trend_60d": 0.25},
                                          source_conflict=True,
                                          conflict_fields=["close"]))
        self.assertEqual(r.confidence, 0.5)
        self.assertTrue(r.components[0]["source_conflict"])

    def test_margin_balance_is_declared_missing_not_invented(self):
        """两融在 ``_computed_result`` 里就被判 missing（连曲线都不去查）。

        接口没找到的时候，正确做法是**说出接口没找到**，不是退回「没有上下文」
        这种看不出所以然的话。
        """
        r = F._computed_result(self._spec("margin_balance_ratio"), {})
        self.assertEqual(r.status, "missing_data")
        self.assertIn("融资余额", r.reason)

    def test_a_curve_is_never_fabricated_without_config(self):
        """RULES_V1 里没有这一格的曲线配置时，不许拿默认曲线凑一个分出来。"""
        r = F._market_result(self._spec("trend_60d"), self._market())
        self.assertEqual(r.status, "missing_data")


class TestMarketStatusExcludesCharacteristics(unittest.TestCase):
    """属性 factor 不进 MARKET 的分母，否则每只股票都恒为 partial。"""

    def test_characteristics_are_reported_separately(self):
        payload = F.to_payload(F.evaluate({}, {}, final={}), {})
        meta = payload["_meta"]
        self.assertIn("free_float_market_cap", meta["market_characteristic_factors"])
        self.assertNotIn("free_float_market_cap", meta["market_missing_factors"])
        expected = [s for s in F.COMPUTED_FACTOR_SPECS
                    if s.factor_group.startswith("market")
                    and s.factor_role != F.ROLE_CHARACTERISTIC]
        self.assertEqual(meta["market_factor_count"], len(expected))

    def test_with_no_context_the_whole_block_is_missing_not_zero(self):
        meta = F.to_payload(F.evaluate({}, {}, final={}), {})["_meta"]
        self.assertTrue(meta["market_missing"])
        self.assertFalse(meta["market_partial"])


class TestResearchContextNeverRaises(unittest.TestCase):
    """``_research_context`` 是接线的唯一入口，**绝不把主流程带下去**。"""

    def test_no_connection_degrades_to_missing(self):
        ctx = engine._research_context(None, "002714", "养殖业")
        self.assertIsInstance(ctx, dict)
        for key in ("peer", "market", "unmapped_industries"):
            self.assertIn(key, ctx)
        self.assertFalse(ctx["peer"]["available"])
        self.assertEqual(ctx["market"]["status"], "error")

    def test_the_context_is_optional_for_run_analysis(self):
        """没有 context 时 ``_run_analysis`` 必须照常出分（相对价值/MARKET 报 missing）。

        这是 ``simulate_price`` 与测试直接调它的前提，也是「模拟页不联网」的实现方式。
        """
        import inspect
        sig = inspect.signature(engine._run_analysis)
        self.assertIsNone(sig.parameters["context"].default)

    def test_industry_survey_matches_names_to_groups(self):
        survey = engine.industry_survey(None)
        self.assertEqual(survey, {"industries": [], "unmapped": []})


class TestPeerGroupResolutionIsAuditable(unittest.TestCase):
    """裁定 6 的机器形式：组落空是**结论**，绝不允许降级到全市场基准。"""

    def test_a_forbidden_basis_cannot_even_be_constructed(self):
        for bad in ("market_all", "market", "all_a", "whole_market"):
            self.assertIn(bad, PG.FORBIDDEN_BASIS)
            with self.assertRaises(ValueError):
                PG.PeerGroup("X", "全市场", bad, (), PG.SOURCE_CURATED)

    def test_every_shipped_group_uses_a_legal_basis(self):
        for g in PG.PEER_GROUP_DEFS:
            self.assertIn(g.basis, PG.BASIS_VALUES)
            self.assertNotIn(g.basis, PG.FORBIDDEN_BASIS)

    def test_tire_is_its_own_group_not_generic_auto_parts(self):
        """§5.1 点名的拆细：轮胎不共用「汽车零部件」一个池。"""
        g, reason = PG.resolve_or_unknown("601163", "汽车零部件")
        self.assertIsNotNone(g, reason)
        self.assertEqual(g.peer_group_id, "TIRE")

    def test_pig_companies_do_not_share_the_agriculture_pool(self):
        pure, _ = PG.resolve_or_unknown("002714", "养殖业")
        mixed, _ = PG.resolve_or_unknown("000876", "饲料")
        self.assertIsNotNone(pure)
        self.assertIsNotNone(mixed)
        self.assertNotEqual(pure.peer_group_id, mixed.peer_group_id)
        self.assertEqual(pure.basis, PG.BASIS_PIG)

    def test_the_bank_group_is_marked_financial(self):
        g, _ = PG.resolve_or_unknown("600036", "银行Ⅱ")
        self.assertIsNotNone(g)
        self.assertIn(g.peer_group_id, PG.FINANCIAL_PEER_GROUPS)

    def test_an_unknown_industry_yields_none_with_a_reason(self):
        g, reason = PG.resolve_or_unknown("999999", "不存在的行业")
        self.assertIsNone(g)
        self.assertTrue(reason)


class TestFrameWeightTableIsTheOnlyOne(unittest.TestCase):
    """四维权重按 frame 配（用户裁定），且每一行都要能自洽。"""

    def test_every_frame_has_four_non_negative_weights_summing_to_one(self):
        self.assertEqual(D.invalid_dimension_weights(), [])

    def test_frames_and_weights_are_a_two_way_match(self):
        self.assertEqual(D.missing_dimension_weights(), [])

    def test_market_weight_is_within_the_spec_range(self):
        """spec §23 给 MARKET 的区间是 0.15~0.20。写成 0.30 会让市场状态压过基本面。"""
        for frame, ws in sorted(D.DIMENSION_WEIGHTS.items()):
            self.assertGreaterEqual(ws[D.MARKET], 0.15, frame)
            self.assertLessEqual(ws[D.MARKET], 0.20, frame)


if __name__ == "__main__":
    unittest.main()
