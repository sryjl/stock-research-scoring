# -*- coding: utf-8 -*-
"""tests/test_engine_modes.py — ``analyze`` 的四种持久化模式（批 6 §六–§十）。

批 6 之前，**「调用 analyze()」就等于「写历史」**：跑一次测试、试算一次新权重、
做一次 A/B 对拍，都会往 ``research_snapshots`` / ``research_stocks`` 里留下真行。
这一组钉住的就是「分开了没有」，以及**分开的边界画在哪**：

* 结果是历史（``research_snapshots`` / ``model_route_snapshot`` / ``research_stocks``
  / ``factor_*``）——**只有** ``PERSIST`` 能写；
* 输入缓存（financial_cache / valuation_history / peer / market）是我们查过什么，
  ``DRY_RUN`` 写它不产生历史，``TEST``/``COMPARE`` 连它也不碰；
* ``TEST``/``COMPARE`` **一次网都不联**——这一条不是「尽量」，是拿会抛异常的
  provider 试出来的（见 :class:`_BoomProvider`）。

默认值是 ``DRY_RUN``：光调 ``analyze()`` 不等于「写历史」，生产三处显式开口。
生产那三处是不是真的开了口，由**读源码**钉住（漏一处就红）——不靠人眼。

全部离线、不联网、不碰真库（临时库 + ``DEFAULT_PATH`` 打桩）。
"""
import inspect
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.db as research_db  # noqa: E402
from research import engine, factor_store  # noqa: E402
from tests.test_audit_gate import _stock_row  # noqa: E402
from tests.test_research import _synth_fin  # noqa: E402

CODE = "600502"

#: 会写**结果**的四张表。COMPARE / DRY_RUN / TEST 跑完，这四张一行都不许动。
_RESULT_TABLES = ("research_snapshots", "model_route_snapshot", "research_stocks",
                  "factor_analysis_runs")


class _BoomProvider(object):
    """谁碰它谁炸。``TEST``/``COMPARE`` 下它必须**一次都没被碰过**。"""

    def __getattr__(self, name):
        raise AssertionError("这一模式不该联网：get_provider().%s 被调用了" % name)


class _FakeProvider(object):
    """只够跑完一条分析路径：行情 + 「最新报告期查不到」（于是走财务缓存）。"""

    def get_quote(self, code):
        return {"code": code, "name": "测试" + code, "price": 10.0,
                "total_market_cap": 1.0e10, "total_shares": None}

    def get_latest_report_period(self, code):
        return None


def _bundle_context():
    """一份形状**合法**的合成上下文：不联网，但四组都不是 None。

    刻意不走全空那条路——COMPARE 的好处就在于两臂共用同一份上下文，
    context 全空会让「冻结包真的被用上了没有」这条断言测不到东西。

    ``peer`` 必须是 ``{"available": False, ...}`` 这个形状而不是随便一个 dict：
    ``factors._peer_result`` 的契约是「不可用要显式声明」，少一个键它会直接
    在 ``peer.get("valuation")`` 上炸（这正是它该有的严格）。
    """
    return {"peer": {"available": False, "reason": "测试：不联网"},
            "market": {"status": "missing", "reasons": {"*": "测试：不联网"}},
            "pe_series": [("2026-08", 12.0), ("2026-09", 13.0)],
            "pig": {"exposure": None, "classification": None, "confidence": 0.0,
                    "readings": {}},
            "unmapped_industries": []}


