from datetime import datetime
import unittest

from strategy_programs.jean_long import MomentumTrader


class JeanLongPremarketFilterTests(unittest.TestCase):
    def make_bot(self, float_shares=25_000_000, volume=499_999):
        bot = MomentumTrader("TEST", "LONG")
        bot.set_context({"float_shares": float_shares})
        bot.day_volume = volume
        return bot

    def test_gap_relative_volume_and_news_are_not_required(self):
        bot = self.make_bot()
        reasons = bot._scanner_filters(5.0, datetime(2026, 10, 23, 8, 0))
        self.assertEqual(reasons, [])
        self.assertNotIn("premarket_gain_pct", bot.last_metrics)
        self.assertNotIn("rvol", bot.last_metrics)
        self.assertNotIn("noticia", bot.last_metrics)

    def test_float_below_fifty_million_is_accepted(self):
        bot = self.make_bot(float_shares=49_999_999)
        self.assertEqual(bot._scanner_filters(5.0, datetime(2026, 10, 23, 8, 0)), [])

    def test_float_at_fifty_million_is_rejected(self):
        bot = self.make_bot(float_shares=50_000_000)
        reasons = bot._scanner_filters(5.0, datetime(2026, 10, 23, 8, 0))
        self.assertTrue(any("float >= 50,000,000" in reason for reason in reasons))

    def test_volume_below_five_hundred_thousand_is_accepted(self):
        bot = self.make_bot(volume=499_999)
        self.assertEqual(bot._scanner_filters(5.0, datetime(2026, 10, 23, 8, 0)), [])

    def test_volume_at_five_hundred_thousand_is_rejected(self):
        bot = self.make_bot(volume=500_000)
        reasons = bot._scanner_filters(5.0, datetime(2026, 10, 23, 8, 0))
        self.assertTrue(any("volumen 500,000" in reason for reason in reasons))

    def test_volume_missing_is_rejected_because_limit_cannot_be_verified(self):
        bot = self.make_bot(volume=None)
        reasons = bot._scanner_filters(5.0, datetime(2026, 10, 23, 8, 0))
        self.assertTrue(any("sin volumen de hoy" in reason for reason in reasons))

    def test_price_range_remains_one_to_twenty_dollars(self):
        bot = self.make_bot()
        reasons = bot._scanner_filters(20.01, datetime(2026, 10, 23, 8, 0))
        self.assertTrue(any("precio fuera" in reason for reason in reasons))

    def test_waiting_pattern_keeps_new_scanner_blockers_visible(self):
        bot = MomentumTrader("TEST", "LONG")
        bot.set_context({})
        bot.c = {
            "open": 5.0,
            "first_move": None,
            "adv_dip": 0.0,
            "entered_here": False,
            "pattern_inverted": False,
        }
        actions = bot._try_enter(5.0, 25, datetime(2026, 10, 23, 8, 0), 0.0)
        self.assertEqual(actions, [])
        self.assertIn("fuera del tramo de entrada (0-20 s)", bot.last_wait_reason)
        self.assertTrue(any("sin volumen de hoy" in reason for reason in bot.last_reject))
        self.assertTrue(any("float desconocido" in reason for reason in bot.last_reject))


if __name__ == "__main__":
    unittest.main()
