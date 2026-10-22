"""Regression tests for the reviewed, simulation-only Python strategy adapter."""
import inspect
import unittest
from datetime import datetime, timedelta, timezone

from strategy_program_engine import _REGISTRY, is_supported_program, simulate_program


class StrategyProgramEngineTests(unittest.TestCase):
    def _payload(self, name, module, kind):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        candles = []
        for i in range(220):
            p = 8.0 + i * 0.002
            candles.append({"open": p, "high": p + 0.03, "low": p - 0.03, "close": p})
        ticks = [
            {"price": 8.45, "timestamp": (now + timedelta(seconds=s)).isoformat(), "day_volume": 600000}
            for s in (0, 5, 10)
        ]
        context = {
            "prev_close": 8, "avg_premarket_volume_30d": 100000,
            "avg_volume_30d": 600000, "volume_today": 600000,
            "float_shares": 10000000,
            "news": [{"published_at": (now - timedelta(minutes=10)).isoformat(),
                      "headline": "Company reports quarterly earnings"}],
        }
        payload = {"strategyName": name, "formula": inspect.getsource(module),
                   "symbol": "SIM", "capital": 10000, "candles": candles,
                   "ticks": ticks, "context": context}
        if kind == "pullback":
            daily = []
            for i in range(400):
                stamp = now - timedelta(days=399-i)
                p = 8.0 + i * 0.002
                daily.append({"timestamp": stamp.isoformat(), "open": p, "high": p + 0.08,
                              "low": p - 0.08, "close": p, "volume": 600000})
            payload["daily_candles"] = daily
        return payload

    def test_only_exact_reviewed_sources_are_accepted(self):
        for name, (module, _class_name, kind) in _REGISTRY.items():
            source = inspect.getsource(module)
            self.assertTrue(is_supported_program(name, source))
            self.assertFalse(is_supported_program(name, source + "\n# modified"))
            self.assertFalse(is_supported_program("untrusted-program", source))

    def test_all_four_reviewed_engines_run_in_simulation_only_mode(self):
        for name, (module, _class_name, kind) in _REGISTRY.items():
            with self.subTest(strategy=name):
                result = simulate_program(self._payload(name, module, kind))
                self.assertTrue(result["ok"])
                self.assertEqual(result["mode"], "simulation_only")
                self.assertFalse(result["real_orders_enabled"])
                self.assertEqual(result["ticks_processed"], 3)
                self.assertIsInstance(result["actions"], list)
                self.assertTrue(all(isinstance(action.get("event_at"), str) for action in result["actions"]))

    def test_modified_python_source_is_never_executed(self):
        module, _class_name, kind = _REGISTRY["JeanLong"]
        payload = self._payload("JeanLong", module, kind)
        payload["formula"] += "\nraise RuntimeError('must not run')"
        with self.assertRaises(ValueError):
            simulate_program(payload)


if __name__ == "__main__":
    unittest.main()
