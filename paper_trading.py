"""Persistent simulation-only paper trading loop for Tradeboot.

Uses confirmed eToro snapshots and saved formula strategies. Never places broker orders.
"""
import json
import math
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from strategy_store import _connect, list_strategies, get_general_capital
from strategy_engine import evaluate_formula, StrategyFormulaError
from app import fetch_market_context, fetch_historical_candles, market_values_from_rate_row

_INTERVAL = max(30, int(os.environ.get("PAPER_TRADING_INTERVAL_SECONDS", "60")))
_STOP_PCT = max(0.1, min(20.0, float(os.environ.get("PAPER_TRADING_STOP_PCT", "2"))))
_TAKE_PCT = max(0.1, min(50.0, float(os.environ.get("PAPER_TRADING_TAKE_PCT", "4"))))
_SYMBOLS = tuple(dict.fromkeys(
    s.strip().upper() for s in os.environ.get("PAPER_TRADING_SYMBOLS", "AAPL,MSFT,NVDA").split(",")
    if s.strip() and len(s.strip()) <= 20
))
_lock = threading.Lock()
_thread = None
_stream_thread = None
_program_bots = {}
_history_cache = {}
_instrument_cache = {}
_stream_state = {"status": "starting", "last_tick_at": None, "symbols": list(_SYMBOLS), "tick_count": 0}
_STREAM_TICK_LOCK = threading.Lock()


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
            last_price DOUBLE PRECISION,
            side TEXT NOT NULL DEFAULT 'LONG'
        )
    """)
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS last_price DOUBLE PRECISION")
    conn.execute("ALTER TABLE tradeboot_paper_trades ADD COLUMN IF NOT EXISTS side TEXT NOT NULL DEFAULT 'LONG'")
    conn.execute("""CREATE TABLE IF NOT EXISTS tradeboot_market_ticks (
        id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, instrument_id BIGINT NOT NULL,
        price DOUBLE PRECISION NOT NULL, tick_at TIMESTAMPTZ NOT NULL,
        day_volume DOUBLE PRECISION, received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS tradeboot_market_ticks_symbol_time_idx ON tradeboot_market_ticks(symbol, tick_at DESC)")
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
                       allocated_usd,stop_price,take_price,pnl_usd,entry_at,exit_at,exit_reason,last_price,side
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
                  "exit_reason":r[14],"last_price":r[15],"side":r[16],
                  "unrealized_pnl_usd":(((float(r[15])-float(r[5])) if r[16] != "SHORT" else (float(r[5])-float(r[15])))*float(r[7]) if r[4]=="OPEN" and r[15] is not None else 0.0)} for r in trades]
        with _STREAM_TICK_LOCK:
            stream_status = dict(_stream_state)
        return {"ok": True, "mode": "paper_only", "worker": state.get("worker", {}).get("value", {"status":"starting"}),
                "stream": stream_status,
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
    # Formula strategies use conservative REST quote checks. The reviewed Python
    # engines run on the independent persistent OHLCV + WebSocket tick pipeline below.
    skipped = []
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
                      "program_engine_status":("live_ticks" if _stream_state.get("status")=="live" else "waiting_for_stream") if active_programs else "idle",
                      "evaluations":evaluated,"matches":matches,"skipped":skipped[:30],
                      "trading_mode":"paper_only","real_orders_enabled":False})




def _parse_tick_timestamp(row, received_at):
    for key in ("timestamp", "date", "time", "dateTime", "Date", "Timestamp"):
        value = row.get(key)
        if value:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return received_at


def _tick_from_message(content, symbol, instrument_id):
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (ValueError, TypeError):
            return None
    if not isinstance(content, dict):
        return None
    row = content
    for key in ("rate", "Rate", "instrumentRate", "InstrumentRate", "data", "Data"):
        nested = row.get(key)
        if isinstance(nested, dict):
            row = {**row, **nested}
    values = market_values_from_rate_row(row)
    price = values.get("price")
    if price is None and values.get("bid") and values.get("ask"):
        price = (values["bid"] + values["ask"]) / 2
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return None
    received = datetime.now(timezone.utc)
    volume = None
    for key in ("dayVolume", "DayVolume", "volumeToday", "VolumeToday", "dailyVolume", "DailyVolume"):
        value = row.get(key)
        if value is not None:
            try:
                parsed = float(value)
                if math.isfinite(parsed) and parsed >= 0:
                    volume = parsed
                    break
            except (TypeError, ValueError, OverflowError):
                pass
    return {"symbol": symbol, "instrument_id": instrument_id, "price": float(price),
            "timestamp": _parse_tick_timestamp(row, received), "day_volume": volume}


