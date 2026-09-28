# -*- coding: utf-8 -*-
"""三档锚 + 赔率（批 4，spec §十一–§十九）。

这一层是「机会」的**唯一**来源，所以这里的测试盯的不是分数，而是三条硬约束：

1. **每档锚都要能自己交代清楚**——``multiple_value / multiple_source /
   sample_size / percentile / as_of / confidence``（倍数）与 ``profit_percentile /
   sample_years / included_years / excluded_years / confidence``（正常化盈利）
   一个都不许少，来源不足时宁肯 ``anchor missing``，**不回落硬编码倍数**。
2. **不许出现没有经济含义的数**——清算价值为负不是「Bear = 负数」，
   价格跌破 Bear 不是「RR = 无穷」，亏损公司的「正常化利润」不是 0。
3. **取不到就是取不到**——``bull = None`` 对烟蒂股是正常结果（§十五），
   不是一个等着被修掉的缺陷。
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import peer_groups as PG          # noqa: E402
from research import rules, valuation_anchors as VA  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具：一个**手搓的 m**。锚只读 m 的这几格（current / annual / industry /
# is_financial），所以不需要走 build_metrics——用真夹具反而会把这次要测的
# 那几格藏在几百行的推导后面。
# --------------------------------------------------------------------------- #
def _m(profits=(-0.5e8, 1e8, 2e8, 3e8, 4e8), price=10.0, mcap=1e10, shares=1e9,
       liquidation=None, asset_ratio=None, industry="汽车零部件",
       is_financial=False, start_year=2022):
    years = list(range(start_year, start_year + len(profits)))
    cur = {"price": price, "total_market_cap": mcap, "total_shares": shares}
    if liquidation is not None:
        cur["liquidation_ratio"] = liquidation
    if asset_ratio is not None:
        cur["asset_value_ratio"] = asset_ratio
    return {"annual": [{"report_period": "%d-12-31" % y, "deduct_profit": p}
                       for y, p in zip(years, profits)],
            "current": cur, "industry": industry, "is_financial": is_financial}


def _peer(sample=(12.0, 15.0, 18.0), group="TIRE_A_SHARE", confidence="high",
          pe_confidence=None):
    """一份 peer 上下文的**最小**形状（只放倍数要用到的那几个键）。

    BATCH 4.1 §一 起，三档锚的倍数读 ``peer_pe_multiple``（**B 语义**：同行
    自己的 PE 分布），**不再读** ``valuation["pe"]["sample"]``（A 语义：本公司
    在组内的位置）。所以两个键都给：``valuation`` 供相对价值因子，
    ``peer_pe_multiple`` 供锚。它们由 ``PeerView.context()`` 并列产出，这里
    手工拼一份等价物——少给一个新的，锚不会报错，只会静默变成
    「同组内没有正 PE 样本」的 missing。

    ``confidence`` 是 **A 语义**那一格的，``pe_confidence`` 是 **B 语义**那一格的；
    后者不传就照 ``confidence_of(count)`` 的真门槛推（≥5 high / 3~4 low / <3 none），
    与 ``PeerView.peer_pe_distribution`` 逐字一致。两个词表分开，是因为它们在
    真实载荷里本来就是两条语义各自的产物。
    """
    values = sorted(float(v) for v in sample)
    count = len(values)
    enough = count >= PG.LOW_CONFIDENCE_MIN
    return {
        "available": True, "peer_group": group, "as_of": "2026-09-25",
        "valuation": {"pe": {"sample": values, "confidence": confidence,
                             "reason": None, "peer_percentile": 0.5}},
        "peer_pe_multiple": {
            "source": "peer_groups.%s.pe" % group,
            "peer_positive_pe_count": count,
            "peer_pe_values": values,
            "peer_pe_median": PG.quantile(values, 0.5) if enough else None,
            "peer_pe_p25": PG.quantile(values, 0.25) if enough else None,
            "peer_pe_p75": PG.quantile(values, 0.75) if enough else None,
            "peer_pe_as_of": "2026-09-25",
            "peer_pe_confidence": pe_confidence or PG.confidence_of(count),
            "sample_status": PG.SAMPLE_OK if enough else PG.SAMPLE_INSUFFICIENT,
            "reason": None if enough else "正 PE 样本只有 %d 家" % count,
            "excluded_nonpositive": [], "excluded_missing": [],
        },
    }


def _resolve(m=None, peer=None, pe_series=None, primary_model=None):
    return VA.resolve(m if m is not None else _m(),
                      context={"peer": peer if peer is not None else _peer(),
                               "pe_series": list(pe_series or [])},
                      primary_model=primary_model)


# --------------------------------------------------------------------------- #
# 1. 每一档锚都要能自己交代清楚（用户裁定 §十二：禁止只返回裸数字）
# --------------------------------------------------------------------------- #
class TestAnchorShape(unittest.TestCase):

    ANCHOR_FIELDS = ("value", "method", "method_label", "inputs", "source",
                     "confidence", "status", "reason")

    def test_every_tier_carries_source_status_and_reason(self):
        out = _resolve()
        for tier in ("bear", "base", "bull"):
            anchor = out[tier]
            for key in self.ANCHOR_FIELDS:
                self.assertIn(key, anchor, "%s 少了 %s" % (tier, key))
            if anchor["status"] == VA.STATUS_OK:
                self.assertIsNotNone(anchor["source"], tier)
                self.assertIn(anchor["method"], VA.METHOD_LABELS, tier)
                self.assertIsNone(anchor["reason"], tier)
            else:
                self.assertTrue(anchor["reason"], "%s 取不到却没有理由" % tier)

    def test_payload_fields_are_all_there(self):
        out = _resolve()
        for key in VA.PAYLOAD_FIELDS:
            self.assertIn(key, out)

    def test_profit_record_carries_the_five_constraints(self):
        """正常化盈利那一格必须同时给出分位、样本年、纳入与排除的年。"""
        profit = _resolve()["profit"]
        for key in VA.PROFIT_FIELDS:
            self.assertIn(key, profit)
        self.assertEqual(profit["sample_years"], 5)
        self.assertEqual(profit["window_years"], 5)
        self.assertEqual(profit["included_years"], ["2022", "2023", "2024", "2025", "2026"])
        self.assertEqual(profit["excluded_years"], [])

    def test_a_blocked_anchor_still_carries_its_evidence(self):
        """取不到的锚也要说清「为什么取不到」——那些数就是理由本身。"""
        bear = VA.asset_anchor(_m(liquidation=-0.5237, asset_ratio=2.2061))
        self.assertEqual(bear["status"], VA.STATUS_MISSING)
        self.assertEqual(bear["inputs"]["liquidation_to_market_cap"], -0.5237)
        self.assertEqual(bear["inputs"]["asset_value_to_market_cap"], 2.2061)


# --------------------------------------------------------------------------- #
# 2. 资产托底锚：只用清算价值/股
# --------------------------------------------------------------------------- #
class TestAssetAnchor(unittest.TestCase):

    def test_positive_liquidation_becomes_the_bear(self):
        out = _resolve(_m(price=12.21, liquidation=0.8744, asset_ratio=1.4764))
        bear = out["bear"]
        self.assertEqual(bear["status"], VA.STATUS_OK)
        self.assertEqual(bear["method"], VA.METHOD_LIQUIDATION)
        self.assertAlmostEqual(bear["value"], 12.21 * 0.8744, places=3)
        self.assertEqual(bear["inputs"]["unit"], "元/股")

    def test_negative_liquidation_gives_no_asset_anchor(self):
        """清算价值为负 → **没有**资产锚，而不是一个负的下行价格。

        这条盯的是实测里真出过的事：``min(清算价值/股, 保守资产价值/股)`` 在
        清算价值为负时**必然**选中那个负数（保守资产价值是不减负债的口径，
        恒不小于清算价值）。华域汽车因此拿到过 Bear = −7.70 元/股，
        ``downside = (14.70 − (−7.70)) / 14.70 = 1.52``——超过 100% 的下行空间。
        """
        anchor = VA.asset_anchor(_m(price=14.70, liquidation=-0.5237,
                                    asset_ratio=2.2061))
        self.assertEqual(anchor["status"], VA.STATUS_MISSING)
        self.assertIsNone(anchor["value"])
        self.assertIn("清算价值/股 = -7.70 元/股", anchor["reason"])

    def test_conservative_asset_value_alone_is_not_an_anchor(self):
        """不减负债的资产价值**不能**单独当锚——它会把 Bear 顶到价格之上。

        对高杠杆公司 ``资产价值/市值`` 可以远大于 1（招行 11.05、平安更高），
        拿它当 Bear 就会伪造出一个 ``BELOW_BEAR_ANCHOR`` 的「机会」。
        """
        anchor = VA.asset_anchor(_m(price=10.0, asset_ratio=2.2))
        self.assertEqual(anchor["status"], VA.STATUS_MISSING)
        self.assertIn("不能充当股东能拿到的下行价格", anchor["reason"])

    def test_no_price_no_asset_anchor(self):
        anchor = VA.asset_anchor(_m(price=None, liquidation=0.9))
        self.assertEqual(anchor["status"], VA.STATUS_MISSING)

    def test_bear_falls_back_to_earnings_when_liquidation_is_negative(self):
        """资产型公司但资产锚取不到 → 退回盈利锚，且**留下降级的痕迹**。"""
        m = _m(price=14.70, liquidation=-0.5237, asset_ratio=2.2061)
        out = _resolve(m, pe_series=[("2026-0%d" % i, 8.0 + i) for i in range(1, 10)])
        bear = out["bear"]
        self.assertEqual(bear["status"], VA.STATUS_OK)
        self.assertNotEqual(bear["method"], VA.METHOD_LIQUIDATION)
        self.assertGreater(bear["value"], 0.0)
        self.assertIn("资产锚取不到", bear["inputs"]["fallback_note"])
        # 关键后果：下行空间回到 [0, 1] 这个有意义的区间里。
        self.assertGreaterEqual(out["raw_downside"], 0.0)
        self.assertLessEqual(out["effective_downside"], 1.0)
        self.assertEqual(out["price_state"], VA.PRICE_ABOVE_BEAR)


# --------------------------------------------------------------------------- #
# 3. 正常化盈利：只用完整年度、亏损年保留、样本不足降置信
# --------------------------------------------------------------------------- #
class TestProfitBands(unittest.TestCase):

    def test_only_complete_years_are_used(self):
        m = _m()
        m["annual"].append({"report_period": "2026-06-30", "deduct_profit": 99e8})
        profit = VA.profit_bands(m)
        self.assertEqual(profit["sample_years"], 5)
        self.assertNotIn(99e8, profit["values"].values())

    def test_loss_years_stay_in_the_sample(self):
        """亏损年份**保留**：只挑盈利年会把 Bear 人为抬高。"""
        profit = VA.profit_bands(_m(profits=(-1e8, -2e8, 3e8, 4e8, 5e8)))
        self.assertEqual(profit["sample_years"], 5)
        self.assertLess(profit["values"]["bear"], 0.0)

    def test_short_sample_lowers_confidence(self):
        self.assertEqual(VA.profit_bands(_m(profits=(1e8, 2e8, 3e8)))["confidence"], 0.5)
        self.assertEqual(VA.profit_bands(_m(profits=(1e8,)))["confidence"], 0.0)
        self.assertEqual(VA.profit_bands(_m())["confidence"], 1.0)

    def test_cyclical_uses_the_longer_window(self):
        """强周期给 8 年：一轮猪周期 4 年以上，5 年窗口很容易整段落在同一侧。

        ``cyclical`` 由 ``_is_cyclical`` 从**行业名单或主模型**判出来，
        ``profit_bands`` 只收这个结论。
        """
        profits = tuple(float(i + 1) * 1e8 for i in range(8))
        self.assertEqual(VA.profit_bands(_m(profits=profits))["window_years"], 5)
        self.assertTrue(VA._is_cyclical(_m(industry="养殖业"), None))
        self.assertFalse(VA._is_cyclical(_m(industry="汽车零部件"), None))
        self.assertTrue(VA._is_cyclical(_m(industry="汽车零部件"), "CYCLICAL_CORE_V2"))
        self.assertEqual(
            VA.profit_bands(_m(profits=profits), cyclical=True)["window_years"], 8)

    def test_quantile_is_the_shared_implementation(self):
        """分位数只有一份实现（``peer_groups.quantile``），本层不重写。

        拿一条**乱序**序列来比：自己排序是那个函数的契约，谁要是把「先排序」
        当成调用方的义务，这里立刻会分叉。
        """
        values = [4e8, -0.5e8, 3e8, 2e8, 1e8]
        profit = VA.profit_bands(_m(profits=tuple(values)))
        for band, q in (("bear", 0.25), ("base", 0.5), ("bull", 0.75)):
            self.assertAlmostEqual(profit["values"][band],
                                   PG.quantile(values, q), places=6, msg=band)

    def test_non_positive_profit_blocks_the_earnings_anchor(self):
        """亏损年做正常化利润 → 不给锚（TCL中环那一类：拿负利润乘 PE 是荒谬的）。"""
        m = _m(profits=(-5e8, -3e8, -1e8, -2e8, -4e8))
        out = _resolve(m, pe_series=[("2026-01", 9.0)])
        bear = out["bear"]
        self.assertEqual(bear["status"], VA.STATUS_MISSING)
        self.assertIn("P25", bear["reason"])
        self.assertIn("2022–2026", bear["reason"])


# --------------------------------------------------------------------------- #
# 4. 倍数：来源必须可审计，不足就 missing
# --------------------------------------------------------------------------- #
class TestMultiples(unittest.TestCase):

    def test_own_pe_multiple_ignores_non_positive_months(self):
        series = [("2026-01", 10.0), ("2026-02", None), ("2026-03", -3.0),
                  ("2026-04", 20.0)]
        own = VA.own_pe_multiple(series, 0.25)
        self.assertEqual(own["sample_size"], 2)
        self.assertEqual(own["as_of"], "2026-04")
        self.assertIn("2 个月为正 PE／共 4 个月，亏损月退出样本", own["multiple_source"])

    def test_own_pe_multiple_confidence_bands_come_from_config(self):
        series = [("2026-%02d" % i, 10.0 + i) for i in range(1, 13)]
        self.assertEqual(VA.own_pe_multiple(series, 0.25)["confidence"], 0.3)

    def test_peer_multiple_is_a_cross_section_quantile(self):
        mult = VA.peer_pe_multiple(_peer(), 0.5)
        self.assertEqual(mult["multiple_value"], 15.0)
        self.assertEqual(mult["sample_size"], 3)
        self.assertEqual(mult["percentile"], 0.5)
        # 3 家 = 「可以用的弱样本」（BATCH 4.1 §三：≥5 high / 3~4 low）。
        # 旧夹具在这里报 1.0，是因为它读的是 A 语义那格手写的 confidence；
        # 现在读的是 B 语义里按**家数**算出来的那一档。
        self.assertEqual(mult["confidence"], 0.5)
        self.assertIn("TIRE_A_SHARE", mult["multiple_source"])

    def test_five_peers_are_a_high_confidence_cross_section(self):
        mult = VA.peer_pe_multiple(_peer(sample=(11.0, 12.0, 13.0, 14.0, 15.0)), 0.5)
        self.assertEqual(mult["sample_size"], 5)
        self.assertEqual(mult["confidence"], 1.0)
        self.assertEqual(mult["multiple_value"], 13.0)

    def test_peer_confidence_words_are_translated_to_numbers(self):
        """``"low"/"none"`` 必须翻成 0.5 / 0.0——直接比大小会 TypeError，
        而顺手写 ``1.0 if c == "high" else 0.5`` 会把「没有样本」当成「弱样本」。

        读的是 **B 语义**那一格的词（``peer_pe_confidence``）：A 语义那格的
        confidence 只跟「本公司 PE 分位」有关，与倍数无关。
        """
        self.assertEqual(
            VA.peer_pe_multiple(_peer(pe_confidence="low"), 0.5)["confidence"], 0.5)
        self.assertEqual(
            VA.peer_pe_multiple(_peer(pe_confidence="none"), 0.5)["confidence"], 0.0)

    def test_no_sample_means_missing_not_a_hardcoded_multiple(self):
        """来源不足时**宁肯 missing**——这是用户裁定的硬约束。"""
        for q in (0.25, 0.5, 0.75):
            mult = VA.peer_pe_multiple(_peer(sample=()), q)
            self.assertIsNone(mult["multiple_value"])
            self.assertEqual(mult["sample_size"], 0)
            self.assertTrue(mult["reason"])
        # 整条链上都不许出现一个「兜底倍数」：配置里也读不到这种数。
        self.assertNotIn("fallback_multiple", rules.RULES_V1.get("risk_reward", {}))

    def test_base_uses_peer_median_of_normalized_profit(self):
        out = _resolve()
        base = out["base"]
        self.assertEqual(base["status"], VA.STATUS_OK)
        self.assertEqual(base["method"], VA.METHOD_PEER_MEDIAN_PE)
        expected = 2e8 * 15.0 * (10.0 / 1e10)      # P50 利润 × 中位 PE × 每股系数
        self.assertAlmostEqual(base["value"], expected, places=4)

    def test_earnings_anchor_carries_the_whole_multiple_record(self):
        """倍数的**六个字段**必须整条进 inputs（摊平成顶层键会静默丢字段）。"""
        inputs = _resolve()["base"]["inputs"]
        multiple = inputs["multiple"]
        self.assertIsInstance(multiple, dict)
        self.assertEqual(sorted(multiple), sorted(VA.MULTIPLE_FIELDS))
        for key in VA.MULTIPLE_FIELDS:
            self.assertIsNotNone(multiple[key], key)


# --------------------------------------------------------------------------- #
# 5. Bull 缺席是结论，不是缺陷（§十五）
# --------------------------------------------------------------------------- #
class TestBull(unittest.TestCase):

    def test_cigar_model_has_no_bull_and_that_is_the_answer(self):
        out = _resolve(primary_model="VALUE_CIGAR_V2")
        self.assertEqual(out["bull"]["status"], VA.STATUS_MISSING)
        self.assertIsNone(out["bull"]["value"])
        self.assertIn("没有可靠的乐观盈利锚", out["bull"]["reason"])
        # 但 Bear/Base 照常有——Bull 缺席不影响另外两档。
        self.assertEqual(out["bear"]["status"], VA.STATUS_OK)
        self.assertEqual(out["base"]["status"], VA.STATUS_OK)

    def test_cyclical_model_gets_bull_from_the_upper_peer_percentile(self):
        m = _m(industry="养殖业", profits=tuple(float(i + 1) * 1e8 for i in range(8)))
        out = _resolve(m, primary_model="CYCLICAL_CORE_V2")
        bull = out["bull"]
        self.assertEqual(bull["status"], VA.STATUS_OK)
        self.assertEqual(bull["method"], VA.METHOD_PEER_UPPER_PE)
        self.assertEqual(bull["inputs"]["multiple"]["percentile"], 0.75)
        self.assertGreater(bull["value"], out["base"]["value"])

    def test_thin_sample_blocks_bull_before_it_blocks_base(self):
        """Bull 的样本门槛比 Base 高：3 个完整年度的 P75 只是一个极端值。"""
        out = _resolve(_m(profits=(1e8, 2e8, 3e8)),
                       primary_model="CYCLICAL_CORE_V2")
        self.assertEqual(out["bull"]["status"], VA.STATUS_MISSING)
        self.assertIn("3", out["bull"]["reason"])
        self.assertEqual(out["base"]["status"], VA.STATUS_OK)


# --------------------------------------------------------------------------- #
# 5b. 倍数只看「同行自己的 PE 分布」（BATCH 4.1 §一–§五、§十）
# --------------------------------------------------------------------------- #
def _peer_of_a_loss_maker(peers=(12.0, 15.0, 18.0, 21.0)):
    """本公司亏损的 peer 上下文：A 语义没有样本，**B 语义照常有**。

    这正是旧实现丢掉的那一段：``valuation["pe"]`` 是 ``valuation_relative`` 在
    「自身 PE ≤ 0」时的 early return 形状（没有 ``sample`` 键），而
    ``peer_pe_multiple`` 里 4 家同行的 PE 一个不少。
    """
    peer = _peer(sample=peers)
    peer["valuation"]["pe"] = {
        "own": -8.0, "peer_median": None, "peer_percentile": None,
        "peer_count": len(peers), "confidence": PG.CONF_NONE,
        "sample_status": PG.SAMPLE_OWN_NOT_APPLICABLE,
        "reason_code": PG.OWN_PE_NOT_MEANINGFUL, "reason": "公司自身 PE ≤ 0",
    }
    return peer


class TestPeerMultipleIsDecoupledFromOwnPe(unittest.TestCase):
    """同行有几倍 PE 与本公司有没有利润**无关**（BATCH 4.1 §一）。"""

    def test_a_loss_maker_can_still_build_base_and_bull(self):
        peer = _peer_of_a_loss_maker()
        self.assertIsNone(peer["valuation"]["pe"].get("sample"))
        out = _resolve(peer=peer, primary_model="CYCLICAL_CORE_V2")
        self.assertEqual(out["base"]["status"], VA.STATUS_OK)
        self.assertEqual(out["base"]["method"], VA.METHOD_PEER_MEDIAN_PE)
        self.assertEqual(out["bull"]["status"], VA.STATUS_OK)
        self.assertEqual(out["bull"]["method"], VA.METHOD_PEER_UPPER_PE)
        # 中位 = (15+18)/2 = 16.5；上分位按同一条横截面算。
        self.assertEqual(out["base"]["inputs"]["multiple"]["multiple_value"], 16.5)
        self.assertEqual(out["base"]["inputs"]["multiple"]["sample_size"], 4)

    def test_three_or_four_peers_lower_the_confidence_not_the_value(self):
        """§十：3~4 家能生成锚，只是置信度降一档——**不是 missing**。"""
        out = _resolve(peer=_peer_of_a_loss_maker((12.0, 15.0, 18.0)),
                       primary_model="CYCLICAL_CORE_V2")
        self.assertEqual(out["base"]["status"], VA.STATUS_OK)
        self.assertEqual(out["base"]["confidence"], 0.5)
        self.assertGreaterEqual(
            out["anchor_confidence"],
            rules.RULES_V1["risk_reward"]["min_anchor_confidence"])

    def test_two_peers_are_insufficient_for_any_multiple(self):
        out = _resolve(peer=_peer_of_a_loss_maker((12.0, 15.0)),
                       primary_model="CYCLICAL_CORE_V2")
        self.assertEqual(out["base"]["status"], VA.STATUS_MISSING)
        self.assertIn("正 PE 样本", out["base"]["reason"])
        for q in (0.25, 0.5, 0.75):
            mult = VA.peer_pe_multiple(_peer_of_a_loss_maker((12.0, 15.0)), q)
            self.assertIsNone(mult["multiple_value"], q)
            self.assertEqual(mult["sample_size"], 2, q)

    def test_the_multiple_never_falls_back_to_the_whole_market(self):
        """没有 peer 组 / 整组亏损 → missing。**绝不退回全市场基准**（裁定 6）。"""
        for peer in ({}, {"available": False, "reason": "行业未识别"},
                     _peer_of_a_loss_maker(())):
            for q in (0.25, 0.5, 0.75):
                mult = VA.peer_pe_multiple(peer, q)
                self.assertIsNone(mult["multiple_value"], (peer, q))
                self.assertTrue(mult["reason"], (peer, q))


# --------------------------------------------------------------------------- #
# 6. 赔率：跌破 Bear 不给无穷、也不给负的下行；下行分母有地板
# --------------------------------------------------------------------------- #
class TestRatioBlock(unittest.TestCase):

    def test_above_bear_computes_all_three(self):
        out = _resolve(_m(price=10.0))
        self.assertEqual(out["price_state"], VA.PRICE_ABOVE_BEAR)
        self.assertAlmostEqual(out["upside"], (out["base"]["value"] - 10.0) / 10.0,
                               places=6)
        self.assertFalse(out["floor_applied"])
        self.assertEqual(out["raw_downside"], out["effective_downside"])

    def test_below_bear_yields_no_infinity(self):
        # 资产托底：Bear = 清算价值/股 = 现价 × 1.2 = 6.0 元，而现价只有 5.0。
        # （资产价值/市值 ≥ asset_heavy_ratio 才会走资产锚，所以两格都要给。）
        out = _resolve(_m(price=5.0, liquidation=1.2, asset_ratio=1.5))
        self.assertEqual(out["price_state"], VA.PRICE_BELOW_BEAR)
        self.assertIsNone(out["risk_reward"])
        # §八：真实下行照实报 0，评分分母报地板——**两个数都要在**，否则读的人
        # 会以为真实下行刚好 5%（0.05 只是分母的地板）。
        self.assertEqual(out["raw_downside"], 0.0)
        self.assertEqual(out["effective_downside"], out["downside_floor"])
        self.assertEqual(out["downside_floor"],
                         rules.RULES_V1["risk_reward"]["min_bear_downside"])
        self.assertTrue(out["floor_applied"])
        self.assertTrue(out["effective_downside"] > 0.0)
        self.assertIsNone(out["raw_risk_reward_ratio"])
        score, note = VA.rr_score(None, out["upside"], out["effective_downside"],
                                  out["price_state"])
        self.assertEqual(score, rules.RULES_V1["risk_reward"]["below_bear_score"])
        self.assertTrue(note)

    def test_downside_is_never_negative(self):
        for price in (5.0, 7.0, 10.0, 30.0):
            out = _resolve(_m(price=price, liquidation=0.9))
            if out["raw_downside"] is None:
                continue
            self.assertGreaterEqual(out["raw_downside"], 0.0, price)
            self.assertGreater(out["effective_downside"], 0.0, price)
            self.assertLessEqual(out["effective_downside"], 1.0, price)

    def test_no_price_no_ratios_but_anchors_survive(self):
        out = _resolve(_m(price=None))
        self.assertIsNone(out["upside"])
        self.assertIsNone(out["raw_downside"])
        self.assertIsNone(out["effective_downside"])
        self.assertIsNone(out["price_state"])
        self.assertEqual(out["bear"]["status"], VA.STATUS_OK)


# --------------------------------------------------------------------------- #
# 6b. 下行分母的地板（BATCH 4.1 §六–§九）
# --------------------------------------------------------------------------- #
class TestDownsideFloor(unittest.TestCase):
    """下限 2% 与 10% 的下行，走到赔率的分母上分别是多少。

    地板**不是**为了「让分数好看」，是为了不让一个小分母把赔率放大成一个
    三位数——那个数不是赔率好，是分母小（华域汽车 raw 下行 1.99% → RR 132）。
    """

    def _raw(self, ratio):
        """造一个「真实下行恰好 = ratio」且**上行是正的**资产托底股票。

        现价 1.0 元 / 市值 10 亿，正常化利润 P50 = 2 亿 × peer 中位 15 倍 = 30 亿
        → 每股 3.0 元；Bear = 现价 × (1 − ratio)。
        """
        return _m(price=1.0, mcap=1e9, shares=1e9,
                  liquidation=1.0 - ratio, asset_ratio=1.5)

    def test_a_two_percent_downside_is_lifted_to_the_floor(self):
        out = _resolve(self._raw(0.02))
        self.assertEqual(out["price_state"], VA.PRICE_ABOVE_BEAR)
        self.assertAlmostEqual(out["raw_downside"], 0.02, places=6)
        self.assertAlmostEqual(out["effective_downside"], 0.05, places=6)
        self.assertEqual(out["downside_floor"], 0.05)
        self.assertTrue(out["floor_applied"])
        self.assertGreater(out["upside"], 0.0)
        # 原始读数**不被覆盖**：raw 赔率照实报，评分用的是 effective 那一个。
        self.assertAlmostEqual(
            out["raw_risk_reward_ratio"], out["upside"] / 0.02, places=4)
        self.assertAlmostEqual(
            out["risk_reward"], out["upside"] / 0.05, places=4)
        # 小分母被顶到 5% 之后，赔率**收敛**（这正是地板存在的理由）。
        self.assertGreater(out["raw_risk_reward_ratio"], out["risk_reward"])

    def test_a_ten_percent_downside_keeps_its_own_denominator(self):
        out = _resolve(self._raw(0.10))
        self.assertAlmostEqual(out["raw_downside"], 0.10, places=6)
        self.assertAlmostEqual(out["effective_downside"], 0.10, places=6)
        self.assertFalse(out["floor_applied"])
        self.assertEqual(out["raw_risk_reward_ratio"], out["risk_reward"])

    def test_the_floor_comes_from_config_and_is_not_hardcoded(self):
        """改配置就能改分母——模块里不许有第二个地板常量。"""
        self.assertFalse(hasattr(VA, "DOWNSIDE_FLOOR"))
        self.assertEqual(rules.RULES_V1["risk_reward"]["min_bear_downside"], 0.05)
        saved = dict(rules.RULES_V1["risk_reward"])
        try:
            rules.RULES_V1["risk_reward"]["min_bear_downside"] = 0.20
            out = _resolve(self._raw(0.02))
            self.assertAlmostEqual(out["effective_downside"], 0.20, places=6)
            self.assertEqual(out["downside_floor"], 0.20)
        finally:
            rules.RULES_V1["risk_reward"].clear()
            rules.RULES_V1["risk_reward"].update(saved)


def _declared_band(ratio):
    """**照配置表**手工读一遍 ``rr_bands``——测试自己的一份读数。

    刻意不复用 ``VA._ladder``：这一条要证明的是「分数来自那张声明表」，
    复用被测函数等于自己证明自己。
    """
    for upper, score in rules.RULES_V1["risk_reward"]["rr_bands"]:
        if upper is None or ratio < upper:
            return score
    return rules.RULES_V1["risk_reward"]["rr_bands"][-1][1]


class TestRatioBandsAreStillBanded(unittest.TestCase):
    """§九：赔率**继续分段**，只是进的数换成 effective ratio。

    换分母会**改档**——这正是本节要的效果：一个 2% 的下行配 5% 的上行，
    原始赔率 2.5 落在「1.5~2 上面」的那一档，而按 5% 的地板算出来只有 1.0。
    """

    def test_the_score_comes_from_the_declared_bands(self):
        for ratio in (0.1, 0.49, 0.5, 0.99, 1.0, 1.49, 1.5, 1.99, 2.0, 2.99, 3.0, 9.0):
            score, _note = VA.rr_score(ratio, 0.5, 0.1, VA.PRICE_ABOVE_BEAR)
            self.assertEqual(score, _declared_band(ratio), ratio)

    def test_a_two_percent_downside_changes_the_band_not_just_the_number(self):
        # 现价 1.0 元、市值 10 亿；P50 利润 7 亿 × peer 中位 15 倍 → 每股 1.05 元
        # → 上行 5%；Bear = 现价 × 0.98 → 真实下行 2%。
        m = _m(price=1.0, mcap=1e9, shares=1e9, liquidation=0.98, asset_ratio=1.5,
               profits=(6e7, 6.5e7, 7e7, 7.5e7, 8e7))
        out = _resolve(m)
        self.assertAlmostEqual(out["upside"], 0.05, places=6)
        self.assertAlmostEqual(out["raw_downside"], 0.02, places=6)
        self.assertAlmostEqual(out["effective_downside"], 0.05, places=6)
        self.assertAlmostEqual(out["raw_risk_reward_ratio"], 2.5, places=4)
        self.assertAlmostEqual(out["risk_reward"], 1.0, places=4)
        score, note = VA.rr_score(out["risk_reward"], out["upside"],
                                  out["effective_downside"], out["price_state"])
        self.assertIsNone(note)
        self.assertEqual(score, _declared_band(out["risk_reward"]))
        # 分母换掉之后**档位真的变了**（不是只改一个显示的小数）。
        self.assertNotEqual(score, _declared_band(out["raw_risk_reward_ratio"]))


# --------------------------------------------------------------------------- #
# 7. 金融隔离与「永不抛异常」
# --------------------------------------------------------------------------- #
class TestFinancialAndRobustness(unittest.TestCase):

    def test_financial_has_no_ordinary_anchors(self):
        out = _resolve(_m(is_financial=True, liquidation=-1.0639, asset_ratio=11.05))
        for tier in ("bear", "base", "bull"):
            self.assertEqual(out[tier]["status"], VA.STATUS_INSUFFICIENT_MODEL, tier)
            self.assertIn("银行/保险", out[tier]["reason"])
        self.assertEqual(out["anchor_confidence"], 0.0)

    def test_anchor_confidence_is_the_weakest_link(self):
        """一个来自 105 个月、另一个来自 3 家同业 → 报弱的那一个。"""
        out = _resolve(pe_series=[("2026-%02d" % i, 8.0 + i) for i in range(1, 13)],
                       peer=_peer(confidence="low"))
        self.assertEqual(out["bear"]["confidence"], 0.3)      # 12 个月 → 0.3
        self.assertEqual(out["base"]["confidence"], 0.5)      # peer 词表 low → 0.5
        self.assertEqual(out["anchor_confidence"], 0.3)

    def test_empty_metrics_never_raises(self):
        out = VA.resolve({})
        for key in VA.PAYLOAD_FIELDS:
            self.assertIn(key, out)
        for tier in ("bear", "base", "bull"):
            self.assertNotEqual(out[tier]["status"], VA.STATUS_OK, tier)
        self.assertEqual(out["anchor_confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
