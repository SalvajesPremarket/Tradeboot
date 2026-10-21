"""Bounded, deterministic simulation adapter for the reviewed Momentum Tramos engine."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from momentum_tramos import CONFIG, MomentumTrader


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Each candle/tick needs an ISO-8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid ISO-8601 timestamp.") from exc
    return parsed


def simulate_momentum(payload: dict) -> dict:
    """Run only the built-in reviewed engine against supplied historical/synthetic data."""
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required.")
    symbol = payload.get("symbol", "SIM")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 20:
        raise ValueError("Invalid symbol.")
    symbol = symbol.strip().upper()
    side = payload.get("side", "LONG")
    if not isinstance(side, str) or side.upper() not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    candles = payload.get("candles", [])
    ticks = payload.get("ticks")
    context = payload.get("context", {})
    config = payload.get("config", {})
    if not isinstance(candles, list) or len(candles) > 5000:
        raise ValueError("candles must be a list with at most 5000 rows.")
    if not isinstance(ticks, list) or not ticks or len(ticks) > 5000:
        raise ValueError("ticks must be a non-empty list with at most 5000 rows.")
    if not isinstance(context, dict) or not isinstance(config, dict):
        raise ValueError("context and config must be objects.")
    if len(config) > len(CONFIG) or any(key not in CONFIG for key in config):
        raise ValueError("Unknown Momentum Tramos config key.")
    bot = MomentumTrader(symbol, side.upper(), config)
    clean_candles = []
    for candle in candles:
        if not isinstance(candle, dict):
            raise ValueError("Each candle must be an object.")
        try:
            values = {key: float(candle[key]) for key in ("open", "high", "low", "close")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Each candle needs numeric open/high/low/close values.") from exc
        if not all(math.isfinite(value) and value > 0 for value in values.values()):
            raise ValueError("Candle prices must be finite and positive.")
        if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]) or values["low"] > values["high"]:
            raise ValueError("Invalid OHLC candle bounds.")
        clean_candles.append(values)
    if clean_candles:
        bot.seed(clean_candles)
    clean_context = dict(context)
    news = clean_context.get("news", [])
    if not isinstance(news, list) or len(news) > 100:
        raise ValueError("context.news must be a list with at most 100 items.")
    clean_news = []
    for item in news:
        if not isinstance(item, dict):
            raise ValueError("Each news item must be an object.")
        clean_news.append({
            "published_at": _timestamp(item.get("published_at")),
            "headline": str(item.get("headline", ""))[:500],
        })
    clean_context["news"] = clean_news
    for key in ("prev_close", "avg_premarket_volume_30d", "avg_volume_30d", "volume_today", "float_shares"):
        value = clean_context.get(key)
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"context.{key} must be numeric.") from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"context.{key} must be finite and non-negative.")
            clean_context[key] = value
    bot.set_context(clean_context)
    actions = []
    for tick in ticks:
        if not isinstance(tick, dict):
            raise ValueError("Each tick must be an object.")
        try:
            price = float(tick["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Each tick needs a numeric price.") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Tick prices must be finite and positive.")
        volume = tick.get("day_volume")
        if volume is not None:
            try:
                volume = float(volume)
            except (TypeError, ValueError) as exc:
                raise ValueError("day_volume must be numeric.") from exc
            if not math.isfinite(volume) or volume < 0:
                raise ValueError("day_volume must be finite and non-negative.")
        actions.extend(bot.on_tick(price, _timestamp(tick.get("timestamp")), volume))
    position = None
    if bot.pos:
        position = {key: value for key, value in bot.pos.items()}
    return {
        "ok": True,
        "engine": "momentum_tramos",
        "mode": "simulation_only",
        "symbol": symbol,
        "side": side.upper(),
        "ticks_processed": len(ticks),
        "candles_seeded": len(clean_candles),
        "actions": actions,
        "open_simulated_position": position,
        "last_reject_reasons": bot.last_reject,
        "last_metrics": bot.last_metrics,
        "note": "Signals only. No broker connection and no real orders are sent.",
    }
