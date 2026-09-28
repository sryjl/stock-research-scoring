# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notifier


class TestAlertRange(unittest.TestCase):
    def test_within_range(self):
        self.assertTrue(notifier.within_alert_range(10.5, 10.5))    # 正好目标
        self.assertTrue(notifier.within_alert_range(10.55, 10.5))   # +0.48%
        self.assertTrue(notifier.within_alert_range(10.45, 10.5))   # -0.48%
        self.assertTrue(notifier.within_alert_range(10.60, 10.5))   # +0.95%
        self.assertFalse(notifier.within_alert_range(10.61, 10.5))  # +1.05%
        self.assertFalse(notifier.within_alert_range(10.39, 10.5))  # -1.05%

    def test_none(self):
        self.assertFalse(notifier.within_alert_range(None, 10.5))
        self.assertFalse(notifier.within_alert_range(10.5, None))


class TestAlertForLot(unittest.TestCase):
    def _lot(self, status, actual=None, plan=None, tranche=1):
        return {"id": 1, "tranche": tranche, "status": status,
                "actual_price": actual, "plan_price": plan}

    def test_sell_alert(self):
        lot = self._lot("已买入", actual=10.0)
        self.assertEqual(notifier.alert_for_lot(lot, 10.5)[0], "sell")
        self.assertEqual(notifier.alert_for_lot(lot, 10.55)[0], "sell")
        self.assertIsNone(notifier.alert_for_lot(lot, 10.39))  # 距目标 >1%
        self.assertIsNone(notifier.alert_for_lot(lot, 10.61))  # 超过目标 >1%

    def test_buy_alert(self):
        self.assertEqual(notifier.alert_for_lot(self._lot("计划买入", plan=10.0), 10.0)[0], "buy")
        self.assertEqual(notifier.alert_for_lot(self._lot("未触发", plan=10.0), 9.95)[0], "buy")

    def test_planned_not_sell(self):
        # 计划买入的笔即使填了成交价，也按计划价触发买入提示，而非卖出提示
        r = notifier.alert_for_lot(self._lot("计划买入", actual=9.5, plan=10.0), 9.975)
        self.assertEqual(r[0], "buy")

    def test_no_alert(self):
        self.assertIsNone(notifier.alert_for_lot(self._lot("已卖出", actual=10.0), 10.5))
        self.assertIsNone(notifier.alert_for_lot(self._lot("计划买入", plan=10.0), 11.5))
        self.assertIsNone(notifier.alert_for_lot(self._lot("已买入", actual=None), 10.0))


if __name__ == "__main__":
    unittest.main()
