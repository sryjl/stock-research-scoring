# -*- coding: utf-8 -*-
"""应付票据的附注证据与分类（P0-3）。

这一组测的是**一类科目**，不是某一个数：应付票据缺省是经营性负债，只有财报
附注明确写了融资属性才算有息。理由见
``asset_semantics.NOTE_FINANCING_PHRASES`` 上面那段——票据种类说明不了经济
实质，而把它算成有息债务会同时污染 Adjusted Net Cash、净现金占市值、利息
覆盖倍数、扣有息负债后资产价值四个指标，却**不改善清算保守性**：清算价值本来
就按 100% 扣掉全部负债里的应付票据。

刻意**不**断言任何真实股票的具体数值。三种标题排版（单格「35、」、单格
「35.」、两格「35、|应付票据」）和各家多出来的票种（信用证、财务公司承兑
汇票）都写进了合成版面，但断言的是**解析出来的关系**（附注合计 == 报表科目
余额、缺省经营、有证据才有息），不是抄下来的数字。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import asset_semantics as sem
from tests.test_asset_semantics import _row

FIX_NP_CURRENT = 1_000_000.00
FIX_NP_PRIOR = 800_000.00


def _report(note_rows, np_current="1,000,000.00", np_prior="800,000.00",
            reported="1,000,000.00"):
    """合成一份最小半年报：合并资产负债表 + 一起应付票据附注。

    ``reported`` 是报表上「负债合计」那一行。它和应付票据的期末对不上时，
    ``parse_liabilities`` 的对平会判 FAIL——那也是一种要测的情形。
    """
    rows = [
        _row(880, ("合并资产负债表", 60)),
        _row(860, ("项目", 60), ("期末余额", 330), ("期初余额", 450)),
        _row(840, ("资产总计", 60), ("9,000,000.00", 330), ("8,000,000.00", 450)),
        _row(820, ("应付票据", 66), (np_current, 330), (np_prior, 450)),
        _row(800, ("负债合计", 60), (reported, 330), ("700,000.00", 450)),
        _row(780, ("负债和所有者权益总计", 60), ("9,000,000.00", 330),
             ("8,000,000.00", 450)),
        _row(760, ("七、合并财务报表项目注释", 60)),
    ]
    rows.extend(note_rows)
    return rows


def _note(heading=(("35、应付票据", 60),), kinds=(), total="1,000,000.00",
          narrative=(), with_gate=True, y=740.0):
    """造一条应付票据附注。``kinds`` 是 ``(名字, 金额)``，金额为 ``None``
    表示那一行只印了名字（票种本期为零）。"""
    rows = [_row(y, *heading)]
    y -= 18.0
    if with_gate:
        rows.append(_row(y, ("√适用□", 60), ("不适用", 200)))
        y -= 18.0
    rows.append(_row(y, ("单位：元币种：人民币", 60)))
    y -= 18.0
    rows.append(_row(y, ("种类", 60), ("期末余额", 330), ("期初余额", 450)))
    y -= 18.0
    for name, amount in kinds:
        cells = [(name, 60)] if amount is None else [(name, 60), (amount, 330)]
        rows.append(_row(y, *cells))
        y -= 18.0
    rows.append(_row(y, ("合计", 60), (total, 330), ("700,000.00", 450)))
    y -= 18.0
    for text in narrative:
        rows.append(_row(y, (text, 60)))
        y -= 18.0
    return rows


def _bank(amount="1,000,000.00"):
    return (("银行承兑汇票", amount),)


class TestFindPayableNote(unittest.TestCase):
    """定位。四重防误命中，每一条都对应一份真实半年报里的干扰行。"""

    def test_three_heading_layouts(self):
        """三种排版：单格点号、单格顿号、两格（序号和科目名被切开）。"""
        for heading in ((("35、应付票据", 60),),
                        (("35.应付票据", 60),),
                        (("35、", 60), ("应付票据", 80))):
            rows = _report(_note(heading=heading, kinds=_bank()))
            found = sem.find_payable_note(rows)
            self.assertIsNotNone(found, heading)

    def test_toc_line_is_not_a_heading(self):
        """目录里印着「应付票据………35」，没有明细表。"""
        rows = _report([_row(740, ("应付票据………………35", 60))])
        self.assertIsNone(sem.find_payable_note(rows))

    def test_related_party_row_is_not_a_heading(self):
        """华域 600741 的关联方交易表：「应付票据 | 合营企业 | 34,040,000.00」。

        它在附注区**内**，所以「必须在合并附注区内」那条拦不住它，要靠
        「标题不超过两个单元格」。
        """
        rows = _report([
            _row(740, ("应付票据", 60), ("合营企业", 200), ("34,040,000.00", 330)),
            _row(722, ("应付票据", 60), ("联营企业", 200), ("1,000.00", 330)),
        ])
        self.assertIsNone(sem.find_payable_note(rows))

    def test_bare_account_row_in_the_statement_is_not_a_heading(self):
        """资产负债表里的裸科目行「应付票据」（华域第 1728 行）。

        它落在某个附注区起点之后，标题也整体匹配，只有「后面得有明细表表头」
        能拦住它。**不能**改成检查附注开头那行「□适用√不适用」——牧原 002714
        和青岛啤酒 600600 都不印那一行。
        """
        rows = [
            _row(880, ("合并资产负债表", 60)),
            _row(860, ("项目", 60), ("期末余额", 330), ("期初余额", 450)),
            _row(840, ("应付票据", 66), ("1,000,000.00", 330), ("800,000.00", 450)),
            _row(820, ("应付账款", 66), ("2,000,000.00", 330), ("1,000.00", 450)),
            _row(800, ("负债合计", 60), ("1,000,000.00", 330), ("800,000.00", 450)),
            _row(780, ("负债和所有者权益总计", 60), ("9,000,000.00", 330)),
            _row(760, ("七、合并财务报表项目注释", 60)),
        ]
        self.assertIsNone(sem.find_payable_note(rows))

    def test_note_without_the_gate_row_is_still_found(self):
        """牧原/青岛啤酒不印「□适用√不适用」，附注照样要能定位到。"""
        rows = _report(_note(kinds=_bank(), with_gate=False))
        self.assertIsNotNone(sem.find_payable_note(rows))

    def test_parent_note_area_is_out_of_scope(self):
        """母公司附注区里的同名附注不算——报表口径不同，不能拿来定合并的数。"""
        rows = _report([_row(740, ("母公司财务报表主要项目注释", 60))])
        rows.extend(_note(kinds=_bank(), y=700.0))
        self.assertIsNone(sem.find_payable_note(rows))


class TestParsePayableNote(unittest.TestCase):
    def _parse(self, note_rows, np_current="1,000,000.00", np_prior="800,000.00"):
        rows = _report(note_rows, np_current=np_current, np_prior=np_prior)
        bs = sem.parse_balance_sheet(rows)
        section = sem.find_payable_note(rows)
        info = bs.get("应付票据") or {}
        return sem.parse_payable_note(rows, section, info.get("current"),
                                     info.get("prior"))

    def test_kinds_split_and_zero_row_without_amount(self):
        """只有科目名、没有金额的单格行 = 该票种本期为零。

        ``parse_note_items`` 会把这种行整条丢掉（``if not nums: continue``），
        票种拆分正是靠它才成立，所以这里不能复用它。
        """
        ev = self._parse(_note(kinds=(("商业承兑汇票", None),
                                      ("银行承兑汇票", "1,000,000.00"))))
        self.assertEqual(ev["commercial_accepted"], 0.0)
        self.assertIn("商业承兑汇票", ev["amount_missing"])
        self.assertAlmostEqual(ev["bank_accepted"], FIX_NP_CURRENT, places=2)
        self.assertTrue(ev["matches_bs"])
        self.assertTrue(ev["kinds_cover_total"])
        self.assertFalse(ev["review"])

    def test_untracked_kind_is_recorded_not_dropped(self):
        """牧原的「信用证」、青岛啤酒的「财务公司承兑汇票」不能静默丢掉。

        丢掉之后票种拆分加起来对不上附注合计，而页面看上去一切正常，只是
        少了几个亿。
        """
        ev = self._parse(_note(kinds=(("财务公司承兑汇票", "300,000.00"),
                                      ("商业承兑汇票", "700,000.00"))))
        self.assertEqual(ev["other_kinds"], {"财务公司承兑汇票": 300_000.00})
        self.assertTrue(ev["review"], "有没归类的票种就该让人回头看")
        self.assertTrue(ev["matches_bs"], "票种没归全，不妨碍附注合计和报表对上")

    def test_prior_only_note_matches_the_prior_period(self):
        """附注只印了期初（东瑞股份 001201 的形态）。

        报表上应付票据期末整格没印 → 期末 0、期初 4,694 万；附注那张表也只印
        了期初一列。只拿 ``nums[0]`` 去比期末，会把「附注只印了期初」误判成
        「附注和报表矛盾」，白报一个 review。
        """
        ev = self._parse(_note(kinds=_bank("800,000.00"), total="800,000.00"),
                         np_current="0.00")
        self.assertEqual(ev["matched_period"], "prior")
        self.assertTrue(ev["matches_bs"])
        self.assertFalse(ev["review"], "期末本来就是零，命中哪一期都无所谓")

    def test_prior_only_note_with_nonzero_current_is_flagged(self):
        """期末真有钱、而附注只印了期初时，才值得回头看。"""
        ev = self._parse(_note(kinds=_bank("800,000.00"), total="800,000.00"))
        self.assertEqual(ev["matched_period"], "prior")
        self.assertTrue(ev["review"])

    def test_note_total_mismatch_voids_the_evidence(self):
        """附注合计和报表科目余额对不上 → 证据作废，回落经营性负债。

        差额取 10 万（远超 ``max(1.0, |bs|*1e-6)`` = 1 元的容差）。用 999,999 对
        1,000,000 只差 1 元，正好压在容差边界上，反倒是**应该**判为相等。
        """
        ev = self._parse(_note(kinds=_bank("900,000.00"), total="900,000.00"))
        self.assertFalse(ev["matches_bs"])
        self.assertEqual(ev["reconciliation"], "FAIL")
        self.assertTrue(ev["review"])
        self.assertEqual(sem.classify_liability("应付票据", evidence=ev),
                         "OPERATING_LIABILITY")

    def test_financing_phrase_makes_it_interest_bearing(self):
        for phrase in ("其中融资性票据 100,000.00 元",
                       "本公司的应付票据融资余额为 100,000.00 元",
                       "该银行承兑汇票融资 100,000.00 元",
                       "应付票据按票面约定计息",
                       "应付票据按融资利率计息",
                       "应付票据实质为借款替代工具"):
            ev = self._parse(_note(kinds=_bank(),
                                   narrative=(phrase,)))
            self.assertEqual(ev["evidence_level"], "account", phrase)
            self.assertTrue(sem._financing_confirmed(ev), phrase)
            self.assertEqual(sem.classify_liability("应付票据", evidence=ev),
                             "INTEREST_BEARING_DEBT", phrase)

    def test_phrase_on_a_kind_row_is_kind_level(self):
        ev = self._parse(_note(kinds=(("融资性票据", "1,000,000.00"),)))
        self.assertEqual(ev["evidence_level"], "kind")
        self.assertEqual(sem.classify_liability("应付票据", evidence=ev),
                         "INTEREST_BEARING_DEBT")

    def test_negation_cancels_the_evidence(self):
        """「融资性票据不计息」——有融资两个字，但明说不计息，不算有息负债。"""
        ev = self._parse(_note(kinds=_bank(),
                               narrative=("本公司的融资性票据不计息",)))
        self.assertTrue(ev["negated"])
        self.assertFalse(sem._financing_confirmed(ev))
        self.assertEqual(sem.classify_liability("应付票据", evidence=ev),
                         "OPERATING_LIABILITY")

    def test_discount_phrasing_only_flags_review(self):
        """票据被贴现是事实，但贴现的是已开出的经营票据，不能凭它推定票据本身
        有融资属性——只标记 review，不改分类。"""
        ev = self._parse(_note(kinds=_bank(),
                               narrative=("期末已贴现未到期的银行承兑汇票为 0 元",)))
        self.assertEqual(ev["evidence_level"], "none")
        self.assertTrue(ev["review"])
        self.assertEqual(sem.classify_liability("应付票据", evidence=ev),
                         "OPERATING_LIABILITY")


class TestClassifyLiability(unittest.TestCase):
    def test_default_is_operating(self):
        self.assertEqual(sem.classify_liability("应付票据"),
                         "OPERATING_LIABILITY")
        self.assertEqual(sem.classify_liability("应付票据", evidence=None),
                         "OPERATING_LIABILITY")

    def test_unmatched_evidence_does_not_count(self):
        """证据有、报表对不上 → 不算。"""
        ev = {"evidence_level": "account", "matches_bs": False}
        self.assertEqual(sem.classify_liability("应付票据", evidence=ev),
                         "OPERATING_LIABILITY")

    def test_other_interest_bearing_accounts_are_unaffected(self):
        """只有应付票据改口径，别的一级科目一个都不许动。"""
        for name in ("短期借款", "长期借款", "应付债券", "租赁负债",
                     "一年内到期的非流动负债", "长期应付款",
                     "其他流动负债", "交易性金融负债"):
            self.assertEqual(sem.classify_liability(name),
                             "INTEREST_BEARING_DEBT", name)
        for name in ("应付账款", "应付职工薪酬", "应交税费", "合同负债"):
            self.assertEqual(sem.classify_liability(name),
                             "OPERATING_LIABILITY", name)

    def test_financial_institutions_active_debt_is_interest_bearing(self):
        """银行 / 保险的**主动负债**算有息：同业存放、拆入资金、向央行借款、
        卖出回购。名字抄的是招行/平安的原文。

        这四样是主动借来的钱；客户存款是别人存进来的钱，按裁定仍算经营性。
        招行 12.4 万亿负债原来只有 1467 亿（1.2%）算有息，客户存款 10.24 万亿
        和同业存放 1.19 万亿全成了经营占款。
        """
        for name in ("同业和其他金融机构存放款项",     # 招行
                     "银行同业及其他金融机构存放款项",   # 平安
                     "拆入资金", "向中央银行借款", "卖出回购金融资产款"):
            self.assertEqual(sem.classify_liability(name),
                             "INTEREST_BEARING_DEBT", name)

    def test_customer_deposits_and_insurance_liabilities_stay_operating(self):
        """客户存款 / 吸收存款 / 保险合同负债是经营性的——按裁定，
        存款是别人存进来的钱，不是公司的融资安排。"""
        for name in ("客户存款", "吸收存款", "保险合同负债", "预收保费",
                     "代理买卖证券款", "衍生金融负债"):
            self.assertEqual(sem.classify_liability(name),
                             "OPERATING_LIABILITY", name)

    def test_merged_deposit_and_interbank_caption_stays_operating(self):
        """「吸收存款及同业存放」是存款 + 同业的**合并列示**，按存款那条裁定
        归经营性。

        格力电器（财务子公司）就有这一行 1.83 亿。规则若图省事写成裸的
        「同业」，一家非金融公司会被算成有息——波及面就不止目标那两只了。
        """
        self.assertEqual(sem.classify_liability("吸收存款及同业存放"),
                         "OPERATING_LIABILITY")

    def test_kind_name_when_confirmed(self):
        ev = {"evidence_level": "account", "matches_bs": True}
        self.assertEqual(sem.classify_liability("应付票据", evidence=ev),
                         "INTEREST_BEARING_DEBT")
        self.assertEqual(sem.interest_bearing_kind("应付票据"),
                         "interest_bearing_notes")


class TestLiabilitySetIntegration(unittest.TestCase):
    """走 ``parse_liabilities`` 的完整路径。"""

    def test_default_operating_and_still_in_total_liabilities(self):
        """缺省经营，但**始终 100% 留在全部负债里**。

        这条不变式是这次改动的关键：应付票据只在「有息 / 非有息」之间搬家，
        清算价值的扣减项（全部负债）逐位不变。
        """
        rows = _report(_note(kinds=_bank()), reported="1,000,000.00")
        bs = sem.parse_balance_sheet(rows)
        ls = sem.parse_liabilities(bs, rows=rows)
        self.assertEqual(ls.reconciliation, "OK")
        self.assertAlmostEqual(ls.parsed_total, FIX_NP_CURRENT, places=2)
        self.assertIn("应付票据", ls.non_interest_bearing)
        self.assertNotIn("应付票据", ls.interest_bearing)
        self.assertAlmostEqual(ls.interest_bearing_total, 0.0, places=2)
        # 金额还在全部负债里，一分没少
        self.assertAlmostEqual(ls.non_interest_bearing_total, FIX_NP_CURRENT,
                               places=2)
        self.assertAlmostEqual(ls.total_liabilities, FIX_NP_CURRENT, places=2)

    def test_without_rows_it_is_still_operating(self):
        """不传版面行（拿不到附注）时缺省经营性——宁可少认一笔有息负债，
        也不要把不知道的东西当有息。"""
        rows = _report(_note(kinds=_bank()), reported="1,000,000.00")
        ls = sem.parse_liabilities(sem.parse_balance_sheet(rows))
        self.assertNotIn("应付票据", ls.interest_bearing)
        self.assertEqual(ls.financing, {},
                         "没有版面行就不该有附注证据")

    def test_confirmed_financing_moves_it_into_interest_bearing(self):
        rows = _report(_note(kinds=_bank(),
                             narrative=("本公司的应付票据为融资性票据",)),
                       reported="1,000,000.00")
        bs = sem.parse_balance_sheet(rows)
        ls = sem.parse_liabilities(bs, rows=rows)
        self.assertIn("应付票据", ls.interest_bearing)
        self.assertAlmostEqual(ls.interest_bearing_total, FIX_NP_CURRENT,
                               places=2)
        # 搬家前后，全部负债一分不变
        self.assertAlmostEqual(ls.parsed_total, FIX_NP_CURRENT, places=2)

    def test_evidence_is_exposed_for_audit(self):
        rows = _report(_note(kinds=_bank()), reported="1,000,000.00")
        bs = sem.parse_balance_sheet(rows)
        d = sem.parse_liabilities(bs, rows=rows).to_dict()
        self.assertIn("notes_payable", d)
        self.assertIn("liability_evidence", d)
        self.assertEqual(d["notes_payable"]["bank_accepted"], FIX_NP_CURRENT)


if __name__ == "__main__":
    unittest.main()
