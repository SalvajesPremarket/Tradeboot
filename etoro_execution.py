"""Guarded eToro market-order adapter.

Demo orders are sent only when ETORO_TRADING_ENABLED=true and
ETORO_TRADING_MODE=demo. This adapter refuses real-money mode entirely.
Keep strategy execution and risk checks upstream of this module; this module
is not a strategy engine.
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://public-api.etoro.com/api/v2/"
TIMEOUT_SECONDS = 12


class EtoroExecutionError(RuntimeError):
    """A sanitized execution/configuration failure."""


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _configuration() -> tuple[str, str, str, float]:
    mode = os.environ.get("ETORO_TRADING_MODE", "disabled").strip().lower()
    # This release is intentionally Demo-only: no environment variable can route
    # orders to the real-money endpoint.
    if mode != "demo":
        raise EtoroExecutionError("demo_mode_required")
    if not _truthy("ETORO_TRADING_ENABLED"):
        raise EtoroExecutionError("trading_disabled")
    api_key = os.environ.get("ETORO_API_KEY", "").strip()
    user_key = os.environ.get("ETORO_DEMO_USER_KEY", "").strip()
    if not user_key:
        user_key = os.environ.get("ETORO_USER_KEY", "").strip()
    if not api_key or not user_key:
        raise EtoroExecutionError("etoro_credentials_not_configured")
    try:
        max_amount = float(os.environ.get("ETORO_MAX_ORDER_AMOUNT_USD", "0"))
    except (TypeError, ValueError):
        max_amount = 0
    if max_amount <= 0:
        raise EtoroExecutionError("max_order_amount_not_configured")
    return mode, api_key, user_key, max_amount


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    mode, api_key, user_key, _ = _configuration()
    request_headers = {
        "x-api-key": api_key,
        "x-user-key": user_key,
        "x-request-id": str(uuid.uuid4()),
        "Accept": "application/json",
        "User-Agent": "Tradeboot/1.0",
    }
    body = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(BASE_URL + path, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(65536).decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"response_received": bool(raw)}
            if not isinstance(data, dict):
                data = {"response_received": True}
            return {"ok": True, "mode": mode, "http_status": response.status, "data": data}
    except HTTPError as exc:
        # Do not return raw provider bodies: they may contain account details.
        raise EtoroExecutionError(f"etoro_order_rejected_http_{exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise EtoroExecutionError("etoro_order_request_failed") from None


def open_market_order(
    instrument_id: int,
    amount_usd: float,
    is_buy: bool,
    *,
    leverage: int = 1,
    stop_loss_rate: float | None = None,
    take_profit_rate: float | None = None,
) -> dict:
    """Open one market position by cash amount, with strict caps and no leverage."""
    mode, _, _, max_amount = _configuration()
    if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id <= 0:
        raise EtoroExecutionError("invalid_instrument_id")
    if isinstance(amount_usd, bool) or not isinstance(amount_usd, (int, float)):
        raise EtoroExecutionError("invalid_order_amount")
    amount = float(amount_usd)
    if amount <= 0 or amount > max_amount:
        raise EtoroExecutionError("order_amount_outside_configured_limit")
    if not isinstance(is_buy, bool):
        raise EtoroExecutionError("invalid_order_side")
    if leverage != 1:
        raise EtoroExecutionError("leverage_must_be_one")
    payload = {
        "action": "open",
        "transaction": "buy" if is_buy else "sellShort",
        "instrumentId": instrument_id,
        "orderType": "mkt",
        "amount": amount,
        "orderCurrency": "usd",
        "leverage": 1,
    }
    # Stop/target fields are only forwarded when the caller supplies validated positive rates.
    for key, value in (("stopLossRate", stop_loss_rate), ("takeProfitRate", take_profit_rate)):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
                raise EtoroExecutionError("invalid_protective_order_rate")
            payload[key] = float(value)
    prefix = "trading/execution/demo/" if mode == "demo" else "trading/execution/"
    return _request("POST", prefix + "orders", payload)


def close_market_position(position_id: int) -> dict:
    """Close a specific position ID; callers must not substitute a ticker symbol."""
    mode, _, _, _ = _configuration()
    if isinstance(position_id, bool) or not isinstance(position_id, int) or position_id <= 0:
        raise EtoroExecutionError("invalid_position_id")
    # The documented close flow requires an instrument ID; do not guess it.
    raise EtoroExecutionError("close_requires_instrument_id_use_close_demo_position")


def close_demo_position(position_id: int, instrument_id: int) -> dict:
    """Submit a full close via eToro Demo v1; acceptance is not final confirmation."""
    mode, api_key, user_key, _ = _configuration()
    if mode != "demo":
        raise EtoroExecutionError("demo_mode_required")
    for name, value in (("position_id", position_id), ("instrument_id", instrument_id)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EtoroExecutionError("invalid_" + name)
    request_id = str(uuid.uuid4())
    request = Request(
        BASE_URL.replace("/api/v2/", "/api/v1/") +
        "trading/execution/demo/market-close-orders/positions/" + str(position_id),
        data=json.dumps({"InstrumentID": instrument_id, "UnitsToDeduct": None}).encode("utf-8"),
        headers={
            "x-api-key": api_key, "x-user-key": user_key, "x-request-id": request_id,
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "Tradeboot/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(65536).decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"response_received": bool(raw)}
            if not isinstance(data, dict):
                data = {"response_received": True}
            return {"ok": True, "mode": "demo", "http_status": response.status,
                    "request_id": request_id, "data": data, "confirmation_required": True}
    except HTTPError as exc:
        raise EtoroExecutionError(f"etoro_close_rejected_http_{exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise EtoroExecutionError("etoro_close_request_failed") from None


def get_demo_portfolio() -> dict:
    """Read current Demo portfolio; this does not execute trades."""
    mode, api_key, user_key, _ = _configuration()
    if mode != "demo":
        raise EtoroExecutionError("demo_mode_required")
    request = Request(
        BASE_URL.replace("/api/v2/", "/api/v1/") + "trading/info/demo/portfolio",
        headers={"x-api-key": api_key, "x-user-key": user_key,
                 "x-request-id": str(uuid.uuid4()), "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(65536).decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return {"ok": True, "mode": "demo", "http_status": response.status,
                    "data": data if isinstance(data, dict) else {}}
    except HTTPError as exc:
        raise EtoroExecutionError(f"etoro_portfolio_rejected_http_{exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise EtoroExecutionError("etoro_portfolio_request_failed") from None
