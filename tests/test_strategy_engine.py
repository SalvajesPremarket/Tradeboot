import unittest

from strategy_engine import StrategyFormulaError, evaluate_formula


class StrategyFormulaTests(unittest.TestCase):
    def test_simple_condition_matches(self):
        result = evaluate_formula("price > 10 and change_pct >= 2", {"price": 12, "change_pct": 2})
        self.assertEqual(result, {"valid": True, "matched": True, "variables": ["change_pct", "price"]})

    def test_condition_can_be_false(self):
        self.assertFalse(evaluate_formula("price > 10", {"price": 8})["matched"])

    def test_arithmetic_is_supported(self):
        self.assertTrue(evaluate_formula("price >= bid + 0.05", {"price": 10.1, "bid": 10})["matched"])

    def test_unknown_variable_is_rejected(self):
        with self.assertRaisesRegex(StrategyFormulaError, "Unsupported variable"):
            evaluate_formula("account_balance > 100", {"account_balance": 200})

    def test_arbitrary_python_is_rejected(self):
        with self.assertRaises(StrategyFormulaError):
            evaluate_formula("__import__('os').system('id')", {})

    def test_missing_value_is_rejected(self):
        with self.assertRaisesRegex(StrategyFormulaError, "Missing value"):
            evaluate_formula("price > 10", {})

    def test_division_by_zero_is_rejected(self):
        with self.assertRaises(StrategyFormulaError):
            evaluate_formula("price / 0 > 2", {"price": 10})

    def test_formula_must_return_boolean(self):
        with self.assertRaisesRegex(StrategyFormulaError, "must return TRUE/FALSE"):
            evaluate_formula("price + 2", {"price": 10})


if __name__ == "__main__":
    unittest.main()
