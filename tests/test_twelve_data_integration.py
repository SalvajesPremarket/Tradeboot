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
        td._MASSIVE_BATCH_CHECKED.clear()
        td._MASSIVE_BATCH_LAST_REQUEST = 0.0

    def tearDown(self):
        td._API_KEY = self.old_key
        td._ENABLE_PAID_ENDPOINTS = self.old_paid_endpoints
        td._COUNTRIES = self.old_countries
        td._COUNTRY_CURSOR = self.old_cursor
        td._CACHE.clear()
        td._COUNTRY_ROWS.clear()
        td._SYMBOL_META.clear()
        td._DIAGNOSTICS.clear()
        td._MASSIVE_BATCH_CHECKED.clear()
        td._MASSIVE_BATCH_LAST_REQUEST = 0.0

    def _disable_public_fallbacks(self):
        """Make outage tests deterministic without making real HTTP requests."""
        return (
            patch.object(td, "urlopen", side_effect=OSError("network disabled in test")),
            patch.object(td, "_get_nasdaq_us_candidates", return_value=([], [])),
            patch("schwab_integration.get_quotes", return_value=(503, {"error": "quotes_unavailable"})),
        )

    def test_basic_plan_skips_twelve_data_paid_endpoint_when_all_candidate_sources_are_unavailable(self):
        with patch("schwab_integration.get_market_movers", return_value=(503, {"error": "schwab_authorization_required"})):
            fallback_patches = self._disable_public_fallbacks()
            with fallback_patches[0], fallback_patches[1], fallback_patches[2]:
                with patch.object(td, "_request_json") as request:
                    self.assertEqual(td.get_global_candidates(limit=25), [])
        request.assert_not_called()
        diagnostic = td.get_diagnostics()["market_movers/stocks"]
        self.assertEqual(diagnostic["status"], "all_candidate_sources_unavailable")
        self.assertEqual(diagnostic["twelve_data_status"], "skipped_plan_basic")

    def test_basic_plan_uses_schwab_even_without_twelve_data_api_key(self):
        td._API_KEY = ""
        with patch("schwab_integration.get_market_movers") as movers:
            movers.side_effect = [
                (200, {"screeners": [
                    {"symbol": "SMALL", "lastPrice": 4.25, "netPercentChange": 15.0},
                ]}),
                (200, {"screeners": []}),
            ]
            self.assertEqual(td.get_global_candidates(limit=25), ["SMALL"])
        self.assertEqual(movers.call_count, 2)
        self.assertEqual(td.get_diagnostics()["market_movers/stocks"]["provider"], "schwab")

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

    def test_basic_plan_reports_no_candidates_when_all_sources_are_unavailable(self):
        with patch("schwab_integration.get_market_movers", return_value=(503, {"error": "schwab_authorization_required"})):
            fallback_patches = self._disable_public_fallbacks()
            with fallback_patches[0], fallback_patches[1], fallback_patches[2]:
                self.assertEqual(td.get_global_candidates(limit=25), [])
        self.assertEqual(td.get_diagnostics()["market_movers/stocks"]["status"], "all_candidate_sources_unavailable")

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

    def test_massive_float_prefetch_batches_symbols_and_caches_verified_values(self):
        from datetime import date
        with patch.dict("os.environ", {"MASSIVE_API_KEY": "massive-test-key"}, clear=False):
            with patch.object(td, "_provider_json", return_value=(200, {"results": [
                {"ticker": "AAA", "free_float": 1234567, "effective_date": date.today().isoformat()},
            ]})) as provider:
                result = td.prefetch_free_floats(["AAA", "BBB", "AAA"])
        self.assertEqual(result, {"status": "ok", "requested": 2, "valid": 1})
        request_url = provider.call_args.args[0]
        self.assertIn("ticker.any_of=AAA%2CBBB", request_url)
        self.assertEqual(td._get_free_float("AAA")["float_shares"], 1234567)
        self.assertEqual(td._get_free_float("AAA")["provider"], "massive")

    def test_massive_batch_miss_skips_individual_massive_request_and_fails_closed(self):
        with patch.dict("os.environ", {"MASSIVE_API_KEY": "massive-test-key"}, clear=False):
            with patch.object(td, "_provider_json", return_value=(200, {"results": []})) as provider:
                td.prefetch_free_floats(["NOFLOAT"])
            with patch.object(td, "_get_yahoo_float", return_value=(None, "yahoo_float_unavailable")):
                result = td._get_free_float("NOFLOAT")
        self.assertIsNone(result["float_shares"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["attempts"], ["massive_no_valid_float", "yahoo_float_unavailable"])
        self.assertEqual(provider.call_count, 1)

    def test_float_lookup_returns_unavailable_when_authorized_sources_have_no_float(self):
        with patch.dict("os.environ", {"MASSIVE_API_KEY": "", "POLYGON_API_KEY": ""}, clear=False):
            with patch.object(td, "_get_yahoo_float", return_value=(None, "yahoo_float_unavailable")):
                with patch.object(td, "urlopen", side_effect=AssertionError("unexpected external request")):
                    result = td._get_free_float("NOFLOAT")
        self.assertIsNone(result["float_shares"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["attempts"], ["yahoo_float_unavailable"])

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


    def test_yahoo_cookie_and_crumb_session_is_reused_between_symbols(self):
        class Response:
            def __init__(self, body):
                self.body = body
            def read(self):
                return self.body
            def close(self):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        class Opener:
            def __init__(self):
                self.calls = []
            def open(self, request, timeout=None):
                self.calls.append(request.full_url)
                if "getcrumb" in request.full_url:
                    return Response(b"shared-crumb")
                if "quoteSummary" in request.full_url:
                    return Response(b'{"quoteSummary":{"result":[{"defaultKeyStatistics":{"floatShares":{"raw":1234567}}}]}}')
                return Response(b"")

        opener = Opener()
        td._YAHOO_OPENER = None
        td._YAHOO_CRUMB = None
        td._YAHOO_CRUMB_EXPIRES = 0.0
        td._YAHOO_COOLDOWN_UNTIL = 0.0
        with patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(td._get_yahoo_float("AAA"), (1234567, None))
            self.assertEqual(td._get_yahoo_float("BBB"), (1234567, None))
        self.assertEqual(sum("getcrumb" in url for url in opener.calls), 1)
        self.assertEqual(sum("fc.yahoo.com" in url for url in opener.calls), 1)
        self.assertEqual(sum("quoteSummary" in url for url in opener.calls), 2)

    def test_yahoo_rate_limit_enters_shared_cooldown_without_new_requests(self):
        td._YAHOO_COOLDOWN_UNTIL = td.time.monotonic() + 600
        with patch("urllib.request.build_opener", side_effect=AssertionError("must respect cooldown")):
            self.assertEqual(td._get_yahoo_float("AAA"), (None, "yahoo_rate_limited_cooldown"))
            self.assertEqual(td._get_yahoo_float("BBB"), (None, "yahoo_rate_limited_cooldown"))
        td._YAHOO_COOLDOWN_UNTIL = 0.0


if __name__ == "__main__":
    unittest.main()
