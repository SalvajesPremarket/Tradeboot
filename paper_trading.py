"""Persistent simulation-only paper trading loop for Tradeboot.

Uses confirmed eToro snapshots and saved formula strategies. Never places broker orders.
"""
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from strategy_store import _connect, list_strategies, get_general_capital
from strategy_engine import evaluate_formula, StrategyFormulaError
from strategy_program_engine import simulate_program
from app import fetch_market_context
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# Read-only provider adapters: automatic candidate discovery is US-only via Schwab
# NASDAQ/NYSE market movers; Twelve Data is only a fallback for US-symbol market data.
from twelve_data_integration import (
    get_global_candidates, get_symbol_metadata, get_scanner_context,
    get_price_history as get_twelve_price_history, get_quote as get_twelve_quote,
    get_diagnostics as get_twelve_data_diagnostics,
    is_configured as twelve_data_configured,
)

_INTERVAL = max(30, int(os.environ.get("PAPER_TRADING_INTERVAL_SECONDS", "30")))
_TICK_MAX_AGE_SECONDS = 30
_STOP_PCT = max(0.1, min(20.0, float(os.environ.get("PAPER_TRADING_STOP_PCT", "2"))))
_TAKE_PCT = max(0.1, min(50.0, float(os.environ.get("PAPER_TRADING_TAKE_PCT", "4"))))
# Formula strategies keep their explicit watchlist. JeanLong uses a separate
# pre-market candidate universe so the default mega-cap symbols cannot silently
# block its own $1-$20 / low-float scanner filters.
_SYMBOLS = tuple(dict.fromkeys(
    s.strip().upper() for s in os.environ.get("PAPER_TRADING_SYMBOLS", "AAPL,MSFT,NVDA").split(",")
    if s.strip() and len(s.strip()) <= 20
))
_EXPLICIT_PROGRAM_SYMBOLS = os.environ.get("PAPER_TRADING_SYMBOLS", "").strip()
# Optional independent universe for reviewed Python scanners; this does not change
# the formula-strategy watchlist.
_PROGRAM_SYMBOLS_RAW = os.environ.get("PAPER_TRADING_PROGRAM_SYMBOLS", "").strip()
_PROGRAM_SYMBOLS = tuple(dict.fromkeys(
    s.strip().upper() for s in _PROGRAM_SYMBOLS_RAW.split(",")
    if s.strip() and len(s.strip()) <= 20
))


def _program_scan_symbols(now):
    """Return only US-listed candidates discovered from Schwab NASDAQ/NYSE movers.

    Deliberately ignore generic symbol-list overrides here: they can contain
    international listings and would bypass the US-only scanner boundary.
    Formula strategies retain their separate, explicitly configured watchlist.
    """
    return get_global_candidates(now=now, limit=6)


