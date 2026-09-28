# -*- coding: utf-8 -*-
"""猪成本链（批 7 ``PIG_COST_CORE_V1``）：抽取 / 归一 / 派生 / 观测仓 / 隔离。

这一批的全部风险集中在**一件事**上：一个成本数被抽错口径、抽错期间、或者被
凭空造出来之后，它在界面上和正确的数**长得一模一样**。所以这里的测试几乎每一条
都在钉「不许发生的那一步」：

1. **口径**：「育肥完全成本」不许升格成「完全成本」，「断奶成本 251 元/头」
   不许当成元/公斤——两者的 scope 与 unit 不同源；
2. **期间**：同一句里同时有「2026年上半年」与「2026年6月」时，取的是**紧邻
   锚的那个**；「近期」根本不是期间；
3. **拒答**：目标、占比、提问、有口径无数字，四类各自进拒答清单并带页码与
   原因——「抽不到」与「抽到了不能用」在界面上必须分得开；
4. **派生**：单位毛利只在同期间 + 同口径 + 同单位三者都成立时相减；同行
   不足 3 家就报 ``INSUFFICIENT_PEERS``，**不凭印象预设谁成本最低**；
5. **隔离**：整条链没有一行写进评分层，``derive`` 与 ``scan`` 都是只读的。

夹具全部是真报告原文行（``tests/pig_cost_fixtures.py``），临时库全部用
``:memory:`` / ``tempfile``——**不联网、不碰真库**。
"""
import contextlib
import io
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import pig_cost_core as cc                          # noqa: E402
from research import pig_evidence as ev                           # noqa: E402
from research import pig_observations as obs                      # noqa: E402
from research import pig_premium as prem                          # noqa: E402
from research.industry import pig as PIG                          # noqa: E402
from tests import pig_cost_fixtures as fx                         # noqa: E402

MY = "002714"
XH = "000876"
DR = "001201"
TK = "002100"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    obs.ensure_schema(conn)
    return conn


def _run(key):
    """一个夹具 → ``extract`` 的结果。"""
    return cc.extract(fx.meta_for(key), fx.rows_for(key))


def _kinds(result):
    return [item["kind"] for item in result["rejected"]]


def _of(result, kind):
    return [item for item in result["rejected"] if item["kind"] == kind]


def _cost(result, metric_id=None, variant=None):
    out = [row for row in result["costs"]
           if (metric_id is None or row["metric_id"] == metric_id)
           and (variant is None or row["metric_variant"] == variant)]
    assert len(out) == 1, "期望恰好一条，拿到 %d 条：%r" % (
        len(out), [(r["metric_id"], r["metric_variant"], r["period"])
                   for r in result["costs"]])
    return out[0]


def _price(period, value, *, code=MY, scope=PIG.SCOPE_COMMODITY):
    """公司侧一条商品猪均价观测（月报口径）——``derive`` 的价格侧。"""
    return obs.Observation(
        PIG.M_PIG_SALE_PRICE, metric_variant="monthly_commodity_price",
        company_code=code, period=period, value=value, unit="CNY/kg",
        scope=scope, source_type=PIG.SRC_MONTHLY_BULLETIN, source_name="夹具来源",
        document="p" * 64, page=2, publication_date="2026-09-08",
        extraction_method="local_parse", is_direct_disclosure=True,
        status=obs.STATUS_OK, fetched_at="2026-09-08 10:00:00",
        first_seen_at="2026-09-08 10:00:00")


def _cost_obs(value, *, period="2026-06", code=MY, variant="full_cost",
              scope=PIG.SCOPE_COMMODITY,
              unit="CNY/kg", metric_id=None, document="c" * 64, status=obs.STATUS_OK,
              registered=None, extraction_confidence=None):
    """一条成本观测。``derivation`` 里的 ``cost_variant`` 是 ``derive`` 认口径的
    唯一入口——夹具必须照抽取器写的那套 ``k=v`` 串来，否则测的就不是真路径。"""
    return obs.Observation(
        metric_id or PIG.M_FULL_COST,
        metric_variant=registered or (
            "FULL_COST_COMPANY_DISCLOSED" if variant == "full_cost" else variant),
        company_code=code, period=period, value=value, unit=unit, scope=scope,
        source_type=PIG.SRC_ANNUAL_REPORT, source_name="夹具年报",
        document=document, page=14, publication_date="2026-03-28",
        extraction_method="local_parse", is_direct_disclosure=True,
        status=status, extraction_confidence=extraction_confidence,
        derivation=prem.join_derivation([("formula", "company_disclosed_cost"),
                                         ("cost_variant", variant)]),
        fetched_at="2026-09-08 10:00:00", first_seen_at="2026-09-08 10:00:00")


