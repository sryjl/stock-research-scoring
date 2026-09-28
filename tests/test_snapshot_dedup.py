# -*- coding: utf-8 -*-
"""tests/test_snapshot_dedup.py — 同一份结果不许反复追加快照。

刷新页面、重复点「重新分析」、服务重启后重跑，都会再走一遍评分并写库。没有
去重的话，同一分钟里会堆出一串**完全一样**的行，把真正的变化淹掉，也让
``research_snapshots`` 无限增长。

判据是两重：逐字段比较**与** ``result_hash`` 都对上才跳过。

    stock_code + rule_version + result_hash  相同  ->  不追加
    价格 / 财报 / 资产语义数据 / 规则 / 评分 / 路由 任一变了  ->  追加

``rule_version`` 仍然在键里，而且仍然有用：评分体系进了 EXPERIMENTAL 之后它不
再逐次递增，但**换制度**（V1.2 -> EXPERIMENTAL）那一次必须留下一行，否则旧口径
的最后一份结果会被当成「重复」丢掉。

这里同时钉住「不许删历史」：去重只影响**要不要新写一行**，任何情况下都不
UPDATE、不 DELETE 已有行。
"""
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.db as research_db  # noqa: E402
from research import engine, rules  # noqa: E402


def _snapshot(price=10.0, route=None):
    snap = {
        "stock_code": "600000", "date": "2026-09-23 10:00:00",
        "current_price": price, "report_period": "2026-06-30",
        "rule_version": rules.RULE_VERSION,
        "type_scores": {"value": 80}, "financial_metrics": {"revenue": [1, 2]},
        "valuation_metrics": {"pb": 1.0, "adjusted_net_cash_to_mcap": 0.4},
        "risk_flags": [], "total_score": 80.0, "confidence": 1.0,
        "category_scores": [{"key": "valuation", "score": 80}],
        "profile_version": "PROFILE_SCORING_V1.2",
    }
    snap["result_hash"] = engine.snapshot_result_hash(snap, route)
    return snap


class TestSnapshotDedup(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _rows(self, code="600000"):
        return research_db.list_snapshots(self.conn, code)

    # ---- 不该追加的 ---- #
    def test_three_identical_analyses_write_one_row(self):
        for i in range(3):
            snap = _snapshot()
            snap["date"] = f"2026-09-23 10:0{i}:00"   # 采集时间每次都不同
            research_db.add_snapshot_if_changed(self.conn, snap)
        self.assertEqual(len(self._rows()), 1, "同一结果被重复写成了多行")

    def test_recomputing_the_same_result_reports_no_change(self):
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, _snapshot()))
        self.assertFalse(research_db.add_snapshot_if_changed(self.conn, _snapshot()))

    def test_date_never_enters_the_hash(self):
        """采集时间进了指纹，去重就会彻底失效——每次都「有变化」。"""
        a, b = _snapshot(), _snapshot()
        a["date"], b["date"] = "2026-09-23 10:00:00", "2026-09-23 23:59:59"
        self.assertEqual(a["result_hash"], b["result_hash"])

    # ---- 该追加的 ---- #
    def test_price_change_appends(self):
        research_db.add_snapshot_if_changed(self.conn, _snapshot(price=10.0))
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, _snapshot(price=10.5)))
        self.assertEqual(len(self._rows()), 2)

    def test_asset_semantic_change_appends_without_touching_the_price(self):
        """资产语义数据变了（如应付票据改口径），价格可以一个数都没动。"""
        research_db.add_snapshot_if_changed(self.conn, _snapshot())
        moved = _snapshot()
        moved["valuation_metrics"] = {"pb": 1.0, "adjusted_net_cash_to_mcap": 0.4027}
        moved["result_hash"] = engine.snapshot_result_hash(moved, None)
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, moved))
        self.assertEqual(len(self._rows()), 2)

    def test_rule_version_change_appends(self):
        """换制度那一次必须留痕：V1.2 -> EXPERIMENTAL 要各留一行。

        评分体系进了 EXPERIMENTAL 之后版本号不再逐次递增，这条测的不再是「升版
        会追加」这个日常路径，而是**切换生效规则**这个真实事件：新规则算出的结果
        与旧规则的最后一行为邻，去重若把它当重复吞掉，旧口径的最后一份结果就成了
        页面上唯一的「当前结果」。
        """
        old = _snapshot()
        old["rule_version"] = "SCORING_V1.2"
        old["result_hash"] = engine.snapshot_result_hash(old, None)
        research_db.add_snapshot_if_changed(self.conn, old)
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, _snapshot()),
                        "换到 EXPERIMENTAL 之后结果被当成重复丢掉了")
        self.assertEqual([r["rule_version"] for r in self._rows()],
                         ["SCORING_V1.2", "SCORING_EXPERIMENTAL"])

    def test_route_change_appends_even_when_the_score_is_identical(self):
        """换了一套模型而分数一个数没动，那是结果变了，不能当重复丢掉。

        这一条是逐字段比较**覆盖不到**的：路由不在那边任何一列里。
        """
        route_a = {"router_version": "MODEL_ROUTER_V1.0", "primary_model": "GENERAL_VALUE_V2",
                   "secondary_model": None, "primary_fit": 71.0, "secondary_fit": None,
                   "route_status": "CLEAR", "profile_scores": {"value": 80},
                   "fit_scores": {"GENERAL_VALUE_V2": 71.0}}
        route_b = dict(route_a, primary_model="VALUE_CIGAR_V2",
                       fit_scores={"VALUE_CIGAR_V2": 68.0})
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, _snapshot(route=route_a)))
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, _snapshot(route=route_b)))
        self.assertEqual(len(self._rows()), 2)

    def test_route_confidence_alone_does_not_append(self):
        """confidence / coverage 是连续量，价格一动就微调，比它们会淹掉真变化。"""
        route_a = {"primary_model": "GENERAL_VALUE_V2", "route_status": "CLEAR",
                   "confidence": 0.71, "coverage": 0.83}
        route_b = dict(route_a, confidence=0.68, coverage=0.81)
        research_db.add_snapshot_if_changed(self.conn, _snapshot(route=route_a))
        self.assertFalse(research_db.add_snapshot_if_changed(self.conn, _snapshot(route=route_b)))

    # ---- 历史行与既有行为 ---- #
    def test_legacy_row_without_a_hash_still_dedups_by_fields(self):
        """指纹列是后加的，历史行是 NULL。NULL 不代表「结果没变」，
        但也不该让缺指纹的行被无限重复追加——退回逐字段比较。"""
        old = _snapshot()
        old.pop("result_hash")
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, old))
        again = _snapshot()
        again.pop("result_hash")
        again["date"] = "2026-09-23 11:00:00"
        self.assertFalse(research_db.add_snapshot_if_changed(self.conn, again))
        self.assertEqual(len(self._rows()), 1)

    def test_dedup_never_reaches_back_into_existing_rows(self):
        """去重只决定「写不写新行」，绝不动历史。"""
        first = _snapshot()
        research_db.add_snapshot_if_changed(self.conn, first)
        raw_before = self.conn.execute(
            "SELECT * FROM research_snapshots ORDER BY id").fetchall()
        for _ in range(3):
            research_db.add_snapshot_if_changed(self.conn, _snapshot(price=11.0))
        raw_after = self.conn.execute(
            "SELECT * FROM research_snapshots ORDER BY id LIMIT %d" % len(raw_before)).fetchall()
        self.assertEqual([tuple(r) for r in raw_before], [tuple(r) for r in raw_after],
                         "去重动了已有行——那是回写历史，不是追加")

    def test_result_hash_is_persisted_and_readable(self):
        self.assertTrue(research_db.add_snapshot_if_changed(self.conn, _snapshot()))
        row = self._rows()[0]
        self.assertIsNotNone(row["result_hash"])
        self.assertEqual(len(row["result_hash"]), 32)


