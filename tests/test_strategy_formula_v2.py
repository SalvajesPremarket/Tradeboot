import unittest

from strategy_formula_v2 import StrategyFormulaError, evaluate_formula, validate_formula


class SafeFormulaTests(unittest.TestCase):
    def test_legacy_expression(self):
        result = evaluate_formula("price > 5 and change_pct >= 2", {"price": 7, "change_pct": 3})
        self.assertTrue(result["matched"])
        self.assertEqual(result["variables"], ["change_pct", "price"])

    def test_multiline_assignment(self):
        result = evaluate_formula("entry = price > 5 and rvol >= 2\nentry and change_pct > 1",
                                  {"price": 7, "rvol": 3, "change_pct": 2})
        self.assertTrue(result["matched"])

    def test_if_else(self):
        formula = "if price > 5:\n    result = rvol >= 2\nelse:\n    result = False\nresult"
        self.assertTrue(evaluate_formula(formula, {"price": 7, "rvol": 3})["matched"])

    def test_missing_market_data_fails_closed(self):
        with self.assertRaisesRegex(StrategyFormulaError, "Missing value"):
            evaluate_formula("ema20 > price", {"price": 7})

    def test_unknown_identifier_rejected(self):
        with self.assertRaisesRegex(StrategyFormulaError, "Unsupported variable"):
            validate_formula("made_up_filter > 1")

    def test_import_rejected(self):
        with self.assertRaises(StrategyFormulaError):
            validate_formula("import os\nprice > 5")

    def test_attribute_access_rejected(self):
        with self.assertRaises(StrategyFormulaError):
            validate_formula("price.real > 5")

    def test_division_by_zero_rejected(self):
        with self.assertRaisesRegex(StrategyFormulaError, "dividir entre cero"):
            evaluate_formula("price / 0 > 1", {"price": 5})


if __name__ == "__main__":
    unittest.main()