# --------------------------------------------------------------------------- #
# 一、抽取：四条真披露
# --------------------------------------------------------------------------- #
class TestExtraction(unittest.TestCase):

    def test_mu_2026h1_gives_the_june_full_cost(self):
        """牧原 2026H1 p11：「2026年6月生猪养殖完全成本在11.7元/kg左右」。"""
        cost = _cost(_run("my_2026h1_p11"))
        self.assertEqual(cost["metric_id"], PIG.M_FULL_COST)
        self.assertEqual(cost["metric_variant"], "FULL_COST_COMPANY_DISCLOSED")
        self.assertEqual(cost["period"], "2026-06")
        self.assertEqual(cost["scope"], PIG.SCOPE_COMMODITY)
        self.assertEqual((cost["value"], cost["unit"]), (11.7, "CNY/kg"))
        self.assertTrue(cost["is_approximate"], "原文写的是「左右」")
        self.assertLess(cost["extraction_confidence"], 1.0)
        self.assertTrue(cost["is_direct_disclosure"])
        self.assertFalse(cost["is_estimated"], "公司自己说的数不是推算")
        self.assertEqual(cost["status"], obs.STATUS_OK,
                         "近似值仍是 OK——不编上下界，也不降级成 RANGE")

    def test_mu_2025a_gives_the_year_cost_and_it_is_approximate(self):
        """牧原 2025A p14：「2025年全年生猪养殖完全成本约12元/kg」。"""
        cost = _cost(_run("my_2025a_p14"))
        self.assertEqual((cost["value"], cost["period"]), (12.0, "2025A"))
        self.assertTrue(cost["is_approximate"], "原文写的是「约」")
        self.assertIn("约", cost["paragraph"])
        # 区间期就是区间期：2025A 不许被折成 12 月或 1 月去配单月售价。
        self.assertFalse(prem.MONTH_RE.match(cost["period"]))

    def test_the_full_year_cost_target_is_not_a_disclosure(self):
        """同一个 p11 上的「全年平均11.5元/kg 的成本目标」→ 拒答，**不产出值**。

        目标是公司打算做到的事。把它当已实现的成本，会系统性高估优秀程度——
        而它在数字上看不出任何区别。
        """
        result = _run("my_2026h1_p11")
        self.assertIn("TARGET", _kinds(result))
        self.assertTrue(any("成本目标" in item["text"]
                            for item in _of(result, "TARGET")))
        self.assertEqual([row["value"] for row in result["costs"]], [11.7],
                         "只许有 6 月那一条，11.5 的目标绝不许落成观测")

    def test_the_ratio_sentences_are_not_costs(self):
        """占比不是成本：比例乘上不知道的分母还是不知道。"""
        for key in ("my_2025a_p20", "my_2025a_p5"):
            with self.subTest(key=key):
                result = _run(key)
                self.assertEqual(result["costs"], [])
                self.assertIn("RATIO", _kinds(result))

    def test_xinxiwang_one_page_carries_two_different_variants(self):
        """新希望 2025A p27：同一页上「育肥完全成本 12.2 元/公斤」与
        「断奶成本 251 元/头」——口径不同、量纲不同、期间不同。"""
        result = _run("xh_2025a_p27")
        fattening = _cost(result, PIG.M_FATTENING_COST,
                          "fattening_full_cost_per_kg")
        self.assertEqual((fattening["value"], fattening["period"]),
                         (12.2, "2025-12"))
        self.assertEqual(fattening["scope"], PIG.SCOPE_FATTENING_NORMAL_LINES)
        self.assertEqual(fattening["unit"], "CNY/kg")
        weaned = _cost(result, PIG.M_WEANED_PIGLET_COST, "weaned_piglet_cost")
        self.assertEqual((weaned["value"], weaned["period"]), (251.0, "2025A"))
        self.assertEqual((weaned["unit"], weaned["scope"]),
                         ("CNY/head", PIG.SCOPE_PIGLET))
        # 这两条**不许**并成一条：升格会把一个子集口径的数变成全口径的数。
        self.assertNotEqual(fattening["metric_id"], weaned["metric_id"])
        self.assertNotEqual(fattening["scope"], weaned["scope"])
        self.assertEqual(fattening["unit"] != weaned["unit"], True)

    def test_a_cost_word_without_a_number_is_rejected_not_guessed(self):
        """「有效降低仔猪断奶成本；」有口径词、没有数 → ``NO_VALUE``。"""
        result = _run("xh_2025a_p27")
        no_value = _of(result, "NO_VALUE")
        self.assertTrue(no_value)
        self.assertTrue(any("断奶成本" in item["text"] for item in no_value))

    def test_dongrui_and_tiankang_disclose_no_number_at_all(self):
        """东瑞与天康：只有定性的「降低养殖成本」。**0 条**，且逐条说明为什么。"""
        for key in ("dr_2025a_p28", "dr_2026h1_p21", "tk_2026h1_p24"):
            with self.subTest(key=key):
                result = _run(key)
                self.assertEqual(result["costs"], [])
                self.assertTrue(result["rejected"],
                                "抽不到也要留下「看过并且拒了」的痕迹")
                self.assertTrue(all(item["text"] for item in result["rejected"]))
                self.assertEqual(result["status"], "rejected")


