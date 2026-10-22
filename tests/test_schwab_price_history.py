import json
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

import schwab_integration


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SchwabPriceHistoryTests(unittest.TestCase):
    @patch("schwab_integration._access_token", return_value="test-access-token")
    @patch("schwab_integration.urlopen")
    def test_minute_history_uses_read_only_pricehistory_endpoint(self, mock_urlopen, _token):
        mock_urlopen.return_value = _Response({"candles": [{"datetime": 1792674000000, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100}]})
        status, result = schwab_integration.get_price_history("aapl")
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["interval"], "1minute")
        self.assertEqual(len(result["candles"]), 1)
        request = mock_urlopen.call_args.args[0]
        parsed = urlparse(request.full_url)
        self.assertTrue(parsed.path.endswith("/pricehistory"))
        params = parse_qs(parsed.query)
        self.assertEqual(params["symbol"], ["AAPL"])
        self.assertEqual(params["frequencyType"], ["minute"])
        self.assertEqual(request.get_method(), "GET")

    @patch("schwab_integration._access_token", return_value="test-access-token")
    @patch("schwab_integration.urlopen")
    def test_daily_history_uses_daily_candles(self, mock_urlopen, _token):
        mock_urlopen.return_value = _Response({"candles": []})
        status, result = schwab_integration.get_price_history("MSFT", daily=True)
        self.assertEqual(status, 200)
        self.assertEqual(result["interval"], "1day")
        params = parse_qs(urlparse(mock_urlopen.call_args.args[0].full_url).query)
        self.assertEqual(params["frequencyType"], ["daily"])
        self.assertEqual(params["symbol"], ["MSFT"])

    @patch("schwab_integration._access_token")
    @patch("schwab_integration.urlopen")
    def test_invalid_symbol_is_rejected_before_network_access(self, mock_urlopen, mock_token):
        status, result = schwab_integration.get_price_history("AAPL&symbols=MSFT")
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_symbol")
        mock_token.assert_not_called()
        mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
