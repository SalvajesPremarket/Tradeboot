"""Read-only HTTP interface for the isolated Tradeboot scaffold.

eToro requests are restricted to GET endpoints and use credentials from environment
variables. No order placement or account-changing requests are implemented.
"""
import json
import os
import uuid
import hmac
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlencode, urlsplit

from tradeboot import Tradeboot
from strategy_engine import StrategyFormulaError, evaluate_formula, validate_formula
from strategy_store import StrategyStoreUnavailable, list_strategies, replace_strategies, get_general_capital, set_general_capital


module = Tradeboot()
ETORO_BASE_URL = "https://public-api.etoro.com/api/v1/"
ETORO_TIMEOUT_SECONDS = 15
DEFAULT_MAX_QUOTE_AGE_SECONDS = 300


def _parse_provider_bool(value):
    """Parse provider booleans without treating strings like 'False' as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "open"):
            return True
        if normalized in ("false", "0", "no", "closed"):
            return False
    return None


def quote_freshness(rates_payload):
    """Annotate snapshot age and provider quality signals; not a streaming guarantee."""
    try:
        max_age = int(os.environ.get("ETORO_MAX_QUOTE_AGE_SECONDS", str(DEFAULT_MAX_QUOTE_AGE_SECONDS)))
        if max_age < 1:
            max_age = DEFAULT_MAX_QUOTE_AGE_SECONDS
    except (TypeError, ValueError):
        max_age = DEFAULT_MAX_QUOTE_AGE_SECONDS

    rate_rows = rates_payload.get("rates") if isinstance(rates_payload, dict) else None
    if isinstance(rate_rows, dict):
        rate_rows = rate_rows.get("rates")
    if not isinstance(rate_rows, list) or not rate_rows or not isinstance(rate_rows[0], dict):
        return {
            "quote_time": None,
            "quote_age_seconds": None,
            "quote_is_fresh": False,
            "freshness_status": "unknown_timestamp",
            "market_open": None,
            "delayed_quote": None,
            "quote_is_usable": False,
            "quote_quality_status": "unknown_timestamp",
            "max_quote_age_seconds": max_age,
        }

    row = rate_rows[0]
    quote_time = row.get("date") or row.get("Date")
    if quote_time is None or not str(quote_time).strip():
        return {
            "quote_time": None,
            "quote_age_seconds": None,
            "quote_is_fresh": False,
            "freshness_status": "unknown_timestamp",
            "market_open": None,
            "delayed_quote": None,
            "quote_is_usable": False,
            "quote_quality_status": "unknown_timestamp",
            "max_quote_age_seconds": max_age,
        }
    try:
        parsed_time = datetime.fromisoformat(str(quote_time).replace("Z", "+00:00"))
        if parsed_time.tzinfo is None:
            parsed_time = parsed_time.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - parsed_time.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return {
            "quote_time": quote_time,
            "quote_age_seconds": None,
            "quote_is_fresh": False,
            "freshness_status": "invalid_timestamp",
            "market_open": None,
            "delayed_quote": None,
            "quote_is_usable": False,
            "quote_quality_status": "invalid_timestamp",
            "max_quote_age_seconds": max_age,
        }

    fresh = age <= max_age
    market_open_raw = row.get("IsMarketOpen", row.get("isMarketOpen", row.get("marketOpen")))
    if market_open_raw is None:
        market_open_raw = row.get("IsExchangeOpen", row.get("isExchangeOpen", row.get("exchangeOpen")))
    market_open = _parse_provider_bool(market_open_raw)

    delayed_values = []
    for key in ("DelayedAsk", "DelayedBid", "delayedAsk", "delayedBid", "isDelayed", "IsDelayed"):
        if key in row:
            parsed = _parse_provider_bool(row.get(key))
            if parsed is not None:
                delayed_values.append(parsed)
    delayed_quote = True if any(delayed_values) else (False if delayed_values else None)

    if not fresh:
        quality_status = "stale"
    elif market_open is False:
        quality_status = "market_closed"
    elif delayed_quote is True:
        quality_status = "delayed_quote"
    elif market_open is True and delayed_quote is False:
        quality_status = "fresh_market_open"
    else:
        quality_status = "fresh_quality_unconfirmed"

    # Never label explicitly stale, closed-market, or delayed quotes as usable.
    # Missing provider metadata remains visible as unconfirmed rather than fabricated.
    usable = fresh and market_open is True and delayed_quote is False
    return {
        "quote_time": quote_time,
        "quote_age_seconds": age,
        "quote_is_fresh": fresh,
        "freshness_status": "fresh" if fresh else "stale",
        "market_open": market_open,
        "delayed_quote": delayed_quote,
        "quote_is_usable": usable,
        "quote_quality_status": quality_status,
        "max_quote_age_seconds": max_age,
    }

def etoro_get(path):
    api_key = os.environ.get("ETORO_API_KEY")
    user_key = os.environ.get("ETORO_USER_KEY")
    if not api_key or not user_key:
        return 503, {
            "ok": False,
            "error": "etoro_credentials_not_configured",
            "required_env": ["ETORO_API_KEY", "ETORO_USER_KEY"],
        }

    # Endpoint paths are fixed by this module; query values are validated and encoded.
    request = Request(
        ETORO_BASE_URL + path,
        headers={
            "x-api-key": api_key,
            "x-user-key": user_key,
            "x-request-id": str(uuid.uuid4()),
            "Accept": "application/json",
            "User-Agent": "Tradeboot/1.0 (read-only eToro API client)",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=ETORO_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, {"ok": True, "data": payload}
    except HTTPError as exc:
        # Expose only a small, sanitized diagnostic; never forward the full upstream body.
        diagnostic = {}
        try:
            raw = exc.read(4096).decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for field in ("code", "error", "message", "detail"):
                    value = parsed.get(field)
                    if isinstance(value, (str, int, float)):
                        safe_value = str(value)[:240]
                        # Avoid returning anything that resembles a long credential/token.
                        import re
                        safe_value = re.sub(r"(?i)(key|token|secret|authorization)\s*[:=]\s*[^,;\s]+", r"\1=[REDACTED]", safe_value)
                        safe_value = re.sub(r"\b[A-Za-z0-9_-]{40,}\b", "[REDACTED]", safe_value)
                        diagnostic[field] = safe_value
        except (ValueError, UnicodeError):
            pass
        return 502, {
            "ok": False,
            "error": "etoro_api_error",
            "upstream_status": exc.code,
            "upstream_diagnostic": diagnostic,
        }
    except (URLError, TimeoutError, OSError, ValueError):
        return 502, {"ok": False, "error": "etoro_api_unavailable"}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload, head_only=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _dispatch(self, head_only=False):
        # Ignore query parameters such as tracking tags added by browsers or link previews.
        path = urlsplit(self.path).path
        if path == "/health":
            return self._json(200, {"ok": True, "service": "Tradeboot"}, head_only)
        if path == "/":
            try:
                dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
                with open(dashboard_path, "rb") as dashboard_file:
                    body = dashboard_file.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head_only:
                    self.wfile.write(body)
                return
            except OSError:
                return self._json(500, {
                    "ok": False,
                    "error": "dashboard_file_unavailable",
                    "trading_enabled": False,
                }, head_only)
        if path in ("/status", "/watchlist"):
            return self._json(200, module.snapshot(), head_only)
        if path == "/etoro/instrument-rates":
            query = parse_qs(urlsplit(self.path).query)
            symbols_raw = query.get("symbols", [""])[0]
            if symbols_raw:
                symbols = [item.strip().upper() for item in symbols_raw.split(",") if item.strip()]
            else:
                symbols = [query.get("symbol", ["AAPL"])[0].strip().upper()]
            allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
            if not symbols or len(symbols) > 5 or any(not symbol or len(symbol) > 20 or any(char not in allowed for char in symbol) for symbol in symbols):
                return self._json(400, {
                    "ok": False,
                    "error": "invalid_symbols",
                    "hint": "Use up to five ticker symbols containing only letters, digits, dot, dash, or underscore.",
                }, head_only)

            def find_instrument_id(symbol):
                search_status, search_result = etoro_get(
                    "market-data/search?" + urlencode({"internalSymbolFull": symbol})
                )
                if search_status != 200 or not search_result.get("ok"):
                    return None, {"ok": False, "symbol": symbol, "stage": "search", "error": search_result.get("error", "search_failed"), "upstream_status": search_status}

                records = []
                def collect_records(value):
                    if isinstance(value, dict):
                        records.append(value)
                        for nested in value.values():
                            if isinstance(nested, (dict, list)):
                                collect_records(nested)
                    elif isinstance(value, list):
                        for nested in value:
                            if isinstance(nested, (dict, list)):
                                collect_records(nested)
                collect_records(search_result.get("data"))

                for record in records:
                    record_symbol = str(record.get("internalSymbolFull") or record.get("InternalSymbolFull") or record.get("symbol") or record.get("Symbol") or "").strip().upper()
                    candidate_id = record.get("instrumentId", record.get("InstrumentId", record.get("instrumentID")))
                    if record_symbol == symbol and candidate_id is not None:
                        try:
                            return int(candidate_id), None
                        except (TypeError, ValueError):
                            continue
                return None, {"ok": False, "symbol": symbol, "error": "instrument_not_found"}

            resolved = []
            results_by_symbol = {}
            for symbol in symbols:
                instrument_id, error = find_instrument_id(symbol)
                if error:
                    results_by_symbol[symbol] = error
                else:
                    resolved.append({"symbol": symbol, "instrument_id": instrument_id})

            if resolved:
                # eToro supports fetching multiple instrument IDs in one rates snapshot.
                ids = ",".join(str(item["instrument_id"]) for item in resolved)
                rates_status, rates_result = etoro_get(
                    "market-data/instruments/rates?" + urlencode({"instrumentIds": ids})
                )
                if rates_status != 200 or not rates_result.get("ok"):
                    for item in resolved:
                        results_by_symbol[item["symbol"]] = {
                            "ok": False, "symbol": item["symbol"], "instrument_id": item["instrument_id"],
                            "stage": "rates_batch", "error": rates_result.get("error", "rates_failed"),
                            "upstream_status": rates_status,
                        }
                else:
                    rates_payload = rates_result.get("data")
                    rate_rows = rates_payload.get("rates") if isinstance(rates_payload, dict) else None
                    if not isinstance(rate_rows, list):
                        rate_rows = []
                    rates_by_id = {}
                    for row in rate_rows:
                        if isinstance(row, dict):
                            raw_id = row.get("instrumentID", row.get("instrumentId", row.get("InstrumentId")))
                            try:
                                rates_by_id[int(raw_id)] = row
                            except (TypeError, ValueError):
                                continue

                    for item in resolved:
                        row = rates_by_id.get(item["instrument_id"])
                        if row is None:
                            results_by_symbol[item["symbol"]] = {
                                "ok": False, "symbol": item["symbol"], "instrument_id": item["instrument_id"],
                                "stage": "rates_batch", "error": "rate_not_returned_for_instrument",
                            }
                            continue
                        single_rate_payload = {"rates": [row]}
                        freshness = quote_freshness(single_rate_payload)
                        results_by_symbol[item["symbol"]] = {
                            "ok": True, "symbol": item["symbol"], "instrument_id": item["instrument_id"],
                            "rates": single_rate_payload, **freshness,
                        }

            results = [results_by_symbol[symbol] for symbol in symbols]
            if len(results) == 1:
                result = results[0]
                return self._json(200 if result.get("ok") else 502, result, head_only)
            all_ok = all(result.get("ok") for result in results)
            all_fresh = all(result.get("ok") and result.get("quote_is_fresh") for result in results)
            all_usable = all(result.get("ok") and result.get("quote_is_usable") for result in results)
            return self._json(200 if all_ok else 207, {
                "ok": all_ok,
                "all_quotes_fresh": all_fresh,
                "all_quotes_usable": all_usable,
                "max_quote_age_seconds": results[0].get("max_quote_age_seconds", DEFAULT_MAX_QUOTE_AGE_SECONDS),
                "results": results,
            }, head_only)
        if path == "/etoro/instrument-stream":
            # Read-only diagnostic using eToro's documented WebSocket snapshot topics.
            query = parse_qs(urlsplit(self.path).query)
            symbols_raw = query.get("symbols", [""])[0]
            symbols = [item.strip().upper() for item in symbols_raw.split(",") if item.strip()]
            allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
            if not symbols or len(symbols) > 5 or any(not symbol or len(symbol) > 20 or any(char not in allowed for char in symbol) for symbol in symbols):
                return self._json(400, {
                    "ok": False,
                    "error": "invalid_symbols",
                    "hint": "Use up to five ticker symbols containing only letters, digits, dot, dash, or underscore.",
                }, head_only)

            api_key = os.environ.get("ETORO_API_KEY")
            user_key = os.environ.get("ETORO_USER_KEY")
            if not api_key or not user_key:
                return self._json(503, {"ok": False, "error": "etoro_credentials_not_configured"}, head_only)

            resolved = []
            errors = {}
            for symbol in symbols:
                search_status, search_result = etoro_get(
                    "market-data/search?" + urlencode({"internalSymbolFull": symbol})
                )
                if search_status != 200 or not search_result.get("ok"):
                    errors[symbol] = {"ok": False, "symbol": symbol, "stage": "search", "error": "instrument_search_failed", "upstream_status": search_status}
                    continue
                records = []
                def collect_stream_records(value):
                    if isinstance(value, dict):
                        records.append(value)
                        for nested in value.values():
                            if isinstance(nested, (dict, list)):
                                collect_stream_records(nested)
                    elif isinstance(value, list):
                        for nested in value:
                            if isinstance(nested, (dict, list)):
                                collect_stream_records(nested)
                collect_stream_records(search_result.get("data"))
                found_id = None
                for record in records:
                    record_symbol = str(record.get("internalSymbolFull") or record.get("InternalSymbolFull") or record.get("symbol") or record.get("Symbol") or "").strip().upper()
                    candidate_id = record.get("instrumentId", record.get("InstrumentId", record.get("instrumentID")))
                    if record_symbol == symbol and candidate_id is not None:
                        try:
                            found_id = int(candidate_id)
                            break
                        except (TypeError, ValueError):
                            pass
                if found_id is None:
                    errors[symbol] = {"ok": False, "symbol": symbol, "stage": "search", "error": "instrument_not_found"}
                else:
                    resolved.append({"symbol": symbol, "instrument_id": found_id})

            if resolved:
                try:
                    import time
                    import websocket
                    stream = websocket.create_connection("wss://ws.etoro.com/ws", timeout=5)
                    try:
                        auth_id = str(uuid.uuid4())
                        stream.send(json.dumps({
                            "id": auth_id,
                            "operation": "Authenticate",
                            "data": {"userKey": user_key, "apiKey": api_key},
                        }))
                        auth_deadline = time.monotonic() + 5
                        authenticated = False
                        while time.monotonic() < auth_deadline:
                            raw_message = stream.recv()
                            if not raw_message:
                                continue
                            try:
                                incoming = json.loads(raw_message)
                            except (json.JSONDecodeError, TypeError):
                                # Ignore empty/non-JSON control frames and keep waiting for the acknowledgement.
                                continue
                            if not isinstance(incoming, dict):
                                continue
                            if incoming.get("operation") == "Authenticate" and incoming.get("id") == auth_id:
                                if not incoming.get("success"):
                                    raise RuntimeError("websocket_authentication_failed")
                                authenticated = True
                                break
                        if not authenticated:
                            raise RuntimeError("websocket_authentication_timeout")

                        stream.send(json.dumps({
                            "id": str(uuid.uuid4()),
                            "operation": "Subscribe",
                            "data": {
                                "topics": ["instrument:" + str(item["instrument_id"]) for item in resolved],
                                "snapshot": True,
                            },
                        }))
                        rows_by_id = {}
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline and len(rows_by_id) < len(resolved):
                            try:
                                raw_message = stream.recv()
                            except websocket.WebSocketTimeoutException:
                                break
                            if not raw_message:
                                continue
                            try:
                                incoming = json.loads(raw_message)
                            except (json.JSONDecodeError, TypeError):
                                # Ignore non-JSON frames; the snapshot may arrive in a later message.
                                continue
                            messages = incoming.get("messages", []) if isinstance(incoming, dict) else []
                            if isinstance(incoming, dict) and incoming.get("topic"):
                                messages = [incoming]
                            for message in messages:
                                if not isinstance(message, dict):
                                    continue
                                topic = str(message.get("topic", ""))
                                try:
                                    instrument_id = int(topic.rsplit(":", 1)[1])
                                except (IndexError, ValueError):
                                    continue
                                content = message.get("content")
                                if isinstance(content, str):
                                    try:
                                        content = json.loads(content)
                                    except ValueError:
                                        continue
                                if isinstance(content, dict):
                                    rows_by_id[instrument_id] = content
                        for item in resolved:
                            row = rows_by_id.get(item["instrument_id"])
                            if row is None:
                                errors[item["symbol"]] = {
                                    "ok": False, "symbol": item["symbol"], "instrument_id": item["instrument_id"],
                                    "stage": "websocket_snapshot", "error": "stream_snapshot_not_returned",
                                }
                                continue
                            freshness = quote_freshness({"rates": [row]})
                            errors[item["symbol"]] = {
                                "ok": True, "symbol": item["symbol"], "instrument_id": item["instrument_id"],
                                "source": "etoro_websocket_snapshot", "rates": {"rates": [row]}, **freshness,
                            }
                    finally:
                        stream.close()
                except Exception as exc:
                    # Keep secrets and provider payloads out of the response.
                    return self._json(502, {
                        "ok": False,
                        "error": "etoro_websocket_diagnostic_failed",
                        "diagnostic": type(exc).__name__,
                        "hint": "The WebSocket snapshot could not be read; retry during market hours.",
                    }, head_only)

            results = [errors.get(symbol, {"ok": False, "symbol": symbol, "error": "no_result"}) for symbol in symbols]
            all_ok = all(result.get("ok") for result in results)
            all_fresh = all(result.get("ok") and result.get("quote_is_fresh") for result in results)
            all_usable = all(result.get("ok") and result.get("quote_is_usable") for result in results)
            return self._json(200 if all_ok else 207, {
                "ok": all_ok,
                "source": "etoro_websocket_snapshot",
                "all_quotes_fresh": all_fresh,
                "all_quotes_usable": all_usable,
                "max_quote_age_seconds": DEFAULT_MAX_QUOTE_AGE_SECONDS,
                "results": results,
            }, head_only)
        if path == "/etoro/instrument-search":
            query = parse_qs(urlsplit(self.path).query)
            symbol = query.get("symbol", ["AAPL"])[0].strip().upper()
            allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
            if not symbol or len(symbol) > 20 or any(char not in allowed for char in symbol):
                return self._json(400, {
                    "ok": False,
                    "error": "invalid_symbol",
                    "hint": "Use a ticker containing only letters, digits, dot, dash, or underscore.",
                }, head_only)
            api_path = "market-data/search?" + urlencode({"internalSymbolFull": symbol})
            status, payload = etoro_get(api_path)
            return self._json(status, payload, head_only)
        if path == "/etoro/watchlist":
            # Use the documented quick-start endpoint for listing default watchlist items.
            status, payload = etoro_get("watchlists/default-watchlists/items")
            return self._json(status, payload, head_only)
        if path == "/etoro/demo/portfolio":
            status, payload = etoro_get("trading/info/demo/portfolio")
            return self._json(status, payload, head_only)
        return self._json(404, {"error": "not_found"}, head_only)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != "/strategies/validate":
            return self._json(404, {"ok": False, "error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return self._json(400, {"ok": False, "error": "invalid_content_length"})
        if length < 1 or length > 16384:
            return self._json(413 if length > 16384 else 400, {
                "ok": False, "error": "request_body_size_invalid", "max_bytes": 16384,
            })
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "invalid_json"})
        if not isinstance(payload, dict) or not isinstance(payload.get("formula"), str):
            return self._json(400, {"ok": False, "error": "formula_required"})
        validate_only = payload.get("validate_only", False) is True
        values = payload.get("values", {})
        if not isinstance(values, dict):
            return self._json(400, {"ok": False, "error": "values_must_be_object"})
        try:
            result = validate_formula(payload["formula"]) if validate_only else evaluate_formula(payload["formula"], values)
            return self._json(200, {"ok": True, **result})
        except StrategyFormulaError as exc:
            return self._json(400, {"ok": False, "error": "invalid_strategy_formula", "detail": str(exc)})

    def _strategy_admin_authorized(self):
        expected = os.environ.get("TRADEBOOT_STRATEGY_ADMIN_TOKEN", "")
        supplied = self.headers.get("X-Strategy-Admin-Token", "")
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    def _read_json_body(self, max_bytes=262144):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return None, (400, {"ok": False, "error": "invalid_content_length"})
        if length < 1 or length > max_bytes:
            return None, (413 if length > max_bytes else 400, {
                "ok": False, "error": "request_body_size_invalid", "max_bytes": max_bytes,
            })
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")), None
        except (UnicodeError, json.JSONDecodeError):
            return None, (400, {"ok": False, "error": "invalid_json"})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/strategies", "/strategies/capital"):
            if not self._strategy_admin_authorized():
                return self._json(401, {"ok": False, "error": "strategy_admin_auth_required"})
            try:
                if path == "/strategies/capital":
                    return self._json(200, {"ok": True, "capital": get_general_capital()})
                return self._json(200, {"ok": True, "strategies": list_strategies()})
            except StrategyStoreUnavailable as exc:
                return self._json(503, {"ok": False, "error": str(exc)})
        self._dispatch()

    def do_PUT(self):
        path = urlsplit(self.path).path
        if path not in ("/strategies", "/strategies/capital"):
            return self._json(404, {"ok": False, "error": "not_found"})
        if not self._strategy_admin_authorized():
            return self._json(401, {"ok": False, "error": "strategy_admin_auth_required"})
        payload, error = self._read_json_body()
        if error:
            return self._json(error[0], error[1])
        try:
            if path == "/strategies/capital":
                if not isinstance(payload, dict) or isinstance(payload.get("capital"), bool) or not isinstance(payload.get("capital"), (int, float)):
                    raise ValueError("A numeric capital amount is required.")
                capital = set_general_capital(payload["capital"])
                return self._json(200, {"ok": True, "capital": capital})
            if not isinstance(payload, dict) or not isinstance(payload.get("strategies"), list):
                return self._json(400, {"ok": False, "error": "strategies_array_required"})
            for item in payload["strategies"]:
                if not isinstance(item, dict) or not isinstance(item.get("formula"), str):
                    raise ValueError("Each strategy needs a formula.")
                validate_formula(item["formula"])
            saved = replace_strategies(payload["strategies"])
            return self._json(200, {"ok": True, "strategies": saved})
        except StrategyFormulaError as exc:
            return self._json(400, {"ok": False, "error": "invalid_strategy_formula", "detail": str(exc)})
        except ValueError as exc:
            return self._json(400, {"ok": False, "error": "invalid_strategy", "detail": str(exc)})
        except StrategyStoreUnavailable as exc:
            return self._json(503, {"ok": False, "error": str(exc)})

    def do_HEAD(self):
        self._dispatch(head_only=True)

    def log_message(self, fmt, *args):
        # Keep logs free of request payloads and potentially sensitive data.
        super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Tradeboot read-only API listening on port {port}")
    server.serve_forever()