# --------------------------------------------------------------------------- #
# 二、抽取：实测踩过并修好的三处回归
# --------------------------------------------------------------------------- #
class TestExtractionRegressions(unittest.TestCase):

    def test_a_sentence_split_across_two_lines_is_still_one_disclosure(self):
        """跨行窗口：口径词在 466.51、**数字在下一行** 443.09。

        逐行匹配会漏掉本批最重要的一条。这条测试就是那次实测的回归。
        """
        texts = [row["cells"][0]["text"] for row in fx.rows_for("my_2025a_p14")]
        # 先证明这条夹具**真的**是断开的：口径词那一行没有数字。
        anchor_line = [text for text in texts if "完全成本约" in text]
        self.assertEqual(len(anchor_line), 1)
        self.assertNotIn("12元/kg", anchor_line[0],
                         "夹具坏了：口径词那一行本来就不该有数字")
        cost = _cost(_run("my_2025a_p14"))
        self.assertEqual(cost["value"], 12.0)
        self.assertEqual(cost["page"], 14)

    def test_the_period_is_the_nearest_one_not_the_first_one(self):
        """同一句里有「2026年上半年」与「2026年6月」，取**紧邻锚**的那个。

        取句首那个会得到 ``2026H1``——一个看起来完全正常的期间，但它把一条
        月粒度的披露说成了半年。
        """
        sentence = fx.DOCUMENTS["my_2026h1_p11"][6][1][1]
        self.assertIn("2026年上半年", sentence)
        self.assertIn("2026年6月", sentence)
        cost = _cost(_run("my_2026h1_p11"))
        self.assertEqual(cost["period"], "2026-06")
        self.assertNotIn(cost["period"], ("2026H1", "2026年上半年"))
        self.assertEqual(prem.parse_derivation(cost["derivation"])["period_basis"],
                         "explicit", "期间是从原文读出来的，不是从报告期推的")

    def test_the_english_copy_of_the_same_report_is_parsed_once(self):
        """000876 的 2025A 在缓存里有中英两份，英文版披露日反而更晚。

        不去重就会把同一份报告解析两遍，产出两条只差 ``document`` 的观测——
        看着像两个来源在互相印证，实际是一份文件。
        """
        with mock.patch("research.pig_segment_tables.local_meta",
                        return_value=list(fx.DUPLICATE_DOCUMENTS)) as patched:
            docs = cc.local_documents(XH)
        self.assertTrue(patched.called)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["_doc_key"], "bb1aaa81a07f5054f8d0",
                         "中文优先，即便英文版披露日更晚")
        self.assertEqual(docs[0]["report_period"], "2025A")

    def test_a_question_in_an_ir_summary_is_not_a_disclosure(self):
        """p44 的「询问公司2月生猪完全成本」——是提问，不是公司给的结论。

        它的来源确实是 L1 定期报告，但**这一句不是结论**：不许产出 OK 观测，
        也不许给它贴一个 L3/L4 的级别（那会把一个提问说成一次披露）。
        """
        result = _run("xh_2025a_p44")
        self.assertEqual(result["costs"], [])
        self.assertEqual(_kinds(result), ["QUESTION"] * len(result["rejected"]))
        for item in result["rejected"]:
            self.assertNotIn("source_level", item,
                             "拒答不是观测，不许带级别")
            self.assertNotIn("value", item)

    def test_every_ratio_sentence_names_its_own_page(self):
        """四处占比是四处**不同的原文**，页码必须让人分得出来。

        合成一个「占比解析器有 bug」的错觉，比漏掉它们更贵。
        """
        seen = {}
        for key in ("my_2025a_p5", "my_2025a_p20", "dr_2025a_p28", "dr_2026h1_p21"):
            result = _run(key)
            ratio = _of(result, "RATIO")
            self.assertTrue(ratio, key)
            for item in ratio:
                seen[(key, item["page"])] = item["text"]
        self.assertEqual(sorted(seen), sorted([
            ("my_2025a_p5", 5), ("my_2025a_p20", 20),
            ("dr_2025a_p28", 28), ("dr_2026h1_p21", 21)]))
        self.assertEqual(len(set(seen.values())), 4, "四处原文不许长得一样")

    def test_a_sentence_is_never_both_extracted_and_rejected(self):
        """同一条事实不许既在「抽到」又在「拒答」里。

        窗口是从「句子起于本行」认领的，一行中间被切开时同一句会从两个窗口各
        出来一次——长的那个收了、短的那个被拒。读的人只会以为解析器在自相矛盾。
        """
        for key in fx.DOCUMENTS:
            result = _run(key)
            for item in result["rejected"]:
                body = item["text"].rstrip("…")
                if len(body) < 12:
                    continue
                for cost in result["costs"]:
                    self.assertNotIn(body, cost["paragraph"],
                                     "%s：被拒的文本是已收下那条的子串" % key)


