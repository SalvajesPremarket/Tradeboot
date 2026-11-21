"""Tests for Calculador Minor's reviewed paper-only engine."""
import unittest
from datetime import datetime, timedelta, timezone

from calculator_minor import CalculatorMinorTrader, analyze_direction, calculate_market_levels


class CalculatorMinorTests(unittest.TestCase):
    def _bars(self, count=260):
        start = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(count):
            price = 8 + i * 0.006
            rows.append({
                "timestamp": (start + timedelta(minutes=i)).isoformat(),
                "open": price - 0.002, "high": price + 0.02,
                "low": price - 0.02, "close": price, "volume": 10000 + i,
            })
        return rows

    def test_direction_has_finite_score_and_five_bar_trigger(self):
        result = analyze_direction(self._bars())
        self.assertGreaterEqual(result["score"], -100)
        self.assertLessEqual(result["score"], 100)
        self.assertGreater(result["trigger_high"], result["trigger_low"])
        self.assertIsNotNone(result["atr"])

    def test_multitimeframe_levels_never_fabricate_missing_atr(self):
        minute = self._bars()
        daily = self._bars(90)
        now = datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc)
        result = calculate_market_levels(
            [{**x, "timestamp": x["timestamp"]} for x in minute],
            [{**x, "timestamp": x["timestamp"]} for x in daily],
            9.5, now,
        )
        self.assertEqual(len(result["timeframes"]), 12)
        self.assertIn("levels", result)

    def test_paper_strategy_emits_only_simulation_actions(self):
        candles = self._bars()
        start = datetime.fromisoformat(candles[-1]["timestamp"].replace("Z", "+00:00"))
        ticks = [
            {"price": 12.0, "timestamp": (start + timedelta(minutes=1, seconds=1)).isoformat()},
        ]
        bot = CalculatorMinorTrader("TEST")
        result = bot.simulate(candles, self._bars(90), ticks)
        self.assertEqual(result["mode"], "simulation_only")
        self.assertFalse(result["real_orders_enabled"])
        self.assertIsInstance(result["actions"], list)

    def test_target_one_closes_half_and_target_two_closes_remainder(self):
        candles = self._bars()
        last = datetime.fromisoformat(candles[-1]["timestamp"].replace("Z", "+00:00"))
        ticks = [
            {"price": 11.0, "timestamp": (last + timedelta(minutes=1, seconds=1)).isoformat()},
            {"price": 12.0, "timestamp": (last + timedelta(minutes=2, seconds=1)).isoformat()},
        ]
        open_position = {
            "side": "LONG", "entry_price": 10.0, "stop_price": 9.0,
            "target1_price": 11.0, "take_price": 12.0,
            "target1_done": False, "entry_at": (last - timedelta(minutes=1)).isoformat(),
        }
        result = CalculatorMinorTrader("TEST").simulate(candles, self._bars(90), ticks, open_position)
        self.assertEqual([item["action"] for item in result["actions"]], ["PARTIAL_EXIT", "EXIT"])
        self.assertEqual(result["actions"][0]["quantity_pct"], 0.5)
        self.assertIn("Objetivo 2", result["actions"][1]["reason"])


if __name__ == "__main__":
    unittest.main()
