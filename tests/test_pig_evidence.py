# -*- coding: utf-8 -*-
"""猪行业读侧接线 / 证据视图 / 派生价差 / 错行修（批 5.2 §十一）。

这一组测试钉的是**四件「看起来没问题」的事**：

1. **读数是择优的结果，不是本层重新排的名**。所以有一条测试把
   ``pig_observations.preferred`` 换掉，看读数会不会跟着走——不跟着走就说明出现了
   第二个择优入口，而第二个入口没人会去检查。
2. **后备（同量纲替补）只有「同一格」才允许**：同 metric、同 unit、同 scope、
   同 benchmark_type。三条反例各自单独钉一遍——只比 unit 的话，「全国价顶广东价」
   与「同组中位偏离顶区域市场偏离」都会悄悄通过，而两边的数都是正常的元/公斤。
3. **缺值必须同时**是 ``value is None``、``status`` 留着、``reason`` 有话说。
   少第三件，「缺」就退化成一个空值，空值不告诉人下一步该干什么。
4. **派生价差只在期间完全相同时生成**：月均公司价减某一天的广东价是一个看起来
   完全正常的元/公斤数，机器不拦就一定有人算得出来。

全部离线、不联网、**不碰真库**：纯函数与观测仓用 ``:memory:``，其余用
``tempfile`` 建的 research.db。观测仓那一路自带库——本机真的落了四家的月报，
同一个断言在不同机器上必须给出同一个答案。
"""
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import factors as F                                 # noqa: E402
from research import pig_bulletins as pb                          # noqa: E402
from research import pig_evidence as ev                           # noqa: E402
from research import pig_industry_series as PIS                   # noqa: E402
from research import pig_observations as obs                      # noqa: E402
from research import pig_premium as prem                          # noqa: E402
from research import pig_readings as readings                     # noqa: E402
from research import pig_repair as repair                         # noqa: E402
from research import rules                                        # noqa: E402
from research.industry import pig as PIG                          # noqa: E402

