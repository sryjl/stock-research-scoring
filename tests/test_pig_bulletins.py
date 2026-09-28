# -*- coding: utf-8 -*-
"""月度经营简报：解析、口径、观测（批 5.1 §七 / §八 / §九 / §十一 / §四十）。

这一组测试锁的都是**实测出来的事实与用户裁定**，不是假想的边界：

* 四家表头口径不同（商品猪 vs 生猪合计）→ 均重只在同口径表上推；
* 合并月份（牧原 1-2 月并一行）不能被当成单月；
* 「关于」前缀（天康 13 份里 11 份）不能静默丢；
* 累计勾稽里「这一年第一行」那条分支曾是死代码；
* 内部销售写的是「商品猪」（牧原 339.0 万头）不是「生猪」。

全部是纯函数测试：不联网、不写库。
"""
import json
import unittest

from research import pig_bulletins as pb
from research import pig_observations as obs
from research.industry import pig as _pig


CODE = "002714"
HASH = "b" * 64
URL = "https://static.cninfo.com.cn/finalpage/2026-09-08/1225551030.PDF"

#: 商品猪口径表头（牧原 / 新希望）。**价格列必须写「商品猪价格」**——「商品猪销售
#: 均价」不行，`price_scope` 认的正是那四个字。
HEAD_COMMODITY = ("商品猪销量（万头）", "商品猪销售收入（万元）",
                  "商品猪价格（元/公斤）")
#: 生猪合计口径表头 + 商品猪价格（东瑞 / 天康）。这正是「口径不足」的来源：
#: 收入与销量含仔猪种猪，价格只是商品猪的。
HEAD_ALL = ("生猪销量（万头）", "生猪销售收入（万元）",
            "商品猪价格（元/公斤）")


def row(page, *texts):
    return {"page": page, "y": 0.0,
            "cells": [{"x": i * 100.0, "text": t} for i, t in enumerate(texts)]}


def line(page, period, heads, cum_heads, revenue, cum_revenue, price):
    """一行表体。**5 个数字列都必须带小数点**（``_NUMBER`` 的判据）。"""
    return row(page, period, "%.2f" % heads, "%.2f" % cum_heads,
               "%.2f" % revenue, "%.2f" % cum_revenue, "%.2f" % price)


def body_06_08():
    """6/7/8 三个月，累计逐月勾稽得上（残差 0）。

    8 月：120 万头 / 12 亿元 / 10 元每公斤 → 均重 100 kg/头。
    """
    return [line(1, "2026年6月", 100, 100, 10, 10, 10),
            line(1, "2026年7月", 110, 210, 11, 21, 10),
            line(1, "2026年8月", 120, 330, 12, 33, 10)]


def bulletin(*, title="2026年8月份销售简报", header=HEAD_COMMODITY, body=None,
             extra=(), code=CODE):
    rows = [row(1, "证券代码：%s" % code), row(1, title), row(1, *header)]
    rows.extend(body_06_08() if body is None else body)
    rows.extend(extra)
    return rows


def parse(rows, **kw):
    kw.setdefault("stock_code", CODE)
    kw.setdefault("source_url", URL)
    return pb.extract_monthly_bulletin(rows, document_hash=HASH, **kw)


def by_metric(parsed, metric_id):
    return [o for o in pb.observations_of(parsed) if o.metric_id == metric_id]


