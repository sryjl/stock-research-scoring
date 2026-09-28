# -*- coding: utf-8 -*-
"""评分语义不变量：SCORING_V1.2 每修掉一个 bug，就在这里钉一条方向性断言。

**为什么不是「score != None」或「A > B」这种弱断言：**

那种测试只能证明函数没崩，证明不了它的方向是对的。V1.1 的 cigar_butt.pb
点表写成降序，被 piecewise 打成了阶梯，跑起来一切正常、分数是个合法数字、
`score is not None` 全绿——但它表达的是「PB 越高越好」，和烟蒂股的定义正好相反。
所以这里钉的是**经济方向**与**关键边界**：

* PB 越低分越高（单调不升），且在点表节点上取到声明的分值
* 正利润时 PE 越低分越高；亏损时 PE 判 not_applicable 而不是 0 分
* 调整后净现金/市值 上升，资产价值得分不得下降
* CAGR 的年数取**日历跨度**，缺年份不能把 4 年跨度当 3 年
* 同名必须同义：任何 display_name 只对应一个 metric_id/公式/时间口径/语义来源
* 已废弃的裸名（「净现金/市值」「CFO/净利润」）不许再出现在代码里

这些断言全部走**真实函数**，不 mock 评分逻辑本身。
"""
import os
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

from research import engine, metric_catalog, rules
from tests.asset_fixtures import fake_provider
from tests.test_research import _annual_from, _metrics, _series


def _comp(block, name):
    for c in block["components"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"找不到分量 {name!r}，现有：{[c['name'] for c in block['components']]}")


def _cigar_pb_score(pb, **kw):
    m = _metrics(**kw)
    m["current"]["pb"] = pb
    return _comp(rules.score_cigar_butt(m, m), "PB")["score"]


def _value_pe_score(pe, **kw):
    m = _metrics(**kw)
    m["current"]["pe_ttm"] = pe
    return _comp(rules.score_value(m, m), "PE")


def _source_files():
    """所有参与评分/展示的源码文件。审计扫的就是这些。"""
    files = []
    for sub in ("research", "static"):
        files.extend(sorted((ROOT / sub).glob("*.py")))
        files.extend(sorted((ROOT / sub).glob("*.js")))
        files.extend(sorted((ROOT / sub).glob("*.html")))
    files.append(ROOT / "server.py")
    return [f for f in files if f.is_file()]


