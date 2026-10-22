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
                strategy_type TEXT NOT NULL DEFAULT 'formula',
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                allocation_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        """)
        conn.execute("ALTER TABLE tradeboot_strategies ADD COLUMN IF NOT EXISTS allocation_percent DOUBLE PRECISION NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE tradeboot_strategies ADD COLUMN IF NOT EXISTS strategy_type TEXT NOT NULL DEFAULT 'formula'")
        rows = conn.execute(
            "SELECT id, name, formula, enabled, created_at, allocation_percent, strategy_type FROM tradeboot_strategies ORDER BY created_at, id"
        ).fetchall()
    return [{
        "id": row[0], "name": row[1], "formula": row[2], "enabled": row[3],
        "createdAt": row[4].isoformat(), "allocationPercent": float(row[5]),
        "strategyType": row[6] or "formula",
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
        strategy_type = item.get("strategyType", "formula")
        enabled = item.get("enabled", False)
        allocation_percent = item.get("allocationPercent", 0)
        created = item.get("createdAt")
        if not isinstance(sid, str) or not sid or len(sid) > 100:
            raise ValueError("Strategy ID is invalid.")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Strategy name must contain 1 to 80 characters.")
        if strategy_type not in ("formula", "python_program"):
            raise ValueError("Strategy type is invalid.")
        if not isinstance(formula, str) or not formula.strip() or len(formula) > 50000:
            raise ValueError("Strategy source must contain 1 to 50000 characters.")
        if not isinstance(enabled, bool):
            raise ValueError("Strategy enabled flag must be boolean.")
        if strategy_type == "python_program" and enabled:
            # Only the four checked-in, reviewed implementations can be activated.
            # Never execute arbitrary Python source submitted by a browser.
            from strategy_program_engine import is_supported_program
            if not is_supported_program(name, formula):
                raise ValueError("Este programa Python no coincide con el motor revisado; restaura el código original antes de activarlo.")
        if isinstance(allocation_percent, bool) or not isinstance(allocation_percent, (int, float)) or not 0 <= allocation_percent <= 100:
            raise ValueError("Strategy allocation percentage must be between 0 and 100.")
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else now
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            created_at = now
        normalized.append((sid, name.strip(), formula, strategy_type, enabled, float(allocation_percent), created_at, now))
    enabled_rows = [row for row in normalized if row[4]]
    if sum(row[5] for row in enabled_rows) > 100.000001:
        raise ValueError("La asignación combinada de las estrategias activas no puede superar el 100% del capital general.")
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
            conn.execute("ALTER TABLE tradeboot_strategies ADD COLUMN IF NOT EXISTS allocation_percent DOUBLE PRECISION NOT NULL DEFAULT 10")
            conn.execute("ALTER TABLE tradeboot_strategies ADD COLUMN IF NOT EXISTS strategy_type TEXT NOT NULL DEFAULT 'formula'")
            conn.execute("DELETE FROM tradeboot_strategies")
            if normalized:
                conn.cursor().executemany("""
                    INSERT INTO tradeboot_strategies
                    (id, name, formula, strategy_type, enabled, allocation_percent, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, normalized)
    return list_strategies()


def get_general_capital():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tradeboot_strategy_settings (
                setting_key TEXT PRIMARY KEY,
                numeric_value DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL
            )
        """)
        row = conn.execute(
            "SELECT numeric_value FROM tradeboot_strategy_settings WHERE setting_key = %s",
            ("general_capital_usd",),
        ).fetchone()
    return float(row[0]) if row else 0.0


def set_general_capital(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1000000000000:
        raise ValueError("General capital must be a non-negative amount.")
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tradeboot_strategy_settings (
                setting_key TEXT PRIMARY KEY,
                numeric_value DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO tradeboot_strategy_settings (setting_key, numeric_value, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (setting_key) DO UPDATE SET
                numeric_value = EXCLUDED.numeric_value,
                updated_at = EXCLUDED.updated_at
        """, ("general_capital_usd", float(value), now))
    return float(value)
