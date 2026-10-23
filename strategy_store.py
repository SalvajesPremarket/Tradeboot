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
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        """)
        conn.execute("ALTER TABLE tradeboot_strategies DROP COLUMN IF EXISTS allocation_percent")
        conn.execute("ALTER TABLE tradeboot_strategies ADD COLUMN IF NOT EXISTS strategy_type TEXT NOT NULL DEFAULT 'formula'")
        rows = conn.execute(
            "SELECT id, name, formula, enabled, created_at, strategy_type FROM tradeboot_strategies ORDER BY created_at, id"
        ).fetchall()
    return [{
        "id": row[0], "name": row[1], "formula": row[2], "enabled": row[3],
        "createdAt": row[4].isoformat(),
        "strategyType": row[5] or "formula",
    } for row in rows]


def validate_program_selection(normalized):
    """Allow multiple independently tracked strategies when each uses a reviewed engine."""
    # Source compatibility is checked per enabled Python program below. The paper
    # worker creates an independent simulation for every enabled strategy.
    return True


def replace_strategies(items):
    # Preserve existing active rows while editing another strategy. Older saved Python
    # programs may no longer match the reviewed adapter exactly; they must not make
    # unrelated formula/strategy toggles fail. Newly enabling unsupported Python is
    # still rejected below.
    prior_enabled_ids = {item["id"] for item in list_strategies() if item.get("enabled")}
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
            # Only source-equivalent reviewed implementations can be activated.
            # Never execute arbitrary Python source submitted by a browser.
            from strategy_program_engine import is_supported_program
            if not is_supported_program(name, formula) and sid not in prior_enabled_ids:
                raise ValueError("Este programa Python no coincide con el motor revisado; restaura el código original antes de activarlo.")
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else now
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            created_at = now
        normalized.append((sid, name.strip(), formula, strategy_type, enabled, created_at, now))
    enabled_rows = [row for row in normalized if row[4]]
    validate_program_selection(normalized)
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
            conn.execute("ALTER TABLE tradeboot_strategies DROP COLUMN IF EXISTS allocation_percent")
            conn.execute("ALTER TABLE tradeboot_strategies ADD COLUMN IF NOT EXISTS strategy_type TEXT NOT NULL DEFAULT 'formula'")
            conn.execute("DELETE FROM tradeboot_strategies")
            if normalized:
                conn.cursor().executemany("""
                    INSERT INTO tradeboot_strategies
                    (id, name, formula, strategy_type, enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
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


def get_usage_percent():
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS tradeboot_strategy_settings (setting_key TEXT PRIMARY KEY, numeric_value DOUBLE PRECISION NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL)""")
        row = conn.execute("SELECT numeric_value FROM tradeboot_strategy_settings WHERE setting_key = %s", ("usage_percent",)).fetchone()
    return float(row[0]) if row else 10.0


def set_usage_percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError("Usage percentage must be between 0 and 100.")
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS tradeboot_strategy_settings (setting_key TEXT PRIMARY KEY, numeric_value DOUBLE PRECISION NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL)""")
        conn.execute("""INSERT INTO tradeboot_strategy_settings (setting_key, numeric_value, updated_at) VALUES (%s, %s, %s) ON CONFLICT (setting_key) DO UPDATE SET numeric_value = EXCLUDED.numeric_value, updated_at = EXCLUDED.updated_at""", ("usage_percent", float(value), now))
    return float(value)
