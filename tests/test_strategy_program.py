import unittest

from strategy_formula_v2 import StrategyFormulaError
from strategy_program import validate_strategy_source


class StrategyProgramValidationTests(unittest.TestCase):
    def test_accepts_complete_class_based_program_without_executing_it(self):
        source = '''"""Momentum sample"""
import math
class MomentumTrader:
    def __init__(self):
        self.price = 1
    def on_tick(self, price):
        self.price = price
        return {"signal": "HOLD"}
'''
        result = validate_strategy_source(source)
        self.assertTrue(result["valid"])
        self.assertFalse(result["executable"])
        self.assertEqual(result["syntax"], "python-program")

    def test_reports_line_and_column_for_syntax_error(self):
        with self.assertRaisesRegex(StrategyFormulaError, "línea 2, columna"):
            validate_strategy_source("class Trader:\n    x = 1.2.3")

    def test_rejects_source_without_class_or_function(self):
        with self.assertRaisesRegex(StrategyFormulaError, "No se detectó"):
            validate_strategy_source("price = 1")

    def test_rejects_empty_source(self):
        with self.assertRaisesRegex(StrategyFormulaError, "programa Python completo"):
            validate_strategy_source("  ")

    def test_rejects_source_over_limit(self):
        with self.assertRaisesRegex(StrategyFormulaError, "50000 caracteres"):
            validate_strategy_source("class A:\n    pass\n" + " " * 50001)


if __name__ == "__main__":
    unittest.main()