class TestTableScope(unittest.TestCase):
    """§四十一：四家表头口径差异，是本模块存在的全部理由。"""

    def test_commodity_table_is_read_as_commodity_and_weight_is_derived(self):
        parsed = parse(bulletin())
        self.assertEqual(parsed["status"], "extracted")
        self.assertEqual(parsed["table_scope"], pb.SCOPE_COMMODITY)
        self.assertEqual(parsed["price_scope"], pb.SCOPE_COMMODITY)
        self.assertEqual(parsed["period"], "2026-08")
        self.assertEqual(parsed["current"]["heads_10k"], 120.0)
        weights = by_metric(parsed, _pig.M_AVERAGE_SALE_WEIGHT)
        self.assertEqual([o.status for o in weights], ["OK"] * 3)
        self.assertEqual(weights[-1].value, 100.0)
        self.assertTrue(weights[-1].is_estimated)
        self.assertEqual(weights[-1].derivation,
                         "revenue_over_heads_times_price")

    def test_all_hogs_table_never_fabricates_a_weight(self):
        """东瑞 / 天康走这条：三项不同口径，算出来 118.31 / 105.55 那种假数。"""
        parsed = parse(bulletin(header=HEAD_ALL, title="2026年8月份生猪销售简报"))
        self.assertEqual(parsed["table_scope"], pb.SCOPE_ALL)
        weights = by_metric(parsed, _pig.M_AVERAGE_SALE_WEIGHT)
        self.assertEqual([o.status for o in weights],
                         [obs.STATUS_INSUFFICIENT_SCOPE])
        self.assertIsNone(weights[0].value)
        # 商品猪销量在这份文件里取不到 → 口径不足，**不是 MISSING**。
        commodity = by_metric(parsed, _pig.M_COMMODITY_HOG_SALES_VOLUME)
        self.assertEqual([o.status for o in commodity],
                         [obs.STATUS_INSUFFICIENT_SCOPE])
        self.assertIsNone(commodity[0].value)

    def test_all_hogs_table_still_yields_total_volume_per_row(self):
        parsed = parse(bulletin(header=HEAD_ALL, title="2026年8月份生猪销售简报"))
        total = by_metric(parsed, _pig.M_HOG_SALES_VOLUME)
        self.assertEqual([o.value for o in total], [100.0, 110.0, 120.0])
        self.assertEqual({o.status for o in total}, {"OK"})
        self.assertEqual({o.scope for o in total}, {pb.SCOPE_ALL})

    def test_insufficient_scope_is_not_missing(self):
        """§十：界面必须把「取到了但不对口径」与「没取到」分开。"""
        parsed = parse(bulletin(header=HEAD_ALL, title="2026年8月份生猪销售简报"))
        for record in pb.observations_of(parsed):
            self.assertNotEqual(record.status, obs.STATUS_MISSING)

    def test_both_headers_at_once_is_rejected_not_guessed(self):
        rows = bulletin()
        rows.append(row(1, "生猪销量（万头）"))
        parsed = parse(rows)
        self.assertEqual(parsed["status"], "scope_ambiguous")

    def test_no_known_header_at_all_is_rejected(self):
        parsed = parse(bulletin(header=("数量（万头）", "收入（万元）", "价格（元）")))
        self.assertEqual(parsed["status"], "scope_ambiguous")


class TestMergedMonths(unittest.TestCase):
    """牧原把 1 月与 2 月并成一行，且从未单独披露这两个月。"""

    def test_merged_row_gets_a_range_period_label(self):
        body = [line(1, "2025年1-2月", 1146.1, 1146.1, 100, 100, 10)]
        parsed = parse(bulletin(title="2025年1-2月份销售简报", body=body))
        self.assertEqual(parsed["status"], "extracted")
        self.assertEqual(parsed["period"], "2025-01~02")
        self.assertTrue(parsed["current"]["is_range"])
        self.assertEqual(parsed["current"]["end_month"], 2)

    def test_merged_row_is_flagged_in_every_derived_observation(self):
        """载荷不在手边时，1,146.1 万头看起来就是一个正常的月度数。"""
        body = [line(1, "2025年1-2月", 1146.1, 1146.1, 100, 100, 10)]
        parsed = parse(bulletin(title="2025年1-2月份销售简报", body=body))
        derived = [o for o in pb.observations_of(parsed)
                   if o.status == obs.STATUS_OK]
        # 销量 / 价格 / 均重三条都由这一行派生。
        self.assertEqual({o.metric_id for o in derived},
                         {_pig.M_COMMODITY_HOG_SALES_VOLUME,
                          _pig.M_PIG_SALE_PRICE, _pig.M_AVERAGE_SALE_WEIGHT})
        for record in derived:
            self.assertIn("合并披露", record.reason)
            self.assertIn("2025-01~02", record.reason)

    def test_single_month_row_carries_no_merged_note(self):
        self.assertEqual(pb._merged_note({"is_range": False}), "")

    def test_range_label_is_not_the_end_month(self):
        """记成 2025-02 会让下游把它当 2 月单月——差一倍且看起来正常。"""
        self.assertEqual(pb._period_label(2025, 1, 2), "2025-01~02")
        self.assertEqual(pb._period_label(2026, 8, 8), "2026-08")
        self.assertNotEqual(pb._period_label(2025, 1, 2), "2025-02")


