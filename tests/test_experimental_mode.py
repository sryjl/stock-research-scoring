# -*- coding: utf-8 -*-
"""tests/test_experimental_mode.py — 评分体系处于 EXPERIMENTAL 模式时的读侧契约。

只保留一套当前生效规则（``SCORING_EXPERIMENTAL``），历史 snapshot 保留但标为
「旧实验结果」，页面默认只展示当前规则重算的结果。这一层全部是**读侧判定**：
不重写任何历史行、不加 schema、不做迁移。

这里钉住四件事：
  1. 「旧实验结果」的判定是一处函数，前端的布尔值来自后端，不是自己解析版本串。
  2. 跨规则版本的差值不算「变化」——两个口径相减出来的数会被读成评分涨跌。
  3. 换生效规则时每只股票**恰好追加一行**，旧行一个字符都不动。
  4. 前端默认只画当前版本，旧结果折叠可展开，且不再有「规则版本」这种常量列。
"""
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.db as research_db  # noqa: E402
from research import engine, rules  # noqa: E402

JS = (ROOT / "static" / "research.js").read_text(encoding="utf-8")


def _template_components(name):
    """把某个真评分模板的分量键做成快照里 ``category_scores`` 那种列表。

    模板身份就是分量键集，所以「换模板」在快照上表现为换一组键。
    """
    return [{"key": k, "score": 80} for k in sorted(rules.RULES_V1["templates"][name])]


def _snapshot(code="600000", version=None, score=80.0, price=10.0,
              date="2026-09-23 10:00:00", cats=None):
    version = version or rules.RULE_VERSION
    snap = {
        "stock_code": code, "date": date, "current_price": price,
        "report_period": "2026-06-30", "rule_version": version,
        "type_scores": {"value": 80}, "financial_metrics": {"revenue": [1, 2]},
        "valuation_metrics": {"pb": 1.0, "adjusted_net_cash_to_mcap": 0.4},
        "risk_flags": [], "total_score": score, "confidence": 1.0,
        "category_scores": cats if cats is not None else [{"key": "valuation", "score": 80}],
        "profile_version": "PROFILE_SCORING_V1.2",
    }
    snap["result_hash"] = engine.snapshot_result_hash(snap, None)
    return snap


class TestLegacyRuleVersion(unittest.TestCase):
    def test_every_earlier_regime_reads_as_legacy(self):
        for old in ("SCORING_V1.0", "SCORING_V1.1", "SCORING_V1.2"):
            self.assertTrue(engine.is_legacy_rule_version(old), f"{old} 没被判成旧结果")

    def test_the_live_regime_does_not(self):
        self.assertFalse(engine.is_legacy_rule_version(rules.RULE_VERSION))

    def test_a_missing_version_reads_as_legacy(self):
        """版本列是 NULL 的行比任何具名版本都旧，判成旧结果而不是当前结果。"""
        self.assertTrue(engine.is_legacy_rule_version(None))

    def test_the_judgement_is_not_a_prefix_match(self):
        """前缀比对会把 SCORING_EXPERIMENTAL_V2 之类误判成当前规则。"""
        self.assertTrue(engine.is_legacy_rule_version(rules.RULE_VERSION + "_V2"))


class TestSnapshotsCarryTheFlag(unittest.TestCase):
    def test_the_marker_is_computed_once_on_the_server(self):
        marked = engine._mark_legacy_snapshots([
            _snapshot(version="SCORING_V1.1"), _snapshot(version=rules.RULE_VERSION),
        ])
        self.assertEqual([m["legacy_rule"] for m in marked], [True, False])

    def test_the_frontend_never_parses_the_version_string(self):
        """前端只读 legacy_rule 布尔值。

        让它自己比版本串，就得在前端复刻一份「当前版本是什么」——那份副本在改
        规则时不会被改，于是同一页上两个地方对「旧」的判断不一致。
        """
        self.assertIn("legacy_rule", JS)
        self.assertNotIn("rule_version ===", JS)
        self.assertNotIn("''SCORING_", JS)


