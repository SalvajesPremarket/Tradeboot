"""Allowlisted, reviewed simulation adapters for the four saved Tradeboot programs.

Never compiles or executes strategy source received over HTTP. Each program name is
bound to a checked-in implementation, and activation requires an exact source match.
Signals only; no broker order path exists.
"""
from __future__ import annotations

import inspect
import math
from datetime import datetime, timezone

from strategy_programs import jean_long, jean_short, pullback_long, pullback_short

_REGISTRY = {
    "JeanLong": (jean_long, "MomentumTrader", "momentum"),
    "JeanShort": (jean_short, "MomentumTrader", "momentum"),
    "PULLBACK LONG EMA50 o 200 D W": (pullback_long, "PullbackLongTrader", "pullback"),
    "PULLBACK SHORT EMA50 o 200 D W": (pullback_short, "PullbackShortTrader", "pullback"),
}

def supported_program_names():
    return tuple(_REGISTRY)

def _normalized_program(source):
    """Normalize Python syntax so comments, whitespace and docstrings do not block activation."""
    import ast

    tree = ast.parse(source, mode="exec")

    def strip_docstrings(node):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            return
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            value = getattr(body[0].value, "value", getattr(body[0].value, "s", None))
            if isinstance(value, str):
                body.pop(0)
        for child in ast.iter_child_nodes(node):
            strip_docstrings(child)

    strip_docstrings(tree)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def resolve_supported_program(name, source):
    """Resolve an uploaded source to a reviewed engine, even when its UI name differs.

    Only syntax-equivalent reviewed logic is accepted. Arbitrary uploaded Python is
    never compiled or executed by the server.
    """
    if not isinstance(source, str) or not source.strip():
        return None
    try:
        candidate = _normalized_program(source)
    except (SyntaxError, ValueError, TypeError, RecursionError):
        return None

    preferred = _REGISTRY.get(name)
    ordered = ([ (name, preferred) ] if preferred else []) + [
        (key, spec) for key, spec in _REGISTRY.items() if key != name
    ]
    for _key, spec in ordered:
        try:
            checked_in = _normalized_program(inspect.getsource(spec[0]))
        except (OSError, TypeError, SyntaxError, ValueError, RecursionError):
            continue
        if candidate == checked_in:
            return spec
    return None


def is_supported_program(name, source):
    """True only if source matches a reviewed engine; display name need not be canonical."""
    return resolve_supported_program(name, source) is not None

def _timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO-8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be an ISO-8601 timestamp.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

def _number(value, field, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric.")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be numeric.") from None
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{field} is outside its valid range.")
    return result

def _bars(rows, field, *, volume_required=False, max_rows=5000):
    if not isinstance(rows, list) or len(rows) > max_rows:
        raise ValueError(f"{field} must be a list with at most {max_rows} rows.")
    cleaned = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{field}[{index}] must be an object.")
        item = {}
        for key in ("open", "high", "low", "close"):
            item[key] = _number(row.get(key), f"{field}[{index}].{key}", positive=True)
        if item["low"] > min(item["open"], item["close"]) or item["high"] < max(item["open"], item["close"]) or item["low"] > item["high"]:
            raise ValueError(f"{field}[{index}] has invalid OHLC bounds.")
        if volume_required or "volume" in row:
            item["volume"] = _number(row.get("volume"), f"{field}[{index}].volume", nonnegative=True)
        stamp = row.get("timestamp", row.get("date", row.get("time")))
        if stamp is not None:
            item["timestamp"] = _timestamp(stamp, f"{field}[{index}].timestamp")
        elif volume_required:
            raise ValueError(f"{field}[{index}] needs a date/timestamp.")
        cleaned.append(item)
    return cleaned

