# -*- coding: utf-8 -*-
"""批 8：猪企核心经营数据**收敛为三项** + 首次研究的缺失补录。**全部离线。**

这一组钉的是五件「看起来没问题」的事：

1. **核心只有三项**（销售均价 / 完全成本 / 出栏量），扩展指标（PSY / MSY /
   料肉比 / 出栏均重 / 断奶仔猪成本 / 现金成本）missing **不触发**任何补录要求；
2. **人工补录不是披露值**：``is_direct_disclosure=False``、级别是人工确认（``LM``）、
   完全成本**永远落旁证口径**——于是 ``canonical_full_cost`` 一格都不多；
3. **三条校验是硬的**：期间必填且只认两种写法、取值域拦量纲错、一条不合法就
   **整批不写**（写一半会留下一个「看起来完整」的状态）；
4. **冲突不许静默覆盖**：人工值撞上自动值时，两条都在，自动行一个字不改；
5. **只弹一次不需要状态**：``pending_prompt`` 只读库、一行不写，而「首次」的
   判据（库里有没有这一行）由服务端在 analyze **之前**问一次。

全部用 ``tempfile`` 建的 research.db，不碰真库、不联网。
"""
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import db as research_db                            # noqa: E402
from research import pig_core as core                             # noqa: E402
from research import pig_cost_core as cost_core                   # noqa: E402
from research import pig_observations as obs                      # noqa: E402
from research import pig_premium as prem                          # noqa: E402
from research import pig_readings as readings                     # noqa: E402
from research import rules as RULES                               # noqa: E402
from research.industry import pig as PIG                          # noqa: E402

PIG_CODE = "002714"          # 猪企对照组的已建档成员（牧原）
OTHER_CODE = "600519"        # 不在任何猪企组里
JS_PATH = ROOT / "static" / "research.js"
SERVER_PY = ROOT / "server.py"

#: 三项的 (metric_id, 单月口径, 区间口径, scope, 单位)
#:
#: 第三项**批 9 起是商品猪口径**（``SCOPE_COMMODITY``），不是生猪合计
#: （``SCOPE_ALL``）。落格与 ``pig.METRIC_GRIDS`` 里
#: ``(M_COMMODITY_HOG_SALES_VOLUME, "annual_heads") → (SCOPE_COMMODITY, None)``
#: 逐字一致——两处不一致就是两份真相，而这一份会被人工补录真的写进库。
THREE = (("pig_sale_price", "monthly_commodity_price", "annual_commodity_price",
          PIG.SCOPE_COMMODITY, "CNY/kg"),
         ("full_cost", "FULL_COST_COMPANY_DISCLOSED",
          "FULL_COST_COMPANY_DISCLOSED", PIG.SCOPE_COMMODITY, "CNY/kg"),
         ("commodity_hog_sales_volume", "monthly_heads", "annual_heads",
          PIG.SCOPE_COMMODITY, "万头"))


def _auto(metric_id, variant, scope, unit, value, period, *,
          source_type=PIG.SRC_ANNUAL_REPORT, level=None, disclosure=True):
    """一条**自动抽取**的观测（人工补录的对照组）。"""
    return obs.Observation(
        metric_id, metric_variant=variant, subject=PIG_CODE,
        company_code=PIG_CODE, period=period, value=value, unit=unit,
        scope=scope, source_type=source_type, source_level=level,
        source_name="定期报告", document="2026半年报",
        extraction_method="local_parse", is_direct_disclosure=disclosure,
        status=obs.STATUS_OK)


def _auto_price(value, period, *, code=PIG_CODE, scope=PIG.SCOPE_COMMODITY):
    """一条**公司月度商品猪均价**——``pig_cost_core`` 的价格侧只认这个 variant。

    级别与文件不重要（价格侧不是减数），但 variant 是：``company_months`` 用
    ``metric_variant == "monthly_commodity_price"`` 筛，写错一个词就测不出东西。
    """
    return obs.Observation(
        PIG.M_PIG_SALE_PRICE, metric_variant="monthly_commodity_price",
        subject=code, company_code=code, period=period, value=value,
        unit="CNY/kg", scope=scope, source_type=PIG.SRC_MONTHLY_BULLETIN,
        source_name="月度销售简报", document="2026半年报",
        extraction_method="local_parse", is_direct_disclosure=True,
        status=obs.STATUS_OK)


def _auto_cost(value, period, *, code=PIG_CODE, variant="full_cost",
               scope=PIG.SCOPE_COMMODITY, metric_id=None, disclosure=True,
               level=None):
    """一条**公司披露**的完全成本观测——``derive`` 唯一肯拿来当减数的那种。

    ``derivation`` 里的 ``cost_variant`` 是认口径的**唯一入口**（``_variant_of``
    只解析这一个键），所以夹具必须照抽取器写的那套 ``k=v`` 串来：只手写一个
    ``metric_variant`` 而省掉 derivation，测的就不是真路径。
    """
    return obs.Observation(
        metric_id or PIG.M_FULL_COST,
        metric_variant=("FULL_COST_COMPANY_DISCLOSED" if variant == "full_cost"
                        else variant),
        subject=code, company_code=code, period=period, value=value,
        unit="CNY/kg", scope=scope, source_type=PIG.SRC_ANNUAL_REPORT,
        source_level=level, source_name="定期报告", document="2026半年报",
        extraction_method="local_parse", is_direct_disclosure=disclosure,
        status=obs.STATUS_OK,
        derivation=prem.join_derivation([("formula", "company_disclosed_cost"),
                                         ("cost_variant", variant)]))


def _auto_volume(value, period, *, code=PIG_CODE, metric_id=None,
                 scope=PIG.SCOPE_COMMODITY, variant="monthly_heads"):
    """一条**商品猪**出栏量（``metric_id`` 可换成生猪合计口径用于缺口径那一组）。"""
    return obs.Observation(
        metric_id or PIG.M_COMMODITY_HOG_SALES_VOLUME, metric_variant=variant,
        subject=code, company_code=code, period=period, value=value,
        unit="万头", scope=scope, source_type=PIG.SRC_MONTHLY_BULLETIN,
        source_name="月度销售简报", document="2026半年报",
        extraction_method="local_parse", is_direct_disclosure=True,
        status=obs.STATUS_OK)


def _all_three(conn, period="2026-08"):
    """三项都落一条自动观测。"""
    obs.append(conn, [_auto(mid, month, scope, unit, 12.0 + i, period,
                            source_type=PIG.SRC_MONTHLY_BULLETIN,
                            disclosure=False)
                      for i, (mid, month, _iv, scope, unit) in enumerate(THREE)])