def _mark_audited(conn, code):
    """给一只股票补一份**可用**的资产语义快照（门禁：「有快照就算完成」）。

    这一组测的是「旧规则的结果怎么标注」，不是审计门禁。而门禁会挡死任何**没有
    资产快照**的股票（不给分数、不给快照列表）——夹具不补这一步，测的就不是标注
    而是门禁了。补的是最小的一行：门禁只认 ``total_assets`` 非空。
    """
    conn.execute(
        "INSERT INTO asset_semantic_snapshot (stock_code, date, metric_version,"
        " report_period, total_assets) VALUES (?, '2026-09-01 00:00:00',"
        " 'ASSET_SEMANTIC_ENGINE_V1.0', '2026-06-30', 1.0e9)", (code,))
    conn.commit()


class TestDeltaScoreStaysInsideOneRegime(unittest.TestCase):
    """跨规则版本的差不是「变化」，是两个口径相减。"""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _write(self, *snaps):
        for s in snaps:
            research_db.add_snapshot_if_changed(self.conn, s)

    def test_a_single_run_has_nothing_to_compare_against(self):
        self._write(_snapshot())
        self.assertIsNone(engine._delta_score(self.conn, "600000", 80.0, rules.RULE_VERSION))

    def test_two_runs_of_the_same_rule_give_the_difference(self):
        self._write(_snapshot(score=80.0), _snapshot(score=86.59, date="2026-09-23 11:00:00"))
        self.assertAlmostEqual(
            engine._delta_score(self.conn, "600000", 86.59, rules.RULE_VERSION), 6.59)

    def test_the_previous_regime_is_not_used_as_a_baseline(self):
        """华域那条实况：86.59（新规则）− 82.94（V1.2）曾经显示成 +3.65。

        必须写**两条**旧快照：只写一条的话，不管实现在不在版本上过滤，条数都
        不足两条而返回 None——测试会因为错的原因通过，正好放过这个缺陷。
        """
        self._write(_snapshot(version="SCORING_V1.2", score=70.0, date="2026-09-21 10:00:00"),
                    _snapshot(version="SCORING_V1.2", score=82.94, date="2026-09-22 10:00:00"))
        self.assertIsNone(
            engine._delta_score(self.conn, "600000", 86.59, rules.RULE_VERSION),
            "跨规则版本算出了差值——那是两个口径相减，会被读成评分涨了")

    def test_legacy_rows_do_not_shift_the_baseline_either(self):
        """旧行夹在中间时，基线要取「同版本的上一条」，不是「上一条」。

        库里的实际顺序就是这样：V1.2 的历史在前，EXPERIMENTAL 的在后。取错的话
        差值会拿新结果去减一条旧口径的结果。
        """
        self._write(_snapshot(version="SCORING_V1.2", score=70.0, date="2026-09-21 10:00:00"),
                    _snapshot(score=80.0, date="2026-09-23 09:00:00"),
                    _snapshot(score=86.59, date="2026-09-23 10:00:00"))
        self.assertAlmostEqual(
            engine._delta_score(self.conn, "600000", 86.59, rules.RULE_VERSION), 6.59)

    def test_a_stock_never_reanalysed_still_compares_inside_its_own_regime(self):
        """还没重算的股票，主记录是旧版本；它跟自己那套历史比仍然成立。"""
        self._write(_snapshot(version="SCORING_V1.2", score=70.0),
                    _snapshot(version="SCORING_V1.2", score=72.5, date="2026-09-23 11:00:00"))
        self.assertAlmostEqual(
            engine._delta_score(self.conn, "600000", 72.5, "SCORING_V1.2"), 2.5)

    def test_none_current_stays_none(self):
        self._write(_snapshot(), _snapshot(score=90.0, date="2026-09-23 11:00:00"))
        self.assertIsNone(engine._delta_score(self.conn, "600000", None, rules.RULE_VERSION))

    def test_a_template_change_is_not_a_comparable_baseline(self):
        """换了模板 = 换了权重 = 换了口径。

        两条快照的 ``rule_version`` 一样（常量 SCORING_EXPERIMENTAL）、``audit_ok_at``
        也一样，所以前两道过滤都拦不住；只有分量键集认得出来。实况即三角轮胎
        601163：价值型一条、烟蒂型一条相邻。
        """
        growth = _template_components("成长型")
        cigar = _template_components("烟蒂/资产价值型")
        self._write(_snapshot(score=80.0, cats=growth),
                    _snapshot(score=86.59, date="2026-09-23 11:00:00", cats=cigar))
        self.assertIsNone(
            engine._delta_score(self.conn, "600000", 86.59, rules.RULE_VERSION,
                                template_keys=engine._template_keys(cigar)),
            "跨模板算出了差值——那是两套权重相减，会被读成评分涨了")

    def test_a_row_from_another_template_is_not_used_as_the_baseline(self):
        """基线要取「上一条**同模板**的」，不是「上一条」。"""
        growth = _template_components("成长型")
        cigar = _template_components("烟蒂/资产价值型")
        self._write(_snapshot(score=70.0, cats=growth, date="2026-09-23 09:00:00"),
                    _snapshot(score=84.0, cats=cigar, date="2026-09-23 10:00:00"),
                    _snapshot(score=86.59, cats=cigar, date="2026-09-23 11:00:00"))
        self.assertAlmostEqual(
            engine._delta_score(self.conn, "600000", 86.59, rules.RULE_VERSION,
                                template_keys=engine._template_keys(cigar)), 2.59)


