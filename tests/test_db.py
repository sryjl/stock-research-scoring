# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
import db
import views


class DBTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "test.db")
        self.conn = db.init_db(self.path)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _create_trade(self, watch=10.0, current=10.2, code="600000"):
        return db.create_stock(self.conn, {
            "code": code, "name": "测试", "stock_type": core.STOCK_TYPE_TRADE,
            "watch_price": watch, "current_price": current, "tags": ["烟蒂"], "notes": "",
        })

    def test_create_trade_creates_three_lots_with_correct_plan(self):
        s = self._create_trade(10.0)
        self.assertEqual(len(s["lots"]), 3)
        plans = [l["plan_price"] for l in s["lots"]]
        self.assertAlmostEqual(plans[0], 10.0)
        self.assertAlmostEqual(plans[1], 9.5)
        self.assertAlmostEqual(plans[2], 9.0)

    def test_create_hold_has_no_lots(self):
        s = db.create_stock(self.conn, {
            "code": "601318", "name": "长持", "stock_type": core.STOCK_TYPE_HOLD,
            "watch_price": 40.0, "current_price": 42.0, "tags": ["红利"], "notes": "",
        })
        self.assertEqual(s["lots"], [])

    def test_change_watch_recomputes_only_unfilled(self):
        s = self._create_trade(10.0)
        lot1, lot2, lot3 = s["lots"]
        # 成交第一笔
        db.update_lot(self.conn, lot1["id"], {
            "actual_price": 9.98, "quantity": 100,
            "buy_date": "2026-09-01", "status": "已买入",
        })
        # 修改观察价 10 → 9
        s2 = db.update_stock(self.conn, s["id"], {"watch_price": 9.0})
        lots = {l["tranche"]: l for l in s2["lots"]}

        # 已成交的第一笔：计划价冻结为 10.0，成交价/数量/日期均不变
        self.assertAlmostEqual(lots[1]["plan_price"], 10.0)
        self.assertAlmostEqual(lots[1]["actual_price"], 9.98)
        self.assertEqual(lots[1]["quantity"], 100)
        self.assertEqual(lots[1]["buy_date"], "2026-09-01")

        # 未成交的第二、三笔按新观察价重算
        self.assertAlmostEqual(lots[2]["plan_price"], 9.0 * 0.95)
        self.assertAlmostEqual(lots[3]["plan_price"], 9.0 * 0.90)

    def test_tiers_always_from_original_base(self):
        s = self._create_trade(10.0)
        v = views.stock_view(s)
        self.assertAlmostEqual(v["tiers"][0]["value"], 10.0)
        self.assertAlmostEqual(v["tiers"][1]["value"], 9.5)
        self.assertAlmostEqual(v["tiers"][2]["value"], 9.0)
        self.assertAlmostEqual(v["up_5"], 10.5)
        self.assertAlmostEqual(v["up_10"], 11.0)

    def test_per_lot_targets_from_actual(self):
        s = self._create_trade(10.0)
        lot2 = s["lots"][1]
        db.update_lot(self.conn, lot2["id"], {"actual_price": 9.50, "status": "已买入"})
        s2 = db.get_stock(self.conn, s["id"])
        v = views.stock_view(s2)
        lot2v = next(l for l in v["lots"] if l["tranche"] == 2)
        self.assertAlmostEqual(lot2v["target_5"], 9.5 * 1.05, places=3)

    def test_persistence_across_reconnect(self):
        s = self._create_trade(12.0)
        self.conn.close()
        conn2 = db.connect(self.path)
        try:
            s2 = db.get_stock(conn2, s["id"])
            self.assertIsNotNone(s2)
            self.assertEqual(s2["name"], "测试")
            self.assertEqual(len(s2["lots"]), 3)
        finally:
            conn2.close()

    def test_sell_auto_archive(self):
        s = self._create_trade(10.0)
        lot1 = s["lots"][0]
        db.update_lot(self.conn, lot1["id"], {
            "actual_price": 10.0, "quantity": 300,
            "buy_date": "2026-09-01", "status": "已买入",
        })
        db.add_sell_record(self.conn, lot1["id"], {
            "sell_price": 10.5, "quantity": 300, "sell_date": "2026-09-02",
        })
        lot1v = db.get_lot(self.conn, lot1["id"])
        # 全部卖出 → 归档（重置为空白）
        self.assertIsNone(lot1v["actual_price"])
        self.assertIsNone(lot1v["quantity"])
        self.assertEqual(lot1v["status"], "未触发")
        # 计划价保留
        self.assertAlmostEqual(lot1v["plan_price"], 10.0)
        # 卖出记录仍在（含成本快照）
        sells = db.list_sell_records(self.conn, lot1["id"])
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0]["buy_price"], 10.0)

    def test_partial_sell_sets_status(self):
        s = self._create_trade(10.0)
        lot1 = s["lots"][0]
        db.update_lot(self.conn, lot1["id"], {"actual_price": 10.0, "quantity": 300, "status": "已买入"})
        db.add_sell_record(self.conn, lot1["id"], {
            "sell_price": 10.5, "quantity": 100, "sell_date": "2026-09-02",
        })
        lot1v = db.get_lot(self.conn, lot1["id"])
        self.assertEqual(lot1v["status"], "部分卖出")
        self.assertAlmostEqual(lot1v["actual_price"], 10.0)

    def test_unrealized_pnl(self):
        s = self._create_trade(10.0)
        lot1 = s["lots"][0]
        db.update_lot(self.conn, lot1["id"], {"actual_price": 10.0, "quantity": 3, "status": "已买入"})
        db.update_stock(self.conn, s["id"], {"current_price": 10.6})
        v = views.stock_view(db.get_stock(self.conn, s["id"]))
        self.assertAlmostEqual(v["lots"][0]["unrealized_pnl"], 180.0, places=2)
        self.assertAlmostEqual(v["unrealized_pnl"], 180.0, places=2)

    def test_realized_pnl_after_sell(self):
        s = self._create_trade(10.0)
        lot1 = s["lots"][0]
        db.update_lot(self.conn, lot1["id"], {"actual_price": 10.0, "quantity": 3, "status": "已买入"})
        db.add_sell_record(self.conn, lot1["id"], {
            "sell_price": 10.5, "quantity": 3, "sell_date": "2026-09-02",
        })
        v = views.stock_view(db.get_stock(self.conn, s["id"]))
        self.assertAlmostEqual(v["lots"][0]["realized_pnl"], 150.0, places=2)
        self.assertAlmostEqual(v["realized_pnl"], 150.0, places=2)

    def test_planned_buy_not_counted_in_pnl(self):
        s = self._create_trade(10.0)
        lot1 = s["lots"][0]
        db.update_lot(self.conn, lot1["id"], {"actual_price": 9.5, "quantity": 3, "status": "计划买入"})
        db.update_stock(self.conn, s["id"], {"current_price": 10.0})
        v = views.stock_view(db.get_stock(self.conn, s["id"]))
        # 计划买入 = 没买，不计浮盈
        self.assertFalse(v["lots"][0]["filled"])
        self.assertEqual(v["lots"][0]["unrealized_pnl"], 0.0)
        self.assertEqual(v["unrealized_pnl"], 0.0)

    def test_manual_plan_price_survives_watch_change(self):
        s = self._create_trade(10.0)
        lot2 = s["lots"][1]  # 自动计划价 9.5
        db.update_lot(self.conn, lot2["id"], {"plan_price": 9.0})  # 手动改成 9.0
        db.update_stock(self.conn, s["id"], {"watch_price": 8.0})
        lot2v = db.get_lot(self.conn, lot2["id"])
        # 手动改过的计划价不被观察价重算覆盖
        self.assertAlmostEqual(lot2v["plan_price"], 9.0)


if __name__ == "__main__":
    unittest.main()
