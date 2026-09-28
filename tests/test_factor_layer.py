# -*- coding: utf-8 -*-
"""canonical factor 层 + 四维框架的不变量。

这一批（批 1）是**纯新增、零接线**：``factors.py`` / ``dimensions.py`` 不改任何
旧路径，所以这里的测试全部是「新层自己说得通吗」+「它真的忠实反映旧层吗」，
而不是「分数变没变」。分数变没变由 ``TestLegacyScoreUnchanged`` 逐个字面量钉住。
"""
import os
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import dimensions as D          # noqa: E402
from research import engine, factors as F, metric_catalog, pig_exposure  # noqa: E402
from research import peer_groups as PG                                    # noqa: E402
from research import rules, router, valuation_anchors as VA               # noqa: E402
from tests.test_research import _SYNTH_QUOTE, _synth_fin  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _analyze(code="002714", industry="养殖业"):
    m, _q, _f = engine.build_metrics(code, _SYNTH_QUOTE, _synth_fin(industry))
    scored = rules.score_modules(m)
    final = rules.final_score(scored["attributes"], scored["cyclical_position"],
                              m, rules.determine_type(scored["attributes"], industry),
                              None)
    return m, scored, final


def _payload(code="002714", industry="养殖业"):
    m, scored, final = _analyze(code, industry)
    return F.to_payload(F.evaluate(m, scored, None, final))


# --------------------------------------------------------------------------- #
# 1. 声明自洽
# --------------------------------------------------------------------------- #
class TestFactorSpecHygiene(unittest.TestCase):

    def test_factor_ids_are_unique_and_semantics_agree(self):
        self.assertEqual(F.duplicate_factor_ids(), [],
                         "同一个 factor_id 出现了两份不同口径的声明——比没声明更坏")

    def test_every_factor_declares_the_full_semantics(self):
        for spec in F.FACTORS:
            self.assertTrue(spec.factor_id, spec)
            self.assertTrue(spec.display_name, spec.factor_id)
            self.assertTrue(spec.formula, f"{spec.factor_id} 没声明公式")
            self.assertTrue(spec.time_basis, f"{spec.factor_id} 没声明时间口径")
            self.assertTrue(spec.source_semantics, f"{spec.factor_id} 没声明语义来源")
            self.assertIn(spec.direction, F.DIRECTIONS,
                          f"{spec.factor_id} 的方向不在五档词表里")

    def test_every_factor_belongs_to_a_declared_group_and_dimension(self):
        known = {gid for gid, _label in F.FACTOR_GROUPS}
        for spec in F.FACTORS:
            self.assertIn(spec.factor_group, known, spec.factor_id)
            self.assertIn(spec.dimension, D.DIMENSIONS,
                          f"{spec.factor_id} 的组 {spec.factor_group} 没归到四维里")

    def test_raw_metric_ids_are_resolvable(self):
        """``raw_metric_ids`` 只能写目录名或 COMPONENT_UNITS 的键。

        写一个**新名字**就等于制造「同义不同名」——那正是 metric_catalog 存在的
        理由。真要新名字，必须先在目录里登记并写清口径限定词。
        """
        self.assertEqual(F.unresolvable_metric_names(), [],
                         "这些名字既不在 metric_catalog 也不在 COMPONENT_UNITS 里")

    def test_no_metric_name_has_two_factor_owners(self):
        self.assertEqual(F.metric_claimed_twice(), {},
                         "同一个指标名被两个 factor 认领，归属就不确定了")

    def test_neutral_factors_explain_themselves(self):
        """方向为 0 的 factor 必须写明为什么没有方向。"""
        for spec in F.FACTORS:
            if spec.direction == F.DIRECTION_NEUTRAL:
                self.assertTrue(spec.note, f"{spec.factor_id} 无方向却没有说明")


class TestDirectionConsistency(unittest.TestCase):
    """方向断言：canonical 的 direction 必须与旧分量「越大越好 / 越小越好」一致。"""

    #: 人肉核过的方向（38 条，批 2.5 逐条重审）。改动其中任何一条都要先想清
    #: 「这个指标在什么意义上算好」——是越大越好、越小越好、有个合理区间、
    #: 还是看离散状态。
    EXPECTED = {
        "roic_level": F.DIRECTION_HIGHER_BETTER,
        "roe_level": F.DIRECTION_HIGHER_BETTER,
        "earnings_positive_years": F.DIRECTION_HIGHER_BETTER,
        "cfo_net_profit": F.DIRECTION_HIGHER_BETTER,
        "ocf_stability": F.DIRECTION_HIGHER_BETTER,
        "debt_asset_ratio": F.DIRECTION_LOWER_BETTER,
        "goodwill_ratio": F.DIRECTION_LOWER_BETTER,
        "interest_debt_cover": F.DIRECTION_HIGHER_BETTER,
        "revenue_cagr": F.DIRECTION_HIGHER_BETTER,
        "revenue_trend": F.DIRECTION_HIGHER_BETTER,
        "profit_cagr": F.DIRECTION_HIGHER_BETTER,
        "earnings_growth_stability": F.DIRECTION_HIGHER_BETTER,
        "adjusted_net_cash_to_mcap": F.DIRECTION_HIGHER_BETTER,
        "liquidation_to_mcap": F.DIRECTION_HIGHER_BETTER,
        "asset_value_to_mcap": F.DIRECTION_HIGHER_BETTER,
        "asset_liquidity": F.DIRECTION_HIGHER_BETTER,
        "price_to_book": F.DIRECTION_LOWER_BETTER,
        "pe_level": F.DIRECTION_LOWER_BETTER,
        "fcf_yield": F.DIRECTION_HIGHER_BETTER,
        "dividend_yield": F.DIRECTION_HIGHER_BETTER,
        "consecutive_dividend_years": F.DIRECTION_HIGHER_BETTER,
        # 批 2.5：派息率从 NEUTRAL 改成 TARGET_RANGE（它一直是有好坏的）。
        "payout_ratio": F.DIRECTION_TARGET_RANGE,
        "dividend_cover": F.DIRECTION_HIGHER_BETTER,
        "earnings_volatility": F.DIRECTION_HIGHER_BETTER,
        "earnings_sign_switches": F.DIRECTION_HIGHER_BETTER,
        "gross_margin_volatility": F.DIRECTION_HIGHER_BETTER,
        "profit_revenue_sync": F.DIRECTION_HIGHER_BETTER,
        "industry_cyclicality": F.DIRECTION_NEUTRAL,
        "profit_percentile": F.DIRECTION_LOWER_BETTER,
        "margin_percentile": F.DIRECTION_LOWER_BETTER,
        "pb_percentile": F.DIRECTION_LOWER_BETTER,
        "industry_margin_state": F.DIRECTION_LOWER_BETTER,
        "profit_reversal": F.DIRECTION_HIGHER_BETTER,
        "margin_recovery": F.DIRECTION_HIGHER_BETTER,
        "balance_trend": F.DIRECTION_HIGHER_BETTER,
        "capex_cycle": F.DIRECTION_HIGHER_BETTER,
        "risk_level": F.DIRECTION_LOWER_BETTER,
        "industry_prior": F.DIRECTION_NEUTRAL,
        # ---- 批 3 新增：计算型（没有 legacy locus，canonical 层自己算）----
        # 相对价值：分位已是方向调整过的，方向照经济含义写。
        "peer_pe_relative": F.DIRECTION_LOWER_BETTER,
        "peer_pb_relative": F.DIRECTION_LOWER_BETTER,
        "peer_fcf_yield_relative": F.DIRECTION_HIGHER_BETTER,
        "peer_quality_adjusted_valuation": F.DIRECTION_HIGHER_BETTER,
        # MARKET 趋势：强 = 高分。**这一档是「状态描述」不是短线预测**，
        # 曲线以 0 为中性两翼对称（见 RULES_V1["market"]["trend"]）。
        "trend_20d": F.DIRECTION_HIGHER_BETTER,
        "trend_60d": F.DIRECTION_HIGHER_BETTER,
        "trend_120d": F.DIRECTION_HIGHER_BETTER,
        "relative_strength_60d": F.DIRECTION_HIGHER_BETTER,
        "distance_from_120d_high": F.DIRECTION_HIGHER_BETTER,
        # MARKET 关注度：两端都不好，所以是 TARGET_RANGE 而不是单调。
        "amount_percentile_20d": F.DIRECTION_TARGET_RANGE,
        "amount_percentile_60d": F.DIRECTION_TARGET_RANGE,
        "turnover_percentile_20d": F.DIRECTION_TARGET_RANGE,
        "turnover_percentile_60d": F.DIRECTION_TARGET_RANGE,
        "volume_ratio": F.DIRECTION_TARGET_RANGE,
        # MARKET 流动性 / 筹码压力。
        "amount_to_float_cap_20d": F.DIRECTION_HIGHER_BETTER,
        "free_float_market_cap": F.DIRECTION_NEUTRAL,
        "unlock_ratio_12m": F.DIRECTION_LOWER_BETTER,
        "holder_num_change": F.DIRECTION_LOWER_BETTER,
        "holder_reduction_count_12m": F.DIRECTION_LOWER_BETTER,
        "margin_balance_ratio": F.DIRECTION_LOWER_BETTER,
        # ---- 批 4 新增：风险收益（三档锚 + 赔率）----
        # 上行 / 赔率越大越好，下行越小越好。**方向表达的是「这一档对投资者
        # 是好是坏」，不是「价格会怎么走」**——没有一档叫「胜率」。
        "base_upside": F.DIRECTION_HIGHER_BETTER,
        "bear_downside": F.DIRECTION_LOWER_BETTER,
        "risk_reward_ratio": F.DIRECTION_HIGHER_BETTER,
        "anchor_confidence": F.DIRECTION_HIGHER_BETTER,
        # ---- 批 4 新增：猪产业专属（本批只建骨架）----
        # 暴露与单价是**状态描述**（暴露高不等于好，见 spec 的 role_reason）；
        # 完全成本降、单位毛利升、成本优势、出栏、产能才是好坏。
        # 批 9 之后这里没有 ``price_premium``：那一格因子已摘（用户裁定不要
        # 区域溢价），所以它连方向声明都不该再出现在这张表里。
        # PSY / MSY 判不出单调好坏（成活率口径，见 spec 的 note），所以是中性。
        "pig_exposure": F.DIRECTION_NEUTRAL,
        "pig_product_price": F.DIRECTION_NEUTRAL,
        "company_sale_price": F.DIRECTION_NEUTRAL,
        "full_cost": F.DIRECTION_LOWER_BETTER,
        "unit_margin": F.DIRECTION_HIGHER_BETTER,
        "cost_advantage": F.DIRECTION_HIGHER_BETTER,
        "output_volume": F.DIRECTION_HIGHER_BETTER,
        "effective_capacity": F.DIRECTION_HIGHER_BETTER,
        "utilization": F.DIRECTION_NEUTRAL,
        "sow_supply_pressure": F.DIRECTION_LOWER_BETTER,
        "piglet_supply_pressure": F.DIRECTION_LOWER_BETTER,
        "psy": F.DIRECTION_HIGHER_BETTER,
        "msy": F.DIRECTION_HIGHER_BETTER,
        # ---- 批 5 新增：猪周期的**周期位置**型组成因子（§三十七）----
        # 前两个是**位置**不是**好坏**：单位毛利在自身历史里处于低位、售价在
        # 周期区间里处于低位，那是「接近周期底部」，所以越低越好。后三个是
        # 供给出清越深越好、产能兑现越足越好、现金生存力越强越好。
        # （其余 7 条水位读数——完全成本 / 单位毛利 / 出栏 / 产能等——是**原始
        # 读数**，不是组成因子，它们的方向在上面的批 4 段里已经声明过。）
        "margin_position": F.DIRECTION_LOWER_BETTER,
        "sale_price_level": F.DIRECTION_LOWER_BETTER,
        "supply_contraction": F.DIRECTION_HIGHER_BETTER,
        "capacity_delivery": F.DIRECTION_HIGHER_BETTER,
        "financial_survivability": F.DIRECTION_HIGHER_BETTER,
    }

    def test_declared_directions(self):
        actual = {s.factor_id: s.direction for s in F.FACTORS}
        self.assertEqual(sorted(actual), sorted(self.EXPECTED),
                         "增删 factor 必须同一次更新这张方向表——它有存在价值就在于"
                         "每一条都是人核过的")
        for fid, want in self.EXPECTED.items():
            self.assertEqual(actual[fid], want,
                             f"{fid} 的方向声明是 {actual[fid]}，应当是 {want}")

    def test_reciprocal_pair_lives_in_one_factor(self):
        """``PB`` 与 ``净资产/市值`` 互为倒数，是同一个经济因素，不许各占一格。

        这一条直接回答「重复计分」：它们此前在 value 与 cigar_butt 两个模块里
        各占一格，方向相反、含义相同。
        """
        pb = F.FACTOR_INDEX["price_to_book"]
        self.assertEqual(set(pb.raw_metric_ids), {"PB", "净资产/市值"})
        owners = [s.factor_id for s in F.FACTORS
                  if "净资产/市值" in s.raw_metric_ids or "PB" in s.raw_metric_ids]
        self.assertEqual(owners, ["price_to_book"],
                         "倒数对必须只由一个 factor 认领")
        self.assertEqual(pb.direction, F.DIRECTION_LOWER_BETTER,
                         "方向以 PB 为准：PB 越低越好")

    def test_cfo_windows_are_one_factor_with_two_windows_documented(self):
        """3 年累计与 5 年累计是同一个经济因素的两个窗口，方向一致。"""
        spec = F.FACTOR_INDEX["cfo_net_profit"]
        self.assertIn("CFO/净利润（3年累计）", spec.raw_metric_ids)
        self.assertIn("CFO/净利润（5年累计）", spec.raw_metric_ids)
        self.assertIn("现金流匹配", spec.raw_metric_ids)


# --------------------------------------------------------------------------- #
# 2. locus 映射：旧的每一个计分位置都要有归属
# --------------------------------------------------------------------------- #
class TestLocusMapping(unittest.TestCase):

    def test_module_components_match_runtime(self):
        """静态底稿必须与 ``rules`` 实际产出的分量名**逐字相等**。

        这张表是手写的（audit 与测试要在没数据时也能列举全部 locus），所以必须
        有这一条把它钉在实现上。它红了就意味着 ``rules`` 加/删了一格分量而
        canonical 层没跟上——那会让归因静默少算一格权重。
        """
        _m, scored, _final = _analyze()
        self.assertEqual(F.unknown_module_components(scored), [],
                         "rules 里有这些分量，MODULE_COMPONENTS 里没有 → 漏映射")
        self.assertEqual(F.missing_module_components(scored), [],
                         "MODULE_COMPONENTS 里有这些分量，rules 这次没产出 → 表过期了")

    def test_static_table_covers_the_46_declared_loci(self):
        total = sum(len(v) for v in F.MODULE_COMPONENTS.values())
        self.assertEqual(total, 46, "模块分量总数变了，下面几条断言要一起看")
        self.assertEqual(sorted(F.MODULE_COMPONENTS), sorted(F.MODULE_KEYS))

    def test_every_scored_locus_is_mapped(self):
        self.assertTrue(F.assert_all_loci_mapped())

    def test_all_three_layers_are_enumerated(self):
        loci = F.all_loci()
        kinds = {}
        for locus in loci:
            kinds.setdefault(F.parse_locus(locus)[0], []).append(locus)
        self.assertEqual(len(kinds[F.KIND_MODULE]), 46)
        self.assertEqual(len(kinds[F.KIND_TEMPLATE]), 13)
        self.assertEqual(len(kinds[F.KIND_ROUTER]),
                         sum(len(s["components"]) for s in router.MODEL_SPECS.values()))
        self.assertEqual(len(loci), len(set(loci)), "locus 必须唯一")

    def test_template_keys_mirror_rules(self):
        """模板键表镜像 ``rules.template_components``，两边不许分叉。"""
        _m, scored, _final = _analyze()
        produced = set(rules.template_components(scored["attributes"],
                                                 scored["cyclical_position"], _m))
        self.assertEqual(set(F.TEMPLATE_KEY_MODULES), produced)
        declared = set()
        for tmpl in rules.RULES_V1["templates"].values():
            declared |= set(tmpl)
        self.assertTrue(declared <= produced,
                        f"模板用到了 template_components 不产出的键：{declared - produced}")

    def test_router_components_are_all_classified(self):
        """Router 的每个 fit 分量要么是画像分、要么直查得到 factor。"""
        unknown = []
        for model_id, spec in sorted(router.MODEL_SPECS.items()):
            for key, _label, _w, _fn in spec["components"]:
                if key in F.ROUTER_PROFILE_MODULE or key in F.ROUTER_COMPONENT_FACTOR:
                    continue
                unknown.append(F.router_locus(model_id, key))
        self.assertEqual(unknown, [], "这些 Router 分量没归到任何 factor")

    def test_profile_components_resolve_to_their_whole_module(self):
        """画像分 = 整个模块，所以它牵涉该模块的全部 factor。"""
        for key, module_key in F.ROUTER_PROFILE_MODULE.items():
            locus = F.router_locus("QUALITY_COMPOUNDER_V2", key)
            self.assertEqual(set(F.locus_factor_ids(locus)),
                             set(F.module_factors(module_key)), key)

    def test_balance_template_resolves_to_the_module_actually_used(self):
        """``balance`` 有两个候选模块，实际用了哪个要按当时取数还原。"""
        self.assertEqual(F.TEMPLATE_KEY_MODULES["balance"], ("asset_value", "cigar_butt"))
        attrs = {"asset_value": {"score": 70.0}}
        self.assertEqual(F.template_key_module("balance", attrs), "asset_value")
        self.assertEqual(F.template_key_module("balance", {"asset_value": {"score": None}}),
                         "cigar_butt")
        # 两个模块都有分时不许"猜"：按 rules 的 if asset is not None 判定
        self.assertEqual(F.template_key_module("balance", {"asset_value": {"score": 0.0}}),
                         "asset_value")
        # 静态列举取并集，因为两种可能都要算「可能碰到」
        union = set(F.locus_factor_ids(F.template_locus("balance")))
        self.assertEqual(union, set(F.module_factors("asset_value"))
                         | set(F.module_factors("cigar_butt")))

    def test_legacy_mapping_is_total(self):
        mapping = F.legacy_locus_to_factors()
        self.assertEqual(len(mapping), 90)
        for locus, fids in mapping.items():
            self.assertTrue(fids, f"{locus} 没有归属")