def _strip_js_comments(src):
    """剥掉 JS 注释。注释是在解释历史（「原先叫净现金/市值」正是应该写的），
    扫「有没有写回兜底链」这类断言时必须先把它们去掉，否则会被自己的注释绊倒。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    return re.sub(r"//[^\n'\"`]*$", "", src, flags=re.M)


def _live_strings(path):
    """取出文件里**会显示给用户**的字符串，附带行号。

    注释和文档字符串一律排除：它们是在解释历史，写「原先叫净现金/市值」
    正是应该的。会被误伤的只有「注释里提过这个名字」——那不是同名异义。
    Python 走 AST 精确区分常量与 docstring；JS/HTML 先剥注释再取引号内容。
    """
    src = path.read_text(encoding="utf-8")
    out = []
    if path.suffix == ".py":
        import ast
        tree = ast.parse(src)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    docstrings.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                out.append((node.lineno, node.value))
    elif path.suffix == ".js":
        stripped = _strip_js_comments(src)
        for m in re.finditer(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`]*)`", stripped):
            text = next(g for g in m.groups() if g is not None)
            out.append((stripped[:m.start()].count("\n") + 1, text))
    else:  # .html：剥掉注释后按标签文本与属性值取
        stripped = re.sub(r"<!--.*?-->", "", src, flags=re.S)
        for m in re.finditer(r">([^<>]+)<", stripped):
            out.append((stripped[:m.start()].count("\n") + 1, m.group(1)))
    return out


#: 合法限定词：紧挨在裸名前，或紧随其后的时间窗口括号，都说明这个口径是有主的
_QUALIFIERS_BEFORE = ("调整后", "报表口径", "一级科目", "旧字段")
_QUALIFIERS_AFTER = ("（", "(")


# --------------------------------------------------------------------------- #
# 1. 同名必须同义
# --------------------------------------------------------------------------- #
class TestSameNameMeansSameThing(unittest.TestCase):
    """同名异义是这套评分里最难发现的一类错误——两个数都合法、都不报错。"""

    def test_no_display_name_has_two_meanings(self):
        bad = metric_catalog.duplicate_display_names()
        self.assertEqual(
            bad, [],
            "同名的指标必须 metric_id / 公式 / 时间口径 / 语义来源四项全同。"
            "口径不同的要在名字里写明（1Y）/（3年累计）/（TTM）/报表口径/调整后：\n" +
            "\n".join(f"  {name}: {[s.metric_id for s in specs]}" for name, specs in bad))

    def test_metric_ids_are_unique(self):
        self.assertEqual(metric_catalog.duplicate_metric_ids(), [],
                         "metric_id 必须全局唯一")

    def test_every_spec_declares_a_time_basis_and_source(self):
        for spec in metric_catalog.CATALOG:
            self.assertTrue(spec.time_basis, f"{spec.metric_id} 没声明时间口径")
            self.assertTrue(spec.source_semantics, f"{spec.metric_id} 没声明语义来源")
            self.assertTrue(spec.formula, f"{spec.metric_id} 没声明公式")

    def test_retired_bare_names_are_gone_from_live_strings(self):
        """「净现金/市值」「CFO/净利润」这两个裸名正是当时同名异义的元凶。

        它们不许再作为**会显示给用户的字符串**出现——一旦有人图省事写回去，
        同一页面上又会冒出两个不同的数共用一个名字。

        注释与文档字符串不算：那里提到旧名是在解释历史，正是应该写的。
        「调整后净现金/市值」与「CFO/净利润（3年累计）」也不算——限定词
        已经说明口径了。
        """
        offenders = []
        for path in _source_files():
            if path.name == "metric_catalog.py":
                continue   # 目录本身就要登记这些废弃名，跳过
            for lineno, text in _live_strings(path):
                for bare in metric_catalog.RETIRED_AMBIGUOUS_NAMES:
                    for m in re.finditer(re.escape(bare), text):
                        before = text[max(0, m.start() - 6):m.start()]
                        after = text[m.end():m.end() + 1]
                        if after in _QUALIFIERS_AFTER:
                            continue
                        if any(before.endswith(q) for q in _QUALIFIERS_BEFORE):
                            continue
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{lineno} 的显示文本里出现裸名"
                            f"「{bare}」：…{text.strip()[:60]}…")
        self.assertEqual(
            offenders, [],
            "以下位置仍在用已废弃的裸名（同名异义）。改名后要带上时间/口径限定词：\n" +
            "\n".join("  " + o for o in offenders))

    def test_the_detector_actually_catches_a_regression(self):
        """自检：把裸名写进一个会显示的字符串里，扫描必须报出来。

        没有这一条，上面那个测试可能只是因为扫描器失效才变绿。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "probe.py"
            f.write_text('X = "净现金/市值"\n', encoding="utf-8")
            hits = [t for _, t in _live_strings(f)
                    for bare in metric_catalog.RETIRED_AMBIGUOUS_NAMES
                    if bare in t]
            self.assertTrue(hits, "扫描器没能抓到裸名，上面那条测试是假的")
            # 注释与该抓的区分开
            f.write_text('# 净现金/市值 曾经的含义\nX = 1\n', encoding="utf-8")
            hits = [t for _, t in _live_strings(f)
                    for bare in metric_catalog.RETIRED_AMBIGUOUS_NAMES
                    if bare in t]
            self.assertEqual(hits, [], "注释不应该被当成显示文本")

    def test_catalogued_names_are_the_ones_actually_used(self):
        """目录不能是自说自话：每个登记的中文名必须真的出现在代码里。

        否则「目录里没有重复」只是因为目录没跟上代码，而不是因为代码没冲突。
        """
        corpus = "\n".join(p.read_text(encoding="utf-8") for p in _source_files())
        missing = [s.display_name for s in metric_catalog.CATALOG
                   if s.display_name not in corpus]
        self.assertEqual(missing, [],
                         f"目录里这些名字在代码里找不到，目录已经和实现对不上了：{missing}")

    def test_every_scored_component_is_catalogued_or_explicitly_local(self):
        """评分分量名要么进目录，要么是纯合成分（不对外当指标展示）。

        合成分（如「财务安全」「现金流存活」）是把多个原始指标揉成一个分数，
        它们不是「指标」，不参与同名异义判定，列在这里白名单。
        """
        SYNTHETIC = {
            "营收增长稳定性", "利润增长稳定性", "盈利稳定性", "财务安全", "现金流存活",
            "财务风险", "负债安全", "盈亏切换", "行业周期", "利润反转", "毛利率恢复",
            "现金流改善", "资产负债改善", "收入企稳", "行业盈利状态", "利润/营收波动比",
            "分红现金覆盖", "派息率", "连续分红年数", "利润CV", "毛利率波动",
            "资产流动性",
        }
        catalogued = {s.display_name for s in metric_catalog.CATALOG}
        undecided = sorted(k for k in rules.COMPONENT_UNITS
                           if k not in catalogued and k not in SYNTHETIC)
        self.assertEqual(undecided, [],
                         f"这些评分分量既没进 metric_catalog 也不在合成分白名单里：{undecided}")


