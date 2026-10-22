import unittest
from unittest.mock import MagicMock, patch

import demo_execution_store


class DemoExecutionProviderIdTests(unittest.TestCase):
    @patch("demo_execution_store._connect")
    def test_finish_persists_provider_order_id_without_confirming_fill(self, connect):
        conn = MagicMock()
        connect.return_value.__enter__.return_value = conn
        demo_execution_store.finish_demo_execution("key-1", "submitted", 98765)
        self.assertIn("ADD COLUMN IF NOT EXISTS provider_order_id", conn.execute.call_args_list[0].args[0])
        update_sql, params = conn.execute.call_args_list[1].args
        self.assertIn("provider_order_id=COALESCE", update_sql)
        self.assertEqual(params[0], "submitted")
        self.assertEqual(params[1], "98765")

    @patch("demo_execution_store._connect")
    def test_ledger_read_exposes_provider_order_id_as_unverified(self, connect):
        conn = MagicMock()
        connect.return_value.__enter__.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            ("key-1", "s1", "ABC", "LONG", 123, 25.0, "submitted",
             MagicMock(isoformat=lambda: "2026-10-21T12:00:00+00:00"),
             MagicMock(isoformat=lambda: "2026-10-21T12:00:01+00:00"), "98765")
        ]
        rows = demo_execution_store.list_demo_executions(10)
        self.assertEqual(rows[0]["provider_order_id"], "98765")
        self.assertEqual(rows[0]["broker_confirmation"], "not_verified")

    def test_rejects_invalid_provider_order_id(self):
        with self.assertRaisesRegex(ValueError, "provider_order_id_invalid"):
            demo_execution_store.finish_demo_execution("key-1", "submitted", True)


if __name__ == "__main__":
    unittest.main()
