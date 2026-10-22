import unittest

from strategy_market_runtime import OneMinuteCandleBuilder, PersistentOneMinuteRuntime, parse_market_timestamp
from market_candle_store import store_closed_candle


class OneMinuteCandleBuilderTests(unittest.TestCase):
    def test_builds_ohlc_and_closes_only_on_next_observed_minute(self):
        builder = OneMinuteCandleBuilder("aapl")
        self.assertIsNone(builder.on_tick(10.0, "2026-10-21T14:30:01Z"))
        self.assertIsNone(builder.on_tick(12.0, "2026-10-21T14:30:20Z"))
        self.assertIsNone(builder.on_tick(9.0, "2026-10-21T14:30:59Z"))
        self.assertEqual(builder.current_candle()["symbol"], "AAPL")
        closed = builder.on_tick(11.0, "2026-10-21T14:31:03Z")
        self.assertEqual(closed["timestamp"], "2026-10-21T14:30:00Z")
        self.assertEqual((closed["open"], closed["high"], closed["low"], closed["close"]), (10.0, 12.0, 9.0, 9.0))
        self.assertEqual(closed["ticks"], 3)
        self.assertTrue(closed["complete"])
        self.assertFalse(builder.current_candle()["complete"])

    def test_missing_minutes_are_not_fabricated(self):
        builder = OneMinuteCandleBuilder("MSFT")
        builder.on_tick(20.0, "2026-10-21T14:30:01Z")
        closed = builder.on_tick(22.0, "2026-10-21T14:33:01Z")
        self.assertEqual(closed["timestamp"], "2026-10-21T14:30:00Z")
        self.assertEqual(builder.current_candle()["timestamp"], "2026-10-21T14:33:00Z")

    def test_rejects_out_of_order_ticks_and_invalid_prices(self):
        builder = OneMinuteCandleBuilder("NVDA")
        builder.on_tick(10.0, "2026-10-21T14:30:10Z")
        with self.assertRaisesRegex(ValueError, "out_of_order_tick"):
            builder.on_tick(11.0, "2026-10-21T14:30:09Z")
        with self.assertRaisesRegex(ValueError, "tick_price_must_be_positive_finite_number"):
            builder.on_tick(0, "2026-10-21T14:30:11Z")

    def test_timestamp_must_include_timezone(self):
        with self.assertRaisesRegex(ValueError, "tick_timestamp_timezone_required"):
            parse_market_timestamp("2026-10-21T14:30:00")


    def test_persistent_runtime_stores_only_closed_candles(self):
        stored = []
        runtime = PersistentOneMinuteRuntime("AAPL", candle_store=lambda candle: stored.append(dict(candle)) or {"stored": True})
        self.assertIsNone(runtime.on_tick(10.0, "2026-10-21T14:30:01Z"))
        self.assertEqual(stored, [])
        result = runtime.on_tick(10.5, "2026-10-21T14:31:01Z")
        self.assertTrue(result["persistence"]["stored"])
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0]["complete"])
        self.assertEqual(stored[0]["close"], 10.0)

    def test_database_store_rejects_open_or_invalid_candles_before_io(self):
        with self.assertRaisesRegex(ValueError, "closed_candle_required"):
            store_closed_candle({"symbol": "AAPL", "complete": False})
        with self.assertRaisesRegex(ValueError, "candle_ohlc_invalid"):
            store_closed_candle({
                "symbol": "AAPL", "timestamp": "2026-10-21T14:30:00Z",
                "open": 10, "high": 9, "low": 8, "close": 10,
                "ticks": 2, "complete": True,
            })


if __name__ == "__main__":
    unittest.main()