_lock = threading.Lock()
_thread = None
_tick_thread = None
_TICK_COLLECTOR_INTERVAL_SECONDS = max(3, min(10, int(os.environ.get("PAPER_TRADING_TICK_INTERVAL_SECONDS", "5"))))


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
            order_type TEXT,
            time_in_force TEXT,
            extended_hours BOOLEAN,
            limit_price DOUBLE PRECISION
        )
    """)
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS side TEXT NOT NULL DEFAULT 'LONG'")
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS last_quote_at TIMESTAMPTZ")
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS order_type TEXT")
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS time_in_force TEXT")
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS extended_hours BOOLEAN")
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS limit_price DOUBLE PRECISION")
    conn.execute("CREATE INDEX IF NOT EXISTS tradeboot_paper_trades_status_idx ON tradeboot_paper_trades(status, strategy_id, symbol)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tradeboot_paper_events (
            id BIGSERIAL PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            event_at TIMESTAMPTZ NOT NULL,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(strategy_id, symbol, action, event_at)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tradeboot_market_ticks (
            symbol TEXT NOT NULL,
            tick_at TIMESTAMPTZ NOT NULL,
            price DOUBLE PRECISION NOT NULL CHECK (price > 0),
            day_volume DOUBLE PRECISION,
            source TEXT NOT NULL DEFAULT 'schwab_quotes',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (symbol, tick_at)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tradeboot_market_ticks_symbol_time ON tradeboot_market_ticks(symbol, tick_at)")


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
                       allocated_usd,stop_price,take_price,pnl_usd,entry_at,exit_at,exit_reason,side,
                       order_type,time_in_force,extended_hours,limit_price
                FROM tradeboot_paper_trades ORDER BY id DESC LIMIT 50
            """).fetchall()
            counts = conn.execute("""
                SELECT COUNT(*) FILTER (WHERE status='OPEN'), COUNT(*) FILTER (WHERE status='CLOSED'),
                       COALESCE(SUM(pnl_usd) FILTER (WHERE status='CLOSED'),0)
                FROM tradeboot_paper_trades
            """).fetchone()
        state = {k: {"value": json.loads(v), "updated_at": t.isoformat()} for k,v,t in rows}
        worker = state.get("worker", {}).get("value", {"status":"starting"})
        tick_collector = state.get("tick_collector", {}).get("value")
        if tick_collector:
            worker["tick_collector"] = tick_collector
        return {"ok": True, "mode": "paper_only", "worker": worker,
                "open_trades": int(counts[0]), "closed_trades": int(counts[1]), "realized_pnl_usd": float(counts[2]),
                "trades": [{"id":r[0],"strategy_id":r[1],"strategy_name":r[2],"symbol":r[3],"status":r[4],
                  "entry_price":r[5],"exit_price":r[6],"quantity":r[7],"allocated_usd":r[8],"stop_price":r[9],
                  "take_price":r[10],"pnl_usd":r[11],"entry_at":r[12].isoformat(),"exit_at":r[13].isoformat() if r[13] else None,
                  "exit_reason":r[14],"side":r[15],"order_type":r[16],"time_in_force":r[17],
                  "extended_hours":r[18],"limit_price":r[19]} for r in trades]}
    except Exception as exc:
        # Keep the UI message safe while making the failure class diagnosable in Render logs/API.
        print(f"Tradeboot paper status query failed: {type(exc).__name__}", flush=True)
        traceback.print_exc()
        return {"ok": False, "mode": "paper_only", "worker": {"status":"unavailable"},
                "error":"paper_storage_unavailable", "diagnostic":type(exc).__name__}