class TestSwitchingTheLiveRuleAppendsExactlyOneRow(unittest.TestCase):
    """换生效规则 = 每只股票 +1 行，旧行不动。"""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)
        self._patch = mock.patch.object(research_db, "DEFAULT_PATH", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_the_old_regime_rows_are_untouched_and_a_new_one_is_appended(self):
        for i in range(3):
            research_db.add_snapshot_if_changed(
                self.conn, _snapshot(version="SCORING_V1.2", score=70.0 + i,
                                     date=f"2026-09-2{i} 10:00:00"))
        before = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM research_snapshots ORDER BY id")]
        self.assertEqual(len(before), 3)

        research_db.add_snapshot_if_changed(self.conn, _snapshot(score=70.0))

        after = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM research_snapshots ORDER BY id")]
        self.assertEqual(len(after), 4, "换规则没有留下新的一行")
        self.assertEqual(before, after[:3], "换规则改动了旧规则时期的行")
        self.assertEqual(self.conn.execute(
            "SELECT rule_version FROM research_snapshots WHERE id=4").fetchone()[0],
            rules.RULE_VERSION)

    def test_re_running_under_the_new_rule_still_dedups(self):
        research_db.add_snapshot_if_changed(self.conn, _snapshot(score=70.0))
        self.assertFalse(research_db.add_snapshot_if_changed(self.conn, _snapshot(score=70.0)),
                         "同一份结果在新规则下被反复追加了")

    def test_get_stock_marks_both_the_record_and_its_snapshots(self):
        """详情页的接线：主记录与每条快照都要带上标记。

        只测 engine._mark_legacy_snapshots 本身是不够的——那是个纯函数，测试会
        通过，而 get_stock 忘了调用它，「快照全都不带标记」这个缺陷照样溜过去。
        所以这里从 get_stock 走一遍真实读取路径。
        """
        for version, score, date in (("SCORING_V1.2", 70.0, "2026-09-20 10:00:00"),
                                     (None, 72.0, "2026-09-22 10:00:00"),
                                     (None, 74.0, "2026-09-23 10:00:00")):
            research_db.add_snapshot_if_changed(
                self.conn, _snapshot(version=version, score=score, date=date))
        self.conn.execute(
            "INSERT INTO research_stocks (code, name, system_type, total_score, rule_version)"
            " VALUES ('600000', '测试', 'value', 74.0, ?)", (rules.RULE_VERSION,))
        self.conn.commit()
        _mark_audited(self.conn, "600000")

        out = engine.get_stock("600000")
        self.assertFalse(out["legacy_rule"])
        self.assertEqual([s["legacy_rule"] for s in out["snapshots"]], [True, False, False],
                         "详情页的快照没有带上旧结果标记")
        self.assertAlmostEqual(out["delta_score"], 2.0,
                               msg="同规则版本的两条快照之间应该算出差值")

    def test_the_list_marks_a_master_record_that_predates_the_switch(self):
        """主记录还停在旧版本时必须在列表上标出来。

        不标的话，顶栏写着当前规则、这一行却带着旧口径的分数，整个列表看起来
        是一条同口径的排名。原始 SQL 是故意的：模拟的就是「上一次分析发生在换
        规则之前、之后再没跑过」这个状态，不走 upsert_stock 那条会顺手写全字段
        的路径。
        """
        for code, score, version in (("600741", 86.59, "SCORING_V1.2"),
                                     ("601163", 80.0, rules.RULE_VERSION)):
            self.conn.execute(
                "INSERT INTO research_stocks (code, name, system_type, total_score, rule_version)"
                " VALUES (?, ?, 'value', ?, ?)", (code, "测试" + code, score, version))
        self.conn.commit()
        for code in ("600741", "601163"):
            _mark_audited(self.conn, code)
        rows = {r["code"]: r for r in engine.list_stocks()}
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows["600741"]["legacy_rule"], "旧版本的主记录在列表上没有任何标记")
        self.assertFalse(rows["601163"]["legacy_rule"])


