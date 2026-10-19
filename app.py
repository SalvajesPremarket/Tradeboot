"""Read-only HTTP interface for the isolated Tradeboot scaffold.

No broker connection, market-data connection, or order execution is implemented.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tradeboot import Tradeboot


module = Tradeboot()


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
        if self.path == "/health":
            return self._json(200, {"ok": True, "service": "Tradeboot"}, head_only)
        if self.path == "/":
            return self._json(200, {
                "service": "Tradeboot",
                "status": "standby",
                "endpoints": ["/health", "/status", "/watchlist"],
                "trading_enabled": False,
            }, head_only)
        if self.path in ("/status", "/watchlist"):
            return self._json(200, module.snapshot(), head_only)
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
