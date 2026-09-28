import unittest
from unittest.mock import patch

from research.pig_shadow import replay_pig_case


URL = "https://static.cninfo.com.cn/finalpage/2026-01-08/example.PDF"


class PigShadowTests(unittest.TestCase):
    def test_blocks_announcement_lookahead_without_fetching(self):
        with patch("research.pig_shadow.inspect_official_sales_bulletin") as fetch:
            result = replay_pig_case("001201", 2025, URL, as_of_date="2025-12-31")
            self.assertEqual(result["status"], "lookahead_blocked")
            fetch.assert_not_called()

    def test_blocks_annual_report_lookahead_without_fetching(self):
        annual = {"report_period": "2025A", "publish_date": "2026-04-28",
                  "status": "extracted"}
        with patch("research.pig_shadow.inspect_cached_pig_report", return_value=[annual]):
            with patch("research.pig_shadow.inspect_official_sales_bulletin") as fetch:
                result = replay_pig_case("001201", 2025, URL,
                                         as_of_date="2026-02-01")
                self.assertEqual(result["status"], "lookahead_blocked")
                fetch.assert_not_called()

    def test_missing_annual_does_not_fetch_or_score(self):
        with patch("research.pig_shadow.inspect_cached_pig_report", return_value=[]):
            with patch("research.pig_shadow.inspect_official_sales_bulletin") as fetch:
                result = replay_pig_case("001201", 2025, URL,
                                         as_of_date="2026-09-25")
                self.assertEqual(result["status"], "annual_report_not_cached")
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
