# -*- coding: utf-8 -*-
"""ASSET_SEMANTIC_ENGINE_V1 + CIGAR/ASSET_VALUE METRICS V2 的测试。

分三层：

1. **字典与折价表**——纯函数，直接构造名字调用。这一层是「规则命不中就
   不许猜」的守门人，也是唯一决定经济类别的地方。
2. **抽取链**——从 ``tests/fixtures/balance_sheet_sample.pdf`` 这份**真实
   PDF** 里解析。断言的是「解析出来的关系」（明细合计 = 科目余额、折价后
   价值 = 账面 × 配置费率），不是写死的数字。写死数字的测试在代码改错时
   照样会通过，只要常量凑巧没变。
3. **LLM 兜底**——不发网络请求，用一个假的 transport 验证门槛、缓存与失败
   降级。Key 与调用纪律是这一层的重点。

刻意**不**断言任何真实股票的具体数值（三角轮胎的 121.6 亿之类）。那些数字
是结果不是规范，写进测试就变成了「为了让测试通过而保住某个结果」。
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import asset_engine
from research import asset_semantics as sem
from research import haircut as hc
from research import llm_classify
from research import pdftext

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "balance_sheet_sample.pdf")

#: fixture 里写死的内容（生成脚本 tests/make_fixture.py 里的同样数字）。
#: 这里出现常量是**输入**，不是**期望输出**——期望输出一律由解析算出。
FIX_TOTAL_ASSETS = 5_941_000_000.00
FIX_CD = 700_000_000.00            # 其他流动资产 → 可转让大额存单
FIX_RESTRICTED = 50_000.00         # 附注正文里的受限金额
FIX_GOODWILL = 300_000_000.00
FIX_FIXED_ASSET = 1_200_000_000.00     # 固定资产 → 房屋建筑物
FIX_TAX_INPUT = 100_000_000.00         # 其他流动资产 → 待抵扣增值税进项税额（折行印的）
FIX_MINUS = -10_000.00                 # 其他流动资产 → 减：其他流动资产减值准备


def _rows():
    with open(FIXTURE, "rb") as f:
        runs, doc = pdftext.extract_runs(f.read())
    assert doc.undecodable == 0, "fixture 不该有解不出的字符"
    return pdftext.to_rows(runs)


# --------------------------------------------------------------------------- #
# 1. 语义字典
# --------------------------------------------------------------------------- #
class TestSemanticDictionary(unittest.TestCase):
    def test_specific_rules_win_over_general(self):
        """越具体的规则必须排在越前面。

        「其他货币资金」若不排在「货币资金」之前，就会被后者先吃掉；这类
        顺序错误不会报错，只会让少数科目悄悄归错类。
        """
        self.assertEqual(sem.classify("其他货币资金")[0], sem.BANK_DEPOSIT)

    def test_negotiable_cd_is_near_cash(self):
        """可转让大额存单是这次要修的核心——它藏在「其他流动资产」里。"""
        cls, _, pat = sem.classify("可转让大额存单")
        self.assertEqual(cls, sem.NEGOTIABLE_CD)
        self.assertIsNotNone(pat)
        self.assertIn(cls, sem.NEAR_CASH_CLASSES)

    def test_term_deposit_variants(self):
        for name in ("定期存款", "一年内到期的定期存款", "通知存款", "协定存款"):
            self.assertEqual(sem.classify(name)[0], sem.TERM_DEPOSIT, name)

    def test_unknown_is_not_guessed(self):
        """认不出来就是认不出来。猜错一个类别比留个 UNKNOWN 伤害大得多：
        UNKNOWN 折价为零、不进清算价值，是「保守」；猜成类现金是「乐观」。"""
        for name in ("其他", "递延收益", "待处理财产损溢", "卖出回购金融资产款"):
            cls, restricted, pat = sem.classify(name)
            self.assertIsNone(pat, name)
            self.assertEqual(cls, sem.OTHER_UNKNOWN, name)

    def test_partial_name_still_matches_its_rule(self):
        """规则是「出现在名字里就算」，不是整串相等——
        附注里的名字常带前后缀，逐字相等什么都匹配不上。"""
        self.assertEqual(sem.classify("商誉减值准备")[0], sem.GOODWILL)
        self.assertEqual(sem.classify("其中：可转让大额存单")[0], sem.NEGOTIABLE_CD)

    def test_sub_item_beats_account(self):
        """附注子项名优先于一级科目名——子项才是真相。"""
        self.assertEqual(sem.classify("可转让大额存单", "其他流动资产")[0],
                         sem.NEGOTIABLE_CD)

    def test_every_class_is_declared(self):
        for _, cls, _ in sem.SEMANTIC_RULES:
            self.assertIn(cls, sem.ECONOMIC_CLASSES)
        self.assertEqual(len(set(sem.ECONOMIC_CLASSES)),
                         len(sem.ECONOMIC_CLASSES), "类别有重名")

    def test_every_class_has_a_label(self):
        """每个类目都要有中文名：审计页靠它把 ``LOAN_RECEIVABLE`` 显示成人话。

        漏了不会报错，只会让页面上出现一串英文常量——而审计页是唯一能看出
        「这笔钱被当成什么」的地方，那里出现读不懂的东西就等于没有明细。
        """
        missing = [c for c in sem.ECONOMIC_CLASSES if c not in sem.CLASS_LABELS]
        self.assertEqual(missing, [])


class TestBankAndInsuranceCaptions(unittest.TestCase):
    """银行 / 保险报表的科目。名字抄的是招行（600036）与平安（601318）的原文。

    这两家的主要资产原来一个都没命中——招行覆盖率 1.68%、平安 32.53%，
    未命中即不许猜，于是两家的资产价值口径基本是空的。
    """

    def test_asset_captions(self):
        """``(名字…)`` 按 ``(sub_item, account)`` 的顺序给——附注子项的名字
        （「套期衍生工具」）认不出来时，走的是它挂在的一级科目。"""
        for names, expect in (
                (("贷款和垫款",), sem.LOAN_RECEIVABLE),
                (("发放贷款及垫款",), sem.LOAN_RECEIVABLE),
                (("发放贷款及垫款（一年内）",), sem.LOAN_RECEIVABLE),
                (("保户质押贷款",), sem.LOAN_RECEIVABLE),
                (("存放中央银行款项",), sem.CENTRAL_BANK_DEPOSIT),
                (("存放同业和其他金融机构款项",), sem.INTERBANK_CLAIM),
                (("拆出资金",), sem.INTERBANK_CLAIM),
                (("买入返售金融资产",), sem.INTERBANK_CLAIM),
                (("结算备付金",), sem.INTERBANK_CLAIM),
                (("贵金属",), sem.PRECIOUS_METALS),
                (("衍生金融资产",), sem.DERIVATIVE_ASSET),
                (("套期衍生工具", "衍生金融资产"), sem.DERIVATIVE_ASSET),
                (("远期外汇合约", "衍生金融资产"), sem.DERIVATIVE_ASSET),
                (("应收保费",), sem.RECEIVABLE_NORMAL)):
            self.assertEqual(sem.classify(*names)[0], expect, names)

    def test_measured_at_amortised_cost_reuses_the_low_risk_class(self):
        """银行把「债权投资」写成「以摊余成本计量的债务工具投资」——同一笔东西的
        两种写法，所以复用同一个类别。

        另立一个平行类目的话，同一笔资产会有两档折价率，日后改一边就漏另一边。
        """
        for name in ("以摊余成本计量的债务工具投资",
                     "以公允价值计量且其变动计入其他综合收益的债务工具投资"):
            self.assertEqual(sem.classify(name)[0], sem.LOW_RISK_FINANCIAL_ASSET,
                             name)

    def test_fvtpl_reuses_the_marketable_class(self):
        """FVTPL 这一格与「交易性金融资产」是同一个计量类别，折价率也共用
        （0.55，比裁定时说的 0.8~0.9 保守）。"""
        for name in ("以公允价值计量且其变动计入当期损益的金融投资",
                     "以公允价值计量且其变动计入当期损益的金融资产",
                     "指定为以公允价值计量且其变动计入其他综合收益的权益工具投资"):
            self.assertEqual(sem.classify(name)[0], sem.MARKETABLE_SECURITY, name)

    def test_sell_repurchase_stays_unknown_on_the_asset_side(self):
        """「卖出回购」是**负债**科目，资产侧那条规则写的是「买入返售」。

        只要有人图省事把它写成 ``返售|回购``，一笔负债就会被当成资产计价。
        """
        cls, _, pat = sem.classify("卖出回购金融资产款")
        self.assertIsNone(pat)
        self.assertEqual(cls, sem.OTHER_UNKNOWN)

    def test_cash_parent_vetoes_the_bank_classes(self):
        """挂在「货币资金」下面的同业/央行存款是现金，不是同业债权。

        青岛啤酒的货币资金下面正好列着这两行（85 亿）：照银行口径认下来，一个
        几乎不借钱的消费公司会凭空少掉 85 亿现金。
        """
        self.assertEqual(sem.classify("存放同业款项(注1)", "货币资金")[0], sem.CASH)
        self.assertEqual(sem.classify("存放中央银行款项(注2)", "货币资金")[0],
                         sem.CASH)
        # 一级科目本身就是这笔钱（银行报表）：照银行口径走
        self.assertEqual(sem.classify("存放同业和其他金融机构款项",
                                      "存放同业和其他金融机构款项")[0],
                         sem.INTERBANK_CLAIM)
        # 银行那种「现金及存放中央银行款项」的栏位名，不在否决之列
        self.assertEqual(sem.classify("存放中央银行款项", "现金及存放中央银行款项")[0],
                         sem.CENTRAL_BANK_DEPOSIT)

    def test_cash_parent_only_vetoes_the_bank_classes(self):
        """否决只对新加的那几个类别生效。「定期存款」挂在货币资金下面是常态，
        它照旧是定期存款——改这个集合等于改既有 21 只的口径。"""
        self.assertEqual(sem.classify("定期存款(注)", "货币资金")[0], sem.TERM_DEPOSIT)
        self.assertEqual(sem.classify("结构性存款", "货币资金")[0],
                         sem.STRUCTURED_DEPOSIT)


# --------------------------------------------------------------------------- #
# 2. 金额解析
# --------------------------------------------------------------------------- #
class TestParseAmount(unittest.TestCase):
    def test_blank_is_none_not_zero(self):
        """「没披露」和「确实是零」必须分开。混为一谈会让缺失数据变成 0 分，
        而缺失数据的正确处置是从分母里拿掉，不是记零。"""
        for blank in ("", "-", "—", "/", "不适用", "无", None):
            self.assertIsNone(sem.parse_amount(blank), repr(blank))
        self.assertEqual(sem.parse_amount("0.00"), 0.0)

    def test_dash_is_zero_in_a_table_cell(self):
        """表格里的「-」是**明确的零**，不是「没披露」。

        两者在自由文本里可以混，在表格列里不能：丢掉一个「-」，后面的列会
        整体左移一格，期初数顶到期末。青岛啤酒的「国债逆回购投资」期末是 0，
        期初 3.5 亿，就是这么被顶上去的——明细合计随即对不上科目余额，
        整条附注作废。资产负债表上更贵：合并列的「-」被跳过后，母公司那一列
        的数字会顶上来冒充合并数，有息负债跟着一起错。
        """
        for dash in ("-", "—", "－", "/"):
            self.assertEqual(sem.parse_amount_slot(dash), 0.0, dash)
        self.assertEqual(sem.parse_amount_slot("1,234.56"), 1234.56)
        # 「没披露」仍然走 parse_amount 的语义：表格之外的空白不是零
        self.assertIsNone(sem.parse_amount_slot(""))

    def test_thousands_and_negatives(self):
        self.assertAlmostEqual(sem.parse_amount("8,072,022,524.78"),
                               8072022524.78, places=2)
        self.assertAlmostEqual(sem.parse_amount("(1,234.56)"), -1234.56, places=2)
        self.assertAlmostEqual(sem.parse_amount("（1,234.56）"), -1234.56, places=2)

    def test_note_number_is_not_an_amount_column(self):
        """附注号长得像金额，必须能被认出来。

        华域汽车的附注列就是光秃秃的 1、13。当成金额读的话，「货币资金」
        会变成 1.00 元，整张资产负债表全错位。
        """
        self.assertTrue(sem._is_note_ref("七、13"))
        self.assertTrue(sem._is_note_ref("13"))
        self.assertFalse(sem._is_note_ref("1,234.56"))
        self.assertFalse(sem._is_note_ref("2,631,000,000.00"))


# --------------------------------------------------------------------------- #
# 3. 折价引擎
# --------------------------------------------------------------------------- #
class TestHaircut(unittest.TestCase):
    def test_goodwill_is_always_zero(self):
        """商誉在任何口径下都不构成清算价值。"""
        for s in hc.SCENARIOS:
            self.assertEqual(hc.rate(sem.GOODWILL, s), 0.0, s)

    def test_unknown_never_valued_as_cash(self):
        """UNKNOWN 不得按高流动性资产估值。

        否则「没认出来」就成了「很值钱」，系统会系统性地奖励抽取失败——
        越读不懂的报告，清算价值越高。
        """
        for s in hc.SCENARIOS:
            unknown = hc.rate(sem.OTHER_UNKNOWN, s)
            self.assertEqual(unknown, 0.0, s)
            for liquid in sem.LIQUID_FINANCIAL_CLASSES:
                self.assertLess(unknown, hc.rate(liquid, s),
                                f"{s}: UNKNOWN 不该高过 {liquid}")
                self.assertGreater(hc.rate(liquid, s), 0.0, liquid)

    def test_scenarios_are_monotonic(self):
        """保守 ≤ 基准 ≤ 乐观。反过来说明表写错了。"""
        order = (hc.CONSERVATIVE, hc.BASE, hc.OPTIMISTIC)
        for cls in sem.ECONOMIC_CLASSES:
            rates = [hc.rate(cls, s) for s in order]
            self.assertEqual(rates, sorted(rates), f"{cls} 的口径不单调：{rates}")

    def test_rates_are_fractions(self):
        for cls in sem.ECONOMIC_CLASSES:
            for s in hc.SCENARIOS:
                r = hc.rate(cls, s)
                self.assertGreaterEqual(r, 0.0, f"{cls}/{s}")
                self.assertLessEqual(r, 1.0, f"{cls}/{s}")

    def test_unknown_class_falls_back_conservatively(self):
        """折价表里没有的类别不能默认按 1.0 处理。"""
        self.assertEqual(hc.rate("NO_SUCH_CLASS", hc.OPTIMISTIC), 0.0)
        self.assertEqual(hc.rate("NO_SUCH_CLASS", hc.BASE), 0.0)

    def test_bad_scenario_is_rejected(self):
        with self.assertRaises(ValueError):
            hc.rate(sem.CASH, "SOMETIMES")

    def test_intangible_and_tax_excluded_from_liquidation(self):
        """无形资产、税项资产、预付费用整类不进清算价值——它们依附于
        「公司继续经营」，清算时不存在。"""
        for cls in (sem.INTANGIBLE_ASSET, sem.TAX_ASSET, sem.PREPAID_ASSET,
                    sem.GOODWILL, sem.OTHER_UNKNOWN):
            self.assertIn(cls, hc.EXCLUDED_FROM_LIQUIDATION, cls)

    def test_every_class_has_a_haircut(self):
        """**每个**经济类别都必须在折价表里有档。

        漏登记的类别不报错：``DEFAULT_HAIRCUT`` 是 {0,0,0}，于是它算进覆盖率
        却不计钱——页面上那一行有类别、有金额、折价后价值是零，看不出异常。
        新加一个类别而忘了加档位，只有这一条能拦住。
        """
        missing = [c for c in sem.ECONOMIC_CLASSES if c not in hc.HAIRCUT_TABLE]
        self.assertEqual(missing, [])

    def test_bank_classes_are_not_cash_tiers(self):
        """存放央行 / 同业按 1.0 计入清算，但**不进类现金档**。

        类现金的含义是「能拿来还债、能分给股东的钱」，银行的法定存款准备金
        不是。进了档，「调整后净现金/市值」和「资产流动性」会跟着一起变，
        那是另一件事。"""
        for cls in (sem.CENTRAL_BANK_DEPOSIT, sem.INTERBANK_CLAIM,
                    sem.LOAN_RECEIVABLE, sem.PRECIOUS_METALS,
                    sem.DERIVATIVE_ASSET):
            for tier in (sem.PURE_CASH_CLASSES, sem.NEAR_CASH_CLASSES,
                         sem.LIQUID_FINANCIAL_CLASSES):
                self.assertNotIn(cls, tier, cls)


# --------------------------------------------------------------------------- #
# 4. 从 fixture PDF 抽取（端到端）
# --------------------------------------------------------------------------- #
class TestExtractionFromFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _rows()
        cls.view = sem.build_economic_view(cls.rows)

    def test_picks_consolidated_not_parent(self):
        """必须取合并报表。

        母公司报表的资产总计只有 30 亿，合并是 59.41 亿。取错了表面上一切
        正常，只是所有数字都小一截——比报错更难发现。
        """
        self.assertAlmostEqual(self.view["total_assets"], FIX_TOTAL_ASSETS, places=2)

    def test_asset_items_reconcile_to_total(self):
        """经济资产明细的合计必须等于资产总计。

        这是整条链唯一一个不依赖排版的正确性判据：会计恒等式。少读一个
        科目、把负债混进来、同一个科目重复计，都会在这里露出来。
        """
        total = sum(it.amount or 0.0 for it in self.view["items"])
        self.assertAlmostEqual(total, self.view["total_assets"], places=2)

    def test_hidden_cd_is_extracted_and_classified(self):
        """藏在「其他流动资产」里的可转让大额存单必须被认出来。

        这正是三角轮胎那个 bug 的形态：一级科目是「其他流动资产」，经济
        实质是随时可变现的大额存单。
        """
        cds = [it for it in self.view["items"]
               if it.economic_class == sem.NEGOTIABLE_CD]
        self.assertEqual(len(cds), 1)
        self.assertAlmostEqual(cds[0].amount, FIX_CD, places=2)
        self.assertEqual(cds[0].account, "其他流动资产")
        self.assertIsNotNone(cds[0].source_text, "要留下原文以便复核")

    def test_minus_line_is_subtracted(self):
        """「减：」是减项，不是排版噪音。

        不当成负数，明细合计永远对不上科目余额，附注被判冲突、退回整笔计价。
        青岛啤酒的其他非流动资产就是靠这一条才对上的：定期存款 53.8 亿
        减一年内到期 16.9 亿 = 38.3 亿。
        """
        minus = [it for it in self.view["items"] if it.sub_item.startswith("减：")]
        self.assertTrue(minus, "fixture 里有一条「减：」明细")
        for it in minus:
            self.assertLess(it.amount, 0.0, it.sub_item)
            self.assertAlmostEqual(it.amount, FIX_MINUS, places=2)

    def test_wrapped_label_is_rejoined(self):
        """折行的科目名要接回去。

        名字太长时排版折行、金额印在中间那一行——接不回去这一项就丢了，
        明细合计对不上，整条附注作废。
        """
        wrapped = [it for it in self.view["items"]
                   if it.sub_item == "待抵扣增值税进项税额"]
        self.assertEqual(len(wrapped), 1)
        # 接回去的同时，金额不能跟着串位：金额印在折行的中间那一行
        self.assertAlmostEqual(wrapped[0].amount, FIX_TAX_INPUT, places=2)

    def test_dash_row_does_not_shift_columns(self):
        """「-」那一行的期初数不能顶到期末去。"""
        dash = [it for it in self.view["items"] if it.sub_item == "国债逆回购投资"]
        self.assertEqual(len(dash), 1)
        self.assertEqual(dash[0].amount, 0.0)
        self.assertAlmostEqual(dash[0].prior, 50_000_000.00, places=2)

    def test_date_header_note_is_parsed(self):
        """表头印报表日而不是「期末余额」时，明细同样要读出来。"""
        fixed = [it for it in self.view["items"] if it.account == "固定资产"]
        self.assertTrue(fixed)
        self.assertAlmostEqual(sum(x.amount for x in fixed), FIX_FIXED_ASSET,
                               places=2)

    def test_cross_validation_rejects_mismatched_note(self):
        """明细合计对不上科目余额时，退回整笔计价并记 conflict。

        附注排版千变万化，靠表头锚定总会偶尔读错表；但「子项加起来等不等于
        一级科目」是会计恒等式，不依赖排版。宁可整笔计价并标出来，也不能
        拿一批对不上的明细去算清算价值。
        """
        for account in self.view["note_conflicts"]:
            self.assertNotEqual(account["expected"], account["parsed_total"])
        # fixture 里每条附注都是对得上的，所以不该有冲突
        self.assertEqual(self.view["note_conflicts"], [])

    def test_restricted_cash_read_from_prose(self):
        """受限金额写在附注正文而不是表格里，要逐字读出来。

        关键不是「读到 5 万」，而是**没有**把 26 亿货币资金整体当受限——
        那会让现金层级凭空少掉一大块。
        """
        self.assertAlmostEqual(self.view["restricted_cash"], FIX_RESTRICTED, places=2)
        self.assertIsNotNone(self.view["restricted_source"])
        self.assertIsNotNone(self.view["restricted_page"])

    def test_cash_tiers_are_nested(self):
        """三级现金是递进的：上一级每一分钱都算在下一级里。"""
        tiers = sem.cash_tiers(self.view["items"], self.view["restricted_cash"])
        self.assertLessEqual(tiers["PureCash"], tiers["NearCash"])
        self.assertLessEqual(tiers["NearCash"], tiers["LiquidFinancialAssets"])
        # 受限部分被扣掉，而不是把整个科目踢出去
        self.assertGreater(tiers["PureCash"], 0.0)

    def test_every_item_has_evidence(self):
        """每一项都要能回答「凭什么这么判」：页码、原文、命中规则。"""
        for it in self.view["items"]:
            self.assertIsNotNone(it.page, f"{it.sub_item} 没有页码")
            self.assertIn(it.economic_class, sem.ECONOMIC_CLASSES)
            self.assertIn(it.method, ("rule", "llm", "unresolved"))
            if it.method == "rule":
                self.assertTrue(it.evidence, f"{it.sub_item} 命中规则却没写依据")

    def test_goodwill_is_priced_at_zero_end_to_end(self):
        lv = hc.liquidating_value(self.view["items"], hc.OPTIMISTIC)
        without = hc.liquidating_value(
            [it for it in self.view["items"] if it.economic_class != sem.GOODWILL],
            hc.OPTIMISTIC)
        self.assertAlmostEqual(lv["gross_adjusted_assets"],
                               without["gross_adjusted_assets"], places=2)
        self.assertEqual(hc.rate(sem.GOODWILL, hc.OPTIMISTIC), 0.0)

    def test_adjusted_value_is_book_times_configured_rate(self):
        """折价后价值 = 账面 × 配置表里的费率。费率只住在一处。"""
        for it in self.view["items"]:
            for s in hc.SCENARIOS:
                self.assertAlmostEqual(
                    hc.adjusted_value(it, s),
                    (it.amount or 0.0) * hc.rate(it.economic_class, s), places=6)


# --------------------------------------------------------------------------- #
# 5. 报表定位的变体
# --------------------------------------------------------------------------- #
class TestStatementHeading(unittest.TestCase):
    def test_heading_variants(self):
        for text in ("合并资产负债表", "1、合并资产负债表",
                     "合并及公司资产负债表", "合并资产负债表-续"):
            self.assertTrue(sem.is_statement_heading(text, "合并"), text)

    def test_toc_line_is_not_a_heading(self):
        """目录行带页码，排在正文标题前面。认成标题会把整张表定位到目录里。"""
        self.assertFalse(sem.is_statement_heading("合并及公司资产负债表29-30", "合并"))

    def test_policy_sentence_is_not_a_heading(self):
        self.assertFalse(sem.is_statement_heading(
            "于资产负债表日，外币货币性项目采用该日即期汇率折算", "合并"))

    def test_parent_heading_does_not_match_consolidated(self):
        self.assertFalse(sem.is_statement_heading("母公司资产负债表", "合并"))
        self.assertTrue(sem.is_statement_heading("母公司资产负债表", "母公司"))


def _row(y, *cells):
    """造一行版面：``_row(700, ("货币资金", 80), ("1,000", 330))``。"""
    return {"page": 1, "y": y,
            "cells": [{"text": t, "x": x} for t, x in cells]}


class TestStatementBoundary(unittest.TestCase):
    """报表区间的**收尾**。

    区间不收在表尾，就会一路吃到附注里。附注里满是同名表格——「1、货币资金」
    下面那张明细表的行名就叫「货币资金」——dict 后写胜出，资产负债表被整段
    盖掉。青岛啤酒就是这样：货币资金从 115.6 亿变成 7.9 亿，应付账款从
    41.5 亿变成 218,946，而每个科目都有值，看不出任何异常。
    """

    def test_total_row_ends_the_statement(self):
        rows = [
            _row(800, ("合并资产负债表", 60)),
            _row(778, ("资产总计", 60), ("100,000,000.00", 330)),
            _row(756, ("负债和所有者权益总计", 60), ("100,000,000.00", 330)),
            _row(734, ("七、合并财务报表项目注释", 60)),
            _row(712, ("1、货币资金", 60)),
            _row(690, ("项目", 60), ("期末余额", 330)),
            _row(668, ("货币资金", 60), ("9,999.00", 330)),
        ]
        start, end = sem.find_statement(rows, "合并资产负债表")
        self.assertEqual(start, 0)
        self.assertEqual(end, 3, "应当收在「负债和所有者权益总计」那一行之后")
        bs = sem.parse_balance_sheet(rows, "合并资产负债表")
        self.assertAlmostEqual(bs["资产总计"]["current"], 100_000_000.00, places=2)
        self.assertNotIn("货币资金", bs, "附注里的同名行不能被当成报表科目")


class TestColumnHeaderAnchor(unittest.TestCase):
    """标题行缺席时靠**列头**定位资产负债表。

    招商银行（600036）的合并资产负债表没有标题行：标题只出现在目录和审计报告
    正文里，表本身从「项目 附注 6月30日 12月31日」直接开始。只认标题的定位方式
    对它一律返回「找不到报表」，而那张表一个字都不缺。锚点必须**两头都咬得住**
    ——单侧命中会认下利润表、现金流量表（列头长得一模一样）或者翻页重印的续页
    列头，而认错的代价是把整段资产切在区间外。
    """

    HEADER = (("项目", 57.0), ("附注", 364.0), ("6月30日", 427.0), ("12月31日", 492.0))

    def test_anchor_locates_the_statement_without_a_title(self):
        rows = [
            _row(800, *self.HEADER),
            _row(778, ("货币资金", 57.0), ("八、1", 364.0), ("1,000", 427.0)),
            _row(756, ("资产合计", 57.0), ("2,000", 427.0)),
            _row(734, ("短期借款", 57.0), ("500", 427.0)),
            _row(712, ("负债及股东权益总计", 57.0), ("2,000", 427.0)),
        ]
        start, end = sem.find_statement(rows, "合并资产负债表")
        self.assertEqual(start, 0, "锚点应当是列头那一行")
        self.assertEqual(end, 5)
        bs = sem.parse_balance_sheet(rows, "合并资产负债表")
        self.assertEqual(sem._asset_total_label(bs), "资产合计")
        self.assertAlmostEqual(bs["资产合计"]["current"], 2_000.0, places=2)
        self.assertIn("货币资金", bs)
        self.assertIn("短期借款", bs)

    def test_page_break_header_does_not_steal_the_anchor(self):
        """翻页后重印的列头不是表头——它后面没有资产合计行。

        招行的报表横跨 83~84 两页，84 页顶上又印了一遍同样的列头，但资产段在
        前一页就收完了。认下它，区间从负债段开始，资产总计和货币资金整个被切在
        区间外——而负债合计照样读得出来，看数字看不出少了什么。
        """
        rows = [
            _row(800, *self.HEADER),
            _row(778, ("货币资金", 57.0), ("1,000", 427.0)),
            _row(756, ("资产合计", 57.0), ("2,000", 427.0)),
            _row(734, *self.HEADER),                      # 续页列头
            _row(712, ("短期借款", 57.0), ("500", 427.0)),
            _row(690, ("负债及股东权益总计", 57.0), ("2,000", 427.0)),
        ]
        start, _end = sem.find_statement(rows, "合并资产负债表")
        self.assertEqual(start, 0, "第一条命中的锚点是正页那张表")
        bs = sem.parse_balance_sheet(rows, "合并资产负债表")
        self.assertIn("货币资金", bs, "资产段不能被留在区间外")
        self.assertAlmostEqual(bs["短期借款"]["current"], 500.0, places=2)

    def test_anchor_must_close_on_a_total_row(self):
        """收不到「负债及股东权益总计」就不认这张表。

        利润表、现金流量表、股东权益变动表的列头跟资产负债表一模一样，唯一的
        区别是它们没有这条收尾行。收不住尾还认下来，区间会一路吃到文件末尾。
        """
        rows = [
            _row(800, *self.HEADER),
            _row(778, ("销售商品、提供劳务收到的现金", 57.0), ("1,000", 427.0)),
            _row(756, ("经营活动现金流入小计", 57.0), ("1,000", 427.0)),
        ]
        self.assertEqual(sem.find_statement(rows, "合并资产负债表"), (None, None))

    def test_anchor_needs_a_real_asset_total_row(self):
        """区间里没有**整格**的「资产总计 / 资产合计」就不认。

        「流动资产合计」不是资产合计——按子串找会先撞上它，资产段在那一行收尾，
        后面的资产全变成负债。这一条与 :meth:`test_heji_must_match_the_whole_cell`
        钉的是同一件事，只是从锚点这一侧看。
        """
        for label in ("流动资产合计", "非流动资产合计", "负债合计"):
            rows = [
                _row(800, *self.HEADER),
                _row(778, (label, 57.0), ("1,000", 427.0)),
                _row(756, ("负债及股东权益总计", 57.0), ("1,000", 427.0)),
            ]
            self.assertEqual(sem.find_statement(rows, "合并资产负债表"),
                             (None, None), label)

    def test_header_without_a_notes_column_is_not_the_statement(self):
        """列头必须带附注列。

        **这一条在手上 24 份文档里没有目击证人**：所有 4 格列头都带附注列，放宽
        这条判据跑出来的结果逐字节相同。它防的是另一种形状——主要财务指标表的
        「项目 | 2026年半年度 | 2025年半年度 | 增减变动」，那张表里也有「资产
        总计」这一行，而它排在资产负债表**之前**，一旦被认成锚点，区间会从指标表
        一路拉到报表，读出来的东西两边都沾一点。
        """
        rows = [
            _row(800, ("项目", 57.0), ("2026年半年度", 300.0),
                 ("2025年半年度", 400.0), ("增减变动", 500.0)),
            _row(778, ("资产总计", 57.0), ("2,000", 300.0), ("1,000", 400.0),
                 ("100%", 500.0)),
            _row(756, ("负债及股东权益总计", 57.0), ("2,000", 300.0)),
        ]
        self.assertEqual(sem.find_statement(rows, "合并资产负债表"), (None, None))

    def test_title_wins_over_the_anchor(self):
        """有标题就不看列头——二十多份别的报告因此逐字节不变。

        断言的是**标题那条**的位置：用列头锚点会定位到后面那张表上去，读出来的
        是另一组数字（9,999）。
        """
        rows = [
            _row(800, ("合并资产负债表", 60.0)),
            _row(778, ("资产总计", 60.0), ("2,000", 380.0)),
            _row(756, ("负债和所有者权益总计", 60.0), ("2,000", 380.0)),
            _row(734, *self.HEADER),
            _row(712, ("资产总计", 57.0), ("9,999", 427.0)),
            _row(690, ("负债和所有者权益总计", 57.0), ("9,999", 427.0)),
        ]
        start, end = sem.find_statement(rows, "合并资产负债表")
        self.assertEqual((start, end), (0, 3))
        bs = sem.parse_balance_sheet(rows, "合并资产负债表")
        self.assertAlmostEqual(bs["资产总计"]["current"], 2_000.0, places=2)

    def test_anchor_is_not_offered_for_the_parent_statement(self):
        """母公司口径没有可靠顺序，宁可返回「找不到」也不猜。"""
        rows = [
            _row(800, *self.HEADER),
            _row(778, ("资产合计", 57.0), ("2,000", 427.0)),
            _row(756, ("负债及股东权益总计", 57.0), ("2,000", 427.0)),
        ]
        self.assertEqual(sem.find_statement(rows, "母公司资产负债表"), (None, None))


class TestNoteAreaHeading(unittest.TestCase):
    """附注区标题的变体。序号是排版噪音，不变量只有「合并财务报表…项目注释」。"""

    def test_numbered_and_bracketed_variants(self):
        for text in ("七、合并财务报表项目注释", "（五）合并财务报表项目注释",
                     "(五)合并财务报表项目注释", "合并财务报表主要项目注释"):
            rows = [_row(800, (text, 60)),
                    _row(778, ("1、货币资金", 60)),
                    _row(756, ("项目", 60), ("期末余额", 330)),
                    _row(734, ("银行存款", 60), ("1,000.00", 330)),
                    _row(712, ("合计", 60), ("1,000.00", 330))]
            sections = sem.find_note_sections(rows)
            self.assertIn(1, sections, text)
            self.assertEqual(sections[1]["name"], "货币资金", text)

    def test_toc_line_is_not_the_note_area(self):
        """目录里也印着同样一行字，后面跟着页码。认成附注区起点，整段附注
        就被当成目录跳过——静默地一条都读不到。"""
        rows = [
            _row(800, ("七、合并财务报表项目注释", 60), ("45", 500)),
            _row(778, ("七、合并财务报表项目注释", 60)),
            _row(756, ("1、货币资金", 60)),
            _row(734, ("项目", 60), ("期末余额", 330)),
            _row(712, ("银行存款", 60), ("1,000.00", 330)),
            _row(690, ("合计", 60), ("1,000.00", 330)),
        ]
        self.assertFalse(sem._is_toc_line("七、合并财务报表项目注释"))
        sections = sem.find_note_sections(rows)
        self.assertIn(1, sections)

    def test_parent_note_area_ends_the_scan(self):
        """母公司附注区一开始，合并附注区就该收尾，否则最后一条会吞下整段。"""
        rows = [
            _row(800, ("七、合并财务报表项目注释", 60)),
            _row(778, ("1、货币资金", 60)),
            _row(756, ("项目", 60), ("期末余额", 330)),
            _row(734, ("银行存款", 60), ("1,000.00", 330)),
            _row(712, ("合计", 60), ("1,000.00", 330)),
            _row(690, ("母公司财务报表主要项目注释", 60)),
            _row(668, ("1、货币资金", 60)),
            _row(646, ("银行存款", 60), ("999,999.00", 330)),
        ]
        sections = sem.find_note_sections(rows)
        self.assertEqual(sections[1]["end"], 5)


class TestSubLineIndentation(unittest.TestCase):
    """缩进写出来的下级行不能当成独立科目再加一遍。

    华域汽车：其他应收款 31.5 亿已经含了应收股利 5.94 亿，而应收股利又被
    当成一个独立的资产科目加了一遍——资产明细合计比资产总计多出 5.94 亿。
    这笔凭空多出来的资产不会报错，只会让分子悄悄变大。
    """

    def test_indented_row_is_marked_sub(self):
        bs = sem.parse_balance_sheet([
            _row(800, ("合并资产负债表", 60)),
            _row(778, ("其他应收款", 79.9), ("3,145,914,097.93", 336.5)),
            _row(756, ("其中：应收利息", 79.9)),
            _row(734, ("应收股利", 111.5), ("594,410,936.16", 346.9)),
            _row(712, ("资产总计", 79.9), ("197,377,630,675.08", 325.9)),
            _row(690, ("负债和所有者权益总计", 79.9), ("197,377,630,675.08", 325.9)),
        ], "合并资产负债表")
        self.assertTrue(bs["应收股利"]["sub"], "缩进的应收股利是下级行")
        self.assertFalse(bs["其他应收款"]["sub"], "同级科目不是下级行")
        walked = [a for a, _, side in sem._walk_sides(bs) if side == "asset"]
        self.assertIn("其他应收款", walked)
        self.assertNotIn("应收股利", walked)


class TestGroupHeaderDrop(unittest.TestCase):
    """「金融投资 ：」这类分组表头：金额等于下面几个子项之和时，丢掉表头。

    招商银行的资产负债表就是这样：表头「金融投资 ：」4,379,405（百万元）正好
    等于四个子项之和，而五个行在版面上 x 完全相同（缩进写成名字开头的空格），
    :func:`_mark_sub_lines` 标不出来，五个都成了独立科目。今天五个都没分类所以
    钱没错；给子项加上规则之后（贷款、金融投资这些正是这次要加的），表头不丢
    就会把同一笔 4.38 万亿算两遍。
    """

    @staticmethod
    def _bs(*rows):
        """造一份 ``parse_balance_sheet`` 的返回值形状：``名字 → info``。"""
        return {name: {"current": amount, "x": x} for name, amount, x in rows}

    def test_header_is_dropped_when_children_sum_to_it(self):
        bs = self._bs(("金融投资 ：", 100.0, 56.0),
                      ("以摊余成本计量的债务工具投资", 60.0, 56.0),
                      ("以公允价值计量且其变动计入当期损益的金融投资", 40.0, 56.0),
                      ("固定资产", 7.0, 40.0))
        out = sem._drop_group_headers(bs)
        self.assertNotIn("金融投资 ：", out)
        self.assertIn("以摊余成本计量的债务工具投资", out)
        self.assertIn("以公允价值计量且其变动计入当期损益的金融投资", out)
        self.assertEqual(len(out), 3)

    def test_header_stays_when_the_sum_does_not_match(self):
        """和不等就**不丢**：要么子项没解析全，要么表头本身就是一笔独立资产，
        两种都不能由这里替它做主——宁可留着，也不静默丢钱。"""
        bs = self._bs(("金融投资 ：", 100.0, 56.0), ("甲", 60.0, 56.0))
        self.assertIn("金融投资 ：", sem._drop_group_headers(bs))

    def test_header_stays_when_nothing_follows(self):
        bs = self._bs(("金融投资 ：", 100.0, 56.0))
        self.assertIn("金融投资 ：", sem._drop_group_headers(bs))

    def test_one_child_is_not_enough(self):
        """只有一条子项且金额相等，更像是同一行被印了两遍，不丢。"""
        bs = self._bs(("金融投资 ：", 100.0, 56.0), ("甲", 100.0, 56.0))
        self.assertIn("金融投资 ：", sem._drop_group_headers(bs))

    def test_subtotal_row_stops_the_lookahead(self):
        """往前看的时候遇到「小计」就收手——它的钱已经含在上面了。"""
        bs = self._bs(("金融投资 ：", 100.0, 56.0), ("甲", 50.0, 56.0),
                      ("小计", 50.0, 56.0), ("乙", 50.0, 56.0))
        self.assertIn("金融投资 ：", sem._drop_group_headers(bs))

    def test_colon_at_the_end_is_required(self):
        """不带冒号的同名行是正常科目。"""
        bs = self._bs(("金融投资", 100.0, 56.0), ("甲", 60.0, 56.0),
                      ("乙", 40.0, 56.0))
        self.assertIn("金融投资", sem._drop_group_headers(bs))

    def test_no_header_means_the_same_object_comes_back(self):
        """没有表头可丢时必须原样返回**同一个对象**：其余 20 只股票的
        「逐字节不变」就押在这条上。"""
        bs = self._bs(("固定资产", 100.0, 40.0))
        self.assertIs(sem._drop_group_headers(bs), bs)

    def test_end_to_end_no_double_count(self):
        """走 ``parse_balance_sheet`` 的完整路径：表头消失、子项齐全、
        资产明细合计不被撑大。"""
        bs = sem.parse_balance_sheet([
            _row(800, ("合并资产负债表", 60)),
            _row(778, ("金融投资 ：", 56), ("300.00", 330)),
            _row(756, ("以摊余成本计量的债务工具投资", 56), ("100.00", 330)),
            _row(734, ("以公允价值计量且其变动计入当期损益的金融投资", 56),
                 ("150.00", 330)),
            _row(712, ("指定为以公允价值计量且其变动计入其他综合收益的权益工具投资",
                       56), ("50.00", 330)),
            _row(690, ("固定资产", 40), ("300.00", 330)),
            _row(668, ("资产总计", 40), ("600.00", 330)),
            _row(646, ("负债和所有者权益总计", 40), ("600.00", 330)),
        ], "合并资产负债表")
        self.assertNotIn("金融投资 ：", bs)
        walked = {a: info["current"] for a, info, side in sem._walk_sides(bs)
                  if side == "asset"}
        self.assertEqual(sum(walked.values()), 600.0, walked)
        self.assertEqual(walked["固定资产"], 300.0)
        self.assertEqual(walked["以摊余成本计量的债务工具投资"], 100.0)
        # 子项各自分类正确，表头那一笔没有以任何形式重复出现
        self.assertEqual(sem.classify("以摊余成本计量的债务工具投资")[0],
                         sem.LOW_RISK_FINANCIAL_ASSET)
        self.assertEqual(
            sem.classify("指定为以公允价值计量且其变动计入其他综合收益的权益工具投资")[0],
            sem.MARKETABLE_SECURITY)


class TestParseNoteItems(unittest.TestCase):
    def test_only_the_first_table_is_read(self):
        """一条附注下面往往并排放着好几张表，列义各不相同。读到「合计」就收手。"""
        rows = [
            _row(800, ("5、其他流动资产", 60)),
            _row(778, ("项目", 60), ("期末余额", 330), ("期初余额", 450)),
            _row(756, ("可转让大额存单", 60), ("700,000,000.00", 330)),
            _row(734, ("合计", 60), ("700,000,000.00", 330)),
            _row(712, ("（1）账龄分析如下：", 60)),
            _row(690, ("项目", 60), ("期末余额", 330), ("期初余额", 450)),
            _row(668, ("一年以内", 60), ("123,456.00", 330)),
        ]
        section = {"start": 0, "end": len(rows), "name": "其他流动资产"}
        items = sem.parse_note_items(rows, section)
        self.assertEqual([i["name"] for i in items], ["可转让大额存单"])


# --------------------------------------------------------------------------- #
# 6. LLM 兜底
# --------------------------------------------------------------------------- #
class _Item:
    """够用的假资产项。"""

    def __init__(self, name, amount, account="其他流动资产"):
        self.account = account
        self.sub_item = name
        self.amount = amount
        self.economic_class = sem.OTHER_UNKNOWN
        self.source_text = f"{account} | {name} | {amount}"
        self.page = 1


class TestLLMGate(unittest.TestCase):
    def _classifier(self):
        cfg = {"api_key": "test-key-not-real", "base_url": "http://x",
               "model": "m", "is_deepseek": True}
        return llm_classify.LLMClassifier(config=cfg)

    def test_no_key_means_unavailable(self):
        """没有 Key 不是错误，是常态。主流程必须照常走完。

        这里传一份**没有** api_key 的显式配置，而不是 ``config=None``：
        ``None`` 的语义是「自己去环境里找」，在配好了 Key 的机器上会真的找到，
        测试就变成了看天吃饭。
        """
        c = llm_classify.LLMClassifier(config={"api_key": None})
        self.assertFalse(c.available)
        self.assertIsNone(c.classify(_Item("其他", 1e9)))

    def test_load_config_without_key_returns_none(self):
        self.assertIsNone(llm_classify.load_config({}))

    def test_config_prefers_deepseek_over_anthropic(self):
        cfg = llm_classify.load_config({
            "DEEPSEEK_API_KEY": "d", "ANTHROPIC_AUTH_TOKEN": "a",
            "DEEPSEEK_MODEL": "dm", "ANTHROPIC_MODEL": "am",
            "DEEPSEEK_BASE_URL": "http://d",
        })
        self.assertEqual(cfg["api_key"], "d")
        self.assertEqual(cfg["model"], "dm")
        self.assertTrue(cfg["is_deepseek"])

    def test_config_reads_anthropic_as_fallback(self):
        cfg = llm_classify.load_config({"ANTHROPIC_AUTH_TOKEN": "a"})
        self.assertEqual(cfg["api_key"], "a")
        self.assertFalse(cfg["is_deepseek"])

    def test_small_amounts_are_not_worth_a_call(self):
        """金额门槛是这一层最重要的节流阀。问一个不改变结论的小科目，
        只是白白换来一份不确定性。"""
        c = self._classifier()
        tiny = _Item("其他", 1_000_000.0)
        self.assertFalse(c.gate(tiny, total_assets=1e10, market_cap=1e9))
        self.assertGreater(c.skipped, 0)

    def test_large_amounts_pass_the_gate(self):
        c = self._classifier()
        big = _Item("其他", 5e8)
        self.assertTrue(c.gate(big, total_assets=1e10, market_cap=1e9))

    def test_gate_ignores_already_classified_items(self):
        """明确命中的项目禁止调用 LLM（§6）。"""
        c = self._classifier()
        item = _Item("可转让大额存单", 5e8)
        item.economic_class = sem.NEGOTIABLE_CD
        self.assertFalse(c.gate(item, total_assets=1e10, market_cap=1e9))

    def test_call_count_is_capped(self):
        """调用次数必须封顶，否则一份报告能把 API 刷爆。"""
        c = self._classifier()
        called = []

        def fake(item):
            called.append(item)
            return {"economic_class": sem.OTHER_KNOWN, "restricted": False,
                    "confidence": 0.5, "evidence": "x"}

        c._request = fake
        for i in range(llm_classify.LLM_MAX_CALLS_PER_REPORT + 4):
            c.classify(_Item(f"其他{i}", 5e8), document_hash="d", paragraph_hash=f"p{i}")
        self.assertEqual(len(called), llm_classify.LLM_MAX_CALLS_PER_REPORT)

    def test_same_paragraph_is_cached(self):
        """缓存键 = 文档哈希 + 段落哈希 + 分类器版本 + 模型名。
        同一段附注重复问没有意义，只是重复花钱。"""
        c = self._classifier()
        calls = []

        def fake(item):
            calls.append(item)
            return {"economic_class": sem.OTHER_KNOWN, "restricted": False,
                    "confidence": 0.5, "evidence": "x"}

        c._request = fake
        item = _Item("其他", 5e8)
        for _ in range(3):
            c.classify(item, document_hash="doc1", paragraph_hash="p1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(c.cache_hits, 2)

    def test_api_failure_degrades_quietly(self):
        """API 挂了不能拖垮主流程，也不能降级成猜测。"""
        c = self._classifier()

        def boom(item):
            raise OSError("connection reset")

        c._request = boom
        self.assertIsNone(c.classify(_Item("其他", 5e8)))
        self.assertEqual(c.failures, 1)

    def test_key_never_leaks_into_error_text(self):
        """Key 不许进日志。异常文本里带出来是最常见的泄漏路径。"""
        c = self._classifier()
        secret = c.config["api_key"]

        def boom(item):
            raise OSError(f"auth failed for {secret}")

        c._request = boom
        c.classify(_Item("其他", 5e8))
        self.assertNotIn(secret, c.last_error)
        self.assertIn("***", c.last_error)

    def test_out_of_enum_answer_is_discarded(self):
        """LLM 越界输出一律作废，不许它自己发明类别。"""
        v = llm_classify.LLMClassifier._parse_verdict(
            '{"economic_class": "SUPER_CASH", "confidence": 0.9}')
        self.assertIsNone(v)
        v = llm_classify.LLMClassifier._parse_verdict(
            '{"economic_class": "OTHER_UNKNOWN", "confidence": 0.9}')
        self.assertIsNone(v, "说不清就等于没说")

    def test_verdict_shape_is_closed(self):
        """LLM 的输出被裁成固定几个字段——它不能改金额、不能定折价。"""
        v = llm_classify.LLMClassifier._parse_verdict(
            '{"economic_class": "TERM_DEPOSIT", "restricted": false,'
            ' "liquidity": "HIGH", "confidence": 0.8, "evidence": "定期存款",'
            ' "amount": 999, "haircut": 0.1, "score": 100}')
        self.assertEqual(set(v), {"economic_class", "restricted", "liquidity",
                                  "confidence", "evidence"})
        self.assertNotIn("amount", v)
        self.assertNotIn("haircut", v)
        self.assertNotIn("score", v)


class _FakeHTTP:
    """把 ``urlopen`` 换成一个返回固定响应的假上下文管理器。

    测的是「拿到响应之后怎么解读」，所以不必真发请求；同时让响应体完全可控
    ——thinking 块、截断、空 content 这些形态都要能复现。
    """

    def __init__(self, payload):
        self.payload = payload

    def __call__(self, req, timeout=None):
        raw = json.dumps(self.payload).encode("utf-8")
        outer = self

        class _Resp:
            def read(self):
                return raw

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return _Resp()


class TestGateRequiresNoteText(unittest.TestCase):
    """没有附注原文就不问。

    §7 给模型的职责是「读附注做语义分类」。上下文为空时它手上只剩一个科目名，
    于是要么回 ``OTHER_UNKNOWN``（被作废，调用白花），要么**照着科目名编一句
    附注**——现役快照里华域「其他流动资产」的 evidence 写着「一年内到期的发放
    贷款及垫款」，而华域是汽车零部件公司，根本没有这个科目。同一份空输入在
    历史上既回过 ``RECEIVABLE_NORMAL`` 也回过 ``OTHER_UNKNOWN``。这种判定
    不可复现，也不该进快照。
    """

    def _classifier(self):
        return llm_classify.LLMClassifier(config={
            "api_key": "test-key-not-real", "base_url": "http://x",
            "model": "m", "is_deepseek": True})

    def test_item_without_source_text_is_not_asked(self):
        c = self._classifier()
        item = _Item("其他流动资产", 5e8)
        item.source_text = None
        self.assertFalse(c.gate(item, total_assets=1e10, market_cap=1e9))
        self.assertGreater(c.skipped, 0)

    def test_whitespace_only_source_text_is_not_asked(self):
        """空串和「只有空格/换行」是同一件事，别让后者漏过去。"""
        c = self._classifier()
        item = _Item("其他流动资产", 5e8)
        item.source_text = "   \n\t "
        self.assertFalse(c.gate(item, total_assets=1e10, market_cap=1e9))

    def test_item_with_source_text_still_passes(self):
        """有原文的照旧不问白不问——闸门只挡掉没有输入的那一类。"""
        c = self._classifier()
        self.assertTrue(c.gate(_Item("其他流动资产", 5e8),
                               total_assets=1e10, market_cap=1e9))


class TestTruncatedResponseIsNotSilence(unittest.TestCase):
    """截断 / 空响应必须与「模型没给出结论」区分开。

    这个端点先吐 thinking 块再吐答案，thinking 也算进 ``max_tokens``。设 300 时
    实测 3 次里 1 次预算在思考阶段就烧完，整条回复只剩 thinking 块、没有 text
    ——返回 ``None``、``failures`` 仍是 0，和「问了个答不出来的问题」在代码上
    完全同形。于是真正的原因静默消失，而「判定为什么不可复现」正是要查这个。
    """

    def _classifier(self):
        return llm_classify.LLMClassifier(config={
            "api_key": "test-key-not-real", "base_url": "http://x",
            "model": "m", "is_deepseek": True})

    def _run_with(self, payload):
        c = self._classifier()
        with mock.patch.object(llm_classify.urllib.request, "urlopen",
                               _FakeHTTP(payload)):
            verdict = c.classify(_Item("其他流动资产", 5e8), "doc", "para")
        return c, verdict

    def test_budget_exhausted_in_the_thinking_block_is_counted(self):
        c, v = self._run_with({
            "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "嗯……"}],
        })
        self.assertIsNone(v)
        self.assertEqual(c.calls, 1)
        self.assertEqual(c.truncated, 1)
        self.assertEqual(c.empty, 0)
        self.assertEqual(c.failures, 0, "截断不是「调用失败」，是另一回事")
        self.assertIn("max_tokens", c.last_error)

    def test_end_turn_without_any_text_block_is_counted_apart(self):
        """模型自己收尾了却没给 text 块——这不是截断，但仍不是「拒答」。"""
        c, v = self._run_with({"stop_reason": "end_turn", "content": []})
        self.assertIsNone(v)
        self.assertEqual(c.truncated, 0)
        self.assertEqual(c.empty, 1)

    def test_a_real_verdict_still_lands(self):
        """对照：正常的 think + text 两段式必须照旧解析出来。"""
        c, v = self._run_with({
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "同业存单属于货币市场工具"},
                {"type": "text", "text": '{"economic_class": "NEGOTIABLE_CD",'
                 ' "restricted": false, "liquidity": "HIGH", "confidence": 0.9,'
                 ' "evidence": "同业存单"}'},
            ],
        })
        self.assertEqual(v["economic_class"], sem.NEGOTIABLE_CD)
        self.assertEqual((c.truncated, c.empty, c.failures), (0, 0, 0))

    def test_the_text_budget_clears_the_thinking_block(self):
        """输出上限必须容得下「一个 thinking 块 + 一段判定 JSON」。

        实测判定本身约 150 字、加思考约 200–250 token，300 会周期性截断。
        这里钉住的是一个**量级**，不是某个具体数字：调小到 300 那一档就
        重新开始丢判定。
        """
        self.assertGreaterEqual(llm_classify.MAX_TOKENS, 600)
        for is_deepseek in (True, False):
            body = llm_classify.LLMClassifier(config={
                "api_key": "k", "base_url": "http://x", "model": "m",
                "is_deepseek": is_deepseek})._payload("prompt")
            self.assertEqual(body["max_tokens"], llm_classify.MAX_TOKENS)
            self.assertEqual(body["temperature"], 0, "分类任务不需要发挥")


class TestLLMResolution(unittest.TestCase):
    def test_unknown_is_left_alone_without_classifier(self):
        """不传分类器就纯规则跑，UNKNOWN 原样留着。"""
        view = sem.build_economic_view(_rows())
        unknowns = [it for it in view["items"]
                    if it.economic_class == sem.OTHER_UNKNOWN]
        self.assertTrue(unknowns)
        for it in unknowns:
            self.assertEqual(it.method, "unresolved")
            self.assertFalse(it.llm)

    def test_missing_key_does_not_change_results(self):
        """没有 Key 时跑出来的结果，与纯规则跑出来的完全一致。"""
        rows = _rows()
        plain = sem.build_economic_view(rows)
        c = llm_classify.LLMClassifier(config=None)
        with_clf = sem.build_economic_view(
            rows, llm_hook=c.classify, llm_gate=c.gate)
        self.assertEqual([i.to_dict() for i in plain["items"]],
                         [i.to_dict() for i in with_clf["items"]])

    def test_llm_can_only_fill_unknowns(self):
        """LLM 只能补 UNKNOWN，不能覆盖规则已经命中的项。"""
        rows = _rows()
        base = sem.build_economic_view(rows)
        rule_hits = {(i.account, i.sub_item): i.economic_class
                     for i in base["items"] if i.method == "rule"}

        c = llm_classify.LLMClassifier(config=None)

        def always_known(item):
            return {"economic_class": sem.NEGOTIABLE_CD, "restricted": False,
                    "confidence": 0.9, "evidence": "always"}

        after = sem.build_economic_view(rows, llm_hook=always_known,
                                        llm_gate=lambda i, t: True)
        for it in after["items"]:
            key = (it.account, it.sub_item)
            if key in rule_hits:
                self.assertEqual(it.economic_class, rule_hits[key], key)
                self.assertFalse(it.llm, key)


def _bs_rows(*, unit=None, total_label="资产总计", extra_after_total=(),
             name_col=60.0, num_col=380.0):
    """造一张最小可解析的资产负债表 + 一条附注。

    附注「1、其他流动资产」的明细合计与科目余额相等，所以会被真正展开——这样
    「附注与报表是否同一量纲」才有东西可验（对不上时会静默退回整笔计价）。
    """
    rows = [_row(800, ("合并资产负债表", name_col))]
    if unit is not None:
        rows.append(_row(778, ("编制单位：样本股份有限公司", name_col),
                         (unit, num_col)))
    rows += [
        _row(756, ("项目", name_col), ("期末余额", num_col)),
        _row(734, ("其他流动资产", name_col), ("七、1", 250.0),
             ("2,000", num_col)),
    ] + list(extra_after_total) + [
        _row(712, (total_label, name_col), ("2,000", num_col)),
        _row(690, ("负债和所有者权益总计", name_col), ("2,000", num_col)),
        _row(668, ("七、合并财务报表项目注释", name_col)),
        _row(646, ("1、其他流动资产", name_col)),
        _row(624, ("项目", name_col), ("期末余额", num_col)),
        _row(602, ("定期存款", name_col), ("2,000", num_col)),
        _row(580, ("合计", name_col), ("2,000", num_col)),
    ]
    return rows


class TestAmountUnit(unittest.TestCase):
    """报表声明的金额单位换算。

    这一层以前完全没有单位概念：报表写着「单位：千元」，金额就被原样当成元
    存进快照，而下游所有比率都要和市值（元）比。中国建筑 2026Q1 是千元、
    招商银行 2026Q1 是百万元——不换算的话招行总资产会被当成 1348 万元，
    而真实是 13.48 万亿元，**错 10⁶ 倍且看起来完全正常**。
    """

    def test_thousand_yuan_is_scaled(self):
        rows = _bs_rows(unit="单位：千元 币种：人民币")
        view = sem.build_economic_view(rows)
        self.assertEqual(view["amount_unit"], "千元")
        self.assertEqual(view["total_assets"], 2_000_000.0)

    def test_million_declaration_in_prose_form(self):
        """叙述式声明：「（除特别注明外，货币单位均以人民币百万元列示）」。

        招商银行就是这一种——它不带「单位：」前缀，只认那一种写法的实现会漏掉
        它，而漏掉的表现是数值小 10⁶ 倍，不是报错。
        """
        rows = _bs_rows(unit="（除特别注明外，货币单位均以人民币百万元列示）")
        view = sem.build_economic_view(rows)
        self.assertEqual(view["amount_unit"], "百万元")
        self.assertEqual(view["total_assets"], 2_000_000_000.0)

    def test_yuan_declaration_is_not_a_multiplier(self):
        """「单位：元」不是倍数声明——它必须被识别成 1 倍，而不是「万元」之类。"""
        rows = _bs_rows(unit="单位：元 币种：人民币")
        view = sem.build_economic_view(rows)
        self.assertIsNone(view["amount_unit"])
        self.assertEqual(view["amount_scale"], 1.0)
        self.assertEqual(view["total_assets"], 2_000.0)

    def test_no_declaration_means_yuan(self):
        """没有声明就没有换算——缺省行为与加这段逻辑之前逐字节相同。"""
        view = sem.build_economic_view(_bs_rows())
        self.assertIsNone(view["amount_unit"])
        self.assertEqual(view["total_assets"], 2_000.0)

    def test_unit_declaration_outside_the_header_band_is_ignored(self):
        """区间**深处**出现「以下金额以人民币千元列示」不算声明。

        只在报表标题下那一小块里找声明。全文搜的话，附注正文里到处都是
        「以人民币千元列示」这类句子，搜到一次整张表就错位 1000 倍。

        这句话必须垫到 :data:`sem._UNIT_BAND` **之外**：带内的话它本来就该被
        认成声明，那就验不出「只搜开头一小块」了。
        """
        rows = _bs_rows(unit="单位：元")
        while len(rows) <= sem._UNIT_BAND + 1:
            rows.append(_row(560 - 22 * len(rows), ("附注说明", 60.0)))
        rows.append(_row(500, ("以下金额以人民币千元列示", 60.0)))
        self.assertGreater(len(rows) - 1, sem._UNIT_BAND)
        view = sem.build_economic_view(rows)
        self.assertIsNone(view["amount_unit"])
        self.assertEqual(view["total_assets"], 2_000.0)

    def test_notes_are_converted_with_the_same_multiplier(self):
        """附注明细与报表必须同一量纲——否则明细会静默退回整笔计价。

        两边倍数不一致时，``明细合计 == 科目余额`` 判负，所有明细都不再展开。
        它不报错、不掉分，只是把「拆开看」的粒度悄悄丢掉，所以专门钉住这一点。
        """
        view = sem.build_economic_view(_bs_rows(unit="单位：千元"))
        subs = [i for i in view["items"] if i.account == "其他流动资产"
                and i.sub_item != "其他流动资产"]
        self.assertEqual([i.sub_item for i in subs], ["定期存款"])
        self.assertEqual(subs[0].amount, 2_000_000.0)


class TestCaptionUnits(unittest.TestCase):
    """全文档的「表首括号式」单位说明——**候选，不是结论**。

    银行财报的正文表格每张都标着「（人民币百万元，百分比除外）」，而资产负债表
    自己一个字都不写。这份候选是唯一能用的线索；但同一份报告里不同表格的量纲可以
    不一样（三棵树正文标千元、报表本身是元），所以谁出现得多只决定**试的顺序**，
    采纳与否由 ``asset_engine.resolve_scale`` 拿市值去量。
    """

    def test_parenthesised_captions_are_counted_and_ranked(self):
        rows = [
            _row(800, ("（人民币百万元，百分比除外）", 60.0)),
            _row(778, ("（人民币百万元，百分比除外）", 60.0)),
            _row(756, ("（金额单位：千元）", 60.0)),
        ]
        self.assertEqual(sem.caption_units(rows), [("百万元", 1e6), ("千元", 1000.0)])

    def test_the_longer_unit_name_wins(self):
        """「万元」是「百万元」的子串——先判短的会把 10⁶ 读成 10⁴。"""
        self.assertEqual(sem.caption_units([_row(800, ("（金额单位：百万元）", 60.0))]),
                         [("百万元", 1e6)])
        self.assertEqual(sem.caption_units([_row(800, ("（金额单位：万元）", 60.0))]),
                         [("万元", 1e4)])

    def test_caption_must_occupy_the_whole_cell(self):
        """整格匹配：**说明独占一格**，句子里带一个括号说明不算。

        子串匹配会把「近三年（人民币百万元）复合增长率」这类句子算进来——那是
        正文的一句话，不是某张表的量纲。
        """
        for text in ("近三年（人民币百万元）复合增长率为 12%",
                     "（金额单位：千元）币种：人民币",
                     "附注（人民币万元）"):
            self.assertEqual(sem.caption_units([_row(800, (text, 60.0))]), [], text)


class TestAssetTotalDialect(unittest.TestCase):
    """资产段合计行的名字按报表自己判定。

    招商银行（600036）的资产段合计叫「资产合计」，少了「总」字。认不出它不只是
    少一个数：段切分收不了尾，**整段负债会被当成资产累加**。
    """

    def test_asset_heji_is_recognised(self):
        rows = _bs_rows(total_label="资产合计")
        view = sem.build_economic_view(rows)
        self.assertEqual(view["total_assets"], 2_000.0)

    def test_heji_must_match_the_whole_cell(self):
        """「流动资产合计」不许被当成资产合计。

        按子串找「资产合计」会先撞上「流动资产合计」，资产段在那一行就收尾，
        后面的资产全变成负债。这是**最贵**的一种错法，所以整格相等。
        """
        rows = [
            _row(800, ("合并资产负债表", 60.0)),
            _row(778, ("流动资产合计", 60.0), ("1,000", 380.0)),
            _row(756, ("长期股权投资", 60.0), ("1,000", 380.0)),
            _row(734, ("资产合计", 60.0), ("2,000", 380.0)),
            _row(712, ("负债和所有者权益总计", 60.0), ("2,000", 380.0)),
        ]
        bs = sem.parse_balance_sheet(rows, "合并资产负债表")
        self.assertEqual(sem._asset_total_label(bs), "资产合计")
        sides = {a: side for a, _, side in sem._walk_sides(bs)}
        self.assertEqual(sides.get("长期股权投资"), "asset")
        self.assertEqual(sem._asset_total(bs), 2_000.0)


class TestAmountRatioGate(unittest.TestCase):
    """候选倍数必须让「资产总计 ÷ 市值」落进量级带。

    这是全流程里唯一能发现「量纲读错」的地方：读错的表现是**数字长得完全正常**
    ——13,785,280 是元还是百万元都读得通，只有和市值比才看得出差 10⁶。带子取得
    很宽（实测 21 只落在 0.72~14.4），只拦差着量级的错。
    """

    CAPTION_MILLION = ("（人民币百万元，百分比除外）", 60.0)
    CAPTION_THOUSAND = ("（金额单位：千元）", 60.0)

    @staticmethod
    def _with_captions(rows, *cells):
        """把表首说明垫到 ``_UNIT_BAND`` **之外**再挂在文档末尾。

        垫开是必须的，否则测的不是候选：「（金额单位：千元）」只要落在报表标题下
        那一小块（:data:`sem._UNIT_BAND`）里，它本来就是**报表自己的声明**，直接
        生效、根本不经过市值那一关。真实文档里这些说明挂在正文表格上，离报表很远
        ——三棵树那 32 处「千元」就是这么来的。
        """
        while len(rows) < sem._UNIT_BAND + 1:
            rows.append(_row(560 - 22 * len(rows), ("附注正文", 60.0)))
        for cell in cells:
            rows.append(_row(560 - 22 * len(rows), cell))
        return rows

    def test_ratio_band_edges(self):
        self.assertTrue(asset_engine._ratio_ok(1.0, 1.0))
        self.assertTrue(asset_engine._ratio_ok(asset_engine.AMOUNT_RATIO_MIN, 1.0))
        self.assertTrue(asset_engine._ratio_ok(asset_engine.AMOUNT_RATIO_MAX, 1.0))
        self.assertFalse(asset_engine._ratio_ok(
            asset_engine.AMOUNT_RATIO_MIN * 0.999, 1.0))
        self.assertFalse(asset_engine._ratio_ok(
            asset_engine.AMOUNT_RATIO_MAX * 1.001, 1.0))

    def test_without_a_market_cap_there_is_no_judgement(self):
        """没有比对的另一半就不判。这条闸拦的是量纲错，不是没市值的股票。"""
        for total, cap in ((1.0, None), (1.0, 0), (1.0, -1.0), (None, 1e8), (0, 1e8)):
            self.assertTrue(asset_engine._ratio_ok(total, cap), (total, cap))

    def test_declaration_is_used_as_is(self):
        """报表自己声明了单位就直接用，不拿市值去量——声明是文档说的，不是猜的。"""
        scale, note = asset_engine.resolve_scale(
            _bs_rows(unit="单位：千元"), "合并资产负债表", None)
        self.assertEqual(scale, ("千元", 1000.0))
        self.assertIsNone(note, "没猜过就不该有说明文字")

    def test_declaration_beats_a_caption_candidate(self):
        """有声明时连候选都不看，哪怕候选的票数在全文里压倒性更多。

        正文表格标「百万元」五十次、报表自己写着「单位：千元」时，听报表的。
        （这一条同时钉住那个「没市值就直接返回声明」的早退分支：market_cap 为
        空时它恰好给出同样的答案，所以少了这条用例，把声明分支整个删掉也测不出来。）
        """
        rows = self._with_captions(_bs_rows(unit="单位：千元"), self.CAPTION_MILLION)
        scale, note = asset_engine.resolve_scale(rows, "合并资产负债表", 2e8)
        self.assertEqual(scale, ("千元", 1000.0))
        self.assertIsNone(note)

    def test_no_market_cap_means_no_guessing(self):
        """没有市值就不猜。宁可维持按元，也不拿一个**没校验过**的倍数去乘。

        候选之所以能采纳，全靠它过了市值那一关；没有市值就没有这一关，此时采纳
        等于凭空放大——那正是这条闸要防的事，不能因为换个入口就绕过去。
        """
        rows = self._with_captions(_bs_rows(), self.CAPTION_THOUSAND)
        scale, note = asset_engine.resolve_scale(rows, "合并资产负债表", None)
        self.assertEqual(scale, (None, 1.0))
        self.assertIsNone(note)

    def test_candidate_is_adopted_only_when_the_magnitude_fits(self):
        rows = self._with_captions(_bs_rows(), self.CAPTION_MILLION)
        # 2,000 按百万元 = 20 亿；市值 2 亿 → 比值 10
        scale, note = asset_engine.resolve_scale(rows, "合并资产负债表", 2e8)
        self.assertEqual(scale, ("百万元", 1e6))
        self.assertIn("百万元", note)
        self.assertIn("量级自洽", note)

    def test_candidate_is_rejected_when_the_magnitude_does_not_fit(self):
        """**三棵树（600585）就是这个形状**：正文表格标「千元」32 处，报表本身
        却是元。按票数采纳会把它现在正确的资产总计放大一千倍。

        ——「谁出现得多」和「报表用什么量纲」没有因果关系，所以只有过闸的才采纳；
        不过闸就维持按元，交给收尾校验去显形。
        """
        rows = self._with_captions(_bs_rows(), self.CAPTION_THOUSAND)
        # 2,000 按千元 = 200 万；市值 100 亿 → 比值 2e-5，出带
        scale, note = asset_engine.resolve_scale(rows, "合并资产负债表", 1e10)
        self.assertEqual(scale, (None, 1.0))
        self.assertIsNone(note)

    def test_first_candidate_that_fits_wins(self):
        """候选按出现次数排；第一个过闸的即采纳。"""
        rows = self._with_captions(_bs_rows(), self.CAPTION_THOUSAND,
                                   self.CAPTION_THOUSAND, self.CAPTION_MILLION)
        # 千元在前（票多）：2,000 → 200 万，市值 2 亿 → 比值 0.01，出带；
        # 轮到百万元：2,000 → 20 亿，比值 10，成立。
        scale, _note = asset_engine.resolve_scale(rows, "合并资产负债表", 2e8)
        self.assertEqual(scale, ("百万元", 1e6))

    def test_the_count_only_decides_the_order_not_the_answer(self):
        """票数多寡与「报表用什么量纲」没有因果关系——票少的照样可以被采纳。"""
        rows = self._with_captions(_bs_rows(), self.CAPTION_MILLION,
                                   self.CAPTION_MILLION, self.CAPTION_THOUSAND,
                                   self.CAPTION_THOUSAND, self.CAPTION_THOUSAND)
        scale, note = asset_engine.resolve_scale(rows, "合并资产负债表", 2e8)
        self.assertEqual(scale, ("百万元", 1e6))
        self.assertIn("百万元", note)

    def test_gate_flags_the_result_that_survives(self):
        """没人认领的倍数最后要在收尾校验里显形，并且**判这份口径不可用**。

        判不可用而不是降个置信度：库里的数字会被评分和界面当真，一个「错 10⁶ 倍
        但看起来很合理」的资产总计没有任何下游能识破。
        """
        rows = self._with_captions(_bs_rows(), self.CAPTION_THOUSAND)
        m = asset_engine.compute(rows, 1e10)
        gate = m["asset_value_profile"]["amount_gate"]
        self.assertTrue(gate["checked"])
        self.assertFalse(gate["valid"])
        # 「相差 0 倍」这种印法没有信息量，比值 2e-7 要照实说出来
        self.assertIn("1/", gate["reason"])
        self.assertEqual(asset_engine._unusable_reason(m), gate["reason"])

    def test_gate_is_not_checked_without_a_market_cap(self):
        """没市值 = 没查，不是查过没问题。两者在快照里必须分得开。"""
        m = asset_engine.compute(_bs_rows(), None)
        gate = m["asset_value_profile"]["amount_gate"]
        self.assertFalse(gate["checked"])
        self.assertIsNone(gate["reason"])
        self.assertIsNone(asset_engine._unusable_reason(m))

    def test_gate_is_not_checked_when_there_is_no_asset_total(self):
        """连资产总计都没有就不是「查过量级」，是没得查——`checked` 要照实为假。"""
        rows = [r for r in _bs_rows() if r["cells"][0]["text"] != "资产总计"]
        m = asset_engine.compute(rows, 1e10)
        self.assertIsNone(m["total_assets"])
        gate = m["asset_value_profile"]["amount_gate"]
        self.assertFalse(gate["checked"])
        self.assertIsNone(gate["ratio"])

    def test_a_snapshot_without_the_gate_key_is_still_usable(self):
        """旧快照的 profile 里没有这个键——不能因为「没有」就判成不可用。"""
        self.assertIsNone(asset_engine._unusable_reason(
            {"total_assets": 1.0, "asset_value_profile": {}}))


class _FakeReportStore:
    """只实现 ``analyze`` 用到的那三件事：列报告期、确保 PDF、取 rows。

    真正的 :class:`research.reports.ReportStore` 会下 PDF、写缓存，拿它跑回退
    的用例既慢又依赖网络。这里要验的是**回退的次序与停止条件**，与下载无关。
    """

    def __init__(self, periods, rows_of, titles=None):
        titles = titles or {}
        self._metas = [{"report_period": p, "title": titles.get(p, "样本 %s" % p),
                        "document_hash": "hash-" + p} for p in periods]
        self._rows = rows_of
        self.fetched = []

    def reports(self, code, kinds=None):
        return list(self._metas)

    def ensure_pdf(self, meta):
        self.fetched.append(meta["report_period"])
        return meta

    def rows_with_diag(self, meta):
        return self._rows[meta["report_period"]], {
            "runs": 0, "cjk_runs": 0, "undecodable": 0, "synthetic_cmap": 0}


class TestReportPeriodFallback(unittest.TestCase):
    """最新一期抽不出可用口径时往旧报告期回溯。

    中国建筑（601668）最新一期的报表页是扫描件、没有文本层，而上一期
    （2026Q1）文本层完好。回调的停止条件必须严格：**第一次成功就停**，否则
    已经能解析的股票会被无谓地重算到更旧的报告期上去。
    """

    GOOD = "2026Q1"


    def _store(self, bad=("2026H1",)):
        rows = {p: ([] if p in bad else _bs_rows()) for p in ("2026H1", "2026Q1", "2025Q3")}
        return _FakeReportStore(("2026H1", "2026Q1", "2025Q3"), rows)

    def test_latest_success_never_looks_further(self):
        store = self._store(bad=())
        m = asset_engine.analyze("TEST", store=store)
        self.assertEqual(m["report_period"], "2026H1")
        self.assertIsNone(m.get("report_period_note"))
        self.assertEqual(store.fetched, ["2026H1"])

    def test_falls_back_when_statement_is_missing(self):
        store = self._store()
        m = asset_engine.analyze("TEST", store=store)
        self.assertEqual(m["report_period"], self.GOOD)
        self.assertIn("2026H1", m["report_period_note"])
        self.assertIn(self.GOOD, m["report_period_note"])
        self.assertEqual(store.fetched, ["2026H1", self.GOOD])

    def test_unusable_total_assets_counts_as_failure(self):
        """找到了表、却认不出合计行 → 同样是失败，必须接着回溯。

        ``build_economic_view`` 这时**不抛错**，而是返回 ``total_assets=None``。
        002460 在库里就留着这样一行 NULL 快照——它当时被当成了一次成功结果。
        """
        no_total = [r for r in _bs_rows() if r["cells"][0]["text"] != "资产总计"]
        store = _FakeReportStore(
            ("2026H1", "2026Q1"),
            {"2026H1": no_total, "2026Q1": _bs_rows()})
        m = asset_engine.analyze("TEST", store=store)
        self.assertEqual(m["report_period"], "2026Q1")
        self.assertEqual(store.fetched, ["2026H1", "2026Q1"])

    def test_amount_gate_failure_counts_as_failure(self):
        """找到了表、也认出了合计行，但量纲对不上市值 → 同样是失败，接着回溯。

        这一条比「认不出合计行」更隐蔽：数字全都读出来了，只有和市值比才看得出
        差着量级。放它过去，库里就多一行「看起来正常、其实错 10⁶ 倍」的资产总计。
        """
        store = _FakeReportStore(
            ("2026H1", "2026Q1"),
            {"2026H1": _bs_rows(), "2026Q1": _bs_rows(unit="单位：千元")})
        # 市值 100 万：H1 按元是 2,000（比值 0.002，出带）；Q1 按千元是 200 万
        # （比值 2，成立）。
        m = asset_engine.analyze("TEST", market_cap=1e6, store=store)
        self.assertEqual(m["report_period"], "2026Q1")
        self.assertEqual(store.fetched, ["2026H1", "2026Q1"])
        self.assertIn("量级", m["report_period_note"])

    def test_gate_failure_is_reported_as_the_reason(self):
        """全期失败时，「为什么」必须是量级那一条，不能退回笼统的「找不到报表」。"""
        store = _FakeReportStore(("2026H1",), {"2026H1": _bs_rows()})
        with self.assertRaises(sem.BalanceSheetError) as ctx:
            asset_engine.analyze("TEST", market_cap=1e6, store=store)
        msg = str(ctx.exception)
        self.assertIn("2026H1", msg)
        self.assertIn("量级", msg)

    def test_every_period_failure_names_every_period(self):
        """全部都失败时，每期各自的原因都要写出来。

        只报最新一期那条会引出「那上一期又为什么不行」——这一层存在的意义就是
        让失败原因可查，而不是让人再猜一轮。
        """
        store = self._store(bad=("2026H1", "2026Q1", "2025Q3"))
        with self.assertRaises(sem.BalanceSheetError) as ctx:
            asset_engine.analyze("TEST", store=store)
        msg = str(ctx.exception)
        for p in ("2026H1", "2026Q1", "2025Q3"):
            self.assertIn(p, msg)

    def test_no_reports_returns_none(self):
        store = _FakeReportStore((), {})
        self.assertIsNone(asset_engine.analyze("TEST", store=store))


if __name__ == "__main__":
    unittest.main()
