# -*- coding: utf-8 -*-
"""tests/test_audit_gate.py — 资产审计门禁（第三期：审计挪到评分之前）。

要修的根因不是某一只股票，是**顺序**：先评分、后审计。落快照不回头重算评分行，
于是主记录里永远存着分析那一刻的副本——安徽建工 600502 的资产快照其实有 29 项
明细，详情页却显示「资产负债表数据缺失」，因为库里那份 ``financial_json`` 写的是
「尚无 ASSET_SEMANTIC_ENGINE_V1.0 快照（未解析过该股定期报告）」。

这一层钉住三件事，以及几条一旦写反就会造成最坏后果的守卫：

1. **「完成」是派生的**（有可用快照就算完成），所以库里永远不许出现 ``'OK'``。
   留一个能写 'OK' 的入口，状态标签迟早会替一个评分层并不认的口径背书。
2. **门禁在财务抓取之前**：未审计的股票不写 financial_cache、不写任何快照——
   没过审计的分数永远进不了历史序列。写反了等于门禁不存在。
3. **读侧兜底**：门禁上线之前落下的、带着分数的行，用户也不许看见它的分数。
   读侧挡一道，那批历史行就不需要迁移脚本。
"""
import copy
import dis
import inspect
import json
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

import research.audit_job as audit_job  # noqa: E402
import research.db as research_db  # noqa: E402
from research import asset_metrics, engine, router, rules  # noqa: E402

JS = (ROOT / "static" / "research.js").read_text(encoding="utf-8")
SERVER_PY = (ROOT / "server.py").read_text(encoding="utf-8")

#: 600502 主记录里那句原话。它必须永远不出现在任何发给界面的对象里。
STALE_NOTE = "尚无 ASSET_SEMANTIC_ENGINE_V1.0 快照（未解析过该股定期报告）"


def _route_accesses(fn):
    """函数体里访问 ``.route`` 的次数，逐条字节码数，**不看文档字符串**。

    文档字符串里正需要写出 ``router.route(...)`` 来说明「这一次调用在哪儿」，
    按源码文本数会在说明文字上误报（tests/test_router.py 同理）。
    """
    return sum(1 for i in dis.get_instructions(fn)
               if i.opname == "LOAD_ATTR" and i.argval == "route")


class _Boom(Exception):
    """探针异常：谁拿到它就证明代码走到了那一步。"""


class FakeProvider(object):
    """只够跑门禁路径的 Provider。

    门禁只取一次行情（``get_quote``），不抓财务；给了可用快照之后同一条路会走到
    ``fetch_financials``，所以这里也备着 ``get_latest_report_period``——它返回
    ``None`` 表示「查不到最新报告期」，缓存又不存在，于是必然去抓。
    """

    def __init__(self, name="测试股票", price=10.0, market_cap=20e9):
        self.name, self.price, self.market_cap = name, price, market_cap
        self.quotes = 0

    def get_quote(self, code):
        self.quotes += 1
        return {"code": code, "name": self.name, "price": self.price,
                "total_market_cap": self.market_cap}

    def get_latest_report_period(self, code):
        return None


def _stock_row(code, **over):
    """一行的**全部**命名列。测试要构造的是「库里已经存在的形状」——包括门禁
    上线之前落下的、带着分数与资产副本的行，所以走 ``upsert_stock`` 那条 29 列
    的写入路径，而不是手写 INSERT 列清单（列名抄错就测不成东西了）。"""
    rec = {
        "code": code, "name": "测试" + code, "board": "MAIN_SH", "industry": "测试行业",
        "system_type": "value", "user_type": "value", "type_confidence": 0.9,
        "risk_level": "GREEN", "total_score": 78.5, "rule_version": rules.RULE_VERSION,
        "latest_report_period": "2026-06-30",
        "attr_scores_json": json.dumps({"value": {"score": 80.0}}),
        "category_scores_json": json.dumps([{"key": "valuation", "score": 80.0}]),
        "valuation_json": json.dumps({"price": 10.0, "adjusted_net_cash_to_mcap": 0.4}),
        "financial_json": json.dumps({"asset_model": {"note": "正常的一份资产副本"}}),
        "risk_json": json.dumps({"level": "GREEN"}),
        "data_completeness": 0.8,
        "first_analyzed_at": "2026-09-01 10:00:00",
        "last_updated_at": "2026-09-02 10:00:00",
        "financial_updated_at": "2026-09-02 10:00:00",
        "router_version": router.ROUTER_VERSION, "primary_model": router.MODEL_DIVIDEND,
        "secondary_model": None, "primary_fit": 0.7, "secondary_fit": None,
        "route_status": "OK", "route_confidence": 0.8, "route_coverage": 0.9,
        "route_json": json.dumps({"reasons": ["测试"]}),
    }
    rec.update(over)
    return rec


