# -*- coding: utf-8 -*-
"""「周期位置」子分量的露出面：只进主记录、不进快照，以及行业组合标记。

这一轮用户问的是「我没看到对于猪企有什么特别的指标，也没有标记」。根因有三层，
其中第三层是技术性的：``cyclical_position`` 是**模板**分量，而 ``template_components``
（rules.py）对它**只取分数**，所以那 4 个子分量从来没被序列化到任何地方——连改动前
就存在的三个分位也一次没显示过。

修法是给**主记录那一份**补一层明细。本文件里最重的一组断言就是钉住「快照没被这层
明细污染」，因为做错的方式非常隐蔽：``_persist`` 里 ``snapshot["category_scores"]``
**就是** ``fin["components"]`` 这个 list，元素也是同一批 dict——就地加一个键会同时
改掉快照，让 ``result_hash`` 与 ``db.add_snapshot_if_changed`` 的逐字段比较同时判定
「变了」，于是每只走周期模板的股票白加一行**分数一模一样**的快照，还把 ``delta_score``
抹成 0。
"""
import copy
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.db as research_db  # noqa: E402
from research import audit_job, engine, industry_margin, pig_core, rules  # noqa: E402
from tests.test_research import _SYNTH_QUOTE, _synth_fin  # noqa: E402

#: 快照里模块级一行的**完整**键集（shape 见 rules.final_score 的 detail 循环）。
#: 写死在这里是刻意的：上游一旦把子明细塞进元素，这条立刻红。
MODULE_KEYS = {"key", "weight", "template_weight", "score", "missing",
               "raw", "raw_max", "coverage", "status"}

PIG = "002714"          # 牧原，在组合里
NON_PIG = "002129"      # TCL中环，走同一个周期模板但不在组合里


class TestBreakdownStaysOutOfSnapshots(unittest.TestCase):
    """子分量是**纯展示增补**：主记录有，快照一个字节没有。"""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)
        self.m, self.period, self.industry = engine.build_metrics(
            PIG, _SYNTH_QUOTE, _synth_fin("养殖业"))
        self.result = engine._run_analysis(self.m)
        self.assertEqual(self.result["final"]["template"], "周期价值型",
                         "合成数据没走到周期价值型，下面钉的快照全是空转")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _persist(self):
        engine._persist(self.conn, PIG, _SYNTH_QUOTE, self.m, self.result,
                        self.period, self.industry, False)

    def _snapshot_blobs(self):
        return [r[0] for r in self.conn.execute(
            "SELECT category_scores FROM research_snapshots"
            " WHERE stock_code=? ORDER BY id", (PIG,))]

    def _record_rows(self, code=PIG):
        raw = research_db.get_stock(self.conn, code)["category_scores_json"]
        return json.loads(raw)["components"]

    def test_the_snapshot_column_is_exactly_fin_components(self):
        """最硬的一条：快照那一列 == 改动前的 ``fin["components"]``，逐字节。"""
        before = copy.deepcopy(self.result["final"]["components"])
        self._persist()
        blobs = self._snapshot_blobs()
        self.assertEqual(len(blobs), 1)
        self.assertEqual(json.loads(blobs[0]), before,
                         "快照那一列被本次的展示增补污染了")

    def test_module_rows_keep_exactly_the_old_key_set(self):
        self._persist()
        for el in json.loads(self._snapshot_blobs()[0]):
            self.assertEqual(set(el), MODULE_KEYS, el.get("key"))

    def test_fin_is_not_mutated_in_place(self):
        """专抓「浅拷贝 list、没拷贝元素」那个坑——就地赋值会连快照一起改掉。"""
        before = json.dumps(self.result["final"], ensure_ascii=False, sort_keys=True)
        self._persist()
        self.assertEqual(
            json.dumps(self.result["final"], ensure_ascii=False, sort_keys=True),
            before, "result['final'] 被就地改了")

    def test_the_record_carries_the_four_sub_components(self):
        """正对照：没有这一条，上面三条在功能根本没做时也是绿的。"""
        self._persist()
        row = next(c for c in self._record_rows() if c["key"] == "cyclical_position")
        subs = row.get("components")
        self.assertIsNotNone(subs, "主记录里没有子分量，概览那个展开箭头会是空的")
        self.assertEqual([s["name"] for s in subs],
                         ["利润分位", "毛利率分位", "PB分位", "行业盈利状态"])
        self.assertEqual([s["max"] for s in subs], [30, 25, 25, 20])

    def test_the_extra_detail_does_not_break_snapshot_dedup(self):
        """主记录多了明细，快照去重照旧——这正是「挪到上游去」会破的那一条。"""
        self._persist()
        self._persist()
        self.assertEqual(len(self._snapshot_blobs()), 1,
                         "展示用明细把快照去重挤破了（第二遍又写了一行）")

    def test_a_result_without_cyclical_position_does_not_crash(self):
        """``tests/test_router.py`` 的 ``_fake_result`` 就是这种形状（没有这个键）。"""
        fin = {"components": [{"key": "valuation", "score": 80.0, "weight": 1.0,
                               "missing": False}],
               "score": 80.0, "completeness": 1.0, "template": "通用价值型",
               "template_source": "route", "template_model": None}
        result = {"type": {"primary": "价值型", "confidence": 0.9}, "final": fin,
                  "risk": {"flags": [], "level": "GREEN"}, "attributes": {}, "route": {}}
        engine._persist(self.conn, "600000", {"price": 1.0}, self.m, result,
                        "2026-06-30", "养殖业", False)
        rows = self._record_rows("600000")
        self.assertEqual(rows, fin["components"],
                         "没有子分量时主记录也必须与 fin 原样一致")


