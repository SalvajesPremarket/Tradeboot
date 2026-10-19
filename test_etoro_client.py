import json
import os
import unittest
from unittest.mock import patch

from etoro_client import (
    EtoroApiError,
    EtoroClient,
    EtoroConfig,
    EtoroConfigurationError,
)


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class EtoroClientTests(unittest.TestCase):
    def setUp(self):
        self.config = EtoroConfig(
            api_key="public-test-key",
            user_key="user-test-key",
            api_base="https://example.test",
        )
        self.client = EtoroClient(self.config)

    @patch("etoro_client.urlopen")
    def test_default_watchlist_uses_required_headers(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse([{"itemId": 1001}])
        result = self.client.get_default_watchlist()
        self.assertEqual(result, [{"itemId": 1001}])
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), "public-test-key")
        self.assertEqual(request.get_header("X-user-key"), "user-test-key")
        self.assertTrue(request.get_header("X-request-id"))
        self.assertIn("/api/v1/watchlists/default-watchlists/items", request.full_url)

    @patch("etoro_client.urlopen")
    def test_demo_portfolio_uses_demo_read_endpoint(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse([])
        self.assertEqual(self.client.get_demo_portfolio(), [])
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("/api/v1/trading/info/demo/portfolio", request.full_url)

    @patch("etoro_client.urlopen")
    def test_health_check_is_read_only(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse([{"itemId": 1}, {"itemId": 2}])
        result = self.client.health_check()
        self.assertEqual(result["provider"], "eToro")
        self.assertTrue(result["connected"])
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(result["default_watchlist_items"], 2)

    def test_missing_credentials_fail_closed(self):
        with patch.dict(os.environ, {"ETORO_API_KEY": "", "ETORO_USER_KEY": ""}):
            with self.assertRaises(EtoroConfigurationError):
                EtoroConfig.from_env()

    @patch("etoro_client.urlopen")
    def test_http_error_does_not_leak_response_body_or_credentials(self, mocked_urlopen):
        from urllib.error import HTTPError
        mocked_urlopen.side_effect = HTTPError(
            "https://example.test", 401, "Unauthorized", {}, None
        )
        with self.assertRaises(EtoroApiError) as raised:
            self.client.get_default_watchlist()
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("public-test-key", str(raised.exception))
        self.assertNotIn("user-test-key", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
