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

# Optional FMP enrichment for the mandatory JeanLong float/news scanner filters.
# Credentials are read only from Render environment variables and are never logged.
_FMP_API_KEY = (os.environ.get("FMP_API_KEY") or
                os.environ.get("FINANCIAL_MODELING_PREP_API_KEY") or "").strip()
_FMP_BASE = "https://financialmodelingprep.com/stable/"
_FMP_CACHE = {}
_FMP_CACHE_LOCK = threading.Lock()


def _fmp_json(endpoint, params, cache_key, ttl_seconds):
    if not _FMP_API_KEY:
        return None
    now = time.monotonic()
    with _FMP_CACHE_LOCK:
        cached = _FMP_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    query = dict(params)
    query["apikey"] = _FMP_API_KEY
    request = Request(_FMP_BASE + endpoint + "?" + urlencode(query),
                      headers={"Accept": "application/json", "User-Agent": "Tradeboot/1.0"},
                      method="GET")
    result = None
    try:
        with urlopen(request, timeout=6) as response:
            parsed = json.loads(response.read().decode("utf-8"))
            if isinstance(parsed, list):
                result = parsed
    except Exception:
        # Fail closed: missing enrichment means JeanLong rejects the entry.
        result = None
    with _FMP_CACHE_LOCK:
        _FMP_CACHE[cache_key] = (now + ttl_seconds, result)
    return result


def _fmp_scanner_context(symbol, now):
    """Return only provider-backed float and recent catalyst headlines for one ticker."""
    if not _FMP_API_KEY:
        return {"provider": "unconfigured", "float_shares": None, "news": []}

    float_rows = _fmp_json("shares-float", {"symbol": symbol},
                           "float:" + symbol, 90 * 60)
    float_shares = None
    if float_rows:
        for row in float_rows:
            if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol:
                raw = row.get("floatShares")
                if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
                    float_shares = int(raw)
                    break

    # Batch-by-symbol request is cached for 30 minutes to stay within conservative API limits.
    today = now.astimezone(ZoneInfo("America/New_York")).date()
    from_date = (today - timedelta(days=1)).isoformat()
    to_date = today.isoformat()
    news_rows = _fmp_json("news/stock", {
        "symbols": symbol, "from": from_date, "to": to_date, "limit": 50, "page": 0,
    }, "news:" + symbol, 30 * 60)
    news = []
    if news_rows:
        for row in news_rows:
            if not isinstance(row, dict):
                continue
            row_symbol = str(row.get("symbol", symbol)).upper()
            if row_symbol and row_symbol != symbol:
                continue
            headline = row.get("title") or row.get("headline")
            published = row.get("publishedDate") or row.get("published_at")
            if not isinstance(headline, str) or not isinstance(published, str):
                continue
            try:
                stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    # FMP news timestamps without offsets are interpreted in New York time.
                    stamp = stamp.replace(tzinfo=ZoneInfo("America/New_York"))
                stamp = stamp.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
            news.append({"published_at": stamp, "headline": headline[:500]})
    return {"provider": "fmp", "float_shares": float_shares, "news": news}



_INTERVAL = max(5, int(os.environ.get("PAPER_TRADING_INTERVAL_SECONDS", "5")))
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


