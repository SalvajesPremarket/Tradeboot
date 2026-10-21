import json
import os
import unittest
from unittest.mock import patch

import etoro_execution as execution


class EtoroExecutionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ETORO_API_KEY": "public-key",
            "ETORO_USER_KEY": "user-key",
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
        with self.assertRaisesRegex(execution.EtoroExecutionError, "trading_mode_disabled"):
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
        self.assertIn("/trading/execution/demo/market-open-orders/by-amount", seen["url"])
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], {
            "InstrumentId": 123, "Amount": 25.0, "Leverage": 1,
            "IsBuy": True, "StopLossRate": 9.5,
        })

    def test_order_above_cap_is_rejected(self):
        with self.assertRaisesRegex(execution.EtoroExecutionError, "order_amount_outside_configured_limit"):
            execution.open_market_order(123, 101, True)

    def test_live_mode_requires_explicit_acknowledgement(self):
        os.environ["ETORO_TRADING_MODE"] = "real"
        os.environ.pop("ETORO_LIVE_TRADING_ACK", None)
        with self.assertRaisesRegex(execution.EtoroExecutionError, "live_trading_acknowledgement_required"):
            execution.open_market_order(123, 10, True)

    def test_close_uses_position_id_and_demo_path(self):
        seen = {}

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _):
                return b'{"accepted":true}'

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data.decode())
            return Response()

        with patch.object(execution, "urlopen", fake_urlopen):
            result = execution.close_market_position(4567)
        self.assertTrue(result["ok"])
        self.assertTrue(seen["url"].endswith("/trading/execution/demo/market-close-orders/positions/4567"))
        self.assertEqual(seen["body"], {"UnitsToDeduct": None})


if __name__ == "__main__":
    unittest.main()
