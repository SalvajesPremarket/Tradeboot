"""Continuous read-only strategy scanner for a Render background worker.

This process evaluates the enabled formula strategy against configured eToro
symbols. It never calls the order-placement adapter.
"""
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

from app import evaluate_demo_scan_snapshot, fetch_market_context
from strategy_store import StrategyStoreUnavailable, list_strategies
from demo_execution_store import record_demo_scan_cycle


_STOP = False


def _stop(_signum, _frame):
    global _STOP
    _STOP = True


def scan_once(symbols):
    enabled = [item for item in list_strategies() if item.get("enabled")]
    if len(enabled) != 1:
        return {
            "ok": False,
            "error": "exactly_one_enabled_strategy_required",
            "results": [],
            "orders_submitted": 0,
        }
    strategy = enabled[0]
    if strategy.get("strategyType") != "formula":
        return {
            "ok": False,
            "error": "formula_strategy_required",
            "results": [],
            "orders_submitted": 0,
        }

    results = []
    for symbol in symbols:
        try:
            status, market = fetch_market_context(symbol)
            if status != 200 or not market.get("ok"):
                results.append({
                    "ok": False,
                    "symbol": symbol,
                    "error": "market_context_unavailable",
                    "execution": "not_attempted",
                })
                continue
            results.append(evaluate_demo_scan_snapshot(strategy, symbol, market))
        except Exception as exc:
            # Keep the worker alive after a per-symbol provider/data error.
            results.append({
                "ok": False,
                "symbol": symbol,
                "error": "scan_item_failed",
                "execution": "not_attempted",
            })
    return {
        "ok": all(item.get("ok") for item in results),
        "mode": "scan_only",
        "strategy": strategy.get("name"),
        "results": results,
        "orders_submitted": 0,
    }


def configured_symbols():
    raw = os.environ.get("TRADEBOOT_SCAN_SYMBOLS", "")
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
    symbols = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if not symbol:
            continue
        if len(symbol) > 20 or any(char not in allowed for char in symbol):
            raise ValueError("TRADEBOOT_SCAN_SYMBOLS contains an invalid symbol")
        if symbol in symbols:
            raise ValueError("TRADEBOOT_SCAN_SYMBOLS contains duplicate symbols")
        symbols.append(symbol)
    if not 1 <= len(symbols) <= 5:
        raise ValueError("TRADEBOOT_SCAN_SYMBOLS must contain 1 to 5 comma-separated symbols")
    return symbols


def validate_environment():
    """Fail fast rather than run a noisy loop with missing database or provider credentials."""
    missing = [
        name for name in ("DATABASE_URL", "ETORO_API_KEY")
        if not os.environ.get(name, "").strip()
    ]
    if not (os.environ.get("ETORO_DEMO_USER_KEY", "").strip()
            or os.environ.get("ETORO_USER_KEY", "").strip()):
        missing.append("ETORO_USER_KEY or ETORO_DEMO_USER_KEY")
    if missing:
        raise ValueError("missing required environment variables: " + ", ".join(missing))


def run():
    global _STOP
    validate_environment()
    symbols = configured_symbols()
    try:
        interval = int(os.environ.get("TRADEBOOT_SCAN_INTERVAL_SECONDS", "60"))
    except (TypeError, ValueError):
        raise ValueError("TRADEBOOT_SCAN_INTERVAL_SECONDS must be an integer")
    if not 30 <= interval <= 3600:
        raise ValueError("TRADEBOOT_SCAN_INTERVAL_SECONDS must be between 30 and 3600")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(json.dumps({
        "event": "scanner_started",
        "symbols": symbols,
        "interval_seconds": interval,
        "mode": "scan_only",
        "orders_submitted": 0,
    }), flush=True)

    while not _STOP:
        started = datetime.now(timezone.utc).isoformat()
        try:
            snapshot = scan_once(symbols)
        except StrategyStoreUnavailable as exc:
            snapshot = {"ok": False, "error": "strategy_store_unavailable", "results": [], "orders_submitted": 0}
        except Exception as exc:
            snapshot = {"ok": False, "error": "scan_cycle_failed",
                        "results": [], "orders_submitted": 0}
        try:
            record_demo_scan_cycle(snapshot.get("strategy") or "unavailable", started, snapshot.get("results", []))
            snapshot["history_persisted"] = True
        except Exception as exc:
            # Keep scanning, but make persistence failure visible in the worker logs.
            snapshot["history_persisted"] = False
            snapshot["history_error"] = "scan_history_persistence_failed"
        print(json.dumps({"event": "scan_cycle", "started_at": started, **snapshot},
                         separators=(",", ":"), default=str), flush=True)
        deadline = time.monotonic() + interval
        while not _STOP and time.monotonic() < deadline:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    print(json.dumps({"event": "scanner_stopped"}), flush=True)


if __name__ == "__main__":
    try:
        run()
    except ValueError as exc:
        print(json.dumps({"event": "scanner_configuration_error", "error": str(exc)}),
              file=sys.stderr, flush=True)
        sys.exit(2)