class TestSnapshotSchemaUpgrade(unittest.TestCase):
    """老库（没有 result_hash 列）必须能在写入路径上自己长出来。

    ``CREATE TABLE IF NOT EXISTS`` 对已存在的表什么都不做，而 dedup 的查询
    恰好在写入路径上——补列漏了就是 ``no such column`` 直接炸。
    """

    def test_old_database_without_result_hash_can_still_be_written(self):
        path = tempfile.mktemp(suffix=".db")
        try:
            # 造一个 V1.2 之前的表结构：没有 result_hash，也没有 profile_version。
            import sqlite3
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("""
                CREATE TABLE research_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL, date TEXT NOT NULL,
                    current_price REAL, report_period TEXT, rule_version TEXT,
                    type_scores TEXT, financial_metrics TEXT, valuation_metrics TEXT,
                    risk_flags TEXT, category_scores TEXT, total_score REAL,
                    confidence REAL)""")
            conn.execute(
                "INSERT INTO research_snapshots (stock_code, date, rule_version,"
                " total_score) VALUES ('600000', '2026-01-01', 'SCORING_V1.1', 70.0)")
            conn.commit()
            conn.close()

            conn = research_db.connect(path)
            self.assertTrue(
                research_db.add_snapshot_if_changed(conn, _snapshot()),
                "老库上写不进去：补列没发生在写入路径上")
            rows = research_db.list_snapshots(conn, "600000")
            self.assertEqual(len(rows), 2)
            self.assertIsNone(rows[0]["result_hash"], "历史行不该被回填指纹")
            self.assertIsNotNone(rows[1]["result_hash"])
            conn.close()
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_real_database_keeps_every_old_row(self):
        """生产库上跑一次 init_db 只加列，不动任何一行。

        指纹要**逐行**比 before/after，不能断言「全是 NULL」：V1.2 正式落库之后
        生产库里本来就有带指纹的新行，那个断言会把「库已经正常用了」判成失败。
        真正要钉住的是「补列不回填」——旧行原来是 NULL 的现在还得是 NULL，
        原来有指纹的一个字符都不能变。
        """
        work = tempfile.mktemp(suffix=".db")
        shutil.copy(ROOT / "data" / "research.db", work)
        try:
            before = research_db.connect(work)
            rows_before = [tuple(r) for r in before.execute(
                "SELECT id, stock_code, date, rule_version, total_score, result_hash"
                " FROM research_snapshots ORDER BY id")]
            null_before = [r[0] for r in rows_before if r[5] is None]
            self.assertTrue(null_before,
                            "生产库里一条无指纹的历史行都没有了？那这个测试已经失去意义")
            before.close()

            research_db.init_db(work).close()

            after = research_db.connect(work)
            rows_after = [tuple(r) for r in after.execute(
                "SELECT id, stock_code, date, rule_version, total_score, result_hash"
                " FROM research_snapshots ORDER BY id")]
            after.close()
            self.assertEqual(rows_before, rows_after, "补列改动了历史行（含指纹列）")
            self.assertTrue(all(r[5] is None for r in rows_after if r[0] in null_before),
                            "历史行被回填了指纹——那等于伪造「这条结果是这次算的」")
        finally:
            if os.path.exists(work):
                os.remove(work)


if __name__ == "__main__":
    unittest.main()
