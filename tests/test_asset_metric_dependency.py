# -*- coding: utf-8 -*-
"""画像层与资产指标的依赖约束（PROFILE_SCORING_V1.2）。

本轮迁移要求：**任何使用资产价值的画像/路由分量，必须读 AssetMetricProvider，
不得继续从旧 balance-sheet 一级科目拼 cash_like**。

光靠 code review 守不住这条——旧字段 ``cur["net_cash_ratio"]`` 还留在
``valuation_metrics`` 里（前端在用），随手一写就能接回去，而且接回去之后
分数照样算得出来，不会报错。所以这里用三种互补的方式把它钉住：

1. **行为测试**：把旧字段改成荒谬值，画像分必须纹丝不动；再改 provider，
   画像分必须跟着动。这条是决定性的——它不关心代码长什么样。
2. **缺失测试**：拿不到资产指标时必须 missing（score=None、不计入归一化），
   **不许补 0、不许回退旧口径**。
3. **源码测试**：四个画像函数里不许出现旧字段名。前两条万一被绕过，
   这条能在改动发生的那一刻就拦住。

三条一起才够：只做 3 会漏掉「换了名字照读旧数」；只做 1 会漏掉「新写了一个
分量又读了旧字段」。
"""
import os
import pathlib
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import asset_metrics, engine, router, rules
from tests.asset_fixtures import fake_provider
from tests.test_research import _metrics

#: 旧口径的资产字段。它们现在**只用于展示**，评分不再读。
#: 任何一条被评分读到，第 1 组测试就会失败。
LEGACY_ASSET_FIELDS = {
    "net_cash_ratio": 999.0,
    "short_debt_cover": 999.0,
    "liquidation_ratio": 999.0,
    "asset_value_ratio": 999.0,
    "liquid_asset_ratio": 999.0,
}

#: 资产类分量 -> 它依赖的 provider 属性。改这些属性，分数必须变。
ASSET_PROFILE_COMPONENTS = (
    # (画像名, 分量名, 评分函数, provider 属性, 拉高的值)
    ("value", rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP, "score_value", "net_cash_to_market_cap", 0.90),
    ("cigar_butt", rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP, "score_cigar_butt", "net_cash_to_market_cap", 0.90),
    ("cigar_butt", "清算价值/市值", "score_cigar_butt", "liquidation_to_market_cap", 1.90),
    ("asset_value", rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP, "score_asset_value", "net_cash_to_market_cap", 0.90),
    ("asset_value", "资产价值/市值", "score_asset_value", "asset_value_to_market_cap", 1.90),
    ("asset_value", "资产流动性", "score_asset_value", "liquid_asset_ratio", 0.90),
)

#: provider 属性的构造参数名。fake_provider 收的是比率参数，名字一一对应。
_FAKE_KW = {
    "net_cash_to_market_cap": "net_cash_ratio",
    "near_cash_to_market_cap": "near_cash_ratio",
    "liquidation_to_market_cap": "liquidation_ratio",
    "asset_value_to_market_cap": "asset_value_ratio",
    "liquid_asset_ratio": "liquid_asset_ratio",
}

FAKE_ASSET_ATTRS = ("net_cash_to_market_cap", "liquidation_to_market_cap",
                    "asset_value_to_market_cap", "liquid_asset_ratio",
                    "interest_debt_cover")


def _comp(block, name):
    for c in block["components"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"找不到分量 {name}，现有：{[c['name'] for c in block['components']]}")


def _profile(m, fn):
    return getattr(rules, fn)(m, m)