class ModeTestCase(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)
        # engine / audit_job 走 db.connect()，读的是模块级 DEFAULT_PATH；
        # 不指过来它们会写到真的 research.db 上。
        self._patch = mock.patch.object(research_db, "DEFAULT_PATH", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._cleanup)
        self._seed()
        self._offline()

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _seed(self):
        """一只**已审计**的股票：主记录 + 资产语义快照（门禁的完成判据）+ 财务缓存。"""
        # factor_* 三张表是 factor_store 自带的 SCHEMA，init_db 不建；先建出来，
        # 「跑了多少行」才是一个从 0 开始数的真计数。
        factor_store.ensure_schema(self.conn)
        research_db.upsert_stock(self.conn, _stock_row(CODE))
        self.conn.execute(
            "INSERT INTO asset_semantic_snapshot (stock_code, date, metric_version,"
            " report_period, total_assets, source_document)"
            " VALUES (?, '2026-09-01 00:00:00', 'ASSET_SEMANTIC_ENGINE_V1.0',"
            " '2026-06-30', 3e10, '测试')", (CODE,))
        fin = _synth_fin("建筑装饰")
        fin["cache_version"] = engine.FIN_CACHE_VERSION
        research_db.set_financial_cache(self.conn, CODE, fin, "2026-06-30")
        self.conn.commit()

    def _offline(self):
        """把「联网取外部数据」那两处关掉，换成形状完整的合成值。

        关掉的是**联网**，不是副作用：`_research_context` 与
        `valuation_history.ensure` 在 PERSIST/DRY_RUN 下本来就会被调用，这里
        只是让它们不真的出门。TEST/COMPARE 下它们**不该被调用**——那两条由
        `_assert_untouched` 单独钉。
        """
        for name, value in (
                ("ensure", lambda *a, **k: None),
                ("pb_series", lambda *a, **k: []),
                ("_research_context", lambda *a, **k: _bundle_context())):
            target = engine.valuation_history if name in ("ensure", "pb_series") else engine
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    # ---------------------------------------------------------------- #
    def _counts(self):
        return {name: self.conn.execute(
            "SELECT COUNT(*) FROM %s" % name).fetchone()[0]
            for name in _RESULT_TABLES}

    def _run(self, **kw):
        with mock.patch.object(engine, "get_provider", _FakeProvider):
            return engine.analyze(CODE, **kw)

    @staticmethod
    def _score(out):
        """``analyze`` 的返回值里总分叫 ``final.score``（``total_score`` 是落库列名）。"""
        return out["final"]["score"]

    def _freeze(self, **kw):
        """冻结也要挡掉真 provider——``freeze_market`` 那一次是**真联网**的。"""
        with mock.patch.object(engine, "get_provider", _FakeProvider):
            return engine.freeze_market(CODE, **kw)

    def _assert_untouched(self, before, msg):
        self.assertEqual(before, self._counts(), msg)


# --------------------------------------------------------------------------- #
# §十一 默认值：签名事实，不是文档承诺
# --------------------------------------------------------------------------- #
class TestTheDefaultMode(unittest.TestCase):
    def test_analyze_defaults_to_dry_run(self):
        """默认必须是 DRY_RUN——批 6 那句「光调它不等于写历史」唯一的落点。"""
        sig = inspect.signature(engine.analyze)
        self.assertEqual(sig.parameters["mode"].default, engine.MODE_DRY_RUN)
        self.assertEqual(sig.parameters["mode"].default, "DRY_RUN")

    def test_freeze_market_defaults_to_dry_run(self):
        """冻结点默认只刷输入缓存：对拍要的就是「最新的输入」，不是「最新的历史」。"""
        sig = inspect.signature(engine.freeze_market)
        self.assertEqual(sig.parameters["mode"].default, engine.MODE_DRY_RUN)

    def test_the_four_modes_are_what_they_say(self):
        """门禁表与常量必须自洽——写着一张、说着一张是最容易出的错。"""
        self.assertEqual(set(engine.MODES),
                         {engine.MODE_PERSIST, engine.MODE_DRY_RUN,
                          engine.MODE_TEST, engine.MODE_COMPARE})
        self.assertEqual(sorted(engine.MODES), sorted(set(engine.MODES)))
        for mode in engine.MODES:
            self.assertEqual(engine.writes_results(mode), mode == engine.MODE_PERSIST)
            self.assertEqual(engine.writes_input_cache(mode),
                             mode in (engine.MODE_PERSIST, engine.MODE_DRY_RUN))

    def test_an_unknown_mode_is_rejected_not_downgraded(self):
        """认不出来就抛。静默按「不写」或「全写」办都是在骗调用方。"""
        for fn in (engine.writes_results, engine.writes_input_cache):
            with self.assertRaises(ValueError):
                fn("PERSIST ")                      # 多个空格都不行：不做模糊匹配
            with self.assertRaises(ValueError):
                fn("persist")
            with self.assertRaises(ValueError):
                fn(None)