# --------------------------------------------------------------------------- #
# 3. 缺失政策：只有 status=="ok" 计分，缺项退出分母
# --------------------------------------------------------------------------- #
class TestHarvestIsReadOnly(unittest.TestCase):

    def test_evaluate_does_not_mutate_its_inputs(self):
        """canonical 层不读原始键，也不许就地改旧结果。

        ``engine`` 里「就地改会让 result_hash 跟着变、给每只股票白加一行快照」
        那条注释防的就是这一类。
        """
        m, scored, final = _analyze()
        before_m = repr(sorted(m))
        before_scored = repr(scored)
        F.evaluate(m, scored, None, final)
        self.assertEqual(repr(sorted(m)), before_m)
        self.assertEqual(repr(scored), before_scored)

    def test_evaluate_is_deterministic(self):
        m, scored, final = _analyze()
        a = F.to_payload(F.evaluate(m, scored, None, final))
        b = F.to_payload(F.evaluate(m, scored, None, final))
        self.assertEqual(a, b)

    def test_no_locus_is_both_missing_and_counted(self):
        payload = _payload()
        for fid, item in payload.items():
            if fid.startswith("_"):
                continue
            if item["score"] is None:
                self.assertFalse(item["eligible"], fid)
                self.assertEqual(item["components"], [] or item["components"])
            else:
                self.assertTrue(item["eligible"], fid)

    def test_eligible_components_carry_a_score_and_a_coverage(self):
        payload = _payload()
        for fid, item in payload.items():
            if fid.startswith("_"):
                continue
            for comp in item["components"]:
                self.assertIn("coverage", comp, f"{fid} 的分量没带 coverage")
                self.assertIn("locus", comp, f"{fid} 的分量没带 locus")
                if comp.get("eligible"):
                    self.assertIsNotNone(comp.get("score"), f"{fid}:{comp['locus']}")

    def test_not_applicable_is_not_the_same_as_missing(self):
        """``not_applicable`` 的 coverage 记 1.0，``missing_data`` 记 0.0。

        两者的分母语义不同（「指标对它没定义」vs「取不到数」），这条判据照
        ``rules._assemble`` 的既有实现走，本层不得另立一套。
        """
        items = [("全部不适用", 1.0, None, 0.0)]
        comps = [("x", None, None, 10.0, "not_applicable", "亏损公司没有 PE"),
                 ("y", None, None, 10.0, "not_applicable", "同上")]
        block = rules.assemble(comps)
        self.assertIsNone(block["score"])
        self.assertEqual(block["completeness"], 0.0)
        for c in block["components"]:
            self.assertEqual(c["coverage"], 1.0, "not_applicable 的 coverage 是满的")
        self.assertEqual(items[0][2], None)

    def test_module_locus_partial_is_excluded(self):
        """模块分量的 ``partial`` **不**计分——这是 ``_assemble`` 的既有政策。"""
        comps = [("a", 1.0, 50.0, 100.0, "ok", None),
                 ("b", 1.0, 50.0, 100.0, "partial", "历史不够长")]
        block = rules.assemble(comps)
        self.assertEqual(block["score"], 50.0)
        self.assertEqual(block["completeness"], 0.5)
        self.assertFalse(block["components"][1]["eligible"])

    def test_template_locus_partial_is_counted(self):
        """模板分量的 ``partial`` **计分**（``final_score`` 的既有政策）。

        这一处旧不一致被刻意保留：改它就会改总分，而本轮是结构重构不是调参。
        翻译在 ``F._template_locus_tuple`` 里，这条测试钉住它没被"顺手修好"。
        """
        entry = {"key": "cashflow_survival", "score": 40.0, "raw": 2.0,
                 "raw_max": 100.0, "coverage": 0.6, "missing": False,
                 "template_weight": 10.0}
        tup = F._template_locus_tuple("template:cashflow_survival", entry)
        self.assertEqual(tup[4], "ok", "partial 在这里必须翻译成计分")
        self.assertEqual(tup[6]["coverage"], 0.6, "但缺口要如实带出")
        block = rules.assemble([tup])
        self.assertEqual(block["score"], 40.0)
        self.assertEqual(block["completeness"], 1.0)   # coverage 在 extra 里，不参与 completeness


class TestComponentNormalization(unittest.TestCase):

    def test_to_pct_is_scale_invariant(self):
        self.assertAlmostEqual(F._to_pct(5.0, 10.0), 50.0)
        self.assertAlmostEqual(F._to_pct(50.0, 100.0), 50.0)
        self.assertIsNone(F._to_pct(None, 10.0))
        self.assertEqual(F._to_pct(7.0, 0), 7.0)
        self.assertEqual(F._to_pct(7.0, None), 7.0)

    def test_every_locus_gets_the_same_max(self):
        """等权：旧满分不参与 canonical 加权（否则混进旧模块的内部政治）。"""
        _m, scored, _final = _analyze()
        for _locus, _key, detail in F.module_loci(scored):
            tup = F._module_locus_tuple(_locus, detail)
            self.assertEqual(tup[3], F.LOCUS_EQUAL_MAX)


# --------------------------------------------------------------------------- #
# 4. coverage / confidence 词表
# --------------------------------------------------------------------------- #
class TestCoverageAndConfidence(unittest.TestCase):

    STATUSES = {"ok", "partial", "missing_data", "not_applicable",
                F.STATUS_DISPLAY_ONLY, F.STATUS_AUDIT_SUPPRESSED}
    LABELS = {"OK", "LOW_CONFIDENCE", "MISSING_DATA", "NOT_APPLICABLE",
              "PARTIAL", "DISPLAY_ONLY", "AUDIT_SUPPRESSED"}

    def test_statuses_are_from_the_declared_vocabulary(self):
        payload = _payload()
        for fid, item in payload.items():
            if fid.startswith("_"):
                continue
            self.assertIn(item["status"], self.STATUSES, fid)

    def test_confidence_labels_are_from_the_declared_vocabulary(self):
        payload = _payload()
        for fid, item in payload.items():
            if fid.startswith("_"):
                continue
            self.assertIn(item["confidence_label"], self.LABELS, fid)

    def test_low_confidence_threshold_is_applied(self):
        payload = _payload()
        for fid, item in payload.items():
            if fid.startswith("_") or not item["eligible"]:
                continue
            want = ("LOW_CONFIDENCE"
                    if item["confidence"] < F.LOW_CONFIDENCE_THRESHOLD else "OK")
            self.assertEqual(item["confidence_label"], want, fid)

    def test_coverage_and_confidence_are_different_numbers(self):
        """两个数不是一回事：coverage 是声明权重覆盖，confidence 再乘分量质量。"""
        payload = _payload()
        got = {fid: (item["coverage"], item["confidence"])
               for fid, item in payload.items()
               if not fid.startswith("_") and item["eligible"]}
        self.assertTrue(any(c > conf for c, conf in got.values()),
                        "夹具里应至少有一个 factor 的 confidence 严格小于 coverage")

    def test_display_only_factors_have_no_score(self):
        payload = _payload()
        item = payload["risk_level"]
        self.assertEqual(item["status"], F.STATUS_DISPLAY_ONLY)
        self.assertIsNone(item["score"])
        self.assertFalse(item["eligible"])
        self.assertEqual(item["confidence_label"], "DISPLAY_ONLY")
        self.assertTrue(item["reason"], "只展示不打分必须写明原因")
        self.assertEqual(item["raw"], "GREEN")

    def test_a_not_applicable_harvest_block_keeps_full_coverage(self):
        """整块都不适用的 harvest 因子，coverage 也是 1.0。

        ``rules._assemble`` 在没有任何可计分分量时提前返回 ``completeness=0.0``
        ——那是**评分覆盖率**的口径。拿它当 factor 的 coverage，会把「这个量在这类
        公司身上没有定义」读成「这一格的数据没抓到」，也与同一份载荷里
        not_applicable 的其余因子（全是 1.0）自相矛盾。亏损公司的 PE 正是这种情形：
        数据齐备，只是 PE 此刻没有定义。
        """
        fin = _synth_fin("养殖业")
        fin["indicators"] = [dict(r, net_profit=-1e8, deduct_profit=-1e8)
                             for r in fin["indicators"]]
        m, _q, _f = engine.build_metrics("002714",
                                        dict(_SYNTH_QUOTE, pe_ttm=-8.0), fin)
        scored = rules.score_modules(m)
        final = rules.final_score(scored["attributes"], scored["cyclical_position"],
                                  m,
                                  rules.determine_type(scored["attributes"], "养殖业"),
                                  None)
        out = F.evaluate(m, scored, None, final, context=_anchor_ctx(m))
        self.assertEqual(out["pe_level"].status, "not_applicable")
        self.assertEqual(out["pe_level"].coverage, 1.0,
                         "「PE 对亏损公司没有定义」不是「PE 没抓到」")
        for fid, item in out.items():
            if item.status == "not_applicable":
                self.assertEqual(item.coverage, 1.0, fid)


# --------------------------------------------------------------------------- #
# 5. 载荷形状
# --------------------------------------------------------------------------- #
class TestPayloadShape(unittest.TestCase):

    def test_meta_key_cannot_collide_with_a_factor_id(self):
        payload = _payload()
        self.assertIn("_meta", payload)
        self.assertFalse([fid for fid in F.FACTOR_IDS if fid.startswith("_")],
                         "factor_id 不许以下划线开头，否则会和 _meta 撞")

    def test_meta_reports_the_factor_count(self):
        payload = _payload()
        self.assertEqual(payload["_meta"]["factor_count"], len(F.FACTORS))
        self.assertEqual(payload["_meta"]["policy_version"], F.POLICY_VERSION)

    def test_every_factor_has_an_entry_with_the_required_fields(self):
        payload = _payload()
        required = {"factor_id", "display_name", "factor_group", "dimension",
                    "raw_metric_ids", "formula", "time_basis", "direction",
                    "source_semantics", "status", "score", "coverage",
                    "confidence", "eligible", "loci", "components", "times_scored",
                    "models_using_it", "router_fit_weight", "confidence_label"}
        for fid in F.FACTOR_IDS:
            self.assertIn(fid, payload)
            self.assertEqual(required - set(payload[fid]), set(), fid)
            self.assertEqual(payload[fid]["factor_id"], fid)

    def test_router_fit_weight_only_mentions_real_models(self):
        payload = _payload()
        for fid, item in payload.items():
            if fid.startswith("_"):
                continue
            for model_id in item["router_fit_weight"]:
                self.assertIn(model_id, router.MODEL_SPECS)

    def test_times_scored_counts_only_non_router_loci(self):
        """``times_scored`` 数的是**结构**位置（``occurrences``），不是取值位置。

        两者在批 2 拆开：指向某个模块的模板键（``template:quality``）结构上确实
        又数了这个因素一次，但它的值是**整个模块**的聚合，记成这个 factor 的值
        就是环（见 ``_harvest`` 的 docstring）。所以结构算、取值不算。
        """
        payload = _payload()
        for fid, item in payload.items():
            if fid.startswith("_"):
                continue
            non_router = [o for o in item["occurrences"]
                          if not o["locus"].startswith(F.KIND_ROUTER + ":")]
            self.assertEqual(item["times_scored"], len(non_router), fid)
            for o in non_router:
                self.assertIn(o["kind"], (F.KIND_MODULE, F.KIND_TEMPLATE), fid)
                self.assertIn("contributes_value", o, fid)

    def test_groups_index_covers_every_factor(self):
        payload = _payload()
        seen = {fid for _gid, g in payload["_meta"]["groups"].items()
                for fid in g["factors"]}
        self.assertEqual(seen, set(F.FACTOR_IDS))

    def test_cfo_factor_is_scored_in_four_places_but_valued_in_three(self):
        """``cfo_net_profit`` 是"同一因素被计多次"的典型：两个模块 + 两个模板键。

        其中 ``template:quality`` 指向的是**整个质量模块**（它的分是模块聚合），
        所以它只进 ``occurrences``。取值的三处：growth 的现金流匹配分量、quality
        的 3 年累计分量、以及从原始序列现算的 ``template:cashflow`` 合成分。
        """
        payload = _payload()
        item = payload["cfo_net_profit"]
        self.assertEqual(item["times_scored"], 4)
        self.assertEqual(sorted(o["locus"] for o in item["occurrences"]),
                         ["module:growth:现金流匹配", "module:quality:CFO/净利润（3年累计）",
                          "template:cashflow", "template:quality"])
        self.assertEqual(sorted(item["loci"]),
                         ["module:growth:现金流匹配", "module:quality:CFO/净利润（3年累计）",
                          "template:cashflow"])
        self.assertEqual(item["times_valued"], 3)
        self.assertEqual([o["contributes_value"] for o in item["occurrences"]
                          if o["locus"] == "template:quality"], [False])

    def test_roic_models_using_it_includes_the_transitive_closure(self):
        """画像分等于整个模块，所以 ROIC 要经 ``growth_profile`` 落到成长模型。"""
        payload = _payload()
        self.assertEqual(payload["roic_level"]["models_using_it"],
                         ["GROWTH_CORE_V2", "QUALITY_COMPOUNDER_V2"])
        self.assertEqual(payload["roic_level"]["router_fit_weight"],
                         {"GROWTH_CORE_V2": 10.0, "QUALITY_COMPOUNDER_V2": 20.0})


# --------------------------------------------------------------------------- #
# 6. 组封顶与 max_reweight_factor 是**配置**
# --------------------------------------------------------------------------- #
def _items(*triples):
    return [(k, w, s, c) for k, w, s, c in triples]


