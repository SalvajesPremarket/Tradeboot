import unittest

from app import evaluate_demo_scan_snapshot


class DemoSignalScanTests(unittest.TestCase):
    def setUp(self):
        self.strategy = {
            "id": "strategy-1",
            "name": "Test formula",
            "strategyType": "formula",
            "formula": "price > 10",
        }
        self.market = {
            "ok": True,
            "instrument_id": 123,
            "quote_time": "2026-10-21T14:00:00Z",
            "quote_age_seconds": 2,
            "quote_is_usable": True,
            "quote_quality_status": "fresh_market_open",
            "market_values": {"price": 12},
        }

    def test_matching_formula_returns_signal_without_execution(self):
        result = evaluate_demo_scan_snapshot(self.strategy, "ABC", self.market)
        self.assertTrue(result["ok"])
        self.assertTrue(result["signal_matched"])
        self.assertEqual(result["execution"], "not_attempted")
        self.assertEqual(result["instrument_id"], 123)

    def test_non_matching_formula_returns_no_signal(self):
        market = {**self.market, "market_values": {"price": 8}}
        result = evaluate_demo_scan_snapshot(self.strategy, "ABC", market)
        self.assertTrue(result["ok"])
        self.assertFalse(result["signal_matched"])
        self.assertEqual(result["execution"], "not_attempted")

    def test_unusable_quote_fails_closed(self):
        market = {**self.market, "quote_is_usable": False, "quote_quality_status": "stale"}
        result = evaluate_demo_scan_snapshot(self.strategy, "ABC", market)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "quote_not_usable")
        self.assertEqual(result["execution"], "not_attempted")

    def test_missing_formula_data_fails_closed(self):
        market = {**self.market, "market_values": {}}
        result = evaluate_demo_scan_snapshot(self.strategy, "ABC", market)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "strategy_data_unavailable")
        self.assertEqual(result["execution"], "not_attempted")

    def test_python_program_is_not_scanned(self):
        strategy = {**self.strategy, "strategyType": "python_program"}
        result = evaluate_demo_scan_snapshot(strategy, "ABC", self.market)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "formula_strategy_required")
        self.assertEqual(result["execution"], "not_attempted")


if __name__ == "__main__":
    unittest.main()
