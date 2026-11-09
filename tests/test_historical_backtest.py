import unittest

from historical_backtest import apply_execution_costs, calculate_performance_metrics


class HistoricalBacktestMetricsTests(unittest.TestCase):
    def test_metrics_use_daily_equity_for_sharpe_and_drawdown(self):
        trades = [
            {"net_pnl_usd": 100},
            {"net_pnl_usd": -50},
            {"net_pnl_usd": 25},
        ]
        curve = [
            {"date": "2024-01-02", "equity": 1100},
            {"date": "2024-01-03", "equity": 1050},
            {"date": "2024-01-04", "equity": 1075},
        ]
        result = calculate_performance_metrics(trades, curve, 1000)
        self.assertEqual(result["trade_count"], 3)
        self.assertAlmostEqual(result["profit_factor"], 2.5)
        self.assertAlmostEqual(result["net_profit_usd"], 75)
        self.assertAlmostEqual(result["max_drawdown_usd"], 50)
        self.assertAlmostEqual(result["max_drawdown_pct"], 100 * 50 / 1100)
        self.assertIsNotNone(result["sharpe_ratio"])

    def test_sharpe_is_not_fabricated_without_daily_equity(self):
        result = calculate_performance_metrics([{"net_pnl_usd": 10}], [], 1000)
        self.assertIsNone(result["sharpe_ratio"])
        self.assertEqual(result["sharpe_status"], "requires_at_least_two_nonconstant_daily_returns")

    def test_profit_factor_undefined_without_losses(self):
        result = calculate_performance_metrics([{"net_pnl_usd": 10}], [], 1000)
        self.assertIsNone(result["profit_factor"])

    def test_long_and_short_execution_costs(self):
        long_trade = apply_execution_costs(
            {"side": "LONG", "entry_price": 10, "exit_price": 11, "quantity": 10},
            slippage_bps=10, commission_per_share=0.01,
        )
        short_trade = apply_execution_costs(
            {"side": "SHORT", "entry_price": 10, "exit_price": 9, "quantity": 10},
            slippage_bps=10, commission_per_share=0.01,
        )
        self.assertLess(long_trade["net_pnl_usd"], 10)
        self.assertLess(short_trade["net_pnl_usd"], 10)

    def test_rejects_duplicate_daily_equity_dates(self):
        with self.assertRaises(ValueError):
            calculate_performance_metrics(
                [], [{"date": "2024-01-02", "equity": 1000},
                     {"date": "2024-01-02", "equity": 1010}], 1000
            )


if __name__ == "__main__":
    unittest.main()
