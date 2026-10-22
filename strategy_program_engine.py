"""Replay the allowlisted Tradeboot strategies over caller-supplied historical data.

This is a simulation-only adapter. It never places orders and never executes source
stored in PostgreSQL. Live scanning needs a separate persistent candle/tick runtime.
"""
from datetime import datetime, timezone
import math

from strategy_program_registry import create_trader, get_program

MAX_REPLAY_TICKS = 5000
MAX_SEED_CANDLES = 2000


def _parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tick_timestamp_required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_tick_timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _number(value, error):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(error)
    return float(value)


def evaluate_strategy_replay(payload):
    """Replay a trusted strategy against supplied candles/ticks; no broker calls."""
    if not isinstance(payload, dict):
        raise ValueError("replay_payload_must_be_object")
    program_id = payload.get("strategyId")
    program = get_program(program_id)
    symbol = payload.get("symbol")
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
    if not isinstance(symbol, str):
        raise ValueError("symbol_required")
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 20 or any(char not in allowed for char in symbol):
        raise ValueError("invalid_symbol")

    candles = payload.get("candles", [])
    ticks = payload.get("ticks")
    if not isinstance(candles, list) or len(candles) > MAX_SEED_CANDLES:
        raise ValueError("candles_must_be_array_within_limit")
    if any(not isinstance(candle, dict) for candle in candles):
        raise ValueError("each_candle_must_be_object")
    if not isinstance(ticks, list) or not 1 <= len(ticks) <= MAX_REPLAY_TICKS:
        raise ValueError("ticks_must_be_array_within_limit")
    if any(not isinstance(tick, dict) for tick in ticks):
        raise ValueError("each_tick_must_be_object")

    capital = payload.get("capital")
    daily_pct = payload.get("dailyPct")
    if program["kind"] == "pullback":
        capital = _number(capital, "positive_capital_required")
        daily_pct = _number(daily_pct, "positive_dailyPct_required")
        if capital <= 0 or daily_pct <= 0:
            raise ValueError("positive_capital_and_dailyPct_required")

    trader = create_trader(
        program_id, symbol, capital=capital, daily_pct=daily_pct,
    )
    trader.seed(candles)
    context = payload.get("context", {} if program["kind"] == "momentum" else [])
    if program["kind"] == "momentum":
        if not isinstance(context, dict):
            raise ValueError("momentum_context_must_be_object")
        trader.set_context(context)
    else:
        if not isinstance(context, list) or any(not isinstance(item, dict) for item in context):
            raise ValueError("pullback_context_must_be_array_of_objects")
        trader.set_context(*context)

    signals = []
    for index, tick in enumerate(ticks):
        price = _number(tick.get("price"), "tick_price_must_be_finite_number")
        timestamp = _parse_timestamp(tick.get("timestamp", tick.get("ts")))
        if program["kind"] == "momentum":
            day_volume = tick.get("dayVolume")
            if day_volume is not None:
                day_volume = _number(day_volume, "dayVolume_must_be_finite_number")
            actions = trader.on_tick(price, timestamp, day_volume=day_volume)
        else:
            actions = trader.on_tick(price, timestamp)
        if actions:
            if not isinstance(actions, list):
                actions = [actions]
            for action in actions:
                if isinstance(action, dict):
                    signals.append({
                        "tick_index": index,
                        "timestamp": timestamp.isoformat(),
                        "action": action,
                    })

    return {
        "ok": True,
        "mode": "simulation",
        "execution": "not_attempted",
        "strategyId": program["id"],
        "strategy": program["name"],
        "symbol": symbol,
        "seed_candles": len(candles),
        "ticks_processed": len(ticks),
        "signal_count": len(signals),
        "signals": signals,
        "note": "Uses only the supplied candles, ticks and context; no market data is fabricated and no orders are sent.",
    }