class TestCombineIsConfigurable(unittest.TestCase):

    def test_caps_live_in_the_group_caps_table(self):
        self.assertEqual(D.GROUP_CAPS[F.GROUP_CASHFLOW_QUALITY], 0.25)
        self.assertEqual(D.GROUP_CAPS[F.GROUP_VALUATION], 0.35)
        self.assertEqual(D.GROUP_CAPS[F.GROUP_ASSET_VALUE], 0.35)

    def test_caps_are_not_literals_inside_combine(self):
        """封顶值必须是配置，不许硬编码在函数里（用户点名要求）。"""
        src = (ROOT / "research" / "dimensions.py").read_text(encoding="utf-8")
        body = src.split("def _combine(", 1)[1].split("\ndef ", 1)[0]
        for value in ("0.35", "0.25", "0.60", "0.6", "1.5"):
            self.assertNotIn(value, body,
                             f"_combine 里出现了字面量 {value}——阈值要住在配置表里")

    def test_thresholds_are_named_constants(self):
        self.assertEqual(D.MAX_REWEIGHT_FACTOR, 1.5)
        self.assertEqual(D.OVERVIEW_COVERAGE_FLOOR, 0.60)
        self.assertTrue(0 < D.MAX_REWEIGHT_FACTOR < D.OVERVIEW_COVERAGE_FLOOR * 4)

    def test_cap_below_the_declared_share_shrinks_and_reports(self):
        """cap 小于声明份额时（``GROUP_CAPS`` 的正常用法）多出来的份额如实上报。

        两项声明份额各 0.5，cap=0.5 → 各自只能拿到 0.25，一半权重没人认领。
        分数不变（等权），变的是「这个分由多少权重撑起来」。
        """
        items = _items(("a", 1.0, 80.0, 1.0), ("b", 1.0, 40.0, 1.0))
        score, cov, used, unalloc, capped = D._combine(items, cap=0.5)
        self.assertAlmostEqual(score, 60.0)
        self.assertEqual(cov, 1.0)
        self.assertAlmostEqual(used, 0.5)
        self.assertAlmostEqual(unalloc, 0.5)
        self.assertAlmostEqual(capped, 0.5)

    def test_real_group_caps_do_not_bite_when_everything_is_present(self):
        """默认配置下，数据齐备时上限**一动不动**——它是天花板，不是默认值。"""
        for dim in (D.VALUE, D.BUSINESS):
            items = [(gid, w, 60.0, 1.0) for gid, w in D.GROUP_WEIGHTS[dim].items() if w]
            declared = sum(w for _g, w, _s, _c in items)
            _s, _c, used, unalloc, capped = D._combine(
                items, cap=D.MAX_REWEIGHT_FACTOR, caps=D.GROUP_CAPS)
            self.assertAlmostEqual(used, 1.0, places=4, msg=dim)
            self.assertAlmostEqual(unalloc, 0.0, places=4, msg=dim)
            self.assertAlmostEqual(capped, 0.0, places=4, msg=dim)
            self.assertAlmostEqual(declared, 1.0, places=4, msg=dim)

    def test_cap_bites_when_a_missing_sibling_would_inflate_the_rest(self):
        """缺一项 → 剩下的按比例重分配；``max_reweight_factor`` 是天花板。

        这是「禁止『缺一项，其他项无限抬权重』」的机器形式。

        a 声明份额 3/4、b 是 1/4，b 没数据 → a 想从 0.75 涨到 1.0，即抬高
        4/3 = 1.333 倍。天花板 1.2 倍 → a 只能拿到 0.75 × 1.2 = 0.9，
        剩下 0.1 份额**没人认领**，如实上报。
        """
        items = _items(("a", 3.0, 90.0, 1.0), ("b", 1.0, None, 0.0))
        _s, cov, used, unalloc, capped = D._combine(items, cap=1.2)
        self.assertAlmostEqual(cov, 0.75, places=4)      # 声明权重里 3/4 有数据
        self.assertAlmostEqual(used, 0.9, places=4)      # 0.75 × 1.2
        self.assertAlmostEqual(unalloc, 0.1, places=4)
        self.assertAlmostEqual(capped, 0.1, places=4)

    def test_cap_does_not_bite_when_it_is_above_the_inflation(self):
        """天花板高于实际抬高倍数时一动不动——封顶只在该咬合的时候咬合。"""
        items = _items(("a", 3.0, 90.0, 1.0), ("b", 1.0, None, 0.0))
        _s, _cov, used, unalloc, capped = D._combine(items, cap=1.5)
        self.assertAlmostEqual(used, 1.0, places=4)      # 4/3 = 1.333 < 1.5
        self.assertAlmostEqual(unalloc, 0.0, places=4)
        self.assertAlmostEqual(capped, 0.0, places=4)

    def test_no_inflation_at_all_when_nothing_is_missing(self):
        items = _items(("a", 3.0, 90.0, 1.0), ("b", 1.0, 40.0, 1.0))
        _s, cov, used, unalloc, capped = D._combine(items, cap=1.5)
        self.assertEqual(cov, 1.0)
        self.assertAlmostEqual(used, 1.0, places=4)
        self.assertAlmostEqual(unalloc, 0.0, places=4)
        self.assertAlmostEqual(capped, 0.0, places=4)

    def test_a_group_with_nothing_scored_allocates_nothing(self):
        items = _items(("a", 1.0, None, 0.0), ("b", 1.0, None, 0.0))
        score, cov, used, unalloc, capped = D._combine(items)
        self.assertIsNone(score)
        self.assertEqual((cov, used, unalloc, capped), (0.0, 0.0, 1.0, 0.0))

    def test_group_caps_are_measured_as_a_share(self):
        """``GROUP_CAPS`` 是份额上限：valuation 的声明权重 0.35，被抬也只能到 0.35。"""
        items = _items((F.GROUP_VALUATION, 0.35, 60.0, 1.0),
                       (F.GROUP_ASSET_VALUE, 0.35, 80.0, 1.0),
                       (F.GROUP_SHAREHOLDER_RETURN, 0.30, None, 0.0))
        _s, cov, used, unalloc, capped = D._combine(
            items, cap=D.MAX_REWEIGHT_FACTOR, caps=D.GROUP_CAPS)
        self.assertAlmostEqual(cov, 0.70, places=4)
        self.assertLessEqual(used, 0.70 + 1e-9)
        self.assertGreaterEqual(capped, 0.0)

    def test_weights_used_is_never_above_the_declared_share(self):
        items = _items(("a", 0.5, 10.0, 1.0), ("b", 0.5, 20.0, 1.0))
        _s, _c, used, unalloc, _cap = D._combine(items, cap=D.MAX_REWEIGHT_FACTOR)
        self.assertLessEqual(used, 1.0 + 1e-9)
        self.assertGreaterEqual(unalloc, 0.0)


# --------------------------------------------------------------------------- #
# 7. 四维与总览
# --------------------------------------------------------------------------- #
class TestDimensionTables(unittest.TestCase):

    def test_dimension_weight_rows_sum_to_one(self):
        self.assertEqual(D.invalid_dimension_weights(), [])

    def test_every_router_model_has_four_dimension_weights(self):
        self.assertEqual(D.missing_model_dimension_weights(), [],
                         "Router 开新模型就必须同一次配四维权重")

    def test_no_group_is_omitted_from_the_weight_table(self):
        """省略一个组比配 0 更危险：0 能被探测，省略不能。"""
        self.assertEqual(D.groups_missing_from_weights(), [])

    def test_no_group_has_factors_but_zero_weight(self):
        self.assertEqual(D.groups_without_weight(), [],
                         "有 factor 成员却 0 权重的组——落地时忘了改权重")

    def test_relative_value_and_risk_reward_are_both_landed(self):
        """批 3 落地 relative_value，批 4 落地 risk_reward。两组都**有成员且有权重**。

        这条以前是「一个已落地、一个显式 0」的哨兵（批 3 时 risk_reward 还没到）。
        批 4 把后半句也变成假的了，所以哨兵到期，改成两边同一条判据：
        **权重不为 0 ⇔ 组里有 factor**。这个等价关系才是真正要钉的东西——
        权重不为 0 而成员为空是空转的权重，成员齐全而权重为 0 是白算的因子。
        """
        for dim, gid in ((D.VALUE, F.GROUP_RELATIVE_VALUE),
                         (D.OPPORTUNITY, F.GROUP_RISK_REWARD)):
            self.assertGreater(D.GROUP_WEIGHTS[dim][gid], 0.0, gid)
            self.assertTrue(F.factors_in_group(gid),
                            f"{gid}：权重不为 0 却没有 factor——空转的权重")
        # 用户裁定 §十九：Risk/Reward 一上来不许超过 0.35。它是本批最新落地的一块，
        # 没有任何回测支撑它占更大比重。
        self.assertLessEqual(D.GROUP_WEIGHTS[D.OPPORTUNITY][F.GROUP_RISK_REWARD], 0.35)

    def test_dimension_groups_are_derived_from_the_factor_layer(self):
        for dim, groups in D.DIMENSION_GROUPS.items():
            for gid in groups:
                self.assertEqual(F.GROUP_DIMENSION.get(gid), dim, gid)
        covered = {gid for groups in D.DIMENSION_GROUPS.values() for gid in groups}
        self.assertEqual(covered, {gid for gid, _l in F.FACTOR_GROUPS},
                         "每个组都必须归到四维中的一维")

    def test_characteristic_groups_are_documented(self):
        for gid in (F.GROUP_CYCLICAL_EXPOSURE, F.GROUP_MARKET_TREND,
                    F.GROUP_MARKET_ATTENTION, F.GROUP_MARKET_OVERHANG):
            self.assertIn(gid, D.CHARACTERISTIC_GROUPS)
            self.assertTrue(D.CHARACTERISTIC_GROUPS[gid])


class TestCompose(unittest.TestCase):

    def setUp(self):
        self.payload = _payload()
        self.route = {"primary_model": "CYCLICAL_CORE_V2", "route_status": "CLEAR"}
        self.out = D.evaluate(self.payload, self.route)

    def test_four_dimensions_are_produced(self):
        self.assertEqual(sorted(self.out["dimensions"]), sorted(D.DIMENSIONS))

    def test_market_is_in_the_overview(self):
        """批 3 起 MARKET 进总览分（用户批 3 目标：「让 MARKET 真正进入总览」）。

        批 2 这条断言的是**反面**（``assertNotIn``）。现在反过来，是因为批 2 的
        前提没了：那时 MARKET 一个可计分的 factor 都没有，拿一块全空的维度和三块
        有数的维度加权等于把总览分稀释成噪声；现在四组都真打分。
        """
        self.assertIn(D.MARKET, D.OVERVIEW_DIMENSIONS)
        self.assertTrue(self.out["dimensions"][D.MARKET]["in_overview"])
        self.assertIn(D.MARKET, self.out["overview"]["weights"])

    def test_overview_weights_sum_to_one_with_all_four_blocks(self):
        weights = self.out["overview"]["weights"]
        # payload 里的权重是逐项 round(4) 过的，所以只比到 3 位
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=3)
        self.assertEqual(set(weights), set(D.OVERVIEW_DIMENSIONS))
        # 周期框架给 MARKET 0.20（spec §23 的四维权重表）。写死这个字面量是有意的：
        # 它是规格里点名的数，改它就得先改规格。
        self.assertAlmostEqual(weights[D.MARKET], 0.20, places=3)

    def test_overview_notes_name_the_market_weight(self):
        notes = " ".join(self.out["overview"]["notes"])
        self.assertIn("MARKET", notes)
        # 含 MARKET 之后必须**说出含了多少**：否则用户会把一个含市场状态的数
        # 当成纯基本面分用。
        self.assertIn("0.20", notes)
        self.assertIn("不是", notes)

    def test_market_without_data_exits_the_score_instead_of_scoring_zero(self):
        """MARKET 没数据时整块退出分子，**不是**以 0 分拖低总分（spec §24）。

        这是「不要强行 0 分」的机器形式：0 分是一个测量结果，而这里没有测量。
        退出的那份权重进 ``unallocated``，**不重分配给其余三块**。
        """
        dim = self.out["dimensions"][D.MARKET]
        self.assertIsNone(dim["score"])
        self.assertTrue(dim["market_missing"])
        overview = self.out["overview"]
        self.assertFalse(overview["market_in_overview"])
        self.assertEqual(overview["market_weight_included"], 0.0)
        # 权重没被别块分掉：unallocated 至少含 MARKET 那一份
        self.assertGreaterEqual(overview["unallocated_weight"], 0.20 - 1e-6)

    def test_market_dimension_score_is_none_and_never_a_zero(self):
        """MARKET 现在没数据，就得是 None（不是 0 分）——0 分会被读成「市场极差」。"""
        self.assertIsNone(self.out["dimensions"][D.MARKET]["score"])

    def test_dimension_coverage_is_a_share(self):
        for dim in D.DIMENSIONS:
            d = self.out["dimensions"][dim]
            self.assertGreaterEqual(d["coverage"], 0.0)
            self.assertLessEqual(d["coverage"], 1.0 + 1e-9)

    def test_dimension_weights_used_reflect_the_signals_declared_weights(self):
        d = self.out["dimensions"][D.BUSINESS]
        self.assertAlmostEqual(d["declared_weight"], 1.0, places=4)
        self.assertTrue(d["effective_weight"] > 0)

    def test_groups_are_reported_per_dimension(self):
        for dim, groups in self.out["groups"].items():
            self.assertEqual(set(groups), set(D.DIMENSION_GROUPS[dim]), dim)
            for gid, g in groups.items():
                self.assertIn("scored_factors", g, gid)
                self.assertIn("unallocated_weight", g, gid)

    def test_floor_removes_a_low_coverage_dimension_without_redistributing(self):
        """coverage 低于地板 → 该维度整块退出分子，权重记进 excluded。"""
        payload = _payload()
        for fid, item in payload.items():
            if fid.startswith("_") or item["dimension"] != D.OPPORTUNITY:
                continue
            item["score"] = None
            item["eligible"] = False
        out = D.evaluate(payload, self.route)
        self.assertIsNone(out["dimensions"][D.OPPORTUNITY]["score"])
        self.assertIn(D.OPPORTUNITY, out["overview"]["excluded"])
        self.assertLess(out["overview"]["weights_used"], 1.0)
        self.assertGreater(out["overview"]["unallocated_weight"], 0.0)

    def test_frame_follows_the_primary_model(self):
        self.assertEqual(self.out["research_frame"]["frame"], D.FRAME_CYCLICAL)

    def test_market_groups_are_scored_only_when_a_score_exists(self):
        for gid, g in self.out["groups"][D.MARKET].items():
            self.assertIsNone(g["score"], gid)
            self.assertEqual(g["scored_factors"], [], gid)


class TestResearchFrame(unittest.TestCase):

    def test_seven_frames_are_declared(self):
        self.assertEqual(len(D.RESEARCH_FRAMES), 7)

    def test_labels_are_unique_and_non_empty(self):
        labels = list(D.RESEARCH_FRAMES.values())
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(all(labels))

    def test_every_model_maps_to_a_declared_frame(self):
        for model_id in router.MODEL_SPECS:
            self.assertIn(model_id, D._MODEL_TO_FRAME, model_id)

    def test_untrusted_route_never_claims_a_specialized_frame(self):
        """兜底 / 数据不足时挂专属框架的名字就是在撒谎。"""
        for status in ("FALLBACK", "INSUFFICIENT_DATA"):
            got = D.research_frame("CYCLICAL_CORE_V2", status)
            self.assertEqual(got["frame"], D.FRAME_GENERAL, status)
            self.assertIn(status, got["reason"])

    def test_missing_primary_model_is_general(self):
        self.assertEqual(D.research_frame(None, "CLEAR")["frame"], D.FRAME_GENERAL)

    def test_unknown_model_is_general_not_an_exception(self):
        self.assertEqual(D.research_frame("NOT_A_MODEL", "CLEAR")["frame"],
                         D.FRAME_GENERAL)


# --------------------------------------------------------------------------- #
# 8. 旧分数逐字不变（本轮不动任何权重 / 点表 / 阈值）
# --------------------------------------------------------------------------- #
class TestLegacyScoreUnchanged(unittest.TestCase):
    """改前先跑一次录下来的字面量。

    这 5 个 fixture 覆盖了 5 个不同模板与行业（含猪、汽车零部件、水泥、银行）。
    任何一个不等，就说明本轮**改了旧路径的分数**——立刻停下查，不许「看着差不多
    就放过」。
    """

    #: (code, industry) → (final score, template, type, completeness)
    EXPECTED = {
        ("002714", "养殖业"): (53.08, "周期价值型", "cyclical", 1.0),
        ("600741", "汽车零部件"): (64.12, "价值型", "value", 1.0),
        ("600585", "水泥"): (64.12, "价值型", "value", 1.0),
        ("600036", "银行Ⅱ"): (62.88, "价值型", "value", 1.0),
        ("601163", "轮胎"): (64.12, "价值型", "value", 1.0),
    }

    def test_final_score_literals(self):
        for (code, industry), (score, tmpl, typ, comp) in sorted(self.EXPECTED.items()):
            _m, _scored, final = _analyze(code, industry)
            self.assertEqual(final["score"], score,
                             f"{code}/{industry} 的旧总分变了")
            self.assertEqual(final["template"], tmpl, f"{code}/{industry} 的模板变了")
            self.assertEqual(final["type"], typ, f"{code}/{industry} 的判型变了")
            self.assertEqual(final["completeness"], comp,
                             f"{code}/{industry} 的 completeness 变了")

    def test_runtime_version_is_still_experimental(self):
        self.assertEqual(rules.RULE_VERSION, "SCORING_EXPERIMENTAL",
                         "本轮不升 RULE_VERSION——一升就把 204 条快照标成 legacy")

    def test_assemble_alias_is_the_same_function(self):
        self.assertIs(rules.assemble, rules._assemble)


