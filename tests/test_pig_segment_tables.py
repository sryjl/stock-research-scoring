# -*- coding: utf-8 -*-
"""tests/test_pig_segment_tables.py —— 分部表解析器（批 6 §十一–§十九）。

夹具是**真报告里的原始片段**（``tests/pig_segment_fixtures.py``），不是合成数据。
这一批修的每一处都在几何上：折行表头、竖排左标签、跨行拆开的金额、只隔 4.47 磅
的两列金额、印在上一页页脚的表题——合成数据造不出这些间距，而解析器就是靠间距
活着的。全部离线：夹具就是本地缓存里那几页的坐标，不联网、不读库。

三件事，按 §十二 的纪律排：

1. **抽出来的对不对**：牧原 p144（分解信息）/ p180（报告分部）、东瑞 p17；
2. **口径有没有丢**：``cost`` 只来自明确的「营业成本」列，**绝不** ``revenue − profit``；
3. **认不出就拒答**：新希望 p23 的文本抽取已损坏 → 0 条 segment；天康 p193 的分部表
   只有表头 → 如实报空，不产出任何一行。

外加几条**改一次就会回退**的几何回归（并字不许毁掉两个完整金额、行标签与数字
不同行、按表自己的行距分行）——它们各自对应一次实测踩过的坑，坑的位置写在注释里。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import pig_segment_tables as pst           # noqa: E402
from tests import pig_segment_fixtures as fx             # noqa: E402

#: 牧原 2026H1 的真实数字（原文 p144 / p180）。写死它们是**故意的**：这几个数换了，
#: 要么是报告换了期，要么是解析器读错列了，两种都得有人看一眼。
MUYUAN = {"养殖分部": (52430296318.87, 54795342077.43),
          "屠宰、肉食分部": (22061248684.20, 21124800426.76),
          "贸易分部": (4319430543.58, 4290206790.32)}
#: 东瑞 2026H1「占营业收入或营业利润 10% 以上」表（原文 p17）。
DONGRUI = {"养殖行业": (990210584.88, 1062255788.11, -0.0728),
           "生猪": (990210584.88, 1062255788.11, -0.0728)}


def parse(*keys):
    return pst.extract_segments(fx.meta(keys[0]), fx.runs_for(*keys))


def by_name(out, name):
    hit = [s for s in out["segments"] if s["raw_segment_name"] == name]
    assert hit, "%r 没抽出来，实际有 %r" % (
        name, [s["raw_segment_name"] for s in out["segments"]])
    return hit[0]


# --------------------------------------------------------------------------- #
# §二十一-12 牧原 p144：折行表头 + 竖排标签 + 跨行金额
# --------------------------------------------------------------------------- #
class TestMuyuanDecomposition(unittest.TestCase):
    """「营业收入和营业成本的分解信息」——四家只有牧原披露这种表。"""

    def setUp(self):
        self.out = parse("p144")

    def test_it_is_extracted_at_all(self):
        self.assertEqual(self.out["status"], "extracted")
        self.assertEqual([t["status"] for t in self.out["tables"]], ["extracted"])

    def test_every_partner_gets_a_revenue_and_a_cost(self):
        for name, (revenue, cost) in MUYUAN.items():
            seg = by_name(self.out, name)
            self.assertEqual(seg["revenue"], revenue, name)
            self.assertEqual(seg["cost"], cost, name)
            self.assertEqual(seg["table_family"], "segment_report")
            self.assertEqual(seg["source_page"], 144)

    def test_the_cost_comes_from_the_cost_column_not_from_subtraction(self):
        """§十四：**只在明确有营业成本列时**填 ``cost``，绝不做 ``revenue − profit``。

        这条要的是**证据链**而不是一个数：两条列坐标必须真的是表头里那两个名字
        的那两列。数值相等但列坐标相同，说明是从收入列抄的。
        """
        seg = by_name(self.out, "养殖分部")
        colmap = seg["column_map"]
        self.assertNotEqual(colmap["cost_column"], colmap["revenue_column"])
        # 毛利率那一列在 p144 这一张里根本不存在，所以 gross_profit / gross_margin
        # 都只能是 None——原文没印的数不许自己算。
        self.assertIsNone(seg["gross_profit"])
        self.assertIsNone(seg["gross_margin"])
        self.assertNotEqual(seg["revenue"] - seg["cost"], seg["gross_profit"])

    def test_raw_name_is_kept_verbatim(self):
        """§十三：不做硬名称匹配。``raw_segment_name`` 原样保留，语义只进 category。"""
        for seg in self.out["segments"]:
            self.assertEqual(seg["raw_segment_name"], seg["segment_name"])
        self.assertEqual(by_name(self.out, "养殖分部")["segment_category"], "PIG")
        self.assertEqual(by_name(self.out, "屠宰、肉食分部")["segment_category"],
                         "SLAUGHTER")

    def test_the_elimination_row_produces_no_record(self):
        """「减：生猪与屠宰之间销售抵消」是勾稽用的，不是分部。"""
        self.assertNotIn("合计", [s["raw_segment_name"] for s in self.out["segments"]])
        self.assertEqual(len(self.out["segments"]), 4)


# --------------------------------------------------------------------------- #
# §二十一-15 牧原 p180：资产/负债如实为 None，理由点名原文
# --------------------------------------------------------------------------- #
class TestMuyuanReportableSegments(unittest.TestCase):
    """「报告分部的财务信息」——列是分部，行是度量（营业收入/营业成本是行标签）。"""

    def setUp(self):
        self.out = parse("p180")

    def test_assets_and_liabilities_stay_none_and_say_why(self):
        """原文 p180 明写「不能披露各报告分部的资产总额和负债总额」。

        这正是 ``assets`` / ``liabilities`` 该为 ``None`` 的那种「没有」——**不是**
        「没抽到」，所以每一行都带着原文那句 ``reason``。
        """
        self.assertEqual(self.out["status"], "extracted")
        self.assertTrue(self.out["segments"])
        for seg in self.out["segments"]:
            self.assertIsNone(seg["assets"])
            self.assertIsNone(seg["liabilities"])
            self.assertIsNone(seg["capex"])
            self.assertIn("不能披露各报告分部的资产总额和负债总额", seg["reason"])

    def test_the_numbers_agree_with_the_other_table(self):
        """p180 与 p144 是两张不同的表，同一家公司的同一个分部应当对得上。"""
        for name, (revenue, cost) in MUYUAN.items():
            seg = by_name(self.out, name)
            self.assertEqual(seg["revenue"], revenue, name)
            self.assertEqual(seg["cost"], cost, name)


# --------------------------------------------------------------------------- #
# §二十一-13 东瑞 p17：「10% 以上」表，表题在**上一页**
# --------------------------------------------------------------------------- #
class TestDongruiIndustryProduct(unittest.TestCase):
    def setUp(self):
        self.out = parse("p16", "p17")       # p16 只有页脚那三行（表题 / 适用 / 单位）

    def test_the_cross_page_title_is_what_lets_this_table_through(self):
        """把 p16 摘掉，这张表就没了——表题印在上一页页脚，数据在下一页。"""
        alone = parse("p17")
        self.assertEqual(alone["status"], "not_found")
        self.assertEqual(alone["segments"], [])

    def test_family_and_values(self):
        self.assertEqual(self.out["status"], "extracted")
        for name, (revenue, cost, margin) in DONGRUI.items():
            seg = by_name(self.out, name)
            self.assertEqual(seg["table_family"], "industry_product")
            self.assertEqual(seg["revenue"], revenue, name)
            self.assertEqual(seg["cost"], cost, name)
            self.assertEqual(seg["gross_margin"], margin, name)
            self.assertEqual(seg["source_section"], "分产品" if name == "生猪" else "分行业")

    def test_the_cost_column_is_a_real_column(self):
        """证据链不是「值对」，是「值来自表头那一列」。"""
        colmap = by_name(self.out, "养殖行业")["column_map"]
        self.assertIn("毛利率", colmap["columns"])
        self.assertNotEqual(colmap["cost_column"], colmap["columns"]["营业收入"])
        self.assertEqual(colmap["cost_column"], colmap["columns"]["营业成本"])


# --------------------------------------------------------------------------- #
# §二十一-14 新希望：文本抽取已损坏 → 拒答，0 条
# --------------------------------------------------------------------------- #
class TestXinxiwangRefusal(unittest.TestCase):
    """这批交付里**最重要**的一条：宁可什么都不给，也不给一个错的。"""

    def setUp(self):
        self.out = parse("p23", "p37")

    def test_broken_text_yields_no_segment_at_all(self):
        self.assertEqual(self.out["status"], "unparsable")
        self.assertEqual(self.out["segments"], [])

    def test_the_refusal_carries_a_reason(self):
        self.assertTrue(self.out["tables"])
        for table in self.out["tables"]:
            self.assertNotEqual(table["status"], "extracted")
            self.assertTrue(table["reason"], table)

    def test_no_segment_record_is_ever_status_extracted_by_luck(self):
        """``status`` 只有 ``extracted`` 一种取值——没有「大概吧」这一档。"""
        for seg in self.out["segments"]:
            self.assertEqual(seg["status"], "extracted")


# --------------------------------------------------------------------------- #
# 天康 p193：分部表只有表头 → 如实报空（不是「没找到」）
# --------------------------------------------------------------------------- #
class TestTiankangEmptySegmentTable(unittest.TestCase):
    """「（2）报告分部的财务信息」底下只有「项目｜分部间抵销｜合计」，一格数字都没有。"""

    def setUp(self):
        self.out = parse("p193")

    def test_it_reports_empty_instead_of_silence(self):
        self.assertEqual(self.out["segments"], [])
        empty = [t for t in self.out["tables"] if t["status"] == "empty"]
        self.assertEqual(len(empty), 1)
        self.assertIn("报告分部的财务信息", empty[0]["reason"])

    def test_a_title_line_is_not_a_table(self):
        """表题自己也是「一行表头块」。它被上一行的表题认领过一次，报出过假条目。"""
        reasons = [t.get("reason") or "" for t in self.out["tables"]]
        self.assertNotIn("（1）报告分部的确定依据与会计政策", reasons)
        self.assertNotIn("6、分部信息", reasons)


# --------------------------------------------------------------------------- #
# 几何回归：每一处都对应一次实测踩过的坑
# --------------------------------------------------------------------------- #
class TestMoneyIsNeverFusedForNothing(unittest.TestCase):
    """牧原 2025 年报 p29 的两列金额只隔 4.47 磅，而并字阈值是 5.4。"""

    def _runs(self, second_x):
        from research import pdftext
        return [pdftext.Run(1, 121.10, 600.0, "140,207,176,872.34", "F1", 9.0),
                pdftext.Run(1, second_x, 600.0, "115,970,785,673.49", "F1", 9.0)]

    def test_two_complete_amounts_stay_two_cells_even_when_the_gap_is_tiny(self):
        lines = pst.page_lines(self._runs(206.57), 1)
        self.assertEqual([c["text"] for c in lines[0][1]],
                         ["140,207,176,872.34", "115,970,785,673.49"])

    def test_a_split_amount_still_gets_joined(self):
        """两片**各不完整**时照并不误——这条规则只管「并了会毁掉两个好数」。"""
        from research import pdftext
        runs = [pdftext.Run(1, 83.50, 600.0, "52,430,29", "F1", 9.0),
                pdftext.Run(1, 92.00, 600.0, "6,318.87", "F1", 9.0)]
        lines = pst.page_lines(runs, 1)
        self.assertEqual([c["text"] for c in lines[0][1]], ["52,430,296,318.87"])

    def test_the_annual_report_table_parses(self):
        out = parse("p29")
        self.assertEqual(out["status"], "extracted")
        seg = by_name(out, "养殖业务")
        self.assertEqual(seg["revenue"], 140207176872.34)
        self.assertEqual(seg["cost"], 115970785673.49)

    def test_a_label_split_across_lines_is_put_back_together(self):
        """这张表把「屠宰、肉食业务」拆在数字的上下两行，半年报那张是并排的。"""
        out = parse("p29")
        self.assertIn("屠宰、肉食业务",
                      [s["raw_segment_name"] for s in out["segments"]])

    def test_sections_are_carried_onto_the_rows(self):
        """分行业 / 分产品 / 分地区 三个小标题各自管住它下面那几行。"""
        out = parse("p29")
        self.assertEqual(by_name(out, "养殖业务")["source_section"], "分行业")
        self.assertEqual(by_name(out, "生猪")["source_section"], "分产品")
        self.assertEqual(by_name(out, "国内")["source_section"], "分地区")


class TestRowPitchComesFromTheTable(unittest.TestCase):
    """新希望 p274 的正文行距 28.8 磅，而那张分部表的数据行只隔 13 磅。"""

    def test_it_takes_the_smaller_of_page_pitch_and_data_pitch(self):
        data = [(600.0 - 13.0 * i, []) for i in range(8)]
        self.assertEqual(pst._row_pitch(data, 28.8), 13.0)

    def test_it_falls_back_when_there_is_nothing_to_measure(self):
        self.assertEqual(pst._row_pitch([(600.0, [])], 28.8), 28.8)
        self.assertEqual(pst._row_pitch([], 28.8), 28.8)

    def test_it_never_widens_the_page_pitch(self):
        data = [(600.0 - 40.0 * i, []) for i in range(5)]
        self.assertEqual(pst._row_pitch(data, 12.0), 12.0)


class TestNotASegment(unittest.TestCase):
    """名字错了比数字错了更难发现——它看着像个分部。"""

    def test_consolidation_columns_are_not_segments(self):
        for name in ("合并", "合计", "分部间抵销", "减：内部抵消"):
            self.assertTrue(pst.is_elimination(name), name)

    def test_period_columns_are_not_segments(self):
        for name in ("本期数", "同期数", "上年同期数"):
            self.assertIn(name, pst._PERIOD_WORDS)

    def test_the_row_table_floor_is_a_floor_not_an_accuracy_claim(self):
        """行式表那条路的 ``bands`` 是从数字区现算的，会把下一张表的行也算进来。

        所以它只能当「有没有只配上一小半」的地板：牧原 2025A 的 8/11（0.727）与
        东瑞 2025A 的 8/12（0.667）要过，两张非分部表的 5/12（0.417）与 4/11
        （0.364）要挡。0.6 是这两组数之间唯一的窄缝。
        """
        def spread(named, total):
            return [("生猪", 0.0)] * named + [(None, None)] * (total - named)

        self.assertTrue(pst._covers_bands(spread(8, 11), list(range(11))))    # 0.727
        self.assertTrue(pst._covers_bands(spread(8, 12), list(range(12))))    # 0.667
        self.assertFalse(pst._covers_bands(spread(5, 12), list(range(12))))   # 0.417
        self.assertFalse(pst._covers_bands(spread(4, 11), list(range(11))))   # 0.364

    def test_the_row_owner_path_demands_every_row(self):
        """``ratio=1.0, slack=1``：一个带都不许漏——允许一行没名字（通常是合计）。

        东瑞 p127 是 6/7（有合计行），过；新希望 p223 那张税项表是 5/12，拒。
        """
        labels = [("生猪", 0.0)] * 6 + [(None, None)]
        self.assertTrue(pst._covers_bands(labels, list(range(7)), ratio=1.0, slack=1))
        self.assertFalse(pst._covers_bands(labels, list(range(12)), ratio=1.0, slack=1))

    def test_no_bands_is_never_a_pass(self):
        """没有带就没有可核的东西——这是「没找到表」，不是「表全配上了」。"""
        self.assertFalse(pst._covers_bands([("生猪", 0.0)], []))


class TestPercentIsConvertedOnce(unittest.TestCase):
    """量纲在解析这一层换算干净：原文印 ``-4.51%``，记录里存 ``-0.0451``。"""

    def test_percent(self):
        self.assertAlmostEqual(pst.parse_percent("-4.51%"), -0.0451, places=6)
        self.assertEqual(pst.parse_percent("1,234"), None)

    def test_percent_is_not_money(self):
        self.assertIsNone(pst.parse_money("-4.51%"))
        self.assertEqual(pst.parse_money("52,430,296,318.87"), 52430296318.87)


if __name__ == "__main__":
    unittest.main()