class TestTitleAndIdentity(unittest.TestCase):
    def test_title_with_guanyu_prefix_is_accepted(self):
        """天康 13 份最近的文件里有 11 份是这个形状。"""
        body = body_06_08()
        parsed = parse(bulletin(title="关于2026年8月份生猪销售简报",
                                header=HEAD_ALL, body=body))
        self.assertEqual(parsed["status"], "extracted")
        self.assertEqual(parsed["period"], "2026-08")

    def test_title_split_across_two_rows_is_accepted(self):
        """天康那份的标题在 PDF 里真的被拆成两行。"""
        rows = bulletin()
        rows[1] = row(1, "2026年8", "月份销售简报")
        self.assertEqual(parse(rows)["status"], "extracted")

    def test_body_subheading_does_not_pass_as_title(self):
        """正文小标题「一、2026年8月份销售情况简报」不是标题——整行匹配才是判据。"""
        rows = bulletin(title="销售简报")
        rows.insert(2, row(1, "一、2026年8月份销售情况简报"))
        self.assertEqual(parse(rows)["status"], "title_not_found")

    def test_two_candidate_titles_are_rejected(self):
        rows = bulletin()
        rows.insert(2, row(1, "2026年7月份销售简报"))
        parsed = parse(rows)
        self.assertEqual(parsed["status"], "ambiguous_title")
        self.assertEqual(len(parsed["titles"]), 2)

    def test_wrong_stock_code_is_rejected(self):
        parsed = parse(bulletin(code="000001"))
        self.assertEqual(parsed["status"], "document_identity_mismatch")

    def test_non_official_url_is_rejected(self):
        for url in ("http://static.cninfo.com.cn/finalpage/2026-09-08/x.PDF",
                    "https://example.com/finalpage/2026-09-08/x.PDF",
                    "https://static.cninfo.com.cn/other/2026-09-08/x.PDF"):
            self.assertEqual(parse(bulletin(), source_url=url)["status"],
                             "untrusted_source", url)

    def test_empty_input_is_rejected(self):
        self.assertEqual(parse([])["status"], "invalid_input")
        self.assertEqual(
            pb.extract_monthly_bulletin(bulletin(), stock_code=CODE,
                                        source_url=URL, document_hash="")["status"],
            "invalid_input")


