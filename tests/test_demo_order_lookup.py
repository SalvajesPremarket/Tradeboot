import json
import os
import unittest
from unittest.mock import patch

import etoro_execution as execution


class DemoOrderLookupTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ETORO_API_KEY": "public-key",
            "ETORO_DEMO_USER_KEY": "demo-user-key",
            "ETORO_TRADING_MODE": "demo",
            "ETORO_TRADING_ENABLED": "false",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_lookup_reads_order_status_without_enabling_trading(self):
        seen = {}

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _): return b'{"orderId":42,"status":{"id":3,"name":"Filled"},"positionExecutions":[{"positionId":9001}]}'

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["body"] = request.data
            return Response()

        with patch.object(execution, "urlopen", fake_urlopen):
            result = execution.get_demo_order_information(42)
        self.assertTrue(result["ok"])
        self.assertIn("/api/v2/trading/info/demo/orders:lookup?orderId=42", seen["url"])
        self.assertEqual(seen["method"], "GET")
        self.assertIsNone(seen["body"])
        self.assertEqual(result["data"]["status"]["name"], "Filled")

    def test_invalid_order_ids_fail_before_network(self):
        with patch.object(execution, "urlopen") as urlopen:
            for value in (0, -1, True, "42"):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(execution.EtoroExecutionError, "invalid_order_id"):
                        execution.get_demo_order_information(value)
            urlopen.assert_not_called()

    def test_lookup_is_demo_only(self):
        os.environ["ETORO_TRADING_MODE"] = "real"
        with patch.object(execution, "urlopen") as urlopen:
            with self.assertRaisesRegex(execution.EtoroExecutionError, "demo_mode_required"):
                execution.get_demo_order_information(42)
            urlopen.assert_not_called()

    def test_lookup_sanitizes_http_errors(self):
        from urllib.error import HTTPError
        with patch.object(execution, "urlopen", side_effect=HTTPError(
                "https://public-api.etoro.com/api/v2/trading/info/demo/orders:lookup?orderId=42",
                404, "Not Found", {}, None)):
            with self.assertRaisesRegex(execution.EtoroExecutionError, "etoro_order_lookup_http_404"):
                execution.get_demo_order_information(42)


if __name__ == "__main__":
    unittest.main()
