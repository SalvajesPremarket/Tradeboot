"""Read-only HTTP interface for the isolated Tradeboot scaffold.

eToro requests are restricted to GET endpoints and use credentials from environment
variables. No order placement or account-changing requests are implemented.
"""
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlencode, urlsplit

from tradeboot import Tradeboot


module = Tradeboot()
ETORO_BASE_URL = "https://public-api.etoro.com/api/v1/"
ETORO_TIMEOUT_SECONDS = 15


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
                        safe_value = re.sub(r"(?i)(key|token|secret|authorization)\\s*[:=]\\s*[^,;\\s]+", r"\\1=[REDACTED]", safe_value)
                        safe_value = re.sub(r"\\b[A-Za-z0-9_-]{40,}\\b", "[REDACTED]", safe_value)
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
            return self._json(200, {
                "service": "Tradeboot",
                "status": "standby",
                "endpoints": [
                    "/health", "/status", "/watchlist",
                    "/etoro/watchlist", "/etoro/demo/portfolio",
                    "/etoro/instrument-search?symbol=AAPL",
                    "/etoro/instrument-rates?symbol=AAPL",
                ],
                "trading_enabled": False,
                "etoro_mode": "demo_read_only",
            }, head_only)
        if path in ("/status", "/watchlist"):
            return self._json(200, module.snapshot(), head_only)
        if path == "/etoro/instrument-rates":
            query = parse_qs(urlsplit(self.path).query)
            symbol = query.get("symbol", ["AAPL"])[0].strip().upper()
            allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
            if not symbol or len(symbol) > 20 or any(char not in allowed for char in symbol):
                return self._json(400, {
                    "ok": False,
                    "error": "invalid_symbol",
                    "hint": "Use a ticker containing only letters, digits, dot, dash, or underscore.",
                }, head_only)

            search_status, search_result = etoro_get(
                "market-data/search?" + urlencode({"internalSymbolFull": symbol})
            )
            if search_status != 200 or not search_result.get("ok"):
                return self._json(search_status, search_result, head_only)

            upstream = search_result.get("data")
            records = upstream.get("data", []) if isinstance(upstream, dict) else []
            if isinstance(records, dict):
                records = [records]
            if not isinstance(records, list):
                records = []

            instrument_id = None
            for record in records:
                if not isinstance(record, dict):
                    continue
                record_symbol = str(
                    record.get("internalSymbolFull") or record.get("symbol") or ""
                ).upper()
                candidate_id = record.get("instrumentId", record.get("InstrumentId"))
                if record_symbol == symbol and isinstance(candidate_id, int):
                    instrument_id = candidate_id
                    break

            if instrument_id is None:
                return self._json(404, {
                    "ok": False,
                    "error": "instrument_not_found",
                    "symbol": symbol,
                    "hint": "Search returned no exact symbol match with an instrument ID.",
                }, head_only)

            rates_status, rates_result = etoro_get(
                "market-data/instruments/rates?" + urlencode({"instrumentIds": str(instrument_id)})
            )
            if rates_status != 200 or not rates_result.get("ok"):
                return self._json(rates_status, rates_result, head_only)
            return self._json(200, {
                "ok": True,
                "symbol": symbol,
                "instrument_id": instrument_id,
                "rates": rates_result.get("data"),
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

    def do_GET(self):
        self._dispatch()

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