def _score_snapshot(code, score, date):
    snap = {
        "stock_code": code, "date": date, "current_price": 10.0,
        "report_period": "2026-06-30", "rule_version": rules.RULE_VERSION,
        "type_scores": {"value": 80.0}, "financial_metrics": {"revenue": [1, 2]},
        "valuation_metrics": {"pb": 1.0}, "risk_flags": [], "total_score": score,
        "confidence": 1.0, "category_scores": [{"key": "valuation", "score": 80.0}],
        "profile_version": "PROFILE_SCORING_V1.2",
    }
    snap["result_hash"] = engine.snapshot_result_hash(snap, None)
    return snap


class AuditGateTestCase(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)
        # engine / audit_job 走的是 db.connect()，它读模块级 DEFAULT_PATH；
        # 不指过来它们会写到真的 research.db 上。
        self._patch = mock.patch.object(research_db, "DEFAULT_PATH", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    # ---------------------------------------------------------------- #
    # 夹具
    # ---------------------------------------------------------------- #
    def _seed_row(self, code, **over):
        research_db.upsert_stock(self.conn, _stock_row(code, **over))

    def _give_snapshot(self, code, total_assets=30e9, date="2026-09-01 00:00:00"):
        """落一份**可用**的资产语义快照（门禁的完成判据）。"""
        self.conn.execute(
            "INSERT INTO asset_semantic_snapshot (stock_code, date, metric_version,"
            " report_period, total_assets, source_document)"
            " VALUES (?, ?, 'ASSET_SEMANTIC_ENGINE_V1.0', '2026-06-30', ?, '测试')",
            (code, date, total_assets))
        self.conn.commit()

    def _counts(self):
        return (self.conn.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0],
                self.conn.execute("SELECT COUNT(*) FROM model_route_snapshot").fetchone()[0])

    def _table(self, name, code):
        return [tuple(r) for r in self.conn.execute(
            f"SELECT * FROM {name} WHERE stock_code=? ORDER BY id", (code,))]

    def _audit_cols(self, code):
        return dict(self.conn.execute(
            "SELECT audit_status, audit_error, audit_started_at, audit_finished_at,"
            " audit_ok_at FROM research_stocks WHERE code=?", (code,)).fetchone())

    def _analyze(self, code):
        """走生产路径，只把行情换成假的——门禁路径本来就只有这一次外部请求。

        **显式 MODE_PERSIST**：批 6 起 ``analyze`` 默认 DRY_RUN（不落库）。本组
        测的正是「生产调用会写什么」——门禁落行、标 RUNNING、清状态——所以要照
        生产入口那样显式开口。默认 DRY_RUN 那条路径由 tests/test_engine_modes.py
        单独钉住。
        """
        with mock.patch.object(engine, "get_provider", FakeProvider):
            return engine.analyze(code, mode=engine.MODE_PERSIST)

    def _listed(self, code):
        return next(r for r in engine.list_stocks() if r["code"] == code)


