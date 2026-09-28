# -*- coding: utf-8 -*-
"""行业序列层：切窗、时区、解析、择优、派生（批 5.1 §三十二 / §三十三 / §四十）。

这一组测试里有三条是**真跑时才会炸的错**的回归：

* 台账行手搓 → ``request_params`` 是 dict 绑不进 TEXT，且漏了两个 NOT NULL 列。
  只有 ``ingest()`` 真跑一次才炸，而炸出来的是「参数绑不进去」；
* ``latest()`` 用 ``source_level``（``"L5"``）去查 ``SOURCE_RANK``（按
  ``source_type`` 建的键表）→ 永远查不到 → 「官方压商业」静默退化成按哈希排；
* 坏窗口那条路径同样手搓过台账行。

全部不联网：假的 provider 把「窗口 → 点」写死。
"""
import io
import json
import sqlite3
import sys
import unittest
from hashlib import sha256
from urllib.parse import urlencode

from research import pig_industry_series as PIS
from research import providers
from research.industry import pig as _pig


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def envelope(rows, *, code=10000):
    """猪好多 ``/zhujia/api/line`` 的真实回包形状：``{"code","message","data"}``。"""
    return {"url": providers.YZ360_LINE_URL, "params": {"type": "pigprice"},
            "raw_sha256": "f" * 64, "raw_bytes": 1234,
            "fetched_at": "2026-09-27 10:00:00", "body": {
                "code": code, "message": "success" if code == 10000 else "error",
                "data": [{"price": p, "date": d} for p, d in rows]}}


class FakeProvider(PIS.Yangzhu360Provider):
    """把「窗口 → 点」写死的假 provider：**行接口被桩掉的猪好多**。

    继承真实的 provider（而不是基类），这样 ``fetch_cross_section`` /
    ``REGION_VARIANT`` / ``SECTION_FIELD`` 跑的都是**真代码**——否则「横截面」
    那几条测试测的是一个只在测试里存在的实现。
    """

    name = "fake"
    source_level = "L6"
    source_type = _pig.SRC_COMMERCIAL_DB
    label = "假数据源（测试用）"
    series_types = {_pig.M_NATIONAL_PIG_PRICE: ("national_avg_price", "pigprice")}

    def __init__(self, rows=(), *, level="L6", source_type=None, fail=None,
                 name="fake", label=None, fetched_at="2026-09-27 10:00:00",
                 section=False):
        self.rows = list(rows)
        self.source_level = level
        self.source_type = source_type or self.source_type
        self.fail = fail
        self.name = name
        self.label = label or self.label      # source_name 取的就是它
        self.fetched_at = fetched_at
        #: 要不要跟着抓横截面。**默认关**：假 provider 覆盖了 ``_fetch``，
        #: 不给开关的话每个用到它的测试都会去真连地图接口。
        self.section = section

    def endpoint(self):
        return "https://example.test/line"

    def _env(self, start=None, end=None):
        """**窗口进 ``url`` 和 ``raw_sha256``**，照 ``providers._fetch_json`` 的真实行为
        （它把 ``sDate``/``eDate`` 拼进 query，而对整窗响应体取 sha256）。

        批 6 之前这里把 ``raw_sha256`` 写死成 ``"f" * 64``，于是「两次不同窗口抓同一个数」
        在夹具里长得和「两次同窗口抓同一个数」一模一样——``test_refetch_only_touches_fetched_at``
        断言的契约是对的，夹具却把 bug 盖住了，26 条测试全绿而库里那 156 组重复照样在。
        **夹具必须和真实响应一样会变**，否则它测的是自己的常数。
        """
        params = {"type": "pigprice"}
        url = self.endpoint()
        if start is not None and end is not None:
            params.update({"sDate": start, "eDate": end})
            url = "%s?%s" % (url, urlencode(params))
        return {"url": url, "params": params,
                "fetched_at": self.fetched_at,
                "raw_sha256": sha256(
                    ("%s|%s" % (url, self.fetched_at)).encode("utf-8")).hexdigest(),
                "raw_bytes": 1234}

    def _fetch(self, start, end, regions, sleep):
        env = self._env(start, end)
        metric, variant = _pig.M_NATIONAL_PIG_PRICE, "national_avg_price"
        if self.fail:
            # 失败**绝不产生数据行**（用户裁定），只留台账。
            return [], [self._entry(env, metric, variant, rows=0,
                                    status=PIS.FETCH_ERROR, reason=self.fail)]
        points = [self._point(metric, variant, value, "CNY/kg", day, env,
                              scope="national") for day, value in self.rows]
        dates = sorted(day for day, _v in self.rows)
        entries = [self._entry(
            env, metric, variant, rows=len(points),
            first_date=dates[0] if dates else None,
            last_date=dates[-1] if dates else None,
            span=(start, end))]
        if not self.section:
            return points, entries
        section_points, section_entries = self.fetch_cross_section()
        return points + section_points, entries + section_entries


