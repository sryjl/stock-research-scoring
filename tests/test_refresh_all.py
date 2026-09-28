# -*- coding: utf-8 -*-
"""tests/test_refresh_all.py — 研究库「↻ 刷新全部价格」。

这个按钮**不加任何评分逻辑**：它就是把详情页那个单只「刷新价格」按顺序对全库跑一遍
（同一条 ``engine.analyze(code, force_financials=False)``）。所以这个文件钉的不是分数，
而是三条一写错就把别的东西弄坏的性质：

1. **串行**。一只一只 ``await``，不是 ``Promise.all``。并发的 analyze 会让详情页、
   审计队列和路由快照互相插队，而项目其它地方（审计 worker、门禁）都是「一次一只」。
2. **一只失败不停下**。25 只里有 1 只取不到行情，不能把另外 24 只也废掉；失败要带
   代码和原因回到界面上，不能吞掉。
3. **闸门一定放回去**。``state.busy`` / ``state.bulkBusy`` 是在 ``finally`` 里恢复的；
   漏掉一处，全站从此点不动任何按钮（analyze 自己那句 ``if (state.busy) return``
   会把后续每一次点击都静默吃掉）。

另外钉住「它不做什么」：不抓财务（``force_financials: false``——重抓财务是单只
「重新分析财务」的活）、不加服务端批量端点、不动 ``analyze()``。

文本断言手法沿用 ``tests/test_scoring_invariants.py`` / ``tests/test_audit_gate.py``：
只扫静态源码，不起浏览器、不引第三方库。
"""
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JS = (ROOT / "static" / "research.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "research.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "research.css").read_text(encoding="utf-8")
SERVER_PY = (ROOT / "server.py").read_text(encoding="utf-8")

#: 函数体的结束标志：紧跟其后的那句顶层绑定。
_AFTER = "$('#btn-refresh-all').addEventListener"


def _strip_js_comments(src):
    """剥掉 JS 注释再断言。注释里正好写着「analyze 一个字没改」这类话，
    不剥掉的话，一条应该失败的断言会被自己的注释喂饱。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    return re.sub(r"//[^\n'\"`]*$", "", src, flags=re.M)


def _refresh_all_body():
    """``refreshAllPrices`` 的函数体（已剥注释）。切片靠两个顶层标志，与缩进无关。"""
    start = JS.index("async function refreshAllPrices() {")
    end = JS.index(_AFTER, start)
    return _strip_js_comments(JS[start:end])


def _slice(src, start_marker, end_marker):
    """两个标志之间的一段，含起点不含终点。找不到就返回空串（断言自己会报）。"""
    start = src.find(start_marker)
    if start < 0:
        return ""
    end = src.find(end_marker, start)
    return src[start:end] if end < 0 else src[start:end]


class TestTheButtonIsThere(unittest.TestCase):
    def test_the_library_head_has_the_button_and_still_has_the_count(self):
        head = _slice(HTML, 'class="library-head-right"', "</div>")
        self.assertIn('id="btn-refresh-all"', head, "研究库头部没有全库刷新按钮")
        self.assertIn('id="library-count"', head, "计数徽标被这次改动弄丢了")

    def test_the_count_is_wrapped_only_once(self):
        # 包一层是为了让 space-between 别把计数挤到正中；包两处就会渲染出两个计数。
        self.assertEqual(HTML.count('class="library-head-right"'), 1)

    def test_the_button_is_wired_to_the_loop(self):
        self.assertIn(_AFTER + "('click', refreshAllPrices)", JS)

    def test_the_button_is_not_the_primary_action(self):
        # 主操作用色留给顶栏的「分析股票」，这里是库内的一次批量动作。
        self.assertIn('class="btn btn-sm" id="btn-refresh-all"', HTML)

    def test_the_group_has_a_style(self):
        self.assertIn(".library-head-right {", CSS)


class TestItStaysOneAtATime(unittest.TestCase):
    def test_the_loop_awaits_each_stock(self):
        body = _refresh_all_body()
        self.assertIn("for (let i = 0; i < codes.length; i++)", body)
        self.assertIn("await api('/api/research/analyze'", body)

    def test_the_loop_is_not_parallel(self):
        # 一次一只。并发 analyze 会互相插队写同一张快照表，也会让「Δ评分」看到中间态。
        self.assertNotIn("Promise.all", _refresh_all_body())

    def test_it_refuses_to_start_while_something_else_is_busy(self):
        self.assertIn("if (state.busy || state.bulkBusy) return;", _refresh_all_body())

    def test_it_does_not_refetch_financials(self):
        # 重抓财务是单只「重新分析财务」的活；全库重抓 25 只的定期报告是另一件事。
        body = _refresh_all_body()
        self.assertIn("force_financials: false", body)
        self.assertNotIn("force_financials: true", body)


class TestOneFailureDoesNotStopTheRest(unittest.TestCase):
    def test_each_stock_is_caught_inside_the_loop(self):
        body = _refresh_all_body()
        loop = _slice(body, "for (let i = 0; i < codes.length; i++)",
                      "if ((i + 1) % REFRESH_ALL_EVERY")
        self.assertIn("catch (e) { failed.push(", loop, "单只失败会把整轮刷新中断")

    def test_the_failures_are_reported(self):
        body = _refresh_all_body()
        self.assertIn("failed.length", body, "失败只记不报，用户看不到哪几只没刷上")

    def test_an_empty_library_is_answered_not_crashed(self):
        self.assertIn("if (!codes.length)", _refresh_all_body())


class TestTheGatesAreAlwaysRestored(unittest.TestCase):
    def test_the_busy_flags_come_back_in_finally(self):
        body = _refresh_all_body()
        tail = _slice(body, "} finally {", "\n}")
        self.assertIn("state.bulkBusy = false", tail)
        self.assertIn("state.busy = false", tail)

    def test_the_button_label_and_disabled_state_come_back(self):
        tail = _slice(_refresh_all_body(), "} finally {", "\n}")
        self.assertIn("btn.disabled = false", tail)
        self.assertIn("btn.textContent = REFRESH_ALL_LABEL", tail)

    def test_the_progress_is_written_where_it_survives(self):
        # toast 2.6 秒就消失，而这一轮要十几秒 —— 进度得写在按钮上。
        self.assertIn("btn.textContent = `刷新中 ${i + 1}/${codes.length}`", _refresh_all_body())

    def test_the_open_detail_is_reloaded_too(self):
        # 详情开着不动、左边批量刷完，右边还挂着旧价格是最像故障的样子。
        # reloadDetail() 自己会在「没开详情」时 return，所以这里无条件调即可。
        self.assertIn("await reloadDetail();", _refresh_all_body())


class TestWhatItDoesNotDo(unittest.TestCase):
    def test_the_single_stock_analyze_is_untouched(self):
        block = re.search(r"async function analyze\(code, forceFinancials\) \{\n(.*?)\n\}",
                          JS, re.S)
        self.assertIsNotNone(block, "analyze() 没了？")
        # 借它的闸门（state.busy），但不许把批量逻辑塞进它里面。
        self.assertIn("if (state.busy) return;", block.group(1))
        self.assertNotIn("bulkBusy", block.group(1))

    def test_no_bulk_endpoint_was_added_to_the_server(self):
        # 串行、进度、门禁复用都是现成的，服务端一行都不用动。
        self.assertIn('if method == "POST" and path == "/api/research/analyze":', SERVER_PY)
        for path in ("/api/research/refresh-all", "/api/research/analyze-all",
                     "/api/research/bulk"):
            self.assertNotIn(path, SERVER_PY)


if __name__ == "__main__":
    unittest.main()