class TestFrontendShowsOnlyTheCurrentRegimeByDefault(unittest.TestCase):
    def test_the_history_section_filters_by_the_server_flag(self):
        self.assertIn("x.legacy_rule", JS)
        self.assertIn("legacyHistoryBlock", JS)

    def test_the_legacy_bucket_is_collapsed_by_default(self):
        self.assertIn("historyLegacyOpen: false", JS)
        self.assertIn("data-history-legacy-toggle", JS)

    def test_the_current_table_has_no_constant_version_column(self):
        """当前版本的表里每一行的版本号都一样，那一列看着有信息其实什么都没带。"""
        header = re.search(r"const HISTORY_TH = '.*?';", JS, re.S).group(0)
        self.assertNotIn("规则版本", header)
        self.assertIn('colspan="6"', JS)

    def test_the_legacy_table_keeps_the_version_column(self):
        """旧结果桶里的版本号是真信息（V1.0 / V1.1 / V1.2 各不相同）。"""
        bucket = re.search(r"function legacyHistoryBlock.*?\n}", JS, re.S).group(0)
        self.assertIn("<th>规则版本</th>", bucket)

    def test_the_old_wording_is_gone(self):
        """「已自动合并 N 条内容相同的旧快照」放进旧结果桶里正好说反了事实。"""
        self.assertNotIn("内容相同的旧快照", JS)

    def test_the_legacy_bucket_says_the_scores_are_not_comparable(self):
        bucket = re.search(r"function legacyHistoryBlock.*?\n}", JS, re.S).group(0)
        self.assertIn("不可比", bucket)
        self.assertIn("只标注、不重算", bucket)

    def test_the_partition_matches_the_one_the_server_marks(self):
        """两半都要用后端那个布尔值切，而且旧的那一半要真的被渲染出来。

        只断言「legacy_rule 这个词出现过」是不够的：把过滤条件写反、或者算出了
        旧结果桶却忘了插进返回值里，字符串扫描照样通过，而页面上要么把旧口径混
        进趋势、要么把它们整个吞掉。
        """
        body = re.search(r"function renderHistory.*?\n}", JS, re.S).group(0)
        self.assertIn("all.filter((x) => !x.legacy_rule)", body)
        self.assertIn("all.filter((x) => x.legacy_rule)", body)
        self.assertIn("legacyHistoryBlock(legacy)", body)
        self.assertIn("snapshotRows(snaps, false)", body,
                      "当前规则那张表不该带版本列")

    def test_the_bucket_can_actually_be_opened(self):
        self.assertIn("closest('[data-history-legacy-toggle]')", JS)

    def test_the_mark_only_renders_for_stale_records(self):
        """标记在 legacy_rule 为假时必须什么都不输出。

        少了那个前置返回，每一行都会挂上「旧结果」——把当前结果标成旧的，比
        不标更坏：它会让一次正常的重算看起来像没生效。
        """
        body = re.search(r"function legacyRuleMark.*?\n}", JS, re.S).group(0)
        self.assertIn("if (!s.legacy_rule) return '';", body)

    def test_the_mark_is_wired_wherever_the_score_is_shown(self):
        """详情页头、列表评分格、概览的状态卡都挂上，否则旧分数会被当成当前分数。"""
        self.assertIn("${legacyRuleMark(s, '旧结果')}", JS)        # 详情页头
        self.assertIn("${legacyRuleMark(s, '旧')}", JS)            # 列表评分格
        self.assertIn("legacyRuleMark(s, ' · 旧实验结果')", JS)     # 概览「研究数据状态」