class TestEast8Clock(unittest.TestCase):
    def test_unix_seconds_are_read_as_east_eight_midnight(self):
        """实测：``1789401600`` = UTC 2026-09-14 16:00 = 东八区 2026-09-15 00:00。

        用本地时区去读会按运行机器的时区漂一天——同一天的价在两个时区里是两天。
        """
        self.assertEqual(PIS.east8_date(1789401600), "2026-09-15")
        self.assertEqual(PIS.east8_date(1789401600 - 86400), "2026-09-14")

    def test_bad_input_yields_none_not_a_guess(self):
        for bad in (None, [], {}, "昨天", ""):
            self.assertIsNone(PIS.east8_date(bad), bad)

    def test_numeric_strings_are_accepted(self):
        """上游回的是整数，但 JSON 转一圈可能变成字符串——两者都要读得出来。"""
        self.assertEqual(PIS.east8_date("1789401600"), "2026-09-15")


class TestSliceWindows(unittest.TestCase):
    def test_windows_are_closed_and_adjacent(self):
        windows = PIS.slice_windows("2025-01-01", "2025-06-30")
        self.assertEqual(windows, [("2025-01-01", "2025-06-30")])
        windows = PIS.slice_windows("2025-01-01", "2025-12-31")
        self.assertEqual(windows[0], ("2025-01-01", "2025-06-30"))
        self.assertEqual(windows[1][0], "2025-07-01")       # 相邻，不重叠
        for start, end in windows:
            span = (PIS.datetime.strptime(end, PIS.DATE_FMT)
                    - PIS.datetime.strptime(start, PIS.DATE_FMT)).days
            self.assertLessEqual(span, providers.YZ360_MAX_WINDOW_DAYS)

    def test_boundary_is_a_measured_one(self):
        """实测 ``2019-01-01 ~ 2019-06-30`` 回 181 条（首尾都含），所以 180 是安全上界。"""
        self.assertEqual(len(PIS.slice_windows("2019-01-01", "2019-06-30")), 1)
        self.assertEqual(len(PIS.slice_windows("2019-01-01", "2019-07-01")), 2)

    def test_backwards_or_unparsable_range_yields_no_window(self):
        self.assertEqual(PIS.slice_windows("2025-06-30", "2025-01-01"), [])
        for bad in (("", ""), ("昨天", "今天"), (None, "2025-01-01")):
            self.assertEqual(PIS.slice_windows(*bad), [], bad)