# --------------------------------------------------------------------------- #
# 三、抽取：语料里没有、代码路径必须有的形态
# --------------------------------------------------------------------------- #
class TestSyntheticShapes(unittest.TestCase):

    def test_a_relative_time_word_is_not_a_period(self):
        """「近期」不是期间——`INSUFFICIENT_PERIOD`，**不产出行**。

        拿一个不知道哪一期的成本去配某个月的售价，那个差额没有任何意义。
        """
        result = _run("synth_vague_period")
        self.assertEqual(result["costs"], [])
        self.assertEqual(_kinds(result), ["INSUFFICIENT_PERIOD"])
        self.assertIn("近期", result["rejected"][0]["why"])

    def test_a_range_keeps_the_real_bounds_and_leaves_the_value_empty(self):
        """区间披露：存真实上下界，``value`` 留空。**不许伪造精确值。**"""
        result = _run("synth_range")
        cost = _cost(result)
        self.assertIsNone(cost["value"])
        self.assertEqual((cost["lower_bound"], cost["upper_bound"]), (11.5, 12.0))
        self.assertEqual(cost["status"], obs.STATUS_RANGE)
        self.assertEqual(cost["unit"], "CNY/kg")

    def test_a_rounded_point_value_is_not_turned_into_a_range(self):
        """近似（「约 12」）与区间是两回事：近似的 ``value`` 有值、边界为空。

        给「约 12」编一个 11.5~12.5 的区间是**我们**造的数，公司没这么说。
        """
        cost = _cost(_run("my_2025a_p14"))
        self.assertEqual(cost["value"], 12.0)
        self.assertIsNone(cost["lower_bound"])
        self.assertIsNone(cost["upper_bound"])
        self.assertEqual(cost["status"], obs.STATUS_OK)

    def test_yuan_per_jin_is_converted_once_and_yuan_per_head_is_not(self):
        """元/斤 → 元/公斤走 ×2；元/头**不换算**。两条都留下换算式。"""
        converted = _cost(_run("synth_yuan_per_jin"))
        pairs = prem.parse_derivation(converted["derivation"])
        self.assertEqual(converted["value"], 11.7)
        self.assertEqual((pairs["raw_value"], pairs["raw_unit"]), ("5.8500", "元/斤"))
        self.assertEqual(pairs["normalized_unit"], "CNY/kg")
        self.assertIn("× 2", pairs["conversion_formula"])
        kept = _cost(_run("xh_2025a_p27"), PIG.M_WEANED_PIGLET_COST)
        pairs = prem.parse_derivation(kept["derivation"])
        self.assertEqual(kept["value"], 251.0, "元/头原样保留，不强行换算")
        # 单位的**写法**归一（元/头 → CNY/head），数字**一个不动**：倍率是 1.0。
        self.assertEqual(pairs["raw_unit"], "元/头")
        self.assertEqual(pairs["normalized_unit"], "CNY/head")
        self.assertEqual(pairs["raw_value"], pairs["normalized_value"])
        self.assertIn("未换算", pairs["conversion_formula"])

    def test_a_bare_farming_cost_word_is_not_a_scope(self):
        """只说「养殖成本」，没有主体词也没有范围限定 → 不构成可比口径。"""
        result = _run("synth_bare_scope_word")
        self.assertEqual(result["costs"], [])
        self.assertEqual(_kinds(result), ["NO_ANCHOR"])

    def test_a_plan_or_ratio_word_far_from_cost_says_so(self):
        """「计划」离成本二字很远、句子里又没有口径词 → ``NO_ANCHOR``。

        判定顺序原本是「先看目标词、再看口径词」，于是任何一句带「计划」「比例」
        的话都被记成「这是目标不是事实」——而实测新希望那份年报里，119 条拒答
        有 10 条是这么来的，它们一个字都没提成本口径。拒答清单是交付材料，
        理由错了就等于没交付。
        """
        result = _run("synth_offtopic_plan_word")
        self.assertEqual(result["costs"], [])
        self.assertEqual(_kinds(result), ["NO_ANCHOR"],
                         "不能记成 TARGET——这句里没有成本口径词")

    def test_a_far_target_word_still_blocks_the_value(self):
        """口径词在、目标词也在（只是离得远）→ **仍然拦住**，理由要照实说。

        反方向的错更贵：把「公司计划把生猪养殖完全成本控制在 11.7 元/kg」
        收成一条已实现的成本，就是一个看起来完全正常的错数。
        """
        result = _run("synth_far_target_word")
        self.assertEqual(result["costs"], [], "目标是打算做到的事，不是已实现的成本")
        self.assertEqual(_kinds(result), ["TARGET"])
        self.assertIn("离成本二字很远", _of(result, "TARGET")[0]["why"])
        self.assertIn("宁可拒答", _of(result, "TARGET")[0]["why"])

    def test_a_ratio_word_attached_to_cost_keeps_its_own_wording(self):
        """贴着成本的占比句（「占营业成本的比例约在 55%-65%」）理由不同。

        两种都拦，但「公司拿占比糊弄」与「恰好同句」不是一回事——写成同一句
        话，读拒答清单的人就分不出来。
        """
        why = _of(_run("my_2025a_p20"), "RATIO")[0]["why"]
        self.assertIn("比例乘上不知道的分母", why)
        self.assertNotIn("离成本二字很远", why)

    def test_no_anchor_leaves_its_scope_undecided(self):
        """7 个锚**每一个**都声明了 scope。

        这条不是形式主义：``_classify`` 里有一条「有值但 scope 是 None → 拒答」
        的守卫。守卫留着是对的（新加锚忘了写 scope 时它必须挡住），但如果哪天
        有人加了一个不写 scope 的锚，这里会先响。
        """
        for anchor in cc.ANCHORS:
            self.assertIsNotNone(anchor.scope, anchor.variant)
            self.assertTrue(anchor.why, "每个锚都要回答「为什么是它」")


