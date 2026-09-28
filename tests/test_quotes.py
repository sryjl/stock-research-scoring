# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quotes


class TestMarketPrefix(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(quotes.market_prefix("600741"), "sh")
        self.assertEqual(quotes.market_prefix("688981"), "sh")
        self.assertEqual(quotes.market_prefix("000002"), "sz")
        self.assertEqual(quotes.market_prefix("300750"), "sz")
        self.assertEqual(quotes.market_prefix("830799"), "bj")
        self.assertEqual(quotes.full_symbol("600741"), "sh600741")
        self.assertEqual(quotes.full_symbol("000002"), "sz000002")


class TestTradingTime(unittest.TestCase):
    def test_trading_windows(self):
        # 2026-09-14 是周一
        self.assertTrue(quotes.is_trading_time(datetime(2026, 9, 14, 9, 30)))
        self.assertTrue(quotes.is_trading_time(datetime(2026, 9, 14, 10, 0)))
        self.assertTrue(quotes.is_trading_time(datetime(2026, 9, 14, 11, 30)))
        self.assertTrue(quotes.is_trading_time(datetime(2026, 9, 14, 13, 0)))
        self.assertTrue(quotes.is_trading_time(datetime(2026, 9, 14, 15, 0)))

    def test_non_trading_windows(self):
        self.assertFalse(quotes.is_trading_time(datetime(2026, 9, 14, 9, 0)))     # 开盘前
        self.assertFalse(quotes.is_trading_time(datetime(2026, 9, 14, 12, 0)))    # 午间休市
        self.assertFalse(quotes.is_trading_time(datetime(2026, 9, 14, 15, 1)))    # 收盘后
        self.assertFalse(quotes.is_trading_time(datetime(2026, 9, 19, 10, 0)))    # 周六
        self.assertFalse(quotes.is_trading_time(datetime(2026, 9, 20, 10, 0)))    # 周日


class TestParseTencentQuote(unittest.TestCase):
    SAMPLE = 'v_sh600741="1~华域汽车~600741~14.98~15.18~15.18~126199~48489~77706~";'

    def test_parse(self):
        q = quotes.parse_tencent_quote(self.SAMPLE, "sh600741")
        self.assertIsNotNone(q)
        self.assertEqual(q["name"], "华域汽车")
        self.assertAlmostEqual(q["price"], 14.98)

    def test_parse_missing(self):
        self.assertIsNone(quotes.parse_tencent_quote(self.SAMPLE, "sz000002"))
        self.assertIsNone(quotes.parse_tencent_quote("", "sh600741"))

    def test_fetch_empty(self):
        self.assertEqual(quotes.fetch_quotes([]), {})


class TestUnescape(unittest.TestCase):
    def test_unescape(self):
        self.assertEqual(quotes._unescape_unicode("\\u4e2d\\u56fd"), "中国")
        self.assertEqual(quotes._unescape_unicode("abc"), "abc")


class TestSearchParse(unittest.TestCase):
    SAMPLE = (
        'v_hint="sh~600741~\\u534e\\u57df\\u6c7d\\u8f66~hyqc~GP-A^'
        'sz~000002~\\u4e07\\u79d1A~wka~GP-A^'
        'hk~01833~\\u5e73\\u5b89\\u597d\\u533b\\u751f~pahys~GP^'
        'sz~180201~\\u5e73\\u5b89\\u5e7f\\u5ddeREIT~x~FJ"'
    )

    def test_parse_filters(self):
        r = quotes.parse_search_result(self.SAMPLE)
        codes = [x["code"] for x in r]
        self.assertEqual(codes, ["600741", "000002"])  # 过滤港股与 REIT
        by = {x["code"]: x for x in r}
        self.assertEqual(by["600741"]["name"], "华域汽车")
        self.assertEqual(by["000002"]["name"], "万科A")
        self.assertEqual(by["600741"]["market"], "sh")


if __name__ == "__main__":
    unittest.main()