class TestReadLine(unittest.TestCase):
    """回显 / 空 / 冲突是三件事，处置完全不同。"""

    def test_type_echo_is_unsupported_not_empty(self):
        env = envelope([], code=10000)
        env["body"]["data"] = ["piglet15"]         # 站点另一套词表里的名字
        self.assertIsNone(PIS.Yangzhu360Provider._read_line(env, "piglet"))
        self.assertEqual(PIS.Yangzhu360Provider._failure_status(env),
                         PIS.FETCH_UNSUPPORTED)
        self.assertIn("回显", PIS.Yangzhu360Provider._failure_reason(env, "piglet"))

    def test_non_success_code_is_none(self):
        self.assertIsNone(PIS.Yangzhu360Provider._read_line(
            envelope([], code=1), "pigprice"))

    def test_data_must_be_a_list(self):
        env = envelope([])
        env["body"]["data"] = {"price": "10.00"}
        self.assertIsNone(PIS.Yangzhu360Provider._read_line(env, "pigprice"))

    def test_same_day_with_equal_values_is_deduped_not_flagged(self):
        """实测 2023-02-14 确实回了两次，值相同——那是同一份事实。

        去重发生在**落库**（指纹相同 → 一行），不在解析：解析层照收两行，只是
        不把它记成冲突——把同值重复当冲突会白扔掉一整天的价。
        """
        env = envelope([("10.00", 1789401600), ("10.00", 1789401600)])
        rows, dates, conflicts = PIS.Yangzhu360Provider._read_line(env, "pigprice")
        self.assertEqual(dates, ["2026-09-15"])
        self.assertEqual(conflicts, {})
        conn = _conn()
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(FakeProvider(list(zip(dates, [10.0]))),),
                   today="2026-09-27")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM pig_industry_series").fetchone()[0], 1)

    def test_same_day_with_two_values_drops_the_day_and_records_the_conflict(self):
        """用户裁定：金额冲突不自动挑一个。**整条不进序列**，但要说清是哪天。"""
        env = envelope([("10.00", 1789401600), ("10.50", 1789401600),
                        ("11.00", 1789488000)])
        rows, dates, conflicts = PIS.Yangzhu360Provider._read_line(env, "pigprice")
        self.assertEqual([r[0] for r in rows], ["2026-09-16"])
        self.assertEqual(conflicts, {"2026-09-15": [10.0, 10.5]})

    def test_unparsable_points_are_skipped(self):
        env = envelope([])
        env["body"]["data"] = [{"price": "abc", "date": 1789401600},
                               {"price": "10.00", "date": None},
                               {"price": "10.00", "date": 1789401600}]
        rows, _dates, conflicts = PIS.Yangzhu360Provider._read_line(env, "pigprice")
        self.assertEqual([r[1] for r in rows], [10.0])
        self.assertEqual(conflicts, {})


class TestLedgerBinding(unittest.TestCase):
    """台账行只有 ``_entry()`` 一份定义——手搓过两次，两次都只有真跑才炸。"""

    def test_unwired_provider_writes_a_bindable_row(self):
        conn = _conn()
        report = PIS.ingest(conn, start="2026-01-01", end="2026-01-31",
                            providers_chain=(PIS.OfficialMoAProvider(),),
                            today="2026-09-27")
        self.assertEqual(report["points"], 0)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series_meta")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], PIS.FETCH_NOT_WIRED)
        self.assertEqual(rows[0]["metric_id"], PIS.UNWIRED_METRIC)
        self.assertIsInstance(rows[0]["request_params"], str)
        self.assertEqual(json.loads(rows[0]["request_params"]), {})
        self.assertEqual(rows[0]["provider"], "official_moa")
        # 「没查」与「查了没有」在库里必须分得开。
        self.assertNotEqual(rows[0]["status"], PIS.FETCH_EMPTY)
        self.assertIn("不得逆向", rows[0]["reason"])

    def test_unwired_row_never_reaches_the_data_table(self):
        conn = _conn()
        PIS.ingest(conn, start="2026-01-01", end="2026-01-31",
                   providers_chain=(PIS.OfficialMoAProvider(),),
                   today="2026-09-27")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM pig_industry_series").fetchone()[0],
            0)

    def test_bad_window_writes_a_bindable_row_too(self):
        conn = _conn()
        PIS.ingest(conn, start="2026-06-30", end="2026-01-01",
                   providers_chain=(PIS.Yangzhu360Provider(),),
                   today="2026-09-27")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series_meta")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], PIS.FETCH_ERROR)
        self.assertIsInstance(rows[0]["request_params"], str)
        self.assertIn("晚于终点", rows[0]["reason"])


