# -*- coding: utf-8 -*-
"""``factor_audit`` —— 只读的 factor 审计表。

这一层最容易出的错**不是算错，而是算第二遍**：审计表如果能重算，它就会在规则
改动之后与界面各说各话，而两边都「看着对」。所以这里钉住三件事：

1. 表里的每个数都能在载荷里找到出处（贡献链自洽、来源只挂在真用它的组上）；
2. 它不重算、不写库（源码里没有评分入口、跑一遍行数不变）；
3. 门禁照走——未审计的股票在这张表上也看不到分数。
"""
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import research.db as research_db                       # noqa: E402
from research import audit_job, engine, factor_audit, factor_store  # noqa: E402
from research import factors as F                       # noqa: E402
from research import peer_groups as PG                  # noqa: E402
from research import pig_exposure, rules, valuation_anchors as VA  # noqa: E402
from tests.test_pig_exposure import _report as _pig_report  # noqa: E402
from tests.test_research import _SYNTH_QUOTE, _synth_fin  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具：真库 + 真落库路径（``_persist``），只有外部上下文是手给的
# --------------------------------------------------------------------------- #
def _market_ctx(conflict=False):
    """一份**离线**的 market 读数。段的划分照 ``RULES_V1["market"]``，factor_id
    就是键——写错段名不会报错，只会静默变成 missing，所以下面有测试盯着 ok。"""
    return {
        "status": "ok", "note": None, "source": "eastmoney", "basis": "qfq",
        "bar_count": 320, "as_of": "2026-09-25", "source_conflict": conflict,
        "conflict_fields": ["close"] if conflict else [],
        "benchmark": {"code": "sh000300", "name": "沪深300", "return_60d": 0.04},
        "trend": {"trend_20d": 0.05, "trend_60d": 0.12, "trend_120d": 0.2,
                  "relative_strength_60d": 0.03, "distance_from_120d_high": -0.08},
        "attention": {"amount_percentile_20d": 0.55, "amount_percentile_60d": 0.6,
                      "turnover_percentile_20d": 0.5,
                      "turnover_percentile_60d": 0.5, "volume_ratio": 1.1},
        "liquidity": {"amount_to_float_cap_20d": 0.012,
                      "free_float_market_cap": 1.2e10},
        "overhang": {"unlock_ratio_12m": 0.0, "holder_num_change": -3.0,
                     "holder_reduction_count_12m": 0.0},
        "reasons": {},
    }


def _peer_ctx(available=True, financial_excluded=None):
    rel = {"own": 12.0, "peer_median": 15.0, "peer_percentile": 0.8,
           "peer_count": 5, "confidence": "high", "excluded": [], "reason": None}
    # 横截面样本：Base/Bull 倍数要用它。BATCH 4.1 §一 起锚读
    # ``peer_pe_multiple``（B 语义：**同行自己的** PE 分布），**不再读**
    # ``valuation["pe"]["sample"]``（A 语义：本公司在组内的位置）。
    # 少了这个键，锚不会报错，只会静默变成「同组内没有正 PE 样本」的 missing——
    # 正是本文件开头说的那种「不报错但各说各话」。
    sample = [11.0, 13.0, 15.0, 17.0, 19.0]
    return {
        "available": available,
        "peer_group": "PIG_DIVERSIFIED", "display_name": "猪产业（多元）",
        "member_count": 5, "basis": "pig", "as_of": "2026-09-25",
        "reason": None if available else "peer 组里取不到样本",
        "describe": "测试用 peer 组", "missing_names": ["新希望"],
        "name_mismatches": [],
        "financial_excluded": dict(financial_excluded or {}),
        "valuation": {"pe": dict(rel, sample=list(sample)),
                      "pb": dict(rel, sample=list(sample))},
        "peer_pe_multiple": {
            "source": "peer_groups.PIG_DIVERSIFIED.pe",
            "peer_positive_pe_count": len(sample),
            "peer_pe_values": list(sample),
            "peer_pe_median": PG.quantile(sample, 0.5),
            "peer_pe_p25": PG.quantile(sample, 0.25),
            "peer_pe_p75": PG.quantile(sample, 0.75),
            "peer_pe_as_of": "2026-09-25", "peer_pe_confidence": "high",
            "sample_status": PG.SAMPLE_OK, "reason": None,
            "excluded_nonpositive": [], "excluded_missing": [],
        },
        "fundamental": {"fcf_yield": dict(rel)},
        "quality_adjusted": {"valuation_gap": 0.15, "peer_count": 4,
                             "confidence": 1.0, "confidence_label": "high",
                             "reason": None, "method": "分位简单平均"},
    }