# --------------------------------------------------------------------------- #
# 状态模型：四档，且「完成」是派生的
# --------------------------------------------------------------------------- #
class TestStatusModel(AuditGateTestCase):
    def test_no_snapshot_no_status_reads_unaudited(self):
        self._seed_row("600502")
        self.assertEqual(audit_job.status_of(self.conn, "600502"), audit_job.UNAUDITED)

    def test_a_queued_stock_reads_running(self):
        self._seed_row("600502")
        research_db.mark_stock_audit(self.conn, "600502", "RUNNING")
        self.assertEqual(audit_job.status_of(self.conn, "600502"), audit_job.RUNNING)

    def test_a_failed_stock_reads_failed(self):
        self._seed_row("600502")
        research_db.mark_stock_audit(self.conn, "600502", "FAILED", "该股票没有可解析的定期报告")
        self.assertEqual(audit_job.status_of(self.conn, "600502"), audit_job.FAILED)
        # 原因要能原样递给用户，否则「审计失败」四个字等于没说。
        self.assertEqual(research_db.get_stock(self.conn, "600502")["audit_error"],
                         "该股票没有可解析的定期报告")

    def test_a_usable_snapshot_wins_over_a_failed_status(self):
        """**有快照就算完成**：审计中途崩过、但产出物已经落库的股票是「已审计」。

        审计的产出物在，过程状态没资格推翻它。反过来写（状态优先）会让一只
        已经能算分的股票被自己的失败记录挡死，且永远挡死——重试按钮点一次失败
        一次，因为它重试的正是那份已经成功的审计。
        """
        self._seed_row("600502")
        research_db.mark_stock_audit(self.conn, "600502", "FAILED", "崩在中途")
        self._give_snapshot("600502")
        self.assertEqual(audit_job.status_of(self.conn, "600502"), audit_job.OK)

    def test_a_snapshot_without_total_assets_is_not_complete(self):
        """快照存在但没认出资**产总计**，等于没跑出来——判据与评分层同一处。

        002460 在库里就留着这样一行 NULL 快照：当时被当成了一次成功结果。
        """
        self._seed_row("600502")
        self._give_snapshot("600502", total_assets=None)
        self.assertEqual(audit_job.status_of(self.conn, "600502"), audit_job.UNAUDITED)


# --------------------------------------------------------------------------- #
# db 层：状态列、过程状态、恢复
# --------------------------------------------------------------------------- #
class TestAuditColumnsInTheDb(AuditGateTestCase):
    def test_marking_ok_is_refused(self):
        self._seed_row("600502")
        with self.assertRaises(ValueError):
            research_db.mark_stock_audit(self.conn, "600502", "OK")
        self.assertIsNone(self._audit_cols("600502")["audit_status"],
                          "拒绝之后状态列被动过了")

    def test_failure_keeps_the_starting_time_and_retry_clears_the_error(self):
        """「什么时候开始试的」与「什么时候失败的」是两件事，都要留。

        重试（``FAILED`` → ``RUNNING``）则相反：清掉上一轮的原因、把排队时刻重置
        成这一轮——队列是按那个时刻先来先跑的，不重置就等于插到所有人前面；重置
        得不彻底（留着原因）会让用户看到一条早已不成立的失败理由。
        """
        self._seed_row("600502")
        self.conn.execute("UPDATE research_stocks SET audit_status='RUNNING',"
                          " audit_started_at='2026-09-01 00:00:00' WHERE code='600502'")
        self.conn.commit()

        research_db.mark_stock_audit(self.conn, "600502", "FAILED", "第一次的原因")

        failed = self._audit_cols("600502")
        self.assertEqual(failed["audit_status"], "FAILED")
        self.assertEqual(failed["audit_error"], "第一次的原因")
        self.assertEqual(failed["audit_started_at"], "2026-09-01 00:00:00",
                         "FAILED 覆盖掉了开始时刻")
        self.assertIsNotNone(failed["audit_finished_at"])

        research_db.mark_stock_audit(self.conn, "600502", "RUNNING")     # 重试

        retried = self._audit_cols("600502")
        self.assertEqual(retried["audit_status"], "RUNNING")
        self.assertIsNone(retried["audit_error"], "重试没有清掉上一轮的原因")
        self.assertIsNone(retried["audit_finished_at"])
        self.assertNotEqual(retried["audit_started_at"], "2026-09-01 00:00:00",
                            "重试没有把排队时刻重置成这一轮")

    def test_clearing_the_process_state_leaves_audit_ok_at_alone(self):
        self._seed_row("600502")
        self.conn.execute("UPDATE research_stocks SET audit_status='RUNNING',"
                          " audit_error='x', audit_started_at='2026-09-01 09:00:00',"
                          " audit_finished_at='2026-09-01 09:05:00',"
                          " audit_ok_at='2026-09-01 09:05:00' WHERE code='600502'")
        self.conn.commit()
        research_db.clear_stock_audit(self.conn, "600502")
        cols = self._audit_cols("600502")
        self.assertEqual(cols, {"audit_status": None, "audit_error": None,
                                "audit_started_at": None, "audit_finished_at": None,
                                "audit_ok_at": "2026-09-01 09:05:00"},
                         "清过程状态时把 audit_ok_at 一起清掉了——那是口径切换的历史事实")

    def test_recovery_clears_finished_rows_and_keeps_the_queue(self):
        """启动时对齐真相：已完成的清掉，**没做完的留着**。

        把 ``RUNNING`` 清成 NULL 等于把用户点过的那次「分析」丢掉（队列就是这一列）。
        """
        self._seed_row("600502")                                    # 有快照 + 残留状态
        self._seed_row("601163")                                    # 没快照 + 排队中
        self._give_snapshot("600502")
        research_db.mark_stock_audit(self.conn, "600502", "FAILED", "崩在中途")
        research_db.mark_stock_audit(self.conn, "601163", "RUNNING")

        cleared = research_db.recover_stale_audits(self.conn)

        self.assertEqual(cleared, 1)
        self.assertIsNone(self._audit_cols("600502")["audit_status"])
        self.assertEqual(self._audit_cols("601163")["audit_status"], "RUNNING",
                         "排队中的股票被启动清理丢掉了")