def simulate_program(payload):
    """Run one checked-in strategy over bounded supplied OHLCV/tick data."""
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required.")
    name = payload.get("strategyName")
    source = payload.get("formula")
    if not is_supported_program(name, source):
        raise ValueError("Este programa no coincide exactamente con una implementación revisada; no se ejecutará código Python recibido.")
    spec = resolve_supported_program(name, source)
    if spec is None:
        raise ValueError("Este programa no coincide con una implementación revisada; no se ejecutará código Python recibido.")
    module, class_name, kind = spec
    symbol = payload.get("symbol", "SIM")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 20:
        raise ValueError("Invalid symbol.")
    symbol = symbol.strip().upper()
    candles = _bars(payload.get("candles", []), "candles")
    ticks = payload.get("ticks")
    if not isinstance(ticks, list) or not ticks or len(ticks) > 5000:
        raise ValueError("ticks must be a non-empty list with at most 5000 rows.")
    clean_ticks = []
    for i, tick in enumerate(ticks):
        if not isinstance(tick, dict):
            raise ValueError(f"ticks[{i}] must be an object.")
        clean_ticks.append({
            "price": _number(tick.get("price"), f"ticks[{i}].price", positive=True),
            "timestamp": _timestamp(tick.get("timestamp"), f"ticks[{i}].timestamp"),
            "day_volume": _number(tick["day_volume"], f"ticks[{i}].day_volume", nonnegative=True) if tick.get("day_volume") is not None else None,
        })
    context = payload.get("context", {})
    if not isinstance(context, dict):
        raise ValueError("context must be an object.")
    clean_context = dict(context)
    for key in ("prev_close", "avg_premarket_volume_30d", "avg_volume_30d", "volume_today", "float_shares"):
        if clean_context.get(key) is not None:
            clean_context[key] = _number(clean_context[key], f"context.{key}", nonnegative=True)
    news = clean_context.get("news", [])
    if not isinstance(news, list) or len(news) > 100:
        raise ValueError("context.news must be a list with at most 100 items.")
    clean_context["news"] = [
        {"published_at": _timestamp(n.get("published_at"), f"context.news[{i}].published_at"),
         "headline": str(n.get("headline", ""))[:500]}
        for i, n in enumerate(news) if isinstance(n, dict)
    ]
    if len(clean_context["news"]) != len(news):
        raise ValueError("Each context.news item must be an object.")

    actions = []
    daily_contexts = []
    if kind == "momentum":
        bot = getattr(module, class_name)(symbol, module.SIDE)
        bot.set_context(clean_context)
        bot.seed(candles)
        for tick in clean_ticks:
            for action in bot.on_tick(tick["price"], tick["timestamp"], tick["day_volume"]):
                action["event_at"] = tick["timestamp"].isoformat()
                actions.append(action)
        position = dict(bot.pos) if bot.pos else None
        diagnostics = {"last_reject_reasons": bot.last_reject, "last_metrics": bot.last_metrics}
    else:
        daily = _bars(payload.get("daily_candles", []), "daily_candles", volume_required=True, max_rows=2000)
        if len(daily) < 60:
            raise ValueError("Pullback necesita al menos 60 velas diarias para evaluar el contexto EMA50/200.")
        import pandas as pd
        daily_rows = []
        for i, row in enumerate(daily):
            if "timestamp" not in row:
                raise ValueError(f"daily_candles[{i}] needs a date/timestamp.")
            daily_rows.append({key: row[key] for key in ("open", "high", "low", "close", "volume")})
        frame = pd.DataFrame(daily_rows, index=pd.DatetimeIndex([row["timestamp"] for row in daily]))
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        info = {"symbol": symbol, "avg_volume_30d": clean_context.get("avg_volume_30d"),
                "float_shares": clean_context.get("float_shares")}
        contexts = []
        for timeframe, df in (("D", frame), ("W", module.to_weekly(frame))):
            try:
                result = module.daily_context(df, info, timeframe)
            except Exception as exc:
                result = {"armed": False, "timeframe": timeframe, "levels": [], "level_names": [],
                          "reasons": [f"context_error:{type(exc).__name__}"], "metrics": {}}
            contexts.append(result)
        daily_contexts = contexts
        if "capital" not in payload:
            raise ValueError("The simulation must receive the strategy budget calculated from saved general capital and allocation percent.")
        capital = _number(payload.get("capital"), "capital", nonnegative=True)
        sizer = module.PositionSizer(capital=capital, daily_pct=100)
        bot = getattr(module, class_name)(symbol, sizer)
        bot.set_context(*contexts)
        bot.seed(candles)
        for tick in clean_ticks:
            for action in bot.on_tick(tick["price"], tick["timestamp"]):
                action["event_at"] = tick["timestamp"].isoformat()
                actions.append(action)
        position = dict(bot.pos) if bot.pos else None
        diagnostics = {"armed": bot.armed, "levels": bot.levels, "level_names": bot.level_names,
                       "last_reject_reasons": bot.last_reject}

    return {"ok": True, "engine": "reviewed_python_program", "strategy": name,
            "mode": "simulation_only", "symbol": symbol, "candles_seeded": len(candles),
            "daily_candles_used": len(payload.get("daily_candles", [])) if kind == "pullback" else 0,
            "ticks_processed": len(clean_ticks), "daily_context": daily_contexts,
            "actions": actions, "open_simulated_position": position, "diagnostics": diagnostics,
            "real_orders_enabled": False,
            "note": "Motor revisado; resultados basados únicamente en los datos suministrados. No se envían órdenes reales."}