def _context(peer=None, market=None, unmapped=None, pe_series=None, pig=None):
    ctx = {"peer": peer if peer is not None else _peer_ctx(),
           "market": market if market is not None else _market_ctx(),
           "unmapped_industries": list(unmapped or [])}
    # ``pe_series`` / ``pig`` 只在显式给了的时候才放进上下文：``_research_context``
    # 一定会带这两个键，而「没带」与「带了但是空的」在锚那一侧是两句不同的理由。
    if pe_series is not None:
        ctx["pe_series"] = list(pe_series)
    if pig is not None:
        ctx["pig"] = pig
    return ctx


def _pe_series(count=60, start=12.0):
    """``[(月, pe_ttm)]`` 升序——60 个月对应置信度 1.0 那一档。"""
    return [("%d-%02d" % (2021 + i // 12, i % 12 + 1), start + (i % 5) * 0.3)
            for i in range(count)]


def _pig_ctx(revenue_share=0.2669, audit_scope="financial_statement_note"):
    """一份解析出来的猪业务暴露（走真的 ``pig_exposure.resolve``，不手搭字典）。"""
    return pig_exposure.resolve([_pig_report(revenue_share=revenue_share,
                                             audit_scope=audit_scope)])


#: 「没给 context」与「给了一个空 context」是两件事，用哨兵区分——默认值写成
#: ``None`` 会让每个夹具都悄悄退化成「没有外部数据」，而测试照样绿。
_DEFAULT = object()


def _relative_value_ids():
    return [s.factor_id for s in F.factors_in_group(F.GROUP_RELATIVE_VALUE)]


class AuditCase(unittest.TestCase):

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.conn = research_db.init_db(self.path)
        self.addCleanup(self.conn.close)

    # -- 夹具 -------------------------------------------------------------- #
    def _persist(self, code="002714", industry="养殖业", context=_DEFAULT):
        if context is _DEFAULT:
            context = _context()
        m, _q, _f = engine.build_metrics(code, _SYNTH_QUOTE, _synth_fin(industry))
        result = engine._run_analysis(m, context=context)
        engine._persist(self.conn, code, _SYNTH_QUOTE, m, result, "2026-06-30",
                        industry, False)
        return result

    def _audited(self, code="002714", **kw):
        """落一次库 + 补一份资产快照——「有快照就算完成」是门禁的完成判据。"""
        result = self._persist(code=code, **kw)
        self.conn.execute(
            "INSERT INTO asset_semantic_snapshot (stock_code, date, metric_version,"
            " report_period, total_assets, source_document)"
            " VALUES (?, '2026-09-01 00:00:00', 'ASSET_SEMANTIC_ENGINE_V1.0',"
            " '2026-06-30', ?, '测试')", (code, 30e9))
        self.conn.commit()
        return result

    def _loaded(self, code):
        return factor_store.load_layer(self.conn, code)


# --------------------------------------------------------------------------- #
# 1. 每个数都能在载荷里找到出处
# --------------------------------------------------------------------------- #
class TestRowsTraceBackToTheLayer(AuditCase):

    def setUp(self):
        super().setUp()
        self._audited()
        self.out = factor_audit.audit(self.conn, "002714")
        self.by_id = {r["factor_id"]: r for r in self.out["factors"]}

    def test_every_stored_factor_gets_exactly_one_row(self):
        layer = self._loaded("002714")
        self.assertEqual(sorted(self.by_id), sorted(layer["factors"]))

    def test_the_contribution_chain_multiplies_out(self):
        """``contribution`` 必须等于 ``effective_weight × score``。

        这就是「一眼能对账」的全部含义：三层权重（组内 → 组 → 维度）已经乘进
        ``effective_weight``，贡献点数不再有第二个算法。
        """
        for fid, r in self.by_id.items():
            if r["contribution"] is None or r["effective_weight"] is None:
                continue
            if r["score"] is None:
                self.assertEqual(r["contribution"], 0.0, fid)
                continue
            self.assertAlmostEqual(r["contribution"],
                                   round(r["effective_weight"] * r["score"], 4),
                                   places=3, msg=fid)

    def test_each_dimension_scores_from_its_own_factors(self):
        """逐 factor 贡献点数之和 == 维度分 × 本维实际花出去的份额。

        **为什么右边要乘 ``effective_weight``**：``_combine`` 是拿 ``used`` 归一化的
        （``score = Σ s·eff / used``）。有一份声明权重因为封顶没花出去时
        （``unallocated_weight > 0``），贡献之和**本来就少于**维度分，少的恰好是
        ``score × unallocated_weight``。所以两边同量纲的写法只有乘上 ``used`` 这一种。

        这条断言以前写成 ``total + unallocated_weight >= score``——那是把**权重份额**
        当**点数**加，只在 ``unallocated`` 恰为 0 时成立；而它恰为 0 时式子恒真、
        什么都测不到（``total`` 那时就等于 ``score``）。批 4 给 OPPORTUNITY 加了一个
        暂时还没有数据的组（risk_reward 的 4 个因子在夹具里全 missing），封顶第一次
        真的咬合，那个式子才露馅。**修的是断言，不是行为**：``_combine`` 的分母语义
        没变，封顶咬合时报 ``unallocated_weight`` 也正是它该做的。

        容差 0.05 是**逐个 round(4) 累加** + 维度分自身 round(2) 的舍入，不是模型误差。
        """
        dims = self._loaded("002714")["dimensions"]
        for dim in sorted(dims):
            rows = [r for r in self.by_id.values()
                    if r["dimension"] == dim and r["contributes_to_dimension"]]
            total = sum(r["contribution"] or 0.0 for r in rows)
            score = dims[dim]["score"]
            if score is None:
                self.assertEqual(rows, [], dim)
                continue
            self.assertAlmostEqual(total, score * dims[dim]["effective_weight"],
                                   delta=0.05, msg=dim)
            # 封顶咬合是**例外**而非常态：没咬合时两边必须严格相等，否则
            # 「贡献之和 == 维度分」这条最简单的账在任何时候都对不上。
            if dims[dim]["unallocated_weight"] == 0.0:
                self.assertAlmostEqual(total, score, delta=0.05, msg=dim)

    def test_a_status_of_display_only_is_not_a_missing_factor(self):
        """``free_float_market_cap`` 有值时也不许混进 MARKET 的分母。

        它一度在有值时返回 ``ok``，后果是**每一只**股票都恒为 market_partial，
        而真正缺数据的那种 partial 就再也看不出来了。
        """
        ff = self.by_id["free_float_market_cap"]
        self.assertEqual(ff["status"], F.STATUS_DISPLAY_ONLY)
        self.assertEqual(ff["kind"], "display_only")
        self.assertIsNotNone(ff["raw"], "这一格是有值的——有值也不进分")

    def test_the_raw_value_travels_with_the_row(self):
        self.assertIsNotNone(self.by_id["trend_60d"]["raw"])
        self.assertEqual(self.by_id["trend_60d"]["unit"], "percent")


# --------------------------------------------------------------------------- #
# 2. 来源只挂在真的用了它的组上
# --------------------------------------------------------------------------- #
class TestDataSourceAttribution(AuditCase):

    def setUp(self):
        super().setUp()
        self._audited()
        self.by_id = {r["factor_id"]: r for r in
                      factor_audit.audit(self.conn, "002714")["factors"]}

    def test_market_source_is_attached_only_where_the_series_is_read(self):
        """``market_regime`` 也以 ``market`` 开头，但它的三个 factor 一个都不读日线。

        给 ``capex_cycle`` 挂一个「数据源 = 东财日线」是编的，而审计表上编出来的
        来源比没有来源更坏——用户会拿它去解释一个它管不着的数。
        """
        for fid in ("capex_cycle", "risk_level", "industry_prior"):
            self.assertIsNone(self.by_id[fid]["data_source"], fid)
        for fid in ("trend_60d", "amount_percentile_20d", "unlock_ratio_12m"):
            src = self.by_id[fid]["data_source"]
            self.assertEqual(src["kind"], "market", fid)
            self.assertEqual(src["source"], "eastmoney", fid)
            self.assertEqual(src["bar_count"], 320, fid)

    def test_peer_rows_carry_the_group_the_count_and_the_missing_names(self):
        for fid in _relative_value_ids():
            src = self.by_id[fid]["data_source"]
            self.assertEqual(src["kind"], "peer", fid)
            self.assertEqual(src["peer_group"], "PIG_DIVERSIFIED", fid)
            self.assertEqual(src["peer_member_count"], 5, fid)
            self.assertEqual(src["missing_names"], ["新希望"], fid)

    def test_a_factor_that_reads_nothing_external_says_so_with_none(self):
        self.assertIsNone(self.by_id["roic_level"]["data_source"])
        self.assertIsNone(self.by_id["dividend_yield"]["data_source"])

    def test_a_source_conflict_is_reported_and_discounts_confidence(self):
        self.conn.execute("DELETE FROM research_stocks")
        self.conn.execute("DELETE FROM factor_analysis_runs")
        self.conn.commit()
        self._audited(context=_context(market=_market_ctx(conflict=True)))
        out = factor_audit.audit(self.conn, "002714")
        by_id = {r["factor_id"]: r for r in out["factors"]}
        self.assertTrue(by_id["trend_60d"]["data_source"]["source_conflict"])
        self.assertAlmostEqual(by_id["trend_60d"]["confidence"], 0.5, places=6)
        self.assertEqual(out["stock"]["market_source_conflict"], True)


# --------------------------------------------------------------------------- #
# 3. 「缺数据」「不适用」「只展示」是三件事
# --------------------------------------------------------------------------- #
class TestMissingIsNotZeroAndNotNotApplicable(AuditCase):

    def test_missing_market_data_is_reported_not_scored_zero(self):
        self._audited(context=_context(market={"status": "error",
                                              "reasons": {"*": "取数失败"}}))
        out = factor_audit.audit(self.conn, "002714")
        by_id = {r["factor_id"]: r for r in out["factors"]}
        self.assertEqual(by_id["trend_60d"]["kind"], "missing")
        self.assertIsNone(by_id["trend_60d"]["score"],
                          "缺数据不许写成 0 分——那会让拿不到数据的股票系统性更差")
        self.assertTrue(out["stock"]["market_missing"])
        self.assertIsNone(out["stock"]["dimension_scores"]["MARKET"])

    def test_a_financial_exclusion_is_not_loaded_into_the_missing_bucket(self):
        """银行/保险退出的因子是 ``not_applicable``，不是 ``missing``。

        两者的分母语义不同（``_assemble`` 对前者给 coverage 1.0、对后者给 0.0），
        混成一个「缺」的列表，用户会把银行读成「数据不全」。
        """
        self._audited(context=_context(peer=_peer_ctx(financial_excluded={
            "peer_pe_relative": "金融行业的资产负债表结构与工商企业不可比"})))
        out = factor_audit.audit(self.conn, "002714")
        by_id = {r["factor_id"]: r for r in out["factors"]}
        self.assertEqual(by_id["peer_pe_relative"]["status"], "not_applicable")
        self.assertEqual(by_id["peer_pe_relative"]["kind"], "not_applicable")
        self.assertIn("peer_pe_relative", out["stock"]["not_applicable_factors"])
        self.assertNotIn("peer_pe_relative", out["stock"]["missing_factors"])
        self.assertIn("金融行业", by_id["peer_pe_relative"]["reason"])


# --------------------------------------------------------------------------- #
# 4. 全库模式：给的是对账，不是两千行明细
# --------------------------------------------------------------------------- #
class TestLibraryMode(AuditCase):

    def setUp(self):
        super().setUp()
        for code, industry in (("002714", "养殖业"), ("000876", "养殖业")):
            self._audited(code=code, industry=industry)
        self.out = factor_audit.audit(self.conn)

    def test_it_reports_every_audited_stock(self):
        self.assertEqual(self.out["mode"], "library")
        self.assertEqual([s["stock_code"] for s in self.out["stocks"]],
                         ["000876", "002714"])
        self.assertEqual(self.out["counts"]["stocks_included"], 2)

    def test_the_library_view_is_one_row_per_factor_not_per_stock(self):
        """全库要是也逐格铺开，就是两千行——而不看的表等于没有。"""
        for row in self.out["factors"]:
            self.assertNotIn("stock_code", row)
            self.assertIn("stocks_total", row)
            self.assertIn("scored_on", row)
        self.assertEqual(len(self.out["factors"]),
                         len(self._loaded("002714")["factors"]))

    def test_it_names_the_factors_that_scored_nowhere(self):
        by_id = {r["factor_id"]: r for r in self.out["factors"]}
        for fid in _relative_value_ids():
            row = by_id[fid]
            self.assertEqual(row["stocks_scored"], 2, fid)
            self.assertEqual(sorted(row["scored_on"]), ["000876", "002714"], fid)
            self.assertEqual(row["kind_counts"].get("missing", 0), 0, fid)

    def test_unmapped_industries_come_from_the_layers_not_from_a_hand_list(self):
        self.conn.execute("DELETE FROM research_stocks")
        self.conn.execute("DELETE FROM factor_analysis_runs")
        self.conn.commit()
        self._audited(context=_context(unmapped=["某个没有 peer 组的行业"]))
        out = factor_audit.audit(self.conn)
        self.assertEqual(out["unmapped_industries"], ["某个没有 peer 组的行业"])

    def test_a_stale_layer_is_named_instead_of_silently_recomputed(self):
        """规则改过而这只没重算时，两组数都在——不替它挑一个。

        载荷里的四维是**当前权重**重建的，库里的四维行是落库那一刻的。审计表
        敢用前者，就必须把差异说出来，否则它就是在按新权重展示一次旧分析。
        """
        self.conn.execute(
            "UPDATE dimension_snapshots SET score = score + 5.0"
            " WHERE dimension_id = 'VALUE' AND analysis_id IN"
            " (SELECT analysis_id FROM factor_analysis_runs WHERE stock_code='002714')")
        self.conn.commit()
        out = factor_audit.audit(self.conn)
        stale = {s["stock_code"]: s for s in out["stale_layers"]}
        self.assertIn("002714", stale)
        self.assertTrue(any(m["dimension_id"] == "VALUE"
                            for m in stale["002714"]["mismatches"]))
        self.assertNotIn("000876", stale)


# --------------------------------------------------------------------------- #
# 4b. 外部事实要活过落库这一趟
# --------------------------------------------------------------------------- #
class TestExternalFactsSurviveTheRoundTrip(AuditCase):
    """读侧的 ``_meta`` 是「静态目录 + 落库时存下的外部事实」。

    少了后半句，peer 组 / MARKET 源 / 落空行业在库里读出来一律是 ``None``——
    而那让审计页分不清「这一次没取到」和「这一层把它丢了」。前者是事实，
    后者是 bug，长得一模一样。
    """

    def test_the_read_side_meta_matches_the_write_side_meta(self):
        written = self._audited()["factor_layer"]["_meta"]
        rebuilt = factor_store.load_layer(self.conn, "002714")["_meta"]
        # 先确认写侧那几个键**真的有值**，否则两边同为 None 也算「相等」，
        # 这条测试就变成了「两个空字典相等」。
        self.assertEqual(written["peer_group"], "PIG_DIVERSIFIED")
        self.assertEqual(written["market_source"], "eastmoney")
        for key in factor_store.CONTEXT_META_KEYS:
            self.assertEqual(rebuilt.get(key), written.get(key), key)

    def test_a_row_without_the_facts_reads_back_as_none_not_as_an_error(self):
        """批 3 之前落的行没有这一列，读出来必须是 ``None``（旧口径），不是崩。"""
        self._audited()
        self.conn.execute("UPDATE factor_analysis_runs SET context_json = NULL")
        self.conn.commit()
        meta = factor_store.load_layer(self.conn, "002714")["_meta"]
        self.assertIsNone(meta["peer_group"])
        self.assertEqual(meta["unmapped_industries"], [])

    def test_the_audit_table_can_name_the_peer_group_it_actually_used(self):
        self._audited()
        out = factor_audit.audit(self.conn, "002714")
        by_id = {r["factor_id"]: r for r in out["factors"]}
        src = by_id["peer_pb_relative"]["data_source"]
        self.assertEqual(src["peer_group"], "PIG_DIVERSIFIED")
        self.assertEqual(src["peer_member_count"], 5)
        self.assertEqual(src["peer_basis"], "pig")


# --------------------------------------------------------------------------- #
# 5. 门禁与「不重算」
# --------------------------------------------------------------------------- #
class TestTheGateAndTheNoRecomputeRule(AuditCase):

    def test_an_unaudited_stock_shows_no_layer(self):
        self._persist()          # 落库了，但没有资产快照 → 门禁不放行
        out = factor_audit.audit(self.conn, "002714")
        self.assertTrue(out["blocked"])
        self.assertEqual(out["factors"], [])
        self.assertEqual(out["skipped"][0]["stock_code"], "002714")
        self.assertIn(audit_job.label_of(out["skipped"][0]["audit_status"]),
                      out["skipped"][0]["reason"])

    def test_an_unaudited_stock_is_left_out_of_the_library_view_too(self):
        self._persist()
        out = factor_audit.audit(self.conn)
        self.assertEqual(out["stocks"], [])
        self.assertEqual(out["factors"], [])
        self.assertEqual(out["counts"]["stocks_skipped"], 1)

    def test_running_the_audit_writes_nothing(self):
        self._audited()
        before = factor_store.counts(self.conn)
        research_db_before = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM research_stocks")]
        factor_audit.audit(self.conn)
        factor_audit.audit(self.conn, "002714")
        self.assertEqual(factor_store.counts(self.conn), before)
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT * FROM research_stocks")], research_db_before)

    def test_the_module_has_no_scoring_entry_point(self):
        """审计层不许出现评分入口——它一旦能自己算，就有了第二个口径。"""
        src = (ROOT / "research" / "factor_audit.py").read_text(encoding="utf-8")
        for banned in ("score_modules", "final_score", "_run_analysis",
                       "market_context", "peer_groups.view", "INSERT INTO",
                       "UPDATE ", "DELETE FROM"):
            self.assertNotIn(banned, src, f"factor_audit 里出现了 {banned!r}")

    def test_a_broken_connection_returns_an_error_not_an_exception(self):
        conn = sqlite3.connect(":memory:")
        conn.close()
        out = factor_audit.audit(conn)
        self.assertEqual(out["mode"], "error")
        self.assertEqual(out["factors"], [])

    def test_a_stock_without_a_layer_is_skipped_with_a_reason(self):
        """有快照、有分数、但**没有 factor 层**（还没跑过新口径）——跳过并说明。

        ``total_score`` 不能留空：``display_status`` 会把「有快照但没出分」如实降级
        成未审计（那是审计 worker 中途那几十秒的真实状态），于是这只股票会因为
        另一个理由被挡——那样这条测试测的就不是它要测的那条路了。
        """
        research_db.upsert_stock(self.conn, {
            "code": "600000", "name": "测试", "board": "SH", "industry": "银行",
            "system_type": None, "user_type": None, "type_confidence": None,
            "risk_level": None, "total_score": 70.0, "rule_version": None,
            "latest_report_period": None, "attr_scores_json": None,
            "category_scores_json": None, "valuation_json": None,
            "financial_json": None, "risk_json": None, "data_completeness": None,
            "first_analyzed_at": None, "last_updated_at": None,
            "financial_updated_at": None, "router_version": None,
            "primary_model": None, "secondary_model": None, "primary_fit": None,
            "secondary_fit": None, "route_status": None, "route_confidence": None,
            "route_coverage": None, "route_json": None})
        self.conn.execute(
            "INSERT INTO asset_semantic_snapshot (stock_code, date, metric_version,"
            " report_period, total_assets, source_document)"
            " VALUES ('600000', '2026-09-01 00:00:00', 'ASSET_SEMANTIC_ENGINE_V1.0',"
            " '2026-06-30', 1.0, '测试')")
        self.conn.commit()
        out = factor_audit.audit(self.conn)
        self.assertEqual(out["counts"]["stocks_included"], 0)
        self.assertEqual(out["counts"]["stocks_skipped"], 1)
        self.assertIn("没有 factor 层", out["skipped"][0]["reason"])


# --------------------------------------------------------------------------- #
# 6. 批 4：审计表要能说出「这一格的锚是怎么来的」
# --------------------------------------------------------------------------- #
class TestBatch4Fields(AuditCase):
    """§三十二的五个字段。

    它们的共同纪律是**从载荷派生、不重算**：锚用了什么方法、那一刻什么状态、
    这一组锚整体多硬、猪业务暴露从哪一段分部表来、适用性门的权重倍数是谁给的、
    金融语义把哪几个因子判成了不适用。重算一遍就会拿今天的规则解释昨天存下来
    的分数——那正是本模块开头「不重算」那一段的理由。所以下面大多数断言都是
    「审计表的这一格 == 载荷里的那一格」。
    """

    def setUp(self):
        super().setUp()
        self._audited(context=_context(pe_series=_pe_series(), pig=_pig_ctx()))
        self.out = factor_audit.audit(self.conn, "002714")
        self.row = self.out["stock"]
        self.layer = self._loaded("002714")

    def _fresh(self, code="600036", industry="银行Ⅱ", pig=None, pe_series=None):
        """换一只股票重来：审计是「一层一层读库」，同一个库里落两层会串味。"""
        self.conn.execute("DELETE FROM research_stocks")
        self.conn.execute("DELETE FROM factor_analysis_runs")
        self.conn.commit()
        self._audited(code=code, industry=industry,
                      context=_context(pig=pig, pe_series=pe_series))
        out = factor_audit.audit(self.conn, code)
        return out["stock"]

    # -- 锚 ---------------------------------------------------------------- #
    def test_the_anchor_methods_are_copied_from_the_layer_not_recomputed(self):
        raw = self.layer["factors"]["base_upside"]["raw"]
        self.assertEqual(
            self.row["anchor_method"],
            {t: (raw.get(t) or {}).get("method") for t in factor_audit.ANCHOR_TIERS})
        # 三档**恒有三个键**：``None`` 是「这一档没有方法」，省略键是「这个字段
        # 不存在」——界面分不出这两者。
        self.assertEqual(sorted(self.row["anchor_method"]),
                         sorted(factor_audit.ANCHOR_TIERS))
        self.assertEqual(self.row["anchor_method"]["base"], VA.METHOD_PEER_MEDIAN_PE)
        self.assertEqual(self.row["anchor_status"]["base"], "ok")

    def test_a_blocked_tier_still_reports_its_method_and_its_status(self):
        """Bear 取不到时**不是**把这个字段抹掉：状态是 missing 且有方法码 ``none``。

        这只的 P25 扣非利润是负的（养殖业 8 年里的低景气年本来就亏），所以下行
        锚取不到——审计表必须能说出「这一档没有锚」，否则读起来像是「忘了填」。
        """
        self.assertEqual(self.row["anchor_status"]["bear"], "missing_data")
        self.assertEqual(self.row["anchor_method"]["bear"], VA.METHOD_NONE)
        # 「为什么取不到」在锚记录自己的 reason 里（带分位与样本区间），不在因子的
        # reason 里——后者只说「见锚的 reason」。落库时两条都跟着 raw 一起存下来了。
        reason = self.layer["factors"]["bear_downside"]["raw"]["bear"]["reason"]
        self.assertIn("P25", reason)
        self.assertIn("8 个完整年度", reason)

    def test_anchor_confidence_is_read_from_the_layer(self):
        self.assertAlmostEqual(
            self.row["anchor_confidence"],
            self.layer["factors"]["anchor_confidence"]["raw"], places=6)
        self.assertGreater(self.row["anchor_confidence"], 0.0)

    def test_a_layer_without_the_group_reports_none_rather_than_omitting_the_keys(self):
        """批 4 之前落的老层没有 risk_reward 行——如实说「没有」，不是崩也不是 0。"""
        self.assertEqual(factor_audit._anchor_fields({}), (None, None, None))
        bare = {"base_upside": {"raw": None, "components": []},
                "bear_downside": {"raw": None, "components": []}}
        methods, statuses, conf = factor_audit._anchor_fields(bare)
        self.assertEqual(methods, {t: None for t in factor_audit.ANCHOR_TIERS})
        self.assertEqual(statuses, {t: None for t in factor_audit.ANCHOR_TIERS})
        self.assertIsNone(conf)

    # -- 猪业务暴露 --------------------------------------------------------- #
    def test_the_business_exposure_carries_its_source_and_its_confidence(self):
        exp = self.row["business_exposure"]
        self.assertEqual(exp["source"], "segment_revenue")
        self.assertAlmostEqual(exp["exposure"], 0.2669, places=6)
        self.assertEqual(exp["classification"], "DIVERSIFIED_AGRI")
        self.assertFalse(exp["is_estimated"], "这是解析出来的占比，不是估计值")
        self.assertGreater(exp["confidence"], 0.0)
        # 它自己那一格也不许手填一个数。
        self.assertEqual(self.layer["factors"]["pig_exposure"]["status"], "ok")

    def test_an_unknown_exposure_stays_missing_and_keeps_the_reason(self):
        """解析不出分部占比 → 正式暴露 missing（用户裁定），审计表照实转述。"""
        row = self._fresh(code="002714", industry="养殖业", pig=None,
                          pe_series=_pe_series())
        exp = row["business_exposure"]
        self.assertEqual(exp["status"], "missing_data")
        self.assertIsNone(exp["exposure"])
        self.assertFalse(exp["is_estimated"])
        self.assertTrue(exp["reason"])

    def test_an_estimate_is_flagged_and_never_becomes_the_formal_exposure(self):
        """有 estimate 时：``is_estimated`` 必须为真，且它**不是** exposure。"""
        pig = pig_exposure.resolve([{"report_period": "2025A",
                                     "financial_note": {"status": "not_found"}}])
        pig = dict(pig, estimate=0.55, estimate_is_estimated=True)
        row = self._fresh(code="002714", industry="养殖业", pig=pig,
                          pe_series=_pe_series())
        exp = row["business_exposure"]
        self.assertIsNone(exp["exposure"])
        self.assertTrue(exp["is_estimated"])
        self.assertAlmostEqual(exp["estimate"], 0.55, places=6)

    # -- 适用性门与金融语义 -------------------------------------------------- #
    def test_the_applicability_gates_name_their_source(self):
        gates = self.row["applicability_source"]
        self.assertTrue(gates, "夹具里 cyclical_exposure 明明给 opportunity 加了权重")
        known = {gid for gid, _label in F.FACTOR_GROUPS}
        for target, gate in gates.items():
            self.assertIn(target, known)
            self.assertTrue(gate["gate_id"], target)
            self.assertTrue(gate["source"], target)
            self.assertIsNotNone(gate["multiplier"], target)

    def test_the_declared_list_is_real_and_nothing_is_blocked_for_an_industrial(self):
        sem = self.row["financial_semantics"]
        self.assertFalse(sem["is_financial"])
        self.assertTrue(sem["declared_factors"])
        for fid in sem["declared_factors"]:
            self.assertIn(fid, F.FACTOR_INDEX, fid)
        self.assertEqual(sem["blocked_factors"], [],
                         "非金融股被屏蔽了因子——那是把判据放宽了")
        self.assertIsNone(sem["reason"])
        self.assertEqual(sorted(sem["still_applicable_groups"]),
                         sorted(rules.RULES_V1["financial_semantics"]
                                ["still_applicable_groups"]))

    def test_a_bank_blocks_exactly_the_declared_list_and_keeps_coverage(self):
        row = self._fresh(pig=_pig_ctx(), pe_series=_pe_series())
        sem = row["financial_semantics"]
        self.assertTrue(sem["is_financial"])
        self.assertTrue(sem["reason"])
        self.assertEqual(sorted(sem["blocked_factors"]),
                         sorted(sem["declared_factors"]))
        for fid in sem["blocked_factors"]:
            self.assertIn(fid, F.FACTOR_INDEX, fid)

    def test_the_blocked_rows_land_in_not_applicable_not_in_missing(self):
        """两者的分母语义不同：混进 missing 列表，用户会把招行读成「数据不全」。"""
        row = self._fresh(pig=_pig_ctx(), pe_series=_pe_series())
        out = factor_audit.audit(self.conn, "600036")
        by_id = {r["factor_id"]: r for r in out["factors"]}
        for fid in row["financial_semantics"]["blocked_factors"]:
            self.assertEqual(by_id[fid]["status"], "not_applicable", fid)
            self.assertEqual(by_id[fid]["kind"], "not_applicable", fid)
            self.assertNotIn(fid, row["missing_factors"], fid)
        self.assertIn(row["financial_semantics"]["blocked_factors"][0],
                      row["not_applicable_factors"])

    def test_the_financial_rows_have_no_anchor_to_report(self):
        """金融股的四格不是「忘了写锚」，是**这一格里没有可用的锚**——三档全 None。"""
        row = self._fresh(pig=_pig_ctx(), pe_series=_pe_series())
        self.assertEqual(row["anchor_method"],
                         {t: None for t in factor_audit.ANCHOR_TIERS})
        self.assertIsNone(row["anchor_confidence"])


if __name__ == "__main__":
    unittest.main()