# --------------------------------------------------------------------------- #
# 2. 烟蒂 PB：PB 越低越好
# --------------------------------------------------------------------------- #
class TestCigarPbDirection(unittest.TestCase):
    """V1.1 的 point 表是降序的，被 piecewise 打成阶梯，方向整个反了。"""

    #: 点表节点，以及每个节点应得的分数（RULES_V1["cigar_butt"]["pb"]）
    NODES = [(0.4, 20), (0.6, 14), (0.8, 8), (1.0, 4), (1.2, 0)]

    def test_nodes_hit_their_declared_scores(self):
        for pb, expected in self.NODES:
            self.assertAlmostEqual(
                _cigar_pb_score(pb), expected, places=6,
                msg=f"PB={pb} 应得 {expected} 分")

    def test_lower_pb_never_scores_lower(self):
        """单调性：PB 上升，烟蒂 PB 分不得上升。

        这是**方向**断言。V1.1 的实现（PB<=1.2 得 0、PB>1.2 得满分）
        能通过任何「score is not None」的测试，但会被这一条直接判死。
        """
        pbs = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5, 2.0, 3.0]
        scores = [_cigar_pb_score(pb) for pb in pbs]
        for (lo_pb, lo), (hi_pb, hi) in zip(zip(pbs, scores), zip(pbs[1:], scores[1:])):
            self.assertGreaterEqual(
                lo, hi,
                f"PB {lo_pb} 得 {lo} 分，PB {hi_pb} 得 {hi} 分——"
                f"烟蒂股 PB 越低越好，分数不该随 PB 上升")

    def test_full_marks_are_reachable(self):
        """声明满分 20 必须真的能拿到，否则是永远不可达的假满分。"""
        self.assertAlmostEqual(max(_cigar_pb_score(pb) for pb in
                                   [0.1, 0.2, 0.3, 0.4, 0.5]), 20)
        self.assertAlmostEqual(_cigar_pb_score(0.4), 20)

    def test_high_pb_scores_zero_not_full(self):
        """V1.1 的反向症状：PB 3.0 拿到 20 分满分。"""
        self.assertEqual(_cigar_pb_score(3.0), 0)
        self.assertEqual(_cigar_pb_score(10.0), 0)

    def test_threshold_dimension_matches_value_pb(self):
        """烟蒂 PB 点表的量纲必须是 PB 本身（0.x），不是百分数。"""
        # 0.4 与 0.6 之间应线性过渡，说明 x 是 0.4~1.2 的 PB 区间
        mid = _cigar_pb_score(0.5)
        self.assertTrue(14 <= mid <= 20, f"PB=0.5 得 {mid}，不在 [14,20] 内")


# --------------------------------------------------------------------------- #
# 3. PE：正利润时越低越好
# --------------------------------------------------------------------------- #
class TestValuePeDirection(unittest.TestCase):
    def test_lower_pe_never_scores_lower_for_positive_profit(self):
        pes = [5, 8, 10, 12, 15, 18, 20, 25, 30, 35, 40]
        scores = [_value_pe_score(pe)["score"] for pe in pes]
        for (lo_pe, lo), (hi_pe, hi) in zip(zip(pes, scores), zip(pes[1:], scores[1:])):
            self.assertGreaterEqual(
                lo, hi,
                f"PE {lo_pe} 得 {lo} 分，PE {hi_pe} 得 {hi} 分——"
                f"正利润时 PE 越低估值分不该越低")

    def test_loss_makes_pe_not_applicable_not_zero(self):
        """亏损 -> PE 不适用（退出归一化），不是 0 分。

        判 0 等于说「这家公司估值极差」，而事实是「PE 这个指标对它没有定义」。
        """
        c = _value_pe_score(-5)
        self.assertEqual(c["status"], "not_applicable")
        self.assertIsNone(c["score"])
        self.assertFalse(c["eligible"])
        self.assertIn("亏损", c["reason"])

    def test_missing_pe_is_missing_data(self):
        c = _value_pe_score(None)
        self.assertEqual(c["status"], "missing_data")
        self.assertIsNone(c["score"])

    def test_engine_backfills_pe_from_derived_ttm(self):
        """V1.2 前：cur["pe_ttm"] 只读行情，engine 回算的 TTM PE 从不回填，
        value.PE 在 9/9 只股票上恒为 missing_data。

        这里刻意让行情源不给 PE（pe_ttm=None），模拟当时的真实处境：
        overview 能从财报序列回算出 TTM PE，但那一路原本没接进 cur。
        """
        from research import engine
        from tests.test_research import TestOverviewBasis

        fin = TestOverviewBasis._fin(TestOverviewBasis.__new__(TestOverviewBasis))
        quote = {"price": 20.0, "total_market_cap": TestOverviewBasis.MCAP,
                 "pe_ttm": None, "pb": 0.85}
        m = engine.build_metrics("600741", quote, fin)[0]
        cur = m["current"]
        ov = (cur.get("overview") or {}).get("pe_ttm") or {}
        if ov.get("status") != "ok" or ov.get("value") is None:
            self.skipTest("该 fixture 下 overview 算不出 TTM PE，换个用例覆盖回填")
        self.assertIsNotNone(
            cur.get("pe_ttm"),
            "overview 里算出了 TTM PE，但 cur['pe_ttm'] 仍是 None——"
            "回填的那一处接线断了，value.PE 会永远 missing_data")
        self.assertAlmostEqual(cur["pe_ttm"], ov["value"])
        self.assertEqual(cur.get("pe_ttm_origin"),
                         ov.get("value_origin") or "derived")
        # 回填之后 value 模块才拿得到 PE——这才是这条接线存在的意义
        c = _comp(rules.score_value(m, m), "PE")
        self.assertEqual(c["status"], "ok", "PE 回填了，value.PE 仍判缺失")
        self.assertIsNotNone(c["score"])


