import unittest
from unittest.mock import patch

import twelve_data_integration as td


class TwelveDataIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old_key = td._API_KEY
        self.old_paid_endpoints = td._ENABLE_PAID_ENDPOINTS
        td._ENABLE_PAID_ENDPOINTS = False
        self.old_countries = td._COUNTRIES
        self.old_cursor = td._COUNTRY_CURSOR
        td._API_KEY = "test-key"
        td._COUNTRIES = ("United States",)
        td._COUNTRY_CURSOR = 0
        td._CACHE.clear()
        td._COUNTRY_ROWS.clear()
        td._SYMBOL_META.clear()
        td._DIAGNOSTICS.clear()
        td._LAST_STATS_REQUEST = 0.0

    def tearDown(self):
        td._API_KEY = self.old_key
        td._ENABLE_PAID_ENDPOINTS = self.old_paid_endpoints
        td._COUNTRIES = self.old_countries
        td._COUNTRY_CURSOR = self.old_cursor
        td._CACHE.clear()
        td._COUNTRY_ROWS.clear()
        td._SYMBOL_META.clear()
        td._DIAGNOSTICS.clear()

    def test_basic_plan_skips_paid_market_movers_without_requesting_endpoint(self):
        with patch.object(td, "_request_json") as request:
            self.assertEqual(td.get_global_candidates(limit=25), [])
        request.assert_not_called()
        self.assertEqual(td.get_diagnostics()["market_movers/stocks"]["status"], "skipped_plan_basic")

    def test_basic_plan_uses_schwab_movers_without_calling_twelve_data_paid_endpoint(self):
        with patch("schwab_integration.get_market_movers") as movers:
            movers.side_effect = [
                (200, {"screeners": [
                    {"symbol": "SMALL", "lastPrice": 4.25, "netPercentChange": 15.0},
                    {"symbol": "EXPENSIVE", "lastPrice": 35.0, "netPercentChange": 40.0},
                ]}),
                (200, {"screeners": [
                    {"symbol": "OTHER", "lastPrice": 8.0, "netPercentChange": 7.0},
                ]}),
            ]
            with patch.object(td, "_request_json") as request:
                self.assertEqual(td.get_global_candidates(limit=25), ["SMALL", "OTHER"])
            request.assert_not_called()
        self.assertEqual(movers.call_count, 2)
        self.assertEqual(td.get_symbol_metadata("SMALL")["provider"], "schwab")
        self.assertEqual(td.get_diagnostics()["market_movers/stocks"]["provider"], "schwab")

    def test_basic_plan_reports_no_candidates_when_schwab_unavailable(self):
        with patch("schwab_integration.get_market_movers", return_value=(503, {"error": "schwab_authorization_required"})):
            self.assertEqual(td.get_global_candidates(limit=25), [])
        self.assertEqual(td.get_diagnostics()["market_movers/stocks"]["status"], "schwab_unavailable")

    def test_global_candidates_filter_invalid_prices_and_keep_market_metadata(self):
        td._ENABLE_PAID_ENDPOINTS = True
        payload = {"values": [
            {"symbol": "GOOD", "last": 5.5, "percent_change": 12.0, "exchange": "NASDAQ"},
            {"symbol": "TOO_HIGH", "last": 25.0, "percent_change": 50.0, "exchange": "NASDAQ"},
            {"symbol": "TOO_LOW", "last": 0.5, "percent_change": 80.0, "exchange": "NASDAQ"},
            {"symbol": "$BAD", "last": 4.0, "percent_change": 10.0, "exchange": "NASDAQ"},
        ]}
        with patch.object(td, "_request_json", return_value=(200, payload)):
            self.assertEqual(td.get_global_candidates(limit=25), ["GOOD"])
        self.assertEqual(td.get_symbol_metadata("GOOD")["exchange"], "NASDAQ")
        self.assertEqual(td.get_symbol_metadata("GOOD")["country"], "United States")

    def test_float_parser_does_not_mistake_shares_outstanding_for_float(self):
        self.assertIsNone(td._find_float_shares({"shares_outstanding": 1000000}))
        self.assertEqual(td._find_float_shares({"statistics": {"float_shares": "12500000"}}), 12500000)

    def test_quote_is_normalized_to_paper_scanner_fields(self):
        with patch.object(td, "_request_json", return_value=(200, {
            "price": "4.25", "datetime": "2026-10-26T14:30:00Z", "volume": "90000"
        })):
            status, payload = td.get_quote("GOOD", {"country": "United States", "exchange": "NASDAQ"})
        self.assertEqual(status, 200)
        quote = payload["quote"]
        self.assertEqual(quote["lastPrice"], 4.25)
        self.assertEqual(quote["totalVolume"], 90000.0)
        self.assertIn("quoteTimeInLong", quote)

    def test_history_rows_are_normalized(self):
        with patch.object(td, "_request_json", return_value=(200, {"values": [
            {"datetime": "2026-10-26 14:29:00", "open": "4", "high": "4.5",
             "low": "3.9", "close": "4.2", "volume": "1000"}
        ]})):
            status, payload = td.get_price_history("GOOD", daily=False, metadata={"country": "Canada"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["candles"][0]["close"], "4.2")
        self.assertEqual(payload["candles"][0]["volume"], "1000")


if __name__ == "__main__":
    unittest.main()
