"""Tests for strategy-aware paper self-test reporting."""
import unittest

from app import _strategy_activity_diagnostics


class PaperSelfTestStrategyReportingTests(unittest.TestCase):
    def test_pullback_short_is_reported_active_without_jeanlong(self):
        enabled = [{
            "id": "pullback-short-id",
            "name": "PULLBACK SHORT EMA50 o 200 D W",
            "strategyType": "python_program",
            "enabled": True,
        }]
        result = _strategy_activity_diagnostics(enabled, {"evaluations": 0, "program_evaluations": []})

        self.assertEqual([item["name"] for item in result["active_python_strategies"]],
                         ["PULLBACK SHORT EMA50 o 200 D W"])
        self.assertFalse(result["jeanlong_enabled"])
        self.assertEqual(result["evaluations_total"], 0)

    def test_python_evaluations_are_counted_per_strategy(self):
        enabled = [
            {"id": "short", "name": "Pullback Short", "strategyType": "python_program", "enabled": True},
            {"id": "long", "name": "Pullback Long", "strategyType": "python_program", "enabled": True},
            {"id": "off", "name": "Disabled", "strategyType": "python_program", "enabled": False},
        ]
        worker = {
            "evaluations": 2,
            "program_evaluations": [
                {"strategy_id": "short", "symbol": "AAA"},
                {"strategy_id": "short", "symbol": "BBB"},
                {"strategy_id": "long", "symbol": "CCC"},
            ],
        }
        result = _strategy_activity_diagnostics(enabled, worker)

        self.assertEqual(result["program_evaluations"], 3)
        self.assertEqual(result["evaluations_total"], 5)
        self.assertEqual(
            {item["id"]: item["evaluations"] for item in result["evaluations_by_strategy"]},
            {"short": 2, "long": 1},
        )


if __name__ == "__main__":
    unittest.main()
