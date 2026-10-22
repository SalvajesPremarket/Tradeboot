"""
PULLBACK SHORT EMA50 o 200 D W  (Tradeboot, corto)
===================================================
Dos piezas en un solo archivo:

PARTE 1 - CONTEXTO DIARIO / SEMANAL   daily_context(df, info, "D" | "W")
  "Armado" cuando TODO esto se cumple:
    - la vela de hoy (o de la semana) toca por PRIMERA vez la EMA50 o la EMA200 viniendo desde abajo
    - esa EMA esta pegada a la Banda de Bollinger SUPERIOR (distancia medida en ATR)
    - hubo un swing desde la Banda de Bollinger INFERIOR (recorrido minimo en ATR)
    - tus filtros de la Estrategia 2: precio $2-$30, volumen >= 300,000, float <= 50M
  Devuelve los niveles (valores de las EMAs) que el motor de 1 minuto usara.

PARTE 2 - EJECUCION EN VELAS DE 1 MINUTO   PullbackShortTrader
  Vela A (setup, no se opera): nace, SUBE en los primeros 20 s tocando el nivel EMA50/200,
      retrocede formando lapida y CIERRA NEGATIVA.
  Vela B (entrada): nace con MAXIMO INFERIOR al de la vela A, sube, retrocede y regresa a su
      apertura (lapida) en los primeros 20 s -> ENTRA EN CORTO.  Debe nacer SOBRE la EMA20.
  Gestion de cada vela (aplica igual a la vela B y a todas las siguientes):
      tramo 1 (0-20 s):  baja y regresa a la apertura sin traspasarla -> mantiene
                         traspasa la apertura (entra en positivo)     -> CIERRA
      tramo 2 (21-40 s): sigue negativa -> mantiene | toca la apertura -> CIERRA
      tramo 3 (41-60 s): toca la apertura -> CIERRA | se consolida negativa -> mantiene
      cierre de la vela: si cierra en zona de resistencia -> CIERRA
  Vela C en adelante: nace, sube, regresa a la apertura con maximo inferior al de la vela
      anterior -> mantiene y baja el stop al CIERRE DE LA VELA ANTERIOR (y asi sucesivamente).
  FIN DE LA OPERACION: vela que al nacer baja inmediatamente, regresa a la apertura y deja su
      MINIMO SUPERIOR al de la vela anterior formando libelula (dragonfly) -> CIERRA.

USO
    sizer = PositionSizer(capital=5000, daily_pct=60)
    bot   = PullbackShortTrader("ABCD", sizer)
    bot.seed(velas_1m_cerradas)                       # 30+ velas, para la EMA20
    ctx_d = daily_context(df_diario, info, "D")
    ctx_w = daily_context(to_weekly(df_diario), info, "W")
    bot.set_context(ctx_d, ctx_w)                     # una vez al dia (o al cargar el simbolo)
    acciones = bot.on_tick(precio, datetime_con_segundos)   # en CADA tick/segundo

    info = {"float_shares": 30_000_000, "avg_volume_30d": 650_000}   # avg_volume_30d opcional

    Acciones: {"action": "ENTER"|"EXIT"|"MOVE_STOP", "symbol", "side": "SHORT",
               "order": "SELL"(abrir corto)|"BUY"(cubrir)|None, "price", "stop", "shares", "reason"}

LO QUE NO ESTABA EN TUS REGLAS (lo defini yo, todo editable en CONFIG)
    - stop inicial: maximo de la vela de entrada + colchon, con tope `hard_stop_pct`
    - maximo superior al de la vela anterior mientras estas dentro -> cierra (se invalida el pullback)
    - zonas de la vela: soporte = minimo..centro del cuerpo, autocorreccion = centro, resistencia = arriba
    - el stop baja al cierre de la vela anterior cuando la vela es negativa, con maximo inferior
    - la vela A debe tocar el nivel EMA50/200 (tolerancia `touch_tol_pct`); se puede apagar
    - mismos filtros de precio/volumen/float que tu Estrategia 2 (se pueden poner en None)
"""

from __future__ import annotations

import math
from datetime import datetime, time as dtime
from typing import Any

import numpy as np
import pandas as pd

