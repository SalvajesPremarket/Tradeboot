import unittest
from datetime import datetime, timedelta

from momentum_tramos import MomentumTrader


class MomentumTramosTests(unittest.TestCase):
    def test_rejects_invalid_side(self):
        with self.assertRaisesRegex(ValueError, "LONG or SHORT"):
            MomentumTrader("ABCD", "SIDEWAYS")

    def test_long_and_short_signal_orders_are_mirrored(self):
        long_bot = MomentumTrader("ABCD", "LONG", {"use_scanner_filters": False})
        short_bot = MomentumTrader("ABCD", "SHORT", {"use_scanner_filters": False})
        self.assertEqual(long_bot._act("ENTER", 2.0)["order"], "BUY")
        self.assertEqual(short_bot._act("ENTER", 2.0)["order"], "SELL")
        self.assertEqual(long_bot._act("EXIT", 2.0)["order"], "SELL")
        self.assertEqual(short_bot._act("EXIT", 2.0)["order"], "BUY")

    def test_missing_scanner_context_fails_closed(self):
        bot = MomentumTrader("ABCD", "LONG")
        reasons = bot._scanner_filters(3.0, datetime(2026, 1, 1, 9, 0))
        self.assertIn("sin cierre anterior (prev_close)", reasons)
        self.assertIn("float desconocido", reasons)
        self.assertIn("sin noticia fresca", reasons)

    def test_scanner_filters_can_be_disabled_as_one_group(self):
        bot = MomentumTrader("ABCD", "LONG", {"use_scanner_filters": False})
        self.assertEqual(bot._scanner_filters(3.0, datetime(2026, 1, 1, 9, 0)), [])

    def test_cancel_entry_clears_simulated_position(self):
        bot = MomentumTrader("ABCD")
        bot.pos = {"entry": 2.0, "stop": 1.9, "k": 1}
        bot.cancel_entry()
        self.assertIsNone(bot.pos)

    def test_late_ticks_do_not_rewind_candle(self):
        bot = MomentumTrader("ABCD", cfg={"use_scanner_filters": False})
        start = datetime(2026, 1, 1, 9, 30, 0)
        bot.on_tick(2.0, start)
        current_candle = bot.c
        bot.on_tick(1.99, start - timedelta(minutes=1))
        self.assertIs(bot.c, current_candle)
        self.assertEqual(bot.c["close"], 2.0)

    def test_stop_only_moves_in_favorable_direction(self):
        bot = MomentumTrader("ABCD", "LONG")
        bot.pos = {"entry": 2.0, "stop": 1.9, "k": 1}
        self.assertIsNone(bot._raise_stop(1.85, 2.1))
        self.assertEqual(bot.pos["stop"], 1.9)
        action = bot._raise_stop(2.0, 2.1)
        self.assertEqual(action["action"], "MOVE_STOP")
        self.assertEqual(bot.pos["stop"], 2.0)


if __name__ == "__main__":
    unittest.main()