# --------------------------------------------------------------------------- #
# 4. 调整后净现金/市值：单调性
# --------------------------------------------------------------------------- #
class TestAdjustedNetCashDirection(unittest.TestCase):
    """比例上升，依赖它的资产价值得分不得下降。"""

    RATIOS = [-0.2, -0.05, 0.0, 0.1, 0.2, 0.3, 0.45, 0.6, 0.8, 1.0]

    def _scores(self, fn, comp_name):
        out = []
        for r in self.RATIOS:
            m = _metrics(assets=fake_provider(net_cash_ratio=r))
            out.append(_comp(getattr(rules, fn)(m, m), comp_name)["score"])
        return out

    def test_cigar_butt_component_is_monotone(self):
        scores = self._scores("score_cigar_butt", rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP)
        for (lo_r, lo), (hi_r, hi) in zip(zip(self.RATIOS, scores),
                                          zip(self.RATIOS[1:], scores[1:])):
            self.assertGreaterEqual(
                hi, lo,
                f"调整后净现金/市值 {lo_r}→{hi_r} 时烟蒂得分 {lo}→{hi} 反而降了")

    def test_asset_value_component_is_monotone(self):
        scores = self._scores("score_asset_value", rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP)
        for (lo_r, lo), (hi_r, hi) in zip(zip(self.RATIOS, scores),
                                          zip(self.RATIOS[1:], scores[1:])):
            self.assertGreaterEqual(hi, lo,
                                    f"调整后净现金/市值 {lo_r}→{hi_r} 时资产价值得分降了")

    def test_value_component_is_monotone(self):
        scores = self._scores("score_value", rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP)
        for (lo_r, lo), (hi_r, hi) in zip(zip(self.RATIOS, scores),
                                          zip(self.RATIOS[1:], scores[1:])):
            self.assertGreaterEqual(hi, lo)

    def test_all_three_profiles_read_the_same_canonical_metric(self):
        """三个画像的「净现金/市值」必须来自同一个 provider 属性。"""
        for fn in ("score_cigar_butt", "score_asset_value", "score_value"):
            m = _metrics(assets=fake_provider(net_cash_ratio=0.9))
            c = _comp(getattr(rules, fn)(m, m), rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP)
            self.assertAlmostEqual(c["raw"], 0.9,
                                   msg=f"{fn} 的调整后净现金/市值不是 provider 的值")

    def test_reported_and_adjusted_are_different_keys(self):
        """报表口径与调整后口径是两个键，不许互相顶替。

        华域汽车实测：调整后 0.4035、报表口径 0.1580——同一个名字下方向相反。
        """
        from research import engine
        self.assertNotEqual(rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP,
                            rules.METRIC_REPORTED_NET_CASH_TO_MCAP)
        self.assertNotEqual(rules.METRIC_REPORTED_NET_CASH,
                            rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP)
        self.assertIn("调整后", rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP)
        self.assertIn("报表口径", rules.METRIC_REPORTED_NET_CASH_TO_MCAP)
        # build_overview 同时给出两套键，值可以不同但都必须存在
        self.assertTrue(callable(engine.build_overview))