class TestRuleVersionIsTheOnlyExperimentalMarker(unittest.TestCase):
    def test_the_data_caliber_axes_are_not_versioned_as_rules(self):
        """资产语义层与画像口径是**数据口径**（折价率表、负债分类），不跟着评分
        规则走：评分规则进 EXPERIMENTAL，这两个还停在自己的号上。"""
        from research import asset_metrics, asset_semantics
        self.assertEqual(asset_semantics.VERSION, "ASSET_SEMANTIC_ENGINE_V1.0")
        self.assertEqual(asset_metrics.AssetMetricProvider.missing("x").metric_version,
                         "ASSET_SEMANTIC_ENGINE_V1.0")
        self.assertEqual(asset_metrics.PROFILE_VERSION, "PROFILE_SCORING_V1.2")

    def test_no_rule_source_sha_is_written_into_snapshots(self):
        """快照里不许出现源码指纹。

        文件 sha 不等于评分规则内容（评分修复也落在 engine.build_metrics、
        画像分量还经 asset_metrics 进来），所以「同 sha ⇒ 同口径」是假的；而且
        它一旦进 result_hash，改一个错别字就作废全部现有行——包括 changelog 本身。
        以后真要加，先想清楚它进不进哈希。
        """
        path = tempfile.mktemp(suffix=".db")
        try:
            conn = research_db.init_db(path)
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(research_snapshots)")]
            finally:
                conn.close()
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.assertNotIn("rule_source_sha256", cols)
        self.assertFalse([c for c in cols if "sha" in c.lower()],
                         f"快照表里出现了指纹列 {cols}")


class TestOneConclusionReachesTheScreen(unittest.TestCase):
    """界面上不许同时出现两个互相竞争的「类型」。

    2026-09-24 之前：概览里既有「股票画像：周期」又有「主模型：通用价值」，
    用户没法判断哪个是结论（赣锋 002460 就是现成的样子）。现在类型画像只是
    Router 的输入，**评分框架只有 primary_model 一个入口**，所以：
    画像格不再有「主画像」那一格；模板必须写明是谁定的；「未路由」必须是一个
    统一的判据，不能五处各写一份。
    """

    def _fn(self, name):
        """取一个顶层函数体的源码（剥掉注释，免得断言落在说明文字上）。"""
        body = re.search(r"function %s\(.*?\n}" % name, JS, re.S).group(0)
        return re.sub(r"/\*.*?\*/", "", body, flags=re.S)

    def _portrait(self):
        return re.search(r"const portrait = section\('股票画像'.*?attribute-grid", JS, re.S).group(0)

    def test_the_second_type_is_gone_from_the_portrait(self):
        portrait = self._portrait()
        self.assertNotIn('主画像', portrait,
                         "「主画像」还在——页面上就会有两个互相竞争的结论")
        self.assertNotIn('它是什么类型', portrait, "画像不再回答「算哪套模型」")
        # 行业 / 市值 / 风险等级 三格。多一格就是有人把类型加回来了。
        self.assertEqual(portrait.count('<div><span>'), 3, portrait)

    def test_the_old_type_is_not_rendered_anywhere_anymore(self):
        self.assertNotIn('s.system_type', JS, "system_type 又被渲染出来了")

    def test_the_template_says_who_chose_it(self):
        # 模板跟着 primary_model 走，所以必须写明是正式路由定的还是兜底的：
        # 只说「采用周期价值型模板」，用户会以为那是 Router 的结论。
        self.assertIn("cat.template_source === 'route'", JS)
        self.assertIn('未路由，按画像判型兜底', JS)
        self.assertIn('主模型 ${esc(modelLabel(cat.template_model)', JS)

    def test_unrouted_is_one_judgement_used_everywhere(self):
        # 判据只有一份。各写各的（一个判 primary_model、一个判 primary_fit）时，
        # 赣锋那种「有兜底模型名、没有适配度」的股票会在一处算未路由、在另一处
        # 算已路由——正是这个改动要根除的分叉。
        self.assertIn('function isUnrouted(s) { return !s.primary_model || bad(s.primary_fit); }', JS)
        for fn in ('routeText', 'filtered', 'renderFilters', 'renderDetailHead', 'renderRouteCard'):
            self.assertIn('isUnrouted(', self._fn(fn), f"{fn} 没走统一判据")

    def test_an_unrouted_stock_still_shows_how_far_off_it_was(self):
        # 「未路由」三个字太干：用户看不出是数据缺失还是差一点点。
        self.assertIn('function topCandidate(route)', JS)
        self.assertIn('最高适配度', JS)
        self.assertIn('最高：', JS)


if __name__ == "__main__":
    unittest.main()
