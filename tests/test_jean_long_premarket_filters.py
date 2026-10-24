import unittest
from datetime import datetime

from strategy_programs.jean_long import MomentumTrader


class JeanLongPremarketFilterTests(unittest.TestCase):
    def test_requires_at_least_five_percent_premarket_gain(self):
        bot = MomentumTrader("TEST", "LONG")
        bot.set_context({
            "premarket_change_pct": 4.9,
            "avg_volume_30d": 100000,
            "float_shares": 10000000,
            "news": [{
                "published_at": datetime(2026, 10, 23, 7, 30),
                "headline": "Company reports quarterly earnings beat",
            }],
        })
        bot.day_volume = 500000
        reasons = bot._scanner_filters(5.0, datetime(2026, 10, 23, 8, 0))
        self.assertTrue(any("ganancia pre-market 4.9%" in reason for reason in reasons))
        self.assertFalse(any("RVol" in reason for reason in reasons))
        self.assertFalse(any("float" in reason for reason in reasons))
        self.assertFalse(any("precio" in reason for reason in reasons))

    def test_explicit_premarket_gain_and_fresh_catalyst_are_accepted(self):
        bot = MomentumTrader("TEST", "LONG")
        bot.set_context({
            "premarket_change_pct": 12.5,
            "prev_close": 10.0,
            "avg_volume_30d": 100000,
            "float_shares": 10000000,
            "news": [{
                "published_at": datetime(2026, 10, 23, 7, 30),
                "headline": "FDA approves company's new treatment",
            }],
        })
        bot.day_volume = 500000
        reasons = bot._scanner_filters(10.1, datetime(2026, 10, 23, 8, 0))
        self.assertEqual(reasons, [])
        self.assertEqual(bot.last_metrics["premarket_gain_pct"], 12.5)
        self.assertTrue(bot.last_metrics["gain_ideal"])
        self.assertEqual(bot.last_metrics["noticia"]["categoria"], "fda")


    def test_rvol_uses_thirty_day_daily_average_before_premarket_average(self):
        bot = MomentumTrader("TEST", "LONG")
        bot.set_context({
            "premarket_change_pct": 8.0,
            "avg_volume_30d": 100000,
            "avg_premarket_volume_30d": 1000000,
            "float_shares": 10000000,
            "news": [{
                "published_at": datetime(2026, 10, 23, 7, 30),
                "headline": "Company reports quarterly earnings beat",
            }],
        })
        bot.day_volume = 500000
        reasons = bot._scanner_filters(10.0, datetime(2026, 10, 23, 8, 0))
        self.assertEqual(reasons, [])
        self.assertEqual(bot.last_metrics["rvol"], 5.0)

    def test_fresh_news_without_recognized_catalyst_is_rejected(self):
        bot = MomentumTrader("TEST", "LONG")
        bot.set_context({
            "premarket_change_pct": 8.0,
            "avg_volume_30d": 100000,
            "float_shares": 10000000,
            "news": [{
                "published_at": datetime(2026, 10, 23, 7, 30),
                "headline": "Company announces a new social media account",
            }],
        })
        bot.day_volume = 500000
        reasons = bot._scanner_filters(10.0, datetime(2026, 10, 23, 8, 0))
        self.assertIn("sin noticia fresca", reasons)


if __name__ == "__main__":
    unittest.main()