# --------------------------------------------------------------------------- #
# 4b. 清算价值/市值 的点表按它自己的量纲标定
# --------------------------------------------------------------------------- #
class TestCigarLiquidationAnchor(unittest.TestCase):
    """`cigar_butt.liquidation` 读的是「(折价后资产 − 全部负债)/市值」，
    `asset_value.asset_value` 读的是「折价后资产/市值」——同名不同义的又一个
    实例（见模块 docstring）。两张表在 2026-09-23 之前**逐点相同**，那是「扣
    负债」修复漏改点表留下的：9 只实测只有三角拿到 9.92/30，其余 8 只归零，
    整个分量在可观测区间里不鉴别，30 分满分实际上不可达。

    这里钉的是重标**依据**，不是那五个数字本身——依据只有两条：零点必须语义
    化、封顶不许取样本极值。哪天两张表又被同步成一份，或者封顶被挪到样本
    最高值上，这两条会红。
    """

    TABLE = rules.RULES_V1["cigar_butt"]["liquidation"]
    # 9 只研究库股票在保守情景下的清算价值/市值实测区间（2026-09-23）。
    # 华域汽车 … 三角轮胎。换样本时这个常量该跟着换，但别为了迁就它改点表。
    OBSERVED = (-0.4846, 0.8547)

    def _liq(self, ratio):
        m = _metrics(assets=fake_provider(liquidation_ratio=ratio))
        return _comp(rules.score_cigar_butt(m, m), "清算价值/市值")

    def test_zero_is_the_semantic_anchor(self):
        """清算价值 = 市值 ⇒ 成交价里没便宜可占 ⇒ 0 分。

        旧表的 0 分点是 0.5（「市值是清算价值的 2 倍才归零」），那是**扣负债
        之前**的口径；扣完全部负债，这个量整体下移，0.5 就变成了一道把绝大多数
        股票挡在门外的墙。
        """
        self.assertEqual(self.TABLE[0], (0.0, 0))
        self.assertEqual(self._liq(0.0)["score"], 0)

    def test_net_liability_scores_zero_and_never_negative(self):
        """净资产清算为负 = 清算价值为负，只能 0 分，piecewise 不得外推出负分。"""
        for ratio in (-0.1, -0.4846, -2.5):
            self.assertEqual(self._liq(ratio)["score"], 0)

    def test_not_the_same_table_as_asset_value(self):
        """两张表读不同的量。再被同步成一份，等于把这个 bug 复发一次。"""
        self.assertNotEqual(list(self.TABLE),
                            list(rules.RULES_V1["asset_value"]["asset_value"]))

    def test_the_best_observed_value_is_neither_zero_nor_saturated(self):
        """实测最高值必须落在中段——既证明重标真的咬住了可观测区间，也证明封顶
        没有贴着样本极值。

        9 只不是 A 股总体，比三角更极端的一定存在；拿样本极值当满格点，那些股票
        会一起贴在顶部、曲线在那段彻底失去鉴别力。
        """
        top = self._liq(self.OBSERVED[1])
        self.assertGreater(top["score"], 15.0, "9 只实测最高值仍在中段以下 = 没重标到")
        self.assertLess(top["score"], top["max"], "封顶贴着样本极值 = 过拟合")

    def test_the_top_point_reaches_the_declared_max(self):
        """封顶点的分值必须等于组件声明的满分，否则声明满分永远不可达。"""
        top_x, top_score = self.TABLE[-1]
        self.assertAlmostEqual(top_score, self._liq(top_x)["max"])
        self.assertAlmostEqual(self._liq(top_x)["score"], top_score)

    def test_score_is_monotone_across_the_observed_range(self):
        ratios = sorted({x for x, _ in self.TABLE} | set(self.OBSERVED) | {0.2, 0.6, 1.0})
        scores = [self._liq(r)["score"] for r in ratios]
        for (lo_r, lo), (hi_r, hi) in zip(zip(ratios, scores), zip(ratios[1:], scores[1:])):
            self.assertGreaterEqual(hi, lo,
                                    f"清算价值/市值 {lo_r}→{hi_r} 时得分 {lo}→{hi} 反而降了")

    def test_the_component_moves_the_profile_score_one_for_one(self):
        """烟蒂画像五个分量声明满分合计正好 100，所以分量原始分与画像分 1:1。

        这不是巧合而是契约，而且后果很硬：**任何一次重标都会原封不动地搬到画像
        分上**，而画像分是 `determine_type` 的 argmax。2026-09-23 那轮就是靠这个
        1:1 差点把三角顶过 value（清算 9.92→23.46，画像 70.46→84.00）。所以改
        分量满分或模板权重时，这条必须一起看。
        """
        block = rules.score_cigar_butt(_metrics(assets=fake_provider()), _metrics())
        self.assertAlmostEqual(sum(c["max"] for c in block["components"]), 100.0)

        low = _metrics(assets=fake_provider(liquidation_ratio=0.0))
        high = _metrics(assets=fake_provider(liquidation_ratio=self.OBSERVED[1]))
        b_low, b_high = rules.score_cigar_butt(low, low), rules.score_cigar_butt(high, high)
        d_comp = (_comp(b_high, "清算价值/市值")["score"]
                  - _comp(b_low, "清算价值/市值")["score"])
        # 画像分量分是原值，画像总分在 _assemble 里 round(…, 2)，所以只在两位
        # 小数内相等；1:1 这个比例关系本身还是精确的。
        self.assertAlmostEqual(b_high["score"] - b_low["score"], d_comp, places=2)


# --------------------------------------------------------------------------- #
# 5. 两个时间窗口不许共用一个名字
# --------------------------------------------------------------------------- #
class TestCfoNetProfitWindows(unittest.TestCase):
    def test_component_uses_three_year_cumulative(self):
        m = _metrics()
        c = _comp(rules.score_quality(m, m), rules.METRIC_CFO_NET_PROFIT_3Y)
        self.assertTrue(c["eligible"])
        self.assertEqual(c["score_years"], 3)
        # OCF 2.3+2.8+3.2=8.3；NP 2.2+2.6+3.0=7.8
        self.assertAlmostEqual(c["value"], 8.3 / 7.8)

    def test_growth_cashflow_match_is_the_same_number(self):
        """成长模块的「现金流匹配」与质量模块的「CFO/净利润（3年累计）」
        必须同源同值——同一个数只该有一个来源。"""
        m = _metrics()
        g = _comp(rules.score_growth(m, m), "现金流匹配")
        q = _comp(rules.score_quality(m, m), rules.METRIC_CFO_NET_PROFIT_3Y)
        self.assertAlmostEqual(g["value"], q["value"])
        self.assertAlmostEqual(g["ratios"]["3Y"], q["ratios"]["3Y"])

    def test_one_year_and_three_year_are_distinct_and_both_exposed(self):
        """青啤实测 1Y=1.001 / 3Y=0.949，分居 1.0 两侧——同名会直接看反。"""
        c = _comp(rules.score_quality(_metrics(), _metrics()),
                  rules.METRIC_CFO_NET_PROFIT_3Y)
        self.assertAlmostEqual(c["ratios"]["1Y"], 3.2 / 3.0)
        self.assertAlmostEqual(c["ratios"]["3Y"], 8.3 / 7.8)
        self.assertNotAlmostEqual(c["ratios"]["1Y"], c["ratios"]["3Y"])

    def test_canonical_names_carry_their_window(self):
        self.assertIn("1Y", rules.METRIC_CFO_NET_PROFIT_1Y)
        self.assertIn("3年累计", rules.METRIC_CFO_NET_PROFIT_3Y)
        self.assertNotEqual(rules.METRIC_CFO_NET_PROFIT_1Y,
                            rules.METRIC_CFO_NET_PROFIT_3Y)

    def test_analyze_persists_both_canonical_keys(self):
        """两个窗口都要落进 valuation_metrics，前端才有 canonical 值可读。"""
        m = _metrics()
        rules.analyze(m)
        cur = m["current"]
        self.assertIn("cfo_net_profit_1y", cur)
        self.assertIn("cfo_net_profit_3y", cur)
        self.assertAlmostEqual(cur["cfo_net_profit_3y"], 8.3 / 7.8)