class TestRowValidation(unittest.TestCase):
    def test_cumulative_mismatch_is_rejected(self):
        body = body_06_08()
        body[-1] = line(1, "2026年8月", 120, 999, 12, 33, 10)
        parsed = parse(bulletin(body=body))
        self.assertEqual(parsed["status"], "cumulative_mismatch")
        self.assertGreater(parsed["residual"], 100)

    def test_first_row_of_a_year_must_equal_its_own_cumulative(self):
        """这条分支**曾经是死代码**：外层守卫写「同年且月号连续才继续」，
        而跨年的月号必然不连续，于是每一份文件里那个一月的累计从来没被核过。"""
        body = [line(1, "2025年12月", 90, 1000, 9, 100, 10),
                line(1, "2026年1月", 95, 777, 9.5, 9.5, 10)]
        parsed = parse(bulletin(title="2026年1月份销售简报", body=body))
        self.assertEqual(parsed["status"], "cumulative_mismatch")
        self.assertGreater(parsed["residual"], 100)

    def test_first_row_of_a_year_is_accepted_when_it_agrees(self):
        body = [line(1, "2025年12月", 90, 1000, 9, 100, 10),
                line(1, "2026年1月", 95, 95, 9.5, 9.5, 10)]
        parsed = parse(bulletin(title="2026年1月份销售简报", body=body))
        self.assertEqual(parsed["status"], "extracted")
        self.assertEqual([r["period"] for r in parsed["rows"]],
                         ["2025-12", "2026-01"])

    def test_month_gap_is_rejected_by_range_not_by_number(self):
        body = [line(1, "2026年6月", 100, 100, 10, 10, 10),
                line(1, "2026年8月", 120, 220, 12, 22, 10)]
        parsed = parse(bulletin(body=body))
        self.assertEqual(parsed["status"], "month_gap")
        self.assertEqual(parsed["after"], "2026-06")

    def test_merged_row_makes_continuity_check_by_range(self):
        """合并行覆盖到 2 月，下一行从 3 月开始就算连续，不算缺一个月。"""
        body = [line(1, "2026年1-2月", 200, 200, 20, 20, 10),
                line(1, "2026年3月", 110, 310, 11, 31, 10)]
        parsed = parse(bulletin(title="2026年3月份销售简报", body=body))
        self.assertEqual(parsed["status"], "extracted")

    def test_newest_row_must_match_the_title_period(self):
        body = [line(1, "2026年6月", 100, 100, 10, 10, 10),
                line(1, "2026年7月", 110, 210, 11, 21, 10)]
        parsed = parse(bulletin(body=body))
        self.assertEqual(parsed["status"], "period_mismatch")
        self.assertEqual(parsed["newest"], "2026-07")

    def test_same_row_printed_twice_with_equal_values_is_kept(self):
        body = body_06_08()
        body.append(line(1, "2026年8月", 120, 330, 12, 33, 10))
        parsed = parse(bulletin(body=body))
        self.assertEqual(parsed["status"], "extracted")
        self.assertEqual([r["period"] for r in parsed["rows"]],
                         ["2026-06", "2026-07", "2026-08"])

    def test_same_month_twice_with_different_values_rejects_the_whole_file(self):
        """东瑞 2026-06 的实际形状：它就是公司自己把年份写错了。"""
        body = body_06_08()
        body.append(line(1, "2026年8月", 130, 460, 13, 46, 10))
        parsed = parse(bulletin(body=body))
        self.assertEqual(parsed["status"], "duplicate_month")
        self.assertIn("2026-08", parsed["period"])
        self.assertIn("2026年8月", parsed["first_row"])
        self.assertIn("2026年8月", parsed["second_row"])
        # 累计勾稽得上 → 更像「上一行的下一个月（年份写错）」，但要人去核对。
        self.assertIn("年份写错", parsed["suspected"])
        self.assertIn("不替它改年份", parsed["suspected"])

    def test_no_table_rows_at_all(self):
        self.assertEqual(parse(bulletin(body=[]))["status"], "no_table")


class TestNarrative(unittest.TestCase):
    def _with(self, *texts):
        return bulletin(extra=[row(1, t) for t in texts])

    def test_narrative_matching_the_table_is_accepted(self):
        parsed = parse(self._with("2026年8月销售商品猪120.00万头。"))
        self.assertEqual(parsed["status"], "extracted")
        self.assertTrue(parsed["narrative"]["agrees"])
        self.assertEqual(parsed["narrative"]["scope"], pb.SCOPE_COMMODITY)

    def test_narrative_disagreeing_with_the_table_rejects_the_file(self):
        parsed = parse(self._with("2026年8月销售商品猪200.00万头。"))
        self.assertEqual(parsed["status"], "narrative_mismatch")
        self.assertEqual(parsed["narrative_heads_10k"], 200.0)
        self.assertEqual(parsed["table_heads_10k"], 120.0)

    def test_narrative_scope_conflicting_with_the_header_rejects_the_file(self):
        """数值对得上、口径名对不上 → 我对表头的理解一定有一处是错的。"""
        parsed = parse(self._with("2026年8月销售生猪120.00万头。"))
        self.assertEqual(parsed["status"], "scope_conflict")
        self.assertEqual(parsed["narrative_scope"], pb.SCOPE_ALL)

    def test_cumulative_sentence_is_not_taken_as_the_month_value(self):
        """天康正文同时有「销售生猪 41.11 万头」与「1—8 月累计销售生猪…」。"""
        parsed = parse(bulletin(
            header=HEAD_ALL, title="2026年8月份生猪销售简报",
            extra=[row(1, "1—8月累计销售生猪265.55万头。"),
                   row(1, "2026年8月销售生猪120.00万头。")]))
        self.assertEqual(parsed["status"], "extracted")
        self.assertEqual(parsed["narrative"]["heads_10k"], 120.0)

    def test_behind_a_cumulative_hint_is_skipped(self):
        parsed = parse(bulletin(
            header=HEAD_ALL, title="2026年8月份生猪销售简报",
            extra=[row(1, "1—8月累计销售生猪265.55万头。")]))
        self.assertIsNone(parsed["narrative"]["heads_10k"])


