import unittest

from strategy_program_engine import evaluate_strategy_replay


class StrategyProgramReplayTests(unittest.TestCase):
    def test_momentum_replay_processes_ticks_without_execution(self):
        result = evaluate_strategy_replay({
            "strategyId": "292a064d-149c-4187-a50a-1d75a3f7a4cd",
            "symbol": "TEST",
            "candles": [],
            "context": {},
            "ticks": [
                {"price": 10.0, "timestamp": "2026-10-21T14:30:01Z"},
                {"price": 10.1, "timestamp": "2026-10-21T14:30:02Z"},
            ],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["ticks_processed"], 2)
        self.assertEqual(result["execution"], "not_attempted")
        self.assertEqual(result["mode"], "simulation")

    def test_pullback_replay_requires_sizing_and_processes_tick(self):
        result = evaluate_strategy_replay({
            "strategyId": "19f003ba-03eb-4769-89f3-4ed0fb190110",
            "symbol": "TEST",
            "capital": 1000,
            "dailyPct": 1,
            "candles": [],
            "context": [],
            "ticks": [{"price": 10.0, "timestamp": "2026-10-21T14:30:01Z"}],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["ticks_processed"], 1)
        self.assertEqual(result["execution"], "not_attempted")

    def test_invalid_program_and_non_numeric_price_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown_trusted_strategy_program"):
            evaluate_strategy_replay({
                "strategyId": "not-allowlisted", "symbol": "TEST",
                "ticks": [{"price": 1, "timestamp": "2026-10-21T14:30:01Z"}],
            })
        with self.assertRaisesRegex(ValueError, "tick_price_must_be_finite_number"):
            evaluate_strategy_replay({
                "strategyId": "292a064d-149c-4187-a50a-1d75a3f7a4cd",
                "symbol": "TEST",
                "ticks": [{"price": True, "timestamp": "2026-10-21T14:30:01Z"}],
            })


if __name__ == "__main__":
    unittest.main()
