"""Regression checks for JeanShort and both Pullback strategy implementations."""
import unittest

import numpy as np
import pandas as pd

from strategy_programs import jean_short, pullback_long, pullback_short


class ThreeStrategyAuditTests(unittest.TestCase):
    def test_jean_short_emits_short_order_directions(self):
        bot = jean_short.MomentumTrader("XYZ")
        self.assertEqual(bot._act("ENTER", 5.0)["order"], "SELL")
        self.assertEqual(bot._act("EXIT", 5.0)["order"], "BUY")

    def test_pullback_long_and_short_order_directions_are_opposites(self):
        long_sizer = pullback_long.PositionSizer(1000, 100)
        short_sizer = pullback_short.PositionSizer(1000, 100)
        long_bot = pullback_long.PullbackLongTrader("XYZ", long_sizer)
        short_bot = pullback_short.PullbackShortTrader("XYZ", short_sizer)

        self.assertEqual(long_bot._act("ENTER", 5.0)["order"], "BUY")
        self.assertEqual(long_bot._act("EXIT", 5.0)["order"], "SELL")
        self.assertEqual(short_bot._act("ENTER", 5.0)["order"], "SELL")
        self.assertEqual(short_bot._act("EXIT", 5.0)["order"], "BUY")

    def test_weekly_context_excludes_partial_last_week(self):
        for module in (pullback_long, pullback_short):
            with self.subTest(strategy=module.STRATEGY_NAME):
                dates = pd.bdate_range(end="2026-11-04", periods=300)
                frame = pd.DataFrame({
                    "open": np.full(len(dates), 10.0),
                    "high": np.full(len(dates), 10.5),
                    "low": np.full(len(dates), 9.5),
                    "close": np.full(len(dates), 10.0),
                    "volume": np.full(len(dates), 500000.0),
                }, index=dates)
                weekly = module.to_weekly(frame)
                self.assertEqual(weekly.index[-1], pd.Timestamp("2026-10-30"))

    def test_completed_friday_week_is_kept(self):
        for module in (pullback_long, pullback_short):
            with self.subTest(strategy=module.STRATEGY_NAME):
                dates = pd.bdate_range(end="2026-11-06", periods=300)
                frame = pd.DataFrame({
                    "open": np.full(len(dates), 10.0),
                    "high": np.full(len(dates), 10.5),
                    "low": np.full(len(dates), 9.5),
                    "close": np.full(len(dates), 10.0),
                    "volume": np.full(len(dates), 500000.0),
                }, index=dates)
                weekly = module.to_weekly(frame)
                self.assertEqual(weekly.index[-1], pd.Timestamp("2026-11-06"))

    def test_daily_context_rechecks_bar_count_after_invalid_rows_are_removed(self):
        dates = pd.bdate_range(end="2026-11-06", periods=70)
        frame = pd.DataFrame({
            "open": np.full(len(dates), 10.0),
            "high": np.full(len(dates), 10.5),
            "low": np.full(len(dates), 9.5),
            "close": np.full(len(dates), 10.0),
            "volume": np.full(len(dates), 500000.0),
        }, index=dates)
        frame.loc[dates[-15:], ["open", "high", "low", "close"]] = np.nan

        for module in (pullback_long, pullback_short):
            with self.subTest(strategy=module.STRATEGY_NAME):
                result = module.daily_context(
                    frame,
                    {"symbol": "XYZ", "avg_volume_30d": 500000, "float_shares": 10000000},
                    "D",
                )
                self.assertFalse(result["armed"])
                self.assertIn("pocas velas completas", result["reasons"])


if __name__ == "__main__":
    unittest.main()