class TestCohortMark(unittest.TestCase):
    """组合标记是**纯字典查表**——它出现在列表的每一行，代价必须为零。"""

    def test_mark_never_reads_the_report_cache(self):
        """``load()`` 会 glob 目录 + 逐个 PDF stat；27 行就是 27 次目录扫描。"""
        with patch.object(industry_margin, "load",
                          side_effect=AssertionError("列表标记不该去读 data/reports")):
            self.assertEqual(industry_margin.cohort_mark(PIG),
                             {"cohort": "pig", "cohort_label": "猪企"})

    def test_non_member_returns_an_empty_dict(self):
        """空 dict 而不是 {"cohort": None}：调用方 update 之后 payload 逐字节不变。"""
        self.assertEqual(industry_margin.cohort_mark("600741"), {})

    def test_the_mark_survives_the_audit_gate(self):
        """「属于哪个组合」是**身份事实**，不是评分结论——门禁清的是能不能看分数。

        门禁按 :data:`engine.AUDIT_SUPPRESSED_FIELDS` **黑名单**清，所以这两个键
        天然穿过去（与 ``industry`` / ``price`` 同类）。钉住它是因为反过来做不会
        报错：哪天有人把门禁改成白名单式，所有正在审计的猪企会集体丢掉标记，而页
        面上没有任何地方会显示异常。
        """
        rec = {"code": PIG, "total_score": 50.87, "industry": "养殖业",
               "valuation": {"price": 12.0}}
        rec.update(industry_margin.cohort_mark(PIG))
        for status in (audit_job.RUNNING, audit_job.FAILED, audit_job.UNAUDITED):
            with self.subTest(status=status):
                out = engine._apply_audit_gate(dict(rec), status)
                self.assertIsNone(out["total_score"],
                                  "门禁没挡住分数，这条就没在测门禁")
                self.assertEqual(out["cohort"], "pig")
                self.assertEqual(out["cohort_label"], "猪企")