def _stock_row(code, **over):
    """``research_stocks`` 一行的**全部**命名列（照 test_audit_gate 的样本）。

    ``upsert_stock`` 是 29 列的具名绑定，少一列就绑不进去——这也是为什么这里
    不手写 INSERT 的列清单。
    """
    rec = {
        "code": code, "name": "测试" + code, "board": "MAIN_SZ",
        "industry": "养殖业", "system_type": None, "user_type": None,
        "type_confidence": None, "risk_level": None, "total_score": None,
        "rule_version": None, "latest_report_period": None,
        "attr_scores_json": None, "category_scores_json": None,
        "valuation_json": None, "financial_json": None, "risk_json": None,
        "data_completeness": None,
        "first_analyzed_at": "2026-09-01 10:00:00",
        "last_updated_at": "2026-09-01 10:00:00",
        "financial_updated_at": None, "router_version": None,
        "primary_model": None, "secondary_model": None, "primary_fit": None,
        "secondary_fit": None, "route_status": None, "route_confidence": None,
        "route_coverage": None, "route_json": None,
    }
    rec.update(over)
    return rec


class PigCoreTestCase(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = research_db.init_db(self.path)
        # ``snapshot`` 里取行业名那一步走的是 ``db.get_stock``；engine 与
        # audit_job 也是读模块级 DEFAULT_PATH，指过来免得写到真库上。
        self._patch = mock.patch.object(research_db, "DEFAULT_PATH", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        # 列缓存是本机的：这一组要的是固定输入，不是这台机器碰巧有什么。
        patcher = mock.patch("research.pig_reports.inspect_cached_pig_report",
                             return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        # 观测仓建表（``append``/``load`` 自己会建，但有几条测试先删后写）。
        obs.ensure_schema(self.conn)
        # 目标股票落一行主记录（「已有记录」那一支的判据）。列清单照
        # test_audit_gate 的样本——手写 INSERT 的列名抄错就测不成东西了。
        research_db.upsert_stock(self.conn, _stock_row(PIG_CODE))

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    # ---- 夹具 ---------------------------------------------------------- #
    def _snapshot(self, code=PIG_CODE):
        return core.snapshot(self.conn, code)

    def _missing(self, code=PIG_CODE):
        return sorted(item["metric_id"] for item in
                      core.snapshot(self.conn, code)["missing"])

    def _rows(self, code=PIG_CODE):
        return obs.load(self.conn, code=code)

    def _manual_rows(self, code=PIG_CODE):
        return [row for row in self._rows(code)
                if row.extraction_method == "manual_entry"]

    def _save(self, items, code=PIG_CODE):
        return core.save(self.conn, code, items)


# --------------------------------------------------------------------------- #
# 一、是否猪企 / 触发条件（§三 / §九）
# --------------------------------------------------------------------------- #
class TestTrigger(PigCoreTestCase):

    def test_a_non_pig_company_is_never_asked(self):
        """① 非猪企 → 不弹。行业名不可得也不弹（两条判据都不命中）。"""
        self.assertFalse(core.is_pig_company(OTHER_CODE))
        self.assertFalse(core.is_pig_company(OTHER_CODE, "白酒"))
        self.assertIsNone(core.pending_prompt(self.conn, OTHER_CODE))

    def test_a_pig_company_with_all_three_is_never_asked(self):
        """② 猪企三项都有 → 不弹。"""
        _all_three(self.conn)
        self.assertEqual(self._missing(), [])
        self.assertIsNone(core.pending_prompt(self.conn, PIG_CODE))

    def test_each_missing_item_is_asked_separately(self):
        """③④⑤ 缺哪一项就只报哪一项。"""
        for index, (mid, _m, _i, _s, _u) in enumerate(THREE):
            with self.subTest(missing=mid):
                self.conn.execute("DELETE FROM pig_metric_observation")
                self.conn.commit()
                _all_three(self.conn)
                self.conn.execute(
                    "DELETE FROM pig_metric_observation WHERE metric_id=?", (mid,))
                self.conn.commit()
                self.assertEqual(self._missing(), [mid])
                payload = core.pending_prompt(self.conn, PIG_CODE)
                self.assertIsNotNone(payload)
                self.assertTrue(payload["needs_manual_input"])
                # 已获取的那两项**读数照旧在**（只读展示靠它，不靠前端再查一次）
                got = [m["metric_id"] for m in payload["metrics"] if m["obtained"]]
                self.assertEqual(len(got), 2)
                self.assertNotIn(mid, got)
                self.assertTrue(index in (0, 1, 2))

    def test_several_missing_items_are_listed_one_by_one(self):
        """⑥ 缺多项 → 逐项列出；已获取项带值、缺项带缺失措辞。"""
        _all_three(self.conn)
        self.conn.execute("DELETE FROM pig_metric_observation"
                          " WHERE metric_id IN ('full_cost','commodity_hog_sales_volume')")
        self.conn.commit()
        payload = core.pending_prompt(self.conn, PIG_CODE)
        self.assertEqual(sorted(m["metric_id"] for m in payload["missing"]),
                         ["commodity_hog_sales_volume", "full_cost"])
        obtained = {m["metric_id"]: m for m in payload["metrics"] if m["obtained"]}
        self.assertEqual(sorted(obtained), ["pig_sale_price"])
        self.assertEqual(obtained["pig_sale_price"]["value"], 12.0)
        # 缺项**不许**拿一个 0 或一个占位值充数。
        for item in payload["missing"]:
            self.assertTrue(item["label"])
            self.assertTrue(item["unit"])
        for item in payload["metrics"]:
            if not item["obtained"]:
                self.assertNotIn("value", item)
                self.assertEqual(item["missing_text"], "未获取可靠公开数据")

    def test_extension_metrics_missing_never_triggers(self):
        """㉔ 扩展指标全缺也不触发——「非三项即扩展」是定义本身。"""
        self.assertFalse(set(core.CORE_METRIC_IDS) & set(PIG.EXTENSION_METRIC_IDS))
        for mid in PIG.EXTENSION_METRIC_IDS:
            self.assertNotIn(mid, core.CORE_METRIC_INDEX)
        _all_three(self.conn)
        payload = self._snapshot()
        self.assertEqual([m["metric_id"] for m in payload["metrics"]],
                         list(core.CORE_METRIC_IDS))
        self.assertEqual(self._missing(), [])
        self.assertFalse(payload["needs_manual_input"])

    def test_the_prompt_carries_the_missing_item_and_the_period_forms(self):
        """载荷自带期间写法与三项标签——前端不写第二份词表。"""
        payload = core.pending_prompt(self.conn, PIG_CODE)
        self.assertEqual(sorted(payload["missing"][0]),
                         ["label", "metric_id", "unit"])
        self.assertTrue(payload["period_forms"])
        self.assertIn("2026-08", payload["period_forms"])
        self.assertEqual([m["label"] for m in payload["metrics"]],
                         [PIG.METRIC_INDEX[m].display_name
                          for m in core.CORE_METRIC_IDS])


# --------------------------------------------------------------------------- #
# 一之二、卡片显示哪一期（§十一「优先展示三项」）
# --------------------------------------------------------------------------- #
class TestShowsTheNewest(PigCoreTestCase):
    """卡片上的三项**必须是最新一期**，不是最早一期。

    这里钉的是一个真实错过的 bug：``_obtained`` 用 ``_newest_first``（「降序排序
    用的键」，空期排最后）却漏了 ``reverse=True``，于是升序排出「最老一期在前」，
    002714 的核心卡片显示的是 2023 年的均价。同一个键在 ``_unit_margin`` 与
    ``pig_readings._anchor`` 里都是降序用的。
    """

    def _price(self, value, period):
        obs.append(self.conn, [_auto(
            "pig_sale_price", "monthly_commodity_price", PIG.SCOPE_COMMODITY,
            "CNY/kg", value, period, source_type=PIG.SRC_MONTHLY_BULLETIN,
            disclosure=False)])

    def _price_item(self):
        return [m for m in self._snapshot()["metrics"]
                if m["metric_id"] == "pig_sale_price"][0]

    def test_the_newest_period_wins(self):
        for value, period in ((14.49, "2023-01~02"), (11.41, "2025-12"),
                              (10.30, "2026-08"), (12.88, "2025-09")):
            self._price(value, period)
        item = self._price_item()
        self.assertEqual(item["period"], "2026-08")
        self.assertEqual(item["value"], 10.30)

    def test_a_record_without_a_period_loses_to_one_with_a_period(self):
        """空期排**最后**：翻成升序就会把「没有期间的数」当成首选显示出去。"""
        self._price(9.99, "")
        self._price(10.30, "2026-08")
        self.assertEqual(self._price_item()["period"], "2026-08")

    def test_the_tie_is_broken_by_the_variant_declaration_order(self):
        """同期两条时按 ``variant_names`` 的顺序取，不依赖行序。"""
        obs.append(self.conn, [
            _auto("pig_sale_price", "monthly_commodity_price",
                  PIG.SCOPE_COMMODITY, "CNY/kg", 10.30, "2026-08",
                  source_type=PIG.SRC_MONTHLY_BULLETIN, disclosure=False),
            _auto("pig_sale_price", "annual_commodity_price",
                  PIG.SCOPE_COMMODITY, "CNY/kg", 13.00, "2026-08",
                  source_type=PIG.SRC_ANNUAL_REPORT, disclosure=False)])
        want = PIG.METRIC_INDEX["pig_sale_price"].variant_names[0]
        self.assertEqual(self._price_item()["metric_variant"], want)

    def test_a_disclosed_number_beats_a_newer_derived_one(self):
        """**来源级别优先于期间**：月报的披露值胜过更新一期的推算值。

        这是真库上的实际形状——牧原出栏量最近三期都是 ``derived``（749.7 @2025-07），
        而公司最后一期**披露**是 857.8 @2024-12；新希望 136.07（推算）对
        180.89（披露），差 24%。只按期间排的话，摆在「核心经营数据」上的是一个
        推算出来的合计数，正是这套系统反复在治的「看起来完全正常的数」。
        """
        obs.append(self.conn, [_auto(
            "commodity_hog_sales_volume", "monthly_heads", PIG.SCOPE_COMMODITY, "万头",
            857.8, "2024-12", source_type=PIG.SRC_MONTHLY_BULLETIN)])
        obs.append(self.conn, [obs.Observation(
            "commodity_hog_sales_volume", metric_variant="monthly_heads", subject=PIG_CODE,
            company_code=PIG_CODE, period="2025-07", value=749.7, unit="万头",
            scope=PIG.SCOPE_COMMODITY, source_type=PIG.SRC_DERIVED,
            source_name="推算", extraction_method="local_parse",
            derivation="商品猪 + 仔猪", is_estimated=True,
            is_direct_disclosure=False, status=obs.STATUS_OK)])
        item = [m for m in self._snapshot()["metrics"]
                if m["metric_id"] == "commodity_hog_sales_volume"][0]
        self.assertEqual((item["value"], item["period"]), (857.8, "2024-12"))

    def test_a_manual_entry_sits_after_the_monthly_bulletin(self):
        """人工确认级在阶梯上的位置：月报 > 人工 > 业绩说明会（§三 的裁定）。"""
        # 只有人工与业绩说明会两条 → 人工胜出
        self._save([{"metric_id": "full_cost", "value": 12.5, "period": "2026-08"}])
        obs.append(self.conn, [_auto(
            "full_cost", "FULL_COST_COMPANY_DISCLOSED", PIG.SCOPE_COMMODITY,
            "CNY/kg", 13.9, "2026-08", source_type=PIG.SRC_EARNINGS_BRIEFING,
            disclosure=False)])
        item = [m for m in self._snapshot()["metrics"]
                if m["metric_id"] == "full_cost"][0]
        self.assertEqual(item["value"], 12.5)
        self.assertEqual(item["source_level"], "LM")
        # 再来一条月报 → 月报胜出
        obs.append(self.conn, [_auto(
            "full_cost", "FULL_COST_COMPANY_DISCLOSED", PIG.SCOPE_COMMODITY,
            "CNY/kg", 11.9, "2026-08", source_type=PIG.SRC_MONTHLY_BULLETIN,
            disclosure=False)])
        item = [m for m in self._snapshot()["metrics"]
                if m["metric_id"] == "full_cost"][0]
        self.assertEqual(item["value"], 11.9)
        self.assertEqual(item["source_level"], "L2")


# --------------------------------------------------------------------------- #
# 二、保存：落格、单位、标记（§五 / §六）
# --------------------------------------------------------------------------- #
class TestSave(PigCoreTestCase):

    def test_saving_three_items_writes_three_observations(self):
        """⑦ 三条 → 三条观测，``append`` 报 (3, 0)；再存一次是 (0, 3)。"""
        out = self._save([
            {"metric_id": "pig_sale_price", "value": 13.2, "period": "2026-08"},
            {"metric_id": "full_cost", "value": 12.5, "period": "2026-08"},
            {"metric_id": "commodity_hog_sales_volume", "value": 210.5, "period": "2026-08"}])
        self.assertTrue(out["ok"], out["errors"])
        self.assertEqual((out["added"], out["duplicated"]), (3, 0))
        self.assertEqual(len(self._manual_rows()), 3)
        self.assertFalse(out["pig_core"]["needs_manual_input"])
        # 幂等：同样的三条再存一次不新增行（hash 一致 → 只刷新 fetched_at）
        again = self._save([
            {"metric_id": "pig_sale_price", "value": 13.2, "period": "2026-08"},
            {"metric_id": "full_cost", "value": 12.5, "period": "2026-08"},
            {"metric_id": "commodity_hog_sales_volume", "value": 210.5, "period": "2026-08"}])
        self.assertEqual(len(self._manual_rows()), 3)
        self.assertEqual(again["duplicated"], 3)

    def test_saving_only_part_keeps_the_rest_missing(self):
        """⑧ 只补一部分：其余保持 missing，不替它们编值。"""
        out = self._save([{"metric_id": "full_cost", "value": 12.5,
                           "period": "2026H1"}])
        self.assertTrue(out["ok"])
        self.assertEqual(self._missing(), ["commodity_hog_sales_volume", "pig_sale_price"])
        self.assertEqual(out["pig_core"]["unit_margin"]["value"], None)

    def test_the_landing_grid_is_decided_by_the_period(self):
        """落格表：单月 → 月口径；区间期 → 区间口径。完全成本两栏都是**旁证**。"""
        out = self._save([
            {"metric_id": "pig_sale_price", "value": 13.2, "period": "2026-08"},
            {"metric_id": "pig_sale_price", "value": 14.0, "period": "2025A"},
            {"metric_id": "full_cost", "value": 12.5, "period": "2026-08"},
            {"metric_id": "full_cost", "value": 13.0, "period": "2025A"},
            {"metric_id": "commodity_hog_sales_volume", "value": 210.5, "period": "2026-08"},
            {"metric_id": "commodity_hog_sales_volume", "value": 1400.0, "period": "2026Q2"}])
        self.assertTrue(out["ok"], out["errors"])
        got = {(row.metric_id, row.period): row.metric_variant
               for row in self._manual_rows()}
        self.assertEqual(got[("pig_sale_price", "2026-08")],
                         "monthly_commodity_price")
        self.assertEqual(got[("pig_sale_price", "2025A")], "annual_commodity_price")
        self.assertEqual(got[("commodity_hog_sales_volume", "2026-08")], "monthly_heads")
        self.assertEqual(got[("commodity_hog_sales_volume", "2026Q2")], "annual_heads")
        # 完全成本**永不落正身**：正身一变，canonical_full_cost 的定义就被人
        # 工录入改掉了。
        for period in ("2026-08", "2025A"):
            self.assertEqual(got[("full_cost", period)],
                             "FULL_COST_COMPANY_DISCLOSED")

    def test_units_come_from_the_metric_definition(self):
        """⑰ 单位取自 ``MetricDef.unit_of``，不写第二份单位表。"""
        self._save([
            {"metric_id": "pig_sale_price", "value": 13.2, "period": "2026-08"},
            {"metric_id": "full_cost", "value": 12.5, "period": "2026-08"},
            {"metric_id": "commodity_hog_sales_volume", "value": 210.5, "period": "2026-08"}])
        units = {row.metric_id: row.unit for row in self._manual_rows()}
        self.assertEqual(units["pig_sale_price"], "CNY/kg")
        self.assertEqual(units["full_cost"], "CNY/kg")
        self.assertEqual(units["commodity_hog_sales_volume"], "万头")
        # 三项**同格同量纲**：同一指标换口径不许换单位（换了就是两把尺子）。
        for metric in core.CORE_METRICS:
            self.assertEqual(metric.unit_for("2026-08"),
                             metric.unit_for("2025A"))
        # scope 与既有出处逐字一致：**三项都是商品猪口径**（批 9 起出栏量也
        # 从生猪合计换成商品猪，见 ``THREE`` 上面的说明）。
        scopes = {row.metric_id: row.scope for row in self._manual_rows()}
        self.assertEqual(scopes["pig_sale_price"], PIG.SCOPE_COMMODITY)
        self.assertEqual(scopes["full_cost"], PIG.SCOPE_COMMODITY)
        self.assertEqual(scopes["commodity_hog_sales_volume"], PIG.SCOPE_COMMODITY)

    def test_manual_rows_are_marked_and_pass_the_self_check(self):
        """⑱ MANUAL_VERIFIED：来源类型 / 级别 / 抽取方式 / 不是披露值 / derivation。"""
        self._save([{"metric_id": "full_cost", "value": 12.5, "period": "2026-08",
                     "source_note": "半年报业绩说明会"}])
        rows = self._manual_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.source_type, PIG.SRC_MANUAL)
        self.assertEqual(row.extraction_method, "manual_entry")
        self.assertEqual(row.source_level, "LM")
        self.assertEqual(obs.level_of(PIG.SRC_MANUAL), "LM")
        self.assertFalse(row.is_direct_disclosure)
        self.assertEqual(row.paragraph, "半年报业绩说明会")
        self.assertIn("origin=user_manual_entry", row.derivation)
        # 完全成本那条**必须**带 derive 认口径用的格子名，否则单位利润派生会
        # 报一句假话（「不是商品猪口径的完全成本」）。
        self.assertIn("cost_variant=full_cost", row.derivation)
        self.assertEqual(row.period, "2026-08")
        self.assertEqual(obs.check_errors(self._rows()), [])
        # 出处非必填，但「没填」这件事要写出来。
        self._save([{"metric_id": "commodity_hog_sales_volume", "value": 210.5,
                     "period": "2026-08"}])
        no_note = [r for r in self._manual_rows()
                   if r.metric_id == "commodity_hog_sales_volume"][0]
        self.assertTrue(no_note.source_name)
        self.assertNotEqual(no_note.source_name,
                            [r for r in self._manual_rows()
                             if r.metric_id == "full_cost"][0].source_name)

    def test_manual_cost_never_becomes_canonical(self):
        """⑲ 人工完全成本**不升级 canonical**：正身那一格仍然什么都没有。"""
        self._save([{"metric_id": "full_cost", "value": 12.5, "period": "2026-08"}])
        store = readings.load(PIG_CODE, conn=self.conn)
        variants = {record.metric_variant for record in store["records"]
                    if record.metric_id == PIG.M_FULL_COST}
        self.assertNotIn(cost_core.CANONICAL_VARIANT, variants)
        self.assertIn("FULL_COST_COMPANY_DISCLOSED", variants)
        # 旁证那一格的读数**有值**（界面上要看得见用户填的数）。
        got = [r for r in store["records"]
               if r.metric_id == PIG.M_FULL_COST
               and r.metric_variant == "FULL_COST_COMPANY_DISCLOSED"]
        self.assertEqual([r.value for r in got], [12.5])

    def test_the_level_sits_after_the_monthly_bulletin(self):
        """人工确认级排在 L2 月报**之后**：自动值仍然优先，人工值留在冲突清单里。"""
        self.assertLess(obs.LEVEL_RANK["L1"], obs.LEVEL_RANK["L2"])
        self.assertLess(obs.LEVEL_RANK["L2"], obs.LEVEL_RANK["LM"])
        self.assertLess(obs.LEVEL_RANK["LM"], obs.LEVEL_RANK["L3"])
        self.assertNotIn("LM", obs.ESTIMATED_LEVELS)
        # 两张表逐位对应（观测层的级别 ↔ 记录层的来源优先级）
        sources = [source for _level, source, _label in obs.SOURCE_LEVELS]
        self.assertEqual(set(sources), set(PIG.SOURCE_PRIORITY))
        self.assertEqual(len(obs.LEVEL_NAMES), len(PIG.SOURCE_PRIORITY))
        self.assertEqual(len(obs.LEVEL_NAMES), len(set(obs.LEVEL_NAMES)))


# --------------------------------------------------------------------------- #
# 三、校验：期间与取值（§七；一条不合法就整批不写）
# --------------------------------------------------------------------------- #
class TestValidation(PigCoreTestCase):

    def test_the_period_is_required_and_only_two_shapes_are_accepted(self):
        """⑮ 期间必填；「近期」这类词不是期间。而且**一条都不写库**。"""
        good = {"metric_id": "full_cost", "value": 12.5, "period": "2025A"}
        for bad_period in ("", "   ", "近期", "目前", "2026年6月", "26-08",
                           "2026-13", "2026M8"):
            with self.subTest(period=bad_period):
                out = self._save([dict(good, period=bad_period),
                                  {"metric_id": "commodity_hog_sales_volume", "value": 210,
                                   "period": "2026-08"}])
                self.assertFalse(out["ok"])
                self.assertTrue(out["errors"])
                self.assertIn("期间", out["errors"][0]["message"])
                self.assertEqual(out["errors"][0]["metric_id"], "full_cost")
                self.assertEqual(self._manual_rows(), [],
                                 "一条不合法就整批不写")
                self.assertEqual(out["saved"], [])

    def test_the_value_must_be_a_plausible_number_with_the_right_unit(self):
        """⑯ 量纲防错：非数 / 布尔 / 非正 / 越域都被拒，理由带单位。"""
        cases = [
            ("full_cost", "abc", "数字"), ("full_cost", None, "数字"),
            ("full_cost", True, "数字"), ("full_cost", 0, "正数"),
            ("full_cost", -1, "正数"),
            ("full_cost", 300, "元/**公斤**"),
            ("commodity_hog_sales_volume", 6000000, "万头"),
            ("commodity_hog_sales_volume", 0.0001, "万头"),
        ]
        for metric_id, value, hint in cases:
            with self.subTest(metric=metric_id, value=value):
                out = self._save([{"metric_id": metric_id, "value": value,
                                   "period": "2026-08"}])
                self.assertFalse(out["ok"], "%r 不该通过" % (value,))
                self.assertIn(hint, out["errors"][0]["message"])
                self.assertEqual(self._manual_rows(), [])
        # 域内正常值照旧能存（域不是经济判断，只是量纲防错）
        for metric_id, value in (("full_cost", 12.5), ("full_cost", 100),
                                 ("commodity_hog_sales_volume", 6000),
                                 ("commodity_hog_sales_volume", 0.001)):
            with self.subTest(ok=value):
                out = self._save([{"metric_id": metric_id, "value": value,
                                   "period": "2026-08"}])
                self.assertTrue(out["ok"], out["errors"])

    def test_only_the_three_core_metrics_are_accepted(self):
        """不是三项之一 → 拒收；重复项 → 拒收。"""
        for metric_id in ("psy", "msy", "cash_cost", "weaned_piglet_cost", ""):
            with self.subTest(metric=metric_id):
                out = self._save([{"metric_id": metric_id, "value": 1.0,
                                   "period": "2026-08"}])
                self.assertFalse(out["ok"])
                self.assertEqual(self._manual_rows(), [])
        # 同一个指标的**同一个期**出现两次 → 拒收（没有哪个是「用户想填的」）
        out = self._save([{"metric_id": "full_cost", "value": 12.5,
                           "period": "2026-08"},
                          {"metric_id": "full_cost", "value": 12.6,
                           "period": "2026-08"}])
        self.assertFalse(out["ok"])
        self.assertIn("两次", out["errors"][0]["message"])
        self.assertEqual(self._manual_rows(), [])
        # 同一个指标的两个**不同的期**是合法的一条请求：单月与区间并存
        out = self._save([{"metric_id": "full_cost", "value": 12.5,
                           "period": "2026-08"},
                          {"metric_id": "full_cost", "value": 12.6,
                           "period": "2025A"}])
        self.assertTrue(out["ok"], out["errors"])
        self.assertEqual(len(self._manual_rows()), 2)


class TestCheckErrorsRuleFour(PigCoreTestCase):
    """第 4 条自检的**口径变更**（批 8 唯一一条政策改动）。

    原文是「人工录入必须有文档出处——『手填』在用户那里是被明令禁止的」。
    批 8 新建了**官方**的人工补录入口，来源不再是强制项，所以这一条改成三件
    更准的事：必须可识别为人工、必须有**来源声明**（「没填」这件事本身要说出来）、
    必须有期间。规则数 5 → 6。
    """

    def _manual(self, **over):
        kwargs = dict(period="2026-08", value=12.5, unit="CNY/kg",
                      scope=PIG.SCOPE_COMMODITY, source_type=PIG.SRC_MANUAL,
                      source_name=core.SOURCE_NAME,
                      extraction_method="manual_entry")
        kwargs.update(over)
        item = obs.Observation(PIG.M_FULL_COST,
                               metric_variant="FULL_COST_COMPANY_DISCLOSED",
                               subject="002714", company_code="002714",
                               status=obs.STATUS_OK, **kwargs)
        return item

    def test_a_well_formed_manual_row_passes(self):
        """出处不填也自洽：来源声明是「用户人工录入（未填出处）」这句话本身。"""
        self.assertEqual(obs.check_errors([self._manual()]), [])
        no_note = self._manual(source_name=core.SOURCE_NAME_NO_SOURCE)
        self.assertEqual(obs.check_errors([no_note]), [])

    def test_a_manual_row_that_hides_itself_is_reported(self):
        """「手工录入」但来源类型不是人工确认 —— 那就是在冒充自动抽取的行。"""
        errors = obs.check_errors([self._manual(source_type=PIG.SRC_ANNUAL_REPORT)])
        self.assertEqual(len(errors), 1)
        self.assertIn("人工确认", errors[0][1])

    def test_a_manual_row_without_any_source_declaration_is_reported(self):
        """没有出处可以，**假装有出处**不可以。"""
        errors = obs.check_errors([self._manual(source_name=None)])
        self.assertEqual(len(errors), 1)
        self.assertIn("来源声明", errors[0][1])

    def test_a_manual_row_without_a_period_is_reported(self):
        """§七：禁止保存无期间的成本——没有期间会被摊到某一期上。"""
        for period in (None, "", "   "):
            with self.subTest(period=period):
                errors = obs.check_errors([self._manual(period=period)])
                self.assertEqual(len(errors), 1)
                self.assertIn("没有期间", errors[0][1])

    def test_a_periodless_manual_row_cannot_get_in_through_save(self):
        """入口这一侧也挡：无期间的成本根本写不进去（两道都硬）。"""
        out = self._save([{"metric_id": "full_cost", "value": 12.5,
                           "period": "  "}])
        self.assertFalse(out["ok"])
        self.assertEqual(self._rear_guard(), [])

    def _rear_guard(self):
        return [row for row in self._rows() if row.extraction_method == "manual_entry"]


# --------------------------------------------------------------------------- #
# 四、改与删：只动人工的行（§十四）
# --------------------------------------------------------------------------- #
class TestReplaceAndDelete(PigCoreTestCase):

    def test_an_automatic_row_cannot_be_touched_through_the_manual_entry(self):
        """⑳ 自动行既改不了也删不了：人工入口只该管人工数据。"""
        _all_three(self.conn)
        before = {row.observation_hash for row in self._rows()}
        auto_hash = sorted(before)[0]
        out = core.delete(self.conn, PIG_CODE, [auto_hash])
        self.assertFalse(out["ok"])
        self.assertIn("人工补录", out["errors"][0]["message"])
        self.assertEqual({row.observation_hash for row in self._rows()}, before)
        # 别的股票的哈希同样删不掉
        other = core.delete(self.conn, OTHER_CODE, [auto_hash])
        self.assertFalse(other["ok"])
        self.assertEqual(self._rows(OTHER_CODE), [])

    def test_a_stale_hash_is_told_apart_from_an_automatic_row(self):
        """拿**改之前**的哈希来删：理由必须是「已经不在了」，不是「这是自动数据」。

        **改一个值会换掉哈希**（``_hash`` 含值），所以旧哈希失效是很容易发生的事
        （两个标签页、或者上一次的回执还捏在手里）。浏览器实测里它原本被报成
        「不是这只股票的人工补录（人工入口不能改自动抽取的数据）」——等于系统在
        指控用户想改自动数据，而库里那条人工行早就被替换掉了。
        """
        self._save([{"metric_id": "full_cost", "value": 12.5,
                     "period": "2026-08"}])
        stale = self._manual_rows()[0].observation_hash
        self._save([{"metric_id": "full_cost", "value": 12.9,
                     "period": "2026-08"}], )
        self.assertNotEqual(self._manual_rows()[0].observation_hash, stale)
        out = core.delete(self.conn, PIG_CODE, [stale])
        self.assertFalse(out["ok"])
        self.assertIn("已经不在库里", out["errors"][0]["message"])
        self.assertNotIn("自动", out["errors"][0]["message"])

    def test_a_manual_row_can_be_corrected(self):
        """㉑ 改：同一 (指标, 口径, 期间) 先 forget 再 append → 一条、值更新。

        不这样做的话 append-only 会把「用户改自己填的数」变成同级别冲突
        （两条都在 → 不给值），用户改完反而看不见值了。
        """
        self._save([{"metric_id": "full_cost", "value": 12.5,
                     "period": "2026-08"}])
        out = self._save([{"metric_id": "full_cost", "value": 11.9,
                           "period": "2026-08"}])
        self.assertTrue(out["ok"])
        self.assertEqual([item["value"] for item in out["replaced"]], [12.5])
        rows = self._manual_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].value, 11.9)
        self.assertFalse(rows[0].status == obs.STATUS_CONFLICT)
        # 换一个期间是**另一条**，不是替换
        out = self._save([{"metric_id": "full_cost", "value": 13.0,
                           "period": "2025A"}])
        self.assertEqual(out["replaced"], [])
        self.assertEqual(len(self._manual_rows()), 2)

    def test_a_manual_row_can_be_deleted(self):
        """㉑ 删：只删得掉人工的那一条，其余原样。"""
        self._save([{"metric_id": "full_cost", "value": 12.5,
                     "period": "2026-08"},
                    {"metric_id": "commodity_hog_sales_volume", "value": 210.5,
                     "period": "2026-08"}])
        _all_three(self.conn)     # 自动行：值 12.0/13.0/14.0
        target = [row for row in self._manual_rows()
                  if row.metric_id == "full_cost"][0]
        out = core.delete(self.conn, PIG_CODE, [target.observation_hash])
        self.assertTrue(out["ok"])
        self.assertEqual(out["deleted"], [target.observation_hash])
        left = self._manual_rows()
        self.assertEqual([row.metric_id for row in left], ["commodity_hog_sales_volume"])
        # 没带哈希 / 空哈希：什么都不删
        self.assertFalse(core.delete(self.conn, PIG_CODE, [])["ok"])
        self.assertEqual(len(self._manual_rows()), 1)

    def test_a_conflicting_manual_value_does_not_overwrite_the_automatic_one(self):
        """㉒ 自动值与人工值冲突**不静默覆盖**：两条都在，自动行一字不改。"""
        auto = _auto(PIG.M_FULL_COST, "FULL_COST_COMPANY_DISCLOSED",
                     PIG.SCOPE_COMMODITY, "CNY/kg", 11.7, "2026-08")
        obs.append(self.conn, [auto])
        before = {(row.observation_hash, row.value, row.source_level)
                  for row in self._rows()}
        out = self._save([{"metric_id": "full_cost", "value": 12.0,
                           "period": "2026-08", "source_note": "说明会"}])
        self.assertTrue(out["ok"])
        after = {(row.observation_hash, row.value, row.source_level)
                 for row in self._rows()}
        self.assertTrue(before <= after, "自动观测被改掉了")
        self.assertEqual(len(after) - len(before), 1)
        # 层级更高的自动值仍然占优：读数是 11.7，不是 12.0
        store = readings.load(PIG_CODE, conn=self.conn)
        got = [r for r in store["records"]
               if r.metric_id == PIG.M_FULL_COST
               and r.metric_variant == "FULL_COST_COMPANY_DISCLOSED"]
        self.assertEqual([r.value for r in got], [11.7])
        # 冲突**说出口**：载荷上有人工条目与它的冲突说明
        entries = out["pig_core"]["manual_entries"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["conflict_note"])
        self.assertIn("没有覆盖", entries[0]["conflict_note"])
        self.assertIn("L1", entries[0]["conflict_note"])

    def test_the_unit_margin_is_missing_when_the_periods_do_not_match(self):
        """㉓ 期间不匹配 → 单位利润 missing，理由**逐字**来自批 7 的派生。"""
        self._save([{"metric_id": "pig_sale_price", "value": 13.2,
                     "period": "2026-08"},
                    {"metric_id": "full_cost", "value": 12.5,
                     "period": "2025A"}])
        snap = self._snapshot()
        um = snap["unit_margin"]
        self.assertIsNone(um["value"])
        self.assertTrue(um["reason"])
        self.assertIn("区间期", um["reason"])
        report = cost_core.derive(self.conn, PIG_CODE)
        self.assertIn(report["skipped"][0]["why"], um["reason"])
        self.assertTrue(um["label"] and um["formula"])

    def test_the_unit_margin_needs_a_direct_disclosure_on_both_sides(self):
        """人工成本**不是**直接披露 → 派生不出单位利润，理由是这一条。"""
        self._save([{"metric_id": "pig_sale_price", "value": 13.2,
                     "period": "2026-08"},
                    {"metric_id": "full_cost", "value": 12.5,
                     "period": "2026-08"}])
        um = self._snapshot()["unit_margin"]
        self.assertIsNone(um["value"])
        self.assertIn("直接披露", um["reason"])