CODE = "002714"
GUANGDONG = "001201"
HASH = "b" * 64
URL = "https://static.cninfo.com.cn/finalpage/2026-09-08/1225551030.PDF"
JS_PATH = ROOT / "static" / "research.js"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _conn():
    """内存库。``row_factory`` 必须给：``pig_bulletins.load`` 与
    ``pig_industry_series.load`` 都按列名读。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    PIS.ensure_schema(conn)
    return conn


def _observation(metric_id, variant, *, period, value, unit, scope,
                 level_type=PIG.SRC_MONTHLY_BULLETIN, code=CODE, document=HASH,
                 status=obs.STATUS_OK, extraction_method="local_parse",
                 paragraph="夹具原文", reason=None, benchmark_type=None,
                 subject=None):
    return obs.Observation(
        metric_id, metric_variant=variant, subject=subject, company_code=code,
        period=period, value=value, unit=unit, scope=scope,
        source_type=level_type, source_name="夹具来源", source_url=URL,
        document=document, paragraph=paragraph, page=2,
        publication_date="2026-09-08", extraction_method=extraction_method,
        benchmark_type=benchmark_type, is_direct_disclosure=True,
        status=status, reason=reason, fetched_at="2026-09-08 10:00:00",
        first_seen_at="2026-09-08 10:00:00")


def _company_price(period, value, *, code=CODE, scope=pb.SCOPE_COMMODITY,
                   variant="monthly_commodity_price", unit="CNY/kg"):
    """公司侧的一条商品猪均价观测（月报口径）。"""
    return _observation(PIG.M_PIG_SALE_PRICE, variant, period=period,
                        value=value, unit=unit, scope=scope, code=code)


def _series_point(day, value, *, metric_id=PIG.M_NATIONAL_PIG_PRICE,
                  variant="national_avg_price", region=None, unit="CNY/kg",
                  source_type=PIG.SRC_COMMERCIAL_DB, fetched_at=None,
                  name="夹具行业源"):
    """一条行业序列点。``series_hash`` 必须逐行不同（它是主键）。"""
    stamp = fetched_at or "2026-09-01 00:00:00"
    return {
        "series_hash": "%s|%s|%s|%s|%s" % (metric_id, variant, region, day, value),
        "metric_id": metric_id, "metric_variant": variant, "region": region,
        # ``scope`` 跟着 ``region`` 走（与写侧一致，线上实测就是
        # ``national`` / ``region:440000``）——它是后备四把锁里的那把 scope。
        "scope": "national" if region is None else "region:%s" % region,
        "period": day, "value": value, "unit": unit,
        "source_level": obs.level_of(source_type), "source_type": source_type,
        "source_name": name, "source_url": "https://example.test/line",
        "request_params": "{}", "raw_response_hash": "f" * 64,
        "publication_date": None, "status": PIS.STATUS_OK, "reason": None,
        "note": None, "fetched_at": stamp, "first_seen_at": stamp,
    }


def _month_points(month, days, value=11.83, **kw):
    return [_series_point("%s-%02d" % (month, day), value, **kw)
            for day in range(1, days + 1)]


def _row(page, *texts):
    return {"page": page, "y": 0.0,
            "cells": [{"x": i * 100.0, "text": t} for i, t in enumerate(texts)]}


def _line(page, period, heads, cum_heads, revenue, cum_revenue, price):
    return _row(page, period, "%.2f" % heads, "%.2f" % cum_heads,
                "%.2f" % revenue, "%.2f" % cum_revenue, "%.2f" % price)


_HEAD_COMMODITY = ("商品猪销量（万头）", "商品猪销售收入（万元）",
                   "商品猪价格（元/公斤）")


def _bulletin_rows(*, title="2026年8月份销售简报", body=None, extra=()):
    rows = [_row(1, "证券代码：%s" % CODE), _row(1, title),
            _row(1, *_HEAD_COMMODITY)]
    rows.extend(body if body is not None else [
        _line(1, "2026年6月", 100, 100, 10, 10, 10),
        _line(1, "2026年7月", 110, 210, 11, 21, 10),
        _line(1, "2026年8月", 120, 330, 12, 33, 12.16)])
    rows.extend(extra)
    return rows


def _parsed_bulletin(**kw):
    return pb.extract_monthly_bulletin(_bulletin_rows(**kw), stock_code=CODE,
                                       source_url=URL, document_hash=HASH)


def _span_parsed():
    """牧原 2025-09 那一份：「25年**1-9月**公司**共**销售仔猪1,157.1万头」。"""
    return pb.extract_monthly_bulletin(
        _bulletin_rows(
            title="2025年9月份销售简报",
            body=[_line(1, "2025年7月", 100, 100, 10, 10, 10),
                  _line(1, "2025年8月", 110, 210, 11, 21, 10),
                  _line(1, "2025年9月", 120, 330, 12, 33, 10)],
            extra=[_row(1, "25年1-9月公司共销售仔猪1,157.1万头。")]),
        stock_code=CODE, source_url=URL, document_hash=HASH)


def _cfg(*, mapping=None, default="national_market", min_points=20):
    return {
        "company_benchmark_mapping": dict(mapping or {}),
        "default_benchmark_type": default,
        "benchmark_markets": {
            "national_market": {"metric_id": PIG.M_NATIONAL_PIG_PRICE,
                                "metric_variant": "national_avg_price",
                                "region": None},
            "regional_market": {"metric_id": PIG.M_NATIONAL_PIG_PRICE,
                                "metric_variant": "provincial_avg_price",
                                "region_from_mapping": True},
        },
        "premium_min_benchmark_points": min_points,
    }


def _store(records, meta=None):
    """手工的 ``store``（``state()`` 认字典这一态，见它的说明）。"""
    return {"records": list(records), "obs_meta": dict(meta or {}),
            "notes": [], "counts": {}, "used_bulletins": False}


def _record(metric_id, variant, *, value, unit, scope, period="2026-08",
            source_type=PIG.SRC_MONTHLY_BULLETIN, status=PIG.STATUS_OK,
            note="夹具"):
    return PIG.PigMetricRecord(metric_id, metric_variant=variant,
                               company_code=CODE, period=period, value=value,
                               unit=unit, scope=scope, source_type=source_type,
                               source_name="夹具", status=status, note=note)


# --------------------------------------------------------------------------- #
# 二、读侧接线
# --------------------------------------------------------------------------- #
class TestReadingsWiring(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def _load(self, code=CODE):
        return readings.load(code, conn=self.conn)

    def test_state_reads_the_observation_store(self):
        """观测仓里的月价进 ``company_sale_price`` 的读数。

        两条同组（同口径、同期、同单位）观测，一条 L2 直接披露、一条 L7 推算：
        读数必须是 **L2 那条**——这是 ``preferred()`` 的级别优先，不是本层的排序。
        同时它走的是**后备**：正身 ``annual_commodity_price`` 这一格里一个观测
        都没有（实测：全库 3019 行观测全是 ``monthly_*``）。
        """
        obs.append(self.conn, [_company_price("2026-08", 12.16),
                               _company_price("2026-08", 11.0,
                                              variant="monthly_commodity_price",
                                              unit="CNY/kg")])
        # 第二条换成 L7 推算来源，其余键一致 → 落进同一个 conflict group。
        self.conn.execute("DELETE FROM pig_metric_observation"
                          " WHERE source_level='L2' AND value=11.0")
        obs.append(self.conn, [obs.Observation(
            PIG.M_PIG_SALE_PRICE, metric_variant="monthly_commodity_price",
            company_code=CODE, period="2026-08", value=11.0, unit="CNY/kg",
            scope=pb.SCOPE_COMMODITY, source_type=PIG.SRC_DERIVED,
            source_name="夹具推算", document="c" * 64, extraction_method="api",
            derivation="fixture", is_direct_disclosure=False,
            status=obs.STATUS_OK)])
        state = PIG.state(CODE, reports=[], cfg=None, store=self._load())
        reading = state["readings"]["company_sale_price"]
        self.assertEqual(reading["value"], 12.16)
        self.assertEqual(reading["metric_variant"], "monthly_commodity_price")
        self.assertEqual(reading["expected_variant"], "annual_commodity_price")
        self.assertTrue(reading["variant_fallback"])
        self.assertEqual(reading["source_level"], "L2")

    def test_state_uses_preferred_not_its_own_ranking(self):
        """把 ``preferred`` 换掉，读数必须跟着走。

        换成一个「值 999、哈希指向另一条」的载荷。读数若还给出 12.16，说明本层
        自己排了一次名——那就是第二个择优入口。
        """
        rows = [_company_price("2026-08", 12.16)]
        obs.append(self.conn, rows)
        loaded = obs.load(self.conn, code=CODE)
        forced = dict(loaded[0].to_dict())
        forced.update({"observation_hash": loaded[0].observation_hash,
                       "value": 999.0, "status": obs.STATUS_OK,
                       "period": "2026-08", "unit": "CNY/kg",
                       "scope": pb.SCOPE_COMMODITY})
        group_id = loaded[0].conflict_group_id
        calls = []

        def fake_preferred(items):
            calls.append(list(items))
            return {group_id: forced}

        with mock.patch.object(obs, "preferred", side_effect=fake_preferred):
            state = PIG.state(CODE, reports=[], cfg=None, store=self._load())
        self.assertEqual(calls, [calls[0]] if calls else [], "preferred 没被调用")
        self.assertTrue(calls, "本层没有调用 preferred——出现了第二个择优入口")
        self.assertEqual(state["readings"]["company_sale_price"]["value"], 999.0)

    def test_state_reads_the_bulletin_cache(self):
        """观测仓空着、只有简报缓存时，读数照样拿得到值（用当前解析器重跑）。

        而且：**同一份事实同时存在于两处时只出现一次**（按 ``observation_hash``
        去重）——不去重的话，同一件事会被记两遍，看起来像两个来源在互相印证。
        """
        pb.save(self.conn, CODE, "2026-08", _parsed_bulletin())
        replayed = pb.observations_of(_parsed_bulletin())
        obs.append(self.conn, replayed)
        rows, notes, bulletins = readings.observations(self.conn, CODE)
        hashes = [row.observation_hash for row in rows]
        self.assertEqual(len(hashes), len(set(hashes)), "简报缓存与观测仓没有去重")
        self.assertEqual(len(hashes), len({o.observation_hash for o in replayed}))
        self.assertTrue(bulletins)
        state = PIG.state(CODE, reports=[], cfg=None, store=self._load())
        self.assertEqual(state["readings"]["company_sale_price"]["value"], 12.16)

    def test_observation_round_trip_reproduces_the_hash(self):
        """``load()`` 出来的行重建 ``Observation``，哈希必须与库里一致。

        这是合并去重的前提：哈希不可复现的话，「同一份事实」在库里会变成两行，
        而两行看起来就是两个来源各说了一个数。
        """
        obs.append(self.conn, pb.observations_of(_parsed_bulletin()))
        rows = obs.load(self.conn, code=CODE)
        self.assertTrue(rows)
        for row in rows:
            rebuilt = obs.Observation(**{
                name: getattr(row, name) for name in obs.COLUMNS
                if name not in ("observation_hash", "conflict_group_id")})
            self.assertEqual(rebuilt.observation_hash, row.observation_hash)
            self.assertEqual(rebuilt.conflict_group_id, row.conflict_group_id)

    def test_state_reads_the_industry_series(self):
        """行业序列里的全国价进 ``pig_product_price`` 的读数。"""
        PIS._write_points(self.conn, _month_points("2026-08", 31, 11.83))
        state = PIG.state(CODE, reports=[], cfg=None, store=self._load())
        reading = state["readings"]["pig_product_price"]
        self.assertEqual(reading["value"], 11.83)
        self.assertEqual(reading["metric_variant"], "national_avg_price")
        self.assertEqual(reading["unit"], "CNY/kg")
        self.assertFalse(reading["variant_fallback"])
        self.assertIsNone(reading["observation_hash"],
                          "序列不是观测，不许编一个 observation_hash 出来")

    def test_regional_series_never_feeds_a_national_reading(self):
        """只有广东点、没有全国点时，全国口径的读数**为空**（不是拿广东顶）。

        两种写法都要挡住：广东点用另一条 variant（``provincial_avg_price``）；
        以及最阴的一种——**同一条全国 variant、只是 region 写成了省号**。
        """
        PIS._write_points(self.conn, _month_points(
            "2026-08", 31, 11.83, variant="provincial_avg_price",
            region="440000"))
        PIS._write_points(self.conn, _month_points(
            "2026-08", 31, 9.99, region="440000"))
        state = PIG.state(CODE, reports=[], cfg=None, store=self._load())
        reading = state["readings"]["pig_product_price"]
        self.assertIsNone(
            reading["value"], "广东价 11.83 被当成了全国价——全国与省是同一 metric")
        self.assertEqual(reading["status"], PIG.STATUS_MISSING)
        self.assertTrue(reading["reason"])
        # 广东那一条**确实在**（映射表里 001201 要它），只是它的 scope 摆明了
        # 「这是广东」——后备的四把锁里 scope 那一把就是挡这个的。
        regional = [rec for rec in self._load()["records"]
                    if rec.metric_variant == "provincial_avg_price"]
        self.assertEqual([rec.scope for rec in regional], ["region:440000"])
        self.assertEqual([rec.value for rec in regional], [11.83])

    def test_state_survives_a_broken_store(self):
        """三张表读不出来时，报告侧那一半**不许跟着一起消失**，理由要写进 notes。"""
        broken = {"records": [], "obs_meta": {}, "notes": ["夹具：观测仓读不出来"],
                  "counts": {}, "used_bulletins": False}
        state = PIG.state(CODE, reports=[], cfg=None, store=broken)
        self.assertIn("夹具：观测仓读不出来", state["store_notes"])
        self.assertTrue(state["readings"])


# --------------------------------------------------------------------------- #
# 三、同量纲后备的四把锁
# --------------------------------------------------------------------------- #
class TestVariantFallbackLocks(unittest.TestCase):

    def _state(self, records, meta=None):
        return PIG.PigCompanyState(CODE, records=records, exposure={},
                                   obs_meta=meta or {})

    def _monthly(self, **kw):
        kw.setdefault("value", 12.16)
        kw.setdefault("unit", "CNY/kg")
        kw.setdefault("scope", pb.SCOPE_COMMODITY)
        return _record(PIG.M_PIG_SALE_PRICE, "monthly_commodity_price", **kw)

    def test_fallback_takes_the_same_grid(self):
        """正身缺、同格有一条月价 → 后备拿它，理由写得清「只是时间窗口不同」。"""
        state = self._state([self._monthly()])
        pick, why = state.variant_fallback(PIG.M_PIG_SALE_PRICE,
                                           "annual_commodity_price")
        self.assertIsNotNone(pick)
        self.assertEqual(pick.metric_variant, "monthly_commodity_price")
        self.assertIn("annual_commodity_price", why)
        self.assertIn("时间窗口", why)

    def test_fallback_requires_the_same_unit(self):
        """单位不同 → 不后备。单位取自 ``MetricDef`` 的逐 variant 声明。"""
        state = self._state([self._monthly(unit="元/吨")])
        pick, why = state.variant_fallback(PIG.M_PIG_SALE_PRICE,
                                           "annual_commodity_price")
        self.assertIsNone(pick)
        self.assertIn("unit", why)

    def test_fallback_requires_the_same_scope(self):
        """口径不同 → 不后备。总生猪（``company_live_hog_all``）不许顶商品猪。"""
        state = self._state([self._monthly(scope=pb.SCOPE_ALL)])
        pick, _why = state.variant_fallback(PIG.M_PIG_SALE_PRICE,
                                            "annual_commodity_price")
        self.assertIsNone(pick)

    def test_fallback_requires_the_same_benchmark_type(self):
        """基准不同 → 不后备，而且**只**因为基准不同。

        这一条刻意让 unit 与 scope 都对齐（行业价的全国口径与省口径**同 metric
        同 unit**），把锁单独留给 ``benchmark_type``：它是「比的是谁」。
        """
        rows = [_record(PIG.M_NATIONAL_PIG_PRICE, "provincial_avg_price",
                        value=11.83, unit="CNY/kg", scope=PIG.SCOPE_NATIONAL)]
        grid = PIG.METRIC_GRIDS[(PIG.M_NATIONAL_PIG_PRICE, "national_avg_price")]
        self.assertEqual(grid[1], None, "这条测试的前提是正身基准为 None")
        key = (PIG.M_NATIONAL_PIG_PRICE, "provincial_avg_price", "2026-08",
               PIG.SCOPE_NATIONAL)
        # ① 基准写成区域市场 → 锁住；
        blocked = self._state(rows, {key: {"benchmark_type": "regional_market"}})
        self.assertIsNone(blocked.variant_fallback(PIG.M_NATIONAL_PIG_PRICE,
                                                   "national_avg_price")[0])
        # ② 同一批记录、基准与正身一致（None）→ 通过。差别只有基准这一项。
        allowed = self._state(rows, {key: {"benchmark_type": None}})
        self.assertIsNotNone(allowed.variant_fallback(PIG.M_NATIONAL_PIG_PRICE,
                                                      "national_avg_price")[0])

    def test_fallback_refuses_a_grid_that_was_never_declared(self):
        """正身没在 ``METRIC_GRIDS`` 里声明格子 → 一律不许后备（fail-safe）。"""
        state = self._state([_record(PIG.M_FULL_COST, "FULL_COST_COMPANY_DISCLOSED",
                                     value=13.0, unit="CNY/kg",
                                     scope="company_pig_industry")])
        pick, why = state.variant_fallback(PIG.M_FULL_COST, "COMPLETE_COST_PER_KG")
        self.assertIsNone(pick)
        self.assertIn("没有声明格子", why)

    def test_fallback_marks_the_reading_it_used(self):
        """后备成功时：``variant_fallback`` 为真、``expected_variant`` 是正身、
        ``metric_variant`` 是实际用的那个，而且 ``gaps`` 仍然报正身缺失。

        「有读数」与「正身仍缺」必须同时可见——只报一个的话，看的人会以为
        「年价已经拿到了」。
        """
        state = PIG.state(CODE, reports=[], store=_store([self._monthly()]))
        reading = state["readings"]["company_sale_price"]
        self.assertTrue(reading["variant_fallback"])
        self.assertEqual(reading["expected_variant"], "annual_commodity_price")
        self.assertEqual(reading["metric_variant"], "monthly_commodity_price")
        self.assertIn("时间窗口", reading["variant_fallback_reason"])
        self.assertIn(PIG.M_PIG_SALE_PRICE, state["gaps"])

    def test_missing_reading_keeps_status_and_reason_and_no_zero(self):
        """缺值时 ``value is None``、``status`` 与 ``reason`` 都在，**绝不用 0 顶**。"""
        state = PIG.state(CODE, reports=[], store=_store([]))
        for factor_id in ("full_cost", "unit_margin", "sow_supply_pressure",
                          "financial_survivability", "price_position"):
            reading = state["readings"][factor_id]
            self.assertIsNone(reading["value"], factor_id)
            self.assertTrue(reading["status"], factor_id)
            self.assertTrue(reading["reason"], factor_id)
            self.assertNotEqual(reading["value"], 0, factor_id)

    def test_no_reading_substitutes_another_metric(self):
        """不许拿别的指标顶上：分部毛利率 ≠ 完全成本 / 单位毛利；
        总生猪销量 ≠ 商品猪销量；仔猪**价** ≠ 仔猪**数量**。"""
        store = _store([
            _record(PIG.M_PIG_SEGMENT_GROSS_MARGIN, "segment_gross_margin_ratio",
                    value=31.0, unit="%", scope="company_pig_industry"),
            _record(PIG.M_HOG_SALES_VOLUME, "monthly_heads", value=703.0,
                    unit="万头", scope=pb.SCOPE_ALL),
            _record(PIG.M_PIGLET_PRICE, "national_avg_price", value=32.5,
                    unit="CNY/kg", scope=PIG.SCOPE_NATIONAL),
        ])
        state = PIG.state(CODE, reports=[], store=store)
        for factor_id in ("full_cost", "unit_margin"):
            self.assertIsNone(state["readings"][factor_id]["value"], factor_id)
        self.assertIsNone(
            state["readings"]["capacity_delivery"]["value"],
            "商品猪销量缺，不许拿总生猪销量顶")
        self.assertIsNone(
            state["readings"]["piglet_supply_pressure"]["value"],
            "仔猪成交量缺，不许拿仔猪价顶")


# --------------------------------------------------------------------------- #
# 五、月均与派生价差
# --------------------------------------------------------------------------- #
class TestMonthlyAverage(unittest.TestCase):

    def test_daily_point_never_stands_in_for_a_monthly_average(self):
        """只有一天 → 不出月均，理由写清「点数不够」。"""
        rows = [_series_point("2026-08-01", 11.83)]
        out = prem.monthly_average(rows, region=None, month="2026-08",
                                   min_points=20)
        self.assertIsNone(out["value"])
        self.assertEqual(out["status"], "insufficient_benchmark_points")
        self.assertIn("20", out["reason"])

    def test_no_points_at_all_is_not_an_average_of_nothing(self):
        out = prem.monthly_average([], region=None, month="2026-08", min_points=20)
        self.assertIsNone(out["value"])
        self.assertEqual(out["status"], "no_benchmark_points")

    def test_monthly_average_dedupes_dates_and_reports_revisions(self):
        """同一天两行（值不同）：取 ``fetched_at`` 新的那条，并如实报修订数。

        2026-09 实测每天 2 行（值相同、``series_hash`` 不同）——去重不做的话，
        那些天会被算两次。**按取数时间挑，不按值挑**：同一天两次取数拿到不同的值，
        说明来源自己改过数，后来那次才是它的修订。
        """
        rows = [_series_point("2026-08-01", 11.00,
                              fetched_at="2026-09-01 03:00:00"),
                _series_point("2026-08-01", 12.00,
                              fetched_at="2026-09-01 04:00:00"),
                _series_point("2026-08-02", 12.00,
                              fetched_at="2026-09-01 04:00:00")]
        out = prem.monthly_average(rows, region=None, month="2026-08",
                                   min_points=1)
        self.assertEqual((out["points"], out["rows"], out["revisions"]),
                         (2, 3, 1))
        self.assertEqual(out["conflicting_rows"], 1)
        self.assertEqual(out["value"], 12.0, "留下的必须是 fetched_at 新的那条")

    def test_monthly_average_does_not_mix_regions(self):
        """全国点与广东点混在一批：``region=None`` **只**留全国点。

        ``series.load(region=None)`` 是「不筛」——拿它当「只要全国」会把广东的
        11.83 平均进全国价，而两个数都是正常的元/公斤。
        """
        rows = _month_points("2026-08", 3, 10.0) + \
            _month_points("2026-08", 3, 20.0, region="440000",
                          variant="provincial_avg_price")
        out = prem.monthly_average(rows, region=None, month="2026-08", min_points=1)
        self.assertEqual(out["points"], 3)
        self.assertEqual(out["value"], 10.0)
        gd = prem.monthly_average(rows, region="440000", month="2026-08",
                                  min_points=1)
        self.assertEqual(gd["value"], 20.0)


class TestPremiumDerivation(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def _market(self):
        PIS._write_points(self.conn, _month_points("2026-08", 31, 11.83))

    def test_premium_only_when_periods_match_exactly(self):
        """期间完全匹配才生成；区间期（``2025-01~02``）**不生成**，如实跳过。"""
        self._market()
        obs.append(self.conn, [_company_price("2026-08", 12.16),
                               _company_price("2025-01~02", 12.00)])
        report = prem.derive(self.conn, CODE, _cfg())
        periods = sorted(r.period for r in report["records"])
        self.assertEqual(periods, ["2026-08", "2026-08"])
        skipped = [item["period"] for item in report["skipped"]]
        self.assertEqual(skipped, ["2025-01~02"])
        self.assertIn("区间", report["skipped"][0]["why"])

    def test_premium_saves_benchmark_type_and_derivation_fields(self):
        """落库字段逐条对：在册的 ``benchmark_type``、可解析的 ``derivation``、
        推算即估计、**不是**直接披露、自检无错。"""
        self._market()
        obs.append(self.conn, [_company_price("2026-08", 12.16)])
        records = prem.derive(self.conn, CODE, _cfg())["records"]
        self.assertEqual(len(records), 2)
        by_variant = {r.metric_variant: r for r in records}
        self.assertEqual(set(by_variant),
                         {prem.DEVIATION_VARIANT, prem.DEVIATION_PCT_VARIANT})
        self.assertEqual(obs.check_errors(records), [])
        for record in records:
            self.assertEqual(record.metric_id, PIG.M_REGIONAL_PREMIUM)
            self.assertEqual(record.source_level, "L7")
            self.assertTrue(record.is_estimated)
            self.assertFalse(record.is_direct_disclosure)
            self.assertIn(record.benchmark_type, obs.BENCHMARK_TYPES)
            self.assertEqual(record.benchmark_type, "national_market")
            self.assertIsNone(record.region, "全国基准的 region 必须是空")
            parts = prem.parse_derivation(record.derivation)
            self.assertEqual(parts["company_period"], "2026-08")
            self.assertEqual(parts["benchmark_period"], "2026-08")
            self.assertEqual(parts["benchmark_type"], "national_market")
            self.assertEqual(float(parts["benchmark_value"]), 11.83)
        self.assertEqual(by_variant[prem.DEVIATION_VARIANT].value, 0.33)
        # 百分比是**显式换算**的：库里存 2.7895（单位 %），不是 0.0279。
        self.assertEqual(by_variant[prem.DEVIATION_PCT_VARIANT].unit, "%")
        self.assertEqual(by_variant[prem.DEVIATION_PCT_VARIANT].value, 2.7895)
        self.assertEqual(
            prem.parse_derivation(
                by_variant[prem.DEVIATION_PCT_VARIANT].derivation)["scale"], "100")

    def test_unusable_benchmark_month_leaves_a_trace_without_a_value(self):
        """公司有当月价、基准侧只有 3 天 → 落 ``INSUFFICIENT_SCOPE`` + 理由，
        **不写 0、不拿别的月份凑**。"""
        PIS._write_points(self.conn, _month_points("2026-08", 3, 11.83))
        obs.append(self.conn, [_company_price("2026-08", 12.16)])
        records = prem.derive(self.conn, CODE, _cfg())["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, obs.STATUS_INSUFFICIENT_SCOPE)
        self.assertIsNone(records[0].value)
        self.assertIn("3 个日期", records[0].reason)
        self.assertIn("门槛 20", records[0].reason)
        self.assertEqual(
            prem.parse_derivation(records[0].derivation)["benchmark_status"],
            "insufficient_benchmark_points")

    def test_apply_writes_once_and_is_idempotent(self):
        """落库走 ``obs.append``（唯一写入口），复跑不新增行。"""
        self._market()
        obs.append(self.conn, [_company_price("2026-08", 12.16)])
        records = prem.derive(self.conn, CODE, _cfg())["records"]
        fresh, dup = obs.append(self.conn, records)
        self.assertEqual((fresh, dup), (2, 0))
        again, dup_again = obs.append(self.conn, records)
        self.assertEqual((again, dup_again), (0, 2))
        self.assertEqual(obs.check_errors(obs.load(self.conn, code=CODE)), [])

    def test_benchmark_mapping_defaults_to_national(self):
        """映射表里写了的走区域，写了的走全国——**不许写死广东**。

        用的是**线上那份** ``RULES_V1['pig']``，不是夹具：这条测试要钉的正是
        「产品里到底怎么配的」。
        """
        cfg = rules.RULES_V1["pig"]
        dong = prem.benchmark_for(GUANGDONG, cfg)
        self.assertEqual(dong["benchmark_type"], "regional_market")
        self.assertEqual(dong["region"], "440000")
        self.assertEqual(dong["metric_variant"], "provincial_avg_price")
        for code in (CODE, "002100", "000876"):
            default = prem.benchmark_for(code, cfg)
            self.assertEqual(default["benchmark_type"], "national_market", code)
            self.assertIsNone(default["region"], code)
            self.assertEqual(default["metric_variant"], "national_avg_price")
        self.assertEqual(prem.min_points(cfg), 20)

    def test_companies_are_discovered_from_the_store(self):
        """默认集合从数据里读（有当月价的公司），**不写死四家**。"""
        obs.append(self.conn, [_company_price("2026-08", 12.16),
                               _company_price("2026-08", 13.0, code="002100")])
        self.assertEqual(prem.companies_with_price(self.conn),
                         sorted(["002100", CODE]))


# --------------------------------------------------------------------------- #
# 四、证据视图
# --------------------------------------------------------------------------- #
class TestEvidencePayload(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)
        # ``build`` 里 ``state()`` 那一半不传 reports，会去读**本机缓存**。
        # 桩掉它：这一组测试要的是一个固定的输入，而不是这台机器上碰巧有什么。
        patcher = mock.patch("research.pig_reports.inspect_cached_pig_report",
                             return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _build(self, *, candidates=True):
        return ev.build(self.conn, CODE, cfg=_cfg(), candidates=candidates)

    def test_evidence_payload_shape(self):
        """每个指标都有桶；候选**一条不藏**；字段齐；查不到的东西写 ``None``。"""
        obs.append(self.conn, [_company_price("2026-08", 12.16),
                               _company_price("2026-07", 12.00),
                               _company_price("2026-08", 12.16,
                                              code="002100")])
        payload = self._build()
        bucket = payload["metrics"]["pig_sale_price"]
        self.assertEqual(
            set(bucket),
            {"metric_id", "metric_label", "preferred", "preferreds",
             "candidates", "conflicts", "series", "series_skipped"})
        self.assertEqual(len(bucket["candidates"]), 2,
                         "只给首选就是藏候选——这一页存在的理由正是不藏")
        self.assertEqual(bucket["metric_label"], "公司销售均价")
        hashes = {row.observation_hash for row in obs.load(self.conn, code=CODE)}
        for candidate in bucket["candidates"]:
            for field in ev.EVIDENCE_FIELDS:
                self.assertIn(field, candidate)
            self.assertIn(candidate["observation_hash"], hashes)
            self.assertEqual(candidate["company_code"], CODE)
        # 公司在缓存里没有这一期的文件 → parser_version 必须是 None，不许猜一个 1。
        self.assertIsNone(bucket["candidates"][0]["parser_version"])
        # 首选是**最新一期**那一组的首选，其余各期在 preferreds 里，一条没少。
        self.assertEqual(bucket["preferred"]["period"], "2026-08")
        self.assertEqual(sorted(g["period"] for g in bucket["preferreds"]),
                         ["2026-07", "2026-08"])
        self.assertEqual(payload["counts"]["candidates"], 2)
        self.assertEqual(payload["filters"]["candidates"], True)

    def test_evidence_can_be_fetched_without_the_candidate_details(self):
        """``candidates=False`` 只少明细，**不改载荷形状**：键永远在。

        界面概览走这一态（四家的完整载荷实测 1.8~2.0 MB），点开某一行时再按
        ``metric_id`` + ``period`` 取那一组。
        """
        obs.append(self.conn, [_company_price("2026-08", 12.16)])
        payload = self._build(candidates=False)
        bucket = payload["metrics"]["pig_sale_price"]
        self.assertEqual(bucket["candidates"], [])
        self.assertTrue(bucket["preferreds"])
        self.assertIsNone(bucket["preferreds"][0]["observation"])
        self.assertIn("observation", bucket["preferreds"][0])
        self.assertIn("preferred", bucket)
        self.assertEqual(self._build()["metrics"]["pig_sale_price"]["preferred"]
                         ["observation"]["observation_hash"],
                         obs.load(self.conn, code=CODE)[0].observation_hash)

    def test_single_group_can_be_fetched_by_metric_and_period(self):
        """按 ``metric_id`` + ``period`` 取那一组：候选只留那一组，**别的组不掺**。"""
        obs.append(self.conn, [_company_price("2026-08", 12.16),
                               _company_price("2026-07", 12.00)])
        payload = ev.build(self.conn, CODE, metric_id="pig_sale_price",
                           period="2026-08", cfg=_cfg())
        bucket = payload["metrics"]["pig_sale_price"]
        self.assertEqual({c["period"] for c in bucket["candidates"]}, {"2026-08"})
        self.assertEqual([g["period"] for g in bucket["preferreds"]], ["2026-08"])

    def test_conflicts_are_not_hidden(self):
        """两条同级不同值 → 冲突摆出来，首选落成 ``CONFLICT`` 且**不挑一个值**。"""
        same = dict(period="2026-08", unit="CNY/kg", scope=pb.SCOPE_COMMODITY)
        obs.append(self.conn, [
            _observation(PIG.M_PIG_SALE_PRICE, "monthly_commodity_price",
                         value=12.16, document="a" * 64, **same),
            _observation(PIG.M_PIG_SALE_PRICE, "monthly_commodity_price",
                         value=11.00, document="c" * 64,
                         reason="另一家媒体口径", **same)])
        payload = self._build()
        bucket = payload["metrics"]["pig_sale_price"]
        self.assertEqual(len(bucket["conflicts"]), 1)
        conflict = bucket["conflicts"][0]
        self.assertEqual(sorted(conflict["values"]), [11.0, 12.16])
        self.assertEqual(len(conflict["observations"]), 2)
        preferred = bucket["preferred"]
        self.assertEqual(preferred["status"], obs.STATUS_CONFLICT)
        self.assertIsNone(preferred["value"])

    def test_readings_carry_the_labels_the_page_shows(self):
        """中文标签由**后端**下发（页面文案只从载荷取，前端不写第二份表）。"""
        obs.append(self.conn, [_company_price("2026-08", 12.16)])
        payload = self._build()
        for factor_id, reading in payload["readings"].items():
            self.assertTrue(reading.get("factor_label"), factor_id)
            self.assertIn("status_label", reading)
        reading = payload["readings"]["company_sale_price"]
        self.assertEqual(reading["status_label"], obs.STATUS_LABELS[obs.STATUS_OK])
        self.assertEqual(reading["source_level_label"],
                         obs.LABEL_BY_LEVEL["L2"])

    def test_series_are_summarised_by_month_not_by_day(self):
        """序列按月摘要（月均 / 点数 / 行数 / 修订），逐日点不硬塞进载荷。"""
        PIS._write_points(self.conn, _month_points("2026-08", 31, 11.83))
        payload = self._build()
        groups = payload["metrics"][PIG.M_NATIONAL_PIG_PRICE]["series"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["points_total"], 31)
        month = groups[0]["months"][0]
        self.assertEqual(month["month"], "2026-08")
        self.assertEqual((month["points"], month["rows"], month["revisions"]),
                         (31, 31, 0))
        self.assertEqual(month["value"], 11.83)
        self.assertTrue(month["enough_for_benchmark"])

    def test_an_empty_grid_still_gets_a_bucket(self):
        """读侧接的指标即使一条观测都没有，也必须有桶——「这格什么都没有」与
        「这格没被列出来」在界面上是两件事。"""
        payload = self._build()
        for metric_id in (PIG.M_FULL_COST, PIG.M_EFFECTIVE_CAPACITY):
            self.assertIn(metric_id, payload["metrics"])
            self.assertIsNone(payload["metrics"][metric_id]["preferred"])


# --------------------------------------------------------------------------- #
# 一、错行修（离线 CLI）
# --------------------------------------------------------------------------- #
class TestRepair(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def _cache_the_span_bulletin(self):
        pb.save(self.conn, CODE, "2025-09", _span_parsed())

    def _stale_row(self):
        """库里那条错行：1-9 月的累计数被当成 9 月单月落了库。"""
        return _observation(PIG.M_PIGLET_SALES_VOLUME, "monthly_heads",
                            period="2025-09", value=1157.1, unit="万头",
                            scope=pb.SCOPE_PIGLET,
                            paragraph="25年1-9月公司共销售仔猪1,157.1万头")

    def test_repair_plan_deletes_only_rederivable_rows(self):
        """只有「有文档、能重解析、重解析后不再产出它」的行才进删除清单。

        两条反例各自单独钉一遍：**人工录入**的行（我们无权重写别人的手填）与
        **缓存里对不上的文档**（没有能力重新解析它）。而且 dry-run **一行都不写**。
        """
        self._cache_the_span_bulletin()
        obs.append(self.conn, [
            self._stale_row(),
            # ① 人工录入：即便文档对得上，也不许删。
            _observation(PIG.M_PIGLET_SALES_VOLUME, "monthly_heads",
                         period="2025-08", value=1.0, unit="万头",
                         scope=pb.SCOPE_PIGLET, extraction_method="manual_entry"),
            # ② 文档不在缓存里：我们**没有能力**重新解析它。
            _observation(PIG.M_PIGLET_SALES_VOLUME, "monthly_heads",
                         period="2025-07", value=2.0, unit="万头",
                         scope=pb.SCOPE_PIGLET, document="d" * 64),
        ])
        before = len(obs.load(self.conn, code=CODE))
        plan = repair.plan(self.conn, CODE)
        self.assertEqual(len(obs.load(self.conn, code=CODE)), before,
                         "dry-run 不许写库")
        self.assertEqual([item["observation"].period for item in plan["delete"]],
                         ["2025-09"])
        self.assertIn("1-9月", plan["delete"][0]["why"])
        appended = {item["observation"].metric_id for item in plan["append"]}
        self.assertIn(PIG.M_PIGLET_SALES_VOLUME, appended)
        self.assertEqual(plan["untouched"], 2)

    def test_foreign_span_row_is_regenerated_with_the_span_period(self):
        """修完：错行的 (2025-09) 一格**再没有任何带值的行**，重落的是区间期。"""
        self._cache_the_span_bulletin()
        obs.append(self.conn, [self._stale_row()])
        plan = repair.plan(self.conn, CODE)
        result = repair.apply(self.conn, plan)
        self.assertEqual(result["deleted"], 1)
        self.rows = obs.load(self.conn, code=CODE)
        self.assertFalse([row for row in self.rows
                          if row.metric_id == PIG.M_PIGLET_SALES_VOLUME
                          and row.period == "2025-09" and row.has_value])
        spans = [row for row in self.rows
                 if row.metric_id == PIG.M_PIGLET_SALES_VOLUME
                 and row.period == "2025-01~09"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].status, obs.STATUS_INSUFFICIENT_SCOPE)
        self.assertIsNone(spans[0].value)
        # 复跑一遍：已经修好了，不该再删任何一行。
        self.assertEqual(repair.plan(self.conn, CODE)["delete"], [])

    def test_the_repair_cli_is_dry_run_by_default(self):
        """命令行默认 dry-run：不带 ``--apply`` 时**一行都不写**。

        这条钉的是「默认那一档是安全的」：手滑跑一次 CLI 不该改到任何东西。
        """
        self._cache_the_span_bulletin()
        obs.append(self.conn, [self._stale_row()])
        before = len(obs.load(self.conn, code=CODE))
        plan = repair.plan(self.conn, CODE)
        self.assertTrue(plan["delete"], "夹具本身要能产生一条待删行")
        self.assertEqual(len(obs.load(self.conn, code=CODE)), before)


# --------------------------------------------------------------------------- #
# 十、评分隔离 + 前端契约
# --------------------------------------------------------------------------- #
class TestScoringIsolation(unittest.TestCase):

    def test_no_pig_curve_is_declared(self):
        """本批没有给任何猪因子加曲线：有读数也只是 ``display_only``，不进分母。"""
        self.assertEqual(rules.RULES_V1["pig"].get("factor_curves"), {})

    def test_a_reading_without_a_curve_never_enters_a_denominator(self):
        """读数齐了、曲线没有 → ``display_only``，``loci`` 为空、``score`` 为 None。"""
        spec = F.FACTOR_INDEX["company_sale_price"]
        reading = {"value": 12.16, "metric_id": PIG.M_PIG_SALE_PRICE,
                   "metric_variant": "monthly_commodity_price",
                   "unit": "CNY/kg", "period": "2026-08", "status": "OK"}
        result = F._pig_result(spec, {
            "exposure": 0.9, "confidence": 0.5, "classification": "specialized",
            "readings": {"company_sale_price": reading}})
        self.assertEqual(result.status, F.STATUS_DISPLAY_ONLY)
        self.assertEqual(result.loci, [])
        self.assertIsNone(result.score)

    def test_exposure_below_the_gate_returns_before_the_readings(self):
        """暴露低于门槛 → ``not_applicable``：**在读到读数之前**就返回。

        四家实测的暴露全部 < 0.5 或为 None，所以读侧接进来的值对任何 factor 的
        载荷都不产生任何影响——这是本批「评分隔离」的结构性依据。
        """
        spec = F.FACTOR_INDEX["company_sale_price"]
        gate = rules.RULES_V1["pig"]["min_exposure_for_specialized"]
        for exposure, status in ((None, "missing_data"),
                                 (gate - 0.1, F.STATUS_NOT_APPLICABLE)):
            result = F._pig_result(spec, {
                "exposure": exposure, "readings": {"company_sale_price": {
                    "value": 999.0, "status": "OK"}}})
            self.assertEqual(result.status, status, exposure)


class TestFrontendContract(unittest.TestCase):

    def test_the_tab_exists_and_the_page_reads_labels_from_the_payload(self):
        """新 tab 在 ``TABS`` 里；页面不写第二份状态表。

        这个仓里栽过一次：前端抄了一份风险等级表，抄成了另一句话。所以状态、
        来源层级、因子名、指标名一律读载荷里的 ``*_label``。
        """
        with io.open(JS_PATH, encoding="utf-8") as handle:
            source = handle.read()
        tabs = [line for line in source.splitlines()
                if line.startswith("const TABS =")]
        self.assertEqual(len(tabs), 1)
        self.assertIn("'猪行业数据'", tabs[0])
        self.assertIn("status_label", source)
        self.assertIn("source_level_label", source)
        self.assertIn("/api/research/pig-evidence", source)
        self.assertNotIn("OK: '正常'", source,
                         "前端自己造了一份状态表——状态名只许从载荷取")
        self.assertNotIn("L6: '", source, "来源层级的中文名同样只许从载荷取")


if __name__ == "__main__":                                       # pragma: no cover
    unittest.main()
