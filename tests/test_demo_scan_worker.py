import os
import unittest
from unittest.mock import patch

import demo_scan_worker


class DemoScanWorkerTests(unittest.TestCase):
    def setUp(self):
        self.strategy = {
            "id": "s1", "name": "Test", "enabled": True,
            "strategyType": "formula", "formula": "price > 10",
        }
        self.market = {
            "ok": True, "instrument_id": 12,
            "quote_time": "2026-10-21T14:00:00Z", "quote_age_seconds": 1,
            "quote_is_usable": True, "quote_quality_status": "fresh_market_open",
            "market_values": {"price": 11},
        }

    @patch("demo_scan_worker.fetch_market_context")
    @patch("demo_scan_worker.list_strategies")
    def test_scan_is_read_only_and_returns_signal(self, strategies, market):
        strategies.return_value = [self.strategy]
        market.return_value = (200, self.market)
        result = demo_scan_worker.scan_once(["ABC"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["results"][0]["signal_matched"])
        self.assertEqual(result["orders_submitted"], 0)
        self.assertEqual(result["mode"], "scan_only")

    @patch("demo_scan_worker.fetch_market_context")
    @patch("demo_scan_worker.list_strategies")
    def test_bad_market_context_does_not_crash_cycle(self, strategies, market):
        strategies.return_value = [self.strategy]
        market.return_value = (502, {"ok": False, "error": "provider_down"})
        result = demo_scan_worker.scan_once(["ABC"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["results"][0]["error"], "market_context_unavailable")
        self.assertEqual(result["orders_submitted"], 0)

    @patch("demo_scan_worker.list_strategies")
    def test_requires_exactly_one_enabled_strategy(self, strategies):
        strategies.return_value = []
        result = demo_scan_worker.scan_once(["ABC"])
        self.assertEqual(result["error"], "exactly_one_enabled_strategy_required")
        self.assertEqual(result["orders_submitted"], 0)

    @patch.dict(os.environ, {"TRADEBOOT_SCAN_SYMBOLS": " aapl, msft "})
    def test_configured_symbols_normalized(self):
        self.assertEqual(demo_scan_worker.configured_symbols(), ["AAPL", "MSFT"])

    @patch.dict(os.environ, {"TRADEBOOT_SCAN_SYMBOLS": "AAPL,AAPL"})
    def test_duplicate_symbols_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            demo_scan_worker.configured_symbols()

    @patch.dict(os.environ, {"TRADEBOOT_SCAN_SYMBOLS": ""})
    def test_empty_symbols_rejected(self):
        with self.assertRaisesRegex(ValueError, "1 to 5"):
            demo_scan_worker.configured_symbols()


if __name__ == "__main__":
    unittest.main()
