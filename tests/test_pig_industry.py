# -*- coding: utf-8 -*-
"""猪企专属数据层（批 5，spec §十三–§四十四）。

这一层最容易犯的两个错，与 ``test_pig_exposure.py`` 那两个是同一族的：

1. **把「有数据」读成「数据能用」**——一条区间披露（「资产占比不超过 22%」）与
   一个精确值在记录表里只差一列；一个分部毛利率（%）与一个每公斤毛利（元/公斤）
   在界面上都叫「毛利」。所以这里逐条钉住 ``status`` 与 ``metric_variant``。
2. **把缺口写成空白**——22 格里本批大多数是 MISSING，这不是缺陷清单而是现状的
   如实记账。**每一格都必须说得出缺的是什么**，否则「没有来源」「解析失败」
   「还没做」三种事在界面上长得一模一样。

另外钉住用户裁定里那几条硬约束：来源七级优先级、推算必须 ``is_estimated``、
模糊披露只给区间不给值、口径不许合并。
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import factors as F, rules                      # noqa: E402
from research import dimensions as D                          # noqa: E402
from research.industry import pig as PIG                      # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具：形状照 ``pig_reports.inspect_cached_pig_report`` 的产物
# --------------------------------------------------------------------------- #
def _report(period="2025A", revenue_share=0.2669, gross_profit_share=0.31,
            assets_share=0.22, audit_scope="financial_statement_note",
            status="extracted", segment_label="猪产业", revenue=1.2e10,
            cogs=1.0e10, source_page=88, product_label="生猪"):
    return {
        "report_period": period, "status": "extracted",
        "financial_note": {
            "status": status, "segment_label": segment_label,
            "audit_scope": audit_scope,
            "exposure": {"revenue_share": revenue_share,
                         "gross_profit_share": gross_profit_share,
                         "assets_share_pre_elimination": assets_share},
            "evidence": {"scope": "company_pig_industry",
                         "document_hash": "doc-%s" % period,
                         "source_page": source_page},
        },
        "revenue": {"value": revenue, "scope": "segment_product_row",
                    "product_label": product_label,
                    "document_hash": "doc-%s" % period, "source_page": 61},
        "cogs": {"value": cogs},
    }


def _bulletin(price=15.75, heads=7166.0, period_end="2025-12-31"):
    return {"status": "extracted",
            "annual_commodity_price": {"value": price, "period_end": period_end,
                                       "scope": "official_bulletin",
                                       "document_hash": "b-1", "source_page": 2,
                                       "basis": "商品猪销售均价（元/公斤）"},
            "annual_sales_heads": {"value": heads, "period_end": period_end,
                                   "scope": "official_bulletin",
                                   "document_hash": "b-1", "source_page": 2}}


def _state(**kwargs):
    # ``store=False``：**不读本地观测仓**。本文件的断言全部是对「给定输入 →
    # 给定读数」的断言，输入必须是这里造的那几条报告；真库里的观测（本机确实
    # 落了四家的月报与行业序列）会让同一个断言在不同机器上给出不同答案。
    # 观测仓那一路的测试在 ``tests/test_pig_evidence.py``，它们自带临时库。
    return PIG.state("002714", reports=[_report(**kwargs)], store=False)


class TestMetricVocabulary(unittest.TestCase):

    def test_the_vocabulary_is_the_declared_ids_and_nothing_else(self):
        """格数 = 批 5 的用户清单 22 格 + 批 5.1 按 spec 拆出的 9 格 + 批 7 的 1 格。

        这条断言防的从来不是数字本身，而是**「多一格就是新造指标」**。所以
        加格子的同时必须说清每一格拆自哪里、混用会给出什么错的答案
        （写在 ``BATCH_5_1_METRIC_IDS`` 与 ``BATCH_7_METRIC_IDS`` 上面的
        分块注释里）；只要答不上来，就不该加。数量与清单一起钉住，改动必须
        同时改这里。

        批 7 那一格（断奶仔猪成本）加的理由与前面几批不同：它**不是**从一个
        已有的格里拆出来的，而是本地语料里真有一条披露才建的（裁定 2：
        「只建真有数据的格子」）。它的量纲是**元/头**，与所有 ``CNY/kg`` 的
        成本格不同源——拿它和完全成本做加减是量纲错误，所以它必须独立成格。
        """
        self.assertEqual(len(PIG.BATCH_5_1_METRIC_IDS), 9)
        self.assertEqual(len(PIG.BATCH_7_METRIC_IDS), 1)
        self.assertEqual(len(PIG.METRIC_IDS), 22 + 9 + 1)
        self.assertIn(PIG.M_WEANED_PIGLET_COST, PIG.METRIC_IDS)
        self.assertNotIn(PIG.M_WEANED_PIGLET_COST, PIG.BATCH_5_1_METRIC_IDS,
                         "批 7 的格子不许混进批 5.1 的清单")
        self.assertEqual(len(set(PIG.METRIC_IDS)),
                         len(PIG.METRIC_IDS), "metric_id 不许重名")
        self.assertEqual(sorted(PIG.METRIC_INDEX), sorted(PIG.METRIC_IDS))
        # 9 格都是新拆出来的口径，**不许**与批 5 的 22 格重名。
        self.assertEqual(set(PIG.METRIC_IDS[:22]) & set(PIG.BATCH_5_1_METRIC_IDS),
                         set())
        for definition in PIG.METRICS:
            self.assertTrue(definition.variants, definition.metric_id)
            self.assertEqual(definition.primary_variant,
                             definition.variant_names[0],
                             "声明顺序即正身：第一个 variant 就是「正身口径」")

    def test_the_contract_self_check_is_clean(self):
        """22 格一对齐 / 桥接名逐字相同 / 缺口理由全覆盖——一次查完。"""
        self.assertEqual(PIG.contract_errors(), [])

    def test_every_factor_the_adapter_feeds_is_a_real_factor(self):
        for factor_id in sorted(PIG.FACTOR_METRICS):
            self.assertIn(factor_id, F.FACTOR_INDEX,
                          "%s 不是 canonical factor" % factor_id)

    def test_the_opportunity_factors_come_from_the_adapter(self):
        """6 个组成因子必须各自**在适配器里**声明依赖（除现金生存力外）。

        ``financial_survivability`` 是用户清单里的名字，但它的落点是一个
        **零权重**的空依赖 factor——不新建重复 id，是「一个经济因素只声明一次」
        这条纪律的直接后果。

        **批 9 起只剩 6 个**：``regional_premium`` 当初的落点是已有的
        ``price_premium``，用户裁定不要区域溢价之后那一格因子整个摘掉了
        （见 ``research.dimensions.GROUP_FACTOR_WEIGHTS`` 同处的说明），
        所以这里也少一个——两处不同批改就会留下一条永远查不出的空声明。
        """
        want = {"margin_position", "sale_price_level", "supply_contraction",
                "cost_advantage", "capacity_delivery", "financial_survivability"}
        table = D.GROUP_FACTOR_WEIGHTS[F.GROUP_PIG_INDUSTRY]
        self.assertTrue(want <= set(table), sorted(want - set(table)))
        for factor_id in sorted(want):
            self.assertIn(factor_id, PIG.FACTOR_METRICS,
                          "%s 没有依赖声明（含空声明）" % factor_id)
        # 空依赖是**显式**的，不是漏写的：它的读数仍然存在，理由是写在明处的。
        self.assertEqual(PIG.FACTOR_METRICS["financial_survivability"], ())
        self.assertEqual(table["financial_survivability"], 0.00)


class TestRecordSchema(unittest.TestCase):

    def test_the_columns_are_the_users_sixteen_plus_three(self):
        """列顺序逐字照用户清单；多出来的三列各自有存在理由。

        ``lower_bound`` / ``upper_bound`` 单列而不塞进 ``value``——把区间塞进
        ``value`` 正是伪精确；``note`` 是「为什么是这个状态」，MISSING 没有理由
        就等于没人知道缺什么。
        """
        user_sixteen = ("metric_id", "metric_variant", "company_code", "period",
                        "value", "unit", "scope", "source_type", "source_name",
                        "document", "source_text", "page", "confidence",
                        "is_estimated", "is_direct_disclosure", "status")
        self.assertEqual(PIG.RECORD_COLUMNS[:16], user_sixteen)
        self.assertEqual(PIG.RECORD_COLUMNS[16:],
                         ("lower_bound", "upper_bound", "note"))
        self.assertEqual(len(PIG.RECORD_COLUMNS), 19)
        self.assertEqual(tuple(PIG.PigMetricRecord.__slots__),
                         PIG.RECORD_COLUMNS)

    def test_a_record_serializes_with_exactly_those_columns(self):
        state = _state()
        self.assertTrue(state["metrics"])
        for row in state["metrics"]:
            self.assertEqual(tuple(row), PIG.RECORD_COLUMNS)
            self.assertEqual(row["company_code"], "002714")

    def test_an_unknown_status_is_refused_at_construction(self):
        """状态词表是**闭的**：写错一个状态名不许静默降级成 MISSING。"""
        with self.assertRaises(ValueError):
            PIG.PigMetricRecord(PIG.M_FULL_COST, status="MAYBE")


class TestVariantIsolation(unittest.TestCase):

    def test_the_three_costs_are_three_metrics_never_one(self):
        ids = (PIG.M_FULL_COST, PIG.M_CASH_COST, PIG.M_FATTENING_COST)
        self.assertEqual(len(set(ids)), 3)
        for metric_id in ids:
            self.assertIn(metric_id, PIG.METRIC_INDEX)
        # 三者可以差一倍以上，所以「成本优势」这四个字必须指向其中一个口径。
        self.assertEqual(PIG.FACTOR_METRICS["full_cost"], (PIG.M_FULL_COST,))
        self.assertNotIn(PIG.M_CASH_COST,
                         [m for ids_ in PIG.FACTOR_METRICS.values()
                          for m in ids_],
                         "现金成本本批没有消费方——它不该被谁顺手拿去用")

    def test_a_gross_margin_ratio_never_becomes_a_per_kg_margin(self):
        """分部毛利率（%）不是每公斤毛利（元/公斤）。量纲错掉的读数看不出来。

        批 5.1 把两者分成了**两个 metric id**（§一：unit_margin 只允许
        ``pig_sale_price − full_cost``，分部毛利率拆到
        ``pig_segment_gross_margin``）。所以这条测试的写法变了，判据反而更硬：
        以前是「同一个 metric 下两个 variant 别混」，现在是「它们根本不在同一格」。
        """
        state = _state()
        rows = state["metric_index"][PIG.M_PIG_SEGMENT_GROSS_MARGIN]
        ratio_rows = [r for r in rows
                      if r["metric_variant"] == "segment_gross_margin_ratio"]
        self.assertTrue(ratio_rows, "夹具里应当有一条推算出来的分部毛利率")
        self.assertEqual(ratio_rows[0]["unit"], "%")
        self.assertTrue(ratio_rows[0]["is_estimated"])
        # 它**不再**出现在单位毛利的记录表里——量纲污染的机器可判定形式。
        self.assertEqual([r for r in state["metric_index"][PIG.M_UNIT_MARGIN]
                          if r["metric_variant"] == "segment_gross_margin_ratio"],
                         [])
        self.assertEqual(PIG.METRIC_INDEX[PIG.M_UNIT_MARGIN].variant_names,
                         ("cny_per_kg",), "单位毛利只剩元/公斤这一个口径")
        # 正身 cny_per_kg 没有值 → 台账里它是缺口，理由必须写明「缺的是正身」。
        self.assertIn(PIG.M_UNIT_MARGIN, state["gaps"])
        self.assertIn("正身", state["gaps"][PIG.M_UNIT_MARGIN])
        # 有解析器、但**解析出来的只是旁证口径**的指标仍然要写缺口理由——
        # 所以它不在 RESOLVER_METRICS 里，却在 GAP_REASONS 里。
        self.assertNotIn(PIG.M_UNIT_MARGIN, PIG.RESOLVER_METRICS)
        self.assertIn(PIG.M_UNIT_MARGIN, PIG.GAP_REASONS)

    def test_the_primary_variant_decides_the_gap_not_the_record_count(self):
        """有记录 != 有值：判据是 (metric_id, variant) 成对。

        夹具里 ``pig_profit_exposure`` 有一条**毛利占比**的真记录，但它的正身是
        净利润占比，所以这一格照旧是缺口——而且理由要写明「缺的是正身」，
        不能因为「这一格有记录」就把它从缺口表里划掉。
        """
        state = _state()
        self.assertTrue(state["gaps"])
        rows = state["metric_index"][PIG.M_PIG_PROFIT_EXPOSURE]
        self.assertTrue([r for r in rows
                         if r["metric_variant"] == "segment_gross_profit_share"
                         and r["value"] is not None])
        self.assertIn(PIG.M_PIG_PROFIT_EXPOSURE, state["gaps"])
        self.assertIn("正身", state["gaps"][PIG.M_PIG_PROFIT_EXPOSURE])


class TestDisclosureHonesty(unittest.TestCase):

    def test_a_derived_value_is_always_marked_estimated(self):
        """用户裁定：推算必须 ``is_estimated = True``——构造方**无权**把它标成假。"""
        record = PIG.PigMetricRecord(
            PIG.M_PIG_SEGMENT_GROSS_MARGIN,
            metric_variant="segment_gross_margin_ratio",
            value=16.6, unit="%", source_type=PIG.SRC_DERIVED, status=PIG.STATUS_OK,
            is_estimated=False)
        self.assertTrue(record.is_estimated)
        self.assertFalse(record.is_direct_disclosure)
        self.assertEqual(PIG.ESTIMATED_SOURCE_TYPES, frozenset({PIG.SRC_DERIVED}))

    def test_a_range_disclosure_keeps_bounds_and_never_becomes_a_value(self):
        """模糊披露 → 区间，不折成一个精确数字。"""
        state = _state()
        rows = [r for r in state["metric_index"][PIG.M_PIG_ASSET_EXPOSURE]
                if r["metric_variant"] == "pre_elimination_upper_bound"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], PIG.STATUS_RANGE)
        self.assertIsNone(row["value"])
        self.assertEqual(row["upper_bound"], 0.22)
        obj = PIG.PigCompanyState("002714", [PIG.PigMetricRecord(
            PIG.M_PIG_ASSET_EXPOSURE,
            metric_variant="pre_elimination_upper_bound", value=None,
            upper_bound=0.22, unit="ratio", status=PIG.STATUS_RANGE)], {})
        record = obj.records[0]
        self.assertFalse(record.scorable, "只有上界不许被当成值消费")
        self.assertTrue(record.has_value)
        self.assertIsNone(obj.best(PIG.M_PIG_ASSET_EXPOSURE,
                                   "pre_elimination_upper_bound"))
        # 但它仍然算「正身口径有值」——所以它不是缺口，只是不可计分。
        self.assertTrue(obj.has_primary(PIG.M_PIG_ASSET_EXPOSURE))

    def test_every_source_type_has_a_declared_rank(self):
        """缺来源类型按**最差**算，不是按最好。

        批 8 加了**人工确认级**（``SRC_MANUAL``），位置是刻意的：排在 L2 月报
        **之后**——定期报告与月报的自动值仍然优先，人工补录的值保留在冲突清单里
        作旁证。``[-1]`` 仍是 ``SRC_DERIVED``（推算永远垫底）。
        """
        self.assertEqual(len(PIG.SOURCE_PRIORITY), 8)
        self.assertEqual(PIG.SOURCE_PRIORITY[0], PIG.SRC_ANNUAL_REPORT)
        self.assertEqual(PIG.SOURCE_PRIORITY[-1], PIG.SRC_DERIVED)
        self.assertLess(PIG.SOURCE_RANK[PIG.SRC_MONTHLY_BULLETIN],
                        PIG.SOURCE_RANK[PIG.SRC_MANUAL],
                        "人工补录不许排在定期报告/月报之前")
        unknown = PIG.PigMetricRecord(PIG.M_PSY, status=PIG.STATUS_MISSING,
                                      source_type="??")
        self.assertEqual(unknown.source_rank, PIG.UNKNOWN_SOURCE_RANK)
        self.assertEqual(PIG.UNKNOWN_SOURCE_RANK, len(PIG.SOURCE_PRIORITY))
        for name, rank in PIG.SOURCE_RANK.items():
            self.assertEqual(rank, PIG.SOURCE_PRIORITY.index(name))


class TestSourcePriority(unittest.TestCase):

    def test_the_higher_priority_source_wins_and_the_other_stays_as_evidence(self):
        """高优先级来源有值时，低优先级只作旁证——不取平均、不互相顶替。"""
        annual = PIG.PigMetricRecord(
            PIG.M_PIG_SALE_PRICE, metric_variant="annual_commodity_price",
            company_code="002714", period="2025A", value=14.20, unit="CNY/kg",
            source_type=PIG.SRC_ANNUAL_REPORT, source_name="定期报告",
            status=PIG.STATUS_OK)
        monthly = PIG.PigMetricRecord(
            PIG.M_PIG_SALE_PRICE, metric_variant="annual_commodity_price",
            company_code="002714", period="2025A", value=15.75, unit="CNY/kg",
            source_type=PIG.SRC_MONTHLY_BULLETIN, source_name="经营简报",
            status=PIG.STATUS_OK)
        obj = PIG.PigCompanyState("002714", [monthly, annual], {})
        self.assertEqual(obj.best(PIG.M_PIG_SALE_PRICE,
                                  "annual_commodity_price").value, 14.20)
        self.assertEqual([r.value for r in obj.records_of(
            PIG.M_PIG_SALE_PRICE, "annual_commodity_price")][0], 14.20)
        # 两条都在 evidence 里，谁也没被丢掉。
        self.assertEqual(len(obj.evidence), 2)

    def test_a_bulletin_lands_as_the_second_priority_source(self):
        """简报是第②优先级：它比定期报告低、比推算高，且**未经审计**。"""
        state = PIG.state("002714", reports=[_report()], bulletins=[_bulletin()],
                          store=False)
        rows = [r for r in state["metric_index"][PIG.M_PIG_SALE_PRICE]
                if r["value"] is not None]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_type"], PIG.SRC_MONTHLY_BULLETIN)
        self.assertEqual(row["value"], 15.75)
        self.assertTrue(row["is_direct_disclosure"])
        self.assertFalse(row["is_estimated"], "披露值不是推算值")
        self.assertNotIn(PIG.M_PIG_SALE_PRICE,
                         [g for g in state["gaps"] if g == PIG.M_PIG_SALE_PRICE])
        self.assertTrue(state["used_bulletins"])
        # 出栏量的单位换算是万头（简报原文是头）。
        heads = [r for r in state["metric_index"][PIG.M_HOG_SALES_VOLUME]
                 if r["value"] is not None][0]
        self.assertEqual(heads["unit"], "万头")
        self.assertEqual(heads["value"], 0.7166)


class TestReadings(unittest.TestCase):

    def test_a_factor_with_a_missing_dependency_stays_missing(self):
        """「宁可 missing，不要伪精确」在**依赖**层面的落地：缺一格就整条缺。"""
        state = _state()
        reading = state["readings"]["sale_price_level"]
        self.assertIsNone(reading["value"])
        self.assertEqual(sorted(reading["missing_metrics"]),
                         sorted([PIG.M_PIG_SALE_PRICE, PIG.M_NATIONAL_PIG_PRICE]))
        self.assertIn("不使用部分输入凑一个数", reading["note"])
        margin = state["readings"]["margin_position"]
        self.assertIsNone(margin["value"])
        self.assertEqual(margin["missing_metrics"], [PIG.M_UNIT_MARGIN])

    def test_a_factor_with_every_dependency_gets_a_value_and_its_provenance(self):
        state = PIG.state("002714", reports=[_report()], bulletins=[_bulletin()],
                          store=False)
        reading = state["readings"]["cost_advantage"]
        self.assertIsNone(reading["value"], "成本优势要同组截面，本批没有")
        price = state["readings"]["company_sale_price"]
        self.assertEqual(price["value"], 15.75)
        self.assertEqual(price["metric_id"], PIG.M_PIG_SALE_PRICE)
        self.assertEqual(price["metric_variant"], "annual_commodity_price")
        self.assertEqual(price["unit"], "CNY/kg")
        self.assertEqual(price["source_type"], PIG.SRC_MONTHLY_BULLETIN)
        self.assertTrue(price["source"], "一条读数必须指得出一个可核对的来源")
        self.assertEqual(len(price["inputs"]), 1)
        # 两个依赖的 factor 仍然缺国民价（位置要「公司价 vs 全国价」两把尺子）。
        position = state["readings"]["sale_price_level"]
        self.assertIsNone(position["value"])
        self.assertEqual(position["missing_metrics"], [PIG.M_NATIONAL_PIG_PRICE])

    def test_the_composite_exposure_is_the_only_exposure_source(self):
        """本层不重算暴露：``pig_exposure_composite`` 记录就是它的输出。"""
        state = _state()
        self.assertEqual(state["exposure"], 0.2669)
        row = [r for r in state["metric_index"][PIG.M_PIG_EXPOSURE_COMPOSITE]
               if r["value"] is not None][0]
        self.assertEqual(row["value"], state["exposure"])
        self.assertEqual(row["metric_variant"], "source_weighted_composite")
        self.assertEqual(state["classification"],
                         rules.RULES_V1["pig"]["default_classification"])

    def test_a_no_dependency_factor_still_has_a_reading_with_a_reason(self):
        """空着不等于没话说：``financial_survivability`` 的理由写在明处。"""
        state = _state()
        reading = state["readings"]["financial_survivability"]
        self.assertIsNone(reading["value"])
        self.assertEqual(reading["missing_metrics"], [])
        self.assertTrue(reading["note"])


class TestGapsAndRobustness(unittest.TestCase):

    def test_every_gap_says_what_is_missing(self):
        state = _state()
        gaps = state["gaps"]
        self.assertTrue(gaps)
        for metric_id, note in sorted(gaps.items()):
            self.assertIn(metric_id, PIG.METRIC_IDS)
            self.assertTrue(note and note.strip(), metric_id)
            self.assertGreater(len(note), 8, "%s 的理由太短，等于没说" % metric_id)

    def test_the_record_table_covers_all_22_cells(self):
        """22 格每一格都有记录（哪怕是 MISSING）——「没有记录」与「是 missing」
        在界面上必须分得开。"""
        for reports in (None, [], [_report()], [_report(status="not_found")]):
            state = PIG.state("002714", reports=reports, store=False)
            seen = {r["metric_id"] for r in state["metrics"]}
            self.assertEqual(seen, set(PIG.METRIC_IDS), reports)

    def test_never_raises_and_always_says_why(self):
        """唯一入口永不抛异常：适配器崩了也要给全 22 格 + 失败原因。"""
        for reports in (None, [], [None], ["x"], [{}], [{"financial_note": None}]):
            state = PIG.state("002714", reports=reports, store=False)
            self.assertEqual({r["metric_id"] for r in state["metrics"]},
                             set(PIG.METRIC_IDS), reports)
            self.assertTrue(state["reason"], reports)
            self.assertEqual(state["adapter"], "research.industry.pig")
            self.assertEqual(state["code"], "002714")

    def test_a_broken_adapter_is_not_the_same_as_no_data(self):
        """「适配器坏了」与「这家公司没有猪业务数据」是两件事。"""
        broken = PIG.state("002714", reports=[_report()], store=False,
                           bulletins=[{"status": "extracted",
                                       "annual_commodity_price": {"value": "x"}}])
        self.assertEqual({r["metric_id"] for r in broken["metrics"]},
                         set(PIG.METRIC_IDS))
        self.assertIn(broken["classification"],
                      rules.RULES_V1["pig"]["gate_multipliers"])

    def test_describe_counts_the_gaps_out_loud(self):
        """格数从 ``len(METRIC_IDS)`` 来，**不写死**——写死的那个 22 在批 5.1
        加了 9 格之后就会开始说谎。"""
        state = _state()
        text = PIG.describe(state)
        self.assertIn("%d 格指标里" % len(PIG.METRIC_IDS), text)
        self.assertIn(str(len(state["gaps"])), text)


class TestFactorLayerHandoff(unittest.TestCase):

    def test_the_factor_layer_reads_the_adapter_readings_verbatim(self):
        """``factors._pig_result`` 读的就是 ``pig["readings"]``：值、口径、
        溯源全部来自记录，**不在这里再加工一遍**。"""
        state = _state()
        item = _evaluate_with(state)["pig_exposure"]
        self.assertEqual(item["raw"]["classification"], state["classification"])
        self.assertEqual(item["raw"]["exposure"], state["exposure"])
        self.assertEqual(item["raw"]["source"], state["exposure_source"])
        component = item["components"][0]
        self.assertEqual(component["exposure"], state["exposure"])
        self.assertEqual(component["source_breakdown"], state["source_breakdown"],
                         "逐来源的 entered/excluded/absent 原样透传，不重排不裁剪")
        self.assertEqual(component["detail"], state["detail"])

    def test_a_factor_with_a_reading_but_no_curve_stays_display_only(self):
        """本批 ``factor_curves`` 是空的：有读数也**不进任何分母**。

        这不是「暂时忽略」——曲线要有观测支撑，没有就先不折成分；补表即开始
        计分，不需要再改代码。
        """
        self.assertEqual(rules.RULES_V1["pig"]["factor_curves"], {})
        # 暴露必须过 ``min_exposure_for_specialized``，否则整组是 not_applicable、
        # 根本走不到「有读数但没曲线」那一条分支。
        state = PIG.state("002714", reports=[_report(revenue_share=0.90)],
                          bulletins=[_bulletin()], store=False)
        self.assertEqual(state["classification"],
                         rules.RULES_V1["pig"]["classification_bands"][0][0])
        item = _evaluate_with(state)["company_sale_price"]
        self.assertEqual(item["status"], F.STATUS_DISPLAY_ONLY)
        self.assertIsNone(item["score"])
        self.assertEqual(item["raw"]["value"], 15.75)
        self.assertIn("不进任何分母", item["reason"])
        # 只展示 = **声明权重都不出现**（不是「进了分母但没有分」）。
        self.assertFalse(item["contributes_to_score"])


def _evaluate_with(pig_state):
    """跑一次真实的 factor 评估，把适配器状态当 ``context["pig"]`` 喂进去。"""
    return F.to_payload(F.evaluate(*_offline_scored(), context={"pig": pig_state}))


def _offline_scored():
    """一份不联网的 ``(m, scored, route, final)``，给 ``F.evaluate`` 用。"""
    from research import engine, router
    from tests.test_research import _SYNTH_QUOTE, _synth_fin
    m, _q, _f = engine.build_metrics("002714", _SYNTH_QUOTE, _synth_fin("养殖业"))
    scored = rules.score_modules(m)
    route = router.route(m, scored["attributes"])
    final = rules.final_score(scored["attributes"], scored["cyclical_position"],
                              m, rules.determine_type(scored["attributes"], "养殖业"),
                              route)
    return m, scored, route, final


if __name__ == "__main__":
    unittest.main()