# --------------------------------------------------------------------------- #
# 四之二、每头利润 / 估算总利润（批 9 §三；纯研究，不进评分）
# --------------------------------------------------------------------------- #
class TestEstimatedProfit(PigCoreTestCase):
    """``estimated_profit`` 是**元/kg 与头之间那道桥**的唯一落点。

    单位利润是元/**公斤**，商品猪出栏量是**头**——两个量纲本来接不上，硬乘出来
    的数在界面上与正确的那个长得一模一样。所以这一组钉的是桥上每一根栏杆：

    1. 同期 → 有值，且**量纲在测试里独立重算一遍**再逐位比；
    2. 两期不等 → missing，理由里**两个期间都在**（不是「数据不足」四个字）；
    3. 只有生猪合计口径 → missing，且理由**说清为什么不拿它顶**；
    4. 120kg 取自 ``RULES_V1``：改常量、数跟着变——写死在函数里就变不了。
    """

    #: 真库 002714 2026-06 的那一对：均价 9.69 − 完全成本 11.7 = −2.01 元/kg，
    #: 商品猪销量 622.7 万头。
    PRICE, COST, MARGIN, VOLUME = 9.69, 11.7, -2.01, 622.7

    def _pair(self):
        """成本与售价配成**同期同口径**的一对 → 单位利润有值。"""
        obs.append(self.conn, [_auto_price(self.PRICE, "2026-06"),
                               _auto_cost(self.COST, "2026-06")])
        self.assertEqual(self._snapshot()["unit_margin"]["value"], self.MARGIN)

    def test_the_same_period_gives_a_value_with_an_explicit_unit_chain(self):
        """① 同期 → 有值；量纲**独立重算**，不照抄实现里的算式。"""
        self._pair()
        obs.append(self.conn, [_auto_volume(self.VOLUME, "2026-06")])
        ep = self._snapshot()["estimated_profit"]
        self.assertEqual(ep["period"], "2026-06")
        self.assertEqual(ep["volume"], self.VOLUME)
        self.assertEqual(ep["per_head"], -241.2)
        self.assertEqual(ep["value"], -15.02)
        # 元/kg × kg/头 = 元/头；万头 × 1e4 = 头；元 ÷ 1e8 = 亿元
        per_head = self.MARGIN * RULES.RULES_V1["pig"]["commodity_hog_weight_kg"]
        total_yi = per_head * self.VOLUME * 10000.0 / 1e8
        self.assertEqual(ep["per_head"], round(per_head, 2))
        self.assertEqual(ep["value"], round(total_yi, 2))
        self.assertIsNone(ep["reason"])
        # 换算每一步都留痕：链子中间那三个数要能在载荷上读到（否则界面上只能
        # 看到一个 −15.02，没法核对）。
        self.assertIn("per_head=-241.2000", ep["derivation"])
        self.assertIn("value_yuan=-1501952400.0000", ep["derivation"])
        self.assertIn("value_yi=-15.019524", ep["derivation"])

    def test_two_different_periods_are_never_multiplied(self):
        """② 出栏量是 2026-08、单位利润是 2026-06 → missing，两个期间都要说出口。"""
        self._pair()
        obs.append(self.conn, [_auto_volume(self.VOLUME, "2026-08")])
        ep = self._snapshot()["estimated_profit"]
        self.assertIsNone(ep["value"])
        self.assertIsNone(ep["per_head"])
        self.assertIsNone(ep["volume"])
        self.assertIsNone(ep["period"])
        self.assertIn("2026-06", ep["reason"])
        self.assertIn("2026-08", ep["reason"])
        self.assertIn("不相乘", ep["reason"])
        # 量纲链一个字都不许留：没有算出数，就没有 derivation。
        self.assertNotIn("value_yi", ep.get("derivation") or "")

    def test_without_a_unit_margin_there_is_no_period_to_pick(self):
        """③ 没有单位利润就**没有「哪一期」**——理由要报出库里出栏量有哪几期。"""
        obs.append(self.conn, [_auto_volume(self.VOLUME, "2026-06"),
                               _auto_volume(120.61, "2026-08")])
        ep = self._snapshot()["estimated_profit"]
        self.assertIsNone(ep["value"])
        self.assertIn("缺单位利润", ep["reason"])
        self.assertIn("2026-06", ep["reason"])
        self.assertIn("2026-08", ep["reason"])

    def test_the_live_hog_total_is_never_used_as_a_stand_in(self):
        """④ 只有生猪合计口径 → 仍 missing，且**说清为什么不拿它顶**。

        合计含仔猪与种猪（仔猪只有十几公斤），拿它乘出来的总利润会平白变大。
        这一句必须写在载荷上：下一次有人看到这格是空的，最顺手的一步就是
        把合计口径接上来。
        """
        self._pair()
        obs.append(self.conn, [_auto_volume(800.0, "2026-06",
                                            metric_id=PIG.M_HOG_SALES_VOLUME,
                                            scope=PIG.SCOPE_ALL)])
        ep = self._snapshot()["estimated_profit"]
        self.assertIsNone(ep["value"])
        self.assertIsNone(ep["volume"])
        self.assertIn("也不拿生猪合计口径顶", ep["reason"])

    def test_a_wrong_scope_row_is_not_used_even_with_the_right_metric_id(self):
        """⑤ 指标 id 对、**scope 错**的那一行不许被 120kg 乘进去。

        ``_obtained`` 只看指标 / 状态 / 值、**不看 scope**；库里若有一条挂了
        商品猪 id、scope 却是生猪合计的错行，它会被择优挑中，然后被乘成一个
        看起来完全正常的总利润——比缺一个数糟得多。
        """
        self._pair()
        obs.append(self.conn, [_auto_volume(self.VOLUME, "2026-06",
                                            scope=PIG.SCOPE_ALL)])
        ep = self._snapshot()["estimated_profit"]
        self.assertIsNone(ep["value"])
        self.assertIsNone(ep["volume"])

    def test_without_the_weight_nothing_is_computed(self):
        """⑥ 常量没配出来就**不算**：不给一个「用了哪个系数只有上帝知道」的数。"""
        self._pair()
        obs.append(self.conn, [_auto_volume(self.VOLUME, "2026-06")])
        cfg = dict(RULES.RULES_V1["pig"])
        cfg.pop("commodity_hog_weight_kg")
        ep = core.snapshot(self.conn, PIG_CODE, cfg=cfg)["estimated_profit"]
        self.assertIsNone(ep["value"])
        self.assertIsNone(ep["weight_kg"])
        self.assertIn("commodity_hog_weight_kg", ep["reason"])

    def test_the_weight_comes_from_the_rules_and_the_wording_from_the_payload(self):
        """⑦ 120kg 取自 ``RULES_V1``；公式与标签**全在载荷里**（前端不写第二份）。"""
        self._pair()
        obs.append(self.conn, [_auto_volume(self.VOLUME, "2026-06")])
        cfg = dict(RULES.RULES_V1["pig"])
        self.assertEqual(cfg["commodity_hog_weight_kg"], 120.0)
        ep = core.snapshot(self.conn, PIG_CODE, cfg=cfg)["estimated_profit"]
        self.assertEqual(ep["weight_kg"], 120.0)
        for key in ("label", "unit", "formula", "missing_text", "per_head_label",
                    "per_head_unit", "volume_label", "volume_unit", "weight_kg"):
            self.assertTrue(ep.get(key), key)
        self.assertIn("120", ep["formula"])
        # 改常量 → 数跟着变。写死在函数里（或者在前端复制一个 120）就变不了。
        cfg["commodity_hog_weight_kg"] = 100.0
        ep100 = core.snapshot(self.conn, PIG_CODE, cfg=cfg)["estimated_profit"]
        self.assertEqual(ep100["weight_kg"], 100.0)
        self.assertEqual(ep100["per_head"], round(self.MARGIN * 100.0, 2))
        self.assertNotEqual(ep100["value"], ep["value"])

    def test_the_estimated_profit_never_enters_the_scoring_path(self):
        """⑧ 它不进评分：载荷在 ``pig_core`` 键下，分数那一层看不见它。"""
        self._pair()
        obs.append(self.conn, [_auto_volume(self.VOLUME, "2026-06")])
        snap = self._snapshot()
        self.assertIn("estimated_profit", snap)
        self.assertNotIn("estimated_profit", core.CORE_METRIC_INDEX)
        self.assertNotIn("estimated_profit", PIG.FACTOR_METRICS)


