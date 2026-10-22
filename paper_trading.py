"""Persistent simulation-only paper trading loop for Tradeboot.

Uses confirmed eToro snapshots and saved formula strategies. Never places broker orders.
"""
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone

from strategy_store import _connect, list_strategies, get_general_capital
from strategy_engine import evaluate_formula, StrategyFormulaError
from app import fetch_market_context

_INTERVAL = max(30, int(os.environ.get("PAPER_TRADING_INTERVAL_SECONDS", "60")))
_STOP_PCT = max(0.1, min(20.0, float(os.environ.get("PAPER_TRADING_STOP_PCT", "2"))))
_TAKE_PCT = max(0.1, min(50.0, float(os.environ.get("PAPER_TRADING_TAKE_PCT", "4"))))
_SYMBOLS = tuple(dict.fromkeys(
    s.strip().upper() for s in os.environ.get("PAPER_TRADING_SYMBOLS", "AAPL,MSFT,NVDA").split(",")
    if s.strip() and len(s.strip()) <= 20
))
_lock = threading.Lock()
_thread = None


def _init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tradeboot_paper_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tradeboot_paper_trades (
            id BIGSERIAL PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED')),
            entry_price DOUBLE PRECISION NOT NULL,
            exit_price DOUBLE PRECISION,
            quantity DOUBLE PRECISION NOT NULL,
            allocated_usd DOUBLE PRECISION NOT NULL,
            stop_price DOUBLE PRECISION NOT NULL,
            take_price DOUBLE PRECISION NOT NULL,
            pnl_usd DOUBLE PRECISION,
            entry_at TIMESTAMPTZ NOT NULL,
            exit_at TIMESTAMPTZ,
            exit_reason TEXT,
            last_quote_at TIMESTAMPTZ,
            last_price DOUBLE PRECISION
        )
    """)
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS last_price DOUBLE PRECISION")
    conn.execute("CREATE INDEX IF NOT EXISTS tradeboot_paper_trades_status_idx ON tradeboot_paper_trades(status, strategy_id, symbol)")


def _state(key, value):
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        _init_tables(conn)
        conn.execute("""
            INSERT INTO tradeboot_paper_state(state_key,state_value,updated_at)
            VALUES (%s,%s,%s)
            ON CONFLICT(state_key) DO UPDATE SET state_value=EXCLUDED.state_value, updated_at=EXCLUDED.updated_at
        """, (key, json.dumps(value, ensure_ascii=False), now))


def get_paper_status():
    try:
        with _connect() as conn:
            _init_tables(conn)
            rows = conn.execute("SELECT state_key,state_value,updated_at FROM tradeboot_paper_state").fetchall()
            trades = conn.execute("""
                SELECT id,strategy_id,strategy_name,symbol,status,entry_price,exit_price,quantity,
                       allocated_usd,stop_price,take_price,pnl_usd,entry_at,exit_at,exit_reason,last_price
                FROM tradeboot_paper_trades ORDER BY id DESC LIMIT 50
            """).fetchall()
            counts = conn.execute("""
                SELECT COUNT(*) FILTER (WHERE status='OPEN'), COUNT(*) FILTER (WHERE status='CLOSED'),
                       COALESCE(SUM(pnl_usd) FILTER (WHERE status='CLOSED'),0)
                FROM tradeboot_paper_trades
            """).fetchone()
        state = {k: {"value": json.loads(v), "updated_at": t.isoformat()} for k,v,t in rows}
        trade_items = [{"id":r[0],"strategy_id":r[1],"strategy_name":r[2],"symbol":r[3],"status":r[4],
                  "entry_price":r[5],"exit_price":r[6],"quantity":r[7],"allocated_usd":r[8],"stop_price":r[9],
                  "take_price":r[10],"pnl_usd":r[11],"entry_at":r[12].isoformat(),"exit_at":r[13].isoformat() if r[13] else None,
                  "exit_reason":r[14],"last_price":r[15],
                  "unrealized_pnl_usd":((float(r[15])-float(r[5]))*float(r[7]) if r[4]=="OPEN" and r[15] is not None else 0.0)} for r in trades]
        return {"ok": True, "mode": "paper_only", "worker": state.get("worker", {}).get("value", {"status":"starting"}),
                "open_trades": int(counts[0]), "closed_trades": int(counts[1]), "realized_pnl_usd": float(counts[2]),
                "unrealized_pnl_usd": sum(t["unrealized_pnl_usd"] for t in trade_items), "trades": trade_items}
    except Exception:
        return {"ok": False, "mode": "paper_only", "worker": {"status":"unavailable"}, "error":"paper_storage_unavailable"}


def run_cycle():
    started = datetime.now(timezone.utc)
    symbols = list(_SYMBOLS)
    enabled_strategies = [s for s in list_strategies() if s.get("enabled")]
    active = [s for s in enabled_strategies if s.get("strategyType", "formula") == "formula"]
    active_programs = [s for s in enabled_strategies if s.get("strategyType") == "python_program"]
    capital = get_general_capital()
    evaluated = 0
    matches = []
    # The reviewed engines are integrated, but this worker's current eToro adapter
    # supplies one quote snapshot, not the historical OHLCV and intraminute ticks
    # these four programs require. Never fabricate bars or signals from snapshots.
    skipped = [{"strategy_id": s["id"], "strategy_name": s["name"],
                "reason": "historical_ohlcv_and_intraminute_tick_feed_required"}
               for s in active_programs]
    for symbol in symbols:
        status, market = fetch_market_context(symbol)
        if status != 200 or not market.get("ok") or not market.get("quote_is_usable"):
            skipped.append({"symbol":symbol,"reason":"quote_unusable","status":status,
                            "quality":market.get("quote_quality_status"),
                            "freshness_status":market.get("freshness_status"),
                            "quote_age_seconds":market.get("quote_age_seconds"),
                            "max_quote_age_seconds":market.get("max_quote_age_seconds"),
                            "market_open":market.get("market_open"),
                            "delayed_quote":market.get("delayed_quote"),
                            "stage":market.get("stage"),
                            "error":market.get("error")})
            continue
        values = market.get("market_values") or {}
        price = values.get("price")
        if price is None:
            price = (values.get("bid", 0) + values.get("ask", 0))/2 if values.get("bid") and values.get("ask") else None
        if not isinstance(price, (int,float)) or price <= 0:
            skipped.append({"symbol":symbol,"reason":"price_missing"})
            continue
        try:
            quote_at = datetime.fromisoformat(str(market["quote_time"]).replace("Z","+00:00"))
            if quote_at.tzinfo is None:
                quote_at = quote_at.replace(tzinfo=timezone.utc)
        except Exception:
            skipped.append({"symbol":symbol,"reason":"quote_timestamp_invalid"})
            continue
        for strategy in active:
            try:
                result = evaluate_formula(strategy["formula"], values)
                matched = bool(result.get("matched"))
            except (StrategyFormulaError, Exception) as exc:
                skipped.append({"symbol":symbol,"strategy_id":strategy["id"],"reason":"formula_or_data_error","detail":type(exc).__name__})
                continue
            evaluated += 1
            matches.append({"strategy_id":strategy["id"],"strategy_name":strategy["name"],"symbol":symbol,
                            "matched":matched,"price":price,"at":started.isoformat()})
            matches = matches[-50:]
            allocation = capital * float(strategy.get("allocationPercent",10))/100
            with _connect() as conn:
                _init_tables(conn)
                with conn.transaction():
                    open_row = conn.execute("""
                        SELECT id,entry_price,quantity,stop_price,take_price FROM tradeboot_paper_trades
                        WHERE strategy_id=%s AND symbol=%s AND status='OPEN' FOR UPDATE
                    """,(strategy["id"],symbol)).fetchone()
                    if open_row:
                        tid, entry, qty, stop, take = open_row
                        reason = "stop_loss" if price <= stop else ("take_profit" if price >= take else ("formula_false" if not matched else None))
                        if not reason:
                            conn.execute("UPDATE tradeboot_paper_trades SET last_price=%s,last_quote_at=%s WHERE id=%s", (price,quote_at,tid))
                        if reason:
                            pnl = (price-entry)*qty
                            conn.execute("""UPDATE tradeboot_paper_trades SET status='CLOSED',exit_price=%s,pnl_usd=%s,
                              exit_at=%s,exit_reason=%s,last_quote_at=%s WHERE id=%s""",
                              (price,pnl,started,reason,quote_at,tid))
                    elif matched and allocation > 0:
                        # The configured percentage is a TOTAL strategy exposure cap,
                        # shared across all symbols, not a fresh allocation per ticker.
                        already_allocated = conn.execute("""
                            SELECT COALESCE(SUM(allocated_usd), 0)
                            FROM tradeboot_paper_trades
                            WHERE strategy_id=%s AND status='OPEN'
                        """, (strategy["id"],)).fetchone()[0]
                        remaining_allocation = max(0.0, allocation - float(already_allocated or 0))
                        if remaining_allocation > 0:
                            qty = remaining_allocation / price
                            conn.execute("""INSERT INTO tradeboot_paper_trades
                              (strategy_id,strategy_name,symbol,status,entry_price,quantity,allocated_usd,stop_price,take_price,entry_at,last_quote_at,last_price)
                              VALUES (%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s,%s)""",
                              (strategy["id"],strategy["name"],symbol,price,qty,remaining_allocation,price*(1-_STOP_PCT/100),
                               price*(1+_TAKE_PCT/100),started,quote_at,price))
    _state("worker", {"status":"running","last_cycle_at":started.isoformat(),"next_cycle_seconds":_INTERVAL,
                      "symbols":symbols,"active_formula_strategies":len(active),
                      "active_program_strategies":len(active_programs),
                      "program_engine_status":"waiting_for_historical_ohlcv_and_intraminute_ticks" if active_programs else "idle",
                      "evaluations":evaluated,"matches":matches,"skipped":skipped[:30],
                      "trading_mode":"paper_only","real_orders_enabled":False})


def _loop():
    print("Tradeboot paper-trading loop entered", flush=True)
    while True:
        try:
            run_cycle()
            print("Tradeboot paper-trading cycle completed", flush=True)
        except Exception as exc:
            print(f"Tradeboot paper-trading cycle failed: {type(exc).__name__}", flush=True)
            traceback.print_exc()
            try:
                _state("worker", {"status":"error","last_cycle_at":datetime.now(timezone.utc).isoformat(),
                                  "error":type(exc).__name__,"trading_mode":"paper_only","real_orders_enabled":False})
            except Exception as state_exc:
                print(f"Tradeboot paper-trading error status could not be persisted: {type(state_exc).__name__}", flush=True)
        time.sleep(_INTERVAL)


def start_paper_trading_worker():
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="tradeboot-paper-trader", daemon=True)
        _thread.start()