class TestLegacyFieldsAreInert(unittest.TestCase):
    """第 1 组：旧字段对评分完全没有影响。"""

    def test_legacy_asset_fields_do_not_move_any_profile(self):
        """把五个旧字段改成荒谬值，四个画像分与风险标记必须一字不变。"""
        base = _metrics()
        baseline = {fn: _profile(base, fn)["score"] for fn in
                    ("score_value", "score_dividend", "score_cigar_butt",
                     "score_asset_value")}
        baseline_risk = rules.detect_risk(base)["level"]

        tampered = _metrics()
        for k, v in LEGACY_ASSET_FIELDS.items():
            tampered["current"][k] = v

        for fn, s in baseline.items():
            self.assertEqual(
                _profile(tampered, fn)["score"], s,
                f"{fn} 的分数被旧字段影响了——它还在读 current 里的旧资产口径。"
                f"画像层取资产数只有 m['assets'] 一条路。")
        self.assertEqual(rules.detect_risk(tampered)["level"], baseline_risk,
                         "风险等级被旧字段 short_debt_cover 影响了")

    def test_removing_the_legacy_fields_entirely_changes_nothing(self):
        """更狠一点：把五个旧字段整个删掉，分数还是不变。

        真实场景里这些字段可能因为上游改动而缺席——那时画像必须仍然算得对，
        而不是悄悄退化。
        """
        base = _metrics()
        stripped = _metrics()
        for k in LEGACY_ASSET_FIELDS:
            stripped["current"].pop(k, None)
        for fn in ("score_value", "score_dividend", "score_cigar_butt",
                   "score_asset_value"):
            self.assertEqual(_profile(stripped, fn)["score"],
                             _profile(base, fn)["score"], fn)

    def test_provider_moves_the_score(self):
        """反向验证：改 provider 必须能推动画像分。

        没有这一条，上面两条可以靠「画像彻底不读资产」蒙混过关。
        """
        for profile, comp_name, fn, attr, high in ASSET_PROFILE_COMPONENTS:
            low_m = _metrics(assets=fake_provider(**{_FAKE_KW[attr]: 0.0}))
            high_m = _metrics(assets=fake_provider(**{_FAKE_KW[attr]: high}))
            low = _comp(_profile(low_m, fn), comp_name)
            high_c = _comp(_profile(high_m, fn), comp_name)
            self.assertFalse(low["missing"], f"{profile}.{comp_name} 不该缺失")
            self.assertGreater(
                high_c["score"], low["score"],
                f"{profile}.{comp_name} 没有随 {attr} 变化——"
                f"provider 的数据没进到评分里")


class TestMissingStaysMissing(unittest.TestCase):
    """第 2 组：拿不到资产指标 -> missing / excluded，不当 0、不回退旧口径。"""

    def test_no_snapshot_means_missing_not_zero(self):
        m = _metrics(assets=fake_provider(available=False,
                                          reason="测试：没有资产语义快照"))
        for profile, comp_name, fn, _attr, _high in ASSET_PROFILE_COMPONENTS:
            c = _comp(_profile(m, fn), comp_name)
            self.assertTrue(c["missing"], f"{profile}.{comp_name} 应当缺失")
            self.assertIsNone(c["score"],
                              f"{profile}.{comp_name} 缺失时 score 必须是 None，不是 0")
            self.assertFalse(c["eligible"])

    def test_missing_components_leave_the_denominator(self):
        """缺失的分量必须退出归一化，而不是以 0 分参与。"""
        full_ratios = dict(net_cash_ratio=0.5, liquidation_ratio=1.2,
                           asset_value_ratio=1.3, liquid_asset_ratio=0.5)
        full = _profile(_metrics(assets=fake_provider(**full_ratios)),
                        "score_cigar_butt")
        gone = _profile(_metrics(assets=fake_provider(available=False)),
                        "score_cigar_butt")
        self.assertLess(gone["max_available"], full["max_available"],
                        "缺失分量的满分没有从分母里去掉")
        # 烟蒂画像里依赖资产指标的是 净现金/市值 30 + 清算价值/市值 30
        # + 财务风险 10（它由类现金覆盖有息负债的倍数决定）
        self.assertAlmostEqual(full["max_available"] - gone["max_available"], 70.0)
        self.assertAlmostEqual(gone["max_available"], 30.0)  # 只剩 PB 20 + 现金流存活 10

    def test_legacy_fallback_is_not_used_when_snapshot_is_absent(self):
        """旧字段明明有值，也不许拿来顶替。"""
        m = _metrics(assets=fake_provider(available=False))
        m["current"].update({"net_cash_ratio": 0.90, "liquidation_ratio": 1.90,
                             "asset_value_ratio": 1.90, "liquid_asset_ratio": 0.90,
                             "short_debt_cover": 9.0})
        for profile, comp_name, fn, _attr, _high in ASSET_PROFILE_COMPONENTS:
            self.assertTrue(_comp(_profile(m, fn), comp_name)["missing"],
                            f"{profile}.{comp_name} 在用旧口径兜底")
        self.assertIsNone(rules._asset_val(m, "net_cash_to_market_cap"))

    def test_single_unavailable_ratio_stays_missing(self):
        """快照在、但这一项算不出来（分母为 0 之类）-> 同样 missing。"""
        m = _metrics(assets=fake_provider(net_cash_ratio=None,
                                          liquidation_ratio=None,
                                          asset_value_ratio=None,
                                          liquid_asset_ratio=None))
        for profile, comp_name, fn, _attr, _high in ASSET_PROFILE_COMPONENTS:
            c = _comp(_profile(m, fn), comp_name)
            self.assertTrue(c["missing"], f"{profile}.{comp_name}")
            self.assertIsNone(c["score"])

    def test_missing_reason_is_surfaced(self):
        m = _metrics(assets=fake_provider(available=False, reason="快照还没生成"))
        c = _comp(_profile(m, "score_cigar_butt"), rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP)
        self.assertEqual(c["reason"], "快照还没生成")

    def test_router_components_stay_missing(self):
        m = _metrics(assets=fake_provider(available=False, reason="没有快照"))
        ctx = router._Ctx(m, {})
        for fn in (router._c_net_cash, router._c_liquidation):
            score, reason = fn(ctx)
            self.assertIsNone(score, f"{fn.__name__} 缺失时不该给分")
            self.assertEqual(reason, "没有快照")

    def test_asset_discount_prominent_is_tristate(self):
        """缺失必须是 None，不能是 False。

        False 的含义是「折价确实不突出」，会让 tie-breaker 把股票推给股息模型。
        把「不知道」读成「不突出」，正是这轮要消灭的那类错误。
        """
        gone = router._Ctx(_metrics(assets=fake_provider(available=False)), {})
        self.assertIsNone(gone.asset_discount_prominent())
        low = router._Ctx(_metrics(assets=fake_provider(asset_value_ratio=0.5)), {})
        self.assertIs(low.asset_discount_prominent(), False)
        high = router._Ctx(_metrics(assets=fake_provider(asset_value_ratio=1.5)), {})
        self.assertIs(high.asset_discount_prominent(), True)


