"""Persistent PostgreSQL storage for named Tradeboot strategies."""
import os
import json
import uuid
from datetime import datetime, timezone


class StrategyStoreUnavailable(RuntimeError):
    pass


def _connect():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise StrategyStoreUnavailable("strategy_database_not_configured")
    try:
        import psycopg
    except ImportError:
        raise StrategyStoreUnavailable("postgres_driver_not_installed") from None
    try:
        return psycopg.connect(url, connect_timeout=5, autocommit=True, sslmode="require")
    except Exception:
        raise StrategyStoreUnavailable("strategy_database_unavailable") from None


def list_strategies():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tradeboot_strategies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                formula TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        """)
        rows = conn.execute(
            "SELECT id, name, formula, enabled, created_at FROM tradeboot_strategies ORDER BY created_at, id"
        ).fetchall()
    return [{
        "id": row[0], "name": row[1], "formula": row[2], "enabled": row[3],
        "createdAt": row[4].isoformat(),
    } for row in rows]


def replace_strategies(items):
    now = datetime.now(timezone.utc)
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each strategy must be an object.")
        sid = item.get("id")
        name = item.get("name")
        formula = item.get("formula")
        enabled = item.get("enabled", False)
        created = item.get("createdAt")
        if not isinstance(sid, str) or not sid or len(sid) > 100:
            raise ValueError("Strategy ID is invalid.")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Strategy name must contain 1 to 80 characters.")
        if not isinstance(formula, str) or not formula.strip() or len(formula) > 1000:
            raise ValueError("Strategy formula must contain 1 to 1000 characters.")
        if not isinstance(enabled, bool):
            raise ValueError("Strategy enabled flag must be boolean.")
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else now
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            created_at = now
        normalized.append((sid, name.strip(), formula.strip(), enabled, created_at, now))
    if len(normalized) > 1000:
        raise ValueError("A maximum of 1000 strategies is supported.")
    with _connect() as conn:
        with conn.transaction():
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tradeboot_strategies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    formula TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)
            conn.execute("DELETE FROM tradeboot_strategies")
            if normalized:
                conn.executemany("""
                    INSERT INTO tradeboot_strategies
                    (id, name, formula, enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, normalized)
    return list_strategies()