class TestAvailableCodes(AuditGateTestCase):
    def test_it_agrees_with_per_stock_load(self):
        """``available_codes`` 与逐只 ``load().available`` 必须逐只一致。

        两处判据一旦分叉，就会出现「页面说已完成、评分层却看不到数」——这是最坏
        的组合：挡住分数的理由和页面上的完成状态对着干，用户无从判断该信哪个。
        所以连「最新一行是 NULL、旧行有值」这种形态也一起比。
        """
        self._seed_row("600502"); self._give_snapshot("600502")
        self._give_snapshot("600502", total_assets=None, date="2026-09-20 00:00:00")
        self._seed_row("601163"); self._give_snapshot("601163", total_assets=None)
        self._seed_row("600741"); self._give_snapshot("600741", total_assets="30e9")
        self._seed_row("000001")                                    # 一行快照都没有

        batch = asset_metrics.available_codes(self.conn)
        for code in ("600502", "601163", "600741", "000001", "不存在的代码"):
            one = asset_metrics.load(self.conn, code).available
            self.assertEqual(code in batch, one,
                             f"{code}：批量判定与逐只 load 不一致（会不会页面说完成、评分层看不到数）")

    def test_the_old_row_with_a_value_does_not_make_it_available(self):
        """最新一行是 NULL 时整只算不可用——旧行的值不能顶上来（那会是一份旧口径）。"""
        self._seed_row("600502")
        self._give_snapshot("600502", total_assets=30e9, date="2026-09-01 00:00:00")
        self._give_snapshot("600502", total_assets=None, date="2026-09-20 00:00:00")
        self.assertNotIn("600502", asset_metrics.available_codes(self.conn))
        self.assertEqual(audit_job.status_of(self.conn, "600502"), audit_job.UNAUDITED)