# --------------------------------------------------------------------------- #
# 9. metric_catalog 补登记没破坏目录自洽
# --------------------------------------------------------------------------- #
class TestCatalogAdditions(unittest.TestCase):

    def test_catalog_is_still_self_consistent(self):
        self.assertEqual(metric_catalog.duplicate_display_names(), [])
        self.assertEqual(metric_catalog.duplicate_metric_ids(), [])

    def test_newly_registered_names_are_present(self):
        names = set(metric_catalog.display_names())
        for name in ("CFO/净利润（5年累计）", "资本开支/营收", "行业周期先验", "风险等级"):
            self.assertIn(name, names)

    def test_no_new_name_is_a_retired_bare_name(self):
        for spec in metric_catalog.CATALOG:
            self.assertNotIn(spec.display_name, metric_catalog.RETIRED_AMBIGUOUS_NAMES)


# --------------------------------------------------------------------------- #
# 10. factor_role：只有 SCORE 能进维度分（批 2 的第一件事）
# --------------------------------------------------------------------------- #
class TestFactorRole(unittest.TestCase):
    """角色与状态是两件事：``factor_role`` 说「这个量是不是好坏」，
    ``status`` 说「这次取到没有」。混起来就会出现「因为它缺失所以它不重要」。"""

    def test_roles_are_a_closed_vocabulary(self):
        self.assertEqual(sorted(F.FACTOR_ROLES),
                         [F.ROLE_APPLICABILITY, F.ROLE_CHARACTERISTIC, F.ROLE_SCORE])
        for spec in F.FACTORS:
            self.assertIn(spec.factor_role, F.FACTOR_ROLES, spec.factor_id)

    def test_only_score_contributes_to_the_dimension(self):
        for spec in F.FACTORS:
            want = spec.factor_role == F.ROLE_SCORE
            self.assertEqual(spec.contributes_to_score, want, spec.factor_id)

    def test_a_factor_without_direction_is_never_a_score(self):
        """**说不出好坏 ⇒ 不是 SCORE。** 判据是 ``direction`` 五档里那一档
        「只描述特征」的（``NEUTRAL``），不是「非单调」——``TARGET_RANGE`` 与
        ``STATE_BASED`` 说得出「什么样算好」，所以它们**可以**是 SCORE。
        """
        bad = []
        for spec in F.FACTORS:
            if spec.direction == F.DIRECTION_NEUTRAL and spec.factor_role == F.ROLE_SCORE:
                bad.append(spec.factor_id)
        self.assertEqual(bad, [], f"这些 factor 没有方向却是 SCORE：{bad}")
        self.assertEqual(tuple(F.role_inconsistencies()), ())

    def test_characteristic_factors_are_the_expected_set(self):
        got = set(F.CHARACTERISTIC_FACTORS)
        groups = {F.FACTOR_INDEX[f].factor_group for f in got}
        self.assertIn(F.GROUP_CYCLICAL_EXPOSURE, groups,
                      "周期暴露整组必须是属性型——它描述「这家公司有多周期」，"
                      "不描述「现在值不值得买机会」")
        self.assertNotIn(F.GROUP_CYCLICAL_OPPORTUNITY, groups,
                         "机会组是 SCORE，不能混进属性型")
        #: 批 2.5 起 **7 条**（派息率已改回 SCORE，见 TestDirectionSemantics）。
        for fid in ("earnings_volatility", "earnings_sign_switches",
                    "gross_margin_volatility", "profit_revenue_sync",
                    "industry_cyclicality", "industry_prior", "capex_cycle"):
            self.assertIn(fid, got, f"{fid} 应该被标为 CHARACTERISTIC")
        self.assertEqual(sorted(got), sorted(F.CHARACTERISTIC_FACTORS))
        self.assertNotIn("payout_ratio", got,
                         "派息率有明确的好坏判断（有合理区间），不是属性")

    def test_reserved_roles_do_not_contradict_the_catalogue(self):
        """预留名（猪暴露 / 公司规模之类）以后一旦进目录，角色必须与本表一致——
        否则「这个量是属性」这件事会在两个地方各说一句。"""
        self.assertEqual(F.reserved_role_conflicts(), [])
        for fid, role in sorted(F.RESERVED_ROLES.items()):
            spec = F.FACTOR_INDEX.get(fid)
            if spec is not None:
                self.assertEqual(spec.factor_role, role, fid)

    def test_characteristic_contribution_is_zero_in_a_real_payload(self):
        out = _evaluated()
        for fid in F.CHARACTERISTIC_FACTORS:
            item = out["factors"][fid]
            self.assertEqual(item["contribution"], 0.0, fid)
            self.assertFalse(item["contributes_to_dimension"], fid)
            self.assertEqual(item["effective_weight"], 0.0, fid)

    def test_score_factor_with_a_nonzero_score_contributes(self):
        out = _evaluated()
        got = [(fid, i["contribution"]) for fid, i in out["factors"].items()
               if i.get("eligible") and i.get("score") and i.get("contribution")]
        self.assertTrue(got, "夹具里应该至少有一个 SCORE 型 factor 真的贡献了点数")
        for fid, pts in got:
            self.assertTrue(F.FACTOR_INDEX[fid].contributes_to_score, fid)
            self.assertGreater(pts, 0.0, fid)

    def test_dimension_score_is_the_sum_of_group_contributions(self):
        """一个分必须能拆到具体 factor 上（spec §17：不许只有光秃秃的 BUSINESS=78）。"""
        out = _evaluated()
        for dim in D.DIMENSIONS:
            d = out["dimensions"][dim]
            if d["score"] is None:
                continue
            total = sum(g["contribution"] for g in out["groups"][dim].values())
            self.assertAlmostEqual(total, d["score"] * d["effective_weight"], places=2, msg=dim)
            factor_pts = sum(i["contribution"] for i in out["factors"].values()
                             if i.get("dimension") == dim)
            self.assertAlmostEqual(factor_pts, total, places=2, msg=dim)


# --------------------------------------------------------------------------- #
# 11. 周期暴露只当门，不当分项（spec §2 §3 §26）
# --------------------------------------------------------------------------- #
def _evaluated(code="002714", industry="养殖业", model="CYCLICAL_CORE_V2",
               exposures=None, opportunities=None, drop_exposure=False,
               prior=None, route_status="CLEAR"):
    """跑一次真实的四维评估，可选地把暴露 / 机会两组的读数改掉再算。

    直接改载荷里的 ``score`` 是有意的：这里要问的是「**如果**暴露读数是 X」，
    而不是「怎么让 fixture 产出 X」——后者测的就变成夹具了。

    ``drop_exposure`` 把整组暴露的读数抹掉（``score = None``），用来测兜底路径；
    ``prior`` 直接给 ``industry_prior`` 一个档位码（离线夹具里没有 Router 的
    evidence，所以它的读数要手给）。
    """
    payload = _payload(code, industry)
    route = {"primary_model": model, "route_status": route_status}
    for group_id, score in (("cyclical_exposure", exposures),
                            ("cyclical_opportunity", opportunities)):
        if score is None:
            continue
        for fid, item in payload.items():
            if fid.startswith("_") or item["factor_group"] != group_id:
                continue
            if item["status"] in ("ok", "partial"):
                item["score"] = float(score)
    if drop_exposure:
        for fid, item in payload.items():
            if fid.startswith("_") or item["factor_group"] != "cyclical_exposure":
                continue
            item["score"] = None
            item["status"] = "missing_data"
    if prior is not None:
        payload["industry_prior"]["score"] = float(prior)
        payload["industry_prior"]["status"] = "ok"
    return D.evaluate(payload, route)


def _cycle_gate_report(out):
    return [g for g in out["applicability_gates"]
            if g["target_group"] == F.GROUP_CYCLICAL_OPPORTUNITY][0]


class TestExposureGate(unittest.TestCase):

    def test_gate_bands_are_configured_not_hardcoded(self):
        gate = D.gate_for_group(F.GROUP_CYCLICAL_OPPORTUNITY)
        self.assertIsNotNone(gate)
        self.assertEqual(D.applicability_multiplier(gate, 10.0), 0.25)
        self.assertEqual(D.applicability_multiplier(gate, 29.9), 0.25)
        self.assertEqual(D.applicability_multiplier(gate, 30.0), 0.50)
        self.assertEqual(D.applicability_multiplier(gate, 50.0), 0.75)
        self.assertEqual(D.applicability_multiplier(gate, 70.0), 1.00)
        self.assertEqual(D.applicability_multiplier(gate, 95.0), 1.00)
        self.assertEqual(D.gates_with_unknown_groups(), [])

    def test_missing_exposure_is_not_the_maximum_multiplier(self):
        """**批 2.5 改掉的语义。** 批 2 把「暴露读数取不到」判成 ×1.00，而 ×1.00
        是这张表里**最大**的倍数——于是「越不知道有多周期，周期机会越该说了算」，
        方向正好反了。缺证据要退回弱适用性，不是发最高权重。
        """
        gate = D.gate_for_group(F.GROUP_CYCLICAL_OPPORTUNITY)
        # 分档函数只处理「读数取到了」的情形；取不到时它**不给一个数**，
        # 逼调用方走 cycle_applicability 的兜底（给 1.00 才会让人忽略兜底）。
        self.assertIsNone(D.applicability_multiplier(gate, None))

        out = _evaluated(drop_exposure=True, prior=100.0)
        report = _cycle_gate_report(out)
        self.assertIsNone(report["source_score"])
        self.assertEqual(report["applicability_source"],
                         D.APPT_SOURCE_FRAME_PRIOR)
        # 夹具默认 CYCLICAL_CORE_V2 → 周期框架；强先验 → ×1.00（兜底也允许到 1.00，
        # 但那是**判出来的**，并且必须带来源与证据强度）。
        self.assertEqual(report["applicability_multiplier"], 1.00)
        self.assertEqual(report["applicability_confidence"], 0.5)
        self.assertEqual(report["industry_prior_tier"], F.PRIOR_TIER_STRONG)

    def test_missing_exposure_with_unknown_evidence_uses_the_lowest_band(self):
        """连兜底都判不出来（路由不可信）→ 默认最低档，并明说这是默认值。"""
        out = _evaluated(drop_exposure=True, prior=100.0,
                         route_status="FALLBACK")
        report = _cycle_gate_report(out)
        self.assertEqual(report["applicability_source"], D.APPT_SOURCE_DEFAULT)
        self.assertEqual(report["applicability_multiplier"],
                         D.CYCLE_APPLICABILITY_DEFAULT)
        self.assertEqual(report["applicability_confidence"], 0.0)
        self.assertIn("默认值", report["applicability_reason"])

    def test_missing_exposure_falls_back_is_not_always_the_same_number(self):
        """兜底不是一个常数：框架是不是周期 + 行业先验档位都会改它。

        如果它是个常数，那和一档死阈值没区别——这张表的意义就在于
        「有证据的缺」与「没证据的缺」给不同的数。
        """
        seen = {}
        for model, prior in (("CYCLICAL_CORE_V2", 100.0),
                             ("CYCLICAL_CORE_V2", 15.0),
                             ("GENERAL_VALUE_V2", 100.0),
                             ("GENERAL_VALUE_V2", 50.0),
                             ("GENERAL_VALUE_V2", 15.0)):
            r = _cycle_gate_report(_evaluated(drop_exposure=True, model=model,
                                              prior=prior))
            seen[(model, prior)] = r["applicability_multiplier"]
        self.assertEqual(seen[("CYCLICAL_CORE_V2", 100.0)], 1.00)
        self.assertEqual(seen[("CYCLICAL_CORE_V2", 15.0)], 0.50)
        self.assertEqual(seen[("GENERAL_VALUE_V2", 100.0)], 0.50)
        self.assertEqual(seen[("GENERAL_VALUE_V2", 50.0)], 0.35)
        self.assertEqual(seen[("GENERAL_VALUE_V2", 15.0)], 0.25)
        self.assertGreater(len(set(seen.values())), 1)

    def test_fallback_table_covers_every_combination(self):
        """漏一个 (框架, 档位) 组合只会在「暴露缺失 + 恰好那个档位」时才炸，
        是最难在生产里碰到的那种 KeyError——所以在配置层查掉。"""
        self.assertEqual(D.cycle_fallback_config_errors(), [])
        self.assertEqual(D.gates_with_unknown_groups(), [])

    def test_high_exposure_does_not_raise_the_opportunity_score(self):
        """**「周期性强」不再自动意味着「周期机会高」。**

        暴露 90 分、机会 10 分：OPPORTUNITY 必须完全跟着机会那一块走，
        暴露的 90 分**一分都不许进分**（它的维度权重是 0.00）。
        """
        out = _evaluated(exposures=90.0, opportunities=10.0)
        groups = out["groups"][D.OPPORTUNITY]
        self.assertEqual(groups[F.GROUP_CYCLICAL_EXPOSURE]["score"], 90.0,
                         "暴露读数照样要显示出来")
        self.assertEqual(groups[F.GROUP_CYCLICAL_EXPOSURE]["declared_weight"], 0.00)
        self.assertEqual(groups[F.GROUP_CYCLICAL_EXPOSURE]["contribution"], 0.0)
        self.assertFalse(groups[F.GROUP_CYCLICAL_EXPOSURE]["contributes_to_dimension"])
        self.assertEqual(groups[F.GROUP_CYCLICAL_OPPORTUNITY]["score"], 10.0)
        self.assertEqual(groups[F.GROUP_CYCLICAL_OPPORTUNITY]["applicability_multiplier"],
                         1.00)
        self.assertLess(out["dimensions"][D.OPPORTUNITY]["score"], 50.0,
                        "暴露高不该把 OPPORTUNITY 抬起来")

    def test_low_exposure_keeps_the_score_but_shrinks_the_weight(self):
        """暴露 10 分、机会 90 分：机会的**分**不变，它的**权重**被门压小。"""
        out = _evaluated(exposures=10.0, opportunities=90.0)
        groups = out["groups"][D.OPPORTUNITY]
        opp = groups[F.GROUP_CYCLICAL_OPPORTUNITY]
        turn = groups[F.GROUP_TURNAROUND]
        self.assertEqual(opp["score"], 90.0, "分不变——门改的是权重，不是分")
        self.assertEqual(opp["applicability_multiplier"], 0.25)
        # 声明权重从配置读，不写死数字：批 4 把 OPPORTUNITY 的组权重从
        # 0.45/0.25 改成 0.35/0.20/0.30，写死的字面量会在每一批重演一次这件事。
        # 这一格要钉的是**语义**（门乘在声明权重上），不是那三个数。
        opp_declared = D.GROUP_WEIGHTS[D.OPPORTUNITY][F.GROUP_CYCLICAL_OPPORTUNITY]
        turn_declared = D.GROUP_WEIGHTS[D.OPPORTUNITY][F.GROUP_TURNAROUND]
        self.assertAlmostEqual(opp["declared_weight_after_applicability"],
                               opp_declared * 0.25, places=4)
        # 重分配之后两个组的**绝对份额**都会变（它们要在和里归一化），所以判据是
        # 比值：门把「机会 : 反转」的比值压到原来的 0.25 倍。
        self.assertAlmostEqual(
            opp["effective_weight"] / turn["effective_weight"],
            (opp_declared / turn_declared) * 0.25, places=2)

    def test_exposure_group_is_exempt_from_the_weight_detector(self):
        """0.00 是配好的结论，不是「忘了配」——探测器不该为它响。"""
        self.assertIn(F.GROUP_CYCLICAL_EXPOSURE, D.GATE_ONLY_GROUPS)
        self.assertNotIn((D.OPPORTUNITY, F.GROUP_CYCLICAL_EXPOSURE),
                         D.groups_without_weight())
        self.assertEqual(D.groups_without_weight(), [])

    def test_opportunity_composition_is_exposure_gate_plus_opportunity_plus_turnaround(self):
        """OPPORTUNITY 的成员是**配全了**的：门 + 机会 + 反转 + 风险收益 + 猪产业。

        暴露门的 0 权重必须在场（``GATE_ONLY_GROUPS`` 豁免）——「不在列表里」和
        「权重是 0」是两回事，前者是漏配。猪企组批 5 起拿真权重 0.15，所以它同时
        从 ``PENDING_DATA_GROUPS`` 里出去了（那是当初写下的处置路径，不是新的豁免）。
        """
        out = _evaluated()
        weights = out["groups"][D.OPPORTUNITY]
        self.assertEqual(sorted(weights),
                         sorted([F.GROUP_CYCLICAL_EXPOSURE, F.GROUP_CYCLICAL_OPPORTUNITY,
                                 F.GROUP_RISK_REWARD, F.GROUP_PIG_INDUSTRY,
                                 F.GROUP_TURNAROUND]))
        self.assertAlmostEqual(weights[F.GROUP_RISK_REWARD]["declared_weight"], 0.30,
                               places=4,
                               msg="批 4 起 risk_reward 正式占权重（配在 RULES/组表里）")
        self.assertAlmostEqual(weights[F.GROUP_PIG_INDUSTRY]["declared_weight"], 0.15,
                               places=4,
                               msg="批 5 起猪企组拿真权重（数据层已落地）")
        self.assertNotIn(F.GROUP_PIG_INDUSTRY, D.PENDING_DATA_GROUPS,
                         "拿了真权重就不该再挂在「数据还没到」的豁免里")
        self.assertNotIn(F.GROUP_RELATIVE_VALUE, weights,
                         "relative_value 归 VALUE，不在 OPPORTUNITY")

    def test_relative_value_sits_in_value_with_real_weight(self):
        """批 3 起 relative_value 归 VALUE 且拿真权重（占位期已结束）。

        占位期这条断言 ``declared_weight == 0.0``。现在它必须是 VALUE 里
        一个**能计分的**组——权重大于 0、且有 factor 成员，否则就是「配了权重
        但没人拿分」，那和占位没区别，只是更难发现。
        """
        out = _evaluated()
        value = out["groups"][D.VALUE]
        self.assertIn(F.GROUP_RELATIVE_VALUE, value)
        self.assertGreater(value[F.GROUP_RELATIVE_VALUE]["declared_weight"], 0.0)
        self.assertTrue(F.factors_in_group(F.GROUP_RELATIVE_VALUE))
        self.assertEqual(D.groups_without_weight(), [])