STRATEGY_NAME = "Pullback Short Ema50 o 200 D W"

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
DW_CONFIG: dict[str, Any] = {
    "price_min": 2.0, "price_max": 30.0,
    "min_avg_daily_volume": 300_000,
    "float_max": 50_000_000,
    "bb_period": 20, "bb_std": 2.0, "atr_period": 14,
    "ema_periods": (50, 200),
    "touch_tol_pct": 0.3,          # la vela "toca" la EMA si su rango llega a +-0.3% de ella
    "first_touch_only": True,      # la vela anterior NO debe haber tocado la EMA
    "approach_from_below": True,   # y su maximo estaba por debajo de la EMA (venia subiendo)
}
DW_TF: dict[str, dict[str, Any]] = {
    "D": {"min_bars": 60, "ema_bb_tol_atr": 1.0, "swing_lookback": 30, "min_bars_since_touch": 3,
          "min_swing_atr": 3.0, "vol_divisor": 1},
    "W": {"min_bars": 55, "ema_bb_tol_atr": 1.0, "swing_lookback": 20, "min_bars_since_touch": 2,
          "min_swing_atr": 3.0, "vol_divisor": 5},
}

CONFIG: dict[str, Any] = {
    "t1_end": 20, "t2_end": 40,
    "ema_fast": 20,
    "open_tol_pct": 0.02,          # tolerancia para decir "traspaso la apertura"
    "min_spike_pct": 0.10,         # vela A: subida minima (en %) para considerarla lapida
    "min_dip_pct": 0.05,           # vela B: subida minima antes de regresar a la apertura
    "gs_return_frac": 0.35,        # vela A: al segundo 20 debe haber devuelto 65% de la subida
    "gs_body_max": 0.20, "gs_wick_min": 0.60, "gs_small_wick_max": 0.20,   # forma de libelula
    "require_1m_touch": True,      # la vela A debe tocar el nivel EMA50/200 diario/semanal
    "touch_tol_pct": 0.5,
    "exit_on_higher_high": True,
    "exit_if_close_in_resistance": True,
    "stop_buffer_pct": 0.05,
    "hard_stop_pct": 3.0,
    "trading_window": None,        # ej. (dtime(4, 0), dtime(20, 0)); None = siempre
}

SIZER_CONFIG: dict[str, Any] = {
    "budget_mode": "concurrent",   # "concurrent" | "cumulative"
    "per_trade_fraction": 1.0,
    "max_trades_per_day": None,
    "max_daily_loss_pct": None,
}


