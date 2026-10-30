"""Isolated JeanLong signal and paper-trade test; never writes to the live paper ledger."""
import inspect
import math
import unittest
from datetime import datetime, timedelta, timezone

from strategy_program_engine import simulate_program
from strategy_programs import jean_long


class JeanLongControlledPaperSimulationTests(unittest.TestCase):
    def _payload(self, premarket_gain=8.0):
        start = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)  # 08:00 New York
        candles = [
            {"open": 8.0, "high": 8.0, "low": 8.0, "close": 8.0}
            for _ in range(220)
        ]
        # Dragonfly setup: open -> dip below open -> recover to open -> hit initial stop.
        prices = ((0, 8.45), (5, 8.44), (10, 8.45), (15, 8.43))
        ticks = [
            {
                "price": price,
                "timestamp": (start + timedelta(seconds=second)).isoformat(),
                "day_volume": 500000,
            }
            for second, price in prices
        ]
        return {
            "strategyName": "JeanLong",
            "formula": inspect.getsource(jean_long),
            "symbol": "SIM",
            "capital": 1000,
            "candles": candles,
            "ticks": ticks,
            "context": {
                "prev_close": 8.0,
                "premarket_change_pct": premarket_gain,
                "avg_volume_30d": 100000,
                "volume_today": 500000,
                "float_shares": 10000000,
                "news": [{
                    "published_at": (start - timedelta(minutes=10)).isoformat(),
                    "headline": "Company reports quarterly earnings beat",
                }],
            },
        }

    def test_jeanlong_accepts_qualifying_historical_setup_and_rejects_below_gap_min(self):
        accepted = simulate_program(self._payload(8.0))
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["mode"], "simulation_only")
        self.assertFalse(accepted["real_orders_enabled"])
        self.assertEqual([a["action"] for a in accepted["actions"]], ["ENTER", "EXIT"])
        self.assertEqual(accepted["actions"][0]["reason"], "libelula en tramo 1 + filtros OK")

        rejected = simulate_program(self._payload(4.9))
        self.assertTrue(rejected["ok"])
        self.assertEqual(rejected["actions"], [])
        self.assertTrue(
            any("ganancia pre-market 4.9%" in reason
                for reason in rejected["diagnostics"]["last_reject_reasons"])
        )
        print(
            "JEANLONG_SIGNAL_TEST="
            f"accepted_actions={[a['action'] for a in accepted['actions']]}; "
            f"rejected_reasons={rejected['diagnostics']['last_reject_reasons']}"
        )

    def test_isolated_paper_trade_entry_stop_exit_and_pnl_without_database_writes(self):
        result = simulate_program(self._payload(8.0))
        self.assertFalse(result["real_orders_enabled"])
        entry, exit_ = result["actions"]
        self.assertEqual(entry["action"], "ENTER")
        self.assertEqual(exit_["action"], "EXIT")
        self.assertEqual(exit_["reason"], "stop")
        self.assertLessEqual(exit_["price"], entry["stop"])

        # Isolated harness budget: $1,000; whole shares only. This does not
        # alter production sizing or insert a row into the regular paper ledger.
        quantity = math.floor(1000 / entry["price"])
        pnl_per_share = exit_["pnl_por_accion"]
        pnl_usd = round(pnl_per_share * quantity, 2)
        self.assertEqual(quantity, 118)
        self.assertEqual(pnl_per_share, -0.02)
        self.assertEqual(pnl_usd, -2.36)
        print(
            "ISOLATED_PAPER_TRADE="
            f"symbol=SIM; quantity={quantity}; entry={entry['price']}; "
            f"stop={entry['stop']}; exit={exit_['price']}; "
            f"reason={exit_['reason']}; pnl_usd={pnl_usd}; "
            "database_writes=0; real_orders_enabled=false"
        )


if __name__ == "__main__":
    unittest.main()
