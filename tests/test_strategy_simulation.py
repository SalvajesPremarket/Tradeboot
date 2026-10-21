import unittest

from strategy_simulation import simulate_momentum


class MomentumSimulationTests(unittest.TestCase):
    def test_simulation_emits_entry_signal_without_live_execution(self):
        candles = [{"open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0} for _ in range(200)]
        candles += [
            {"open": 2.0 + i * 0.01, "high": 2.0 + i * 0.01,
             "low": 2.0 + i * 0.01, "close": 2.0 + i * 0.01}
            for i in range(1, 21)
        ]
        result = simulate_momentum({
            "symbol": "ABCD",
            "side": "LONG",
            "config": {"use_scanner_filters": False, "max_prior_streak": 100, "min_dip_pct": 0.05},
            "candles": candles,
            "ticks": [
                {"price": 2.2, "timestamp": "2026-01-01T09:30:00"},
                {"price": 2.197, "timestamp": "2026-01-01T09:30:05"},
                {"price": 2.2, "timestamp": "2026-01-01T09:30:10"},
            ],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "simulation_only")
        self.assertEqual([item["action"] for item in result["actions"]], ["ENTER"])
        self.assertEqual(result["actions"][0]["side"], "LONG")
        self.assertIn("No broker connection", result["note"])

    def test_rejects_missing_ticks(self):
        with self.assertRaisesRegex(ValueError, "ticks must be a non-empty list"):
            simulate_momentum({"symbol": "ABCD", "ticks": []})

    def test_rejects_oversized_tick_payload(self):
        with self.assertRaisesRegex(ValueError, "at most 5000"):
            simulate_momentum({"symbol": "ABCD", "ticks": [{"price": 2, "timestamp": "2026-01-01T09:30:00"}] * 5001})

    def test_rejects_invalid_timestamp(self):
        with self.assertRaisesRegex(ValueError, "ISO-8601"):
            simulate_momentum({"symbol": "ABCD", "ticks": [{"price": 2, "timestamp": "yesterday"}]})


    def test_stop_triggers_simulated_exit_and_clears_position(self):
        candles = [{"open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0} for _ in range(200)]
        candles += [
            {"open": 2.0 + i * 0.01, "high": 2.0 + i * 0.01,
             "low": 2.0 + i * 0.01, "close": 2.0 + i * 0.01}
            for i in range(1, 21)
        ]
        result = simulate_momentum({
            "symbol": "ABCD",
            "side": "LONG",
            "config": {"max_prior_streak": 100},
            "candles": candles,
            "ticks": [
                {"price": 2.2, "timestamp": "2026-01-01T09:30:00"},
                {"price": 2.197, "timestamp": "2026-01-01T09:30:05"},
                {"price": 2.2, "timestamp": "2026-01-01T09:30:10"},
                {"price": 2.19, "timestamp": "2026-01-01T09:30:15"},
            ],
            "context": {
                "prev_close": 2.0,
                "avg_premarket_volume_30d": 100000,
                "volume_today": 600000,
                "float_shares": 10000000,
                "news": [{
                    "published_at": "2026-01-01T09:20:00",
                    "headline": "Company reports quarterly earnings",
                }],
            },
        })
        self.assertEqual([item["action"] for item in result["actions"]], ["ENTER", "EXIT"])
        self.assertEqual(result["actions"][-1]["reason"], "stop")
        self.assertIsNone(result["open_simulated_position"])


if __name__ == "__main__":
    unittest.main()