class TestOffTableHeads(unittest.TestCase):
    def test_piglet_and_breeding_are_summed_into_a_derived_total(self):
        parsed = parse(bulletin(extra=[
            row(1, "销售仔猪15.00万头，销售种猪5.00万头。")]))
        total = by_metric(parsed, _pig.M_HOG_SALES_VOLUME)
        self.assertEqual(len(total), 1)
        self.assertEqual(total[0].value, 140.0)
        self.assertEqual(total[0].derivation, "sum_of_disclosed_product_volumes")
        self.assertFalse(total[0].is_direct_disclosure)
        self.assertTrue(total[0].is_estimated)
        self.assertEqual(total[0].period, "2026-08")

    def test_missing_products_make_the_total_insufficient_not_guessed(self):
        parsed = parse(bulletin())
        total = by_metric(parsed, _pig.M_HOG_SALES_VOLUME)
        self.assertEqual([o.status for o in total],
                         [obs.STATUS_INSUFFICIENT_SCOPE])
        self.assertIsNone(total[0].value)

    def test_only_one_product_disclosed_is_not_enough_to_sum(self):
        parsed = parse(bulletin(extra=[row(1, "销售仔猪15.00万头。")]))
        total = by_metric(parsed, _pig.M_HOG_SALES_VOLUME)
        self.assertEqual([o.status for o in total],
                         [obs.STATUS_INSUFFICIENT_SCOPE])
        self.assertIsNone(total[0].value)

    def test_piglet_breeding_and_slaughter_get_their_own_metrics(self):
        parsed = parse(bulletin(extra=[
            row(1, "销售仔猪15.00万头，销售种猪5.00万头，屠宰生猪281.80万头。")]))
        piglet = by_metric(parsed, _pig.M_PIGLET_SALES_VOLUME)
        breeding = by_metric(parsed, _pig.M_BREEDING_PIG_SALES_VOLUME)
        slaughter = by_metric(parsed, _pig.M_HOG_SLAUGHTER_VOLUME)
        self.assertEqual(piglet[0].value, 15.0)
        self.assertEqual(piglet[0].scope, pb.SCOPE_PIGLET)
        self.assertEqual(breeding[0].value, 5.0)
        self.assertEqual(breeding[0].scope, pb.SCOPE_BREEDING)
        self.assertEqual(slaughter[0].value, 281.8)
        self.assertEqual(slaughter[0].scope, pb.SCOPE_SLAUGHTER)

    def test_internal_sale_of_commodity_hogs_is_recognized(self):
        """牧原写的是「销售**商品猪**339.0万头」——只认「生猪」会整笔漏掉。

        主叙述句必须排在括号句**之前**：``_SALES_HEADS`` 也会命中括号里那句
        （「销售商品猪339.0万头」），而叙述段取的是第一个命中——真实文件里
        主句本来就在前，夹具照排。
        """
        parsed = parse(bulletin(extra=[
            row(1, "2026年8月销售商品猪120.00万头。"),
            row(1, "（其中向全资子公司牧原肉食品有限公司及其子公司合计销售"
                   "商品猪339.0万头）")]))
        self.assertEqual(parsed["internal_sales_heads_10k"], 339.0)
        self.assertEqual(parsed["internal_sales_product"], "商品猪")
        # 内部销售不另立格子，拼进当月销量那条观测的理由里。
        current = [o for o in by_metric(parsed, _pig.M_COMMODITY_HOG_SALES_VOLUME)
                   if o.period == "2026-08"]
        self.assertEqual(len(current), 1)
        self.assertIn("内部调拨", current[0].reason)
        self.assertIn("339.0", current[0].reason)

    def test_internal_sale_of_live_hogs_is_also_recognized(self):
        parsed = parse(bulletin(extra=[
            row(1, "2026年8月销售商品猪120.00万头。"),
            row(1, "其中向全资子公司销售生猪0.78万头。")]))
        self.assertEqual(parsed["internal_sales_heads_10k"], 0.78)
        self.assertEqual(parsed["internal_sales_product"], "生猪")

    def test_each_observation_carries_its_source_paragraph(self):
        parsed = parse(bulletin(extra=[row(1, "销售仔猪15.00万头。")]))
        record = by_metric(parsed, _pig.M_PIGLET_SALES_VOLUME)[0]
        self.assertIn("销售仔猪15.00万头", record.paragraph)
        self.assertEqual(record.document, HASH)
        self.assertEqual(record.publication_date, "2026-09-08")
        self.assertEqual(record.source_type, _pig.SRC_MONTHLY_BULLETIN)

    def test_price_variant_is_the_monthly_commodity_price(self):
        parsed = parse(bulletin())
        prices = by_metric(parsed, _pig.M_PIG_SALE_PRICE)
        self.assertEqual({o.metric_variant for o in prices},
                         {"monthly_commodity_price"})
        self.assertEqual({o.unit for o in prices}, {"CNY/kg"})
        self.assertEqual([o.value for o in prices], [10.0, 10.0, 10.0])