# --------------------------------------------------------------------------- #
# 四、口径词表与文档覆盖
# --------------------------------------------------------------------------- #
class TestVocabularyAndCoverage(unittest.TestCase):

    def test_the_nine_variants_are_all_declared_and_none_is_mergeable(self):
        """§二 的九个口径**全部**登记，互不可替代的声明逐条在。"""
        self.assertEqual(len(cc.COST_VARIANTS), 9)
        self.assertEqual([row[0] for row in cc.COST_VARIANTS],
                         ["full_cost", "fattening_full_cost", "breeding_full_cost",
                          "cash_cost", "piglet_cost", "weaned_piglet_cost",
                          "feed_cost", "non_feed_cost", "other_cost"])
        for variant, label, unit, note in cc.COST_VARIANTS:
            self.assertTrue(label and note, variant)
            self.assertIn(unit, (cc.UNIT_CNY_KG, cc.UNIT_CNY_HEAD), variant)
        # 只有真有数据的三个建了格子——「建格子」与「进词表」是两件事。
        self.assertEqual(len(cc.REGISTERED_COST_GRID), 3)
        self.assertTrue(set(row[0] for row in cc.REGISTERED_COST_GRID)
                        <= set(row[0] for row in cc.COST_VARIANTS))

    def test_the_extractor_registry_and_the_read_side_registry_do_not_drift(self):
        """两份写法必须对齐：``pig_evidence.COST_GRIDS`` ↔ ``REGISTERED_COST_GRID``。

        层级方向逼出了两份（读侧不该 import 抽取器），而两份只有在**测试会响**
        的前提下才可接受。三个方向各查一遍：
        ① 抽得到的格子必须在页面上有行；② 页面上的行必须是评分层在册的 variant；
        ③ 页面上的 variant 不许出现重名格子。
        """
        declared = {(metric_id, variant) for metric_id, variant in ev.COST_GRIDS}
        registered = {(row[1], row[2]) for row in cc.REGISTERED_COST_GRID}
        self.assertTrue(registered <= declared,
                        "抽取器建了格子，页面上却没有这一行：%r" % (
                            sorted(registered - declared),))
        self.assertEqual(len(declared), len(ev.COST_GRIDS), "COST_GRIDS 里有重名格")
        for metric_id, variant in declared:
            definition = PIG.METRIC_INDEX.get(metric_id)
            self.assertIsNotNone(definition, metric_id)
            self.assertIn(variant, definition.variant_names,
                          "页面要显示的 variant 在评分层没有在册：%s/%s"
                          % (metric_id, variant))

    def test_contract_errors_is_clean(self):
        """本模块的配置自检：注册的 variant / 单位 / scope 必须真在评分层在册。"""
        self.assertEqual(cc.contract_errors(), [])

    def test_the_document_coverage_names_the_classes_with_no_provider(self):
        """八类文档逐类报数：「本地零份」与「本地零份 + 根本没有 provider」是两件事。

        后者不是补缓存能解决的——它要的是接一个数据源，而那是另一批的事。
        """
        with mock.patch("research.pig_segment_tables.local_meta", return_value=[]):
            rows = cc.coverage((MY, DR, TK, XH))
        self.assertEqual([row["label"] for row in rows], [
            "定期报告", "月度经营简报", "投资者关系活动记录表", "业绩说明会",
            "ESG 报告", "公司公告全文", "行业数据", "第三方研究"])
        no_provider = [row for row in rows if not row["has_provider"]]
        self.assertEqual([row["label"] for row in no_provider],
                         ["投资者关系活动记录表", "业绩说明会", "ESG 报告",
                          "公司公告全文", "第三方研究"])
        for row in no_provider:
            self.assertIsNone(row["provider"])
        with_provider = [row for row in rows if row["has_provider"]]
        self.assertEqual([row["provider"] for row in with_provider],
                         ["reports", "pig_bulletins", "pig_industry_series"])

    def test_the_coverage_command_needs_no_database(self):
        """``--coverage`` 在连库**之前**就返回——它是一个报告，不是一次写库。"""
        buf = io.StringIO()
        with mock.patch("research.pig_segment_tables.local_meta", return_value=[]):
            with contextlib.redirect_stdout(buf):
                code = cc.main(["--coverage", "--code", MY])
        self.assertEqual(code, 0)
        self.assertIn("无 provider", buf.getvalue())

    def test_the_extractor_does_not_reach_into_the_scoring_modules(self):
        """抽取器只取数，不许碰评分侧的三个模块（照 ``test_pig_industry`` 的写法）。"""
        source = (ROOT / "research" / "pig_cost_core.py").read_text(encoding="utf-8")
        for name in ("cyclical_slots", "pig_reconciliation", "pig_shadow"):
            self.assertNotIn("import %s" % name, source)
            self.assertNotIn("from .%s" % name, source)


