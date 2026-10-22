"""PostgreSQL idempotency ledger for eToro Demo execution requests."""
from datetime import datetime, timezone
from strategy_store import _connect, StrategyStoreUnavailable


def reserve_demo_execution(key, strategy_id, symbol, side, instrument_id, amount_usd):
    if not isinstance(key, str) or not key.strip() or len(key) > 160:
        raise ValueError("idempotency_key_invalid")
    now = datetime.now(timezone.utc)
    try:
        with _connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tradeboot_demo_executions (
                idempotency_key TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL, instrument_id BIGINT NOT NULL,
                amount_usd DOUBLE PRECISION NOT NULL, status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""")
            with conn.transaction():
                inserted = conn.execute("""INSERT INTO tradeboot_demo_executions
                    (idempotency_key,strategy_id,symbol,side,instrument_id,amount_usd,status,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s)
                    ON CONFLICT (idempotency_key) DO NOTHING RETURNING idempotency_key""",
                    (key.strip(), strategy_id, symbol, side, instrument_id, float(amount_usd), now, now)).fetchone()
                if inserted:
                    return {"reserved": True, "status": "pending"}
                old = conn.execute("""SELECT strategy_id,symbol,side,instrument_id,amount_usd,status
                    FROM tradeboot_demo_executions WHERE idempotency_key=%s""", (key.strip(),)).fetchone()
                if not old:
                    raise StrategyStoreUnavailable("execution_reservation_unavailable")
                same = (old[0] == strategy_id and old[1] == symbol and old[2] == side
                        and old[3] == instrument_id and float(old[4]) == float(amount_usd))
                return {"reserved": False, "same_request": same, "status": old[5]}
    except StrategyStoreUnavailable:
        raise
    except Exception:
        raise StrategyStoreUnavailable("execution_store_unavailable") from None


def finish_demo_execution(key, status):
    if status not in ("submitted", "rejected", "unknown"):
        raise ValueError("execution_status_invalid")
    try:
        with _connect() as conn:
            conn.execute("""UPDATE tradeboot_demo_executions SET status=%s, updated_at=%s
                WHERE idempotency_key=%s""", (status, datetime.now(timezone.utc), key.strip()))
    except Exception:
        raise StrategyStoreUnavailable("execution_store_update_failed") from None


def list_demo_executions(limit=100):
    """Return recent ledger entries for authenticated Demo reconciliation review."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("execution_limit_invalid")
    try:
        with _connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tradeboot_demo_executions (
                idempotency_key TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL, instrument_id BIGINT NOT NULL,
                amount_usd DOUBLE PRECISION NOT NULL, status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""")
            rows = conn.execute("""SELECT idempotency_key,strategy_id,symbol,side,instrument_id,
                amount_usd,status,created_at,updated_at
                FROM tradeboot_demo_executions ORDER BY updated_at DESC LIMIT %s""", (limit,)).fetchall()
        return [{
            "idempotency_key": row[0], "strategy_id": row[1], "symbol": row[2],
            "side": row[3], "instrument_id": row[4], "amount_usd": float(row[5]),
            "status": row[6], "created_at": row[7].isoformat(),
            "updated_at": row[8].isoformat(),
        } for row in rows]
    except StrategyStoreUnavailable:
        raise
    except Exception:
        raise StrategyStoreUnavailable("execution_store_unavailable") from None
