# -*- coding: utf-8 -*-
"""字体层：Adobe-GB1 的 CID 派生。

这一层以前完全没有字体概念——只读 ``/ToUnicode``，读不到就按 UTF-16BE 猜码位。
招商银行的半年报内嵌的是 CID-keyed CFF：**没有 ToUnicode、字形也没有名字**，
CID 号根本不是 Unicode 码位，于是 41,089 个文字片段里只有 1 个含汉字，整份
财报解成乱码，而诊断信息看起来还算干净（``undecodable`` 只有两千）。

字符信息其实不在字体程序里，而在 Adobe-GB1 这本字符集的定义里，而它可以从
标准库的 ``gb2312`` 当场算出来。这一批测试钉住的正是那张表、和它**不许生效**
的三种情形。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import pdftext  # noqa: E402
from tests import make_fixture  # noqa: E402


def _decode(pdf):
    runs, doc = pdftext.extract_runs(pdf)
    return runs, doc, pdftext.extraction_diag(doc, runs)


def _text(runs):
    return "".join(r.text for r in runs)


class TestGb1Table(unittest.TestCase):
    """派生表本身。三个锚点各钉一遍：ASCII 段、符号段、汉字段。"""

    def test_ascii_segment(self):
        """``1..95`` 就是半角 ASCII：``chr(cid + 0x1F)``。"""
        table = pdftext.gb1_cmap()
        self.assertEqual(table[1], " ")
        self.assertEqual(table[32], "?")
        self.assertEqual(table[95], "~")
        # 「0」的 CID 是 17——金额全靠这一段才读得出来
        self.assertEqual(table[0x30 - 0x1F], "0")

    def test_symbol_segment_ranked_by_assigned_position(self):
        """符号段按「已分配位置」排名，未分配的格子不占 CID。

        钉住 ``273 == '，'``：它等于 ``96 + rank(A3AC)``，而 rank 是数出来的
        ——把未分配格子也当成占位、或者按区号直接乘出来的实现，都会在这里偏。
        一个标点的 CID 偏了，全篇中文的标点就全串位，而正文照样「有字」。
        """
        table = pdftext.gb1_cmap()
        self.assertEqual(table[96], "　")
        self.assertEqual(table[273], "，")
        # 与 fixture 里那份**独立算出来**的编号对上：两边都从 GB2312 出发，
        # 但一条是「按已分配位置数排名」，一条是「直接查表」，能对上才说明
        # 排名规则没数错。
        self.assertEqual(make_fixture._gb1_cid("，"), 273)

    def test_hanzi_segment(self):
        """``940`` 起是 GB2312 的 16 区往后的 94×94 方阵。"""
        table = pdftext.gb1_cmap()
        self.assertEqual(table[940], "啊")      # 0xB0A1，一级汉字第一个
        self.assertEqual(table[941], "阿")
        self.assertEqual(make_fixture._gb1_cid("啊"), 940)
        # 16 区 94 个字位正好占满 940..1033，换区之后仍然连续
        self.assertEqual(table[1033], "剥")      # 0xB0FE，16 区最后一个
        self.assertEqual(table[1034], "薄")      # 0xB1A1，17 区第一个

    def test_hanzi_segment_starts_at_940(self):
        """汉字段的起点钉在 ``940``：939 必须是空的，940 必须有字。

        939 这一格值得单独写一条：它落在 GB2312 的 15 区（``0xAFFE``），而
        10~15 区在 GB2312 里整体是空的，于是「起点往回挪一格」在表上**什么
        都看不见**——拿这种改动当变异测试，会得到一个假的「测试没抓住」。
        真正有内容、能被抓住的是 940 本身（``啊``）。
        """
        table = pdftext.gb1_cmap()
        self.assertIn(940, table)
        self.assertNotIn(939, table)
        self.assertEqual(make_fixture._gb1_cid("啊"), 940)

    def test_no_hanzi_in_the_symbol_segment(self):
        """符号段里不该出现汉字——出现了就说明排名规则错了。

        这是这段代码最容易出静默错误的地方：排偏一格，标点会变成某个汉字，
        文本依旧「读得出来」，只是全是错字。
        """
        table = pdftext.gb1_cmap()
        for cid in range(96, 778):
            ch = table[cid]
            self.assertFalse(0x4E00 <= ord(ch) <= 0x9FFF,
                             "CID %d 解成了汉字 %r" % (cid, ch))

    def test_gap_between_symbols_and_hanzi_is_empty(self):
        """``778``~``939`` 是空的，不填。"""
        table = pdftext.gb1_cmap()
        for cid in range(778, 940):
            self.assertNotIn(cid, table)

    def test_table_is_cached(self):
        """惰性且只算一次——这份表有七千多条，每页都重算会拖慢整库。"""
        self.assertIs(pdftext.gb1_cmap(), pdftext.gb1_cmap())


class TestGb1Derivation(unittest.TestCase):
    """没有 ToUnicode 的 Adobe-GB1 字体：按字符集定义解出来。"""

    def test_decodes_hanzi_without_tounicode(self):
        pdf = make_fixture.gb1_pdf()
        runs, doc, diag = _decode(pdf)
        self.assertEqual(_text(runs), make_fixture.GB1_TEXT)
        self.assertEqual(diag["derived_cmap"], 1)
        self.assertEqual(diag["undecodable"], 0)
        self.assertEqual(diag["synthetic_cmap"], 0)
        self.assertEqual(diag["cjk_runs"], 1)

    def test_digits_and_punctuation_come_through(self):
        """金额与标点都要读出来——只解汉字的话资产负债表上全是空金额。"""
        runs, _doc, _diag = _decode(make_fixture.gb1_pdf())
        text = _text(runs)
        self.assertIn("13,785,280", text)
        self.assertIn("，", text)
        self.assertIn("资产合计", text)


class TestGb1Gates(unittest.TestCase):
    """三道门禁：没有 cmap、两字节、``/CIDSystemInfo`` 就是 ``(Adobe, GB1)``。

    第三道是**前提**而不是补充说明：派生表只在 CID 属于这套字符集时成立。
    """

    def test_identity_ordering_is_not_derived(self):
        """字符集写着 Identity 就不能派生。

        Identity 的意思是「CID 就是码位」，与 GB2312 毫无关系。当成 GB1 来解
        会把整篇财报解成另一种编号下的字——**看起来还是中文**，只是全错。
        """
        pdf = make_fixture.gb1_pdf(ordering=b"Identity")
        runs, doc, diag = _decode(pdf)
        self.assertEqual(diag["derived_cmap"], 0)
        self.assertNotIn("资产合计", _text(runs))

    def test_japan1_ordering_is_not_derived(self):
        """别的字符集一律不认——猜错的代价是整篇解成另一种语言。"""
        pdf = make_fixture.gb1_pdf(ordering=b"Japan1")
        runs, doc, diag = _decode(pdf)
        self.assertEqual(diag["derived_cmap"], 0)
        self.assertNotIn("资产合计", _text(runs))

    def test_tounicode_wins_over_derivation(self):
        """字体自己声明了映射就以它为准，派生表一次都不用。

        这条门禁坏掉的后果是**悄悄改用另一套映射**：ToUnicode 是文档自己写的，
        派生表是我们算的，两者不一致时没有理由信我们。
        """
        pdf = make_fixture.gb1_pdf(with_tounicode=True)
        runs, doc, diag = _decode(pdf)
        self.assertEqual(_text(runs), make_fixture.GB1_TEXT)
        self.assertEqual(diag["derived_cmap"], 0)
        self.assertEqual(diag["synthetic_cmap"], 0)

    def test_single_byte_font_is_untouched(self):
        """单字节字体那条路不受影响：既有 fixture 逐字节等价。

        ``balance_sheet_sample.pdf`` 是 Type0 + Identity 字符集 + ToUnicode，
        两个门禁都过不了，解出来的东西必须和加这段逻辑之前一模一样。
        """
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "balance_sheet_sample.pdf")
        with open(path, "rb") as f:
            runs, doc, diag = _decode(f.read())
        self.assertEqual(diag["derived_cmap"], 0)
        self.assertIn("资产总计", _text(runs))


if __name__ == "__main__":
    unittest.main()