class TestIngestAndLoad(unittest.TestCase):
    def test_points_land_and_a_second_run_adds_nothing(self):
        conn = _conn()
        fake = FakeProvider([("2026-09-14", 10.83), ("2026-09-15", 10.90)])
        first = PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                           providers_chain=(fake,), today="2026-09-27")
        self.assertEqual(first["points"], 2)
        self.assertEqual(first["new"], 2)
        second = PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                            providers_chain=(fake,), today="2026-09-27")
        self.assertEqual(second["points"], 2)
        self.assertEqual(second["new"], 0)          # append-only，不重复落
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM pig_industry_series").fetchone()[0], 2)

    def test_refetch_only_touches_last_seen_at(self):
        conn = _conn()
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(FakeProvider(
                       [("2026-09-15", 10.90)],
                       fetched_at="2026-09-27 10:00:00"),),
                   today="2026-09-27")
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(FakeProvider(
                       [("2026-09-15", 10.90)],
                       fetched_at="2026-09-27 18:00:00"),),
                   today="2026-09-27")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series")]
        self.assertEqual(len(rows), 1)
        # 同一个数重取一次**不是**另一条序列。批 6 起「最后见到」记在 last_seen_at 上，
        # fetched_at / first_seen_at 钉死在第一次写入的时刻（它们答的是「首次见到」）。
        self.assertEqual(rows[0]["last_seen_at"], "2026-09-27 18:00:00")
        self.assertEqual(rows[0]["fetched_at"], "2026-09-27 10:00:00")
        self.assertEqual(rows[0]["first_seen_at"], "2026-09-27 10:00:00")
        self.assertEqual(rows[0]["revision_count"], 1)   # 值没变过 = 只有 1 个版本

    def test_a_different_window_does_not_split_the_fact(self):
        """**批 6 的核心回归**：同一份事实、两次**不同窗口**的抓取 → 仍然只有 1 行。

        这正是库里 2026-09-01…09-26 那 156 组重复的成因：``series_hash`` 以前把
        ``raw_response_hash``（整窗响应体的 sha256）和 ``url``（带 sDate/eDate）也算进
        身份，于是「同样的日期、同样的值」只要来自不同窗口就是两个哈希、写两行。
        抓取身份**不是**事实身份。
        """
        conn = _conn()
        first = PIS.ingest(conn, start="2026-09-01", end="2026-09-27",
                           providers_chain=(FakeProvider(
                               [("2026-09-15", 10.90)],
                               fetched_at="2026-09-27 03:52:00"),),
                           today="2026-09-27")
        second = PIS.ingest(conn, start="2026-06-28", end="2026-09-26",
                            providers_chain=(FakeProvider(
                                [("2026-09-15", 10.90)],
                                fetched_at="2026-09-27 03:56:00"),),
                            today="2026-09-27")
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["points"], 1)
        self.assertEqual(second["new"], 0)          # 窗口变了，事实没变
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 10.90)
        # 溯源仍留着**首次**那次窗口，能一路查回去
        self.assertIn("sDate=2026-09-01", rows[0]["source_url"] or "")

    def test_a_changed_value_is_a_revision_not_a_second_fact(self):
        """值被上游改了 = 真修订：写新行，旧行一字不动，两行都记着版本数。"""
        conn = _conn()
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(FakeProvider(
                       [("2026-09-15", 10.90)],
                       fetched_at="2026-09-27 10:00:00"),),
                   today="2026-09-27")
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(FakeProvider(
                       [("2026-09-15", 11.40)],
                       fetched_at="2026-09-27 18:00:00"),),
                   today="2026-09-27")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series ORDER BY value")]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["value"] for r in rows], [10.90, 11.40])
        # 旧值仍在，读侧据此报 conflict，不是被静默替换
        self.assertEqual([r["revision_count"] for r in rows], [2, 2])
        self.assertEqual(rows[0]["first_seen_at"], "2026-09-27 10:00:00")
        self.assertEqual(rows[1]["first_seen_at"], "2026-09-27 18:00:00")

    def test_failure_produces_no_data_row(self):
        conn = _conn()
        report = PIS.ingest(conn, start="2026-09-01", end="2026-09-30",
                            providers_chain=(FakeProvider([], fail="连接被重置"),),
                            today="2026-09-27")
        self.assertEqual(report["points"], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM pig_industry_series").fetchone()[0], 0)
        self.assertEqual(report["by_status"].get(PIS.FETCH_ERROR), 1)
        self.assertEqual(report["failures"][0]["reason"], "连接被重置")

    def test_future_end_is_clamped_to_today(self):
        """实测 eDate 落在未来会回空——白跑一次请求。"""
        conn = _conn()
        report = PIS.ingest(conn, start="2026-09-01", end="2030-01-01",
                            providers_chain=(FakeProvider([("2026-09-15", 10.9)]),),
                            today="2026-09-27")
        self.assertEqual(report["end"], "2026-09-27")

    def test_unit_comes_from_the_source_not_from_a_default(self):
        conn = _conn()
        fake = FakeProvider([("2026-09-15", 10.90)])
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(fake,), today="2026-09-27")
        rows = PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE)
        self.assertEqual({r["unit"] for r in rows}, {"CNY/kg"})
        self.assertEqual({r["source_level"] for r in rows}, {"L6"})
        self.assertEqual({r["source_type"] for r in rows},
                         {_pig.SRC_COMMERCIAL_DB})   # 商业源，**不标成官方**

    def test_load_filters_by_window_and_status(self):
        conn = _conn()
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(FakeProvider([("2026-09-14", 10.83),
                                                  ("2026-09-15", 10.90)]),),
                   today="2026-09-27")
        self.assertEqual(len(PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE,
                                      start="2026-09-15")), 1)
        self.assertEqual(len(PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE,
                                      end="2026-09-14")), 1)
        self.assertEqual(PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE,
                                  statuses=(PIS.STATUS_MISSING,)), [])

    def test_region_is_part_of_the_identity(self):
        conn = _conn()
        fake = FakeProvider([("2026-09-15", 11.40)])
        fake._fetch_original = fake._fetch

        def fetch(start, end, regions, sleep):
            env = fake._env()
            return [fake._point(_pig.M_NATIONAL_PIG_PRICE, "national_avg_price",
                                11.40, "CNY/kg", "2026-09-15", env,
                                region="440000", scope="region:440000")], []

        fake._fetch = fetch
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(fake,), today="2026-09-27")
        self.assertEqual(PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE,
                                  region="440000")[0]["region"], "440000")
        self.assertEqual(PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE,
                                  region="110000"), [])
        # 落库时地区号码进 ``region``，不塞进指标名——同一个 metric_id 两个地区
        # 是两条序列，不是一个指标的两个 variant。
        self.assertEqual({r["metric_id"] for r in
                          PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE)},
                         {_pig.M_NATIONAL_PIG_PRICE})