# --------------------------------------------------------------------------- #
# 五、「只弹一次」不需要状态（§九 / §十）
# --------------------------------------------------------------------------- #
class TestOnlyOnce(PigCoreTestCase):

    def test_the_prompt_is_a_read_only_derivation_of_the_database(self):
        """⑫ 只弹一次不靠状态：问一次不改库（重启之后答案还是同一个）。

        没有 ``needs_pig_manual_input`` 这一列，也没有任何「弹过没」的表——
        这一条同时钉住了「不新增状态机」与「重启不再弹」。
        """
        columns = {row[1] for row in
                   self.conn.execute("PRAGMA table_info(research_stocks)")}
        for name in columns:
            self.assertIsNone(re.search(r"manual|prompt|asked|pig_input", name),
                              "「弹过没」被做成了列：%s" % name)
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for name in tables:
            self.assertIsNone(re.search(r"manual_pig|manual_cost|manual_metric",
                                        name),
                              "出现了平行的人工数据表：%s" % name)
        before = (len(self._rows()), self._snapshot_code_row())
        first = core.pending_prompt(self.conn, PIG_CODE)
        second = core.pending_prompt(self.conn, PIG_CODE)
        self.assertEqual(first["missing"], second["missing"])
        self.assertEqual(before, (len(self._rows()), self._snapshot_code_row()))

    def _snapshot_code_row(self):
        row = research_db.get_stock(self.conn, PIG_CODE)
        return (row["code"], row["industry"], row["total_score"])

    def test_the_question_is_asked_before_analyze_writes_the_row(self):
        """⑪ 刷新不再弹：问在 analyze **之前**，而 analyze 会把行落下来。

        判据是「库里没有这一行」——落库之后**永久为假**。这里把它变成两句话
        的源码断言：``_pig_core_prompt`` 里先问 ``get_stock``，且 analyze 路由
        先问 prompt 再调 ``analyze``。
        """
        server = SERVER_PY.read_text(encoding="utf-8")
        self.assertIn("research_db.get_stock(pconn, code) is not None", server)
        route = server.split('path == "/api/research/analyze"')[1]
        self.assertLess(route.index("self._pig_core_prompt(code)"),
                        route.index("research_engine.analyze("),
                        "问在 analyze 之后就不是「首次」了")
        self.assertIn("pig_core_prompt", route)
        # 落一行 = 不是首次。落库之后同一个 code 不再是「首次」。
        self.assertIsNotNone(research_db.get_stock(self.conn, PIG_CODE))
        self.assertIsNone(research_db.get_stock(self.conn, OTHER_CODE))

    def test_skipping_still_leaves_the_stock_in_the_library(self):
        """⑩ 「暂不填写」不发请求：股票照常在研究库里，缺项照旧如实标出。"""
        self.assertIsNotNone(core.pending_prompt(self.conn, PIG_CODE))
        self.assertEqual(self._manual_rows(), [])
        self.assertIsNotNone(research_db.get_stock(self.conn, PIG_CODE))
        self.assertEqual(len(self._snapshot()["missing"]), 3)
        js = JS_PATH.read_text(encoding="utf-8")
        # 「暂不填写」在页面上**只出现一次**，而且那一次是一颗只关闭弹窗的按钮；
        # 关掉弹窗的函数体里没有任何请求——跳过就是跳过，不发任何东西。
        lines = [line for line in js.splitlines() if ">暂不填写<" in line]
        self.assertEqual(len(lines), 1, "「暂不填写」被写成了两颗按钮/两处文案")
        self.assertIn("data-pig-core-close", lines[0])
        closer = js.split("function closePigCoreDialog()")[1].split("\n}")[0]
        self.assertNotIn("api(", closer, "「暂不填写」不许发请求")
        self.assertNotIn("fetch(", closer)

    def test_an_existing_stock_can_be_filled_in_by_hand(self):
        """⑬⑭ 已有猪企不自动弹，但快照仍算得出缺项，且随时可以手动补。"""
        self.assertIsNotNone(research_db.get_stock(self.conn, PIG_CODE))
        snap = self._snapshot()
        self.assertEqual(len(snap["missing"]), 3)
        self.assertTrue(snap["needs_manual_input"])
        self.assertTrue(snap["is_pig_company"])
        out = self._save([{"metric_id": "commodity_hog_sales_volume", "value": 210.5,
                           "period": "2026-08"}])
        self.assertTrue(out["ok"])
        self.assertEqual(self._missing(), ["full_cost", "pig_sale_price"])
        # 手动补的一条可以改也可以删（§十的入口需要这两件事）
        entry = out["pig_core"]["manual_entries"][0]
        self.assertEqual(entry["metric_label"],
                         PIG.METRIC_INDEX[PIG.M_COMMODITY_HOG_SALES_VOLUME].display_name)
        self.assertTrue(core.delete(self.conn, PIG_CODE,
                                    [entry["observation_hash"]])["ok"])
        self.assertEqual(self._missing(),
                         ["commodity_hog_sales_volume", "full_cost", "pig_sale_price"])

    def test_a_non_pig_company_has_no_entry_point_in_the_page(self):
        """非猪企：载荷里没有补录入口（按钮由 ``is_pig_company`` 决定）。"""
        snap = core.snapshot(self.conn, OTHER_CODE)
        self.assertFalse(snap["is_pig_company"])
        self.assertFalse(snap["needs_manual_input"])
        self.assertEqual(snap["manual_entries"], [])

    def test_the_margin_reason_is_shown_as_plain_text(self):
        """后端理由里的 Markdown 星号与硬截断都不该出现在卡片小字上（实测缺陷）。

        实测形状：新希望的卡片小字是「…是 weaned_piglet_cost / co」——一个单词被
        拦腰截断，`**` 也照原样露出来。``pigReason`` 只做显示层收尾：去星号、
        超长加省略号；**不改后端那句话、不重算任何口径**。
        """
        js = JS_PATH.read_text(encoding="utf-8")
        body = js.split("function pigReason(")[1].split("\n}")[0]
        self.assertIn(r"replace(/\*\*/g, '')", body)
        self.assertIn("plain.slice(0, limit) + '…'", body)
        self.assertNotIn("slice(0, 60)", js, "又回到硬截断了")
        # 悬停的那句话不截断（limit 传 0）
        self.assertIn("pigReason(um.reason, 0)", js)


if __name__ == "__main__":
    unittest.main()