def run_cycle():
    started = datetime.now(timezone.utc)
    formula_symbols = list(_SYMBOLS)
    enabled_strategies = [s for s in list_strategies() if s.get("enabled")]
    active = [s for s in enabled_strategies if s.get("strategyType", "formula") == "formula"]
    active_programs = [s for s in enabled_strategies if s.get("strategyType") == "python_program"]
    program_symbols = _program_scan_symbols(started) if active_programs else []
    symbols = list(dict.fromkeys(formula_symbols + program_symbols))
    capital = get_general_capital()
    evaluated = 0
    skipped = []
    program_skipped = []
    program_evaluations = []
    scanner_context_summaries = {}
    if active_programs and not program_symbols:
        scan_diagnostic = dict(get_twelve_data_diagnostics().get(
            "market_movers/stocks",
            {"status": "missing_api_key" if not twelve_data_configured() else "no_usable_candidates"},
        ))
        program_skipped.append({
            "reason": "global_candidate_scan_unavailable",
            "detail": "No US candidate source returned usable symbols; inspect scanner_diagnostic for provider-specific errors.",
            "scanner_context_provider": "twelve_data" if twelve_data_configured() else "missing_twelve_data_api_key",
            "scanner_diagnostic": scan_diagnostic,
        })
    # Formula strategies use their explicit eToro watchlist. Reviewed Python
    # scanners use a separate bounded candidate universe and Schwab history.
    for symbol in (formula_symbols if active else []):
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
                              (strategy_id,strategy_name,symbol,status,entry_price,quantity,allocated_usd,stop_price,take_price,entry_at,last_quote_at)
                              VALUES (%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s)""",
                              (strategy["id"],strategy["name"],symbol,price,qty,remaining_allocation,price*(1-_STOP_PCT/100),
                               price*(1+_TAKE_PCT/100),started,quote_at))

    if active_programs:
        from schwab_integration import get_price_history as get_schwab_price_history
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
        now_minute = started.replace(second=0, microsecond=0)
        for symbol in program_symbols:
            symbol_meta = get_symbol_metadata(symbol)
            country = str(symbol_meta.get("country", "")).strip().lower()
            is_us_listing = not symbol_meta or country in ("us", "usa", "united states")
            history_provider = "schwab" if is_us_listing else "twelve_data"
            if is_us_listing:
                minute_status, minute_payload = get_schwab_price_history(symbol, daily=False)
                daily_status, daily_payload = get_schwab_price_history(symbol, daily=True)
                if ((minute_status != 200 or not minute_payload.get("ok") or daily_status != 200 or not daily_payload.get("ok"))
                    and twelve_data_configured()):
                    td_minute_status, td_minute_payload = get_twelve_price_history(symbol, daily=False, metadata=symbol_meta)
                    td_daily_status, td_daily_payload = get_twelve_price_history(symbol, daily=True, metadata=symbol_meta)
                    if td_minute_status == 200 and td_minute_payload.get("ok") and td_daily_status == 200 and td_daily_payload.get("ok"):
                        minute_status, minute_payload = td_minute_status, td_minute_payload
                        daily_status, daily_payload = td_daily_status, td_daily_payload
                        history_provider = "twelve_data"
            else:
                minute_status, minute_payload = get_twelve_price_history(symbol, daily=False, metadata=symbol_meta)
                daily_status, daily_payload = get_twelve_price_history(symbol, daily=True, metadata=symbol_meta)
            if minute_status != 200 or not minute_payload.get("ok"):
                program_skipped.append({"symbol": symbol, "reason": history_provider + "_minute_history_unavailable",
                                "status": minute_status, "detail": minute_payload.get("error")})
                continue
            if daily_status != 200 or not daily_payload.get("ok"):
                program_skipped.append({"symbol": symbol, "reason": history_provider + "_daily_history_unavailable",
                                "status": daily_status, "detail": daily_payload.get("error")})
                continue

            def clean_candle(row, field):
                if not isinstance(row, dict):
                    return None
                try:
                    raw_time = row.get("datetime")
                    if isinstance(raw_time, (int, float)):
                        stamp = datetime.fromtimestamp(float(raw_time) / 1000, timezone.utc)
                    elif isinstance(raw_time, str):
                        stamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            stamp = stamp.replace(tzinfo=timezone.utc)
                    else:
                        return None
                    values = {key: float(row[key]) for key in ("open", "high", "low", "close")}
                    volume = float(row.get("volume", 0))
                    if (not all(__import__("math").isfinite(v) and v > 0 for v in values.values())
                        or not __import__("math").isfinite(volume) or volume < 0
                        or values["low"] > min(values["open"], values["close"])
                        or values["high"] < max(values["open"], values["close"])
                        or values["low"] > values["high"]):
                        return None
                    if field == "minute" and stamp >= now_minute:
                        return None  # never act on a still-forming candle
                    return {"timestamp": stamp.astimezone(timezone.utc).isoformat(),
                            **values, "volume": volume, "_stamp": stamp}
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    return None

            minute_rows = [clean_candle(row, "minute") for row in minute_payload.get("candles", [])]
            daily_rows = [clean_candle(row, "daily") for row in daily_payload.get("candles", [])]
            minute_rows = sorted([row for row in minute_rows if row], key=lambda x: x["_stamp"])
            daily_rows = sorted([row for row in daily_rows if row], key=lambda x: x["_stamp"])
            # Pullback D/W context must use completed sessions, not today's partial daily candle.
            current_eastern_date = started.astimezone(eastern).date()
            daily_rows = [row for row in daily_rows if row["_stamp"].astimezone(eastern).date() < current_eastern_date]
            if not minute_rows:
                program_skipped.append({"symbol": symbol, "reason": "no_completed_minute_candles",
                                        "received_candles": len(minute_rows),
                                        "validated_completed_candles": 0,
                                        "cycle_minute_utc": started.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()})
                continue
            # Historical candles initialize indicators only. Do not require a candle
            # from today's session: Schwab may omit premarket/current-session bars.
            # Live entry-pattern ticks are fetched separately below from Schwab quotes.
            seed_rows = [row for row in minute_rows if row["_stamp"] < started]
            if not seed_rows:
                program_skipped.append({"symbol": symbol, "reason": "no_historical_seed_candles"})
                continue
            # Remove private parsing fields before passing validated OHLCV to the engines.
            seed_candles = [{k:v for k,v in row.items() if not k.startswith("_")} for row in seed_rows[-4000:]]
            # Capture a provider quote; historical minute closes are NOT live ticks.
            quote = None
            quote_provider = history_provider
            quote_status, quote_payload = 503, {"error": "quote_not_requested"}
            if history_provider == "schwab":
                from schwab_integration import get_quotes
                quote_status, quote_payload = get_quotes([symbol])
                if quote_status == 200 and quote_payload.get("ok"):
                    raw_symbol = quote_payload.get("quotes", {}).get(symbol, {})
                    quote = raw_symbol.get("quote", raw_symbol) if isinstance(raw_symbol, dict) else None
                if not isinstance(quote, dict) and twelve_data_configured():
                    quote_status, quote_payload = get_twelve_quote(symbol, metadata=symbol_meta)
                    quote = quote_payload.get("quote") if quote_status == 200 and quote_payload.get("ok") else None
                    if isinstance(quote, dict):
                        quote_provider = "twelve_data"
            else:
                quote_status, quote_payload = get_twelve_quote(symbol, metadata=symbol_meta)
                quote = quote_payload.get("quote") if quote_status == 200 and quote_payload.get("ok") else None
            if not isinstance(quote, dict):
                program_skipped.append({"symbol": symbol, "reason": quote_provider + "_live_quote_unavailable",
                                "status": quote_status, "detail": quote_payload.get("error")})
                continue
            # Validate provider timestamps against response-receipt time, not cycle start.
            # History requests may take several seconds before this live quote is fetched.
            quote_received_at = datetime.now(timezone.utc)
            age = None
            timestamp_field = None
            live_at = None
            try:
                live_price = float(quote.get("lastPrice") or quote.get("mark") or 0)
                # Parse every available timestamp. Prefer the newest timestamp that
                # satisfies the freshness window; a valid-but-old preferred field
                # must not hide a fresh alternative supplied by Schwab.
                parsed_stamps = []
                for field in ("quoteTimeInLong", "tradeTimeInLong", "tradeTime", "quoteTime"):
                    candidate = quote.get(field)
                    if candidate is None or candidate == "":
                        continue
                    try:
                        if isinstance(candidate, str) and not candidate.strip().isdigit():
                            parsed_at = datetime.fromisoformat(candidate.strip().replace("Z", "+00:00"))
                            if parsed_at.tzinfo is None:
                                parsed_at = parsed_at.replace(tzinfo=timezone.utc)
                        else:
                            stamp = float(candidate)
                            if not __import__("math").isfinite(stamp) or stamp <= 0:
                                raise ValueError("invalid_timestamp")
                            parsed_at = datetime.fromtimestamp(
                                stamp / (1000 if stamp > 10_000_000_000 else 1), timezone.utc
                            )
                        parsed_at = parsed_at.astimezone(timezone.utc)
                        stamp_age = (quote_received_at - parsed_at).total_seconds()
                        parsed_stamps.append((parsed_at, field, stamp_age))
                    except (TypeError, ValueError, OverflowError, OSError):
                        continue
                if not parsed_stamps:
                    raise ValueError("quote_timestamp_unavailable")
                fresh_stamps = [item for item in parsed_stamps
                                if -10 <= item[2] <= _TICK_MAX_AGE_SECONDS]
                # Never fall back to a timestamp outside the accepted freshness window.
                # A future timestamp must not be silently preferred over valid market time.
                if not fresh_stamps:
                    if parsed_stamps:
                        newest = max(parsed_stamps, key=lambda item: item[0])
                        live_at, timestamp_field, age = newest
                        raise ValueError("quote_timestamp_outside_freshness_window")
                    raise ValueError("quote_timestamp_unavailable")
                live_at, timestamp_field, age = max(fresh_stamps, key=lambda item: item[0])
                if live_price <= 0 or not __import__("math").isfinite(live_price):
                    raise ValueError("quote_price_invalid")
                if age < -10 or age > _TICK_MAX_AGE_SECONDS:
                    raise ValueError("quote_stale_or_invalid")
                raw_volume = quote.get("totalVolume", quote.get("volume"))
                live_volume = float(raw_volume) if raw_volume is not None else None
                if live_volume is not None and (not __import__("math").isfinite(live_volume) or live_volume < 0):
                    live_volume = None
                with _connect() as conn:
                    _init_tables(conn)
                    conn.execute("""
                        INSERT INTO tradeboot_market_ticks(symbol,tick_at,price,day_volume,source)
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT(symbol,tick_at) DO UPDATE SET
                          price=EXCLUDED.price, day_volume=COALESCE(EXCLUDED.day_volume,tradeboot_market_ticks.day_volume)
                    """, (symbol, live_at.astimezone(timezone.utc), live_price, live_volume, "twelve_data_quotes" if quote_provider == "twelve_data" else "schwab_quotes"))
            except (TypeError, ValueError, OverflowError, OSError) as exc:
                diagnostic = {"symbol": symbol, "reason": quote_provider + "_live_quote_stale_or_invalid",
                              "detail": str(exc)[:80]}
                if "age" in locals() and isinstance(age, (int, float)):
                    diagnostic["age_seconds"] = round(age, 1)
                    diagnostic["max_age_seconds"] = _TICK_MAX_AGE_SECONDS
                if "timestamp_field" in locals() and timestamp_field:
                    diagnostic["timestamp_field"] = timestamp_field
                if "live_at" in locals() and live_at is not None:
                    diagnostic["quote_timestamp_utc"] = live_at.isoformat()
                program_skipped.append(diagnostic)
                continue

            # Feed only stored real quote timestamps to JeanLong. Minute candles seed indicators only.
            day_start = datetime.combine(started.astimezone(eastern).date(), datetime.min.time(), tzinfo=eastern)
            with _connect() as conn:
                _init_tables(conn)
                tick_rows = conn.execute("""
                    SELECT tick_at,price,day_volume FROM tradeboot_market_ticks
                    WHERE symbol=%s AND tick_at >= %s AND tick_at <= %s
                    ORDER BY tick_at DESC LIMIT 5000
                """, (symbol, day_start.astimezone(timezone.utc), quote_received_at + timedelta(seconds=10))).fetchall()
            # SQL selects the newest 5,000 ticks; restore chronological order for engines.
            tick_rows.reverse()
            ticks = [{"price": float(row[1]), "timestamp": row[0].astimezone(timezone.utc).isoformat(),
                      "day_volume": float(row[2]) if row[2] is not None else None}
                     for row in tick_rows]
            if not ticks:
                program_skipped.append({"symbol": symbol, "reason": "no_persisted_live_ticks"})
                continue
            cumulative_volume = next((tick["day_volume"] for tick in reversed(ticks) if tick["day_volume"] is not None), 0.0)
            daily_candles = [{k:v for k,v in row.items() if not k.startswith("_")} for row in daily_rows[-1000:]]
            if len(daily_candles) < 60:
                program_skipped.append({"symbol": symbol, "reason": "insufficient_daily_history",
                                "daily_candles": len(daily_candles)})
                continue
            daily_volumes = [row["volume"] for row in daily_candles[-30:] if row["volume"] >= 0]
            scanner_context = get_scanner_context(symbol, started, symbol_meta)
            context_diag = scanner_context.get("diagnostic", {})
            scanner_context_summaries[symbol] = {
                "float_available": bool(context_diag.get("float_available")),
                "float_provider": context_diag.get("float_provider"),
                "float_status": context_diag.get("float_status"),
                "float_attempts": context_diag.get("float_attempts", []),
                "news_status": context_diag.get("status"),
                "news_provider": context_diag.get("news_provider"),
                "news_count": context_diag.get("news_count", 0),
                "news_latest_at": context_diag.get("news_latest_at"),
            }
            context = {
                "prev_close": daily_candles[-1]["close"] if daily_candles else None,
                "avg_volume_30d": (sum(daily_volumes) / len(daily_volumes)) if daily_volumes else None,
                "volume_today": cumulative_volume,
                "float_shares": scanner_context.get("float_shares"),
                "news": scanner_context.get("news", []),
            }
            context = {key:value for key,value in context.items() if value is not None}

            for strategy in active_programs:
                try:
                    from strategy_program_engine import resolve_supported_program
                    spec = resolve_supported_program(strategy["name"], strategy["formula"])
                    if not spec:
                        program_skipped.append({"symbol": symbol, "strategy_id": strategy["id"],
                                        "reason": "unsupported_reviewed_strategy"})
                        continue
                    module, _class_name, kind = spec
                    payload = {
                        "strategyName": strategy["name"], "formula": strategy["formula"],
                        "symbol": symbol, "capital": capital * float(strategy.get("allocationPercent", 10)) / 100,
                        "candles": seed_candles, "ticks": ticks, "context": context,
                    }
                    if kind == "pullback":
                        payload["daily_candles"] = daily_candles
                    result = simulate_program(payload)
                    result_diagnostics = result.get("diagnostics", {})
                    program_evaluations.append({
                        "symbol": symbol,
                        "strategy_id": strategy["id"],
                        "actions_generated": len(result.get("actions", [])),
                        "last_reject_reasons": result_diagnostics.get("last_reject_reasons", []),
                        "last_metrics": result_diagnostics.get("last_metrics", {}),
                        "last_pattern_state": result_diagnostics.get("last_pattern_state"),
                        "last_wait_reason": result_diagnostics.get("last_wait_reason"),
                        "scanner_context_diagnostic": scanner_context.get("diagnostic", {}),
                    })
                    new_actions = 0
                    with _connect() as conn:
                        _init_tables(conn)
                        with conn.transaction():
                            for action in result.get("actions", []):
                                event_at = action.get("event_at")
                                if not event_at or action.get("action") not in ("ENTER", "EXIT", "MOVE_STOP"):
                                    continue
                                try:
                                    event_time = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
                                    if event_time.tzinfo is None:
                                        event_time = event_time.replace(tzinfo=timezone.utc)
                                except (TypeError, ValueError):
                                    continue
                                side = str(action.get("side", "LONG")).upper()
                                is_jean_long = strategy.get("name") == "JeanLong"
                                if is_jean_long and action["action"] == "ENTER":
                                    # JeanLong may open LONG paper positions from 04:00 until 16:00 New York time.
                                    eastern_event_time = event_time.astimezone(eastern)
                                    premarket_time = eastern_event_time.time()
                                    if side != "LONG" or not (datetime.strptime("04:00", "%H:%M").time() <= premarket_time < datetime.strptime("16:00", "%H:%M").time()):
                                        continue
                                inserted = conn.execute("""
                                    INSERT INTO tradeboot_paper_events(strategy_id,symbol,action,event_at,reason)
                                    VALUES (%s,%s,%s,%s,%s)
                                    ON CONFLICT(strategy_id,symbol,action,event_at) DO NOTHING
                                    RETURNING id
                                """, (strategy["id"], symbol, action["action"], event_time, str(action.get("reason", ""))[:160])).fetchone()
                                if not inserted:
                                    continue
                                new_actions += 1
                                side = str(action.get("side", "LONG")).upper()
                                price = action.get("price")
                                if not isinstance(price, (int, float)) or not __import__("math").isfinite(price) or price <= 0:
                                    continue
                                open_row = conn.execute("""
                                    SELECT id,entry_price,quantity,allocated_usd,stop_price,side
                                    FROM tradeboot_paper_trades
                                    WHERE strategy_id=%s AND symbol=%s AND status='OPEN'
                                    ORDER BY id DESC LIMIT 1 FOR UPDATE
                                """, (strategy["id"], symbol)).fetchone()
                                if action["action"] == "ENTER" and not open_row:
                                    allocation = capital * float(strategy.get("allocationPercent", 10)) / 100
                                    used = conn.execute("""
                                        SELECT COALESCE(SUM(allocated_usd),0) FROM tradeboot_paper_trades
                                        WHERE strategy_id=%s AND status='OPEN'
                                    """, (strategy["id"],)).fetchone()[0]
                                    budget = max(0.0, allocation - float(used or 0))
                                    if budget <= 0:
                                        continue
                                    stop = action.get("stop")
                                    if not isinstance(stop, (int, float)) or stop <= 0:
                                        stop = price * (1 - _STOP_PCT / 100 if side == "LONG" else 1 + _STOP_PCT / 100)
                                    take = price * (1 + _TAKE_PCT / 100 if side == "LONG" else 1 - _TAKE_PCT / 100)
                                    conn.execute("""
                                        INSERT INTO tradeboot_paper_trades
                                        (strategy_id,strategy_name,symbol,status,entry_price,quantity,allocated_usd,
                                         stop_price,take_price,entry_at,last_quote_at,side,order_type,time_in_force,extended_hours,limit_price)
                                        VALUES (%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                    """, (strategy["id"],strategy["name"],symbol,price,budget/price,budget,
                                          stop,take,event_time,event_time,side,
                                          "LIMIT" if is_jean_long else None,
                                          "DAY" if is_jean_long else None,
                                          True if is_jean_long else None,
                                          round(price * 1.01, 2) if is_jean_long else None))
                                elif action["action"] == "MOVE_STOP" and open_row:
                                    old_stop = float(open_row[4])
                                    improves = price > 0 and ((side == "LONG" and isinstance(action.get("stop"), (int,float)) and action["stop"] > old_stop)
                                                              or (side == "SHORT" and isinstance(action.get("stop"), (int,float)) and action["stop"] < old_stop))
                                    if improves:
                                        conn.execute("UPDATE tradeboot_paper_trades SET stop_price=%s,last_quote_at=%s WHERE id=%s",
                                                     (action["stop"],event_time,open_row[0]))
                                elif action["action"] == "EXIT" and open_row:
                                    trade_id, entry, quantity, allocated, old_stop, open_side = open_row
                                    pnl = ((price - entry) if open_side == "LONG" else (entry - price)) * quantity
                                    conn.execute("""
                                        UPDATE tradeboot_paper_trades SET status='CLOSED',exit_price=%s,pnl_usd=%s,
                                        exit_at=%s,exit_reason=%s,last_quote_at=%s WHERE id=%s
                                    """, (price,pnl,event_time,str(action.get("reason","strategy_exit"))[:160],event_time,trade_id))
                    evaluated += 1
                    if result.get("diagnostics", {}).get("last_reject_reasons"):
                        program_skipped.append({"symbol":symbol,"strategy_id":strategy["id"],
                                        "reason":"strategy_rejected_or_waiting","diagnostics":result["diagnostics"]["last_reject_reasons"]})
                except Exception as exc:
                    # Preserve validation details without exposing credentials or stopping other symbols.
                    program_skipped.append({"symbol": symbol, "strategy_id": strategy["id"],
                                    "reason": "program_replay_failed", "detail": type(exc).__name__,
                                    "message": str(exc)[:180]})

    scan_diagnostic = get_twelve_data_diagnostics().get("market_movers/stocks", {})
    scan_provider = scan_diagnostic.get("provider")
    scan_sources = {
        "schwab": "schwab_us_market_movers",
        "yahoo_finance": "yahoo_us_day_gainers",
        "finviz": "finviz_us_screener",
        "nasdaq_screener": "nasdaq_public_screener",
        "last_known_us_candidates": "last_known_us_candidates",
        "us_seed_fallback": "us_seed_fallback_live_quotes",
    }
    scan_source = scan_sources.get(scan_provider, "no_candidate_feed")
    scan_log = {
        "source": scan_source,
        "provider": scan_diagnostic.get("provider"),
        "status": scan_diagnostic.get("status"),
        "detail": scan_diagnostic.get("detail"),
        "candidate_count": scan_diagnostic.get("candidate_count", len(program_symbols)),
        "symbols": program_symbols,
        "schwab_errors": scan_diagnostic.get("schwab_errors", []),
        "yahoo_error": scan_diagnostic.get("yahoo_error"),
        "finviz_error": scan_diagnostic.get("finviz_error"),
        "nasdaq_errors": scan_diagnostic.get("nasdaq_errors", []),
        "evaluations": evaluated,
        "skip_reason_counts": {
            reason: sum(1 for item in program_skipped if item.get("reason") == reason)
            for reason in sorted({item.get("reason", "unknown") for item in program_skipped})
        },
    }
    print("Tradeboot scanner diagnostic: " + json.dumps(scan_log, ensure_ascii=False, sort_keys=True), flush=True)
    _state("worker", {"status":"running","last_cycle_at":started.isoformat(),"next_cycle_seconds":_INTERVAL,
                      "symbols":symbols,"formula_symbols":formula_symbols,"program_scan_symbols":program_symbols,
                      "program_scan_source":scan_source,
                      "active_formula_strategies":len(active),
                      "active_program_strategies":len(active_programs),
                      "program_engine_status":"provider_live_ticks_plus_historical_indicator_seed" if active_programs else "idle",
                      "scanner_context_provider":"twelve_data" if twelve_data_configured() else "missing_twelve_data_api_key",
                      "scanner_diagnostic":dict(get_twelve_data_diagnostics().get("market_movers/stocks", {"status":"missing_api_key"} if not twelve_data_configured() else {"status":"not_requested"})),
                      "evaluations":evaluated,"skipped":skipped[:30],
                      "program_skipped":program_skipped[:100],
                      "program_evaluations":program_evaluations[:100],
                      "program_skip_reason_counts":{reason:sum(1 for item in program_skipped if item.get("reason")==reason) for reason in sorted({item.get("reason","unknown") for item in program_skipped})},
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


def _tick_collector_loop():
    """Collect fresh, read-only quotes between strategy cycles so 20-second entry windows are observable."""
    from schwab_integration import get_quotes

    print("Tradeboot live tick collector entered", flush=True)
    while True:
        try:
            with _connect() as conn:
                _init_tables(conn)
                row = conn.execute(
                    "SELECT state_value FROM tradeboot_paper_state WHERE state_key='worker'"
                ).fetchone()
            worker = json.loads(row[0]) if row and row[0] else {}
            symbols = worker.get("program_scan_symbols") or []
            symbols = list(dict.fromkeys(
                str(symbol).strip().upper() for symbol in symbols
                if isinstance(symbol, str) and symbol.strip()
            ))[:25]
            quote_status = None
            quote_error = None
            received_quotes = 0
            persisted_ticks = 0
            if symbols:
                status, payload = get_quotes(symbols)
                quote_status = status
                quote_error = payload.get("error") if isinstance(payload, dict) else "invalid_quote_response"
                quotes = payload.get("quotes", {}) if status == 200 and payload.get("ok") else {}
                received_quotes = len(quotes) if isinstance(quotes, dict) else 0
                now = datetime.now(timezone.utc)
                rows = []
                for symbol in symbols:
                    raw = quotes.get(symbol)
                    quote = raw.get("quote", raw) if isinstance(raw, dict) else None
                    if not isinstance(quote, dict):
                        continue
                    try:
                        price = float(quote.get("lastPrice") or quote.get("mark") or 0)
                        stamps = []
                        for field in ("quoteTimeInLong", "tradeTimeInLong", "tradeTime", "quoteTime"):
                            value = quote.get(field)
                            if value is None or value == "":
                                continue
                            try:
                                if isinstance(value, str) and not value.strip().isdigit():
                                    stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                                    if stamp.tzinfo is None:
                                        stamp = stamp.replace(tzinfo=timezone.utc)
                                else:
                                    numeric = float(value)
                                    stamp = datetime.fromtimestamp(
                                        numeric / (1000 if numeric > 10_000_000_000 else 1),
                                        timezone.utc,
                                    )
                                stamp = stamp.astimezone(timezone.utc)
                                age = (now - stamp).total_seconds()
                                if -10 <= age <= _TICK_MAX_AGE_SECONDS:
                                    stamps.append(stamp)
                            except (TypeError, ValueError, OverflowError, OSError):
                                continue
                        if not stamps or not __import__("math").isfinite(price) or price <= 0:
                            continue
                        stamp = max(stamps)
                        volume_value = quote.get("totalVolume", quote.get("volume"))
                        try:
                            volume = float(volume_value) if volume_value is not None else None
                            if volume is not None and (not __import__("math").isfinite(volume) or volume < 0):
                                volume = None
                        except (TypeError, ValueError):
                            volume = None
                        rows.append((symbol, stamp, price, volume))
                    except (TypeError, ValueError, OverflowError, OSError):
                        continue
                if rows:
                    with _connect() as conn:
                        _init_tables(conn)
                        with conn.transaction():
                            for symbol, stamp, price, volume in rows:
                                conn.execute(
                                    """INSERT INTO tradeboot_market_ticks(symbol,tick_at,price,day_volume,source)
                                       VALUES (%s,%s,%s,%s,'schwab_quotes')
                                       ON CONFLICT(symbol,tick_at) DO UPDATE SET
                                         price=EXCLUDED.price,
                                         day_volume=COALESCE(EXCLUDED.day_volume,tradeboot_market_ticks.day_volume)""",
                                    (symbol, stamp, price, volume),
                                )
                    persisted_ticks = len(rows)
            _state("tick_collector", {
                "status": "ok" if persisted_ticks else ("no_symbols" if not symbols else "no_fresh_quotes"),
                "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                "symbols_requested": len(symbols),
                "quotes_received": received_quotes,
                "fresh_ticks_persisted": persisted_ticks,
                "quote_http_status": quote_status,
                "detail": quote_error if quote_status not in (200, None) else None,
                "interval_seconds": _TICK_COLLECTOR_INTERVAL_SECONDS,
            })
            if not persisted_ticks:
                print("Tradeboot tick collector diagnostic: " + json.dumps({
                    "status": "no_symbols" if not symbols else "no_fresh_quotes",
                    "symbols": symbols,
                    "quotes_received": received_quotes,
                    "quote_http_status": quote_status,
                    "detail": quote_error if quote_status not in (200, None) else None,
                    "max_quote_age_seconds": _TICK_MAX_AGE_SECONDS,
                }, ensure_ascii=False, sort_keys=True), flush=True)
        except Exception as exc:
            # Keep the collector isolated: a quote/API issue must not stop the strategy worker.
            try:
                _state("tick_collector", {
                    "status": "error",
                    "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                    "detail": type(exc).__name__,
                    "interval_seconds": _TICK_COLLECTOR_INTERVAL_SECONDS,
                })
            except Exception:
                pass
            print(f"Tradeboot live tick collector cycle failed: {type(exc).__name__}", flush=True)
        time.sleep(_TICK_COLLECTOR_INTERVAL_SECONDS)


def start_paper_trading_worker():
    global _thread, _tick_thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="tradeboot-paper-trader", daemon=True)
            _thread.start()
        if _tick_thread is None or not _tick_thread.is_alive():
            _tick_thread = threading.Thread(
                target=_tick_collector_loop,
                name="tradeboot-live-tick-collector",
                daemon=True,
            )
            _tick_thread.start()