# --------------------------------------------------------------------------- #
# 6. CAGR：年数取日历跨度
# --------------------------------------------------------------------------- #
class TestCagrCalendarSpan(unittest.TestCase):
    def test_negative_base_has_no_growth_rate(self):
        """(-100, -50) 同负，比值 0.5 < 1 会算出「-50% 增长」这种假增长。"""
        self.assertIsNone(rules.cagr(-100, -50, 1))
        self.assertIsNone(rules.cagr(-1, 1, 3))

    def test_zero_and_none_and_bad_years(self):
        self.assertIsNone(rules.cagr(0, 10, 3))
        self.assertIsNone(rules.cagr(None, 10, 3))
        self.assertIsNone(rules.cagr(10, None, 3))
        self.assertIsNone(rules.cagr(10, 20, 0))
        self.assertIsNone(rules.cagr(10, 20, -1))

    def test_normal_growth_still_works(self):
        self.assertAlmostEqual(rules.cagr(100, 121, 2), 0.1)
        self.assertAlmostEqual(rules.cagr(100, 100, 3), 0.0)

    def test_year_span_uses_calendar_gap_not_list_length(self):
        """缺年份不能改变真实的日历跨度。

        2018→2022 是 4 年，哪怕中间 2019/2020/2021 都没取到。
        按 len(list)-1 算会把 4 年当 1 年，CAGR 被系统性高估。
        """
        gap = rules._year_span([("2018-12-31", 1.0), ("2022-12-31", 2.0)])
        self.assertEqual(gap, 4, "2018→2022 的日历跨度是 4 年")
        contiguous = rules._year_span([("2020-12-31", 1.0), ("2021-12-31", 2.0),
                                      ("2022-12-31", 3.0)])
        self.assertEqual(contiguous, 2)

    def test_year_span_is_none_when_undecidable(self):
        self.assertIsNone(rules._year_span([]))
        self.assertIsNone(rules._year_span([("2020-12-31", 1.0)]))
        self.assertIsNone(rules._year_span([("bad", 1.0), ("2022-12-31", 2.0)]))
        # 同一报告期两点 -> 跨度 0，不是有效的年数
        self.assertIsNone(rules._year_span([("2020-12-31", 1.0), ("2020-12-31", 2.0)]))

    def test_missing_year_changes_the_result_materially(self):
        """同一个倍数增长，压缩年数会把 CAGR 显著抬高——这正是当时的 bug。"""
        # 4 年 2 倍：年化约 18.9%
        right = rules.cagr(100.0, 200.0, rules._year_span(
            [("2018-12-31", 100.0), ("2022-12-31", 200.0)]))
        # 若误当成 1 年，就变成 100%
        wrong = rules.cagr(100.0, 200.0, 1)
        self.assertAlmostEqual(right, 2 ** 0.25 - 1, places=9)
        self.assertAlmostEqual(wrong, 1.0)
        self.assertLess(right, wrong / 2)

    def test_growth_profile_uses_calendar_span(self):
        """营收CAGR 的年数必须按报告期的**日历跨度**算，不是按列表长度。

        这里给三年：2018、2019、2022。列表长度 3 ⇒ 旧算法认为跨 2 年，
        真实日历跨度是 2018→2022 = 4 年。营收 100 → 200：

            按 4 年：2^(1/4) - 1 ≈ 0.1892
            按 2 年：2^(1/2) - 1 ≈ 0.4142

        两者都算得出数、都不报错，只是后者把增速凭空放大一倍多。所以这里
        断言的是**具体数值**，不是「算出来了」。
        """
        m = _metrics(revenue=[("2018-12-31", 100.0), ("2019-12-31", 110.0),
                              ("2022-12-31", 200.0)],
                     deduct_profit=[("2018-12-31", 10.0), ("2019-12-31", 11.0),
                                    ("2022-12-31", 20.0)])
        rev = _comp(rules.score_growth(m, m), "营收CAGR")
        self.assertEqual(rev["status"], "ok",
                         f"这条用例本该能算出营收CAGR，实际 {rev['status']}：{rev['reason']}")
        self.assertAlmostEqual(
            rev["raw"], 2 ** 0.25 - 1, places=6,
            msg="营收CAGR 用的应是日历跨度 4 年（2018→2022）；"
                "按列表长度算 2 年的话结果会是 0.4142")