def _history_for(symbol, instrument_id, force=False):
    now = time.monotonic()
    cached = _history_cache.get(symbol)
    if cached and not force and now - cached.get("minute_fetched_at", 0) < 55 and now - cached.get("daily_fetched_at", 0) < 1800:
        return cached
    minute_rows = cached.get("minute", []) if cached else []
    daily_rows = cached.get("daily", []) if cached else []
    minute_fetched_at = cached.get("minute_fetched_at", 0) if cached else 0
    daily_fetched_at = cached.get("daily_fetched_at", 0) if cached else 0
    if force or now - minute_fetched_at >= 55 or not minute_rows:
        minute_fetched_at = now
        status, payload = fetch_historical_candles(instrument_id, "OneMinute", 220)
        if status == 200 and payload.get("candles"):
            minute_rows = payload["candles"]
    if force or now - daily_fetched_at >= 1800 or not daily_rows:
        daily_fetched_at = now
        status, payload = fetch_historical_candles(instrument_id, "OneDay", 400)
        if status == 200 and payload.get("candles"):
            daily_rows = payload["candles"]
    cached = {"minute": minute_rows, "daily": daily_rows,
              "minute_fetched_at": minute_fetched_at, "daily_fetched_at": daily_fetched_at,
              "instrument_id": instrument_id}
    _history_cache[symbol] = cached
    return cached


def _program_signature(strategy):
    return (strategy.get("name"), strategy.get("formula"), float(strategy.get("allocationPercent", 0) or 0))


def _build_program_bot(strategy, symbol, history):
    from strategy_program_engine import _REGISTRY, is_supported_program
    name = strategy.get("name")
    spec = _REGISTRY.get(name)
    if not spec or not is_supported_program(name, strategy.get("formula", "")):
        return None, "program_not_in_reviewed_allowlist"
    minute_rows = history.get("minute", [])
    daily_rows = history.get("daily", [])
    if len(minute_rows) < 30:
        return None, "insufficient_one_minute_ohlcv"
    if spec[2] == "pullback" and len(daily_rows) < 200:
        return None, "insufficient_daily_ohlcv_for_ema200"
    module, class_name, kind = spec
    capital = get_general_capital()
    budget = max(0.0, capital * float(strategy.get("allocationPercent", 0) or 0) / 100.0)
    if kind == "momentum":
        bot = getattr(module, class_name)(symbol, module.SIDE)
        current_date = datetime.now(timezone.utc).date().isoformat()
        latest_is_today = bool(daily_rows and str(daily_rows[-1].get("timestamp",""))[:10] == current_date)
        completed_daily = daily_rows[:-1] if latest_is_today else daily_rows
        volumes = [float(row["volume"]) for row in completed_daily[-30:] if row.get("volume") is not None]
        avg_volume = sum(volumes) / len(volumes) if volumes else None
        prev_close = completed_daily[-1]["close"] if completed_daily else None
        # Float and news are intentionally not invented. The strategy's own filters will
        # reject entries until those mandatory data sources are configured.
        bot.set_context({"prev_close": prev_close, "avg_volume_30d": avg_volume,
                         "avg_premarket_volume_30d": None, "volume_today": None,
                         "float_shares": None, "news": []})
        bot.seed([{k: row[k] for k in ("open", "high", "low", "close")} for row in minute_rows])
    else:
        import pandas as pd
        daily_frame_rows = [row for row in daily_rows if row.get("timestamp") and row.get("volume") is not None]
        if len(daily_frame_rows) < 200:
            return None, "daily_ohlcv_volume_missing_or_insufficient"
        frame = pd.DataFrame([{k: row[k] for k in ("open", "high", "low", "close", "volume")}
                              for row in daily_frame_rows],
                             index=pd.DatetimeIndex([row["timestamp"] for row in daily_frame_rows]))
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        volumes = [float(row["volume"]) for row in daily_frame_rows[-31:-1]]
        info = {"symbol": symbol, "avg_volume_30d": sum(volumes) / len(volumes) if volumes else None,
                "float_shares": None}
        contexts = [module.daily_context(frame, info, "D"),
                    module.daily_context(module.to_weekly(frame), info, "W")]
        sizer = module.PositionSizer(capital=budget, daily_pct=100)
        bot = getattr(module, class_name)(symbol, sizer)
        bot.seed([{k: row[k] for k in ("open", "high", "low", "close")} for row in minute_rows])
        bot.set_context(*contexts)
    return {"bot": bot, "module": module, "kind": kind, "signature": _program_signature(strategy),
            "strategy": strategy, "symbol": symbol}, None


