# -*- coding: utf-8 -*-
"""清算口径（LIQUIDATION_MODEL_V1）与完整负债解析的约束测试。

这一轮修的是两件事，它们必须一起成立才有效：

1. **数据层**：负债表要**完整**解析（流动 + 非流动两大段），并且和报表印的
   「负债合计」对平。以前的分段开关在「流动负债合计」那一行就把整段非流动
   负债判成了权益段，9 只股票全部中招——华域汽车少认了 53.93 亿有息负债。
2. **命名层**：``清算价值`` 扣的必须是**全部**负债。以前扣的是有息负债，
   却叫「清算价值」——华域汽车 2026H1 因此虚高 241.30 亿，而那个数看起来
   完全正常，不会报错，只会静默参与评分。

所以这里的断言分四组：

* **对平**：解析负债 == 报表负债合计；对不平就不许出数。
* **完整**：非流动负债整段、空科目行、单栏金额行都不能把数据搞丢或搞错位。
* **恒等式**：清算价值 = 折价后资产 − 全部负债；且清算价值 <= 折价后资产；
  且若所有折价率 <= 1，折价后资产 <= 总资产。
* **禁令**：不许把「折价后资产 − 有息负债」叫清算价值——这组的断言方式是
  **行为**（两个数在有息负债 != 全部负债时必须不等），不是源码字符串匹配。

期望值一律由解析与恒等式算出；写进这里的常量是 fixture 的**输入内容**。
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
from tests.test_asset_semantics import _row, _rows
from tests.test_research import _metrics

#: fixture（tests/make_fixture.py）里的负债侧输入，不是期望输出。
FIX_CURRENT_LIAB = 1_700_000_000.00
FIX_NONCURRENT_LIAB = 800_000_000.00
FIX_TOTAL_LIAB = 2_500_000_000.00
#: 短借 + 一年内到期 + 长借 + 债券 + 租赁。**不含应付票据**：它缺省是经营性
#: 负债，只有附注明确写了融资属性才算有息（见 ``sem.NOTE_FINANCING_PHRASES``）。
FIX_INTEREST_BEARING = 1_550_000_000.00
#: fixture 报表上的应付票据。它**始终**计入全部负债（清算价值 100% 扣它），
#: 但不计入有息负债。这两个数差的就是这一笔。
FIX_NOTES_PAYABLE = 50_000_000.00
FIX_PRIOR_ONLY = 4_242_100.00                    # 「预收款项」期末整格没印，只有期初


def _liab_set(rows=None):
    return sem.parse_liabilities(sem.parse_balance_sheet(rows or _rows()))


def _sheet(*liability_rows, total=None, current_total=None):
    """合成一张只有负债侧的资产负债表，用来构造边界情形。"""
    amount_x, prior_x = 330.0, 450.0

    def two(value, prior):
        return ((f"{value:,.2f}", amount_x), (f"{prior:,.2f}", prior_x))

    rows = [
        _row(800, ("合并资产负债表", 60)),
        _row(778, ("项目", 60), ("期末余额", amount_x), ("期初余额", prior_x)),
        _row(756, ("资产总计", 60), *two(10_000_000_000.0, 9_000_000_000.0)),
    ]
    y = 734.0
    for spec in liability_rows:
        name, values = spec[0], spec[1:]
        rows.append(_row(y, (name, 60), *values))
        y -= 22.0
        _ = name
    if current_total is not None:
        rows.append(_row(y, ("流动负债合计", 60), *two(current_total, current_total)))
        y -= 22.0
    if total is not None:
        rows.append(_row(y, ("负债合计", 60), *two(total, total)))
        y -= 22.0
    rows.append(_row(y, ("负债和所有者权益总计", 60),
                     *two(10_000_000_000.0, 9_000_000_000.0)))
    return rows


# --------------------------------------------------------------------------- #
# 1. 数据层：负债解析必须和报表对平
# --------------------------------------------------------------------------- #
class TestLiabilityReconciliation(unittest.TestCase):
    def test_parsed_liabilities_equal_reported_total(self):
        ls = _liab_set()
        self.assertAlmostEqual(ls.parsed_total, FIX_TOTAL_LIAB, places=2)
        self.assertEqual(ls.reported_total, FIX_TOTAL_LIAB)
        self.assertEqual(ls.reconciliation, "OK")
        self.assertTrue(ls.usable)
        self.assertEqual(ls.total_liabilities, FIX_TOTAL_LIAB)

    def test_both_liability_segments_are_complete(self):
        """两段都要在，而且各自和分段合计对得上。

        分段开关失灵时（「负债合计」是「流动负债合计」的子串），整段非流动
        负债会**静默**归到权益段去：科目一个不少，只是全都不见了。
        """
        ls = _liab_set()
        for name in ("长期借款", "应付债券", "租赁负债", "递延收益", "预计负债",
                     "递延所得税负债"):
            self.assertIn(name, ls.noncurrent, f"非流动负债段漏了 {name}")
        for name in ("短期借款", "应付账款", "合同负债", "应付职工薪酬", "应交税费"):
            self.assertIn(name, ls.current, f"流动负债段漏了 {name}")
        self.assertAlmostEqual(sum(ls.noncurrent.values()), FIX_NONCURRENT_LIAB, places=2)
        self.assertAlmostEqual(sum(ls.current.values()), FIX_CURRENT_LIAB, places=2)

    def test_only_current_liabilities_is_normal(self):
        """只有流动负债的公司是小公司常态，不该被判成「整段缺失」。"""
        rows = _sheet(("短期借款", ("100,000,000.00", 330.0), ("90,000,000.00", 450.0)),
                      current_total=100_000_000.0, total=100_000_000.0)
        ls = sem.parse_liabilities(sem.parse_balance_sheet(rows))
        self.assertEqual(ls.noncurrent, {})
        self.assertEqual(ls.reconciliation, "OK")
        self.assertAlmostEqual(ls.total_liabilities, 100_000_000.0, places=2)

    def test_empty_account_row_is_not_a_missing_segment(self):
        """「持有待售负债」这种没金额的科目行，不能把分段逻辑带跑。

        fixture 的流动负债段里就插了这么一行。它在的时候和不在的时候，
        解析结果必须一致。
        """
        ls = _liab_set()
        self.assertNotIn("持有待售负债", ls.current)
        self.assertAlmostEqual(ls.parsed_total, FIX_TOTAL_LIAB, places=2)

    def test_interest_bearing_is_a_subset_of_total_liabilities(self):
        ls = _liab_set()
        every = set(ls.interest_bearing) | set(ls.non_interest_bearing)
        self.assertTrue(set(ls.interest_bearing) <= every)
        self.assertAlmostEqual(ls.interest_bearing_total, FIX_INTEREST_BEARING, places=2)
        # 两块相加必须正好是全部负债——有息负债是它的子集，不许另算一套
        self.assertAlmostEqual(
            ls.interest_bearing_total + ls.non_interest_bearing_total,
            ls.parsed_total, places=2)

    def test_interest_bearing_kinds_cover_every_kind(self):
        """每一种**由名字就能认定**的有息科目，都要被认出来并归到对应的 kind。"""
        kinds = _liab_set().interest_bearing_by_kind()
        for kind in ("short_borrowings", "current_portion_of_long_term_debt",
                     "long_term_borrowings", "bonds_payable",
                     "lease_liabilities"):
            self.assertGreater(kinds.get(kind, 0.0), 0.0, f"{kind} 没认出来")
        # 应付票据**故意不在**上面那一串里：fixture 的附注没写融资属性，所以它
        # 缺省是经营性负债。「附注写了融资性票据时它才出现」那个正向断言在
        # tests/test_notes_payable.py —— 那里才有能构造出附注的合成版面行。
        self.assertNotIn("interest_bearing_notes", kinds)

    def test_sub_line_is_not_counted_twice(self):
        """「其中：应付利息」已经含在「其他应付款」里，再加一遍就对不平了。"""
        ls = _liab_set()
        self.assertNotIn("其中：应付利息", ls.current)
        self.assertAlmostEqual(sum(ls.current.values()), FIX_CURRENT_LIAB, places=2)

    def test_mismatch_blocks_total_liabilities(self):
        """对不平 → usable=False、total_liabilities=None、状态写明差多少。"""
        rows = _sheet(("短期借款", ("100,000,000.00", 330.0), ("90,000,000.00", 450.0)),
                      current_total=100_000_000.0,
                      total=150_000_000.0)          # 故意差 5000 万
        ls = sem.parse_liabilities(sem.parse_balance_sheet(rows))
        self.assertEqual(ls.reconciliation, "FAIL")
        self.assertFalse(ls.usable)
        self.assertIsNone(ls.total_liabilities,
                          "对不平还给出「全部负债」，等于把清算价值悄悄放行")
        self.assertTrue(ls.status.startswith("invalid_base"), ls.status)
        self.assertIn("reconciliation_failed", ls.status)

    def test_missing_reported_total_is_also_invalid(self):
        rows = _sheet(("短期借款", ("100,000,000.00", 330.0), ("90,000,000.00", 450.0)),
                      current_total=100_000_000.0)   # 没有「负债合计」那一行
        ls = sem.parse_liabilities(sem.parse_balance_sheet(rows))
        self.assertEqual(ls.reconciliation, "NO_REPORTED_TOTAL")
        self.assertIsNone(ls.total_liabilities)
        self.assertTrue(ls.status.startswith("invalid_base"), ls.status)

    def test_prior_only_row_does_not_shift_into_the_current_column(self):
        """期末那格**整格没印**时，剩下的那个数是期初，期末是 0。

        fixture 的「预收款项」就是这样印的。当成期末的话，流动负债会多出
        424.21 万，而负债合计永远差这么多——对不平，于是清算价值全被压住。
        """
        bs = sem.parse_balance_sheet(_rows())
        self.assertEqual(bs["预收款项"]["current"], 0.0)
        self.assertEqual(bs["预收款项"]["prior"], FIX_PRIOR_ONLY)
        self.assertEqual(bs["其他流动负债"]["current"], 0.0)
        # 反向证明这条断言是有用的：把它按期末加回去，立刻对不平
        ls = _liab_set()
        self.assertAlmostEqual(ls.parsed_total, ls.reported_total, places=2)
        self.assertGreater(abs(ls.parsed_total + FIX_PRIOR_ONLY - ls.reported_total),
                           ls.tolerance)

    def test_blank_current_column_survives_on_a_synthetic_sheet(self):
        """同样的情形换成合成表也必须成立——不依赖 fixture 的排版。"""
        rows = [
            _row(800, ("合并资产负债表", 60)),
            _row(778, ("资产总计", 60), ("100,000,000.00", 330.0), ("90,000,000.00", 450.0)),
            _row(756, ("应付账款", 60), ("50,000,000.00", 330.0), ("40,000,000.00", 450.0)),
            _row(734, ("预收款项", 60), ("7,000,000.00", 450.0)),
            _row(712, ("流动负债合计", 60), ("50,000,000.00", 330.0), ("47,000,000.00", 450.0)),
            _row(690, ("负债合计", 60), ("50,000,000.00", 330.0), ("47,000,000.00", 450.0)),
            _row(668, ("负债和所有者权益总计", 60), ("100,000,000.00", 330.0),
                 ("90,000,000.00", 450.0)),
        ]
        ls = sem.parse_liabilities(sem.parse_balance_sheet(rows))
        self.assertEqual(ls.current["预收款项"], 0.0)
        self.assertEqual(ls.reconciliation, "OK")


# --------------------------------------------------------------------------- #
# 2. 口径层：四个指标必须分开
# --------------------------------------------------------------------------- #
class TestLiquidationIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = sem.build_economic_view(_rows())["items"]

    def test_v1_is_gross_minus_total_liabilities(self):
        for s in hc.SCENARIOS:
            lv = hc.liquidating_value(self.items, s, FIX_TOTAL_LIAB)
            self.assertEqual(lv["model"], hc.LIQUIDATION_MODEL_V1)
            self.assertEqual(lv["liability_deduction"], 1.0)
            self.assertAlmostEqual(
                lv["liquidation_value"],
                lv["gross_adjusted_assets"] - FIX_TOTAL_LIAB, places=2)

    def test_liquidation_never_exceeds_gross(self):
        for s in hc.SCENARIOS:
            lv = hc.liquidating_value(self.items, s, FIX_TOTAL_LIAB)
            self.assertLessEqual(lv["liquidation_value"], lv["gross_adjusted_assets"])

    def test_every_haircut_is_at_most_one(self):
        """折价率一旦大于 1，折价后资产就会大于总资产——那是凭空造资产。"""
        for cls in hc.HAIRCUT_TABLE:
            for s in hc.SCENARIOS:
                self.assertLessEqual(hc.rate(cls, s), 1.0, f"{cls}/{s}")
                self.assertGreaterEqual(hc.rate(cls, s), 0.0, f"{cls}/{s}")

    def test_gross_never_exceeds_total_assets(self):
        for s in hc.SCENARIOS:
            lv = hc.liquidating_value(self.items, s, FIX_TOTAL_LIAB)
            self.assertLessEqual(lv["gross_adjusted_assets"], 5_941_000_000.00)

    def test_net_interest_bearing_value_is_not_liquidation_value(self):
        """两个数在「有息 != 全部」时必须不等——这就是那条命名禁令。

        只扣有息负债会把应付账款、合同负债、职工薪酬、应交税费全部漏掉，
        它们清算时同样要还。fixture 里差了 9 亿（900,000,000.00）。
        """
        for s in hc.SCENARIOS:
            lv = hc.liquidating_value(self.items, s, FIX_TOTAL_LIAB,
                                      interest_bearing_debt=FIX_INTEREST_BEARING)
            self.assertAlmostEqual(
                lv["net_interest_bearing_asset_value"],
                lv["gross_adjusted_assets"] - FIX_INTEREST_BEARING, places=2)
            self.assertAlmostEqual(
                lv["liquidation_value"] - lv["net_interest_bearing_asset_value"],
                FIX_INTEREST_BEARING - FIX_TOTAL_LIAB, places=2)
            self.assertNotAlmostEqual(lv["liquidation_value"],
                                      lv["net_interest_bearing_asset_value"], places=2)

    def test_unknown_total_liabilities_yields_none_not_the_old_number(self):
        """负债没对平时，清算价值是 None——不许退回「扣有息负债」那个数。"""
        lv = hc.liquidating_value(self.items, hc.BASE, None,
                                  interest_bearing_debt=FIX_INTEREST_BEARING)
        self.assertIsNone(lv["liquidation_value"])
        self.assertIsNotNone(lv["net_interest_bearing_asset_value"],
                             "正名后的那个指标与负债对平无关，应当照常给出")
        self.assertIsNone(lv["total_liabilities"])

    def test_no_liabilities_means_liquidation_equals_gross(self):
        lv = hc.liquidating_value(self.items, hc.BASE, 0.0)
        self.assertAlmostEqual(lv["liquidation_value"],
                               lv["gross_adjusted_assets"], places=2)

    def test_excluded_classes_never_enter_the_gross(self):
        view = sem.build_economic_view(_rows())
        lv = hc.liquidating_value(view["items"], hc.OPTIMISTIC, FIX_TOTAL_LIAB)
        without = hc.liquidating_value(
            [it for it in view["items"]
             if it.economic_class not in hc.EXCLUDED_FROM_LIQUIDATION],
            hc.OPTIMISTIC, FIX_TOTAL_LIAB)
        self.assertAlmostEqual(lv["gross_adjusted_assets"],
                               without["gross_adjusted_assets"], places=2)

    def test_profile_carries_the_liability_side(self):
        view = sem.build_economic_view(_rows())
        prof = hc.asset_value_profile(view["items"], FIX_TOTAL_LIAB, 1e9,
                                      interest_bearing_debt=FIX_INTEREST_BEARING)
        self.assertEqual(prof["liquidation_model"], hc.LIQUIDATION_MODEL_V1)
        self.assertEqual(prof["total_liabilities"], FIX_TOTAL_LIAB)
        self.assertEqual(prof["interest_bearing_debt"], FIX_INTEREST_BEARING)
        s = prof["scenarios"][hc.CONSERVATIVE]
        self.assertAlmostEqual(s["liquidation_to_market_cap"],
                               s["liquidation_value"] / 1e9, places=12)

    def test_no_market_cap_still_reports_the_absolute_numbers(self):
        items = sem.build_economic_view(_rows())["items"]
        prof = hc.asset_value_profile(items, FIX_TOTAL_LIAB)
        self.assertIsNotNone(
            prof["scenarios"][hc.BASE]["liquidation_value"])


# --------------------------------------------------------------------------- #
# 3. 取数层：对平失败要一路传到画像，变成 missing 而不是 0
# --------------------------------------------------------------------------- #
class TestProviderGate(unittest.TestCase):
    def _payload(self, **over):
        prof = {"liquidation_model": hc.LIQUIDATION_MODEL_V1,
                "total_liabilities": 1e9,
                "scenarios": {"CONSERVATIVE": {
                    "gross_adjusted_assets": 3e9,
                    "liquidation_value": 2e9,
                    "net_interest_bearing_asset_value": 2.5e9}},
                "liabilities": {"reconciliation": "OK", "status": "OK",
                                "reported_total": 1e9}}
        prof.update(over.pop("profile", {}))
        return {"stock_code": "T", "metric_version": sem.VERSION,
                "market_cap": 1e9, "total_assets": 4e9,
                "asset_value_profile": prof, "items": []}

    def test_four_metrics_are_separately_exposed(self):
        a = asset_metrics.AssetMetricProvider(self._payload(), market_cap=1e9)
        d = a.display_model()
        for key in ("gross_conservative_asset_value", "adjusted_liquidation_value",
                    "net_interest_bearing_asset_value", "adjusted_net_cash",
                    "total_liabilities", "interest_bearing_debt",
                    # 卡片的副标题要写全公式「类现金 X − 有息负债 Y」，X 得单独给；
                    # 闸门开关状态也要给，页面据此说明「为什么这几格是空的」。
                    "near_cash", "interest_debt_valid", "liability_status"):
            self.assertIn(key, d, f"展示模型里没有 {key}")
        self.assertEqual(d["gross_conservative_asset_value"], 3e9)
        self.assertEqual(d["adjusted_liquidation_value"], 2e9)
        self.assertEqual(d["net_interest_bearing_asset_value"], 2.5e9)
        self.assertNotEqual(d["adjusted_liquidation_value"],
                            d["net_interest_bearing_asset_value"])
        # 老键名继续指向新口径，前端不用改
        self.assertEqual(d["liquidation_value"], d["adjusted_liquidation_value"])
        self.assertEqual(d["adjusted_asset_value"],
                         d["gross_conservative_asset_value"])

    def test_legacy_snapshot_is_not_read_as_liquidation_value(self):
        """没有 liquidation_model 的快照是旧口径，那个数不是清算价值。"""
        payload = self._payload()
        payload["asset_value_profile"].pop("liquidation_model")
        a = asset_metrics.AssetMetricProvider(payload, market_cap=1e9)
        self.assertFalse(a.liquidation_valid)
        self.assertIsNone(a.liquidation_value())
        self.assertIsNone(a.liquidation_to_market_cap)
        self.assertTrue(a.liquidation_status.startswith("invalid_base"),
                        a.liquidation_status)
        self.assertIn("LIQUIDATION_MODEL_V1", a.liquidation_status,
                      "要把「口径不对」和「基数不对」分开说")

    def test_failed_reconciliation_blocks_liquidation_but_not_gross(self):
        a = asset_metrics.AssetMetricProvider(
            self._payload(profile={"liabilities": {
                "reconciliation": "FAIL", "status": "invalid_base / x"}}),
            market_cap=1e9)
        self.assertFalse(a.liquidation_valid)
        self.assertIsNone(a.liquidation_value())
        self.assertIsNone(a.liquidation_to_market_cap)
        # 折价后资产不依赖负债，不能跟着一起消失
        self.assertEqual(a.gross_adjusted_assets(), 3e9)
        self.assertEqual(a.asset_value_to_market_cap, 3.0)

    def test_liquidation_component_goes_missing_when_unreconciled(self):
        """一路传到画像：分量 missing、score=None、不进归一化。"""
        broken = _metrics(assets=fake_provider(
            asset_value_ratio=1.30, liquidation_ratio=1.90,
            liability_reconciliation="FAIL"))
        block = rules.score_cigar_butt(broken, broken)
        comp = [c for c in block["components"] if c["name"] == "清算价值/市值"][0]
        self.assertTrue(comp["missing"])
        self.assertIsNone(comp["score"])
        good = _metrics(assets=fake_provider(
            asset_value_ratio=1.30, liquidation_ratio=1.90))
        ok = [c for c in rules.score_cigar_butt(good, good)["components"]
              if c["name"] == "清算价值/市值"][0]
        self.assertFalse(ok["missing"])
        self.assertGreater(ok["score"], 0.0)


# --------------------------------------------------------------------------- #
# 4. 端到端：从 fixture 走到落库指纹
# --------------------------------------------------------------------------- #
class TestEndToEnd(unittest.TestCase):
    def test_engine_profile_is_consistent_and_valid(self):
        m = eng.compute(_rows(), market_cap=1_000_000_000.0)
        prof = m["asset_value_profile"]
        self.assertEqual(prof["liquidation_model"], hc.LIQUIDATION_MODEL_V1)
        self.assertEqual(prof["liabilities"]["reconciliation"], "OK")
        self.assertEqual(prof["total_liabilities"], FIX_TOTAL_LIAB)
        self.assertAlmostEqual(
            prof["liabilities"]["parsed_total"],
            prof["liabilities"]["reported_total"], places=2)
        s = prof["scenarios"][hc.CONSERVATIVE]
        self.assertAlmostEqual(
            s["liquidation_value"],
            s["gross_adjusted_assets"] - FIX_TOTAL_LIAB, places=2)
        self.assertAlmostEqual(
            m["net_cash"]["AdjustedNetCash"],
            m["cash_tiers"]["NearCash"] - FIX_INTEREST_BEARING, places=2)

    def test_result_hash_covers_the_liability_side(self):
        """负债侧变了，指纹就得变。

        否则「整段非流动负债漏掉」那一版结果会被当成重复结果**静默丢弃**，
        界面继续显示旧数字——资产侧一个数都没变，光比资产是看不出来的。
        """
        def metrics(total_liab):
            return {"total_assets": 5.9e9, "cash_tiers": {}, "net_cash": {},
                    "asset_value_profile": {
                        "scenarios": {},
                        "liabilities": {"reported_total": total_liab}},
                    "items": []}

        self.assertNotEqual(eng._result_hash(metrics(2.5e9)),
                            eng._result_hash(metrics(1.7e9)))

    def test_unreconciled_computation_yields_no_liquidation_value(self):
        """把报表的「负债合计」改错一格，整条链必须自己收紧。"""
        rows = list(_rows())
        for i, r in enumerate(rows):
            if r["cells"] and r["cells"][0]["text"] == "负债合计":
                rows[i] = _row(r["y"], ("负债合计", 60.0),
                               ("9,999,999,999.00", 330.0),
                               ("2,525,602,334.26", 450.0))
                break
        m = eng.compute(rows, market_cap=1_000_000_000.0)
        prof = m["asset_value_profile"]
        self.assertEqual(prof["liabilities"]["reconciliation"], "FAIL")
        self.assertIsNone(prof["total_liabilities"])
        for s in hc.SCENARIOS:
            self.assertIsNone(prof["scenarios"][s]["liquidation_value"])
        a = asset_metrics.AssetMetricProvider(
            {"total_assets": m["total_assets"],
             "asset_value_profile": prof, "items": [], "market_cap": 1e9})
        self.assertIsNone(a.liquidation_to_market_cap)


if __name__ == "__main__":
    unittest.main()