class TestLatest(unittest.TestCase):
    def _write(self, conn, provider, rows):
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(FakeProvider(rows,
                                                 **provider),), today="2026-09-27")

    def test_official_source_wins_on_the_same_day(self):
        """择优按 ``source_type``：L5 官方必须压 L6 商业。"""
        conn = _conn()
        self._write(conn, {"level": "L6", "name": "commercial",
                           "label": "商业源",
                           "source_type": _pig.SRC_COMMERCIAL_DB,
                           "fetched_at": "2026-09-27 10:00:00"},
                    [("2026-09-15", 11.50)])
        self._write(conn, {"level": "L5", "name": "official",
                           "label": "官方源",
                           "source_type": _pig.SRC_OFFICIAL_INDUSTRY,
                           "fetched_at": "2026-09-27 11:00:00"},
                    [("2026-09-15", 11.04)])
        best = PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE)
        self.assertEqual(best["value"], 11.04)
        self.assertEqual(best["source_level"], "L5")
        self.assertEqual(best["sources"], ["商业源", "官方源"])

    def test_commercial_is_used_when_it_is_the_only_one(self):
        conn = _conn()
        self._write(conn, {"level": "L6", "name": "commercial"},
                    [("2026-09-15", 11.50)])
        self.assertEqual(PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE)["value"], 11.50)

    def test_a_regional_point_is_never_returned_as_the_national_one(self):
        """``region`` 必须精确相等：广东 11.40 被当成「全国 11.40」看不出来。"""
        conn = _conn()
        fake = FakeProvider([])

        def fetch(start, end, regions, sleep):
            env = fake._env()
            return [fake._point(_pig.M_NATIONAL_PIG_PRICE, "national_avg_price",
                                11.40, "CNY/kg", "2026-09-15", env,
                                region="440000", scope="region:440000")], []

        fake._fetch = fetch
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(fake,), today="2026-09-27")
        self.assertIsNone(PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE))
        self.assertEqual(PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE,
                                    region="440000")["value"], 11.40)

    def test_empty_store_returns_none_not_a_default(self):
        """用户裁定：API 失败保持 missing，不 fallback 到手填数据。"""
        conn = _conn()
        self.assertIsNone(PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE))
        self.assertIsNone(PIS.latest(conn, "不存在的指标"))

    def test_as_of_does_not_look_into_the_future(self):
        conn = _conn()
        self._write(conn, {}, [("2026-09-15", 11.50)])
        self.assertIsNone(PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE,
                                     as_of="2026-09-14"))
        self.assertEqual(PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE,
                                    as_of="2026-09-15")["value"], 11.50)