class TestForeignPeriodGuards(unittest.TestCase):
    """表外头数（仔猪 / 种猪 / 屠宰）的期间守卫（§四十 补）。

    这一圈**没有**主叙述句那样的表格交叉校验：主叙述句每个数都要与表格对账，挑错了
    会被 ``narrative_mismatch`` 整份挡掉；表外头数取错则直接变成一条 ``status=OK``、
    ``is_direct_disclosure=True`` 的当月观测。实测漏网的就是牧原 2025-09 那份——
    原文「25年**1-9月**公司**共**销售仔猪1,157.1万头」被当成 9 月单月数落了库。
    """

    def test_cumulative_span_is_refused_not_stored_as_the_month(self):
        rows = bulletin(
            title="2025年9月份销售简报",
            body=[line(1, "2025年7月", 100, 100, 10, 10, 10),
                  line(1, "2025年8月", 110, 210, 11, 21, 10),
                  line(1, "2025年9月", 120, 330, 12, 33, 10)],
            extra=[row(1, "25年1-9月公司共销售仔猪1,157.1万头。")])
        parsed = parse(rows)
        self.assertEqual(parsed["status"], "extracted")
        self.assertEqual(parsed["period"], "2025-09")
        self.assertEqual(parsed["foreign_spans"], {"piglet": "1-9月"})
        self.assertIsNone(parsed["piglet_heads_10k"])
        piglet = by_metric(parsed, _pig.M_PIGLET_SALES_VOLUME)
        self.assertEqual([o.status for o in piglet],
                         [obs.STATUS_INSUFFICIENT_SCOPE])
        self.assertIsNone(piglet[0].value)
        # 「它其实是什么」必须写在理由里——观测被单独拿出来看时载荷不在手边，
        # 理由不点名的话，读者只知道「这格空着」，不知道空掉的是什么。
        self.assertIn("1-9月", piglet[0].reason)
        self.assertIn("2025-09", piglet[0].reason)
        self.assertIn("销售仔猪1,157.1万头", piglet[0].reason)

    def test_the_cumulative_span_also_guards_breeding_and_slaughter(self):
        rows = bulletin(
            title="2025年9月份销售简报",
            body=[line(1, "2025年7月", 100, 100, 10, 10, 10),
                  line(1, "2025年8月", 110, 210, 11, 21, 10),
                  line(1, "2025年9月", 120, 330, 12, 33, 10)],
            extra=[row(1, "1-9月累计销售种猪5.00万头，累计屠宰生猪281.80万头。")])
        parsed = parse(rows)
        for metric_id in (_pig.M_BREEDING_PIG_SALES_VOLUME,
                          _pig.M_HOG_SLAUGHTER_VOLUME):
            records = by_metric(parsed, metric_id)
            self.assertEqual([o.status for o in records],
                             [obs.STATUS_INSUFFICIENT_SCOPE], metric_id)
            self.assertIsNone(records[0].value, metric_id)

    def test_no_ok_observation_is_produced_for_the_foreign_span(self):
        """反面钉死：坏路径的产物**只有** INSUFFICIENT_SCOPE，不是「既落一条错的
        又落一条对的」。"""
        rows = bulletin(
            title="2025年9月份销售简报",
            body=[line(1, "2025年7月", 100, 100, 10, 10, 10),
                  line(1, "2025年8月", 110, 210, 11, 21, 10),
                  line(1, "2025年9月", 120, 330, 12, 33, 10)],
            extra=[row(1, "25年1-9月公司共销售仔猪1,157.1万头。")])
        records = by_metric(parse(rows),
                            _pig.M_PIGLET_SALES_VOLUME)
        self.assertEqual(len(records), 1)
        self.assertNotIn(obs.STATUS_OK, [o.status for o in records])

    def test_a_span_equal_to_this_period_is_a_legitimate_merged_disclosure(self):
        """反向守卫：区间**等于本期**时它不是外来期间，照落。

        牧原「2025-01~02」那份的「1-2月份…销售仔猪219.2万头」值就是本期值
        （表体那一行的 ``period`` 也是 ``2025-01~02``）。守卫若写成「只要句子里有
        区间就丢」，这条合法的合并披露会跟着一起丢——那是把 bug 换成另一种 bug。
        """
        body = [line(1, "2025年1-2月", 1146.1, 1146.1, 100, 100, 10)]
        parsed = parse(bulletin(title="2025年1-2月份销售简报", body=body,
                                extra=[row(1, "1-2月份公司销售仔猪219.2万头。")]))
        self.assertEqual(parsed["period"], "2025-01~02")
        self.assertEqual(parsed["foreign_spans"], {})
        piglet = by_metric(parsed, _pig.M_PIGLET_SALES_VOLUME)
        self.assertEqual(len(piglet), 1)
        self.assertEqual(piglet[0].status, obs.STATUS_OK)
        self.assertEqual(piglet[0].value, 219.2)
        self.assertEqual(piglet[0].period, "2025-01~02")
        self.assertTrue(piglet[0].is_direct_disclosure)

    def test_single_month_bulletin_is_untouched_by_the_guard(self):
        """没有时间状语（或只写本期月份）时，守卫必须放行——否则等于把正路堵死。"""
        parsed = parse(bulletin(extra=[row(1, "销售仔猪15.00万头。")]))
        piglet = by_metric(parsed, _pig.M_PIGLET_SALES_VOLUME)
        self.assertEqual(parsed["foreign_spans"], {})
        self.assertEqual(piglet[0].value, 15.0)
        self.assertEqual(piglet[0].status, obs.STATUS_OK)

    def test_foreign_span_helper_is_the_one_deciding(self):
        """把判据本身钉住：返回值是**那个说法**，不是布尔。"""
        self.assertEqual(
            pb._foreign_span("25年1-9月公司共销售仔猪", 12, month=9, title_end=9),
            "1-9月")
        self.assertEqual(
            pb._foreign_span("累计销售仔猪", 6, month=9, title_end=9), "累计")
        self.assertIsNone(
            pb._foreign_span("1-2月份公司销售仔猪", 9, month=1, title_end=2))
        self.assertIsNone(pb._foreign_span("销售仔猪", 4, month=9, title_end=9))