# --------------------------------------------------------------------------- #
# 12. engine 并行接线的 Legacy 回归（spec §8 §9）
# --------------------------------------------------------------------------- #
class TestWiredLegacyUnchanged(unittest.TestCase):
    """走**真实引擎**（含 Router 选模板）的那条链，逐字钉住接线后的字面量。

    与 ``TestLegacyScoreUnchanged`` 的区别：那一条走 ``final_score(..., route=None)``
    的兜底判型，这一条走 ``primary_model`` 决定模板的那条路（2026-09-24 起模板跟随
    主模型）。两条都要钉——水泥在两条路上的模板本来就不同，这不是本轮改出来的。
    """

    #: (code, industry) → (score, template, system_type, risk_level, primary_model)
    EXPECTED = {
        ("002714", "养殖业"): (53.08, "周期价值型", "cyclical", "GREEN", "CYCLICAL_CORE_V2"),
        ("600741", "汽车零部件"): (64.12, "价值型", "value", "GREEN", "GENERAL_VALUE_V2"),
        ("600585", "水泥"): (53.08, "周期价值型", "value", "GREEN", "CYCLICAL_CORE_V2"),
        ("600036", "银行Ⅱ"): (62.88, "价值型", "value", "GREEN", "GENERAL_VALUE_V2"),
        ("601163", "轮胎"): (64.12, "价值型", "value", "GREEN", "GENERAL_VALUE_V2"),
    }

    def test_wired_path_literals(self):
        for (code, industry), want in sorted(self.EXPECTED.items()):
            m, _q, _f = engine.build_metrics(code, _SYNTH_QUOTE, _synth_fin(industry))
            r = engine._run_analysis(m)
            got = (r["final"]["score"], r["final"]["template"], r["type"]["primary"],
                   r["risk"]["level"], r["route"]["primary_model"])
            self.assertEqual(got, want, f"{code}/{industry}：接线动了旧链路")

    def test_the_new_layer_is_the_only_new_key(self):
        """接线只准**加**一个键。旧链路的每一个键都必须还在，且没有第二个新名字。"""
        m, _q, _f = engine.build_metrics("002714", _SYNTH_QUOTE, _synth_fin("养殖业"))
        r = engine._run_analysis(m)
        legacy = set(self._legacy_keys(m))
        self.assertEqual(set(r) - legacy, {"factor_layer"})
        self.assertEqual(legacy - set(r), set(), "接线删掉了旧键")
        self.assertIn("dimensions", r["factor_layer"])
        self.assertIn("research_frame", r["factor_layer"])

    @staticmethod
    def _legacy_keys(m):
        """接线**之前**的 ``_run_analysis`` 正文，原样四行。"""
        scored = rules.score_modules(m)
        route = router.route(m, scored["attributes"])
        result = rules.finalize(scored, m, route)
        result["route"] = route
        return result

    def test_attribute_scores_are_untouched_by_the_new_layer(self):
        """新层是**只读**的：它挂在结果上，不改 ``scored`` / ``final`` 的任何一格。"""
        m, _q, _f = engine.build_metrics("002714", _SYNTH_QUOTE, _synth_fin("养殖业"))
        scored_before = rules.score_modules(m)
        r = engine._run_analysis(m)
        self.assertEqual(
            {k: v["score"] for k, v in r["attributes"].items()},
            {k: v["score"] for k, v in scored_before["attributes"].items()})
        self.assertEqual(r["final"]["components"],
                         rules.final_score(scored_before["attributes"],
                                           scored_before["cyclical_position"], m,
                                           rules.determine_type(scored_before["attributes"],
                                                                "养殖业"),
                                           r["route"])["components"],
                         "接线后 final 的分量必须是同一次路由下的同一份")


# --------------------------------------------------------------------------- #
# 13. 审计门禁（spec §14 §15）
# --------------------------------------------------------------------------- #
class TestAuditSuppression(unittest.TestCase):

    def test_asset_semantic_factors_are_suppressed_without_a_snapshot(self):
        """``tests/test_research`` 的夹具没有资产语义快照 → 这 5 个 factor 必须
        ``audit_suppressed``、不给分，且**不退回旧的一级科目口径**。"""
        out = _evaluated()
        for fid in F.AUDIT_SUPPRESSED_FACTORS:
            item = out["factors"][fid]
            self.assertEqual(item["status"], F.STATUS_AUDIT_SUPPRESSED, fid)
            self.assertIsNone(item["score"], fid)
            self.assertFalse(item["eligible"], fid)
            self.assertIn("审计", item["reason"], fid)
        self.assertFalse(rules.asset_semantics_available(_analyze()[0]))

    def test_suppressed_evidence_never_carries_a_score(self):
        """证据可以留（旧结果里确实有那些位置），但**不许带分**——带一半比全不带
        更像「算过的」。"""
        out = _evaluated()
        item = out["factors"]["liquidation_to_mcap"]
        self.assertTrue(item["components"], "旧结果里确实有这一格，要留证据")
        for comp in item["components"]:
            self.assertIsNone(comp["score"], comp.get("locus"))
            self.assertIsNotNone(comp["locus"], "证据必须说明它在旧体系的哪一处")

    def test_an_available_snapshot_makes_them_score_again(self):
        from tests.asset_fixtures import fake_provider
        m, _q, _f = engine.build_metrics("002714", _SYNTH_QUOTE, _synth_fin("养殖业"))
        m["assets"] = fake_provider(net_cash_ratio=0.30, liquidation_ratio=0.40,
                                    asset_value_ratio=1.20, liquid_asset_ratio=0.30,
                                    interest_debt_cover=3.0)
        self.assertTrue(rules.asset_semantics_available(m))
        scored = rules.score_modules(m)
        final = rules.final_score(scored["attributes"], scored["cyclical_position"], m,
                                  rules.determine_type(scored["attributes"], "养殖业"),
                                  None)
        payload = F.to_payload(F.evaluate(m, scored, None, final))
        suppressed = [fid for fid in F.AUDIT_SUPPRESSED_FACTORS
                      if payload[fid]["status"] == F.STATUS_AUDIT_SUPPRESSED]
        self.assertEqual(suppressed, [], "有快照就不该再抑制")

    def test_the_gate_clears_the_new_fields_too(self):
        """未审计的股票不许漏出研究总览分（spec §14 的「不许绕过」）。"""
        for name in ("factor_layer", "research_summary"):
            self.assertIn(name, engine.AUDIT_SUPPRESSED_FIELDS)
        rec = {"factor_layer": {"overview": {"score": 88.0}},
               "research_summary": {"overview_score": 88.0}, "total_score": 88.0}
        engine._apply_audit_gate(rec, "AUDITING")
        self.assertIsNone(rec["factor_layer"])
        self.assertIsNone(rec["research_summary"])
        self.assertIsNone(rec["total_score"])


# --------------------------------------------------------------------------- #
# 14. factor store（spec §10 §12 §13）
# --------------------------------------------------------------------------- #
class TestFactorStore(unittest.TestCase):
    """用内存库跑：这三张表自成一体，不需要 ``db.connect()`` 那一套。"""

    def setUp(self):
        import sqlite3
        from research import factor_store
        self.store = factor_store
        # 与 ``db.connect()`` 一致：生产里传进来的连接带 ``sqlite3.Row``，
        # 这三张表的读侧按列名取值。夹具照生产的形状搭，而不是让模块去兼容两种连接。
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.layer = _evaluated()

    def tearDown(self):
        self.conn.close()

    def test_schema_is_created_lazily(self):
        self.assertEqual(self.store.counts(self.conn),
                         {t: 0 for t in ("factor_analysis_runs", "factor_snapshots",
                                         "dimension_snapshots")})
        self.store.save(self.conn, "002714", self.layer)
        got = self.store.counts(self.conn)
        self.assertEqual(got["factor_analysis_runs"], 1)
        self.assertEqual(got["factor_snapshots"], len(F.FACTORS))
        self.assertEqual(got["dimension_snapshots"], len(D.DIMENSIONS))

    def test_repeat_analysis_adds_no_row(self):
        first = self.store.save(self.conn, "002714", self.layer)
        again = self.store.save(self.conn, "002714", self.layer)
        self.assertTrue(first["inserted"])
        self.assertFalse(again["inserted"])
        self.assertEqual(again["analysis_id"], first["analysis_id"])
        self.assertEqual(self.store.counts(self.conn)["factor_analysis_runs"], 1)

    def test_a_changed_factor_result_adds_a_run(self):
        self.store.save(self.conn, "002714", self.layer)
        changed = _evaluated()
        changed["factors"]["roic_level"]["score"] = 42.0
        out = self.store.save(self.conn, "002714", changed)
        self.assertTrue(out["inserted"])
        self.assertEqual(self.store.counts(self.conn)["factor_analysis_runs"], 2)
        self.assertEqual(len(self.store.list_runs(self.conn, "002714")), 2)

    def test_a_changed_research_frame_adds_a_run(self):
        self.store.save(self.conn, "002714", self.layer)
        changed = _evaluated()
        changed["research_frame"] = {"frame": D.FRAME_GENERAL, "label": "通用框架",
                                     "reason": "测试"}
        self.assertTrue(self.store.save(self.conn, "002714", changed)["inserted"])

    def test_the_hash_ignores_dates_and_legacy_scores(self):
        """spec §12：hash 不含日期 / UI 字段 / Legacy score。"""
        a = _evaluated()
        b = _evaluated()
        b["factors"]["roic_level"]["note"] = "改了说明文字"
        b["factors"]["roic_level"]["display_name"] = "改了展示名"
        b["factors"]["roic_level"]["loci"] = ["另一处"]
        self.assertEqual(self.store.factor_result_hash(a),
                         self.store.factor_result_hash(b))

    def test_the_hash_moves_when_the_weight_config_moves(self):
        """权重配置进 hash——它是「同一份数据会算出不同结果」的原因之一。

        改常量必须在**同一个窗口内**算 hash：``weight_config()`` 是调用时读模块
        常量的，出了窗口就还原了。
        """
        for key in ("group_weights", "group_caps", "max_reweight_factor",
                    "applicability_gates", "overview_coverage_floor",
                    "dimension_weights", "gate_only_groups"):
            self.assertIn(key, self.store.weight_config("CYCLICAL_CORE_V2"))
        self.assertNotEqual(
            self.store.weight_config("CYCLICAL_CORE_V2"),
            self.store.weight_config("VALUE_CIGAR_V2"))
        layer = _evaluated()
        base = self.store.factor_result_hash(layer)
        before = D.MAX_REWEIGHT_FACTOR
        try:
            D.MAX_REWEIGHT_FACTOR = before + 0.5
            moved = self.store.factor_result_hash(layer)
        finally:
            D.MAX_REWEIGHT_FACTOR = before
        self.assertNotEqual(base, moved, "改了重分配上限，hash 必须变")
        self.assertEqual(base, self.store.factor_result_hash(layer), "常量还原后 hash 回来")

    def test_every_model_reaches_its_own_frame_weights(self):
        """模型 → frame → 权重这一跳必须真的发生（批 3 在这跳过一次）。

        退化形态很难看出来：``dimension_weights_for(model_id)`` 查不到 key，
        于是**每个模型都拿到 GENERAL 权重**——权重表的形状、和值、类型全对，
        只有「换了模型分数构成不变」这一件事悄悄错了。所以这里逐模型对账：
        每个模型拿到的权重必须等于**它自己那个 frame** 的那一行。
        """
        from research import router
        seen_frames = set()
        for model in sorted(router.MODEL_SPECS):
            frame = D.frame_of_model(model)
            self.assertIn(frame, D.DIMENSION_WEIGHTS, model)
            self.assertEqual(
                self.store.weight_config(model)["dimension_weights"],
                D.DIMENSION_WEIGHTS[frame], model)
            seen_frames.add(frame)
        self.assertGreater(len(seen_frames), 1,
                           "所有模型落进同一个 frame，这条测试就测不出翻译错了")
        # 不认识的模型必须退回 GENERAL，且**不能**抛
        self.assertEqual(
            self.store.weight_config("不存在的模型")["dimension_weights"],
            D.FALLBACK_DIMENSION_WEIGHTS)

    def test_analysis_id_is_deterministic(self):
        h = self.store.factor_result_hash(self.layer)
        self.assertEqual(self.store.analysis_id("002714", h),
                         self.store.analysis_id("002714", h))
        self.assertNotEqual(self.store.analysis_id("002714", h),
                            self.store.analysis_id("000876", h))

    def test_the_read_side_rebuilds_the_same_layer(self):
        """落库 → 读回 → 用**同一份** ``dimensions.evaluate`` 重建，必须一模一样。

        这是读侧唯一的防漂移手段：库里存的是测量列，group / dimension / overview
        那一层不是另写一份，而是原路重算。
        """
        self.store.save(self.conn, "002714", self.layer)
        got = self.store.load_layer(self.conn, "002714")
        self.assertIsNotNone(got)
        self.assertTrue(got["reconstruction"]["stored_rows_match"],
                        got["reconstruction"]["stored_dimension_mismatches"])
        for dim in D.DIMENSIONS:
            self.assertEqual(got["dimensions"][dim]["score"],
                             self.layer["dimensions"][dim]["score"], dim)
            self.assertEqual(got["dimensions"][dim]["effective_weight"],
                             self.layer["dimensions"][dim]["effective_weight"], dim)
        for fid in F.FACTOR_IDS:
            want, have = self.layer["factors"][fid], got["factors"][fid]
            for key in ("score", "status", "coverage", "confidence", "base_weight",
                        "applicability_multiplier", "effective_weight", "contribution",
                        "time_basis", "source_semantics", "loci"):
                self.assertEqual(have.get(key), want.get(key), f"{fid}.{key}")
        self.assertEqual(got["research_frame"]["frame"],
                         self.layer["research_frame"]["frame"])
        self.assertEqual(got["duplicate_report"]["duplicate_factors"],
                         self.layer["duplicate_report"]["duplicate_factors"])

    def test_summary_is_a_column_read(self):
        self.store.save(self.conn, "002714", self.layer)
        s = self.store.summary(self.conn, "002714")
        self.assertEqual(s["overview_score"], self.layer["overview"]["score"])
        self.assertEqual(s["research_frame"], self.layer["research_frame"]["frame"])
        self.assertEqual(s["dimension_scores"],
                         {k: v["score"] for k, v in self.layer["dimensions"].items()})
        self.assertIsNone(self.store.summary(self.conn, "999999"))

    def test_no_second_layer_for_a_stock_without_a_run(self):
        self.assertIsNone(self.store.load_layer(self.conn, "002714"))

    def test_save_tolerates_missing_keys(self):
        """``_persist`` 会被既有测试用**不带 factor 层**的 result 调用。"""
        self.assertFalse(self.store.save(self.conn, "002714", {})["inserted"])
        self.assertFalse(self.store.save(self.conn, "002714", None)["inserted"])

    def test_baseline_tag_and_note_are_labels_that_do_not_touch_the_hash(self):
        """标签是「这批 run 是谁跑的」，不是「算出来是什么」——所以它**不进 hash**。

        判据：同一份载荷，带标签与不带标签的 ``factor_result_hash`` 相同。反过来说，
        如果它进了 hash，给一批 run 补个名字就会让每只股票白增一行。
        """
        bare = self.store.factor_result_hash(self.layer)
        self.store.save(self.conn, "002714", self.layer,
                        baseline_tag="CANONICAL_BASELINE_2026_09",
                        note="legacy experimental scoring drift reconciled")
        run = self.store.latest_run(self.conn, "002714")
        self.assertEqual(run["baseline_tag"], "CANONICAL_BASELINE_2026_09")
        self.assertEqual(run["note"], "legacy experimental scoring drift reconciled")
        self.assertEqual(run["factor_hash"], bare)
        # 重跑一次同样的载荷：标签相同不新增；**换标签也不新增**（hash 没变）。
        again = self.store.save(self.conn, "002714", self.layer,
                                baseline_tag="OTHER")
        self.assertFalse(again["inserted"])
        self.assertEqual(self.store.latest_run(self.conn, "002714")["baseline_tag"],
                         "CANONICAL_BASELINE_2026_09",
                         "标签相同就不该被后来的标签覆盖——一次运行只有一个名字")

    def test_baseline_tag_defaults_to_none(self):
        saved = self.store.save(self.conn, "002714", self.layer)
        self.assertTrue(saved["inserted"])
        run = self.store.latest_run(self.conn, "002714")
        self.assertIsNone(run["baseline_tag"])
        self.assertIsNone(run["note"])


