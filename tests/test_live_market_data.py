"""Tests for the eToro historical OHLCV adapter and WebSocket tick parsing."""
import unittest
from unittest.mock import patch

import app
from paper_trading import _tick_from_message


class HistoricalCandleTests(unittest.TestCase):
    @patch("app.etoro_get")
    def test_normalizes_historical_candles_oldest_first(self, get):
        get.return_value = (200, {"ok": True, "data": {"candles": [
            {"fromDate": "2026-10-22T14:02:00Z", "open": 10.2, "high": 10.5,
             "low": 10.1, "close": 10.4, "volume": 1200},
            {"fromDate": "2026-10-22T14:01:00Z", "open": 10.0, "high": 10.3,
             "low": 9.9, "close": 10.2, "volume": 900},
        ]}})
        status, result = app.fetch_historical_candles(123, "OneMinute", 220)
        self.assertEqual(status, 200)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["candles"][0]["timestamp"], "2026-10-22T14:01:00+00:00")
        self.assertEqual(result["candles"][0]["volume"], 900.0)
        get.assert_called_once_with("market-data/instruments/123/history/candles/desc/OneMinute/220")

    @patch("app.etoro_get")
    def test_rejects_invalid_interval_and_count_without_provider_call(self, get):
        self.assertEqual(app.fetch_historical_candles(123, "1m", 220)[0], 400)
        self.assertEqual(app.fetch_historical_candles(123, "OneDay", 1001)[0], 400)
        get.assert_not_called()

    @patch("app.etoro_get")
    def test_reports_empty_or_invalid_provider_data(self, get):
        get.return_value = (200, {"ok": True, "data": {"candles": [
            {"fromDate": "not-a-date", "open": 10, "high": 11, "low": 9, "close": 10}
        ]}})
        status, result = app.fetch_historical_candles(123, "OneDay", 10)
        self.assertEqual(status, 502)
        self.assertEqual(result["error"], "provider_returned_no_valid_ohlcv")


class LiveTickParserTests(unittest.TestCase):
    def test_parses_last_price_timestamp_and_day_volume(self):
        tick = _tick_from_message({
            "lastPrice": 12.34,
            "timestamp": "2026-10-22T14:02:03Z",
            "dayVolume": 123456,
        }, "AAPL", 123)
        self.assertIsNotNone(tick)
        self.assertEqual(tick["symbol"], "AAPL")
        self.assertEqual(tick["price"], 12.34)
        self.assertEqual(tick["day_volume"], 123456.0)
        self.assertEqual(tick["timestamp"].isoformat(), "2026-10-22T14:02:03+00:00")

    def test_parses_bid_ask_midpoint(self):
        tick = _tick_from_message({"bid": 10.0, "ask": 10.2}, "AAPL", 123)
        self.assertIsNotNone(tick)
        self.assertAlmostEqual(tick["price"], 10.1)

    def test_rejects_messages_without_a_valid_price(self):
        self.assertIsNone(_tick_from_message({"bid": 0, "ask": 0}, "AAPL", 123))


if __name__ == "__main__":
    unittest.main()