class TestWeightArithmetic(unittest.TestCase):
    def test_unit_conversion_is_explicit(self):
        # 12 亿元 / 120 万头 / 10 元每公斤 = 100 kg/头
        self.assertEqual(pb.average_weight_kg(12, 120, 10), 100.0)

    def test_non_positive_or_missing_inputs_yield_none(self):
        for args in ((None, 120, 10), (12, None, 10), (12, 120, None),
                     (0, 120, 10), (12, 0, 10), (12, 120, 0)):
            self.assertIsNone(pb.average_weight_kg(*args), args)


class TestRejectionReason(unittest.TestCase):
    def test_rejection_reason_names_the_status(self):
        parsed = parse(bulletin(header=HEAD_ALL, title="2026年8月份生猪销售简报"))
        self.assertEqual(parsed["status"], "extracted")
        rejected = parse(bulletin(body=[]))
        text = pb._rejection_reason(rejected)
        self.assertIn("no_table", text)
        self.assertTrue(text.strip())

    def test_rejection_reason_carries_the_extra_evidence(self):
        body = body_06_08()
        body.append(line(1, "2026年8月", 130, 460, 13, 46, 10))
        text = pb._rejection_reason(parse(bulletin(body=body)))
        self.assertIn("duplicate_month", text)
        self.assertIn("first_row", text)
        self.assertIn("suspected", text)


