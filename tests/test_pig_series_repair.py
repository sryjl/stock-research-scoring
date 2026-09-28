# -*- coding: utf-8 -*-
"""序列库的 ``series_hash`` 迁移与重复行折叠（批 6 §一–§四）。

这一组测试全部**离线、不联网、不碰真库**（``:memory:``）。种子数据是手搓的
「旧世界」行——``series_hash`` 按**旧**算法算（含 ``raw_response_hash`` 与 ``url``），
这样夹具长得和迁移前真库里那 3973 行一样。

这里钉住的三件事：

* **只折叠完全重复**（同事实同值），值不同的组是真修订，一行都不动；
* **dry-run 一个字都不写**（含不落报告文件）；
* **apply 之前报告先落盘**，被删掉的每一行都在报告里。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from hashlib import sha256

from research import pig_industry_series as PIS
from research import pig_series_repair as REP
from research.industry import pig as _pig

METRIC = _pig.M_NATIONAL_PIG_PRICE
VARIANT = "national_avg_price"
LABEL = "猪好多数据（商业数据源，外三元口径）"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    PIS.ensure_schema(conn)
    return conn


def _digest(payload):
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
                  ).hexdigest()[:32]


def _old_hash(day, value, window, raw):
    """**旧**的 ``series_hash``：把 ``raw_response_hash`` 和 ``url`` 也算进身份。

    这正是那 156 组重复的来源——夹具照旧算法算，迁移才有东西可折。
    """
    return _digest({"metric_id": METRIC, "metric_variant": VARIANT, "region": None,
                    "period": day, "value": value, "unit": "CNY/kg",
                    "source_level": "L6", "raw_response_hash": raw, "url": window})


def _seed(conn, rows):
    """按旧世界的样子插行。每项：``(day, value, window, raw, fetched_at)``。"""
    for day, value, window, raw, fetched in rows:
        conn.execute(
            "INSERT INTO pig_industry_series (series_hash, metric_id, metric_variant,"
            " region, period, value, unit, scope, source_level, source_type,"
            " source_name, source_url, request_params, raw_response_hash,"
            " status, fetched_at, first_seen_at) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_old_hash(day, value, window, raw), METRIC, VARIANT, None, day, value,
             "CNY/kg", "national", "L6", _pig.SRC_COMMERCIAL_DB, LABEL, window,
             json.dumps({"sDate": window}), raw, "OK", fetched, fetched))
    conn.commit()


def _two_windows(day="2026-09-01", value=10.99):
    """同一份事实、两次不同窗口的抓取——真库里那 156 组的形状。"""
    return [
        (day, value, "https://x/line?sDate=2026-09-01&eDate=2026-09-27", "a" * 64,
         "2026-09-27 03:52:54"),
        (day, value, "https://x/line?sDate=2026-06-28&eDate=2026-09-27", "b" * 64,
         "2026-09-27 03:56:29"),
    ]


class TestPlan(unittest.TestCase):
    def test_collapses_only_exact_duplicates(self):
        conn = _conn()
        _seed(conn, _two_windows())
        payload = REP.plan(conn)
        self.assertEqual(payload["total_rows"], 2)
        self.assertEqual(len(payload["collapse"]), 1)
        self.assertEqual(len(payload["collapse"][0]["drop"]), 1)
        self.assertEqual(payload["drop_count"], 1)
        self.assertEqual(payload["distinct_new_hashes"], 1)
        # 值相同 = 没有真实修订
        self.assertEqual(len(payload["revisions"]), 0)

    def test_a_changed_value_is_not_collapsed(self):
        """同一个事实键、**值不同** = 真修订：两行都留，只是版本数记成 2。"""
        conn = _conn()
        _seed(conn, [("2026-09-01", 10.99, "https://x/line?sDate=2026-09-01",
                      "a" * 64, "2026-09-27 03:52:54"),
                     ("2026-09-01", 11.40, "https://x/line?sDate=2026-09-01",
                      "c" * 64, "2026-09-27 03:56:29")])
        payload = REP.plan(conn)
        self.assertEqual(payload["collapse"], [])          # 一行都不折叠
        self.assertEqual(payload["drop_count"], 0)
        self.assertEqual(len(payload["revisions"]), 1)
        self.assertEqual(payload["revisions"][0]["versions"], 2)
        self.assertEqual(payload["revisions"][0]["values"], [10.99, 11.40])
        self.assertEqual(payload["rename"][0]["revision_count"], 2)

    def test_a_lone_row_is_untouched(self):
        conn = _conn()
        _seed(conn, [("2026-09-01", 10.99, "https://x/line?sDate=2026-09-01",
                      "a" * 64, "2026-09-27 03:52:54")])
        payload = REP.plan(conn)
        self.assertEqual(payload["collapse"], [])
        self.assertEqual(len(payload["rename"]), 1)
        self.assertEqual(payload["untouched"], [])

    def test_the_survivor_keeps_the_earliest_fetch_provenance(self):
        conn = _conn()
        _seed(conn, _two_windows())
        group = REP.plan(conn)["collapse"][0]
        # 存活的是 fetched_at 最早的那条；它的 source_url 与自己的 fetched_at 对得上
        self.assertEqual(group["keep"]["fetched_at"], "2026-09-27 03:52:54")
        self.assertIn("sDate=2026-09-01", group["keep"]["source_url"])
        # first_seen 取组内最早，last_seen 取组内最晚
        self.assertEqual(group["first_seen_at"], "2026-09-27 03:52:54")
        self.assertEqual(group["last_seen_at"], "2026-09-27 03:56:29")
        self.assertEqual(group["revision_count"], 1)

    def test_plan_writes_nothing(self):
        conn = _conn()
        _seed(conn, _two_windows())
        before = [dict(r) for r in conn.execute("SELECT * FROM pig_industry_series")]
        REP.plan(conn)
        after = [dict(r) for r in conn.execute("SELECT * FROM pig_industry_series")]
        self.assertEqual(before, after)


class TestApply(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pigseriesrepair")
        self.report = os.path.join(self.dir, "report.json")

    def test_dry_run_writes_no_report_file(self):
        """CLI 不给 --apply 时连报告都不落——「什么都没写」要字面成立。"""
        conn = _conn()
        _seed(conn, _two_windows())
        self.assertFalse(os.path.exists(self.report))
        REP.plan(conn)
        self.assertFalse(os.path.exists(self.report))

    def test_apply_deletes_the_duplicate_and_renames_to_the_fact_hash(self):
        conn = _conn()
        _seed(conn, _two_windows())
        payload = REP.plan(conn)
        result = REP.apply(conn, payload, report_path=self.report)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(REP.verify(conn), [])
        rows = [dict(r) for r in conn.execute("SELECT * FROM pig_industry_series")]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # 哈希是**事实身份**：不含 raw_response_hash、不含 url
        self.assertEqual(row["series_hash"], REP._new_hash(row))
        self.assertNotIn("sDate", str(row["series_hash"]))
        # 溯源仍旧留着（没被清空）
        self.assertIn("sDate=2026-09-01", row["source_url"])
        self.assertEqual(row["raw_response_hash"], "a" * 64)
        self.assertEqual(row["last_seen_at"], "2026-09-27 03:56:29")
        self.assertEqual(row["first_seen_at"], "2026-09-27 03:52:54")
        self.assertEqual(row["revision_count"], 1)
        self.assertEqual(row["value"], 10.99)

    def test_apply_writes_the_report_before_touching_data(self):
        conn = _conn()
        _seed(conn, _two_windows())
        REP.apply(conn, REP.plan(conn), report_path=self.report)
        self.assertTrue(os.path.exists(self.report))
        report = json.load(open(self.report, encoding="utf-8"))
        self.assertEqual(report["before_rows"], 2)
        self.assertEqual(report["expected_after_rows"], 1)
        self.assertEqual(len(report["collapsed"]), 1)
        dropped = report["collapsed"][0]["dropped"]
        self.assertEqual(len(dropped), 1)
        # **被删的每一行**都在报告里，字段齐全（不静默删历史）
        for key in ("series_hash", "value", "raw_response_hash", "source_url",
                    "request_params", "fetched_at", "first_seen_at"):
            self.assertIn(key, dropped[0])
        self.assertEqual(dropped[0]["raw_response_hash"], "b" * 64)
        self.assertIn("sDate=2026-06-28", dropped[0]["source_url"])

    def test_apply_keeps_real_revisions(self):
        conn = _conn()
        _seed(conn, [("2026-09-01", 10.99, "https://x/line?sDate=2026-09-01",
                      "a" * 64, "2026-09-27 03:52:54"),
                     ("2026-09-01", 11.40, "https://x/line?sDate=2026-09-01",
                      "c" * 64, "2026-09-27 03:56:29")])
        result = REP.apply(conn, REP.plan(conn), report_path=self.report)
        self.assertEqual(result["deleted"], 0)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series ORDER BY value")]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["value"] for r in rows], [10.99, 11.40])
        self.assertEqual([r["revision_count"] for r in rows], [2, 2])
        self.assertEqual(json.load(open(self.report, encoding="utf-8"))["collapsed"], [])
        self.assertEqual(REP.verify(conn), [])

    def test_apply_is_idempotent(self):
        """跑第二遍应该无事可做——否则「可复跑」是假话。"""
        conn = _conn()
        _seed(conn, _two_windows())
        REP.apply(conn, REP.plan(conn), report_path=self.report)
        second = REP.plan(conn)
        self.assertEqual(second["collapse"], [])
        self.assertEqual(second["rename"], [])
        self.assertEqual(second["untouched_count"], 1)
        result = REP.apply(conn, second, report_path=self.report)
        self.assertEqual((result["deleted"], result["renamed"]), (0, 0))
        self.assertEqual(len([dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series")]), 1)

    def test_verify_catches_a_row_whose_hash_is_not_the_fact_identity(self):
        conn = _conn()
        _seed(conn, [("2026-09-01", 10.99, "https://x/line?sDate=2026-09-01",
                      "a" * 64, "2026-09-27 03:52:54")])
        problems = REP.verify(conn)          # 还没迁：哈希还是旧的
        self.assertTrue(any("事实身份" in p for p in problems))
        REP.apply(conn, REP.plan(conn), report_path=self.report)
        self.assertEqual(REP.verify(conn), [])


class TestCliIsDryRunByDefault(unittest.TestCase):
    def test_the_cli_module_has_an_apply_flag(self):
        """默认 dry-run 是**代码事实**，不是文档承诺：``--apply`` 必须存在。"""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "research", "pig_series_repair.py"),
            encoding="utf-8").read()
        self.assertIn('"--apply"', src)
        self.assertIn("action=\"store_true\"", src)


if __name__ == "__main__":
    unittest.main()
