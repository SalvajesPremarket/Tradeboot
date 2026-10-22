"""Strict tick-to-candle primitives for the Python strategy runtime.

Only observed prices are used. Missing minutes are not synthesized, and a candle
is emitted as closed only after a tick from a later minute arrives.
"""
from datetime import datetime, timezone
import math


def parse_market_timestamp(value):
    """Parse an ISO timestamp and normalize it to UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tick_timestamp_required")
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_tick_timestamp") from None
    if result.tzinfo is None:
        raise ValueError("tick_timestamp_timezone_required")
    return result.astimezone(timezone.utc)


def _finite_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("tick_price_must_be_finite_number")
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("tick_price_must_be_positive_finite_number")
    return price


class OneMinuteCandleBuilder:
    """Build OHLC candles from timestamped, observed prices for one symbol.

    The caller owns persistence and provider reconnection. Out-of-order ticks
    are rejected instead of silently corrupting candle ordering. Gaps remain
    gaps: no synthetic candles are generated.
    """

    def __init__(self, symbol):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol_required")
        self.symbol = symbol.strip().upper()
        self._current = None
        self._last_timestamp = None

    @staticmethod
    def _minute(timestamp):
        return timestamp.replace(second=0, microsecond=0)

    def on_tick(self, price, timestamp):
        """Consume one observed tick; return the newly closed candle or None."""
        price = _finite_price(price)
        timestamp = parse_market_timestamp(timestamp)
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ValueError("out_of_order_tick")
        self._last_timestamp = timestamp
        minute = self._minute(timestamp)

        if self._current is None:
            self._current = self._new_candle(minute, price)
            return None

        if minute == self._current["timestamp"]:
            self._current["high"] = max(self._current["high"], price)
            self._current["low"] = min(self._current["low"], price)
            self._current["close"] = price
            self._current["ticks"] += 1
            return None

        if minute < self._current["timestamp"]:
            raise ValueError("out_of_order_tick")

        closed = dict(self._current)
        self._current = self._new_candle(minute, price)
        return closed

    def current_candle(self):
        """Return a copy of the still-open candle, if one exists."""
        return None if self._current is None else dict(self._current)

    def _new_candle(self, minute, price):
        return {
            "symbol": self.symbol,
            "timestamp": minute.isoformat().replace("+00:00", "Z"),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "ticks": 1,
            "complete": False,
        }