def _program_scan_symbols(now):
    """Build a bounded candidate list for reviewed Python scanners from FMP gainers."""
    if _EXPLICIT_PROGRAM_SYMBOLS:
        return list(_SYMBOLS)
    if not _FMP_API_KEY:
        return []
    rows = _fmp_json("biggest-gainers", {}, "program_scan:biggest-gainers", 60)
    if not isinstance(rows, list):
        return []
    candidates = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or len(symbol) > 20 or not symbol.replace(".", "").replace("-", "").isalnum() or symbol in seen:
            continue
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if not (1.0 <= price <= 20.0):
            continue
        seen.add(symbol)
        candidates.append(symbol)
        if len(candidates) >= 25:
            break
    return candidates
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
            last_quote_at TIMESTAMPTZ
        )
    """)
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS side TEXT NOT NULL DEFAULT 'LONG'")
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
                       allocated_usd,stop_price,take_price,pnl_usd,entry_at,exit_at,exit_reason,side
                FROM tradeboot_paper_trades ORDER BY id DESC LIMIT 50
            """).fetchall()
            counts = conn.execute("""
                SELECT COUNT(*) FILTER (WHERE status='OPEN'), COUNT(*) FILTER (WHERE status='CLOSED'),
                       COALESCE(SUM(pnl_usd) FILTER (WHERE status='CLOSED'),0)
                FROM tradeboot_paper_trades
            """).fetchone()
        state = {k: {"value": json.loads(v), "updated_at": t.isoformat()} for k,v,t in rows}
        return {"ok": True, "mode": "paper_only", "worker": state.get("worker", {}).get("value", {"status":"starting"}),
                "open_trades": int(counts[0]), "closed_trades": int(counts[1]), "realized_pnl_usd": float(counts[2]),
                "trades": [{"id":r[0],"strategy_id":r[1],"strategy_name":r[2],"symbol":r[3],"status":r[4],
                  "entry_price":r[5],"exit_price":r[6],"quantity":r[7],"allocated_usd":r[8],"stop_price":r[9],
                  "take_price":r[10],"pnl_usd":r[11],"entry_at":r[12].isoformat(),"exit_at":r[13].isoformat() if r[13] else None,
                  "exit_reason":r[14],"side":r[15]} for r in trades]}
    except Exception:
        return {"ok": False, "mode": "paper_only", "worker": {"status":"unavailable"}, "error":"paper_storage_unavailable"}


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
    if active_programs and not program_symbols:
        skipped.append({"reason": "premarket_candidate_scan_unavailable",
                        "detail": "Configure FMP_API_KEY or PAPER_TRADING_SYMBOLS to supply scanner candidates.",
                        "scanner_context_provider": "fmp" if _FMP_API_KEY else "missing_fmp_api_key"})
    # Formula strategies use their explicit eToro watchlist. Reviewed Python
    # scanners use a separate bounded candidate universe and Schwab history.
    for symbol in formula_symbols:
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
        from schwab_integration import get_price_history
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
        now_minute = started.replace(second=0, microsecond=0)
        for symbol in program_symbols:
            minute_status, minute_payload = get_price_history(symbol, daily=False)
            daily_status, daily_payload = get_price_history(symbol, daily=True)
            if minute_status != 200 or not minute_payload.get("ok"):
                skipped.append({"symbol": symbol, "reason": "schwab_minute_history_unavailable",
                                "status": minute_status, "detail": minute_payload.get("error")})
                continue
            if daily_status != 200 or not daily_payload.get("ok"):
                skipped.append({"symbol": symbol, "reason": "schwab_daily_history_unavailable",
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
                skipped.append({"symbol": symbol, "reason": "no_completed_minute_candles"})
                continue
            # Historical candles initialize indicators only. Do not require a candle
            # from today's session: Schwab may omit premarket/current-session bars.
            # Live entry-pattern ticks are fetched separately below from Schwab quotes.
            seed_rows = [row for row in minute_rows if row["_stamp"] < started]
            if not seed_rows:
                skipped.append({"symbol": symbol, "reason": "no_historical_seed_candles"})
                continue
            # Remove private parsing fields before passing validated OHLCV to the engines.
            seed_candles = [{k:v for k,v in row.items() if not k.startswith("_")} for row in seed_rows[-4000:]]
            # Capture an actual Schwab quote; historical minute closes are NOT ticks.
            from schwab_integration import get_quotes
            quote_status, quote_payload = get_quotes([symbol])
            quote = None
            if quote_status == 200 and quote_payload.get("ok"):
                raw_symbol = quote_payload.get("quotes", {}).get(symbol, {})
                quote = raw_symbol.get("quote", raw_symbol) if isinstance(raw_symbol, dict) else None
            if not isinstance(quote, dict):
                skipped.append({"symbol": symbol, "reason": "schwab_live_quote_unavailable",
                                "status": quote_status, "detail": quote_payload.get("error")})
                continue
            try:
                live_price = float(quote.get("lastPrice") or quote.get("mark") or 0)
                raw_stamp = (quote.get("quoteTimeInLong") or quote.get("tradeTimeInLong")
                             or quote.get("tradeTime") or quote.get("quoteTime"))
                if isinstance(raw_stamp, str) and not raw_stamp.isdigit():
                    live_at = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
                    if live_at.tzinfo is None:
                        live_at = live_at.replace(tzinfo=timezone.utc)
                else:
                    raw_stamp = float(raw_stamp)
                    live_at = datetime.fromtimestamp(raw_stamp / (1000 if raw_stamp > 10_000_000_000 else 1), timezone.utc)
                age = (started - live_at.astimezone(timezone.utc)).total_seconds()
                if live_price <= 0 or not __import__("math").isfinite(live_price) or age < -10 or age > _TICK_MAX_AGE_SECONDS:
                    raise ValueError("quote_stale_or_invalid")
                raw_volume = quote.get("totalVolume", quote.get("volume"))
                live_volume = float(raw_volume) if raw_volume is not None else None
                if live_volume is not None and (not __import__("math").isfinite(live_volume) or live_volume < 0):
                    live_volume = None
                with _connect() as conn:
                    _init_tables(conn)
                    conn.execute("""
                        INSERT INTO tradeboot_market_ticks(symbol,tick_at,price,day_volume,source)
                        VALUES (%s,%s,%s,%s,'schwab_quotes')
                        ON CONFLICT(symbol,tick_at) DO UPDATE SET
                          price=EXCLUDED.price, day_volume=COALESCE(EXCLUDED.day_volume,tradeboot_market_ticks.day_volume)
                    """, (symbol, live_at.astimezone(timezone.utc), live_price, live_volume))
            except (TypeError, ValueError, OverflowError, OSError):
                skipped.append({"symbol": symbol, "reason": "schwab_live_quote_stale_or_invalid"})
                continue

            # Feed only stored real quote timestamps to JeanLong. Minute candles seed indicators only.
            day_start = datetime.combine(started.astimezone(eastern).date(), datetime.min.time(), tzinfo=eastern)
            with _connect() as conn:
                _init_tables(conn)
                tick_rows = conn.execute("""
                    SELECT tick_at,price,day_volume FROM tradeboot_market_ticks
                    WHERE symbol=%s AND tick_at >= %s AND tick_at <= %s
                    ORDER BY tick_at ASC LIMIT 5000
                """, (symbol, day_start.astimezone(timezone.utc), started + timedelta(seconds=10))).fetchall()
            ticks = [{"price": float(row[1]), "timestamp": row[0].astimezone(timezone.utc).isoformat(),
                      "day_volume": float(row[2]) if row[2] is not None else None}
                     for row in tick_rows]
            if not ticks:
                skipped.append({"symbol": symbol, "reason": "no_persisted_live_ticks"})
                continue
            cumulative_volume = next((tick["day_volume"] for tick in reversed(ticks) if tick["day_volume"] is not None), 0.0)
            daily_candles = [{k:v for k,v in row.items() if not k.startswith("_")} for row in daily_rows[-1000:]]
            if len(daily_candles) < 60:
                skipped.append({"symbol": symbol, "reason": "insufficient_daily_history",
                                "daily_candles": len(daily_candles)})
                continue
            daily_volumes = [row["volume"] for row in daily_candles[-30:] if row["volume"] >= 0]
            scanner_context = _fmp_scanner_context(symbol, started)
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
                        skipped.append({"symbol": symbol, "strategy_id": strategy["id"],
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
                                         stop_price,take_price,entry_at,last_quote_at,side)
                                        VALUES (%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s,%s)
                                    """, (strategy["id"],strategy["name"],symbol,price,budget/price,budget,
                                          stop,take,event_time,event_time,side))
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
                        skipped.append({"symbol":symbol,"strategy_id":strategy["id"],
                                        "reason":"strategy_rejected_or_waiting","diagnostics":result["diagnostics"]["last_reject_reasons"]})
                except Exception as exc:
                    skipped.append({"symbol": symbol, "strategy_id": strategy["id"],
                                    "reason": "program_replay_failed", "detail": type(exc).__name__})

    _state("worker", {"status":"running","last_cycle_at":started.isoformat(),"next_cycle_seconds":_INTERVAL,
                      "symbols":symbols,"formula_symbols":formula_symbols,"program_scan_symbols":program_symbols,
                      "program_scan_source":"explicit" if _EXPLICIT_PROGRAM_SYMBOLS else ("fmp_biggest_gainers" if _FMP_API_KEY else "missing_fmp_api_key"),
                      "active_formula_strategies":len(active),
                      "active_program_strategies":len(active_programs),
                      "program_engine_status":"schwab_live_ticks_plus_historical_indicator_seed" if active_programs else "idle",
                      "scanner_context_provider":"fmp" if _FMP_API_KEY else "missing_fmp_api_key",
                      "evaluations":evaluated,"skipped":skipped[:30],
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