def map_envelope(features):
    return {"url": providers.YZ360_MAP_URL, "params": {},
            "raw_sha256": "e" * 64, "raw_bytes": 151077,
            "fetched_at": "2026-09-27 10:00:00",
            "body": {"type": "FeatureCollection", "features": features}}


def feature(code, name, price):
    props = {"areacode": str(code), "adcode": code, "name": name}
    if price is not None:
        props["pigprice"] = price
    return {"type": "Feature", "properties": props}


class TestCrossSection(unittest.TestCase):
    """地图接口：一次请求回全国的**横截面**（§地区价格必须保存 region）。"""

    def _patch_map(self, features):
        original = providers.get_yangzhu360_region_map

        def fake(timeout=20):
            return map_envelope(features)

        providers.get_yangzhu360_region_map = fake
        self.addCleanup(setattr, providers, "get_yangzhu360_region_map", original)

    def test_provinces_without_a_price_are_reported_not_silently_dropped(self):
        """实测台湾 / 香港 / 澳门 / 南海诸岛本来就没有 pigprice。"""
        rows, skipped = PIS.Yangzhu360Provider._read_map(map_envelope([
            feature(110000, "北京", "10.35"), feature(710000, "台湾", None)]))
        self.assertEqual(rows, [("110000", 10.35)])
        self.assertEqual(skipped, ["台湾"])

    def test_bad_structure_is_none_not_an_empty_cross_section(self):
        for body in ({}, {"features": "no"}, {"features": None}):
            env = {"body": body}
            self.assertEqual(PIS.Yangzhu360Provider._read_map(env), (None, None))

    def test_prices_are_read_as_numbers_not_strings(self):
        rows, _ = PIS.Yangzhu360Provider._read_map(map_envelope([
            feature(110000, "北京", "10.35"), feature(440000, "广东", "11.04")]))
        self.assertEqual(rows, [("110000", 10.35), ("440000", 11.04)])

    def test_ingest_stores_every_province_under_its_own_region(self):
        conn = _conn()
        self._patch_map([feature(110000, "北京", "10.35"),
                         feature(440000, "广东", "11.04"),
                         feature(710000, "台湾", None)])
        PIS.ingest(conn, start="2026-09-26", end="2026-09-26",
                   providers_chain=(FakeProvider([("2026-09-26", 10.39)], section=True),),
                   today="2026-09-27")
        section = PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE,
                           variant=PIS.Yangzhu360Provider.REGION_VARIANT)
        self.assertEqual({r["region"] for r in section}, {"110000", "440000"})
        self.assertEqual({r["scope"] for r in section},
                         {"region:110000", "region:440000"})
        self.assertEqual({r["source_level"] for r in section}, {"L6"})
        self.assertEqual({r["unit"] for r in section}, {"CNY/kg"})
        # 全国那条**不带头**，横截面那两条带头 —— 两者互不顶替。
        national = PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE,
                            variant="national_avg_price")
        self.assertEqual([r["region"] for r in national], [None])

    def test_snapshot_date_is_the_east_eight_fetch_day(self):
        """响应里**没有日期字段**，所以只能记抓取日——不许编一个。"""
        conn = _conn()
        self._patch_map([feature(110000, "北京", "10.35")])
        PIS.ingest(conn, start="2026-09-26", end="2026-09-26",
                   providers_chain=(FakeProvider([], section=True),), today="2026-09-27")
        rows = PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE,
                        variant=PIS.Yangzhu360Provider.REGION_VARIANT)
        self.assertEqual(PIS.datetime.strptime(rows[0]["period"],
                                               PIS.DATE_FMT).date(),
                         PIS.datetime.now(PIS.TZ_CN).date())

    def test_the_skipped_provinces_are_in_the_ledger_reason(self):
        conn = _conn()
        self._patch_map([feature(110000, "北京", "10.35"),
                         feature(710000, "台湾", None)])
        PIS.ingest(conn, start="2026-09-26", end="2026-09-26",
                   providers_chain=(FakeProvider([], section=True),), today="2026-09-27")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series_meta WHERE rows > 0"
            " AND metric_variant = ?", (PIS.Yangzhu360Provider.REGION_VARIANT,))]
        reasons = " ".join(r["reason"] or "" for r in rows)
        self.assertIn("台湾", reasons)
        self.assertIn("快照", reasons)

    def test_a_failed_cross_section_leaves_no_data_row(self):
        conn = _conn()
        original = providers.get_yangzhu360_region_map
        providers.get_yangzhu360_region_map = lambda timeout=20: {
            "url": providers.YZ360_MAP_URL, "params": {}, "error": "超时",
            "body": None, "fetched_at": "2026-09-27 10:00:00"}
        self.addCleanup(setattr, providers, "get_yangzhu360_region_map", original)
        report = PIS.ingest(conn, start="2026-09-26", end="2026-09-26",
                            providers_chain=(FakeProvider([], section=True),),
                            today="2026-09-27")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM pig_industry_series").fetchone()[0], 0)
        self.assertEqual(report["by_status"].get(PIS.FETCH_ERROR), 1)

    def test_regional_line_series_uses_the_provincial_variant(self):
        """把广东的数挂在全国口径下面是一句假话——读的人只看 ``variant``。

        这条**桩掉行接口**跑真的 ``Yangzhu360Provider._fetch``：变体是按「这次要
        不要地区」选的，只断言一个常量等于没测。
        """
        conn = _conn()
        self._patch_map([])
        original = providers.get_yangzhu360_price_line
        providers.get_yangzhu360_price_line = (
            lambda series_type, s, e, area_id="-1", timeout=15: {
                "url": providers.YZ360_LINE_URL,
                "params": {"areaId": area_id, "type": series_type},
                "raw_sha256": "d" * 64, "raw_bytes": 90,
                "fetched_at": "2026-09-27 10:00:00",
                "body": {"code": 10000, "message": "success",
                         "data": [{"price": "11.40", "date": 1790352000}]}})
        self.addCleanup(setattr, providers, "get_yangzhu360_price_line", original)
        PIS.ingest(conn, start="2026-09-26", end="2026-09-26",
                   providers_chain=(PIS.Yangzhu360Provider(),),
                   regions=("440000",), sleep=0, today="2026-09-27")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pig_industry_series")]
        # 批 9 起 ``series_types`` 只剩生猪价格一项 → 两个地区（全国 + 广东）
        # 各一个点。数目与指标名都从 ``series_types`` 算出来，不抄字面量：
        # 抄一份的话，将来再加一个指标时这条测试只会「数字对不上」，
        # 而看不出它本来要钉的是**口径不许挂错**。
        want_metrics = set(PIS.Yangzhu360Provider.series_types)
        self.assertEqual(len(rows), 2 * len(want_metrics))
        self.assertEqual({r["period"] for r in rows}, {"2026-09-26"})
        self.assertEqual({r["metric_id"] for r in rows}, want_metrics)
        by_region = {}
        for row in rows:
            by_region.setdefault(row["region"], set()).add(row["metric_variant"])
        self.assertEqual(by_region[None],
                         {PIS.Yangzhu360Provider.series_types[m][0]
                          for m in want_metrics})
        self.assertEqual(by_region["440000"],
                         {PIS.Yangzhu360Provider.REGION_VARIANT})

    def test_same_day_two_values_are_flagged_not_silently_picked(self):
        conn = _conn()
        for value in (11.04, 11.50):
            PIS.ingest(conn, start="2026-09-26", end="2026-09-26",
                       providers_chain=(FakeProvider([("2026-09-26", value)],
                                                     fetched_at="2026-09-27 %02d:00:00"
                                                     % int(value % 10)),),
                       today="2026-09-27")
        national = PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE)
        self.assertTrue(national["conflict"])
        self.assertEqual(national["values"], [11.04, 11.50])
        self.assertIn(national["value"], national["values"])

    def test_agreeing_rows_are_not_a_conflict(self):
        conn = _conn()
        for stamp in ("2026-09-27 10:00:00", "2026-09-27 11:00:00"):
            PIS.ingest(conn, start="2026-09-26", end="2026-09-26",
                       providers_chain=(FakeProvider([("2026-09-26", 11.04)],
                                                     fetched_at=stamp),),
                       today="2026-09-27")
        national = PIS.latest(conn, _pig.M_NATIONAL_PIG_PRICE)
        self.assertFalse(national["conflict"])
        self.assertEqual(national["values"], [11.04])