class TestNoLegacySourceReads(unittest.TestCase):
    """第 3 组：源码层面，画像函数里不许再出现旧资产字段名。"""

    #: 画像模块 -> 该模块内禁止读取的旧字段
    FORBIDDEN_IN_RULES = tuple(LEGACY_ASSET_FIELDS)
    #: 这些旧字段允许出现在 rules.py 的哪些函数里（它们的定义/展示）
    ALLOWED_FUNCS = ("_asset_val", "_assets", "_asset_comp", "_asset_missing_reason")

    @staticmethod
    def _func_sources(path, prefix="def "):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        out = {}
        current, buf = None, []
        for line in src.splitlines():
            m = re.match(rf"{prefix}(\w+)", line)
            if m:
                if current:
                    out[current] = "\n".join(buf)
                current, buf = m.group(1), [line]
            elif current:
                buf.append(line)
        if current:
            out[current] = "\n".join(buf)
        return out

    def test_profile_scorers_do_not_read_legacy_fields(self):
        src = pathlib.Path(rules.__file__).read_text(encoding="utf-8")
        scorers = ("score_value", "score_dividend", "score_cigar_butt",
                   "score_asset_value", "_score_financial_safety",
                   "_score_financial_risk", "_score_liability_safety",
                   "detect_risk")
        # 只禁「从 current 里取旧字段」这个动作。cfg["net_cash_ratio"] 是
        # RULES_V1["value"] 里的**曲线**键名，跟指标同名叫法而已，不在此列。
        reads = tuple(re.compile(rf'(?:cur|self\.cur|m\["current"\])\s*'
                                 rf'(?:\.get\(\s*|\[\s*)"{field}"')
                      for field in self.FORBIDDEN_IN_RULES)
        funcs = self._func_sources(rules.__file__)
        for name in scorers:
            body = funcs.get(name)
            self.assertIsNotNone(body, f"rules.py 里找不到 {name}")
            for field, pattern in zip(self.FORBIDDEN_IN_RULES, reads):
                self.assertIsNone(
                    pattern.search(body),
                    f"{name} 还在从 current 读旧资产字段 {field}——"
                    f"必须改读 m['assets']")
        # 反向保险：扫描本身得抓得住问题写法。没有这一条，正则哪天写歪了
        # （比如少了转义），上面整个循环会变成永远通过的空转。
        self.assertIsNotNone(reads[0].search('x = cur.get("net_cash_ratio")'))
        self.assertIsNotNone(reads[0].search('x = m["current"].get("net_cash_ratio")'))
        self.assertIsNotNone(reads[0].search('x = cur["net_cash_ratio"]'))
        # 曲线配置的键名与指标同名叫法，不该被误伤
        self.assertIsNone(reads[0].search('cfg["net_cash_ratio"]'))

    def test_engine_has_no_legacy_cash_like_concatenation(self):
        """``_cash_like`` 必须彻底消失。

        它是旧口径的源头（货币资金 + 交易性金融资产）。留着这个函数，就等于
        留着一条随时能被接回去的暗管；删掉之后，想接回去必须重新写一遍，
        而这个动作在 code review 里是看得见的。
        """
        self.assertFalse(hasattr(engine, "_cash_like"),
                         "engine._cash_like 又回来了——那是旧口径的拼接入口")

    def test_every_asset_component_reads_the_provider(self):
        """六个资产类分量的数值必须来自 provider 的属性。"""
        m = _metrics()
        assets = m["assets"]
        for profile, comp_name, fn, attr, _high in ASSET_PROFILE_COMPONENTS:
            c = _comp(_profile(m, fn), comp_name)
            expected = getattr(assets, attr)
            self.assertAlmostEqual(
                c["value"], expected, places=9,
                msg=f"{profile}.{comp_name} 的值 {c['value']} 不等于 "
                    f"provider.{attr}={expected}——它读的不是新口径")


