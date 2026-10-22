import unittest
from unittest.mock import patch

from schwab_integration import _new_state, _valid_state


class SchwabOAuthStateTests(unittest.TestCase):
    def test_state_round_trip(self):
        with patch("schwab_integration.time.time", return_value=1_800_000_000):
            state = _new_state("test-client-secret")
            self.assertTrue(_valid_state(state, "test-client-secret"))

    def test_state_rejects_wrong_secret(self):
        with patch("schwab_integration.time.time", return_value=1_800_000_000):
            state = _new_state("test-client-secret")
            self.assertFalse(_valid_state(state, "different-secret"))

    def test_state_rejects_expired_value(self):
        with patch("schwab_integration.time.time", return_value=1_800_000_000):
            state = _new_state("test-client-secret")
        with patch("schwab_integration.time.time", return_value=1_800_000_700):
            self.assertFalse(_valid_state(state, "test-client-secret"))


if __name__ == "__main__":
    unittest.main()