# --------------------------------------------------------------------------- #
# 门禁：analyze 挡住未审计的股票
# --------------------------------------------------------------------------- #
class TestTheGateInAnalyze(AuditGateTestCase):
    def test_an_unaudited_stock_gets_no_score_and_no_snapshots(self):
        before = self._counts()
        self._analyze("600502")

        row = research_db.get_stock(self.conn, "600502")
        self.assertIsNotNone(row, "门禁没把主记录落下来，列表里根本看不到这只股票")
        self.assertIsNone(row["total_score"])
        self.assertIsNone(row["rule_version"])
        self.assertIsNone(row["financial_json"], "未审计的股票写了财务摘要副本")
        self.assertIsNone(row["attr_scores_json"])
        self.assertEqual(row["audit_status"], "RUNNING")
        self.assertEqual(json.loads(row["valuation_json"]), {"price": 10.0},
                         "价格是市场事实，不该被门禁连坐清掉")
        self.assertEqual(self._counts(), before,
                         "未审计的股票写进了历史序列（评分快照或路由快照）")
        self.assertEqual(audit_job._claim(self.conn), "600502",
                         "入队的那只不在队列里——队列就是 audit_status='RUNNING' 这一列")

    def test_the_result_carries_the_status_instead_of_a_score(self):
        out = self._analyze("600502")
        self.assertTrue(out["gate"])
        self.assertIsNone(out["total_score"])
        self.assertEqual(out["audit_status"], audit_job.RUNNING)
        self.assertEqual(out["audit_status_label"], "审计中")
        self.assertNotIn("route", out, "门禁路径不该产出路由结论")

    def test_running_is_not_requeued(self):
        """已经在跑的股票再分析一次，不许重置 ``audit_started_at``。

        队列是按 ``audit_started_at`` 先来先跑的；每次刷新价格都把它重置成现在，
        排队顺序就被打乱了，先入队的股票可能永远排不到。
        """
        self._analyze("600502")
        started = self._audit_cols("600502")["audit_started_at"]
        self.conn.execute("UPDATE research_stocks SET audit_started_at='2026-09-01 00:00:00'"
                          " WHERE code='600502'")
        self.conn.commit()
        self._analyze("600502")
        self.assertEqual(self._audit_cols("600502")["audit_started_at"], "2026-09-01 00:00:00",
                         "重复入队把排队顺序重置了")

    def test_the_gate_stands_before_the_financial_fetch(self):
        """**核心断言**：门禁在抓财务之前。写反了等于门禁不存在。

        两句对照着看：同一只股票，没有快照时 ``fetch_financials`` 一次都不许调；
        给了快照之后同一个调用必须走到那儿。只看前半句是不够的——把门禁写在抓
        财务之后、或者干脆写坏让整条路都走不到抓财务，前半句照样通过。
        """
        with mock.patch.object(engine, "fetch_financials") as fetch:
            self._analyze("600502")
            fetch.assert_not_called()

        self._give_snapshot("600502")
        with mock.patch.object(engine, "fetch_financials", side_effect=_Boom) as fetch:
            with self.assertRaises(_Boom):
                self._analyze("600502")
            self.assertTrue(fetch.called, "有了快照还是没去抓财务——门禁把整条路堵死了")

    def test_the_gate_never_touches_audit_ok_at_or_the_history(self):
        """门禁只落状态，不动 ``audit_ok_at``、不删任何历史快照。

        ``audit_ok_at`` 是口径切换的历史事实，Δ评分靠它把审计之前的快照摘出去；
        顺手改它等于把一批本来就是完整口径算的历史快照追溯性地判成旧口径。
        """
        self._seed_row("600502", financial_json=json.dumps({"asset_model": {"note": STALE_NOTE}}))
        self.conn.execute("UPDATE research_stocks SET audit_ok_at='2026-09-10 09:00:00'"
                          " WHERE code='600502'")
        self.conn.commit()
        research_db.add_snapshot_if_changed(self.conn, _score_snapshot("600502", 77.0, "2026-09-11 10:00:00"))
        research_db.add_snapshot_if_changed(self.conn, _score_snapshot("600502", 78.5, "2026-09-12 10:00:00"))
        snaps = self._table("research_snapshots", "600502")
        self.assertEqual(len(snaps), 2)

        self._analyze("600502")

        self.assertEqual(self._audit_cols("600502")["audit_ok_at"], "2026-09-10 09:00:00")
        self.assertEqual(self._table("research_snapshots", "600502"), snaps,
                         "门禁改动了历史评分快照")
        self.assertIsNone(research_db.get_stock(self.conn, "600502")["total_score"],
                          "停在旧口径上的分数没有被清掉")

    def test_an_audited_stock_reads_back_exactly_as_stored(self):
        """已经有可用快照的股票，读出来的每一个数都还是库里那个——门禁是空操作。

        这是「上线的这一批股票零影响」的判据：门禁只该在**没有**快照时做任何事。
        """
        self._seed_row("600502")
        self._give_snapshot("600502")
        row = self._listed("600502")
        self.assertEqual(row["total_score"], 78.5)
        self.assertEqual(row["attr_scores"], {"value": {"score": 80.0}})
        self.assertEqual(row["risk"], {"level": "GREEN"})
        self.assertEqual(row["risk_level"], "GREEN")
        self.assertEqual(row["system_type"], "value")
        self.assertEqual(row["rule_version"], rules.RULE_VERSION)
        self.assertEqual(row["latest_report_period"], "2026-06-30")
        self.assertEqual(row["valuation"]["adjusted_net_cash_to_mcap"], 0.4)
        self.assertEqual(row["valuation"]["price"], 10.0)
        self.assertEqual(row["net_cash_source"], "canonical")
        self.assertEqual(row["primary_model"], router.MODEL_DIVIDEND)
        self.assertEqual(row["route_coverage"], 0.9)
        self.assertEqual(row["audit_status"], audit_job.OK)
        self.assertEqual(row["audit_status_label"], "已审计")
        detail = engine.get_stock("600502")
        self.assertEqual(detail["financial"], {"asset_model": {"note": "正常的一份资产副本"}})
        self.assertEqual(detail["category_scores"], [{"key": "valuation", "score": 80.0}])
        self.assertEqual(detail["snapshots"], [])