class TestProviderContract(unittest.TestCase):
    """provider 自身的行为约束。"""

    def test_missing_provider_is_unavailable_and_silent(self):
        a = asset_metrics.AssetMetricProvider.missing("测试")
        self.assertFalse(a.available)
        self.assertEqual(a.to_dict()["ratios"]["net_cash_to_market_cap"], None)
        self.assertEqual(a.display_model()["breakdown"], [])

    def test_zero_debt_is_not_a_zero_cover(self):
        """完全没有有息负债时覆盖倍数无定义，但那是最好的一档。"""
        a = fake_provider(near_cash_ratio=0.5, interest_debt_cover=None)
        a.net_cash_block["TotalInterestBearingDebt"] = 0.0
        self.assertTrue(a.debt_free)
        self.assertIsNone(a.interest_debt_cover)
        m = _metrics(assets=a)
        self.assertEqual(rules._score_financial_risk(m), 10)

    def test_ratios_follow_the_current_market_cap(self):
        """比率必须用**当前市值**现算，不是快照里那个。

        股价刷新时不该重新解析财报；反过来说，市值变了比率就得跟着变，
        否则拿旧市值算出来的净现金/市值会一直错下去。
        """
        a = fake_provider(market_cap=20e9, net_cash_ratio=0.5)
        self.assertAlmostEqual(a.net_cash_to_market_cap, 0.5)
        a.market_cap = 10e9
        self.assertAlmostEqual(a.net_cash_to_market_cap, 1.0)

    def test_profile_and_metric_versions_are_distinct_axes(self):
        # 三个版本号各自演进：画像口径（PROFILE_*）、资产语义引擎
        # （ASSET_SEMANTIC_ENGINE_*）、评分规则（SCORING_*）。评分规则进了
        # EXPERIMENTAL（不再递增 V1.x），另外两个**都停在自己的号上不动**——
        # 那两条轴是数据口径（折价表、负债分类），不是评分规则，不跟着走。
        self.assertEqual(asset_metrics.PROFILE_VERSION, "PROFILE_SCORING_V1.2")
        self.assertEqual(rules.RULE_VERSION, "SCORING_EXPERIMENTAL")
        self.assertEqual(asset_metrics.AssetMetricProvider.missing("x").metric_version,
                         "ASSET_SEMANTIC_ENGINE_V1.0")


if __name__ == "__main__":
    unittest.main()
