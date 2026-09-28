# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core


class TestTierPrices(unittest.TestCase):
    def test_tiers_relative_to_original_base(self):
        # 基准价 10 → [10, 9.5, 9.0]
        self.assertEqual(core.tier_prices(10.0), [10.0, 9.5, 9.0])
        # 关键规则：第三档 = P × 0.90，绝不链式（9.5 × 0.90 = 8.55 是错误的）
        self.assertAlmostEqual(core.tier_prices(10.0)[2], 10.0 * 0.90)
        self.assertNotAlmostEqual(core.tier_prices(10.0)[2], 9.5 * 0.90)

    def test_tiers_precision(self):
        p = 9.87
        tiers = core.tier_prices(p)
        self.assertAlmostEqual(tiers[0], 9.87)
        self.assertAlmostEqual(tiers[1], 9.87 * 0.95)
        self.assertAlmostEqual(tiers[2], 9.87 * 0.90)

    def test_none_base(self):
        self.assertEqual(core.tier_prices(None), [None, None, None])


class TestTargetPrices(unittest.TestCase):
    def test_per_lot_targets(self):
        t5, t10 = core.target_prices(10.00)
        self.assertAlmostEqual(t5, 10.50, places=3)
        self.assertAlmostEqual(t10, 11.00, places=3)

        t5, t10 = core.target_prices(9.50)
        self.assertAlmostEqual(t5, 9.975, places=3)
        self.assertAlmostEqual(t10, 10.45, places=3)

        t5, t10 = core.target_prices(9.00)
        self.assertAlmostEqual(t5, 9.45, places=3)
        self.assertAlmostEqual(t10, 9.90, places=3)

    def test_none_actual(self):
        self.assertEqual(core.target_prices(None), (None, None))


class TestDistance(unittest.TestCase):
    def test_distance(self):
        self.assertAlmostEqual(core.distance_pct(10.5, 10.0), 5.0)
        self.assertAlmostEqual(core.distance_pct(9.5, 10.0), -5.0)
        self.assertIsNone(core.distance_pct(10.0, 0))
        self.assertIsNone(core.distance_pct(None, 10.0))


class TestLotReturn(unittest.TestCase):
    def test_return(self):
        self.assertAlmostEqual(core.lot_return_pct(10.5, 10.0), 5.0)
        self.assertAlmostEqual(core.lot_return_pct(9.0, 10.0), -10.0)
        self.assertIsNone(core.lot_return_pct(10.0, None))


class TestTpHint(unittest.TestCase):
    def test_hints(self):
        self.assertEqual(core.tp_hint(10.6, 10.0), "达到 +5%")
        self.assertEqual(core.tp_hint(10.5, 10.0), "达到 +5%")
        self.assertIsNone(core.tp_hint(10.4, 10.0))
        self.assertIsNone(core.tp_hint(None, 10.0))
        self.assertIsNone(core.tp_hint(10.0, None))


class TestUpPrices(unittest.TestCase):
    def test_up_prices(self):
        up = core.up_prices(10.0)
        self.assertAlmostEqual(up[0], 10.5, places=3)
        self.assertAlmostEqual(up[1], 11.0, places=3)
        self.assertAlmostEqual(core.up_prices(9.5)[0], 9.975, places=3)
        self.assertEqual(core.up_prices(None), [None, None])


class TestRounding(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(core.round_half_up(9.975, 3), 9.975)
        self.assertEqual(core.round_half_up(9.975, 2), 9.98)
        self.assertEqual(core.round_half_up(1.005, 2), 1.01)
        self.assertEqual(core.round_half_up(9.5 * 0.95, 2), 9.03)


if __name__ == "__main__":
    unittest.main()