# --------------------------------------------------------------------------- #
# 读侧门禁：门禁上线之前落下的行
# --------------------------------------------------------------------------- #
class TestTheReadSideGate(AuditGateTestCase):
    def test_the_list_blanks_every_scored_field_of_an_unaudited_row(self):
        self._seed_row("600502")
        row = self._listed("600502")
        for name, empty in (("total_score", None), ("attr_scores", {}), ("risk", {}),
                            ("data_completeness", None), ("system_type", None),
                            ("risk_level", None), ("rule_version", None),
                            ("latest_report_period", None), ("primary_model", None),
                            ("primary_fit", None), ("route_coverage", None),
                            ("route_status", None), ("route_confidence", None)):
            self.assertEqual(row[name], empty, f"{name} 没有被门禁清空")
        self.assertEqual(row["valuation"], {"price": 10.0}, "价格被门禁连坐清掉了")
        self.assertEqual(row["adjusted_net_cash_to_mcap"], None)
        self.assertEqual(row["net_cash_source"], "missing",
                         "没有资产层却还报着 canonical 的净现金口径")
        # 手工落下的这一行从没入过队（audit_status 是 NULL），所以读侧报「未审计」：
        # 界面据此把它放进置顶的待审计分组。状态列是谁写的就是什么，读侧不替它编。
        self.assertEqual(row["audit_status"], audit_job.UNAUDITED)
        self.assertEqual(row["audit_status_label"], "未审计")

    def test_the_detail_never_hands_over_the_stale_asset_copy(self):
        """600502 的实况：主记录里存着一份「尚无快照」的资产副本，详情页如实显示
        「资产负债表数据缺失」——如实，但事实是它已经有 29 项明细了。

        这一条测的是**那份旧副本一个字段都不许到界面上**。
        """
        self._seed_row("600502", financial_json=json.dumps({"asset_model": {"note": STALE_NOTE}}))
        out = engine.get_stock("600502")
        self.assertEqual(out["financial"], {})
        self.assertEqual(out["attr_scores"], {})
        self.assertEqual(out["category_scores"], {})
        self.assertEqual(out["risk"], {})
        self.assertEqual(out["snapshots"], [])
        self.assertEqual(out["financial_series"], {"indicators": {}, "cashflow": []})
        self.assertIsNone(out["total_score"])
        self.assertIsNone(out["delta_score"])
        # 这一行从没排过队（audit_status 是 NULL）→ 界面显示「未审计」并把它放进
        # 置顶的待审计分组；用户点一次分析才会变成「审计中」。
        self.assertEqual(out["audit_status"], audit_job.UNAUDITED)
        self.assertNotIn(STALE_NOTE, json.dumps(out, ensure_ascii=False),
                         "「尚无快照」那句旧副本又被递给界面了")

    def test_simulation_stops_at_the_gate(self):
        """未审计的模拟返回 None，**与「没有财务缓存」走同一条路径**。

        前后两句必须对着看，否则这条测试可能是假的：把 ``get_financial_cache``
        换成抛异常，未审计的那次依旧返回 None（说明它压根没去读缓存），而有快照的
        那一次直接炸出来（说明同一条路真的会因为缓存而往下走）。
        """
        self._seed_row("600502")
        before = (self._counts(), self._audit_cols("600502"),
                  research_db.get_stock(self.conn, "600502")["last_updated_at"])
        with mock.patch.object(research_db, "get_financial_cache", side_effect=_Boom) as cache:
            self.assertIsNone(engine.simulate_price("600502", 12.0))
            cache.assert_not_called()
        self.assertEqual((self._counts(), self._audit_cols("600502"),
                          research_db.get_stock(self.conn, "600502")["last_updated_at"]), before,
                         "模拟在未审计的股票上写了库")

        self._give_snapshot("600502")
        with mock.patch.object(research_db, "get_financial_cache", side_effect=_Boom):
            with self.assertRaises(_Boom):
                engine.simulate_price("600502", 12.0)