# --------------------------------------------------------------------------- #
# §七 DRY_RUN：不写历史，但读到的分与 PERSIST 逐位相同
# --------------------------------------------------------------------------- #
class TestDryRun(ModeTestCase):
    def test_dry_run_writes_no_history_but_still_scores(self):
        before = self._counts()
        dry = self._run(mode=engine.MODE_DRY_RUN)
        self._assert_untouched(before, "DRY_RUN 往结果表里写了东西")
        self.assertIsNotNone(self._score(dry), "DRY_RUN 没算出分")
        self.assertFalse(dry["persisted"])
        self.assertEqual(dry["mode"], engine.MODE_DRY_RUN)

        live = self._run(mode=engine.MODE_PERSIST)
        self.assertTrue(live["persisted"])
        # **同一个输入、同一条路径，只是写不写的区别**——分数必须逐位相等。
        self.assertEqual(self._score(dry), self._score(live),
                         "DRY_RUN 算出来的分与 PERSIST 不同，那它就不是试算")
        self.assertEqual(dry["route"]["primary_model"], live["route"]["primary_model"])

    def test_persist_is_the_only_mode_that_moves_the_counts(self):
        """四种模式跑一遍，只有 PERSIST 让结果表的行数动。"""
        for mode in (engine.MODE_TEST, engine.MODE_DRY_RUN):
            before = self._counts()
            self._run(mode=mode)
            self._assert_untouched(before, "%s 动了结果表" % mode)

        before = self._counts()
        self._run(mode=engine.MODE_COMPARE, frozen=self._freeze())
        self._assert_untouched(before, "COMPARE 动了结果表")

        before = self._counts()
        self._run(mode=engine.MODE_PERSIST)
        after = self._counts()
        self.assertGreater(after["research_snapshots"], before["research_snapshots"])
        self.assertEqual(after["factor_analysis_runs"],
                         before["factor_analysis_runs"] + 1)

    def test_dry_run_still_refreshes_the_input_cache(self):
        """输入缓存是「我们查过什么」，不是结论——DRY_RUN 落它不产生历史。"""
        called = []
        with mock.patch.object(engine.valuation_history, "ensure",
                               lambda *a, **k: called.append("ensure")), \
                mock.patch.object(engine, "_research_context",
                                  lambda *a, **k: _bundle_context()):
            self._run(mode=engine.MODE_DRY_RUN)
        self.assertEqual(called, ["ensure"], "DRY_RUN 没刷输入缓存，下次试算还得重联网")


# --------------------------------------------------------------------------- #
# §八 TEST：零写、零联网
# --------------------------------------------------------------------------- #
class TestTestMode(ModeTestCase):
    def test_test_mode_writes_nothing_and_never_reaches_the_network(self):
        """`_research_context` 与 `valuation_history.ensure` 一次都不许被调用。

        用**会炸的替身**而不是计数：计数只能证明「我没看见」，替身能证明
        「真的没走到那一步」。
        """
        def boom(*a, **k):
            raise AssertionError("TEST 模式不该联网/不该写输入缓存")

        before = self._counts()
        with mock.patch.object(engine, "_research_context", boom), \
                mock.patch.object(engine.valuation_history, "ensure", boom):
            out = self._run(mode=engine.MODE_TEST)
        self._assert_untouched(before, "TEST 写了库")
        self.assertIsNotNone(self._score(out), "TEST 没跑完")
        self.assertFalse(out["persisted"])
        self.assertEqual(out["mode"], engine.MODE_TEST)

    def test_test_mode_still_reads_the_quote(self):
        """零联网 ≠ 零输入：行情那一次仍然走 provider（测试里是假的那个）。

        这条划的是边界——「TEST 连行情都不取」会让它变成另一条产品路径，
        而它要证明的恰恰是**同一套算法**在不写库时给出同一个分。
        """
        seen = []

        class Recording(_FakeProvider):
            def get_quote(self, code):
                seen.append(code)
                return _FakeProvider.get_quote(self, code)

        with mock.patch.object(engine, "get_provider", Recording):
            engine.analyze(CODE, mode=engine.MODE_TEST)
        self.assertEqual(seen, [CODE])