class _Handle:
    """把库递给 CLI 时套一层：``close()`` 只记账，不真关。

    ``main`` 在 ``finally`` 里关掉**它自己开的**库——这是对的，要断言这一点。
    但真关掉之后测试就没法再看里面写了什么（``sqlite3.ProgrammingError:
    Cannot operate on a closed database``，这条错误第一次跑就是这么来的）。
    所以关的动作记账，读的动作原样透下去。
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "close_calls", 0)

    def close(self):
        object.__setattr__(self, "close_calls", self.close_calls + 1)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_real"), name, value)


class TestCli(unittest.TestCase):

    def test_the_dry_run_opens_the_database_and_writes_nothing(self):
        """``--code`` 这条路要**真能跑**。

        CLI 取库那一行写错过一次（``from . import research_db``——这个模块名
        根本不存在），而当时的测试全部绕过 ``main`` 直接调函数，所以它一路绿到
        命令行上才炸。取库用桩、真缓存照读：这条路里唯一该被替换掉的只有库。
        """
        conn = _conn()
        self.addCleanup(conn.close)
        handle = _Handle(conn)
        out = io.StringIO()
        with mock.patch("research.db.connect", return_value=handle) as opener:
            with contextlib.redirect_stdout(out):
                code = cc.main(["--code", MY])
        self.assertTrue(opener.called, "干跑也要连库——派生读的就是它")
        self.assertEqual(code, 0)
        self.assertIn(MY, out.getvalue())
        self.assertEqual(handle.close_calls, 1, "自己开的库要自己关")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM pig_metric_observation").fetchone()[0], 0,
            "没给 --apply 就一行都不许写")

    def test_the_apply_flag_is_the_only_thing_that_writes(self):
        """``--apply`` 才写，且写的是**抽取器的产出**（经同一个观测入口）。"""
        if not cc.local_documents(MY):
            self.skipTest("本机没有 %s 的报告缓存，这条端到端路径跑不了" % MY)
        conn = _conn()
        self.addCleanup(conn.close)
        handle = _Handle(conn)
        out = io.StringIO()
        with mock.patch("research.db.connect", return_value=handle):
            with contextlib.redirect_stdout(out):
                code = cc.main(["--code", MY, "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("落库: 新增", out.getvalue())
        landed = obs.load(conn, code=MY)
        self.assertTrue(landed)
        for row in landed:
            self.assertEqual(row.company_code, MY)
            self.assertEqual(row.extraction_method, "local_parse")
        self.assertEqual(obs.check_errors(landed), [])


# --------------------------------------------------------------------------- #
# 五、派生：单位毛利 / 成本优势
# --------------------------------------------------------------------------- #
class TestDerivation(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def _land(self, observations):
        obs.append(self.conn, observations)

    def _margin(self, result):
        out = [row for row in result["records"]
               if row.metric_id == PIG.M_UNIT_MARGIN]
        assert len(out) == 1, result
        return out[0]

    def test_unit_margin_only_when_period_and_scope_and_unit_all_match(self):
        """牧原 2026-06：9.69 − 11.7 = −2.01 元/公斤。

        成本侧来自**真夹具**（抽取器产出、``to_observations`` 入库），不是手搓的
        字典——否则测的是「我以为 deri推导 认什么」，不是它真认什么。
        """
        costs = cc.to_observations(_run("my_2026h1_p11")["costs"])
        self._land(costs + [_price("2026-06", 9.69)])
        result = cc.derive(self.conn, MY)
        record = self._margin(result)
        self.assertEqual(record.value, -2.01)
        self.assertEqual(record.period, "2026-06")
        self.assertEqual(record.scope, PIG.SCOPE_COMMODITY)
        self.assertEqual(record.unit, PIG.METRIC_INDEX[PIG.M_UNIT_MARGIN]
                         .unit_of("cny_per_kg"))
        self.assertTrue(record.is_estimated, "相减出来的数不是披露值")
        self.assertTrue(record.is_approximate, "成本侧标了近似，派生值也带着")
        pairs = prem.parse_derivation(record.derivation)
        self.assertEqual(pairs["cost_variant"], "full_cost")
        self.assertEqual(pairs["price_metric"], PIG.M_PIG_SALE_PRICE)
        self.assertEqual(pairs["formula"],
                         "company_sale_price-minus-comparable_cost")
        # 两个来源的哈希都在——这条数的每一个输入都点得到。
        hashes = {row.observation_hash for row in costs}
        self.assertIn(pairs["cost_observation_hash"], hashes)
        self.assertTrue(pairs["price_observation_hash"])

    def test_an_interval_period_cost_is_never_paired_with_a_month(self):
        """2025A 的成本与任何单月售价都不同期 → 跳过，**不产出行**。"""
        costs = cc.to_observations(_run("my_2025a_p14")["costs"])
        self._land(costs + [_price("2025-12", 12.0)])
        result = cc.derive(self.conn, MY)
        self.assertEqual([row for row in result["records"]
                          if row.metric_id == PIG.M_UNIT_MARGIN], [])
        self.assertTrue(any("区间期" in item["why"] for item in result["skipped"]))

    def test_a_subset_scope_cost_is_never_subtracted_from_a_full_price(self):
        """子集口径（正常运营场线）的成本不许与全口径商品猪均价相减。

        差额是「被限定的差」，而那个限定在数字上看不出来。本批**不建**
        ``fattening_unit_margin``。
        """
        costs = cc.to_observations(_run("xh_2025a_p27")["costs"])
        self._land(costs + [_price("2025-12", 12.0, code=XH)])
        result = cc.derive(self.conn, XH)
        self.assertEqual(result["records"], [])
        whys = " ".join(item["why"] for item in result["skipped"])
        self.assertIn("口径不可比", whys)
        self.assertIn("fattening_unit_margin", whys)

    def test_a_conflated_cost_side_is_not_picked_apart(self):
        """成本侧同级冲突 → 不派生，理由写进 ``skipped``。**不挑一个。**"""
        same = dict(period="2026-06", scope=PIG.SCOPE_COMMODITY)
        self._land([
            _cost_obs(value=11.7, document="a" * 64, **same),
            _cost_obs(value=12.4, document="b" * 64, **same),
            _price("2026-06", 9.69)])
        result = cc.derive(self.conn, MY)
        self.assertEqual(result["records"], [])
        self.assertTrue(any("conflict" in item["why"] for item in result["skipped"]))

    def test_cost_advantage_needs_three_peers_and_never_assumes_one(self):
        """同行 3 家以上才算得出中位数；**不凭印象预设谁成本最低**。"""
        for index, value in enumerate((12.5, 12.8, 12.2)):
            self._land([_cost_obs(value=value, code="90000%d" % index,
                                  document=("%d" % index) * 64)])
        self._land([_cost_obs(value=11.7)])
        result = cc.derive(self.conn, MY)
        advantage = [row for row in result["records"]
                     if row.metric_id == PIG.M_COST_ADVANTAGE]
        self.assertEqual(len(advantage), 1)
        record = advantage[0]
        # 中位数 12.5 − 公司 11.7 = 0.8 元/公斤；正值表示公司**低于**同行。
        self.assertEqual(record.value, 6.4)
        self.assertEqual(record.unit, PIG.METRIC_INDEX[PIG.M_COST_ADVANTAGE]
                         .unit_of("peer_median_deviation"))
        pairs = prem.parse_derivation(record.derivation)
        self.assertEqual(pairs["peer_count"], "3")
        self.assertEqual(pairs["peer_median_cost"], "12.5000")
        self.assertEqual(pairs["cost_gap_cny_per_kg"], "0.8000")

    def test_two_peers_are_not_a_median_saying_nothing(self):
        """两家的「中位数」就是均值——这一格用中位数正因为分布右偏。"""
        for index, value in enumerate((12.5, 12.8)):
            self._land([_cost_obs(value=value, code="90000%d" % index,
                                  document=("%d" % index) * 64)])
        self._land([_cost_obs(value=11.7)])
        result = cc.derive(self.conn, MY)
        self.assertEqual([row for row in result["records"]
                          if row.metric_id == PIG.M_COST_ADVANTAGE], [])
        whys = " ".join(item["why"] for item in result["skipped"])
        self.assertIn("2 家 < 3", whys)
        self.assertIn("INSUFFICIENT_PEERS", whys)

    def test_the_subset_scope_is_kept_out_of_the_peer_pool(self):
        """四家都披露同一个子集口径 → 同行池仍然是**空的**，报口径不足。

        子集口径硬性排除出池（裁定 3）：拿「正常运营场线」的成本去和全口径的
        成本排中位数，比的是一个被限定的数和一个没被限定的数。
        """
        subset = dict(metric_id=PIG.M_FATTENING_COST,
                      variant="fattening_full_cost",
                      registered="fattening_full_cost_per_kg",
                      scope=PIG.SCOPE_FATTENING_NORMAL_LINES,
                      unit="CNY/kg", period="2025-12")
        for index, value in enumerate((12.2, 12.4, 12.6)):
            self._land([_cost_obs(value=value, code="90000%d" % index,
                                  document=("%d" % index) * 64, **subset)])
        self._land([_cost_obs(value=12.2, **subset)])
        result = cc.derive(self.conn, MY)
        self.assertEqual([row for row in result["records"]
                          if row.metric_id == PIG.M_COST_ADVANTAGE], [])
        whys = " ".join(item["why"] for item in result["skipped"])
        self.assertIn("0 家 < 3", whys)
        self.assertIn("硬性排除出池", whys)

    def test_derivation_is_read_only(self):
        """``derive`` 一行都不写：跑前跑后行数逐位相同。"""
        costs = cc.to_observations(_run("my_2026h1_p11")["costs"])
        self._land(costs + [_price("2026-06", 9.69)])
        before = self.conn.execute(
            "SELECT COUNT(*) FROM pig_metric_observation").fetchone()[0]
        cc.derive(self.conn, MY)
        after = self.conn.execute(
            "SELECT COUNT(*) FROM pig_metric_observation").fetchone()[0]
        self.assertEqual(before, after)

    def test_only_the_cli_can_write_and_only_when_asked(self):
        """整个模块**只有一个** ``obs.append`` 调用点，且只在 ``--apply`` 分支里。

        「dry-run 不写库」如果靠调用方自觉，它迟早会变成一句假话。
        """
        source = (ROOT / "research" / "pig_cost_core.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("obs.append("), 1)
        self.assertIn("if apply:", source)
        apply_at = source.index("if apply:")
        self.assertGreater(source.index("obs.append("), apply_at,
                           "唯一的写入必须在 --apply 分支之内")

    def test_a_company_disclosed_cost_never_becomes_the_canonical_variant(self):
        """``canonical_full_cost``（``COMPLETE_COST_PER_KG``）**仍然 missing**。

        公司自报口径与统一口径回答的不是同一个问题，不许升格。所以这一批之后
        「完全成本」那一格的**正身**依然是空的——这是结果，不是缺陷。
        """
        costs = cc.to_observations(_run("my_2026h1_p11")["costs"])
        variants = {row.metric_variant for row in costs}
        self.assertEqual(variants, {"FULL_COST_COMPANY_DISCLOSED"})
        self.assertNotIn(cc.CANONICAL_VARIANT, variants)
        self._land(costs + [_price("2026-06", 9.69)])
        cc.derive(self.conn, MY)
        landed = {row.metric_variant for row in obs.load(self.conn, code=MY)}
        self.assertNotIn(cc.CANONICAL_VARIANT, landed,
                         "派生也不许顺手造一个正身出来")


# --------------------------------------------------------------------------- #
# 六、观测仓：批 7 新加的两列
# --------------------------------------------------------------------------- #
class TestObservationColumns(unittest.TestCase):

    def test_the_two_new_columns_are_added_idempotently(self):
        """老库（没有这两列）跑一次 ``ensure_schema`` 就长出来，且默认值是真值。

        ``CREATE TABLE IF NOT EXISTS`` 对已存在的表一个字都不改——老库必须靠
        ALTER。跑两次结果必须一样（``load`` 的第一步就会调它）。
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(obs.SCHEMA)
        # 把表还原成「批 6 的库」：删掉批 7 才有的两列。
        for column in ("extraction_confidence", "is_approximate"):
            conn.execute("ALTER TABLE pig_metric_observation DROP COLUMN %s"
                         % column)
        conn.commit()
        old = {row[1] for row in conn.execute(
            "PRAGMA table_info(pig_metric_observation)")}
        self.assertNotIn("is_approximate", old)
        conn.execute(
            "INSERT INTO pig_metric_observation (observation_hash, metric_id,"
            " metric_variant, subject, period, value, source_level, source_type,"
            " extraction_method, status, conflict_group_id, first_seen_at)"
            " VALUES ('h', 'full_cost', 'FULL_COST_COMPANY_DISCLOSED', '002714',"
            " '2026-06', 11.7, 'L1', 'annual_report', 'local_parse', 'OK',"
            " 'g', 't')")
        conn.commit()
        obs.ensure_schema(conn)
        obs.ensure_schema(conn)   # 幂等：跑几次都一样
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(pig_metric_observation)")}
        self.assertTrue({"is_approximate", "extraction_confidence"} <= columns)
        row = conn.execute("SELECT * FROM pig_metric_observation").fetchone()
        self.assertEqual(row["is_approximate"], 0, "老库里的行不是「近似」，是 0")
        self.assertIsNone(row["extraction_confidence"])

    def test_the_hash_covers_the_approximation_flag(self):
        """「公司说约 12」与「公司说 12」是两次不同的披露，不许去重成一条。

        ``observation_hash`` 不覆盖它的话，后落库的那条会被 ``ON CONFLICT``
        吃掉，而库里留下的那条看着完全正常。
        """
        base = dict(metric_variant="FULL_COST_COMPANY_DISCLOSED", company_code=MY,
                    period="2025A", value=12.0, unit="CNY/kg",
                    scope=PIG.SCOPE_COMMODITY, source_type=PIG.SRC_ANNUAL_REPORT,
                    is_direct_disclosure=True, status=obs.STATUS_OK)
        exact = obs.Observation(PIG.M_FULL_COST, **base)
        approx = obs.Observation(PIG.M_FULL_COST, is_approximate=True,
                                 extraction_confidence=0.9, **base)
        self.assertNotEqual(exact.observation_hash, approx.observation_hash)
        conn = _conn()
        self.addCleanup(conn.close)
        added, dupes = obs.append(conn, [exact, approx])
        self.assertEqual((added, dupes), (2, 0))
        self.assertEqual(len(obs.load(conn, code=MY)), 2)

    def test_the_preferred_branches_all_carry_the_same_keys(self):
        """三个分支给出**同一套键**：缺键的那个会逼消费方写第二套读取路径。"""
        rows = [_cost_obs(value=None, status=obs.STATUS_INSUFFICIENT_SCOPE)]
        payloads = obs.preferred(rows)
        self.assertEqual(len(payloads), 1)
        valueless = next(iter(payloads.values()))
        conflict_rows = [_cost_obs(value=11.7, document="a" * 64,
                                   extraction_confidence=1.0),
                         _cost_obs(value=12.4, document="b" * 64,
                                   extraction_confidence=1.0)]
        conflicted = next(iter(obs.preferred(conflict_rows).values()))
        winner = next(iter(obs.preferred(conflict_rows[:1]).values()))
        self.assertEqual(conflicted["status"], obs.STATUS_CONFLICT)
        self.assertIsNone(conflicted["value"])
        for payload in (valueless, conflicted, winner):
            self.assertTrue(set(obs._PREFERRED_FIELDS) <= set(payload))
        # 三支的键集**完全相同**：差一个键就会逼消费方写第二套读取路径。
        self.assertEqual(set(valueless), set(conflicted))
        self.assertEqual(set(conflicted), set(winner))
        # 批 7 那两个新键在两支「没有获胜值」的路上是**显式传出去**的
        # （骨架给的是 None），所以无值那一支也要带着它自己的那条观测的值。
        self.assertTrue(valueless["is_approximate"] is False)
        self.assertEqual(winner["extraction_confidence"], 1.0)

    def test_an_approximation_without_a_lowered_confidence_is_reported(self):
        """标了近似却没降置信是**配置漏写**，不是数据缺失——它该响。"""
        bad = obs.Observation(PIG.M_FULL_COST, metric_variant="FULL_COST_COMPANY_DISCLOSED",
                              company_code=MY, period="2025A", value=12.0,
                              unit="CNY/kg", scope=PIG.SCOPE_COMMODITY,
                              source_type=PIG.SRC_ANNUAL_REPORT,
                              is_direct_disclosure=True, status=obs.STATUS_OK,
                              is_approximate=True, extraction_confidence=1.0)
        errors = obs.check_errors([bad])
        self.assertEqual(len(errors), 1)
        self.assertIn("没降下来", errors[0][1])
        bad.extraction_confidence = 0.9
        self.assertEqual(obs.check_errors([bad]), [])

    def test_the_two_new_columns_survive_a_round_trip(self):
        """``append → load`` 往返：近似标记与抽取置信度原样回来。"""
        conn = _conn()
        self.addCleanup(conn.close)
        observations = cc.to_observations(_run("my_2026h1_p11")["costs"])
        obs.append(conn, observations)
        loaded = obs.load(conn, code=MY)
        self.assertEqual(len(loaded), 1)
        row = loaded[0]
        self.assertTrue(row.is_approximate)
        self.assertEqual(row.extraction_confidence, 0.9)
        self.assertTrue(row.to_dict()["is_approximate"])
        self.assertEqual(row.to_dict()["extraction_confidence"], 0.9)
        # 抽取出来的观测本身就自洽——落库之前不许有配置漏写。
        self.assertEqual(obs.check_errors(observations), [])