# --------------------------------------------------------------------------- #
# 审计跑完、分数还没落的那几十秒
# --------------------------------------------------------------------------- #
class TestTheAuditWindow(AuditGateTestCase):
    """worker 落完快照、还没算出分的中间态。

    真实序列（``audit_job._audit_one``）：先 ``asset_engine.save`` 落快照，**回头才**
    调 ``engine.analyze`` 算分，中间要抓一次财务（网络，几秒到几十秒）。那一段库里是
    「有快照 + ``audit_status`` 还是 RUNNING + 主记录全 NULL」。

    按 OK 发出去会怎样：界面把这一行当成一只**数据齐全但没有数**的股票，显示满屏
    「数据缺失 / 评分不足」；轮询又因为没有任何 RUNNING 行而停下，于是停在那儿，非得
    手动刷新才恢复——实测就是用户看到的现象。
    """

    def _enter_the_window(self, code="600502"):
        """用产品路径造出那一瞬，不手写 SQL：先走门禁（入队 + 全 NULL），再补一份
        可用快照（等价于 ``_audit_one`` 里的 ``asset_engine.save``）。"""
        self._analyze(code)                       # → RUNNING + 全 NULL + 不抓财务
        self.assertEqual(self._audit_cols(code)["audit_status"], audit_job.RUNNING)
        self._give_snapshot(code)                 # → 快照有了，分数还没算
        return code

    def test_display_status_only_downgrades_a_snapshot_without_a_score(self):
        self.assertEqual(audit_job.display_status(audit_job.RUNNING, True, False),
                         audit_job.RUNNING, "还在出分，界面该继续轮询")
        self.assertEqual(audit_job.display_status(audit_job.FAILED, True, False),
                         audit_job.FAILED, "审计失败要留着原因和重试按钮")
        self.assertEqual(audit_job.display_status(None, True, False), audit_job.UNAUDITED)
        # 有分数就是已审计；没有快照时与 derive_status 逐档一致（原样透传）。
        self.assertEqual(audit_job.display_status(audit_job.RUNNING, True, True), audit_job.OK)
        for stored in (None, audit_job.RUNNING, audit_job.FAILED, audit_job.OK):
            self.assertEqual(audit_job.display_status(stored, False, False),
                             audit_job.derive_status(stored, False))

    def test_the_write_side_gate_keeps_saying_ok(self):
        """**死锁守卫**：显示降级不许渗进写侧。

        worker 落完快照回头调的就是 ``engine.analyze``，它张嘴先问 ``status_of``。
        这一步要是也跟着降级，算分会被自己的门禁挡回队列——永远算不出分。
        """
        code = self._enter_the_window()
        self.assertEqual(audit_job.status_of(self.conn, code), audit_job.OK,
                         "写侧门禁被显示口径带偏了：worker 再也算不出分")
        self.assertEqual(audit_job.derive_status(audit_job.RUNNING, True), audit_job.OK)

    def test_the_list_does_not_hand_out_an_empty_scored_row(self):
        code = self._enter_the_window()
        row = self._listed(code)
        self.assertEqual(row["audit_status"], audit_job.RUNNING)
        self.assertEqual(row["audit_status_label"], "审计中")
        self.assertIsNone(row["total_score"])
        self.assertIsNone(row["system_type"])
        self.assertIsNone(row["rule_version"])
        self.assertEqual(row["attr_scores"], {})
        self.assertEqual(row["risk"], {})
        self.assertEqual(row["valuation"], {"price": 10.0}, "价格是市场事实，不该被连坐")
        self.assertFalse(row["legacy_rule"], "一只还没有结果的股票挂着「旧实验结果」角标")

    def test_the_detail_does_not_hand_out_an_empty_scored_row(self):
        code = self._enter_the_window()
        out = engine.get_stock(code)
        self.assertEqual(out["audit_status"], audit_job.RUNNING)
        self.assertIsNone(out["total_score"])
        self.assertEqual(out["financial"], {})
        self.assertEqual(out["attr_scores"], {})
        self.assertEqual(out["snapshots"], [])

    def test_the_window_closes_the_moment_the_score_lands(self):
        """窗口的另一端：分数落库之后这一行必须立刻恢复成已审计。

        手工摆出 `_persist` + `clear_stock_audit` 留下的那副样子（有分数、过程状态
        已清），是为了不在这里真去抓一次财务。少这一条，前面几条只证明了「挡住」，
        证明不了「该放开时会放开」。
        """
        code = self._enter_the_window()
        self.conn.execute("UPDATE research_stocks SET total_score=78.5, system_type='value',"
                          " risk_level='GREEN', rule_version=?, audit_status=NULL,"
                          " audit_started_at=NULL WHERE code=?", (rules.RULE_VERSION, code))
        self.conn.commit()
        row = self._listed(code)
        self.assertEqual(row["audit_status"], audit_job.OK)
        self.assertEqual(row["audit_status_label"], "已审计")
        self.assertEqual(row["total_score"], 78.5)
        self.assertEqual(row["system_type"], "value")