def _record_program_action(strategy, symbol, action, price, tick_at):
    kind = str(action.get("action", "")).upper()
    side = str(action.get("side", "LONG")).upper()
    if side not in ("LONG", "SHORT") or kind not in ("ENTER", "EXIT", "MOVE_STOP"):
        return
    capital = get_general_capital()
    allocation = max(0.0, capital * float(strategy.get("allocationPercent", 0) or 0) / 100.0)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("UPDATE tradeboot_paper_trades SET last_price=%s,last_quote_at=%s WHERE strategy_id=%s AND symbol=%s AND status='OPEN'",
                         (price, tick_at, strategy["id"], symbol))
            opened = conn.execute("""SELECT id,entry_price,quantity,stop_price,side FROM tradeboot_paper_trades
                WHERE strategy_id=%s AND symbol=%s AND status='OPEN' FOR UPDATE""",
                (strategy["id"], symbol)).fetchone()
            if kind == "ENTER":
                if opened:
                    return
                used = conn.execute("SELECT COALESCE(SUM(allocated_usd),0) FROM tradeboot_paper_trades WHERE strategy_id=%s AND status='OPEN'",
                                    (strategy["id"],)).fetchone()[0]
                remaining = max(0.0, allocation - float(used or 0))
                if remaining <= 0 or price <= 0:
                    return
                requested = action.get("shares")
                if requested is not None:
                    try:
                        quantity = min(max(0.0, float(requested)), math.floor(remaining / price))
                    except (TypeError, ValueError, OverflowError):
                        quantity = 0
                else:
                    quantity = remaining / price
                if quantity <= 0:
                    return
                stop = action.get("stop")
                try:
                    stop = float(stop) if stop is not None else (price * (1-_STOP_PCT/100) if side == "LONG" else price * (1+_STOP_PCT/100))
                except (TypeError, ValueError):
                    stop = price * (1-_STOP_PCT/100) if side == "LONG" else price * (1+_STOP_PCT/100)
                take = price * (1+_TAKE_PCT/100) if side == "LONG" else price * (1-_TAKE_PCT/100)
                conn.execute("""INSERT INTO tradeboot_paper_trades
                    (strategy_id,strategy_name,symbol,status,entry_price,quantity,allocated_usd,stop_price,take_price,entry_at,last_quote_at,last_price,side)
                    VALUES (%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (strategy["id"],strategy["name"],symbol,price,quantity,quantity*price,stop,take,tick_at,tick_at,price,side))
            elif kind == "MOVE_STOP" and opened:
                new_stop = action.get("stop")
                if new_stop is not None:
                    try:
                        new_stop = float(new_stop)
                        current_stop = float(opened[3])
                        improves = new_stop > current_stop if side == "LONG" else new_stop < current_stop
                        if improves:
                            conn.execute("UPDATE tradeboot_paper_trades SET stop_price=%s WHERE id=%s", (new_stop, opened[0]))
                    except (TypeError, ValueError, OverflowError):
                        pass
            elif kind == "EXIT" and opened:
                trade_id, entry, qty, _stop, trade_side = opened
                pnl = (price - float(entry)) * float(qty) if trade_side != "SHORT" else (float(entry) - price) * float(qty)
                conn.execute("""UPDATE tradeboot_paper_trades SET status='CLOSED',exit_price=%s,pnl_usd=%s,
                    exit_at=%s,exit_reason=%s,last_quote_at=%s,last_price=%s WHERE id=%s""",
                    (price,pnl,tick_at,str(action.get("reason","strategy_exit"))[:120],tick_at,price,trade_id))


def _process_stream_tick(tick, active_strategies):
    symbol = tick["symbol"]
    instrument_id = tick["instrument_id"]
    price = tick["price"]
    tick_at = tick["timestamp"]
    day_volume = tick.get("day_volume")
    history = _history_cache.get(symbol) or {}
    daily_rows = history.get("daily", [])
    if day_volume is None and daily_rows:
        latest_daily = daily_rows[-1]
        if str(latest_daily.get("timestamp",""))[:10] == tick_at.date().isoformat() and latest_daily.get("volume") is not None:
            try:
                day_volume = float(latest_daily["volume"])
            except (TypeError, ValueError, OverflowError):
                day_volume = None
    with _connect() as conn:
        tick_id = conn.execute("""INSERT INTO tradeboot_market_ticks(symbol,instrument_id,price,tick_at,day_volume)
                        VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                     (symbol,instrument_id,price,tick_at,day_volume)).fetchone()[0]
        conn.execute("""DELETE FROM tradeboot_market_ticks WHERE symbol=%s AND id NOT IN
                        (SELECT id FROM tradeboot_market_ticks WHERE symbol=%s ORDER BY tick_at DESC,id DESC LIMIT 5000)""",
                     (symbol,symbol))
        conn.execute("""UPDATE tradeboot_paper_trades SET last_price=%s,last_quote_at=%s
                        WHERE symbol=%s AND status='OPEN'""", (price,tick_at,symbol))
    desired = {str(s["id"]): s for s in active_strategies
               if s.get("enabled") and s.get("strategyType") == "python_program"}
    for key in list(_program_bots):
        if key[1] == symbol and (key[0] not in desired or _program_bots[key].get("signature") != _program_signature(desired[key[0]])):
            _program_bots.pop(key, None)
    for strategy_id, strategy in desired.items():
        key = (strategy_id, symbol)
        entry = _program_bots.get(key)
        if entry is None:
            history = _history_for(symbol, instrument_id)
            built, error = _build_program_bot(strategy, symbol, history)
            if error:
                with _STREAM_TICK_LOCK:
                    _stream_state["last_error"] = strategy.get("name","strategy")+": "+error
                    _stream_state["strategy_blockers"] = {**_stream_state.get("strategy_blockers",{}), strategy_id:error}
                continue
            entry = built
            with _STREAM_TICK_LOCK:
                _stream_state.setdefault("strategy_blockers", {}).pop(strategy_id, None)
            # Rebuild state from recent real stream ticks after a process restart; don't
            # record these replayed actions a second time.
            with _connect() as conn:
                replay = conn.execute("""SELECT price,tick_at,day_volume FROM tradeboot_market_ticks
                    WHERE symbol=%s AND tick_at >= %s AND id < %s ORDER BY tick_at ASC,id ASC""",
                    (symbol, datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0), tick_id)).fetchall()
            for old_price, old_at, old_volume in replay:
                try:
                    entry["bot"].on_tick(float(old_price), old_at, old_volume) if entry["kind"] == "momentum" else entry["bot"].on_tick(float(old_price), old_at)
                except Exception:
                    pass
            _program_bots[key] = entry
        try:
            actions = entry["bot"].on_tick(price, tick_at, day_volume) if entry["kind"] == "momentum" else entry["bot"].on_tick(price, tick_at)
            rejects = getattr(entry["bot"], "last_reject", None)
            if rejects and not actions:
                with _STREAM_TICK_LOCK:
                    _stream_state.setdefault("strategy_blockers", {})[strategy_id] = "; ".join(str(x) for x in rejects)[:500]
            elif actions:
                with _STREAM_TICK_LOCK:
                    _stream_state.setdefault("strategy_blockers", {}).pop(strategy_id, None)
            for action in actions:
                _record_program_action(strategy, symbol, action, price, tick_at)
                with _STREAM_TICK_LOCK:
                    recent = _stream_state.setdefault("recent_actions", [])
                    recent.append({"strategy_id": strategy_id, "strategy_name": strategy["name"], "symbol": symbol,
                                   "action": action.get("action"), "side": action.get("side"), "price": price,
                                   "reason": str(action.get("reason", ""))[:160], "at": tick_at.isoformat()})
                    del recent[:-30]
        except Exception as exc:
            with _STREAM_TICK_LOCK:
                _stream_state["last_error"] = "strategy_tick_error:"+type(exc).__name__


