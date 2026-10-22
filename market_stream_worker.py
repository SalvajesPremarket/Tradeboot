"""Long-running, read-only eToro tick consumer for a Render Background Worker.

Required env: ETORO_API_KEY, ETORO_USER_KEY, DATABASE_URL,
TRADEBOOT_STREAM_SYMBOLS (comma-separated, 1-5 symbols).
No order/trading endpoints are called by this worker.
"""
import json
import os
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import websocket

from etoro_stream_adapter import parse_instrument_ticks
from strategy_market_runtime import PersistentOneMinuteRuntime

API_BASE = "https://public-api.etoro.com/api/v1/"
MAX_SYMBOLS = 5


def _get_json(path, api_key, user_key):
    request = Request(API_BASE + path, headers={
        "x-api-key": api_key,
        "x-user-key": user_key,
        "x-request-id": str(uuid.uuid4()),
        "Accept": "application/json",
    })
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
    except HTTPError as exc:
        return exc.code, None
    except (URLError, TimeoutError, OSError, ValueError):
        return 0, None


def _find_instrument_id(payload, symbol):
    records = []

    def visit(value):
        if isinstance(value, dict):
            records.append(value)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(payload)
    for row in records:
        row_symbol = str(row.get("internalSymbolFull") or row.get("InternalSymbolFull") or row.get("symbol") or row.get("Symbol") or "").strip().upper()
        candidate = row.get("instrumentId", row.get("InstrumentId", row.get("instrumentID")))
        if row_symbol == symbol and candidate is not None:
            try:
                instrument_id = int(candidate)
            except (TypeError, ValueError, OverflowError):
                continue
            if instrument_id > 0:
                return instrument_id
    return None


def resolve_symbols(symbols, api_key, user_key):
    """Resolve exact ticker matches; never subscribe to a fuzzy match."""
    resolved = {}
    for symbol in symbols:
        status, payload = _get_json("market-data/search?" + urlencode({"internalSymbolFull": symbol}), api_key, user_key)
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError("instrument_search_failed")
        instrument_id = _find_instrument_id(payload.get("data", payload), symbol)
        if instrument_id is None:
            raise RuntimeError("instrument_not_found")
        if instrument_id in resolved:
            raise RuntimeError("duplicate_instrument_id")
        resolved[instrument_id] = symbol
    return resolved


def _configured_symbols():
    raw = os.environ.get("TRADEBOOT_STREAM_SYMBOLS", "").strip()
    symbols = [part.strip().upper() for part in raw.split(",") if part.strip()]
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_")
    if not 1 <= len(symbols) <= MAX_SYMBOLS or len(set(symbols)) != len(symbols):
        raise RuntimeError("configure_one_to_five_unique_symbols")
    if any(len(symbol) > 20 or any(char not in allowed for char in symbol) for symbol in symbols):
        raise RuntimeError("invalid_stream_symbol")
    return symbols


def run_forever():
    api_key = os.environ.get("ETORO_API_KEY", "").strip()
    user_key = os.environ.get("ETORO_USER_KEY", "").strip()
    if not api_key or not user_key:
        raise RuntimeError("etoro_credentials_not_configured")
    if not os.environ.get("DATABASE_URL", "").strip():
        raise RuntimeError("strategy_database_not_configured")

    symbols = _configured_symbols()
    instrument_to_symbol = resolve_symbols(symbols, api_key, user_key)
    runtimes = {symbol: PersistentOneMinuteRuntime(symbol) for symbol in instrument_to_symbol.values()}
    topics = ["instrument:" + str(instrument_id) for instrument_id in instrument_to_symbol]
    delay = 1

    while True:
        stream = None
        try:
            stream = websocket.create_connection("wss://ws.etoro.com/ws", timeout=10)
            auth_id = str(uuid.uuid4())
            stream.send(json.dumps({
                "id": auth_id,
                "operation": "Authenticate",
                "data": {"userKey": user_key, "apiKey": api_key},
            }))
            authenticated = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                incoming = stream.recv()
                if not isinstance(incoming, str):
                    continue
                try:
                    response = json.loads(incoming)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(response, dict) and response.get("operation") == "Authenticate" and response.get("id") == auth_id:
                    if not response.get("success"):
                        raise RuntimeError("websocket_authentication_failed")
                    authenticated = True
                    break
            if not authenticated:
                raise RuntimeError("websocket_authentication_timeout")

            stream.send(json.dumps({
                "id": str(uuid.uuid4()),
                "operation": "Subscribe",
                "data": {"topics": topics, "snapshot": False},
            }))
            delay = 1
            print("eToro stream connected; subscribed symbols=" + ",".join(symbols), flush=True)

            while True:
                try:
                    raw = stream.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not isinstance(raw, str):
                    # Ignore binary heartbeat/control frames.
                    continue
                for tick in parse_instrument_ticks(raw, instrument_to_symbol):
                    try:
                        result = runtimes[tick["symbol"]].on_tick(tick["price"], tick["timestamp"])
                        if result is not None:
                            print(
                                "closed_candle symbol=" + result["symbol"]
                                + " timestamp=" + result["timestamp"]
                                + " persisted=" + str(bool(result["persistence"].get("stored"))),
                                flush=True,
                            )
                    except ValueError:
                        # Invalid/out-of-order provider data must never create a candle.
                        print("tick_rejected symbol=" + tick["symbol"] + " reason=invalid_tick", flush=True)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            # Error class only; never log credentials, provider payloads, or request headers.
            print("eToro stream reconnecting error=" + type(exc).__name__, flush=True)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        time.sleep(delay)
        delay = min(delay * 2, 30)


if __name__ == "__main__":
    run_forever()
