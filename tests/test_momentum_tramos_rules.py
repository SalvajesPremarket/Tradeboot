import unittest
from datetime import datetime
from unittest.mock import patch

from momentum_tramos import MomentumTrader


class MomentumTramosRulesTests(unittest.TestCase):
    def test_long_entry_rejects_ema_between_ema20_and_upper_bollinger(self):
        bot = MomentumTrader("TEST", "LONG", {"use_scanner_filters": False})
        history = [{"open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0} for _ in range(220)]
        bot.seed(history)
        bot.c = {"open": 2.0}
        bot.streak = 0
        bot.ema[20].v = 1.8
        bot.ema[50].v = 1.9
        bot.ema[200].v = 1.7
        bot.m_fast.v, bot.m_slow.v = 2.1, 1.8
        with patch.object(bot, "_bands", return_value=(2.2, 1.5)):
            reasons = bot._entry_filters(2.0, datetime(2026, 10, 23, 10, 0))
        self.assertIn("EMA50 interpuesta entre EMA20 y Bollinger superior", reasons)
        self.assertNotIn("EMA200 interpuesta entre EMA20 y Bollinger superior", reasons)

    def test_no_new_entries_at_or_after_two_pm_new_york(self):
        bot = MomentumTrader("TEST", "LONG", {"use_scanner_filters": False})
        bot.c = {
            "first_move": "adv", "adv_dip": 0.1, "pattern_inverted": False,
            "entered_here": False, "low": 1.99, "high": 2.01, "open": 2.0,
        }
        with patch.object(bot, "_entry_filters", return_value=[]):
            actions = bot._try_enter(2.0, 10, datetime(2026, 10, 23, 14, 1), 0.0)
        self.assertEqual(actions, [])
        self.assertIsNone(bot.pos)

    def test_force_closes_open_simulated_position_at_four_pm_new_york(self):
        bot = MomentumTrader("TEST", "LONG", {"use_scanner_filters": False})
        bot.pos = {"entry": 2.0, "stop": 1.9, "k": 1}
        actions = bot.on_tick(2.1, datetime(2026, 10, 23, 16, 0))
        self.assertEqual([a["action"] for a in actions], ["EXIT"])
        self.assertIn("16:00", actions[0]["reason"])
        self.assertIsNone(bot.pos)

    def test_news_without_supported_catalyst_is_rejected(self):
        bot = MomentumTrader("TEST", "LONG", {"use_scanner_filters": False, "news_require_keyword": True})
        bot.set_context({"news": [{
            "published_at": datetime(2026, 10, 23, 9, 0),
            "headline": "Company launches redesigned investor website",
        }]})
        result = bot._fresh_news(datetime(2026, 10, 23, 10, 0))
        self.assertIsNone(result)
        bot.set_context({"news": [{
            "published_at": datetime(2026, 10, 23, 9, 0),
            "headline": "Company announces record quarterly earnings and revenue",
        }]})
        result = bot._fresh_news(datetime(2026, 10, 23, 10, 0))
        self.assertEqual(result["categoria"], "earnings")


if __name__ == "__main__":
    unittest.main()