def _stream_loop():
    global _program_bots
    print("Tradeboot eToro live tick stream starting (simulation only)", flush=True)
    while True:
        try:
            api_key, user_key = os.environ.get("ETORO_API_KEY"), os.environ.get("ETORO_USER_KEY")
            if not api_key or not user_key:
                with _STREAM_TICK_LOCK:
                    _stream_state.update({"status":"missing_credentials","last_error":"ETORO_API_KEY/ETORO_USER_KEY not configured"})
                _state("market_stream", dict(_stream_state))
                time.sleep(20)
                continue
            resolved = []
            for symbol in _SYMBOLS:
                status, market = fetch_market_context(symbol)
                instrument_id = market.get("instrument_id")
                if instrument_id:
                    resolved.append({"symbol":symbol,"instrument_id":int(instrument_id)})
                    _history_for(symbol, int(instrument_id))
            if not resolved:
                raise RuntimeError("instrument_resolution_failed")
            import websocket
            stream = websocket.create_connection("wss://ws.etoro.com/ws", timeout=5)
            try:
                auth_id = str(uuid.uuid4())
                stream.send(json.dumps({"id":auth_id,"operation":"Authenticate",
                                        "data":{"userKey":user_key,"apiKey":api_key}}))
                authenticated = False
                deadline = time.monotonic()+8
                while time.monotonic() < deadline:
                    raw = stream.recv()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(msg,dict) and msg.get("operation")=="Authenticate" and msg.get("id")==auth_id:
                        if not msg.get("success"):
                            raise RuntimeError("websocket_authentication_failed")
                        authenticated=True
                        break
                if not authenticated:
                    raise RuntimeError("websocket_authentication_timeout")
                stream.send(json.dumps({"id":str(uuid.uuid4()),"operation":"Subscribe",
                    "data":{"topics":["instrument:"+str(x["instrument_id"]) for x in resolved],"snapshot":False}}))
                stream.settimeout(1)
                with _STREAM_TICK_LOCK:
                    _stream_state.update({"status":"live","last_error":None,"symbols":[x["symbol"] for x in resolved],
                                          "instrument_ids":{x["symbol"]:x["instrument_id"] for x in resolved},
                                          "history_counts":{x["symbol"]:{"one_minute":len((_history_cache.get(x["symbol"]) or {}).get("minute",[])),
                                                                       "daily":len((_history_cache.get(x["symbol"]) or {}).get("daily",[]))}
                                                           for x in resolved},
                                          "connected_at":datetime.now(timezone.utc).isoformat()})
                _state("market_stream", dict(_stream_state))
                active_strategies = list_strategies()
                refresh_at = time.monotonic()+5
                last_status_save = time.monotonic()
                by_id = {x["instrument_id"]:x["symbol"] for x in resolved}
                while True:
                    if time.monotonic() >= refresh_at:
                        active_strategies = list_strategies()
                        refresh_at = time.monotonic()+5
                    try:
                        raw = stream.recv()
                    except websocket.WebSocketTimeoutException:
                        raw = None
                    if raw:
                        try:
                            incoming = json.loads(raw)
                        except (ValueError, TypeError):
                            incoming = None
                        messages = incoming.get("messages",[]) if isinstance(incoming,dict) else []
                        if isinstance(incoming,dict) and incoming.get("topic"):
                            messages = [incoming]
                        for message in messages:
                            if not isinstance(message,dict):
                                continue
                            topic = str(message.get("topic",""))
                            try:
                                instrument_id = int(topic.rsplit(":",1)[1])
                            except (IndexError,ValueError):
                                continue
                            symbol = by_id.get(instrument_id)
                            if not symbol:
                                continue
                            tick = _tick_from_message(message.get("content"),symbol,instrument_id)
                            if not tick:
                                continue
                            _process_stream_tick(tick,active_strategies)
                            with _STREAM_TICK_LOCK:
                                _stream_state["last_tick_at"] = tick["timestamp"].isoformat()
                                _stream_state["tick_count"] = int(_stream_state.get("tick_count",0))+1
                    if time.monotonic()-last_status_save >= 10:
                        with _STREAM_TICK_LOCK:
                            status_copy = dict(_stream_state)
                        _state("market_stream",status_copy)
                        last_status_save = time.monotonic()
            finally:
                stream.close()
        except Exception as exc:
            with _STREAM_TICK_LOCK:
                _stream_state.update({"status":"reconnecting","last_error":type(exc).__name__})
            try:
                _state("market_stream",dict(_stream_state))
            except Exception:
                pass
            print("Tradeboot tick stream reconnecting: "+type(exc).__name__,flush=True)
            time.sleep(5)


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
    global _thread, _stream_thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="tradeboot-paper-trader", daemon=True)
            _thread.start()
        if _stream_thread is None or not _stream_thread.is_alive():
            _stream_thread = threading.Thread(target=_stream_loop, name="tradeboot-market-stream", daemon=True)
            _stream_thread.start()
