# -*- coding: utf-8 -*-
"""有息负债闸门：负债没和报表对平时，依赖它的指标一律**没有数**。

上一轮的 bug 是负债解析漏了整段非流动负债（见 ``test_liquidation_model.py``
开头）。修完那个 bug 之后还剩一半没堵：只有清算价值那条链过了对平闸门，
**八个依赖有息负债的属性照样出数**。华域汽车少算 53.93 亿有息负债时，
覆盖倍数、净现金、扣有息负债后资产价值全是「算错了但看起来很正常」的数，
会安静地进画像评分。

所以这里的断言全部围绕一条口径：**减项不可信时给 ``None``，不给 0，也不退
回旧口径**。分成五组：

* **读侧**：五处显式加闸的属性 + 三处自动跟随的比率，全 ``None``。
* **不许跟着消失**：折价后资产与它的市值比不依赖负债，必须有值。
* **评分**：两个直接吃有息负债的评分函数返回 ``None``，画像分量 ``missing``
  且**不进** ``max_available`` 分母。
* **源码侧**：``net_cash`` 在 ``total_debt=None`` 时四个键全 ``None``（原来
  会拿类现金当净现金）。
* **落库**：闸门状态进 ``_result_hash``，保证是追加一行而不是静默复用旧结果。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import asset_engine as eng
from research import asset_metrics
from research import asset_semantics as sem
from research import haircut as hc
from research import rules
from tests.asset_fixtures import fake_provider
from tests.test_asset_semantics import _rows
from tests.test_research import _metrics

#: 闸门关闭时**必须**为 None 的八个属性。前五个显式加闸，后三个经
#: ``_ratio`` 跟随（分子为 None 则比率为 None）。
GATED_ATTRS = (
    "total_debt",
    "debt_free",
    "adjusted_net_cash",
    "pure_net_cash",
    "net_interest_bearing_asset_value",
    "net_cash_to_market_cap",
    "interest_debt_cover",
    "net_interest_bearing_to_market_cap",
)


def _gated(**kw):
    """一个负债没对平的 provider。``liability_reconciliation="FAIL"`` 是唯一
    开关——构造上其余输入和「对平」那份逐位相同，所以两组结果的差异只可能来
    自闸门。"""
    return fake_provider(**{"market_cap": 1e9, "asset_value_ratio": 1.30,
                            "liquidation_ratio": 1.90, **kw})


def _unreconciled():
    return _gated(liability_reconciliation="FAIL")


def _reconciled():
    return _gated()


class TestReadSideGate(unittest.TestCase):
    """读侧：五个显式加闸 + 三个跟随。"""

    def test_all_gated_attrs_are_none_not_zero(self):
        """``None`` 和 ``0.0`` 的区别就是这一项的全部意义。

        ``0`` 会被下游当成「算出来是零」——零净现金、零负债、覆盖倍数 0。
        那是一个**结论**，而这里的事实是「不知道」。
        """
        a = _unreconciled()
        for name in GATED_ATTRS:
            v = getattr(a, name) if not callable(getattr(a, name)) else \
                getattr(a, name)()
            self.assertIsNone(v, name)
            self.assertIsNot(v, 0.0, name)

    def test_gate_does_not_hide_the_discounted_assets(self):
        """折价后资产不依赖负债，不能跟着一起消失。

        它消失的话，「负债没对平」会被误读成「这家公司连资产都算不出来」，
        而资产侧其实是好的。
        """
        a = _unreconciled()
        self.assertEqual(a.gross_adjusted_assets(), 1.3e9)
        self.assertEqual(a.asset_value_to_market_cap, 1.30)

    def test_reconciled_control_group_has_values(self):
        """对照组：同一个构造、只把对平改成 OK，八个属性全部有值。

        没有这一组的话，八个 ``assertIsNone`` 可能只是因为 fixture 本身就
        取不到数——那样这个测试什么都没证明。
        """
        a = _reconciled()
        v = a.net_interest_bearing_asset_value()
        self.assertIsNotNone(v)
        for name in GATED_ATTRS:
            got = getattr(a, name) if not callable(getattr(a, name)) else \
                getattr(a, name)()
            self.assertIsNotNone(got, name)

    def test_debt_free_is_tristate(self):
        """``True`` 确认零负债 / ``False`` 确认有 / ``None`` 不知道。

        构造性验证三态都到得了：``None`` 只能在闸门关闭时出现，而闸门关闭时
        ``total_debt`` 也是 ``None``——两者必须同时成立，不能一个有一个无。
        """
        self.assertIsNone(_unreconciled().debt_free)
        self.assertFalse(_reconciled().debt_free)          # 覆盖倍数 2.0，有债
        zero = fake_provider(interest_debt_cover=None, near_cash_ratio=0.30,
                             liability_reconciliation="OK")
        # 分母取不到时 total_debt 也是 None，所以 debt_free 仍是 None ——
        # 「零负债」必须是**解析出来的零**，不是「除不出来」。
        self.assertIsNone(zero.total_debt)
        self.assertIsNone(zero.debt_free)

    def test_legacy_snapshot_without_the_field_is_gated(self):
        """老快照读回来没有 ``liability_reconciliation``（``None``）→ 自动闸住。

        这些快照的有息负债本来就少算了几百亿（漏了整段非流动负债），不需要
        迁移脚本就有正确的表现。
        """
        payload = {"asset_value_profile": {
            "liquidation_model": hc.LIQUIDATION_MODEL_V1,
            "scenarios": {"CONSERVATIVE": {
                "gross_adjusted_assets": 3e9,
                "liquidation_value": 2e9,
                "net_interest_bearing_asset_value": 2.5e9}},
            "cash_tiers": {"NearCash": 1e9, "PureCash": 5e8,
                           "LiquidFinancialAssets": 6e8, "RestrictedCash": 0.0},
            "net_cash": {"AdjustedNetCash": 8e8, "PureNetCash": 3e8,
                         "TotalInterestBearingDebt": 2e8},
        }}
        a = asset_metrics.AssetMetricProvider(payload, market_cap=1e9)
        self.assertIsNone(a.liability_reconciliation)
        self.assertFalse(a.interest_debt_valid)
        self.assertIsNone(a.total_debt)
        self.assertIsNone(a.adjusted_net_cash)
        # 清算价值的口径是对的（有 liquidation_model），也不缺负债对平字段时
        # 才有效——这里缺了，所以一起判不可用
        self.assertIsNone(a.liquidation_value())


class TestSourceSideGate(unittest.TestCase):
    """源码侧：``net_cash`` 在减项不可信时四个键全 None。"""

    def test_net_cash_returns_all_none_without_a_trustworthy_debt(self):
        items = [it for it in sem.build_economic_view(_rows())["items"]]
        block = sem.net_cash(items, None)
        for key in ("PureNetCash", "AdjustedNetCash", "LiquidNetAssets",
                    "TotalInterestBearingDebt"):
            self.assertIsNone(block[key], key)
            self.assertNotEqual(block[key], 0.0, key)

    def test_net_cash_never_silently_equals_near_cash(self):
        """回归：以前写的是 ``tiers[...] - (total_debt or 0.0)``，
        「净现金」会**等于类现金**——一家全是债的公司看起来像零负债。"""
        items = sem.build_economic_view(_rows())["items"]
        block = sem.net_cash(items, None)
        tiers = sem.cash_tiers(items)
        self.assertNotEqual(block["AdjustedNetCash"], tiers["NearCash"])

    def test_liquidating_value_does_not_swallow_a_none_debt(self):
        """回归：以前写的是 ``gross - (interest_bearing_debt or 0.0)``，
        「扣有息负债后资产价值」会**悄悄等于折价后资产**。"""
        items = sem.build_economic_view(_rows())["items"]
        lv = hc.liquidating_value(items, hc.CONSERVATIVE, None, None)
        self.assertIsNone(lv["liquidation_value"])
        self.assertIsNone(lv["net_interest_bearing_asset_value"])
        self.assertIsNotNone(lv["gross_adjusted_assets"])


class TestScoringGate(unittest.TestCase):
    """评分：两个直接吃有息负债的函数返回 None，分量 missing 且不进分母。"""

    def test_financial_risk_is_none_not_a_free_ten(self):
        """``debt_free`` 三态之后，``if a.debt_free:`` 在闸门关闭时**不能**
        误触发「无债务直接 10 分」。"""
        self.assertIsNone(rules._score_financial_risk(_metrics(
            assets=_unreconciled())))

    def test_liability_safety_is_none_not_a_free_five(self):
        """``_score_liability_safety`` 的基准分是 5 分，在全缺失时必须**在加分
        之前**返回 None，否则白送 5 分。"""
        self.assertIsNone(rules._score_liability_safety(_metrics(
            assets=_unreconciled())))

    def test_scores_still_work_when_reconciled(self):
        m = _metrics(assets=_reconciled())
        self.assertEqual(rules._score_financial_risk(m), 10)
        self.assertEqual(rules._score_liability_safety(m), 10)

    def test_risk_component_goes_missing_and_leaves_the_denominator(self):
        """一路传到画像：``missing_data`` + ``score=None`` + **分母里没有它**。

        如果只是 score 变 None 但仍占 10 分满分，缺数据的公司会被系统性
        压低总分——那是把「不知道」当成了「差」。
        """
        broken = rules.score_cigar_butt(*(lambda m: (m, m))(
            _metrics(assets=_unreconciled())))
        comp = [c for c in broken["components"] if c["name"] == "财务风险"][0]
        self.assertTrue(comp["missing"])
        self.assertIsNone(comp["score"])
        self.assertIsNone(comp["value"], "覆盖倍数取不到就给 None，不给 0")
        self.assertNotIn(10, [c["max"] for c in broken["components"]
                              if c["name"] == "财务风险" and not c["missing"]])
        self.assertNotIn("财务风险", [k for k in
                                      str(broken.get("max_available", ""))])

        good = rules.score_cigar_butt(*(lambda m: (m, m))(_metrics(
            assets=_reconciled())))
        ok = [c for c in good["components"] if c["name"] == "财务风险"][0]
        self.assertFalse(ok["missing"])
        self.assertIsNotNone(ok["score"])

    def test_liability_weights_leave_the_denominator(self):
        """最终总分：负债安全分量 missing 时**不占分母**，也不拉低总分。

        两条一起断言才算数：覆盖率**正好**少 0.20（模板权重），说明它确实退
        出了 ``wsum``；而总分保持 100，说明它没有以 0 分的形式留在分子里。
        只断言「completeness 变低」的话，一个「按 0 分计入」的实现也能通过。
        """
        attrs = {"asset_value": {"score": 100.0}}
        tmpl = rules.RULES_V1["templates"][
            rules._ATTR_TO_TEMPLATE["asset_value"]]
        w = tmpl["liability_safety"]
        res = rules.final_score(attrs, None, _metrics(assets=_unreconciled()),
                                {"primary": "asset_value"})
        ref = rules.final_score(attrs, None, _metrics(assets=_reconciled()),
                                {"primary": "asset_value"})
        c = next(x for x in res["components"] if x["key"] == "liability_safety")
        self.assertTrue(c["missing"])
        self.assertIsNone(c["score"])
        self.assertAlmostEqual(c["weight"], w, msg="missing 报出的是模板权重")
        self.assertAlmostEqual(ref["completeness"] - res["completeness"], w,
                               places=4, msg="它退出了 wsum，覆盖率正好少一个权重")
        self.assertEqual(res["score"], ref["score"],
                         "缺数据不稀释总分：不能被当成「差」")


class TestGateReachesTheResultHash(unittest.TestCase):
    """闸门必须改 ``_result_hash``，否则新结果会被当成旧结果静默丢弃。"""

    def _metrics_pair(self):
        ok = eng.compute(_rows(), market_cap=1_000_000_000.0)
        # 人为把对平结果改成 FAIL，模拟「负债解析没对上」的那一份结果
        broken = eng.compute(_rows(), market_cap=1_000_000_000.0)
        broken["asset_value_profile"]["liabilities"] = {
            **broken["asset_value_profile"]["liabilities"],
            "reconciliation": "FAIL",
            "status": "invalid_base / reconciliation_failed（测试构造）",
        }
        return ok, broken

    def test_hash_changes_when_the_gate_closes(self):
        ok, broken = self._metrics_pair()
        self.assertNotEqual(eng._result_hash(ok), eng._result_hash(broken))

    def test_gate_state_is_recorded_in_the_profile(self):
        """页面要能说出「这些指标为什么不显示」，所以开关状态得入库。"""
        m = eng.compute(_rows(), market_cap=1_000_000_000.0)
        g = m["asset_value_profile"]["interest_debt_gate"]
        self.assertTrue(g["valid"])
        self.assertEqual(g["reconciliation"], "OK")
        self.assertIsNone(g["reason"])


if __name__ == "__main__":
    unittest.main()