# --------------------------------------------------------------------------- #
# 15. 方向语义：五档词表，以及「非单调」不再是属性型的理由
# --------------------------------------------------------------------------- #
class TestDirectionSemantics(unittest.TestCase):
    """批 2.5 的核心修正。

    批 2 只有「越大越好 / 越小越好 / 无方向」三档，于是「有合理区间」的东西只能
    挤进 ``NEUTRAL``，而 ``NEUTRAL`` 又被当成「没有好坏」的同义词——``payout_ratio``
    因此被标成属性、不进分。修法不是特例它一个，而是**把方向词表补全**。
    """

    def test_the_vocabulary_is_closed(self):
        self.assertEqual(sorted(F.DIRECTIONS),
                         sorted([F.DIRECTION_HIGHER_BETTER, F.DIRECTION_LOWER_BETTER,
                                 F.DIRECTION_TARGET_RANGE, F.DIRECTION_STATE_BASED,
                                 F.DIRECTION_NEUTRAL]))
        for d in F.DIRECTIONS:
            self.assertTrue(F.DIRECTION_LABELS.get(d), d)
            self.assertTrue(F.DIRECTION_MEANING.get(d), d)

    def test_only_neutral_refuses_to_express_goodness(self):
        """判据是「说不说得出好坏」，不是「单不单调」——这一点就是本批的全部。"""
        self.assertFalse(F.DIRECTION_EXPRESSES_GOODNESS[F.DIRECTION_NEUTRAL])
        for d in (F.DIRECTION_HIGHER_BETTER, F.DIRECTION_LOWER_BETTER,
                  F.DIRECTION_TARGET_RANGE, F.DIRECTION_STATE_BASED):
            self.assertTrue(F.DIRECTION_EXPRESSES_GOODNESS[d], d)
            self.assertIn(d, F.SCORE_CAPABLE_DIRECTIONS, d)
        self.assertNotIn(F.DIRECTION_NEUTRAL, F.SCORE_CAPABLE_DIRECTIONS)

    def test_target_range_can_be_a_score(self):
        """``TARGET_RANGE`` + ``SCORE`` **合法**——它是本批新增的能力位。"""
        spec = _fake_spec(direction=F.DIRECTION_TARGET_RANGE, role=F.ROLE_SCORE)
        self.assertTrue(spec.direction_expresses_goodness)
        self.assertTrue(spec.contributes_to_score)
        self.assertNotIn(spec.direction, F.MONOTONE_DIRECTIONS)
        # 真实的目录里也确实有这样一个 factor。
        payout = F.FACTOR_INDEX["payout_ratio"]
        self.assertEqual(payout.direction, F.DIRECTION_TARGET_RANGE)
        self.assertEqual(payout.factor_role, F.ROLE_SCORE)

    def test_state_based_can_be_a_score(self):
        """``STATE_BASED`` + ``SCORE`` **合法**：IMPROVING 就是比 DETERIORATING 好。"""
        spec = _fake_spec(direction=F.DIRECTION_STATE_BASED, role=F.ROLE_SCORE)
        self.assertTrue(spec.direction_expresses_goodness)
        self.assertTrue(spec.contributes_to_score)
        self.assertNotIn(spec.direction, F.MONOTONE_DIRECTIONS)

    def test_neutral_cannot_be_a_score(self):
        """``NEUTRAL`` + ``SCORE`` **非法**——这条从批 2 保留至今，方向改词表不改它。

        注意 ``contributes_to_score`` 在这里仍然是 ``True``：它是**角色的函数**，
        不看方向。这是刻意的——让非法组合在 ``role_inconsistencies()`` 那里**报出来**，
        而不是在属性里悄悄把它降成不贡献。后者会把一个声明错误伪装成「它本来就不
        进分」，正是本层要治的那种「静默」。
        """
        spec = _fake_spec(direction=F.DIRECTION_NEUTRAL, role=F.ROLE_SCORE,
                          factor_id="__illegal__")
        self.assertEqual(spec.contributes_to_score,
                         F.ROLE_CONTRIBUTES_TO_SCORE[F.ROLE_SCORE])
        self.assertFalse(spec.direction_expresses_goodness)
        original = F.FACTORS
        try:
            F.FACTORS = (spec,)
            self.assertEqual(F.role_inconsistencies(), ("__illegal__",))
            self.assertFalse(D._structure_ok(
                F.structure_status({}, None)))
        finally:
            F.FACTORS = original
        self.assertEqual(tuple(F.role_inconsistencies()), ())
        self.assertEqual(F.role_reason_missing(), ())

    def test_payout_ratio_is_a_score_with_a_target_range(self):
        """**批 2.5 的定点修复。** 派息率不是「没有好坏」，是有合理区间。"""
        spec = F.FACTOR_INDEX["payout_ratio"]
        self.assertEqual(spec.factor_role, F.ROLE_SCORE)
        self.assertEqual(spec.direction, F.DIRECTION_TARGET_RANGE)
        self.assertTrue(spec.role_reason, "区间型方向必须写明区间是什么")
        self.assertNotIn("payout_ratio", F.CHARACTERISTIC_FACTORS)
        self.assertTrue(spec.contributes_to_score)

    def test_payout_ratio_actually_scores_in_a_real_payload(self):
        """改角色的**效果**：它在股东回报组里真的出一个分，且贡献非零。"""
        out = _evaluated()
        item = out["factors"]["payout_ratio"]
        self.assertTrue(item["eligible"], item["reason"])
        self.assertIsNotNone(item["score"])
        self.assertTrue(item["contributes_to_dimension"])
        self.assertGreater(item["contribution"], 0.0)
        self.assertEqual(item["direction"], F.DIRECTION_TARGET_RANGE)
        self.assertEqual(item["role_label"], F.ROLE_LABELS[F.ROLE_SCORE])

    def test_the_characteristic_with_a_monotone_direction_must_explain_itself(self):
        """单调方向 + 属性型**允许**（周期暴露就是），但必须写明为什么。

        「越大越好」却不进分，看上去是自相矛盾；理由要在声明里说清楚，
        不能靠读的人自己回忆。这份名单**逐条钉住**：新增一条要显式改测试。
        """
        self.assertEqual(sorted(F.monotone_characteristics()),
                         sorted(["earnings_volatility", "earnings_sign_switches",
                                 "gross_margin_volatility", "profit_revenue_sync",
                                 "capex_cycle", "psy", "msy"]))
        # 组归属也钉住：前四条是「周期暴露强度」，capex_cycle 是「钱花在周期的哪
        # 一段」——都不表达好坏，但理由不同，所以分散在两个组里。
        groups = {fid: F.FACTOR_INDEX[fid].factor_group
                  for fid in F.monotone_characteristics()}
        self.assertEqual(groups["capex_cycle"], F.GROUP_MARKET_REGIME)
        for fid in ("earnings_volatility", "earnings_sign_switches",
                    "gross_margin_volatility", "profit_revenue_sync"):
            self.assertEqual(groups[fid], F.GROUP_CYCLICAL_EXPOSURE, fid)
        # 批 4 的 PSY / MSY：方向是单调的（成活率越高越好是事实），角色是属性型
        # （没有可审计来源，且成熟猪企之间的差异小于测量噪声）。**两者不矛盾**：
        # 方向说的是经济含义，角色说的是「这个数现在值不值得进分」。
        for fid in ("psy", "msy"):
            self.assertEqual(groups[fid], F.GROUP_PIG_INDUSTRY, fid)
            self.assertEqual(F.FACTOR_INDEX[fid].direction,
                             F.DIRECTION_HIGHER_BETTER, fid)
            self.assertFalse(F.FACTOR_INDEX[fid].contributes_to_score, fid)

    def test_every_role_and_direction_that_needs_a_reason_has_one(self):
        self.assertEqual(list(F.role_reason_missing()), [])
        for spec in F.FACTORS:
            if spec.factor_role != F.ROLE_SCORE or spec.direction not in F.MONOTONE_DIRECTIONS:
                self.assertTrue(spec.role_reason, spec.factor_id)

    def test_cyclical_exposure_components_never_contribute_to_the_opportunity_score(self):
        """spec §10.5：整组暴露的成员**一个都不许**直接贡献 OPPORTUNITY 分。

        逐成员查，而不是只查组的 ``contribution``——组的那一项是 0.00（权重是
        0），但「成员各自的 ``contribution`` 也是 0」是另一句话，而它才是
        「暴露没有混进这一维的分」的直接判据。
        """
        out = _evaluated()
        members = [fid for fid, i in out["factors"].items()
                   if i.get("factor_group") == F.GROUP_CYCLICAL_EXPOSURE]
        self.assertTrue(members, "夹具里应该有周期暴露的成员")
        for fid in members:
            item = out["factors"][fid]
            self.assertFalse(item["contributes_to_score"], fid)
            self.assertFalse(item["contributes_to_dimension"], fid)
            self.assertEqual(item["contribution"], 0.0, fid)
            self.assertEqual(item["effective_weight"], 0.0, fid)
            self.assertEqual(item["factor_role"], F.ROLE_CHARACTERISTIC, fid)
        group = out["groups"][D.OPPORTUNITY][F.GROUP_CYCLICAL_EXPOSURE]
        self.assertEqual(group["contribution"], 0.0)
        self.assertEqual(group["effective_weight"], 0.0,
                         "暴露的权重必须是 0：它只当门")

    def test_the_semantic_review_records_are_not_stale(self):
        """发现但本轮不改的疑点必须留痕，且记录不能过期。

        过期比没有记录更坏——它会让人以为「当时查过，确实是这样」。
        """
        self.assertEqual(list(F.semantic_review_stale()), [])
        self.assertEqual(sorted(F.SEMANTIC_REVIEW),
                         sorted(["debt_asset_ratio", "dividend_yield",
                                 "cfo_net_profit", "capex_cycle",
                                 "balance_trend", "industry_prior"]))
        for fid, rec in F.SEMANTIC_REVIEW.items():
            spec = F.FACTOR_INDEX[fid]
            self.assertEqual((spec.direction, spec.factor_role), rec["current"], fid)
            self.assertTrue(rec["why"], fid)
            self.assertTrue(rec["impact"], fid)

    def test_the_review_record_catches_a_direction_change(self):
        """记录真的会随着代码变红——不是一张写完就烂掉的表。"""
        record = F.SEMANTIC_REVIEW["capex_cycle"]
        self.assertEqual(record["current"], (F.DIRECTION_HIGHER_BETTER,
                                             F.ROLE_CHARACTERISTIC))
        self.assertEqual(record["proposed"], (F.DIRECTION_TARGET_RANGE,
                                              F.ROLE_CHARACTERISTIC))
        self.assertIn("纯重标", record["impact"])


def _fake_spec(direction, role, factor_id="__probe__"):
    """造一个只用来问语义的 factor 声明（不进目录、不出现在任何载荷里）。"""
    return F.FactorSpec(factor_id, "探针", F.GROUP_PROFITABILITY, ("ROIC",),
                        "探针", F.FACTOR_INDEX["roic_level"].time_basis, direction,
                        F.FACTOR_INDEX["roic_level"].source_semantics, "ratio",
                        role=role, role_reason="探针")


