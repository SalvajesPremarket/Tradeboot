import json
import os
import unittest
from unittest.mock import patch

import etoro_execution as execution


class EtoroExecutionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ETORO_API_KEY": "public-key",
            "ETORO_DEMO_USER_KEY": "demo-user-key", "ETORO_REAL_USER_KEY": "real-user-key",
            "ETORO_TRADING_ENABLED": "true",
            "ETORO_TRADING_MODE": "demo",
            "ETORO_MAX_ORDER_AMOUNT_USD": "100",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_execution_disabled_by_default(self):
        os.environ.pop("ETORO_TRADING_ENABLED", None)
        os.environ.pop("ETORO_TRADING_MODE", None)
        with self.assertRaisesRegex(execution.EtoroExecutionError, "demo_mode_required"):
            execution.open_market_order(123, 10, True)

    def test_open_order_uses_demo_path_and_caps_amount(self):
        seen = {}

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _):
                return b'{"orderId":42}'

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["body"] = json.loads(request.data.decode())
            return Response()

        with patch.object(execution, "urlopen", fake_urlopen):
            result = execution.open_market_order(123, 25, True, stop_loss_rate=9.5)
        self.assertTrue(result["ok"])
        self.assertIn("/api/v2/trading/execution/demo/orders", seen["url"])
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], {
            "action": "open", "transaction": "buy", "instrumentId": 123,
            "orderType": "mkt", "amount": 25.0, "orderCurrency": "usd",
            "leverage": 1, "stopLossRate": 9.5,
        })

    def test_short_order_uses_sell_short_transaction(self):
        seen = {}

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _):
                return b'{"orderId":43}'

        def fake_urlopen(request, timeout):
            seen["body"] = json.loads(request.data.decode())
            return Response()

        with patch.object(execution, "urlopen", fake_urlopen):
            result = execution.open_market_order(123, 25, False)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["body"]["transaction"], "sellShort")

    def test_demo_reuses_existing_etoro_user_key(self):
        os.environ.pop("ETORO_DEMO_USER_KEY", None)
        os.environ["ETORO_USER_KEY"] = "existing-render-user-key"
        seen = {}

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _):
                return b'{"orderId":42}'

        def fake_urlopen(request, timeout):
            seen["user_key"] = request.get_header("X-user-key")
            seen["url"] = request.full_url
            return Response()

        with patch.object(execution, "urlopen", fake_urlopen):
            result = execution.open_market_order(123, 25, True)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["user_key"], "existing-render-user-key")
        self.assertIn("/api/v2/trading/execution/demo/orders", seen["url"])

    def test_order_above_cap_is_rejected(self):
        with self.assertRaisesRegex(execution.EtoroExecutionError, "order_amount_outside_configured_limit"):
            execution.open_market_order(123, 101, True)

    def test_real_money_mode_is_rejected_even_if_configured(self):
        os.environ["ETORO_TRADING_MODE"] = "real"
        os.environ["ETORO_LIVE_TRADING_ACK"] = execution.LIVE_ACK if hasattr(execution, "LIVE_ACK") else "I_UNDERSTAND_REAL_MONEY_RISK"
        with self.assertRaisesRegex(execution.EtoroExecutionError, "demo_mode_required"):
            execution.open_market_order(123, 10, True)

    def test_close_uses_documented_demo_v1_path_and_instrument_id(self):
        seen = {}

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _):
                return b'{"orderId":987}'

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data.decode())
            seen["method"] = request.get_method()
            return Response()

        with patch.object(execution, "urlopen", fake_urlopen):
            result = execution.close_demo_position(4567, 123)
        self.assertTrue(result["ok"])
        self.assertTrue(seen["url"].endswith("/api/v1/trading/execution/demo/market-close-orders/positions/4567"))
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], {"InstrumentID": 123, "UnitsToDeduct": None})
        self.assertTrue(result["confirmation_required"])

    def test_legacy_close_without_instrument_id_fails_closed(self):
        with self.assertRaisesRegex(execution.EtoroExecutionError, "close_requires_instrument_id"):
            execution.close_market_position(4567)

    def test_demo_portfolio_is_read_only(self):
        seen = {}

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _):
                return b'{"clientPortfolio":{}}'

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            return Response()

        with patch.object(execution, "urlopen", fake_urlopen):
            result = execution.get_demo_portfolio()
        self.assertTrue(result["ok"])
        self.assertEqual(seen["method"], "GET")
        self.assertTrue(seen["url"].endswith("/api/v1/trading/info/demo/portfolio"))


if __name__ == "__main__":
    unittest.main()