# --------------------------------------------------------------------------- #
# 七、隔离：这一批一个字都没进评分层
# --------------------------------------------------------------------------- #
class TestScoringIsolation(unittest.TestCase):

    def test_the_new_metric_is_registered_but_not_consumed(self):
        """批 7 的格子**只登记、不消费**：没有因子读它、没有曲线、没有解析器。"""
        self.assertIn(PIG.M_WEANED_PIGLET_COST, PIG.METRIC_IDS)
        self.assertNotIn(PIG.M_WEANED_PIGLET_COST, PIG.FACTOR_METRICS,
                         "加因子就是改评分——本批不做")
        self.assertNotIn(PIG.M_WEANED_PIGLET_COST, PIG.RESOLVER_METRICS)
        self.assertNotIn(PIG.M_CASH_COST, PIG.FACTOR_METRICS)
        self.assertNotIn(PIG.M_FATTENING_COST, PIG.FACTOR_METRICS)
        self.assertEqual(PIG.rules.RULES_V1["pig"].get("factor_curves"), {},
                         "曲线为空 = 任何读数都进不了分母")

    def test_the_cost_metric_ids_are_the_declared_ones_and_nothing_else(self):
        """``COST_METRIC_IDS`` 与注册表必须一一对应——多一个就会多读一批观测，
        少一个就会让派生静默地看不到它。"""
        self.assertEqual(sorted(cc.COST_METRIC_IDS),
                         sorted({row[1] for row in cc.REGISTERED_COST_GRID}))

    def test_the_extractor_only_ever_produces_registered_metric_ids(self):
        """抽出来的每一条都落在在册的 metric_id + variant 上。

        这是「口径词表 → 评分层网格」这条链的**唯一**出口：注册表里没有的口径
        一律进拒答（``NOT_REGISTERED``），不许落成一条没有归属的观测。
        """
        allowed = {(row[1], row[2]) for row in cc.REGISTERED_COST_GRID}
        for key in fx.DOCUMENTS:
            with self.subTest(key=key):
                for cost in _run(key)["costs"]:
                    self.assertIn((cost["metric_id"], cost["metric_variant"]),
                                  allowed, key)


if __name__ == "__main__":
    unittest.main()
