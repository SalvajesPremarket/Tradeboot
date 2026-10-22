import math
import unittest

from app import demo_allocation_cap


class DemoRiskLimitTests(unittest.TestCase):
    def test_allocation_cap_is_percentage_of_general_capital(self):
        self.assertEqual(demo_allocation_cap(1000, 10), 100)
        self.assertEqual(demo_allocation_cap(250, 20), 50)

    def test_zero_capital_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "valid_general_capital_required"):
            demo_allocation_cap(0, 10)

    def test_invalid_allocation_is_rejected(self):
        for value in (0, -1, 100.1, True, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "valid_strategy_allocation_required"):
                    demo_allocation_cap(1000, value)

    def test_invalid_capital_is_rejected(self):
        for value in (-1, True, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "valid_general_capital_required"):
                    demo_allocation_cap(value, 10)


if __name__ == "__main__":
    unittest.main()
