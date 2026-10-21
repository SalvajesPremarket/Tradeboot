"""Trusted Tradeboot implementation of Momentum Tramos.

This module contains the reviewed strategy logic. It emits simulation signals only;
it never places orders or connects to a broker.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import Any

SIDE = "LONG"
STRATEGY_NAME = "Momentum Tramos"

CONFIG: dict[str, Any] = {
    "t1_end": 20, "t2_end": 40,
    "ema_fast": 20, "max_prior_streak": 1,
    "macd_fast": 12, "macd_slow": 26, "clearance_pct": 2.0,
    "open_tol_pct": 0.02, "min_dip_pct": 0.05, "trading_window": None,
    "t2_close_if_back_to_open": True, "close_on_return_from_candle3": False,
    "candle2_stop_delay_sec": 20, "stop_buffer_pct": 0.05, "hard_stop_pct": 3.0,
    "bb_period": 20, "bb_std": 2.0, "gs_body_max": 0.20,
    "gs_reject_wick_min": 0.60, "gs_small_wick_max": 0.20, "gs_live_from_sec": 45,
    "use_scanner_filters": True, "gap_min_pct": 5.0, "gap_ideal_pct": 10.0,
    "rvol_min": 5.0, "price_min": 1.0, "price_max": 20.0,
    "float_max": 20_000_000, "news_required": True,
    "news_max_age_hours": 16, "news_require_keyword": False,
}
CATALYSTS = {
    "earnings": ("earnings", "quarter", "q1", "q2", "q3", "q4", "revenue", "eps", "guidance", "resultados", "ganancias", "trimestre"),
    "fda": ("fda", "phase", "trial", "approval", "approves", "clearance", "nda", "bla", "aprobacion", "aprobación", "ensayo"),
    "contrato": ("contract", "award", "government", "department of", "pentagon", "dod", "contrato", "gobierno"),
    "fusion": ("merger", "acquisition", "acquire", "acquires", "buyout", "takeover", "fusion", "fusión", "adquisicion", "adquisición"),
    "alianza": ("partnership", "partners", "collaboration", "agreement", "alliance", "alianza", "acuerdo", "asociacion", "asociación"),
}


class _EMA:
    def __init__(self, n: int):
        self.n, self.a, self.v, self.count = n, 2 / (n + 1), None, 0

    def update(self, x: float) -> float:
        self.v = x if self.v is None else self.a * x + (1 - self.a) * self.v
        self.count += 1
        return self.v

    def peek(self, x: float) -> float:
        return x if self.v is None else self.a * x + (1 - self.a) * self.v

    @property
    def ready(self) -> bool:
        return self.count >= self.n


class MomentumTrader:
    """One independent, simulation-only strategy instance per ticker."""

    def __init__(self, symbol: str, side: str = SIDE, cfg: dict | None = None):
        side = side.upper()
        if side not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG or SHORT")
        self.symbol, self.side = symbol, side
        self.d = 1 if side == "LONG" else -1
        self.cfg = {**CONFIG, **(cfg or {})}
        self.ema = {n: _EMA(n) for n in (self.cfg["ema_fast"], 50, 200)}
        self.m_fast, self.m_slow = _EMA(self.cfg["macd_fast"]), _EMA(self.cfg["macd_slow"])
        self.closes = deque(maxlen=self.cfg["bb_period"])
        self.streak = 0
        self.prev = self.c = self.pos = None
        self.last_reject, self.ctx, self.last_metrics = [], {}, {}
        self.day_volume = None

    def seed(self, candles: list[dict]) -> None:
        for candle in candles:
            self._update_indicators(float(candle["close"]))
            self.prev = self._snap(candle)

    def set_context(self, ctx: dict) -> None:
        self.ctx = ctx or {}

    def on_tick(self, price: float, ts: datetime, day_volume: float | None = None) -> list[dict]:
        if not math.isfinite(float(price)) or price <= 0:
            return []
        actions = []
        if day_volume is not None:
            self.day_volume = day_volume
        start = ts.replace(second=0, microsecond=0)
        if self.c is None:
            self._new_candle(start, price)
        elif start > self.c["start"]:
            actions += self._close_candle()
            self._new_candle(start, price)
        elif start < self.c["start"]:
            return actions
        c = self.c
        c["high"], c["low"], c["close"] = max(c["high"], price), min(c["low"], price), price
        sec = ts.second + ts.microsecond / 1e6
        actions += self._manage(price, sec, ts)
        return actions

    def _manage(self, price, sec, ts):
        cfg, c = self.cfg, self.c
        dev = self.d * (price - c["open"]) / c["open"] * 100
        tol = cfg["open_tol_pct"]
        if c["first_move"] is None and abs(dev) >= tol:
            c["first_move"] = "fav" if dev > 0 else "adv"
        c["adv_dip"] = max(c["adv_dip"], -dev)
        if sec <= cfg["t1_end"]:
            if dev >= tol:
                c["went_fav_t1"] = True
            elif c["went_fav_t1"] and dev <= -tol:
                c["pattern_inverted"] = True
        if self.pos is None:
            return self._try_enter(price, sec, ts, dev)
        return self._manage_position(price, sec, dev)

    def _try_enter(self, price, sec, ts, dev):
        cfg, c = self.cfg, self.c
        if (sec > cfg["t1_end"] or c["entered_here"] or c["pattern_inverted"]
                or c["first_move"] != "adv" or c["adv_dip"] < cfg["min_dip_pct"] or dev < 0):
            return []
        reasons = self._entry_filters(price, ts)
        self.last_reject = reasons
        if reasons:
            return []
        d = self.d
        extreme = c["low"] if d == 1 else c["high"]
        stop = extreme - d * price * cfg["stop_buffer_pct"] / 100
        hard = price * (1 - d * cfg["hard_stop_pct"] / 100)
        stop = max(stop, hard) if d == 1 else min(stop, hard)
        if d * (price - stop) <= 0:
            stop = hard
        self.pos = {"entry": price, "stop": stop, "k": 1}
        c["entered_here"] = True
        return [self._act("ENTER", price, stop=stop, reason="libelula en tramo 1 + filtros OK", metrics=dict(self.last_metrics))]

    def _entry_filters(self, price, ts):
        cfg, c, d = self.cfg, self.c, self.d
        reasons = []
        window = cfg["trading_window"]
        if window and not (window[0] <= ts.time() < window[1]):
            reasons.append("fuera de horario")
        reasons += self._scanner_filters(price, ts)
        ema = self.ema[cfg["ema_fast"]]
        macd = self._macd_live(price)
        if not ema.ready or macd is None:
            reasons.append("sin historial suficiente")
            return reasons
        if d * (c["open"] - ema.v) <= 0 or d * (price - ema.peek(price)) <= 0:
            reasons.append("la vela no nace del lado correcto de la EMA20")
        if self.streak > cfg["max_prior_streak"]:
            reasons.append(f"ya lleva {self.streak} velas del lado de la EMA20")
        if d * macd <= 0:
            reasons.append("MACD en contra")
        for n in (50, 200):
            e = self.ema[n]
            if e.ready:
                ahead = d * (e.v - price)
                if 0 < ahead / price * 100 <= cfg["clearance_pct"]:
                    reasons.append(f"EMA{n} en el camino")
        return reasons

    def _manage_position(self, price, sec, dev):
        cfg, c, d, pos = self.cfg, self.c, self.d, self.pos
        if d * (price - pos["stop"]) <= 0:
            return self._exit(price, "stop")
        k = pos["k"]
        if k == 1:
            if sec <= cfg["t1_end"] and c["pattern_inverted"]:
                return self._exit(price, "subio, regreso a la apertura y se fue en contra (tramo 1)")
            if cfg["t1_end"] < sec <= cfg["t2_end"] and cfg["t2_close_if_back_to_open"] and dev <= 0:
                return self._exit(price, "perdio la apertura en tramo 2")
            if sec > cfg["t2_end"] and dev <= 0:
                return self._exit(price, "regreso a la apertura en tramo 3")
        else:
            prev = self.prev
            ext = c["low"] if d == 1 else c["high"]
            higher = prev is not None and d * (ext - prev["adv_ext"]) > 0
            positive_open = c["first_move"] == "fav"
            if k == 2 and positive_open and dev <= 0:
                return self._exit(price, "vela 2 regreso a su apertura")
            if k >= 3 and cfg["close_on_return_from_candle3"] and positive_open and dev <= 0:
                return self._exit(price, "regreso a su apertura")
            if higher and dev > 0 and (k >= 3 or (positive_open and sec >= cfg["candle2_stop_delay_sec"])):
                action = self._raise_stop(prev["close"], price)
                if action:
                    return [action]
        if sec >= cfg["gs_live_from_sec"] and self._is_reversal_candle(c, price):
            return self._exit(price, "lapida/libelula tocando Bollinger")
        return []

    def _raise_stop(self, new_stop, price):
        if self.d * (new_stop - self.pos["stop"]) > 0 and self.d * (price - new_stop) > 0:
            self.pos["stop"] = new_stop
            return self._act("MOVE_STOP", price, stop=new_stop, reason="stop al cierre de la vela anterior")
        return None

    def _exit(self, price, reason):
        pos = self.pos
        pnl = self.d * (price - pos["entry"])
        action = self._act("EXIT", price, stop=pos["stop"], reason=reason, pnl_por_accion=round(pnl, 4))
        self.pos = None
        return [action]

    def cancel_entry(self):
        self.pos = None

    def _close_candle(self):
        c, d, actions = self.c, self.d, []
        if self.pos:
            if self._is_reversal_candle(c, c["close"]):
                actions += self._exit(c["close"], "lapida/libelula al cierre de la vela")
            elif self.pos["k"] == 1 and d * (c["close"] - c["open"]) <= 0:
                actions += self._exit(c["close"], "la vela de entrada cerro sin ganancia")
        self._update_indicators(c["close"])
        self.prev = self._snap(c)
        if self.pos:
            self.pos["k"] += 1
        return actions

    def _new_candle(self, start, price):
        self.c = {"start": start, "open": price, "high": price, "low": price, "close": price,
                  "first_move": None, "adv_dip": 0.0, "went_fav_t1": False,
                  "pattern_inverted": False, "entered_here": False}

    def _snap(self, c):
        return {"close": c["close"], "open": c["open"], "high": c["high"], "low": c["low"],
                "adv_ext": c["low"] if self.d == 1 else c["high"]}

    def _update_indicators(self, close):
        for ema in self.ema.values():
            ema.update(close)
        self.m_fast.update(close)
        self.m_slow.update(close)
        self.closes.append(close)
        ema20 = self.ema[self.cfg["ema_fast"]]
        self.streak = self.streak + 1 if self.d * (close - ema20.v) > 0 else 0

    def _macd_live(self, price):
        if not self.m_slow.ready:
            return None
        return self.m_fast.peek(price) - self.m_slow.peek(price)

    def _bands(self, price):
        n = self.cfg["bb_period"]
        hist = list(self.closes)[-(n - 1):]
        if len(hist) < n - 1:
            return None
        vals = hist + [price]
        mean = sum(vals) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / n)
        return mean + self.cfg["bb_std"] * sd, mean - self.cfg["bb_std"] * sd

    def _is_reversal_candle(self, candle, close_price):
        bands = self._bands(close_price)
        rng = candle["high"] - candle["low"]
        if bands is None or rng <= 0:
            return False
        o, h, l, cfg = candle["open"], candle["high"], candle["low"], self.cfg
        body = abs(close_price - o) / rng
        top_wick, bot_wick = (h - max(o, close_price)) / rng, (min(o, close_price) - l) / rng
        if self.d == 1:
            touched, reject, small = h >= bands[0], top_wick, bot_wick
        else:
            touched, reject, small = l <= bands[1], bot_wick, top_wick
        return touched and body <= cfg["gs_body_max"] and reject >= cfg["gs_reject_wick_min"] and small <= cfg["gs_small_wick_max"]

    def _scanner_filters(self, price, ts):
        cfg, ctx, reasons = self.cfg, self.ctx, []
        metrics = {}
        self.last_metrics = metrics
        if not cfg["use_scanner_filters"]:
            return reasons
        metrics["precio"] = round(price, 4)
        if not cfg["price_min"] <= price <= cfg["price_max"]:
            reasons.append(f"precio fuera de ${cfg['price_min']}-${cfg['price_max']}")
        previous_close = ctx.get("prev_close")
        if not previous_close or previous_close <= 0:
            reasons.append("sin cierre anterior (prev_close)")
        else:
            gap = (price - previous_close) / previous_close * 100
            metrics["gap_pct"], metrics["gap_ideal"] = round(gap, 2), gap >= cfg["gap_ideal_pct"]
            if gap < cfg["gap_min_pct"]:
                reasons.append(f"gap {gap:.1f}% < {cfg['gap_min_pct']}%")
        base = ctx.get("avg_premarket_volume_30d") or ctx.get("avg_volume_30d")
        volume = self.day_volume if self.day_volume is not None else ctx.get("volume_today")
        if not base or base <= 0 or volume is None:
            reasons.append("sin volumen promedio 30d o volumen de hoy")
        else:
            rvol = volume / base
            metrics["rvol"] = round(rvol, 2)
            if rvol < cfg["rvol_min"]:
                reasons.append(f"RVol {rvol:.1f}x < {cfg['rvol_min']}x")
        float_shares = ctx.get("float_shares")
        if not float_shares or float_shares <= 0:
            reasons.append("float desconocido")
        else:
            metrics["float"] = int(float_shares)
            if float_shares >= cfg["float_max"]:
                reasons.append(f"float >= {cfg['float_max']:,}")
        if cfg["news_required"]:
            news = self._fresh_news(ts)
            if news is None:
                reasons.append("sin noticia fresca")
            else:
                metrics["noticia"] = news
        return reasons

    def _fresh_news(self, now):
        best = None
        for news in self.ctx.get("news") or []:
            published = news.get("published_at")
            if not isinstance(published, datetime):
                continue
            if published.tzinfo is None and now.tzinfo is not None:
                published = published.replace(tzinfo=now.tzinfo)
            elif published.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=published.tzinfo)
            age = (now - published).total_seconds() / 3600
            if age < 0 or age > self.cfg["news_max_age_hours"]:
                continue
            headline = news.get("headline", "")
            category = next((name for name, words in CATALYSTS.items() if any(word in headline.lower() for word in words)), None)
            if self.cfg["news_require_keyword"] and category is None:
                continue
            if best is None or age < best[0]:
                best = (age, category or "otro", headline[:120])
        return None if best is None else {"categoria": best[1], "titular": best[2], "edad_horas": round(best[0], 1)}

    def _act(self, kind, price, **kwargs):
        order = {"ENTER": "BUY" if self.d == 1 else "SELL",
                 "EXIT": "SELL" if self.d == 1 else "BUY"}.get(kind)
        return {"action": kind, "symbol": self.symbol, "side": self.side, "order": order,
                "price": round(price, 4), **{k: round(v, 4) if isinstance(v, float) else v for k, v in kwargs.items()}}