class TestYoyMom(unittest.TestCase):
    def _ingest(self, rows):
        conn = _conn()
        PIS.ingest(conn, start="2025-01-01", end="2026-09-30",
                   providers_chain=(FakeProvider(rows),), today="2026-09-27")
        return conn

    def test_bands_are_bands_not_tolerances(self):
        conn = self._ingest([("2025-09-01", 12.0), ("2026-08-01", 10.0),
                             ("2026-09-01", 11.0)])
        out = PIS.yoy_mom(conn, _pig.M_NATIONAL_PIG_PRICE)
        self.assertEqual(out["as_of"], "2026-09-01")
        self.assertEqual(out["mom"], 10.0)                    # 31 天 → 一个月前
        self.assertEqual(out["mom_base"]["period"], "2026-08-01")
        self.assertAlmostEqual(out["yoy"], -8.3333, places=3)  # 365 天
        self.assertEqual(out["yoy_base"]["gap_days"], 365)

    def test_too_close_does_not_count_as_mom(self):
        """14 天前不是「上个月」——拿它算环比会得到 4.5% 这种看着很正常的错数。"""
        conn = self._ingest([("2026-08-18", 10.0), ("2026-09-01", 11.0)])
        out = PIS.yoy_mom(conn, _pig.M_NATIONAL_PIG_PRICE)
        self.assertIsNone(out["mom"])
        self.assertIsNone(out["yoy"])
        self.assertIn("不拿邻近的点硬凑", out["reason"])

    def test_mom_is_none_before_a_series_is_long_enough(self):
        conn = self._ingest([("2026-09-01", 11.0)])
        self.assertIsNone(PIS.yoy_mom(conn, _pig.M_NATIONAL_PIG_PRICE)["mom"])

    def test_empty_series_says_so_instead_of_raising(self):
        out = PIS.yoy_mom(_conn(), _pig.M_NATIONAL_PIG_PRICE)
        self.assertIsNone(out["latest"])
        self.assertIn("先跑 ingest", out["reason"])

    def test_derived_values_are_not_stored_as_duplicate_truth(self):
        """同比是**算出来的**，不是库里的第二份真值。"""
        conn = self._ingest([("2025-09-01", 12.0), ("2026-09-01", 11.0)])
        stored = PIS.load(conn, _pig.M_NATIONAL_PIG_PRICE)
        self.assertEqual([r["value"] for r in stored], [12.0, 11.0])


class TestDescribeNeverRaises(unittest.TestCase):
    def test_empty_store_is_describable(self):
        """空仓要**说得出话**，而不是抛异常或返回一个看着像数据的空结构。"""
        conn = _conn()
        self.assertIsInstance(PIS.summary(conn), dict)
        text = PIS.describe(conn)
        self.assertIsInstance(text, str)
        self.assertIn("空的", text)

    def test_summary_counts_by_status(self):
        conn = _conn()
        PIS.ingest(conn, start="2026-09-01", end="2026-09-15",
                   providers_chain=(
                       FakeProvider([("2026-09-15", 10.9)]),
                       FakeProvider([], fail="超时")),
                   today="2026-09-27")
        text = json.dumps(PIS.summary(conn), ensure_ascii=False, default=str)
        self.assertIn(PIS.FETCH_OK, text)
        self.assertIn(PIS.FETCH_ERROR, text)


if __name__ == "__main__":
    unittest.main()