# --------------------------------------------------------------------------- #
# 16. 行业先验：从 Router 的 fit 明细收上来，供周期门兜底用
# --------------------------------------------------------------------------- #
class TestIndustryPrior(unittest.TestCase):

    def test_tier_bands_agree_with_the_router(self):
        """档位门槛必须与 ``router.INDUSTRY_PRIOR_TIERS`` 一致。

        **不复制那张表**（复制就是两份会漂移的定义），只把「几分算哪一档」写下来
        并与它对照：router 哪天把 50 改成 40，这条测试就红。
        """
        from research import router
        for value, _label, _keywords in router.INDUSTRY_PRIOR_TIERS:
            self.assertIsNotNone(F.prior_tier_of(value),
                                 f"router 的先验值 {value} 落不进任何一档")
        expected = {100.0: F.PRIOR_TIER_STRONG, 50.0: F.PRIOR_TIER_MEDIUM,
                    15.0: F.PRIOR_TIER_WEAK,
                    router.INDUSTRY_PRIOR_NONE[0]: F.PRIOR_TIER_UNKNOWN}
        for value, tier in expected.items():
            self.assertEqual(F.prior_tier_of(value), tier, value)
        floors = sorted(v for v, _t in F.PRIOR_TIER_BANDS)
        self.assertEqual(floors, sorted(v for v, _l, _k in router.INDUSTRY_PRIOR_TIERS),
                         "门槛值集合必须与 router 完全一致（多一档少一档都要改这里）")

    def test_tier_of_out_of_range_values(self):
        self.assertIsNone(F.prior_tier_of(None))
        self.assertEqual(F.prior_tier_of(0.0), F.PRIOR_TIER_UNKNOWN)
        self.assertEqual(F.prior_tier_of(-1.0), F.PRIOR_TIER_UNKNOWN)

    def test_industry_prior_gets_a_reading_from_the_route_evidence(self):
        """批 2 它报 ``missing_data``（尽管 Router 每只都算了它）；批 2.5 收上来。

        **但只在能拿到 Router 结果时**——离线调用 ``route=None`` 仍然是「没有
        取值位置」，不是「不适用」。
        """
        route = {"primary_model": "CYCLICAL_CORE_V2", "route_status": "CLEAR",
                 "evidence": [{"model": "CYCLICAL_CORE_V2", "components": [
                     {"key": "industry_prior", "score": 100.0, "available": True},
                     {"key": "risk_level", "score": 70.0, "available": True},
                 ]}]}
        self.assertEqual(F.router_component_values(route),
                         {"industry_prior": 100.0})
        self.assertEqual(F.router_component_conflicts(route), [])
        self.assertEqual(F.router_component_values(None), {})
        self.assertEqual(F.router_component_values({}), {})

    def test_a_reading_that_only_some_models_have_still_lands(self):
        """读数只出现在一个模型的 evidence 里也要收得上来（与**主模型是谁无关**：
        它是行业的函数，跟着路由结果漂才是错的）。"""
        route = {"evidence": [
            {"model": "GENERAL_VALUE_V2", "components": []},
            {"model": "CYCLICAL_CORE_V2", "components": [
                {"key": "industry_prior", "score": 50.0, "available": True}]},
        ]}
        self.assertEqual(F.router_component_values(route), {"industry_prior": 50.0})

    def test_an_unavailable_component_is_not_a_reading(self):
        route = {"evidence": [{"model": "M", "components": [
            {"key": "industry_prior", "score": None, "available": False}]}]}
        self.assertEqual(F.router_component_values(route), {})

    def test_the_value_is_a_tier_code_not_a_measurement(self):
        """载荷里必须能看出「100 是强周期这一档」而不是「100 分」。"""
        m, scored, final = _analyze()
        route = {"primary_model": "CYCLICAL_CORE_V2", "route_status": "CLEAR",
                 "evidence": [{"model": "CYCLICAL_CORE_V2", "components": [
                     {"key": "industry_prior", "score": 100.0, "available": True}]}]}
        out = F.to_payload(F.evaluate(m, scored, route, final))
        item = out["industry_prior"]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["score"], 100.0)
        self.assertEqual(item["raw"]["tier"], F.PRIOR_TIER_STRONG)
        self.assertEqual(item["raw"]["tier_label"], F.PRIOR_TIER_LABELS[F.PRIOR_TIER_STRONG])
        self.assertEqual(item["raw"]["source"], "router.INDUSTRY_PRIOR_TIERS")
        self.assertEqual(item["unit"], "points")

    def test_industry_prior_never_contributes_to_any_dimension(self):
        """它只是兜底信号，**不进任何分**——MARKET 因此照旧 NO_DATA。"""
        m, scored, final = _analyze()
        route = {"primary_model": "CYCLICAL_CORE_V2", "route_status": "CLEAR",
                 "evidence": [{"model": "CYCLICAL_CORE_V2", "components": [
                     {"key": "industry_prior", "score": 100.0, "available": True}]}]}
        out = D.evaluate(F.to_payload(F.evaluate(m, scored, route, final)), route)
        item = out["factors"]["industry_prior"]
        self.assertTrue(item["eligible"])
        self.assertEqual(item["contribution"], 0.0)
        self.assertFalse(item["contributes_to_dimension"])
        self.assertEqual(out["dimensions"][D.MARKET]["status"], "NO_DATA")

    def test_the_structure_check_reports_the_tier_rules(self):
        out = _evaluated()
        status = out["structure_status"]
        self.assertEqual(status["role_reason_missing"], [])
        self.assertEqual(status["semantic_review_stale"], [])
        self.assertTrue(status["monotone_characteristics"])
        self.assertTrue(status["ok"], status)

    def test_the_direction_vocabulary_ships_with_the_layer(self):
        """五档词表必须**随载荷下发**：前端不许写第二份（同 research_frame 的规矩）。

        判据是「载荷里有」，不是「factors.py 里有」——中间少带一环（例如
        ``dimensions.evaluate`` 的返回值把它丢了），界面就只能自己硬编码五个
        字符串，而那正是这个仓库反复在治的病。
        """
        out = _evaluated()
        meta = out.get("_meta")
        self.assertIsNotNone(meta, "factor 层的 _meta 没被带出来")
        self.assertEqual(list(meta["directions"]), list(F.DIRECTIONS))
        self.assertEqual(meta["score_capable_directions"],
                         list(F.SCORE_CAPABLE_DIRECTIONS))
        for d in F.DIRECTIONS:
            self.assertTrue(meta["direction_labels"][d])
            self.assertTrue(meta["direction_meaning"][d])

    def test_the_read_side_also_carries_the_direction_vocabulary(self):
        """读侧（详情页走的那条）与写侧必须是同一份 ``_meta``。"""
        import sqlite3
        import tempfile
        import research.db as research_db
        from research import factor_store
        conn = research_db.init_db(tempfile.mktemp(suffix=".db"))
        try:
            m, scored, final = _analyze()
            route = {"primary_model": "CYCLICAL_CORE_V2", "route_status": "CLEAR"}
            layer = D.evaluate(F.evaluate(m, scored, route, final), route)
            factor_store.save(conn, "002714", layer)
            read_back = factor_store.load_layer(conn, "002714").get("_meta")
            self.assertIsNotNone(read_back, "读侧丢了 _meta")
            self.assertEqual(list(read_back["directions"]), list(F.DIRECTIONS))
        finally:
            conn.close()
# --------------------------------------------------------------------------- #
BASELINE_TAG = "CANONICAL_BASELINE_2026_09"
DRIFT_NOTE = "legacy experimental scoring drift reconciled"


def _persisted(**kwargs):
    """跑一次真实的 ``_persist``（合成数据 + 临时库），返回 ``(conn, code, rows)``。

    用 ``_run_analysis`` 而不是手拼 result：标签要落到 run 行上，而 run 行是
    ``_persist`` 里有 factor 层才会写的——手拼一个 result 就等于把被测的那一段
    替换掉了。
    """
    import tempfile
    import research.db as research_db
    conn = research_db.init_db(tempfile.mktemp(suffix=".db"))
    code, industry = "002714", "养殖业"
    m, _q, _f = engine.build_metrics(code, _SYNTH_QUOTE, _synth_fin(industry))
    result = engine._run_analysis(m)
    engine._persist(conn, code, _SYNTH_QUOTE, m, result, "2026-06-30", industry,
                    False, **kwargs)
    return conn, code


class TestRunLabelsTravelFromAnalyze(unittest.TestCase):
    """§12/§13 要求「这一次为什么重算」能落到 run 上，而不是事后手工 UPDATE。

    链子是 ``analyze(note=, baseline_tag=)`` → ``_persist`` → ``factor_store.save``。
    中间任何一环丢了参数都**不会报错**，只会安静地存下 None——这正是要防的失败模式，
    所以两段都单独钉。
    """

    def test_the_labels_land_on_the_run_row(self):
        from research import factor_store
        conn, code = _persisted(baseline_tag=BASELINE_TAG, note=DRIFT_NOTE)
        try:
            run = factor_store.latest_run(conn, code)
            self.assertIsNotNone(run, "没有 factor 层被落库，下面两条是空转")
            self.assertEqual(run["baseline_tag"], BASELINE_TAG)
            self.assertEqual(run["note"], DRIFT_NOTE)
        finally:
            conn.close()

    def test_labels_do_not_add_a_run_or_move_the_hash(self):
        """标签是「谁跑的」不是「算出来什么」：同一份结果带标签重跑不白增一行。"""
        from research import factor_store
        conn, code = _persisted()
        try:
            bare = factor_store.latest_run(conn, code)["factor_hash"]
            before = factor_store.counts(conn)["factor_analysis_runs"]
        finally:
            conn.close()
        conn, code = _persisted(baseline_tag=BASELINE_TAG, note=DRIFT_NOTE)
        try:
            run = factor_store.latest_run(conn, code)
            self.assertEqual(run["factor_hash"], bare, "标签动了 hash")
            self.assertEqual(factor_store.counts(conn)["factor_analysis_runs"], before)
            self.assertEqual(run["baseline_tag"], BASELINE_TAG)
        finally:
            conn.close()

    def test_analyze_forwards_both_labels_to_persist(self):
        """把 ``analyze`` 的外层依赖全部换成空实现，只看它有没有把标签传下去。

        ``analyze`` 里唯一不能碰的是**真实库与网络**（它会写 research_stocks），
        所以 ``db.connect`` / 财务缓存 / 资产层 / 估值历史全部被挡掉；被观察的只有
        ``_persist`` 收到的那两个关键字。
        """
        from unittest.mock import MagicMock, patch
        seen = {}

        def fake_persist(conn, code, quote, m, result, period, industry, refreshed,
                         note=None, baseline_tag=None, mode=None):
            seen["note"] = note
            seen["baseline_tag"] = baseline_tag

        provider = MagicMock()
        provider.get_quote.return_value = {"name": "牧原股份", "price": 40.0}
        provider.get_latest_report_period.return_value = None
        with patch.object(engine.db, "connect", MagicMock(return_value=MagicMock())), \
                patch.object(engine.db, "get_financial_cache",
                             MagicMock(return_value=None)), \
                patch.object(engine.asset_metrics, "load",
                             MagicMock(return_value=None)), \
                patch.object(engine.valuation_history, "ensure", MagicMock()), \
                patch.object(engine.valuation_history, "pb_series",
                             MagicMock(return_value=None)), \
                patch.object(engine.audit_job, "status_of",
                             MagicMock(return_value=engine.audit_job.OK)), \
                patch.multiple(
                    engine,
                    get_provider=MagicMock(return_value=provider),
                    fetch_financials=MagicMock(return_value={}),
                    build_metrics=MagicMock(
                        return_value=({}, "2026-06-30", "养殖业")),
                    _run_analysis=MagicMock(return_value={}),
                    _persist=MagicMock(side_effect=fake_persist)):
            engine.analyze("002714", False, note=DRIFT_NOTE,
                           baseline_tag=BASELINE_TAG)
        self.assertEqual(seen["note"], DRIFT_NOTE,
                         "analyze 没有把 note 传给 _persist——标签会在这一环丢掉")
        self.assertEqual(seen["baseline_tag"], BASELINE_TAG)

    def test_the_http_layer_normalizes_the_labels(self):
        """接口层只做「去空白 + 截断 + 空串归 None」，不解释内容。"""
        import server
        self.assertIsNone(server._label(None))
        self.assertIsNone(server._label(""))
        self.assertIsNone(server._label("   "))
        self.assertIsNone(server._label({"note": "不是字符串"}))
        self.assertEqual(server._label("  重算  "), "重算")
        self.assertEqual(len(server._label("x" * 500)), server.LABEL_MAX)


# --------------------------------------------------------------------------- #
# 12. 批 4：风险回报 / 金融隔离 / 猪产业在 factor 层的语义
# --------------------------------------------------------------------------- #
def _pig_note(revenue_share=0.267, audit_scope="financial_statement_note",
              period="2025A"):
    """一期带分部信息表的缓存报告（形状照 ``pig_segment_notes`` 的产物）。"""
    return [{"report_period": period,
             "financial_note": {
                 "status": "extracted", "segment_label": "猪产业",
                 "audit_scope": audit_scope,
                 "exposure": {"revenue_share": revenue_share,
                              "gross_profit_share": 0.31,
                              "assets_share_pre_elimination": 0.22},
                 "evidence": {"source_page": 88}},
             "revenue": {"value": 1.2e10}, "cogs": {"value": 1.0e10}}]


def _anchor_ctx(m, peer_sample=(12.0, 15.0, 18.0), pe_series=None, pig=None,
                model="CYCLICAL_CORE_V2"):
    """一份装配好的上下文（含 ``anchors``）——与 ``engine`` 的装配顺序一致。

    BATCH 4.1 §一 起 ``peer`` 里多一条 ``peer_pe_multiple``（B 语义：同行自己的
    PE 分布）。锚读的是它，**不读** ``valuation["pe"]["sample"]``——后者属 A
    语义，本公司亏损时整段消失。两个都给，与 ``PeerView.context()`` 对齐。
    """
    values = sorted(float(v) for v in peer_sample)
    count = len(values)
    enough = count >= PG.LOW_CONFIDENCE_MIN
    ctx = {"peer": {"available": True, "peer_group": "TEST_PEERS",
                    "as_of": "2026-09-25",
                    "valuation": {"pe": {"sample": values, "confidence": "high",
                                         "reason": None, "peer_percentile": 0.5}},
                    "peer_pe_multiple": {
                        "source": "peer_groups.TEST_PEERS.pe",
                        "peer_positive_pe_count": count,
                        "peer_pe_values": values,
                        "peer_pe_median": (PG.quantile(values, 0.5)
                                           if enough else None),
                        "peer_pe_p25": (PG.quantile(values, 0.25)
                                        if enough else None),
                        "peer_pe_p75": (PG.quantile(values, 0.75)
                                        if enough else None),
                        "peer_pe_as_of": "2026-09-25",
                        "peer_pe_confidence": "high" if enough else "none",
                        "sample_status": (PG.SAMPLE_OK if enough
                                          else PG.SAMPLE_INSUFFICIENT),
                        "reason": None if enough else "正 PE 样本只有 %d 家" % count,
                        "excluded_nonpositive": [], "excluded_missing": []}},
           "pe_series": list(pe_series or []), "pig": pig or {},
           "unmapped_industries": []}
    if not peer_sample:
        ctx["peer"]["available"] = False
    ctx["anchors"] = VA.resolve(m, context=ctx, primary_model=model)
    return ctx


def _batch4_factors(industry="养殖业", ctx=None, model="CYCLICAL_CORE_V2",
                    code="002714"):
    m, scored, final = _analyze(code, industry)
    route = {"primary_model": model, "route_status": "CLEAR"} if model else None
    if ctx is None:
        ctx = _anchor_ctx(m, model=model or "CYCLICAL_CORE_V2")
    return m, F.evaluate(m, scored, route, final, context=ctx)


