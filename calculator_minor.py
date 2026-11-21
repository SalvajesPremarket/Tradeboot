"""Calculador Minor: reviewed, paper-only trading strategy.

Consumes completed Schwab OHLCV bars and fresh Schwab ticks. It emits
simulation actions only; it has no broker order adapter.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

STRATEGY_NAME = "Calculador Minor"
SIDE = "BOTH"
ET = ZoneInfo("America/New_York")
TIMEFRAMES_MINUTES = (1, 2, 3, 5, 10, 13, 15, 30, 60)


def _stamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp inválido")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi(values, period=14):
    if len(values) <= period:
        return None
    gains, losses = [], []
    for left, right in zip(values[-period-1:-1], values[-period:]):
        delta = right - left
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _true_range_atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    values = []
    for previous, current in zip(bars[-period-1:-1], bars[-period:]):
        values.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))
    return sum(values) / len(values) if values else None


def _valid_bars(rows):
    result = []
    for row in rows or []:
        try:
            item = {
                "timestamp": _stamp(row.get("timestamp", row.get("datetime"))),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume", 0) or 0),
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (not all(math.isfinite(item[k]) and item[k] > 0 for k in ("open", "high", "low", "close"))
                or not math.isfinite(item["volume"]) or item["volume"] < 0
                or item["low"] > min(item["open"], item["close"])
                or item["high"] < max(item["open"], item["close"])
                or item["low"] > item["high"]):
            continue
        result.append(item)
    return sorted(result, key=lambda x: x["timestamp"])


def _aggregate_minutes(bars, minutes, now):
    grouped = defaultdict(list)
    for bar in bars:
        local = bar["timestamp"].astimezone(ET)
        anchor = local.replace(hour=4, minute=0, second=0, microsecond=0)
        if local < anchor:
            anchor -= timedelta(days=1)
        elapsed = int((local - anchor).total_seconds() // 60)
        start = anchor + timedelta(minutes=(elapsed // minutes) * minutes)
        end = start + timedelta(minutes=minutes)
        if end.astimezone(timezone.utc) <= now:
            grouped[start].append(bar)
    output = []
    for start in sorted(grouped):
        rows = grouped[start]
        output.append({
            "timestamp": start.astimezone(timezone.utc),
            "open": rows[0]["open"], "high": max(x["high"] for x in rows),
            "low": min(x["low"] for x in rows), "close": rows[-1]["close"],
            "volume": sum(x["volume"] for x in rows),
        })
    return output


def _aggregate_calendar(daily, unit):
    grouped = defaultdict(list)
    for bar in daily:
        local = bar["timestamp"].astimezone(ET)
        if unit == "week":
            key = (local.isocalendar().year, local.isocalendar().week)
        else:
            key = (local.year, local.month)
        grouped[key].append(bar)
    result = []
    for key in sorted(grouped):
        rows = grouped[key]
        result.append({
            "timestamp": rows[0]["timestamp"], "open": rows[0]["open"],
            "high": max(x["high"] for x in rows), "low": min(x["low"] for x in rows),
            "close": rows[-1]["close"], "volume": sum(x["volume"] for x in rows),
        })
    return result


def _timeframe_reference(bars, price, name):
    if len(bars) < 20:
        return {"timeframe": name, "status": "insufficient", "state": "neutral", "reference": None}
    closes = [x["close"] for x in bars]
    lines = {}
    for period in (3, 9, 20, 50, 200):
        value = _ema(closes, period)
        if value is not None:
            lines["EMA" + str(period)] = value
    window = closes[-20:]
    mean = sum(window) / 20
    variance = sum((x - mean) ** 2 for x in window) / 20
    deviation = math.sqrt(variance)
    upper, lower = mean + 2 * deviation, mean - 2 * deviation
    lines["BB_UPPER"], lines["BB_LOWER"] = upper, lower
    ordered = sorted((value, key) for key, value in lines.items())
    below = [item for item in ordered if item[0] <= price]
    above = [item for item in ordered if item[0] >= price]
    if below and above:
        chosen = [below[-1], above[0]]
        reference = sum(item[0] for item in chosen) / 2
        reference_lines = chosen[0][1] + " + " + chosen[1][1]
    else:
        nearest = min(ordered, key=lambda item: abs(item[0] - price))
        reference, reference_lines = nearest[0], nearest[1]
    state = "sobrecompra" if price > upper else ("sobreventa" if price < lower else "neutral")
    return {
        "timeframe": name, "status": "ok", "state": state,
        "reference": reference, "reference_lines": reference_lines,
        "ema": {key: value for key, value in lines.items() if key.startswith("EMA")},
        "bb_upper": upper, "bb_lower": lower, "candles": len(bars),
    }


def calculate_market_levels(minute_rows, daily_rows, price, now):
    """Port of the TradeScanner multi-timeframe Bollinger/EMA reference model."""
    minute = _valid_bars(minute_rows)
    daily = _valid_bars(daily_rows)
    frames = []
    for minutes in TIMEFRAMES_MINUTES:
        bars = _aggregate_minutes(minute, minutes, now)
        frames.append(_timeframe_reference(bars, price, str(minutes) + "m"))
    frames.append(_timeframe_reference(daily, price, "1d"))
    frames.append(_timeframe_reference(_aggregate_calendar(daily, "week"), price, "1w"))
    frames.append(_timeframe_reference(_aggregate_calendar(daily, "month"), price, "1mo"))
    over = [x["reference"] for x in frames if x["status"] == "ok" and x["state"] == "sobrecompra"]
    under = [x["reference"] for x in frames if x["status"] == "ok" and x["state"] == "sobreventa"]
    avg_over = sum(over) / len(over) if over else None
    avg_under = sum(under) / len(under) if under else None

    local_now = now.astimezone(ET)
    today = local_now.date()
    today_bars = [x for x in minute if x["timestamp"].astimezone(ET).date() == today]
    current_range = (max(x["high"] for x in today_bars) - min(x["low"] for x in today_bars)) if today_bars else None
    completed_ranges = [x["high"] - x["low"] for x in daily[-2:]]
    atr_inputs = completed_ranges + ([current_range] if current_range is not None else [])
    atr = sum(atr_inputs) / len(atr_inputs) if len(atr_inputs) == 3 else None
    gm1 = gm2 = gm3 = None
    clock = local_now.time().replace(tzinfo=None)
    from datetime import time
    if atr is not None and time(4, 0) <= clock < time(9, 30):
        gm1, gm2, gm3 = 2 * atr / 3, atr / 3, atr
        gap_mode = "premarket"
    elif atr is not None and time(9, 30) <= clock < time(16, 0):
        gm1, gm2, gm3 = atr / 3, atr / 6, 2 * atr / 3
        gap_mode = "market_open"
    else:
        gap_mode = "outside_session"
    bp = (avg_over + avg_under) / 2 if avg_over is not None and avg_under is not None else None
    levels = {}
    if bp is not None and gm1 is not None:
        levels = {
            "BP": bp, "MaxPos": bp + gm1, "IntPos": bp + gm2,
            "IntNeg": bp - gm2, "MaxNeg": bp - gm1,
            "TopReversal": bp + gm3, "BottomReversal": bp - gm3,
        }
    return {
        "timeframes": frames, "avg_overbought": avg_over, "avg_oversold": avg_under,
        "overbought_count": len(over), "oversold_count": len(under),
        "atr_daily": atr, "gm1": gm1, "gm2": gm2, "gm3": gm3,
        "gap_mode": gap_mode, "levels": levels,
    }


def analyze_direction(candles):
    valid = _valid_bars(candles)
    if len(valid) < 30:
        raise ValueError("Schwab no devolvió al menos 30 velas cerradas de 1 minuto.")
    closes = [x["close"] for x in valid]
    last = closes[-1]
    e9, e20 = _ema(closes[-60:], 9), _ema(closes[-80:], 20)
    e9_prev = _ema(closes[-65:-5], 9)
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    macd_line = fast - slow if fast is not None and slow is not None else None
    macd_values = []
    for index in range(25, len(closes)):
        f, s = _ema(closes[:index + 1], 12), _ema(closes[:index + 1], 26)
        if f is not None and s is not None:
            macd_values.append(f - s)
    signal = _ema(macd_values, 9)
    histogram = macd_line - signal if macd_line is not None and signal is not None else None
    pv = sum(((x["high"] + x["low"] + x["close"]) / 3) * x["volume"] for x in valid)
    volume = sum(x["volume"] for x in valid)
    vwap = pv / volume if volume > 0 else None
    rsi = _rsi(closes)
    atr = _true_range_atr(valid, 14)
    score, notes = 0, []
    if e9 is not None and e20 is not None:
        score += 22 if e9 > e20 else -22
        notes.append("EMA9 sobre EMA20" if e9 > e20 else "EMA9 bajo EMA20")
    if e9 is not None and e9_prev is not None:
        score += 12 if e9 > e9_prev else -12
        notes.append("EMA9 con pendiente positiva" if e9 > e9_prev else "EMA9 con pendiente negativa")
    if vwap is not None:
        score += 16 if last > vwap else -16
        notes.append("precio sobre VWAP" if last > vwap else "precio bajo VWAP")
    if histogram is not None:
        score += 14 if histogram > 0 else -14
        notes.append("MACD positivo" if histogram > 0 else "MACD negativo")
    if rsi is not None:
        if 55 <= rsi < 75:
            score += 10
            notes.append("RSI favorece compradores")
        elif 25 < rsi <= 45:
            score -= 10
            notes.append("RSI favorece vendedores")
        elif rsi >= 75 or rsi <= 25:
            notes.append("RSI extremo")
    recent, prior = valid[-6:], valid[-12:-6]
    move = recent[-1]["close"] - recent[0]["open"]
    score += 8 if move > 0 else (-8 if move < 0 else 0)
    if prior:
        prior_move = prior[-1]["close"] - prior[0]["open"]
        score += 5 if move > prior_move else (-5 if move < prior_move else 0)
    return {
        "score": max(-100, min(100, score)), "last": last, "atr": atr,
        "vwap": vwap, "rsi": rsi, "notes": notes,
        "trigger_high": max(x["high"] for x in valid[-5:]),
        "trigger_low": min(x["low"] for x in valid[-5:]),
        "as_of": valid[-1]["timestamp"],
    }


class CalculatorMinorTrader:
    """Replays one ticker's fresh ticks and manages one LONG or SHORT paper position."""

    def __init__(self, symbol, config=None):
        self.symbol = symbol
        self.config = config or {}
        self.position = None

    def simulate(self, candles, daily_candles, ticks, open_position=None):
        minute = _valid_bars(candles)
        daily = _valid_bars(daily_candles)
        if len(minute) < 30:
            raise ValueError("Historial insuficiente: se requieren 30 velas cerradas de 1 minuto.")
        latest = _stamp(ticks[-1]["timestamp"])
        direction = analyze_direction(minute)
        score = direction["score"]
        bias = "LONG" if score >= 22 else ("SHORT" if score <= -22 else "WAIT")
        risk_atr = direction["atr"]
        risk = max(risk_atr * math.sqrt(60) * 0.65, risk_atr) if risk_atr and risk_atr > 0 else None
        if bias != "WAIT" and risk is None:
            bias = "WAIT"
        levels = calculate_market_levels(minute, daily, _finite(ticks[-1]["price"]), latest)
        diagnostics = {
            "direction": bias, "score": score, "bull_evidence": round((score + 100) / 2),
            "bear_evidence": 100 - round((score + 100) / 2), "notes": direction["notes"],
            "entry_long": direction["trigger_high"] + 0.01,
            "entry_short": direction["trigger_low"] - 0.01,
            "risk_per_share": risk, "market_levels": levels,
            "calculator_timeframes": len(levels["timeframes"]),
        }
        actions = []
        if open_position:
            self.position = {
                "side": str(open_position["side"]).upper(),
                "entry": float(open_position["entry_price"]),
                "stop": float(open_position["stop_price"]),
                "target1": float(open_position.get("target1_price") or open_position["take_price"]),
                "target2": float(open_position["take_price"]),
                "target1_done": bool(open_position.get("target1_done")),
                "entry_at": _stamp(open_position["entry_at"]),
            }
        for tick in ticks:
            stamp = _stamp(tick["timestamp"])
            price = float(tick["price"])
            if self.position:
                pos = self.position
                if stamp <= pos["entry_at"]:
                    continue
                opposite = (pos["side"] == "LONG" and bias == "SHORT") or (pos["side"] == "SHORT" and bias == "LONG")
                stopped = price <= pos["stop"] if pos["side"] == "LONG" else price >= pos["stop"]
                target1_hit = price >= pos["target1"] if pos["side"] == "LONG" else price <= pos["target1"]
                target2_hit = price >= pos["target2"] if pos["side"] == "LONG" else price <= pos["target2"]
                if stopped:
                    actions.append({"action": "EXIT", "side": pos["side"], "price": price,
                                    "reason": "Calculador Minor · Stop Loss", "event_at": stamp.isoformat()})
                    self.position = None
                elif opposite:
                    actions.append({"action": "EXIT", "side": pos["side"], "price": price,
                                    "reason": "Calculador Minor · cambio confirmado de dirección", "event_at": stamp.isoformat()})
                    self.position = None
                elif target2_hit:
                    actions.append({"action": "EXIT", "side": pos["side"], "price": price,
                                    "reason": "Calculador Minor · Objetivo 2 (2R)", "event_at": stamp.isoformat()})
                    self.position = None
                elif target1_hit and not pos["target1_done"]:
                    actions.append({"action": "PARTIAL_EXIT", "side": pos["side"], "price": price,
                                    "quantity_pct": 0.5, "reason": "Calculador Minor · Objetivo 1 (1R), cerrar 50%",
                                    "event_at": stamp.isoformat()})
                    pos["target1_done"] = True
                continue
            if bias == "WAIT" or risk is None:
                continue
            side = "LONG" if bias == "LONG" else "SHORT"
            entry = direction["trigger_high"] + 0.01 if side == "LONG" else direction["trigger_low"] - 0.01
            crossed = price >= entry if side == "LONG" else price <= entry
            if not crossed:
                continue
            stop = entry - risk if side == "LONG" else entry + risk
            target1 = entry + risk if side == "LONG" else entry - risk
            target2 = entry + 2 * risk if side == "LONG" else entry - 2 * risk
            actions.append({
                "action": "ENTER", "side": side, "price": price, "stop": stop,
                "target1": target1, "target2": target2,
                "reason": "Calculador Minor · ruptura confirmada de cinco velas; sesgo %s (%s/100)" % (bias, score),
                "event_at": stamp.isoformat(),
            })
            self.position = {"side": side, "entry": price, "stop": stop, "target1": target1,
                             "target2": target2, "target1_done": False, "entry_at": stamp}
        return {
            "ok": True, "strategy": STRATEGY_NAME, "mode": "simulation_only",
            "symbol": self.symbol, "actions": actions,
            "open_simulated_position": self.position, "diagnostics": diagnostics,
            "real_orders_enabled": False,
        }