# --------------------------------------------------------------------------- #
# 7. 年与年之间必须相邻才算「同比」
# --------------------------------------------------------------------------- #
class TestYoYAdjacency(unittest.TestCase):
    def test_gap_years_are_not_treated_as_consecutive(self):
        """2018 与 2022 是跨缺口的两个非相邻年，不能当「同比」比较。"""
        s = [("2018-12-31", 1.0), ("2022-12-31", 2.0)]
        self.assertEqual(rules._yoy_positive_years(s), 0,
                         "两个点之间隔了 3 年，不构成一次「同比增长」")

    def test_contiguous_years_still_count(self):
        s = [("2020-12-31", 1.0), ("2021-12-31", 2.0), ("2022-12-31", 3.0)]
        # 两个相邻对：2020→2021 增、2021→2022 增
        self.assertEqual(rules._yoy_positive_years(s), 2)

    def test_mixed_gap_counts_only_adjacent_pairs(self):
        s = [("2018-12-31", 1.0), ("2019-12-31", 2.0), ("2023-12-31", 3.0)]
        # 只有 2018→2019 相邻；2019→2023 跨了 4 年。
        # 旧实现按列表相邻算，会数出 2。
        self.assertEqual(rules._yoy_positive_years(s), 1)

    def test_next_year_predicate(self):
        self.assertTrue(rules._is_next_year("2020-12-31", "2021-12-31"))
        self.assertFalse(rules._is_next_year("2020-12-31", "2022-12-31"))
        self.assertFalse(rules._is_next_year("2021-12-31", "2020-12-31"))
        self.assertFalse(rules._is_next_year(None, "2021-12-31"))
        self.assertFalse(rules._is_next_year("bad", "2021-12-31"))