# ============================================================================
# PARTE 1 - CONTEXTO DIARIO / SEMANAL
# ============================================================================
def to_weekly(df_daily: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df_daily.resample("W-FRI").agg(agg).dropna(subset=["close"])


def daily_context(df: pd.DataFrame, info: dict, timeframe: str = "D", cfg: dict | None = None) -> dict:
    cfg = {**DW_CONFIG, **(cfg or {})}
    tf = DW_TF[timeframe]
    out = {"armed": False, "timeframe": timeframe, "symbol": info.get("symbol"),
           "levels": [], "level_names": [], "metrics": {}, "reasons": []}
    try:
        if df is None or len(df) < tf["min_bars"]:
            out["reasons"].append("pocas velas")
            return out
        df = df.sort_index().dropna(subset=["open", "high", "low", "close"])
        close = df["close"]
        mid = close.rolling(cfg["bb_period"]).mean()
        sd = close.rolling(cfg["bb_period"]).std(ddof=0)
        upper, lower = mid + cfg["bb_std"] * sd, mid - cfg["bb_std"] * sd
        pc = close.shift(1)
        tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
        atr = float(tr.ewm(alpha=1 / cfg["atr_period"], adjust=False).mean().iloc[-1])
        last, prev = df.iloc[-1], df.iloc[-2]
        price = float(last["close"])
        up = float(upper.iloc[-1])

        # filtros basicos (tus filtros de la Estrategia 2)
        if cfg["price_min"] is not None and not (cfg["price_min"] <= price <= cfg["price_max"]):
            out["reasons"].append(f"precio fuera de ${cfg['price_min']}-${cfg['price_max']}")
        avg_vol = float(info.get("avg_volume_30d") or df["volume"].iloc[-20:].mean() / tf["vol_divisor"])
        if cfg["min_avg_daily_volume"] is not None and avg_vol < cfg["min_avg_daily_volume"]:
            out["reasons"].append(f"volumen < {cfg['min_avg_daily_volume']:,}")
        fl = info.get("float_shares")
        if cfg["float_max"] is not None and (not fl or fl > cfg["float_max"]):
            out["reasons"].append("float desconocido o mayor al maximo")

        # swing desde la banda inferior
        n, lb, gap = len(df), tf["swing_lookback"], tf["min_bars_since_touch"]
        swing_ok, swing_val = False, None
        for i in range(n - 1 - gap, max(0, n - 1 - lb) - 1, -1):
            if df["low"].iloc[i] <= lower.iloc[i]:
                swing_val = round((price - float(df["low"].iloc[i])) / atr, 2)
                swing_ok = swing_val >= tf["min_swing_atr"]
                break
        out["metrics"]["swing_atr"] = swing_val
        if not swing_ok:
            out["reasons"].append("sin swing desde la banda inferior (o muy corto)")

        # EMA50/200 pegadas a la banda superior y tocadas por primera vez
        tol = cfg["touch_tol_pct"] / 100
        for p in cfg["ema_periods"]:
            if n < p:
                continue
            e = close.ewm(span=p, adjust=False).mean()
            ev, ev_prev = float(e.iloc[-1]), float(e.iloc[-2])
            near_bb = abs(ev - up) <= tf["ema_bb_tol_atr"] * atr
            touch = last["high"] >= ev * (1 - tol) and last["low"] <= ev * (1 + tol)
            prev_touch = prev["high"] >= ev_prev * (1 - tol) and prev["low"] <= ev_prev * (1 + tol)
            first = (not prev_touch) if cfg["first_touch_only"] else True
            below = (prev["high"] < ev_prev) if cfg["approach_from_below"] else True
            out["metrics"][f"EMA{p}"] = {"valor": round(ev, 4), "cerca_BB_sup": bool(near_bb),
                                         "toca": bool(touch), "primera_vez": bool(first and below)}
            if near_bb and touch and first and below:
                out["levels"].append(ev)
                out["level_names"].append(f"EMA{p}")
        if not out["levels"]:
            out["reasons"].append("ninguna EMA50/200 tocada por primera vez y pegada a la banda superior")

        out["metrics"]["bb_superior"] = round(up, 4)
        out["metrics"]["atr"] = round(atr, 4)
        out["armed"] = not out["reasons"]
    except Exception as e:
        out["reasons"].append(f"error: {e}")
    return out


# ============================================================================
# PARTE 2 - TAMANO DE POSICION + MOTOR DE 1 MINUTO
# ============================================================================
class PositionSizer:
    """Se comparte entre todos los simbolos: monto asignado / precio, limitado por el % del dia."""

    def __init__(self, capital: float, daily_pct: float, cfg: dict | None = None):
        self.cfg = {**SIZER_CONFIG, **(cfg or {})}
        self.capital, self.daily_pct = float(capital), float(daily_pct)
        self.new_day()

    def set_budget(self, capital: float, daily_pct: float) -> None:
        self.capital, self.daily_pct = float(capital), float(daily_pct)

    def new_day(self) -> None:
        self.used = self.spent_today = self.pnl_today = 0.0
        self.trades_today = 0

    @property
    def daily_budget(self) -> float:
        return self.capital * self.daily_pct / 100

    def available(self) -> float:
        base = self.used if self.cfg["budget_mode"] == "concurrent" else self.spent_today
        return max(0.0, self.daily_budget - base)

    def can_trade(self) -> tuple[bool, str]:
        c = self.cfg
        if c["max_trades_per_day"] is not None and self.trades_today >= c["max_trades_per_day"]:
            return False, "limite de operaciones diarias"
        if c["max_daily_loss_pct"] is not None and self.pnl_today <= -self.capital * c["max_daily_loss_pct"] / 100:
            return False, "perdida maxima diaria alcanzada"
        return True, ""

    def shares_for(self, price: float) -> int:
        amount = min(self.available(), self.daily_budget * self.cfg["per_trade_fraction"])
        return int(math.floor(amount / price)) if price > 0 else 0

    def reserve(self, notional: float) -> None:
        self.used += notional
        self.spent_today += notional
        self.trades_today += 1

    def release(self, notional: float, pnl: float) -> None:
        self.used = max(0.0, self.used - notional)
        self.pnl_today += pnl


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


class PullbackShortTrader:
    """Un objeto por simbolo. Solo opera en corto. Alimentalo con ticks (precio, datetime)."""

    SIDE = "SHORT"

    def __init__(self, symbol: str, sizer: PositionSizer, cfg: dict | None = None):
        self.symbol, self.sizer = symbol, sizer
        self.cfg = {**CONFIG, **(cfg or {})}
        self.ema20 = _EMA(self.cfg["ema_fast"])
        self.levels: list[float] = []
        self.level_names: list[str] = []
        self.prev: dict | None = None
        self.c: dict | None = None
        self.pos: dict | None = None
        self.last_reject: list[str] = []

    # ---------------- preparacion ----------------
    def seed(self, candles: list[dict]) -> None:
        """Velas CERRADAS de 1 min en orden: {"open","high","low","close"}."""
        for cd in candles:
            self.ema20.update(cd["close"])
            self.prev = {**{k: cd[k] for k in ("open", "high", "low", "close")}, "setup_ok": False}

    def set_context(self, *ctxs: dict) -> None:
        """Recibe los resultados de daily_context (D y/o W). Usa los niveles de los que estan armados."""
        self.levels, self.level_names = [], []
        for c in ctxs:
            if c and c.get("armed"):
                self.levels += c["levels"]
                self.level_names += [f"{n}-{c['timeframe']}" for n in c["level_names"]]

    @property
    def armed(self) -> bool:
        return bool(self.levels)

    # ---------------- entrada principal ----------------
    def on_tick(self, price: float, ts: datetime) -> list[dict]:
        actions: list[dict] = []
        start = ts.replace(second=0, microsecond=0)
        if self.c is None:
            self._new_candle(start, price)
        elif start > self.c["start"]:
            actions += self._close_candle()
            self._new_candle(start, price)
        elif start < self.c["start"]:
            return actions
        c = self.c
        sec = ts.second + ts.microsecond / 1e6
        if price > c["high"]:
            c["high_sec"] = sec
        c["high"], c["low"], c["close"] = max(c["high"], price), min(c["low"], price), price
        if sec <= self.cfg["t1_end"]:
            c["price_t1_end"] = price
        actions += self._manage(price, sec, ts)
        return actions

    # ---------------- logica por tick ----------------
    def _manage(self, price: float, sec: float, ts: datetime) -> list[dict]:
        cfg, c = self.cfg, self.c
        o = c["open"]
        dev = (o - price) / o * 100            # % a favor del corto (+ = por debajo de la apertura)
        tol = cfg["open_tol_pct"]
        if c["first_move"] is None and abs(dev) >= tol:
            c["first_move"] = "fav" if dev > 0 else "adv"
        c["max_adv"] = max(c["max_adv"], -dev)
        c["max_fav"] = max(c["max_fav"], dev)
        if c["first_move"] == "adv" and dev >= 0 and c["max_adv"] >= cfg["min_dip_pct"]:
            c["pattern_done"] = True           # subio y regreso a la apertura (lapida)
        if self.pos is None:
            return self._try_enter(price, sec, ts, dev)
        return self._manage_position(price, sec, dev)

    # ---------------- entrada ----------------
    def _try_enter(self, price, sec, ts, dev) -> list[dict]:
        cfg, c, prev = self.cfg, self.c, self.prev
        if (not self.armed or prev is None or not prev.get("setup_ok") or c["entered_here"]
                or sec > cfg["t1_end"] or not c["pattern_done"] or dev < 0):
            return []
        r: list[str] = []
        if c["high"] >= prev["high"]:
            r.append("el maximo no es inferior al de la vela anterior")
        ok, why = self.sizer.can_trade()
        if not ok:
            r.append(why)
        w = cfg["trading_window"]
        if w and not (w[0] <= ts.time() < w[1]):
            r.append("fuera de horario")
        if c["ema20_open"] is None:
            r.append("sin historial para EMA20")
        elif c["open"] <= c["ema20_open"] or price <= self.ema20.peek(price):
            r.append("el pullback no esta iniciando por encima de la EMA20")
        shares = self.sizer.shares_for(price)
        if shares < 1:
            r.append("sin fondos suficientes para 1 accion")
        self.last_reject = r
        if r:
            return []

        stop = c["high"] + price * cfg["stop_buffer_pct"] / 100
        hard = price * (1 + cfg["hard_stop_pct"] / 100)
        stop = min(stop, hard)
        if stop <= price:
            stop = hard
        notional = shares * price
        self.pos = {"entry": price, "stop": stop, "shares": shares, "k": 1, "notional": notional}
        c["entered_here"] = True
        self.sizer.reserve(notional)
        return [self._act("ENTER", price, stop=stop, shares=shares, reason="vela B: lapida con maximo inferior + setup OK")]

    # ---------------- gestion ----------------
    def _manage_position(self, price, sec, dev) -> list[dict]:
        cfg, c, pos, prev = self.cfg, self.c, self.pos, self.prev
        tol = cfg["open_tol_pct"]
        # fin de la operacion: libelula en tramo 1 con minimo superior al de la vela anterior
        if (sec <= cfg["t1_end"] and c["first_move"] == "fav" and c["max_fav"] >= cfg["min_dip_pct"]
                and dev <= 0 and prev and c["low"] > prev["low"]):
            return self._exit(price, "libelula con minimo superior: fin del pullback")
        if price >= pos["stop"]:
            return self._exit(price, "stop")
        if cfg["exit_on_higher_high"] and prev and c["high"] > prev["high"]:
            return self._exit(price, "maximo superior al de la vela anterior")

        if sec <= cfg["t1_end"]:
            if (c["pattern_done"] or c["first_move"] == "fav") and dev < -tol:
                return self._exit(price, "traspaso la apertura (tramo 1)")
        elif dev <= 0:
            return self._exit(price, "toco la apertura (tramo 2/3)" if sec <= cfg["t2_end"] else "toco la apertura (tramo 3)")

        # velas siguientes: stop al cierre de la vela anterior
        if pos["k"] >= 2 and prev and dev > 0 and c["high"] < prev["high"]:
            new = prev["close"]
            if new < pos["stop"] and price < new:
                pos["stop"] = new
                return [self._act("MOVE_STOP", price, stop=new, shares=pos["shares"], reason="stop al cierre de la vela anterior")]
        return []

    def _exit(self, price: float, reason: str) -> list[dict]:
        pos = self.pos
        pnl = (pos["entry"] - price) * pos["shares"]
        self.sizer.release(pos["notional"], pnl)
        a = self._act("EXIT", price, stop=pos["stop"], shares=pos["shares"], reason=reason, pnl_estimado=round(pnl, 2))
        self.pos = None
        return [a]

    # ---------------- cierre de vela ----------------
    def _close_candle(self) -> list[dict]:
        c, prev, actions = self.c, self.prev, []
        setup = self._is_setup(c)
        if self.pos:
            if prev and c["low"] > prev["low"] and self._is_dragonfly(c):
                actions += self._exit(c["close"], "libelula con minimo superior al cierre de la vela")
            elif self.pos["k"] == 1 and c["close"] >= c["open"]:
                actions += self._exit(c["close"], "la vela de entrada no cerro negativa")
            elif self.cfg["exit_if_close_in_resistance"] and self._zone(c) == "resistencia":
                actions += self._exit(c["close"], "cerro en zona de resistencia")
        self.ema20.update(c["close"])
        self.prev = {"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "setup_ok": setup}
        if self.pos:
            self.pos["k"] += 1
        return actions

    def _new_candle(self, start: datetime, price: float) -> None:
        self.c = {"start": start, "open": price, "high": price, "low": price, "close": price,
                  "first_move": None, "max_adv": 0.0, "max_fav": 0.0, "high_sec": 0.0,
                  "price_t1_end": price, "pattern_done": False, "entered_here": False,
                  "ema20_open": self.ema20.v if self.ema20.ready else None}

    # ---------------- patrones de vela ----------------
    def _is_setup(self, c: dict) -> bool:
        """Vela A: sube en los primeros 20 s, retrocede formando lapida y cierra negativa."""
        cfg = self.cfg
        if not self.armed or c["ema20_open"] is None or c["open"] <= c["ema20_open"]:
            return False
        o, h = c["open"], c["high"]
        spike = (h - o) / o * 100
        if spike < cfg["min_spike_pct"] or c["high_sec"] > cfg["t1_end"] or c["close"] >= o:
            return False
        if c["price_t1_end"] > o + cfg["gs_return_frac"] * (h - o):
            return False
        if cfg["require_1m_touch"]:
            t = cfg["touch_tol_pct"] / 100
            if not any(h >= lv * (1 - t) for lv in self.levels):
                return False
        return True

    def _is_dragonfly(self, c: dict) -> bool:
        rng = c["high"] - c["low"]
        if rng <= 0:
            return False
        o, cl, cfg = c["open"], c["close"], self.cfg
        body = abs(cl - o) / rng
        lower = (min(o, cl) - c["low"]) / rng
        upper = (c["high"] - max(o, cl)) / rng
        return body <= cfg["gs_body_max"] and lower >= cfg["gs_wick_min"] and upper <= cfg["gs_small_wick_max"]

    @staticmethod
    def _zone(c: dict) -> str:
        """soporte: minimo..centro del cuerpo | autocorreccion: centro | resistencia: por encima del centro."""
        o, cl, rng = c["open"], c["close"], max(c["high"] - c["low"], 1e-9)
        mid = (o + cl) / 2
        if cl > mid + 0.05 * rng:
            return "resistencia"
        if abs(cl - mid) <= 0.05 * rng:
            return "autocorreccion"
        return "soporte"

    # ---------------- utilidades ----------------
    def _act(self, kind: str, price: float, **kw) -> dict:
        order = {"ENTER": "SELL", "EXIT": "BUY"}.get(kind)
        return {"action": kind, "symbol": self.symbol, "side": self.SIDE, "order": order,
                "price": round(price, 4), **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in kw.items()}}
