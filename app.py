"""Read-only HTTP interface for the isolated Tradeboot scaffold.

No broker connection, market-data connection, or order execution is implemented.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tradeboot import Tradeboot


module = Tradeboot()


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "service": "Tradeboot"})
        if self.path == "/":
            return self._json(200, {
                "service": "Tradeboot",
                "status": "standby",
                "endpoints": ["/health", "/status", "/watchlist"],
                "trading_enabled": False,
            })
        if self.path in ("/status", "/watchlist"):
            return self._json(200, module.snapshot())
        return self._json(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        # Keep logs free of request payloads and potentially sensitive data.
        super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Tradeboot read-only API listening on port {port}")
    server.serve_forever()