class TestCohortReachesThePayload(unittest.TestCase):
    """列表与详情都必须带上标记，且**只带**组合成员。"""

    def _members_present(self, codes):
        return {c for c in industry_margin.COHORTS["pig"]["members"] if c in codes}

    def test_exactly_the_members_carry_the_mark_in_the_list(self):
        rows = {r["code"]: r for r in engine.list_stocks()}
        marked = {c for c, r in rows.items() if r.get("cohort_label")}
        expected = self._members_present(set(rows))
        self.assertTrue(expected, "组合成员一只都不在库里？这条测试已经失去意义")
        self.assertEqual(marked, expected)
        for code in marked:
            self.assertEqual(rows[code]["cohort_label"],
                             industry_margin.COHORTS["pig"]["label"])
            self.assertEqual(rows[code]["cohort"], "pig")

    def test_a_same_template_non_member_carries_no_key_at_all(self):
        """002129 与 4 只猪企走**同一个模板**，所以标记必须只能从组合成员取。"""
        rows = {r["code"]: r for r in engine.list_stocks()}
        if NON_PIG not in rows:
            self.skipTest(f"{NON_PIG} 不在库里")
        self.assertNotIn("cohort", rows[NON_PIG])
        self.assertNotIn("cohort_label", rows[NON_PIG])

    def test_detail_carries_the_same_mark(self):
        if PIG not in {r["code"] for r in engine.list_stocks()}:
            self.skipTest(f"{PIG} 不在库里")
        detail = engine.get_stock(PIG)
        self.assertEqual(detail["cohort"], "pig")
        self.assertEqual(detail["cohort_label"],
                         industry_margin.COHORTS["pig"]["label"])

    def test_detail_of_a_non_member_carries_no_key(self):
        rows = {r["code"] for r in engine.list_stocks()}
        if NON_PIG not in rows:
            self.skipTest(f"{NON_PIG} 不在库里")
        self.assertNotIn("cohort", engine.get_stock(NON_PIG))

    def test_the_pig_flag_is_the_same_ruler_as_the_dialog(self):
        """「是不是这一组公司」在列表 / 详情里**与补录弹窗同一把尺子**。

        ``pig_core.is_pig_company`` 比 ``cohort`` **更宽**（peer 组 + 已建档成员
        表），而前端决定「要不要给这个 tab」时手上只有列表 / 详情那一份载荷。
        两把尺子并存会造出一个自相矛盾的状态：**弹窗问你这只新股票的三个数，
        页面上却没有那个 tab**。所以这里比的不是「看起来一样」，而是**同一个
        函数**的返回值，逐只比。
        """
        rows = {r["code"]: r for r in engine.list_stocks()}
        self.assertTrue(rows, "库里一只都没有？这条测试已经失去意义")
        for code, row in rows.items():
            with self.subTest(code=code):
                self.assertIn("is_pig_company", row,
                              "每一行都要有这一格（不是也是值，不是缺键）")
                self.assertEqual(
                    row["is_pig_company"],
                    pig_core.is_pig_company(code, row.get("industry")))
        marked = {c for c, r in rows.items() if r["is_pig_company"]}
        self.assertTrue(marked & self._members_present(set(rows)),
                        "对照组里在库的成员一个都没标上？")
        if NON_PIG in rows:
            detail = engine.get_stock(NON_PIG)
            self.assertEqual(detail["is_pig_company"],
                             pig_core.is_pig_company(NON_PIG,
                                                     detail.get("industry")))
            self.assertFalse(detail["is_pig_company"])


class TestFrontendContract(unittest.TestCase):
    """前端只显示后端下发的中文名，不留第二份。照 test_experimental_mode 的做法
    直接读源码文本断言——这一层没有别的办法能钉住。"""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "research.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "research.css").read_text(encoding="utf-8")

    def test_no_second_copy_of_the_cohort_label(self):
        label = industry_margin.COHORTS["pig"]["label"]
        self.assertNotIn(label, self.js,
                         "「组合中文名」被写进了前端；它必须只从 payload 来")

    def test_no_second_copy_of_the_band_table(self):
        """档位词一律不许出现在前端——档位由 rules 算好、作为子分量的原始值下发。

        「正常」是常用词，不当判据（它也是档位表里唯一一个常见词）。
        """
        bands = rules.RULES_V1["cyclical_position"]["industry_margin_bands"]
        for _, label, _ in bands:
            if label == "正常":
                continue
            with self.subTest(label=label):
                self.assertNotIn(label, self.js,
                                 f"前端出现了档位词「{label}」，等于复制了阈值表")

    def test_sub_component_names_are_not_hardcoded(self):
        """名字与满分从 components 自己取；写死的话 rules 一改名这里就开始说谎。"""
        for name in ("利润分位", "毛利率分位", "PB分位"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.js)

    def test_overview_has_the_in_place_expander(self):
        self.assertIn('data-score-toggle="cyclical_position"', self.js)
        self.assertIn('class="score-accordion contrib-group', self.js)

    def test_new_styles_are_appended_not_spliced_in(self):
        """research.css 的既有约定：新样式追加在文件末尾，上面一行都不改。"""
        for cls in (".contrib-group", ".cohort-chip", ".detail-ident .detail-cohort"):
            self.assertIn(cls, self.css)
        self.assertLess(self.css.index(".state-actions"), self.css.index(".cohort-chip"),
                        "新样式插到了已有规则中间——必须追加在文件末尾")

    def test_the_evidence_card_reads_the_payload_not_a_fallback(self):
        self.assertIn("industry_margin", self.js)
        self.assertIn("industryMarginBlock", self.js)


if __name__ == "__main__":
    unittest.main()
