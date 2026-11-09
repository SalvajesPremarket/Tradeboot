"""Deterministic performance analytics for isolated historical backtests.

This module never connects to the paper ledger or submits broker orders. Strategy
adapters supply closed trades and a daily mark-to-market equity curve.
"""
from datetime import date, datetime
import math


def _finite(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be numeric.") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite.")
    return number


def _date_value(value, field):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                pass
    raise ValueError(f"{field} must be an ISO date or datetime.")


def calculate_performance_metrics(trades, equity_curve, initial_capital, *,
                                  risk_free_rate=0.0, periods_per_year=252):
    """Calculate trade and portfolio metrics from net closed trades and daily equity.

    Sharpe is deliberately unavailable unless at least two daily equity returns
    exist; trade-by-trade returns are not substituted for daily returns.
    """
    capital = _finite(initial_capital, "initial_capital")
    rf = _finite(risk_free_rate, "risk_free_rate")
    if capital <= 0:
        raise ValueError("initial_capital must be positive.")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year < 1:
        raise ValueError("periods_per_year must be a positive integer.")
    if not isinstance(trades, list) or not isinstance(equity_curve, list):
        raise ValueError("trades and equity_curve must be lists.")

    clean_trades = []
    for index, trade in enumerate(trades):
        if not isinstance(trade, dict):
            raise ValueError(f"trades[{index}] must be an object.")
        pnl = _finite(trade.get("net_pnl_usd"), f"trades[{index}].net_pnl_usd")
        clean_trades.append({**trade, "net_pnl_usd": pnl})

    clean_curve = []
    for index, point in enumerate(equity_curve):
        if not isinstance(point, dict):
            raise ValueError(f"equity_curve[{index}] must be an object.")
        when = _date_value(point.get("date") or point.get("timestamp"), f"equity_curve[{index}].date")
        equity = _finite(point.get("equity"), f"equity_curve[{index}].equity")
        if equity <= 0:
            raise ValueError(f"equity_curve[{index}].equity must be positive.")
        clean_curve.append((when, equity))
    clean_curve.sort(key=lambda item: item[0])
    if any(clean_curve[i][0] == clean_curve[i - 1][0] for i in range(1, len(clean_curve))):
        raise ValueError("equity_curve must contain at most one end-of-day value per date.")

    wins = [t["net_pnl_usd"] for t in clean_trades if t["net_pnl_usd"] > 0]
    losses = [t["net_pnl_usd"] for t in clean_trades if t["net_pnl_usd"] < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_profit = sum(t["net_pnl_usd"] for t in clean_trades)

    peak = capital
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    returns = []
    previous_equity = capital
    for _, equity in clean_curve:
        peak = max(peak, equity)
        drawdown = peak - equity
        drawdown_pct = drawdown / peak if peak else 0.0
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
        returns.append(equity / previous_equity - 1.0)
        previous_equity = equity

    sharpe = None
    annualized_volatility = None
    if len(returns) >= 2:
        daily_rf = (1.0 + rf) ** (1.0 / periods_per_year) - 1.0
        excess = [value - daily_rf for value in returns]
        mean = sum(excess) / len(excess)
        variance = sum((value - mean) ** 2 for value in excess) / (len(excess) - 1)
        deviation = math.sqrt(variance)
        if deviation > 0:
            sharpe = mean / deviation * math.sqrt(periods_per_year)
            annualized_volatility = math.sqrt(variance + daily_rf * 0) * math.sqrt(periods_per_year)

    final_equity = clean_curve[-1][1] if clean_curve else capital + net_profit
    elapsed_years = ((clean_curve[-1][0] - clean_curve[0][0]).days / 365.25) if len(clean_curve) > 1 else 0.0
    cagr = ((final_equity / capital) ** (1.0 / elapsed_years) - 1.0) if elapsed_years > 0 and final_equity > 0 else None

    return {
        "trade_count": len(clean_trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": (len(wins) / len(clean_trades) * 100.0) if clean_trades else None,
        "gross_profit_usd": gross_profit,
        "gross_loss_usd": gross_loss,
        "net_profit_usd": net_profit,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (None if gross_profit == 0 else None),
        "average_win_usd": (gross_profit / len(wins)) if wins else None,
        "average_loss_usd": (sum(losses) / len(losses)) if losses else None,
        "expectancy_per_trade_usd": (net_profit / len(clean_trades)) if clean_trades else None,
        "initial_capital_usd": capital,
        "final_equity_usd": final_equity,
        "total_return_pct": (final_equity / capital - 1.0) * 100.0,
        "cagr_pct": cagr * 100.0 if cagr is not None else None,
        "sharpe_ratio": sharpe,
        "annualized_volatility_pct": annualized_volatility * 100.0 if annualized_volatility is not None else None,
        "max_drawdown_usd": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct * 100.0,
        "equity_observations": len(clean_curve),
        "sharpe_status": "ok" if sharpe is not None else "requires_at_least_two_nonconstant_daily_returns",
        "drawdown_basis": "daily_mark_to_market_equity" if clean_curve else "no_equity_curve",
    }


def apply_execution_costs(trade, *, slippage_bps=0.0, commission_per_share=0.0):
    """Return one LONG/SHORT trade with conservative entry/exit slippage and fees."""
    if not isinstance(trade, dict):
        raise ValueError("trade must be an object.")
    side = str(trade.get("side", "")).upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT.")
    entry = _finite(trade.get("entry_price"), "entry_price")
    exit_price = _finite(trade.get("exit_price"), "exit_price")
    quantity = _finite(trade.get("quantity"), "quantity")
    bps = _finite(slippage_bps, "slippage_bps")
    fee = _finite(commission_per_share, "commission_per_share")
    if min(entry, exit_price, quantity) <= 0 or bps < 0 or fee < 0 or bps >= 10000:
        raise ValueError("Prices/quantity must be positive and costs nonnegative.")
    slip = bps / 10000.0
    if side == "LONG":
        effective_entry, effective_exit = entry * (1 + slip), exit_price * (1 - slip)
        gross = (effective_exit - effective_entry) * quantity
    else:
        effective_entry, effective_exit = entry * (1 - slip), exit_price * (1 + slip)
        gross = (effective_entry - effective_exit) * quantity
    commissions = 2 * fee * quantity
    return {
        **trade,
        "effective_entry_price": effective_entry,
        "effective_exit_price": effective_exit,
        "gross_pnl_usd": gross,
        "commissions_usd": commissions,
        "net_pnl_usd": gross - commissions,
    }


class BacktestEngine:
    """Isolated execution-cost and portfolio-metrics engine.

    It accepts strategy-generated closed trades plus a daily mark-to-market
    equity curve. It does not load, mutate, or reuse the production paper ledger.
    """

    def __init__(self, initial_capital, *, slippage_bps=0.0, commission_per_share=0.0,
                 risk_free_rate=0.0, periods_per_year=252):
        self.initial_capital = _finite(initial_capital, "initial_capital")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive.")
        self.slippage_bps = _finite(slippage_bps, "slippage_bps")
        self.commission_per_share = _finite(commission_per_share, "commission_per_share")
        self.risk_free_rate = _finite(risk_free_rate, "risk_free_rate")
        self.periods_per_year = periods_per_year
        if self.slippage_bps < 0 or self.commission_per_share < 0:
            raise ValueError("Execution costs must be nonnegative.")

    def run(self, closed_trades, daily_equity_curve):
        if not isinstance(closed_trades, list):
            raise ValueError("closed_trades must be a list.")
        executed = [
            apply_execution_costs(
                trade, slippage_bps=self.slippage_bps,
                commission_per_share=self.commission_per_share,
            )
            for trade in closed_trades
        ]
        metrics = calculate_performance_metrics(
            executed, daily_equity_curve, self.initial_capital,
            risk_free_rate=self.risk_free_rate,
            periods_per_year=self.periods_per_year,
        )
        return {
            "ok": True,
            "engine": "tradeboot_isolated_historical_backtest",
            "mode": "backtest_only",
            "initial_capital_usd": self.initial_capital,
            "execution_costs": {
                "slippage_bps_per_side": self.slippage_bps,
                "commission_per_share_per_side_usd": self.commission_per_share,
            },
            "trades": executed,
            "metrics": metrics,
            "real_orders_enabled": False,
            "paper_ledger_touched": False,
        }
