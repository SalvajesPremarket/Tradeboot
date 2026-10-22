import unittest
from unittest.mock import MagicMock, patch

import demo_execution_store


class DemoScanHistoryTests(unittest.TestCase):
    @patch("demo_execution_store._connect")
    def test_record_cycle_persists_json_and_prunes_old_rows(self, connect):
        conn = MagicMock()
        connect.return_value.__enter__.return_value = conn
        demo_execution_store.record_demo_scan_cycle(
            "Test strategy", "2026-10-21T14:00:00+00:00",
            [{"symbol": "ABC", "signal_matched": True, "execution": "not_attempted"}],
        )
        self.assertEqual(conn.execute.call_count, 3)
        insert_sql = conn.execute.call_args_list[1].args[0]
        self.assertIn("INSERT INTO tradeboot_demo_scan_cycles", insert_sql)
        self.assertIn('"signal_matched":true', conn.execute.call_args_list[1].args[2])

    @patch("demo_execution_store._connect")
    def test_list_recent_cycles(self, connect):
        conn = MagicMock()
        connect.return_value.__enter__.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            (4, "Test", MagicMock(isoformat=lambda: "2026-10-21T14:00:00+00:00"),
             [{"symbol": "ABC"}], MagicMock(isoformat=lambda: "2026-10-21T14:00:01+00:00"))
        ]
        rows = demo_execution_store.list_demo_scan_cycles(10)
        self.assertEqual(rows[0]["id"], 4)
        self.assertEqual(rows[0]["results"], [{"symbol": "ABC"}])
        self.assertEqual(rows[0]["strategy"], "Test")

    def test_rejects_invalid_history_limits(self):
        for limit in (0, 201, True, "10"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    demo_execution_store.list_demo_scan_cycles(limit)

    def test_rejects_oversized_scan_cycle(self):
        with self.assertRaises(ValueError):
            demo_execution_store.record_demo_scan_cycle("Test", "2026-10-21T14:00:00Z", [{}] * 6)


if __name__ == "__main__":
    unittest.main()
