"""Bounded PostgreSQL persistence for observed, closed one-minute candles."""
from strategy_store import _connect, StrategyStoreUnavailable


def store_closed_candle(candle):
    """Persist a validated closed candle idempotently; never store open candles."""
    if not isinstance(candle, dict) or candle.get("complete") is not True:
        raise ValueError("closed_candle_required")
    symbol = candle.get("symbol")
    timestamp = candle.get("timestamp")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 20:
        raise ValueError("candle_symbol_invalid")
    if not isinstance(timestamp, str) or not timestamp.strip() or len(timestamp) > 80:
        raise ValueError("candle_timestamp_invalid")
    prices = []
    for field in ("open", "high", "low", "close"):
        value = candle.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("candle_price_invalid")
        prices.append(float(value))
    opened, high, low, closed = prices
    if min(prices) <= 0 or low > min(opened, closed) or high < max(opened, closed) or low > high:
        raise ValueError("candle_ohlc_invalid")
    ticks = candle.get("ticks")
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 1:
        raise ValueError("candle_tick_count_invalid")
    try:
        with _connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tradeboot_market_candles_1m (
                symbol TEXT NOT NULL,
                candle_time TIMESTAMPTZ NOT NULL,
                open DOUBLE PRECISION NOT NULL,
                high DOUBLE PRECISION NOT NULL,
                low DOUBLE PRECISION NOT NULL,
                close DOUBLE PRECISION NOT NULL,
                tick_count INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'observed_ticks',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (symbol, candle_time)
            )""")
            row = conn.execute("""INSERT INTO tradeboot_market_candles_1m
                (symbol, candle_time, open, high, low, close, tick_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, candle_time) DO NOTHING
                RETURNING symbol""",
                (symbol.strip().upper(), timestamp, opened, high, low, closed, ticks)).fetchone()
        return {"stored": bool(row), "duplicate": row is None}
    except StrategyStoreUnavailable:
        raise
    except Exception:
        raise StrategyStoreUnavailable("market_candle_persistence_failed") from None


def list_closed_candles(symbol, limit=500):
    """Return persisted closed candles oldest-first, with a bounded query size."""
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol.strip()) > 20:
        raise ValueError("candle_symbol_invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise ValueError("candle_limit_invalid")
    try:
        with _connect() as conn:
            rows = conn.execute("""SELECT symbol, candle_time, open, high, low, close, tick_count
                FROM (SELECT symbol, candle_time, open, high, low, close, tick_count
                      FROM tradeboot_market_candles_1m WHERE symbol=%s
                      ORDER BY candle_time DESC LIMIT %s) recent
                ORDER BY candle_time ASC""", (symbol.strip().upper(), limit)).fetchall()
        return [{
            "symbol": row[0], "timestamp": row[1].isoformat(),
            "open": float(row[2]), "high": float(row[3]), "low": float(row[4]),
            "close": float(row[5]), "ticks": int(row[6]), "complete": True,
        } for row in rows]
    except StrategyStoreUnavailable:
        raise
    except Exception:
        raise StrategyStoreUnavailable("market_candle_read_failed") from None