# --------------------------------------------------------------------------- #
# 8. 风险等级文案只有一份
# --------------------------------------------------------------------------- #
class TestRiskLabelSingleSource(unittest.TestCase):
    """前端抄过一份，抄成了 GREEN=「低风险」，后端当时是「暂无明显风险信号」。"""

    def test_labels_fit_the_list_column(self):
        """一律四个字：左侧列表「风险」那栏只占 27% 宽、10px 字，窄屏下
        risk-chip 是 nowrap + overflow:visible，超过四个字就溢出折断。
        这条钉住的是**显示约束**，不是文风偏好。"""
        for level, label in rules.RISK_SIGNAL_LABELS.items():
            self.assertLessEqual(
                len(label), 4,
                f"{level} 的文案「{label}」{len(label)} 个字，会在左侧列表里折断")
            self.assertTrue(label, f"{level} 的文案是空的")

    def test_four_labels_are_distinct(self):
        labels = list(rules.RISK_SIGNAL_LABELS.values())
        self.assertEqual(len(set(labels)), len(labels),
                         "两个风险等级用了同一句话，等于没有等级")

    def test_frontend_fallback_matches_backend_labels(self):
        src = (ROOT / "static" / "research.js").read_text(encoding="utf-8")
        block = re.search(r"const RISK_LABELS_FALLBACK = \{(.*?)\};", src, re.S)
        self.assertIsNotNone(block, "前端没有 RISK_LABELS_FALLBACK 兜底表")
        pairs = dict(re.findall(r"(\w+):\s*'([^']*)'", block.group(1)))
        self.assertEqual(
            pairs, rules.RISK_SIGNAL_LABELS,
            "前端兜底文案与后端 rules.RISK_SIGNAL_LABELS 不一致——"
            "同一个风险等级在两个地方说法不同")

    def test_frontend_reads_labels_from_meta_first(self):
        src = (ROOT / "static" / "research.js").read_text(encoding="utf-8")
        self.assertIn("risk_signal_labels", src,
                      "前端没有从 /api/meta 读 risk_signal_labels")
        self.assertRegex(src, r"function riskLabels\(\)")

    def test_frontend_has_no_second_copy_of_the_level_list(self):
        """风险面板的说明文字也从同一份现拼，不许再手写一遍等级清单。"""
        src = (ROOT / "static" / "research.js").read_text(encoding="utf-8")
        self.assertNotIn("风险等级：低风险、关注、较高风险、高风险", src,
                         "风险面板又手写了一份等级清单")

    def test_meta_endpoint_exposes_the_labels(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("risk_signal_labels", src)


# --------------------------------------------------------------------------- #
# 9. 前端不再写死版本号
# --------------------------------------------------------------------------- #
class TestFrontendVersionComesFromMeta(unittest.TestCase):
    def test_html_has_no_hardcoded_rule_version(self):
        """前端不许写死版本串。

        正则必须匹配**任意** SCORING_ 形态，不能只写 ``SCORING_V\\d+``：规则进了
        EXPERIMENTAL 之后，旧正则在 research.html 里再也匹配不到任何东西，于是
        「写死 SCORING_EXPERIMENTAL」也能通过这条专门用来抓写死的测试——测试会
        静默失效，比不写还坏。
        """
        src = (ROOT / "static" / "research.html").read_text(encoding="utf-8")
        hits = re.findall(r"SCORING_[A-Z0-9_.]*", src)
        self.assertEqual(hits, [],
                         f"research.html 里写死了规则版本号 {hits}；"
                         f"rules.py 改口径后它会继续显示旧版本")
        self.assertIn("rule-version", src)

    def test_js_fills_it_from_meta(self):
        src = (ROOT / "static" / "research.js").read_text(encoding="utf-8")
        self.assertIn("rule-version", src)
        self.assertIn("rule_version", src)


# --------------------------------------------------------------------------- #
# 10. 列表排序只读当前主记录的 canonical 指标
#
# 旧写法是前端 `adjusted_net_cash_to_mcap ?? net_cash_ratio` 的兜底链。两个键
# 都在主记录里，看起来「优先新的、退回旧的」很稳，实际兜底那份可能陈旧到方向
# 相反——华域汽车主记录里的 net_cash_ratio 还是应付票据改口径之前算的 0.158，
# 而当前 provider 重算出来是 0.3994，差 2.5 倍。排出来的顺序一切正常，所以这
# 类错只能靠「口径判定只允许有一处、且必须可标注来源」来根除。
# --------------------------------------------------------------------------- #
class TestSortReadsTheCanonicalMetric(unittest.TestCase):
    def test_canonical_wins_even_when_the_legacy_alias_is_present(self):
        """华域汽车的原始形态：两个键都在，值不一样，必须取 canonical。"""
        value, source = engine.net_cash_sort_value(
            {"adjusted_net_cash_to_mcap": 0.3994, "net_cash_ratio": 0.158})
        self.assertEqual(value, 0.3994, "排序读到了旧的 net_cash_ratio")
        self.assertEqual(source, engine.NET_CASH_SOURCE_CANONICAL)

    def test_legacy_alias_is_used_but_labelled(self):
        """只有旧名时不给排名留空白，但必须标明——不许它冒充 canonical。"""
        value, source = engine.net_cash_sort_value({"net_cash_ratio": 0.158})
        self.assertEqual(value, 0.158)
        self.assertEqual(source, engine.NET_CASH_SOURCE_LEGACY)

    def test_zero_is_a_value_and_absence_is_not(self):
        """0.0 是「算出来正好是 0」，不是「没有数据」；两者不能混成一个。"""
        self.assertEqual(engine.net_cash_sort_value({"net_cash_ratio": 0.0}),
                         (0.0, engine.NET_CASH_SOURCE_LEGACY))
        self.assertEqual(engine.net_cash_sort_value({}),
                         (None, engine.NET_CASH_SOURCE_MISSING))
        self.assertEqual(engine.net_cash_sort_value(None),
                         (None, engine.NET_CASH_SOURCE_MISSING))

    def test_record_carries_the_value_and_its_source_at_top_level(self):
        rec = {"valuation": {"adjusted_net_cash_to_mcap": 0.3994, "net_cash_ratio": 0.158}}
        engine._with_canonical_metrics(rec, rec["valuation"])
        self.assertEqual(rec["adjusted_net_cash_to_mcap"], 0.3994)
        self.assertEqual(rec["net_cash_source"], engine.NET_CASH_SOURCE_CANONICAL)
        self.assertIn(rec["net_cash_source"],
                      (engine.NET_CASH_SOURCE_CANONICAL, engine.NET_CASH_SOURCE_LEGACY,
                       engine.NET_CASH_SOURCE_MISSING),
                      "来源标签是个自由字符串，前端没法据此判断")

    def test_the_legacy_key_is_read_in_exactly_one_place(self):
        """兜底只允许有一次。多了就说明有人在别处又自己翻了一遍 valuation。"""
        src = (ROOT / "research" / "engine.py").read_text(encoding="utf-8")
        reads = src.count('val.get("net_cash_ratio")')
        self.assertEqual(reads, 1,
                         f"engine.py 里有 {reads} 处直接读 net_cash_ratio；"
                         f"排序口径必须只在 net_cash_sort_value 里判定一次")

    def test_both_accessors_go_through_the_same_helper(self):
        src = (ROOT / "research" / "engine.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("_with_canonical_metrics(rec, rec[\"valuation\"])"), 1)
        self.assertEqual(src.count("_with_canonical_metrics(out, out[\"valuation\"])"), 1,
                         "list_stocks 与 get_stock 必须走同一个口径判定")

    def test_frontend_sort_has_no_fallback_chain(self):
        src = _strip_js_comments((ROOT / "static" / "research.js").read_text(encoding="utf-8"))
        self.assertIn("net_cash_ratio: s.adjusted_net_cash_to_mcap,", src)
        for pattern in ("?? v.net_cash_ratio", "?? s.net_cash_ratio",
                        "|| v.net_cash_ratio", "|| s.net_cash_ratio"):
            self.assertNotIn(pattern, src,
                             f"前端又写回了兜底链 {pattern!r}——旧名那份可能陈旧到方向相反")

    def test_frontend_marks_the_stale_rows(self):
        """混了旧口径的列表必须看得见，否则它看起来就是一条同口径排名。"""
        src = (ROOT / "static" / "research.js").read_text(encoding="utf-8")
        self.assertIn("legacy_alias", src)
        self.assertIn("function hasLegacyNetCash", src)


if __name__ == "__main__":
    unittest.main()
