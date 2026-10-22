"""PostgreSQL ledger and bounded scan history for eToro Demo operations."""
import json
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
                provider_order_id TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""")
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


def finish_demo_execution(key, status, provider_order_id=None):
    if status not in ("submitted", "rejected", "unknown"):
        raise ValueError("execution_status_invalid")
    if provider_order_id is not None:
        if isinstance(provider_order_id, bool) or not isinstance(provider_order_id, (str, int)):
            raise ValueError("provider_order_id_invalid")
        provider_order_id = str(provider_order_id).strip()[:200] or None
    try:
        with _connect() as conn:
            conn.execute("""ALTER TABLE tradeboot_demo_executions
                ADD COLUMN IF NOT EXISTS provider_order_id TEXT""")
            conn.execute("""UPDATE tradeboot_demo_executions
                SET status=%s, provider_order_id=COALESCE(%s, provider_order_id), updated_at=%s
                WHERE idempotency_key=%s""",
                (status, provider_order_id, datetime.now(timezone.utc), key.strip()))
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
            conn.execute("""ALTER TABLE tradeboot_demo_executions
                ADD COLUMN IF NOT EXISTS provider_order_id TEXT""")
            rows = conn.execute("""SELECT idempotency_key,strategy_id,symbol,side,instrument_id,
                amount_usd,status,created_at,updated_at,provider_order_id
                FROM tradeboot_demo_executions ORDER BY updated_at DESC LIMIT %s""", (limit,)).fetchall()
        return [{
            "idempotency_key": row[0], "strategy_id": row[1], "symbol": row[2],
            "side": row[3], "instrument_id": row[4], "amount_usd": float(row[5]),
            "status": row[6], "created_at": row[7].isoformat(),
            "updated_at": row[8].isoformat(), "provider_order_id": row[9],
            "broker_confirmation": "not_verified",
        } for row in rows]
    except StrategyStoreUnavailable:
        raise
    except Exception:
        raise StrategyStoreUnavailable("execution_store_unavailable") from None


def record_demo_scan_cycle(strategy_name, started_at, results):
    """Persist a bounded, read-only scanner cycle for authenticated review."""
    if not isinstance(strategy_name, str) or len(strategy_name) > 200:
        raise ValueError("scan_strategy_name_invalid")
    if not isinstance(started_at, str) or not started_at.strip() or len(started_at) > 80:
        raise ValueError("scan_started_at_invalid")
    if not isinstance(results, list) or len(results) > 5:
        raise ValueError("scan_results_invalid")
    serialized = json.dumps(results, separators=(",", ":"), default=str)
    if len(serialized) > 50000:
        raise ValueError("scan_results_too_large")
    try:
        with _connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tradeboot_demo_scan_cycles (
                id BIGSERIAL PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                results JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            conn.execute("""INSERT INTO tradeboot_demo_scan_cycles
                (strategy_name, started_at, results)
                VALUES (%s, %s, %s::jsonb)""",
                (strategy_name, started_at, serialized))
            # Keep storage bounded while preserving the newest 2,000 scan cycles.
            conn.execute("""DELETE FROM tradeboot_demo_scan_cycles
                WHERE id NOT IN (SELECT id FROM tradeboot_demo_scan_cycles
                                 ORDER BY id DESC LIMIT 2000)""")
    except StrategyStoreUnavailable:
        raise
    except Exception:
        raise StrategyStoreUnavailable("scan_history_persistence_failed") from None


def list_demo_scan_cycles(limit=50):
    """Return recent read-only scan cycles, newest first."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("scan_history_limit_invalid")
    try:
        with _connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tradeboot_demo_scan_cycles (
                id BIGSERIAL PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                results JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            rows = conn.execute("""SELECT id, strategy_name, started_at, results, created_at
                FROM tradeboot_demo_scan_cycles ORDER BY id DESC LIMIT %s""", (limit,)).fetchall()
        return [{
            "id": row[0], "strategy": row[1], "started_at": row[2].isoformat(),
            "results": row[3] if isinstance(row[3], (dict, list)) else json.loads(row[3]),
            "created_at": row[4].isoformat(),
        } for row in rows]
    except StrategyStoreUnavailable:
        raise
    except Exception:
        raise StrategyStoreUnavailable("scan_history_read_failed") from None