class TestListBulletins(unittest.TestCase):
    """"没认出来"必须是一个报出来的数，不是一个要靠人想起来的疑点。"""

    def _stub(self, titles):
        items = [{"announcementTitle": t, "announcementTime": 1789000000000,
                  "adjunctUrl": "finalpage/2026-09-08/x.PDF"} for t in titles]
        return json.dumps({"totalAnnouncement": len(items),
                           "announcements": items}).encode("utf-8")

    def _patch(self, titles):
        """只桩掉 HTTP。**orgId 那次查询也要照真实契约回**（返回 list），否则
        ``org_id()`` 拿到 dict 会当成「没查到」——那是另一条路径的行为，不是这里
        要测的东西。"""
        from research import reports
        original = reports._http

        def fake(url, **kw):
            if url == reports.ExchangeOfficialProvider.SEARCH:
                return json.dumps([{"code": CODE, "orgId": "9900022995"}]
                                  ).encode("utf-8"), {}
            return self._stub(titles), {}

        reports._http = fake
        self.addCleanup(setattr, reports, "_http", original)

    def test_guanyu_titles_are_matched_not_silently_dropped(self):
        self._patch(["关于2026年6月份生猪销售简报", "2026年8月份销售简报"])
        result = pb.list_bulletins(CODE)
        self.assertEqual(len(result["matched"]), 2)
        self.assertEqual([m["period"] for m in result["matched"]],
                         ["2026-06", "2026-08"])
        self.assertEqual(result["near_miss"], [])

    def test_bulletin_like_titles_that_did_not_match_are_reported(self):
        self._patch(["2026年8月份销售简报", "2026年半年度销售简报"])
        result = pb.list_bulletins(CODE)
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(result["near_miss"], ["2026年半年度销售简报"])

    def test_correction_notices_are_visible_but_marked_skipped(self):
        self._patch(["2026年8月份销售简报（更正）"])
        result = pb.list_bulletins(CODE)
        self.assertEqual(result["near_miss"], [])
        self.assertEqual(result["matched"][0]["skipped"], "更正")


class TestObservationPayloadIntegrity(unittest.TestCase):
    def test_plain_bulletin_produces_no_rejectable_status(self):
        parsed = parse(bulletin(header=HEAD_ALL, title="2026年8月份生猪销售简报"))
        self.assertEqual(obs.check_errors(pb.observations_of(parsed)), [])

    def test_commodity_bulletin_obs_are_self_consistent(self):
        parsed = parse(bulletin())
        records = pb.observations_of(parsed)
        self.assertEqual(obs.check_errors(records), [])
        for record in records:
            self.assertEqual(record.company_code, CODE)
            self.assertEqual(record.subject, CODE)

    def test_rejected_parse_yields_no_observations(self):
        parsed = parse(bulletin(body=[]))
        self.assertEqual(pb.observations_of(parsed), [])

    def test_observations_carry_fetched_at_when_given(self):
        parsed = parse(bulletin())
        records = pb.observations_of(parsed, fetched_at="2026-09-27 10:00:00")
        self.assertEqual({o.fetched_at for o in records}, {"2026-09-27 10:00:00"})


if __name__ == "__main__":
    unittest.main()
