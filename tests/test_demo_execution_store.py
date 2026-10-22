import unittest
from unittest.mock import patch

import demo_execution_store as ledger


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


if __name__ == "__main__":
    unittest.main()