# --------------------------------------------------------------------------- #
# 前端与接口：标签一处、路由一处
# --------------------------------------------------------------------------- #
class TestTheInterfaceAgreesWithTheServer(unittest.TestCase):
    def test_the_fallback_table_matches_the_server_word_for_word(self):
        """前端自己抄一份中文名，就会在加状态时漏掉一个，页面上露出英文常量。

        这份兜底表必须与 ``audit_job.AUDIT_STATUS_LABELS`` 逐字相等——**包括
        ``OK``**：门禁的四档里只有它是「能看分数」，漏了它页面会把通过的股票
        也当成被挡住的。
        """
        block = re.search(r"const AUDIT_STATUS_LABELS_FALLBACK = \{(.*?)\};", JS, re.S)
        self.assertIsNotNone(block, "前端没有兜底标签表")
        fallback = dict(re.findall(r"(\w+):\s*'([^']*)'", block.group(1)))
        self.assertEqual(fallback, audit_job.AUDIT_STATUS_LABELS)
        self.assertEqual(set(fallback), {audit_job.UNAUDITED, audit_job.RUNNING,
                                         audit_job.FAILED, audit_job.OK})

    def test_the_labels_are_served_and_preferred_over_the_fallback(self):
        self.assertIn('"audit_status_labels": audit_job.AUDIT_STATUS_LABELS', SERVER_PY,
                      "/api/meta 没有下发审计状态标签")
        self.assertIn("state.meta && state.meta.audit_status_labels", JS,
                      "前端没有优先读后端下发的标签")
        self.assertEqual(JS.count("AUDIT_STATUS_LABELS_FALLBACK"), 2,
                         "兜底表被用在了别处——它只该有一份定义、一处引用")

    def test_begin_audit_computes_no_route(self):
        """没算分就没有路由可算。

        「一次分析只算一次路由」在 tests/test_router.py 里是用源码计数钉死的；门禁
        路径多算一次，界面显示的模型就和落库的不是同一个——那正是 SCORING 层修过的
        display/scoring 不一致的翻版。2026-09-24 起那一次调用在
        ``engine._run_analysis`` 里，门禁与 analyze 都不许自己算。
        """
        self.assertEqual(_route_accesses(engine._begin_audit), 0)
        self.assertEqual(_route_accesses(engine.analyze), 0)
        self.assertEqual(_route_accesses(engine._run_analysis), 1)

    def test_the_gate_leaves_the_row_alone_when_it_is_ok(self):
        """``_apply_audit_gate`` 在 ``OK`` 时只挂状态字段，一个业务字段都不动。"""
        rec = {"total_score": 78.5, "attr_scores": {"value": 80.0}, "risk": {"level": "GREEN"},
               "valuation": {"price": 10.0, "adjusted_net_cash_to_mcap": 0.4},
               "primary_model": router.MODEL_DIVIDEND, "snapshots": [{"date": "x"}]}
        before = copy.deepcopy(rec)
        engine._apply_audit_gate(rec, audit_job.OK, None, None)
        for key, value in before.items():
            self.assertEqual(rec[key], value, f"{key} 在 OK 时被改动了")
        self.assertEqual(rec["audit_status"], audit_job.OK)
        self.assertEqual(rec["audit_status_label"], "已审计")

    def test_the_suppressed_fields_are_derived_from_the_route_fields(self):
        """路由那一组由 ``route_fields`` 现推，不手抄——它将来加字段时门禁自动跟上。"""
        for name in engine.route_fields({}):
            self.assertIn(name, engine.AUDIT_SUPPRESSED_FIELDS,
                          f"{name} 路由字段加进来了，门禁没跟上：未审计的股票会漏出它")


if __name__ == "__main__":
    unittest.main()