# --------------------------------------------------------------------------- #
# §九 / §十 COMPARE：两臂共用一份冻结输入
# --------------------------------------------------------------------------- #
class TestCompareMode(ModeTestCase):
    def test_two_arms_share_the_frozen_inputs_and_never_touch_the_provider(self):
        """冻结的意义：两臂之间把外部世界换掉，结果**一动不动**。"""
        bundle = self._freeze()
        first = self._run(mode=engine.MODE_COMPARE, frozen=bundle)
        before = self._counts()
        # 第二臂之前把 provider 换成会炸的：frozen 路径只要碰它一下就红。
        with mock.patch.object(engine, "get_provider", _BoomProvider):
            second = engine.analyze(CODE, mode=engine.MODE_COMPARE, frozen=bundle)
        self._assert_untouched(before, "COMPARE 写了库")
        self.assertEqual(self._score(first), self._score(second))
        self.assertEqual(first["route"], second["route"])

    def test_a_frozen_bundle_overrides_the_live_world(self):
        """冻结包说了算，不是「参考一下」。把包里的价格改掉，分就该跟着动。

        反过来说：如果分**不**动，说明冻结包根本没被用上，两臂比的还是各自的
        现场行情——那正是这一批要消灭的假 A/B。
        """
        cheap = self._freeze()
        expensive = dict(cheap, quote=dict(cheap["quote"], price=999.0,
                                           total_market_cap=9.99e11))
        a = self._run(mode=engine.MODE_COMPARE, frozen=cheap)
        b = self._run(mode=engine.MODE_COMPARE, frozen=expensive)
        self.assertNotEqual(self._score(a), self._score(b),
                            "改了冻结包里的行情，结果没变——冻结包没被用上")

    def test_the_same_frozen_inputs_produce_one_snapshot_not_two(self):
        """A/B 的判据就是这句：两臂落在**同一个** ``result_hash`` 上。

        直接复用 ``add_snapshot_if_changed`` 的幂等去重（``snapshot_result_hash``
        不含 ``date``），所以「两臂结果是否相同」不需要另造一套比较口径。
        """
        bundle = self._freeze()
        for _ in range(2):
            self._run(mode=engine.MODE_PERSIST, frozen=bundle)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM research_snapshots WHERE stock_code=?",
            (CODE,)).fetchone()[0]
        self.assertEqual(rows, 1, "同一份冻结输入跑两臂却写了两行快照")


# --------------------------------------------------------------------------- #
# §十一 生产三处必须**显式**开口
# --------------------------------------------------------------------------- #
class TestTheProductionCallSitesSayPersist(unittest.TestCase):
    """读源码钉住，而不是靠人眼。

    ``analyze`` 的默认从「写」变成「不写」是**行为变更**：任何一处生产调用点
    漏了 ``MODE_PERSIST``，用户就会看到「跑完了但分数没进库」。三处全在
    ``server.py``（analyze / refresh）与 ``research/audit_job.py``（审计补分）。
    """

    @staticmethod
    def _text(rel):
        return pathlib.Path(ROOT, rel).read_text(encoding="utf-8")

    def test_server_has_exactly_two_persist_call_sites(self):
        self.assertEqual(self._text("server.py").count("mode=research_engine.MODE_PERSIST"),
                         2, "server.py 的 analyze / refresh 必须各自显式 PERSIST")

    def test_the_audit_worker_has_exactly_one(self):
        self.assertEqual(
            self._text(os.path.join("research", "audit_job.py"))
            .count("mode=research_engine.MODE_PERSIST"),
            1, "审计线程补分那一处必须显式 PERSIST，否则审计跑完分数不进库")

    @staticmethod
    def _call_args(text):
        """每处 ``research_engine.analyze(...)`` 的实参原文（按括号配对切）。

        不能用正则 ``\\(([^)]*)\\)``：实参里就有 ``data.get("code")``，第一个
        ``)`` 会把调用切一半，于是「没写 mode」这条断言永远看的是半句话。
        """
        out, at = [], 0
        needle = "research_engine.analyze("
        while True:
            at = text.find(needle, at)
            if at < 0:
                return out
            depth, i = 0, at + len(needle) - 1
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            out.append(text[at + len(needle):i])
            at = i

    def test_no_production_module_calls_analyze_without_a_mode(self):
        """反向检查：生产模块里不该有「裸调 analyze」——那等于默认 DRY_RUN。"""
        for rel in ("server.py", os.path.join("research", "audit_job.py")):
            calls = self._call_args(self._text(rel))
            self.assertTrue(calls, "%s 里一处 analyze 调用都没有？读错文件了" % rel)
            for call in calls:
                self.assertIn("mode=", call,
                              "%s 里有一处 analyze 调用没写 mode：%r" % (rel, call))


if __name__ == "__main__":
    unittest.main()
