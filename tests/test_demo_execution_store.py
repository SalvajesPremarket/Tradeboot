import unittest
from unittest.mock import patch

import demo_execution_store as ledger


class FakeCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConnection:
    def __init__(self, inserted=None, existing=None):
        self.inserted = inserted
        self.existing = existing
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return FakeTransaction()

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "INSERT INTO tradeboot_demo_executions" in sql:
            return FakeCursor(self.inserted)
        if "SELECT strategy_id,symbol,side,instrument_id,amount_usd,status" in sql:
            return FakeCursor(self.existing)
        return FakeCursor()


class DemoExecutionLedgerTests(unittest.TestCase):
    def test_rejects_empty_or_overlong_key_before_database_access(self):
        with patch.object(ledger, "_connect") as connect:
            with self.assertRaisesRegex(ValueError, "idempotency_key_invalid"):
                ledger.reserve_demo_execution("", "strategy", "AAPL", "LONG", 123, 10)
            with self.assertRaisesRegex(ValueError, "idempotency_key_invalid"):
                ledger.reserve_demo_execution("x" * 161, "strategy", "AAPL", "LONG", 123, 10)
            connect.assert_not_called()

    def test_finish_accepts_only_known_states(self):
        with patch.object(ledger, "_connect") as connect:
            with self.assertRaisesRegex(ValueError, "execution_status_invalid"):
                ledger.finish_demo_execution("request-1", "filled")
            connect.assert_not_called()

    def test_new_key_is_reserved_as_pending(self):
        connection = FakeConnection(inserted=("key-1",))
        with patch.object(ledger, "_connect", return_value=connection):
            result = ledger.reserve_demo_execution("key-1", "strategy", "AAPL", "LONG", 123, 10)
        self.assertEqual(result, {"reserved": True, "status": "pending"})
        self.assertTrue(any("ON CONFLICT (idempotency_key) DO NOTHING" in sql for sql, _ in connection.calls))

    def test_duplicate_same_request_is_not_reserved_twice(self):
        connection = FakeConnection(existing=("strategy", "AAPL", "LONG", 123, 10.0, "submitted"))
        with patch.object(ledger, "_connect", return_value=connection):
            result = ledger.reserve_demo_execution("key-1", "strategy", "AAPL", "LONG", 123, 10)
        self.assertEqual(result, {"reserved": False, "same_request": True, "status": "submitted"})

    def test_duplicate_key_with_different_request_is_flagged(self):
        connection = FakeConnection(existing=("strategy", "AAPL", "LONG", 123, 10.0, "unknown"))
        with patch.object(ledger, "_connect", return_value=connection):
            result = ledger.reserve_demo_execution("key-1", "strategy", "AAPL", "LONG", 123, 25)
        self.assertEqual(result, {"reserved": False, "same_request": False, "status": "unknown"})

    def test_finish_updates_status_and_timestamp(self):
        connection = FakeConnection()
        with patch.object(ledger, "_connect", return_value=connection):
            ledger.finish_demo_execution("key-1", "submitted")
        update = next((sql, params) for sql, params in connection.calls if "UPDATE tradeboot_demo_executions" in sql)
        self.assertIn("status=%s", update[0])
        self.assertEqual(update[1][0], "submitted")
        self.assertEqual(update[1][2], "key-1")

    def test_list_executions_rejects_invalid_limit_before_database_access(self):
        with patch.object(ledger, "_connect") as connect:
            with self.assertRaisesRegex(ValueError, "execution_limit_invalid"):
                ledger.list_demo_executions(0)
            with self.assertRaisesRegex(ValueError, "execution_limit_invalid"):
                ledger.list_demo_executions(501)
            connect.assert_not_called()

    def test_list_executions_returns_recent_rows(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        row = ("key-1", "strategy", "AAPL", "LONG", 123, 10.0, "pending", now, now)

        class ListingConnection(FakeConnection):
            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                if "SELECT idempotency_key,strategy_id,symbol,side,instrument_id" in sql:
                    return FakeCursor([row])
                return FakeCursor()

        connection = ListingConnection()
        with patch.object(ledger, "_connect", return_value=connection):
            result = ledger.list_demo_executions(10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["idempotency_key"], "key-1")
        self.assertEqual(result[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
