import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import schwab_integration


class SchwabHistoricalHistoryTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    @patch("schwab_integration._access_token", return_value="test-access-token")
    @patch("schwab_integration.urlopen")
    def test_ten_year_daily_request_uses_supported_parameters(self, mocked_urlopen, _token):
        mocked_urlopen.return_value = self.Response({
            "candles": [{"datetime": 1704153600000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10}]
        })
        status, result = schwab_integration.get_historical_price_history(
            "SPY", granularity="daily", lookback=10
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["coverage"]["count"], 1)
        params = parse_qs(urlsplit(mocked_urlopen.call_args.args[0].full_url).query)
        self.assertEqual(params["periodType"], ["year"])
        self.assertEqual(params["period"], ["10"])
        self.assertEqual(params["frequencyType"], ["daily"])

    @patch("schwab_integration._access_token", return_value="test-access-token")
    @patch("schwab_integration.urlopen")
    def test_ten_day_minute_request_includes_extended_hours(self, mocked_urlopen, _token):
        mocked_urlopen.return_value = self.Response({
            "candles": [{"datetime": 1704153600000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10}]
        })
        status, result = schwab_integration.get_historical_price_history(
            "SPY", granularity="minute", lookback=10
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["interval"], "1minute")
        params = parse_qs(urlsplit(mocked_urlopen.call_args.args[0].full_url).query)
        self.assertEqual(params["periodType"], ["day"])
        self.assertEqual(params["period"], ["10"])
        self.assertEqual(params["frequencyType"], ["minute"])
        self.assertEqual(params["needExtendedHoursData"], ["true"])

    def test_rejects_unsupported_lookbacks(self):
        self.assertEqual(
            schwab_integration.get_historical_price_history(
                "SPY", granularity="daily", lookback=11
            )[1]["error"],
            "invalid_daily_lookback",
        )
        self.assertEqual(
            schwab_integration.get_historical_price_history(
                "SPY", granularity="minute", lookback=20
            )[1]["error"],
            "invalid_minute_lookback",
        )


if __name__ == "__main__":
    unittest.main()