class TestRiskRewardFactors(unittest.TestCase):
    """§十六–§十九：三档锚 → 上行 / 下行 / 赔率 / 锚置信度。"""

    RR = ("base_upside", "bear_downside", "risk_reward_ratio", "anchor_confidence")

    def test_anchors_drive_the_whole_group(self):
        m, _scored, _final = _analyze()
        # 夹具的 8 年扣非利润里 P25 是**负数**（养殖业本来就这样），所以盈利类
        # Bear 会被非正利润挡掉；给它一份真实的资产托底（清算价值/市值 0.5）。
        m["current"]["liquidation_ratio"] = 0.5
        m["current"]["asset_value_ratio"] = 1.2
        out = F.evaluate(m, rules.score_modules(m), None, None,
                         context=_anchor_ctx(m))
        for fid in self.RR:
            self.assertEqual(out[fid].status, "ok", fid)
        self.assertEqual(out["anchor_confidence"].raw,
                         out["anchor_confidence"].score / 100.0)
        self.assertIn("bear", out["risk_reward_ratio"].raw)
        self.assertIn("multiple", out["risk_reward_ratio"].raw["base"]["inputs"])

    def test_anchor_confidence_is_applicability_and_never_scores(self):
        _m, out = _batch4_factors()
        spec = F.FACTOR_INDEX["anchor_confidence"]
        self.assertEqual(spec.factor_role, F.ROLE_APPLICABILITY)
        self.assertFalse(spec.contributes_to_score)
        self.assertIsInstance(out["anchor_confidence"].score, float)

    def test_below_bear_is_not_an_infinity(self):
        """价格跌破下行锚：RR 无定义，但**不许**变成无穷大也**不许**变成 0 分。"""
        m, _scored, _final = _analyze()
        m["current"]["liquidation_ratio"] = 1.5
        m["current"]["asset_value_ratio"] = 1.6      # 资产托底 → Bear = 15 > 现价 10
        ctx = _anchor_ctx(m)
        self.assertEqual(ctx["anchors"]["price_state"], VA.PRICE_BELOW_BEAR)
        out = F.evaluate(m, rules.score_modules(m), None, None, context=ctx)
        raw = out["risk_reward_ratio"].raw
        self.assertIsNone(raw["risk_reward"])
        # §八：真实下行照实报 0，评分分母报地板——**两个数都要能在载荷里查到**，
        # 否则读的人会以为真实下行刚好 5%。
        self.assertEqual(raw["raw_downside"], 0.0)
        self.assertEqual(raw["effective_downside"],
                         rules.RULES_V1["risk_reward"]["min_bear_downside"])
        self.assertTrue(raw["floor_applied"])
        self.assertIsNone(raw["raw_risk_reward_ratio"])
        self.assertEqual(out["risk_reward_ratio"].score,
                         rules.RULES_V1["risk_reward"]["below_bear_score"])
        self.assertTrue(out["risk_reward_ratio"].reason)

    def test_thin_anchors_turn_the_group_not_applicable(self):
        """锚撑不住（样本太薄）→ 三格 not_applicable 且**不惩罚覆盖度**。"""
        m, _scored, _final = _analyze()
        m["current"]["asset_value_ratio"] = None
        m["current"]["liquidation_ratio"] = None
        # 只有一个完整年度 → 盈利置信度 0.0；peer 只有 1 家 → 倍数置信度低。
        m["annual"] = m["annual"][-1:]
        ctx = _anchor_ctx(m, peer_sample=(15.0,))
        self.assertLess(ctx["anchors"]["anchor_confidence"],
                        rules.RULES_V1["risk_reward"]["min_anchor_confidence"])
        out = F.evaluate(m, rules.score_modules(m), None, None, context=ctx)
        for fid in self.RR:
            self.assertEqual(out[fid].status, "not_applicable", fid)
            self.assertEqual(out[fid].coverage, 1.0, fid)

    def test_no_anchor_context_is_missing_data(self):
        """engine 没装配锚 → 如实报 missing（**不是** 0 分、也不是 not_applicable）。"""
        _m, out = _batch4_factors(ctx={})
        for fid in self.RR:
            self.assertEqual(out[fid].status, "missing_data", fid)
            self.assertIn("锚", out[fid].reason)

    def test_a_loss_maker_keeps_its_rr_group_when_its_peers_have_pe(self):
        """BATCH 4.1 §四–§五：自身亏损**不再吞掉**三档锚。

        这只股票当前的 PE 是负的（``peer_pe_relative`` 该报 not_applicable），
        但三档锚只用「同行当前几倍 PE」——那件事与本公司有没有利润无关。
        旧实现两条语义共用一条 early return，于是这里整组 RR 一起变成
        not_applicable，和它有没有机会毫无关系。
        """
        m, _scored, _final = _analyze()
        m["current"]["liquidation_ratio"] = None
        m["current"]["asset_value_ratio"] = None
        m["annual"] = [{"report_period": "%d-12-31" % y, "deduct_profit": p}
                       for y, p in zip(range(2021, 2026),
                                       (2e8, 3e8, 4e8, 5e8, 6e8))]
        ctx = _anchor_ctx(m, peer_sample=(12.0, 15.0, 18.0, 21.0))
        # 自己亏损：A 语义没有样本（B 语义的 4 家同行 PE 一个不少）。
        ctx["peer"]["valuation"]["pe"] = {
            "own": -8.0, "peer_percentile": None, "peer_count": 4,
            "confidence": PG.CONF_NONE,
            "sample_status": PG.SAMPLE_OWN_NOT_APPLICABLE,
            "reason_code": PG.OWN_PE_NOT_MEANINGFUL, "reason": "公司自身 PE ≤ 0"}
        self.assertEqual(ctx["anchors"]["base"]["status"], VA.STATUS_OK)
        self.assertEqual(ctx["anchors"]["bull"]["status"], VA.STATUS_OK)
        out = F.evaluate(m, rules.score_modules(m), None, None, context=ctx)
        self.assertEqual(out["peer_pe_relative"].status, "not_applicable")
        self.assertEqual(out["peer_pe_relative"].coverage, 1.0)
        for fid in ("base_upside", "bear_downside", "risk_reward_ratio"):
            self.assertEqual(out[fid].status, "ok", fid)


class TestFinancialIsolationFactors(unittest.TestCase):
    """§二十五–§三十一：银行/保险的工业口径因子退出计分，但不掉覆盖度。"""

    def _blocked(self):
        return list((rules.RULES_V1["financial_semantics"] or {})
                    .get("industrial_not_applicable_factors") or [])

    def test_the_declared_block_all_exists_in_the_catalog(self):
        """名单里的每个 id 都必须真的存在——写错一个字母会**静默**屏蔽不了任何东西。"""
        self.assertTrue(self._blocked())
        for fid in self._blocked():
            self.assertIn(fid, F.FACTOR_INDEX)
        self.assertEqual(
            F._financial_blocked_factors({"is_financial": False}), frozenset())
        self.assertEqual(F._financial_blocked_factors({"is_financial": True}),
                         frozenset(self._blocked()))

    def test_blocked_factors_are_not_applicable_without_losing_coverage(self):
        m, scored, final = _analyze("600036", "银行Ⅱ")
        self.assertTrue(m["is_financial"])
        out = F.evaluate(m, scored, None, final, context=_anchor_ctx(m))
        for fid in self._blocked():
            item = out[fid]
            self.assertEqual(item.status, "not_applicable", fid)
            self.assertEqual(item.coverage, 1.0,
                             "%s 是「这个口径对它不成立」而不是「没抓到数」" % fid)
            self.assertFalse(item.eligible, fid)
            self.assertIsNone(item.score, fid)

    def test_the_financial_peer_factors_also_keep_full_coverage(self):
        """银行/保险的两个相对价值因子同样是 1.0，**不是 0.0**。

        它们的 ``not_applicable`` 来自 peer 层的 ``financial_excluded``——与自己
        亏损 / 整组亏损那条出口是两个分支，``coverage`` 必须两边都传。漏传的那个
        出口会让因子表上只有这两格显示「覆盖 0」，读起来正是「这一格没抓到数」，
        也就是 §三十一 明令不要的那种读法。
        """
        group = PG.PeerGroup("BANK", "商业银行", PG.BASIS_EXPLICIT,
                             (("600036", "招商银行"),), PG.SOURCE_CURATED)
        peer = PG.PeerView(
            peer_group=group, own_code="600036",
            rows=[{"stock_code": "600036", "name": "招商银行",
                   "pe": 6.76, "pb": 0.90}]).context()
        m, scored, final = _analyze("600036", "银行Ⅱ")
        ctx = _anchor_ctx(m, model="GENERAL_VALUE_V2")
        ctx["peer"] = peer
        out = F.evaluate(m, scored, None, final, context=ctx)
        for fid in ("peer_pe_relative", "peer_fcf_yield_relative"):
            self.assertEqual(out[fid].status, "not_applicable", fid)
            self.assertEqual(out[fid].coverage, 1.0,
                             "%s 是「这个口径对银行没有定义」，不是「没抓到数」" % fid)
        for fid in ("peer_pb_relative", "peer_quality_adjusted_valuation"):
            self.assertNotEqual(out[fid].status, "not_applicable",
                                "%s 在金融组里是允许的" % fid)

    def test_the_old_components_stay_as_evidence(self):
        """旧算法确实给招行算过清算价值——那件事要查得到，但一分不进新层。"""
        m, scored, final = _analyze("600036", "银行Ⅱ")
        out = F.evaluate(m, scored, None, final, context=_anchor_ctx(m))
        for fid in self._blocked():
            if out[fid].components:
                break
        else:
            self.fail("被屏蔽的因子一个证据都没留下——查不到「旧层本来算了什么」")
        # 三个 RR 因子对金融业同样不适用（§三十一），理由来自锚那一侧。
        for fid in ("base_upside", "bear_downside", "risk_reward_ratio",
                    "anchor_confidence"):
            self.assertEqual(out[fid].status, "not_applicable", fid)
            self.assertEqual(out[fid].coverage, 1.0, fid)


class TestPigFactors(unittest.TestCase):
    """§二十–§二十四：暴露只当适用性，13 个专属因子取不到就 missing。"""

    SPECIALIZED = tuple(fid for fid in F.PIG_FACTOR_IDS if fid != "pig_exposure")

    def test_unknown_exposure_keeps_everything_missing(self):
        pig = pig_exposure.resolve([])
        _m, out = _batch4_factors(ctx=_anchor_ctx(
            _analyze()[0], pig=pig))
        self.assertEqual(out["pig_exposure"].status, "missing_data")
        self.assertIn("没有这家公司的定期报告", out["pig_exposure"].reason)
        for fid in self.SPECIALIZED:
            self.assertEqual(out[fid].status, "missing_data", fid)
            self.assertIsNone(out[fid].raw, fid)

    def test_low_exposure_is_not_applicable_without_penalty(self):
        pig = pig_exposure.resolve(_pig_note(revenue_share=0.267))
        _m, out = _batch4_factors(ctx=_anchor_ctx(_analyze()[0], pig=pig))
        self.assertEqual(out["pig_exposure"].status, "ok")
        self.assertEqual(out["pig_exposure"].score, 26.7)
        for fid in self.SPECIALIZED:
            self.assertEqual(out[fid].status, "not_applicable", fid)
            self.assertEqual(out[fid].coverage, 1.0, fid)

    def test_high_exposure_without_readings_is_still_missing(self):
        """暴露够高、但**没有可靠数据** → 一律 missing。§二十四：不许手填。"""
        pig = pig_exposure.resolve(_pig_note(revenue_share=0.9))
        self.assertEqual(pig["classification"], "PURE_PIG")
        _m, out = _batch4_factors(ctx=_anchor_ctx(_analyze()[0], pig=pig))
        for fid in self.SPECIALIZED:
            self.assertEqual(out[fid].status, "missing_data", fid)
            self.assertIsNone(out[fid].score, fid)

    def test_exposure_itself_is_applicability_not_a_score(self):
        spec = F.FACTOR_INDEX["pig_exposure"]
        self.assertEqual(spec.factor_role, F.ROLE_APPLICABILITY)
        self.assertFalse(spec.contributes_to_score)
        # 批 5：它自己就是一个**因子级门的门源**（pig_industry_from_exposure）。
        # 门只认 APPLICABILITY 型的 factor——SCORE 型没有 classification 可查。
        gate = D.gate_for_group(F.GROUP_PIG_INDUSTRY)
        self.assertIsNotNone(gate)
        self.assertEqual(D.gate_source_kind(gate), "source_factor")
        self.assertEqual(gate["source_factor"], "pig_exposure")
        self.assertEqual(D.class_multiplier_table(gate["config_section"]),
                         rules.RULES_V1["pig"]["gate_multipliers"])

    def test_an_estimate_never_becomes_the_formal_exposure(self):
        """业务描述只能生成 estimate（用户裁定），正式暴露保持 missing。"""
        pig = pig_exposure.resolve([{"report_period": "2025A",
                                     "financial_note": {"status": "not_found"}}])
        self.assertIsNone(pig["exposure"])
        self.assertIn("missing", pig["reason"])
        _m, out = _batch4_factors(ctx=_anchor_ctx(_analyze()[0], pig=pig))
        self.assertEqual(out["pig_exposure"].status, "missing_data")
        self.assertNotEqual(out["pig_exposure"].score, 0.0)


class TestPigIndustryGroup(unittest.TestCase):
    """批 5：猪企组的组内权重 + 因子级门（§三十七–§三十九）。"""

    def test_group_weights_are_configuration_not_literals(self):
        """5 个组成因子的权重全部从 ``GROUP_FACTOR_WEIGHTS`` 读，且和为 1.00。

        批 9 摘掉 ``price_premium``（原 0.10）之后，剩下五格按**原比例**放大
        （各自 ÷0.90 = 5:3:4:3:3 份）。这里断言的是**归一的结果**，不是
        「重新拍过的五个整数」——所以写成分数而不是小数：``5/18`` 一眼能看出
        它来自 ``0.25/0.90``，写成 ``0.2778`` 就看不出这层关系了。
        """
        table = D.GROUP_FACTOR_WEIGHTS[F.GROUP_PIG_INDUSTRY]
        for fid, want in (("margin_position", 0.25 / 0.90),
                          ("sale_price_level", 0.15 / 0.90),
                          ("supply_contraction", 0.20 / 0.90),
                          ("cost_advantage", 0.15 / 0.90),
                          ("capacity_delivery", 0.15 / 0.90)):
            self.assertAlmostEqual(table[fid], want, places=6, msg=fid)
        self.assertNotIn("price_premium", table,
                         "批 9 已摘掉这一格；它回来了就必须在这里重新配权并归一")
        self.assertAlmostEqual(sum(table.values()), 1.0, places=6,
                               msg="正权重五格加起来必须是 1.00")
        self.assertEqual(D.group_factor_weight_errors(), [])

    def test_the_zero_weight_members_are_score_factors_not_applicability_ones(self):
        """0.00 只配给**有定义但不该在这组里再计一次**的 SCORE 成员。"""
        table = D.GROUP_FACTOR_WEIGHTS[F.GROUP_PIG_INDUSTRY]
        zeros = {fid for fid, w in table.items() if w == 0.0}
        self.assertTrue(zeros)
        for fid in sorted(zeros):
            self.assertEqual(F.FACTOR_INDEX[fid].factor_role, F.ROLE_SCORE, fid)
        # 「有 SCORE 成员没配权重」是这条自检存在的理由：漏一行会静默拿默认 1.0。
        self.assertEqual(D.group_factor_weight_errors(), [])

    def test_unknown_exposure_removes_the_group_from_the_denominator(self):
        """暴露未知 → ×0.00：那一组的 0.15 **整块退出维度分母**。

        「不知道有多少业务在猪上」不该让 OPPORTUNITY 的覆盖度掉一块——那会
        把「行业数据源还没接」读成「这家公司数据质量差」。这正是 PENDING_DATA
        当初豁免的那件事，现在由**数据判据**而不是全局豁免来做。
        """
        out = _evaluated()
        group = out["groups"][D.OPPORTUNITY][F.GROUP_PIG_INDUSTRY]
        self.assertEqual(group["declared_weight"], 0.15)
        self.assertEqual(group["applicability_multiplier"], 0.00)
        self.assertEqual(group["effective_declared_weight"], 0.0)
        self.assertEqual(group["contributes_to_dimension"], False)
        report = [g for g in out["applicability_gates"]
                  if g["target_group"] == F.GROUP_PIG_INDUSTRY][0]
        self.assertEqual(report["applicability_source"],
                         D.APPT_SOURCE_CLASS_MISSING)
        self.assertEqual(report["applicability_confidence"], 0.0)
        self.assertEqual(report["source_kind"], "source_factor")

    def test_the_multiplier_follows_the_classification_code(self):
        """同一个读数、不同的分类码 → 不同的倍数，且都照 RULES_V1 的表。"""
        table = rules.RULES_V1["pig"]["gate_multipliers"]
        for code, want in sorted(table.items()):
            payload = {"pig_exposure": {"status": "ok",
                                        "raw": {"exposure": 0.9,
                                                "classification": code}}}
            gate = D.gate_for_group(F.GROUP_PIG_INDUSTRY)
            ap = D.exposure_class_applicability(gate, payload)
            self.assertEqual(ap["multiplier"], want, code)
            self.assertEqual(ap["source"], D.APPT_SOURCE_CLASS, code)
            self.assertEqual(ap["classification"], code, code)

    def test_a_missing_classification_never_borrows_another_code(self):
        """读不到分类 → 走 ``gate_missing_multiplier``，**不拿行业名或默认档顶替**。"""
        gate = D.gate_for_group(F.GROUP_PIG_INDUSTRY)
        for payload in ({}, {"pig_exposure": {"status": "missing_data", "raw": None}},
                        {"pig_exposure": {"status": "ok", "raw": {}}}):
            ap = D.exposure_class_applicability(gate, payload)
            self.assertEqual(ap["multiplier"],
                             rules.RULES_V1["pig"]["gate_missing_multiplier"])
            self.assertEqual(ap["source"], D.APPT_SOURCE_CLASS_MISSING)
            self.assertIsNone(ap["classification"])
            self.assertTrue(ap["reason"])

    def test_a_config_drift_is_reported_not_silently_defaulted(self):
        """分类码不在表里（配置漂移）→ 与「读不到」同一档，但理由必须不同。"""
        gate = D.gate_for_group(F.GROUP_PIG_INDUSTRY)
        ap = D.exposure_class_applicability(
            gate, {"pig_exposure": {"status": "ok",
                                    "raw": {"classification": "NO_SUCH_CLASS"}}})
        self.assertEqual(ap["multiplier"],
                         rules.RULES_V1["pig"]["gate_missing_multiplier"])
        self.assertIn("NO_SUCH_CLASS", ap["reason"])

    def test_the_class_table_is_checked_at_config_time(self):
        """漏配一个分类码只会在「恰好撞上那个档位」时发作——在配置层查掉。"""
        self.assertEqual(D.class_gate_config_errors(), [])
        self.assertEqual(D.gates_with_unknown_groups(), [])
        self.assertEqual(D.group_factor_weight_errors(), [])


if __name__ == "__main__":
    unittest.main()
