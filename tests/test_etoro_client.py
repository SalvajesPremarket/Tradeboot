import unittest
from unittest.mock import Mock

import requests

from etoro_client import (
    EtoroAPIError,
    EtoroClient,
    EtoroConfig,
    EtoroConfigurationError,
)


class EtoroClientTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "api_key": "test-api-key",
            "user_key": "test-user-key",
            "mode": "demo",
            "timeout_seconds": 3.0,
            "live_trading_enabled": False,
        }
        values.update(overrides)
        return EtoroConfig(**values)

    def test_demo_status_never_enables_order_execution(self):
        client = EtoroClient(self.config())
        status = client.status()
        self.assertEqual(status["provider"], "eToro")
        self.assertEqual(status["mode"], "demo")
        self.assertFalse(status["order_execution_enabled"])
        self.assertFalse(status["live_trading_enabled"])

    def test_real_mode_locked_unless_explicitly_enabled(self):
        with self.assertRaises(EtoroConfigurationError):
            EtoroClient(self.config(mode="real", live_trading_enabled=False))

    def test_watchlist_request_uses_required_headers_and_timeout(self):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.json.return_value = [{"itemId": 123}]
        session.request.return_value = response
        client = EtoroClient(self.config(), session=session)

        result = client.get_default_watchlist()

        self.assertEqual(result, [{"itemId": 123}])
        args, kwargs = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].endswith("/watchlists/default-watchlists/items"))
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-api-key")
        self.assertEqual(kwargs["headers"]["x-user-key"], "test-user-key")
        self.assertTrue(kwargs["headers"]["x-request-id"])
        self.assertEqual(kwargs["timeout"], 3.0)

    def test_api_error_does_not_expose_credentials(self):
        session = Mock()
        response = Mock()
        response.status_code = 401
        session.request.return_value = response
        client = EtoroClient(self.config(), session=session)

        with self.assertRaises(EtoroAPIError) as caught:
            client.get_default_watchlist()

        self.assertIn("401", str(caught.exception))
        self.assertNotIn("test-api-key", str(caught.exception))
        self.assertNotIn("test-user-key", str(caught.exception))

    def test_network_error_is_sanitized(self):
        session = Mock()
        session.request.side_effect = requests.Timeout("secret details")
        client = EtoroClient(self.config(), session=session)

        with self.assertRaises(EtoroAPIError) as caught:
            client.get_account_pnl()

        self.assertNotIn("secret details", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
