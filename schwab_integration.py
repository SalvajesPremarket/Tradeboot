"""Read-only Schwab OAuth and market-data integration for Tradeboot."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from strategy_store import _connect, StrategyStoreUnavailable

OAUTH_BASE = "https://api.schwabapi.com/v1/oauth"
MARKET_DATA_BASE = "https://api.schwabapi.com/marketdata/v1"
TIMEOUT_SECONDS = 15


def _config():
    client_id = os.environ.get("SCHWAB_CLIENT_ID") or os.environ.get("SCHWAB_APP_KEY")
    client_secret = os.environ.get("SCHWAB_CLIENT_SECRET") or os.environ.get("SCHWAB_APP_SECRET")
    redirect_uri = os.environ.get("SCHWAB_REDIRECT_URI") or os.environ.get("SCHWAB_CALLBACK_URL")
    return client_id, client_secret, redirect_uri


def _fernet():
    key = os.environ.get("SCHWAB_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise ValueError("schwab_token_encryption_key_missing")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise ValueError("schwab_token_encryption_key_invalid")


def _ensure_token_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tradeboot_schwab_oauth (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            encrypted_payload TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def _save_tokens(tokens):
    cipher = _fernet()
    encrypted = cipher.encrypt(json.dumps(tokens, separators=(",", ":")).encode("utf-8")).decode("ascii")
    with _connect() as conn:
        _ensure_token_table(conn)
        conn.execute("""
            INSERT INTO tradeboot_schwab_oauth (id, encrypted_payload, updated_at)
            VALUES (1, %s, NOW())
            ON CONFLICT (id) DO UPDATE
            SET encrypted_payload = EXCLUDED.encrypted_payload, updated_at = NOW()
        """, (encrypted,))


def _load_tokens():
    with _connect() as conn:
        _ensure_token_table(conn)
        row = conn.execute(
            "SELECT encrypted_payload FROM tradeboot_schwab_oauth WHERE id = 1"
        ).fetchone()
    if row:
        try:
            return json.loads(_fernet().decrypt(row[0].encode("ascii")).decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeError, json.JSONDecodeError, TypeError, IndexError):
            raise ValueError("schwab_stored_tokens_unreadable")
    # Backward-compatible bootstrap from the refresh token the operator added to Render.
    refresh_token = os.environ.get("SCHWAB_REFRESH_TOKEN", "").strip()
    if refresh_token:
        return {"refresh_token": refresh_token, "expires_at": 0}
    return {}


def _state_signature(message, secret):
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _new_state(client_secret):
    issued = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    message = issued + "." + nonce
    return message + "." + _state_signature(message, client_secret)


def _valid_state(state, client_secret):
    try:
        issued, nonce, signature = state.split(".", 2)
        timestamp = int(issued)
    except (ValueError, AttributeError):
        return False
    message = issued + "." + nonce
    expected = _state_signature(message, client_secret)
    return abs(int(time.time()) - timestamp) <= 600 and hmac.compare_digest(signature, expected)


def authorization_url():
    client_id, client_secret, redirect_uri = _config()
    if not client_id or not client_secret or not redirect_uri:
        raise ValueError("schwab_oauth_environment_not_configured")
    # Validate encryption configuration before starting a flow that cannot safely persist tokens.
    _fernet()
    state = _new_state(client_secret)
    return "https://api.schwabapi.com/v1/oauth/authorize?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    })


def _token_request(form):
    client_id, client_secret, redirect_uri = _config()
    if not client_id or not client_secret:
        raise ValueError("schwab_oauth_environment_not_configured")
    credentials = base64.b64encode((client_id + ":" + client_secret).encode("utf-8")).decode("ascii")
    request = Request(
        OAUTH_BASE + "/token",
        data=urlencode(form).encode("ascii"),
        headers={
            "Authorization": "Basic " + credentials,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "Tradeboot/1.0 (read-only Schwab market-data client)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Never return the upstream response body; it may contain sensitive diagnostics.
        raise ValueError("schwab_token_exchange_rejected_" + str(exc.code))
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
        raise ValueError("schwab_token_service_unavailable")
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ValueError("schwab_token_response_invalid")
    now = int(time.time())
    payload["expires_at"] = now + max(60, int(payload.get("expires_in", 1800)))
    _save_tokens(payload)
    return payload


def complete_authorization(query):
    client_id, client_secret, redirect_uri = _config()
    if not client_id or not client_secret or not redirect_uri:
        return 503, {"ok": False, "error": "schwab_oauth_environment_not_configured"}
    if query.get("error"):
        return 400, {"ok": False, "error": "schwab_authorization_denied"}
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    if not code or not state or not _valid_state(state, client_secret):
        return 400, {"ok": False, "error": "schwab_callback_parameters_invalid"}
    try:
        _fernet()
        _token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        })
        return 200, {
            "ok": True,
            "provider": "schwab",
            "authenticated": True,
            "message": "Schwab OAuth completed. Tokens were stored encrypted.",
        }
    except Exception as exc:
        # Error names only; never return credentials or provider response bodies.
        return 503, {"ok": False, "error": str(exc) if str(exc).startswith("schwab_") else "schwab_token_storage_unavailable"}


def _access_token():
    client_id, client_secret, redirect_uri = _config()
    if not client_id or not client_secret:
        raise ValueError("schwab_oauth_environment_not_configured")
    _fernet()
    try:
        tokens = _load_tokens()
    except StrategyStoreUnavailable:
        raise ValueError("schwab_token_storage_unavailable")
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("schwab_token_storage_unavailable")
    access_token = tokens.get("access_token")
    expires_at = int(tokens.get("expires_at") or 0)
    if access_token and expires_at > int(time.time()) + 90:
        return access_token
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise ValueError("schwab_authorization_required")
    refreshed = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    if not refreshed.get("refresh_token"):
        # Do not overwrite a working refresh token with an incomplete rotation response.
        refreshed["refresh_token"] = refresh_token
        _save_tokens(refreshed)
    return refreshed["access_token"]


def get_status():
    client_id, client_secret, redirect_uri = _config()
    configured = bool(client_id and client_secret and redirect_uri and os.environ.get("SCHWAB_TOKEN_ENCRYPTION_KEY"))
    try:
        tokens = _load_tokens()
        has_refresh = bool(tokens.get("refresh_token"))
        authorized = bool(tokens.get("access_token") and tokens.get("refresh_token"))
        return 200, {
            "ok": True,
            "provider": "schwab",
            "configured": configured,
            "authorized": authorized,
            "refresh_token_present": has_refresh,
            "connection_verified": False,
            "access_token_cached": bool(tokens.get("access_token") and int(tokens.get("expires_at") or 0) > int(time.time())),
            "market_data_read_only": True,
            "real_orders_enabled": False,
            "message": "OAuth configuration present." if configured else "Set Schwab OAuth and token-encryption environment variables.",
        }
    except ValueError as exc:
        return 503, {"ok": False, "provider": "schwab", "configured": configured, "error": str(exc)}
    except Exception:
        return 503, {"ok": False, "provider": "schwab", "configured": configured, "error": "schwab_token_storage_unavailable"}


def get_quotes(symbols):
    try:
        access_token = _access_token()
        request = Request(
            MARKET_DATA_BASE + "/quotes?" + urlencode({"symbols": ",".join(symbols)}),
            headers={
                "Authorization": "Bearer " + access_token,
                "Accept": "application/json",
                "User-Agent": "Tradeboot/1.0 (read-only Schwab market-data client)",
            },
            method="GET",
        )
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return 502, {"ok": False, "error": "schwab_quote_response_invalid"}
        return 200, {
            "ok": True,
            "provider": "schwab",
            "source": "marketdata_api",
            "symbols": symbols,
            "quotes": payload,
            "real_orders_enabled": False,
        }
    except HTTPError as exc:
        return 502, {"ok": False, "error": "schwab_market_data_rejected", "upstream_status": exc.code}
    except ValueError as exc:
        return 503, {"ok": False, "error": str(exc)}
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        return 502, {"ok": False, "error": "schwab_market_data_unavailable"}



def get_market_movers(universe="NASDAQ", sort="PERCENT_CHANGE_UP", frequency=0):
    """Read-only Schwab market movers for US exchanges; no order endpoints are used."""
    allowed_universes = {"NASDAQ", "NYSE", "EQUITY_ALL"}
    allowed_sorts = {"VOLUME", "TRADES", "PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN"}
    if universe not in allowed_universes or sort not in allowed_sorts or frequency not in (0, 1, 5, 10, 30, 60):
        return 400, {"ok": False, "error": "invalid_mover_parameters"}
    try:
        access_token = _access_token()
        request = Request(
            MARKET_DATA_BASE + "/movers/" + universe + "?" + urlencode({"sort": sort, "frequency": frequency}),
            headers={
                "Authorization": "Bearer " + access_token,
                "Accept": "application/json",
                "User-Agent": "Tradeboot/1.0 (read-only Schwab market-movers client)",
            },
            method="GET",
        )
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("screeners") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return 502, {"ok": False, "error": "schwab_movers_response_invalid"}
        return 200, {"ok": True, "provider": "schwab", "universe": universe, "screeners": rows,
                     "real_orders_enabled": False}
    except HTTPError as exc:
        return 502, {"ok": False, "error": "schwab_movers_rejected", "upstream_status": exc.code}
    except ValueError as exc:
        return 503, {"ok": False, "error": str(exc)}
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        return 502, {"ok": False, "error": "schwab_movers_unavailable"}

def get_price_history(symbol, *, daily=False):
    """Read-only Schwab price history: minute bars for replay or daily bars for D/W context."""
    import re
    if not isinstance(symbol, str):
        return 400, {"ok": False, "error": "invalid_symbol"}
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 20 or not re.fullmatch(r"[A-Z0-9._-]+", symbol):
        return 400, {"ok": False, "error": "invalid_symbol"}
    if daily:
        params = {
            "symbol": symbol, "periodType": "year", "period": 2,
            "frequencyType": "daily", "frequency": 1,
            "needExtendedHoursData": "false",
        }
    else:
        params = {
            "symbol": symbol, "periodType": "day", "period": 5,
            "frequencyType": "minute", "frequency": 1,
            "needExtendedHoursData": "true",
        }
    try:
        access_token = _access_token()
        request = Request(
            MARKET_DATA_BASE + "/pricehistory?" + urlencode(params),
            headers={
                "Authorization": "Bearer " + access_token,
                "Accept": "application/json",
                "User-Agent": "Tradeboot/1.0 (read-only Schwab price-history client)",
            },
            method="GET",
        )
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        candles = payload.get("candles") if isinstance(payload, dict) else None
        if not isinstance(candles, list):
            return 502, {"ok": False, "error": "schwab_price_history_response_invalid"}
        return 200, {
            "ok": True, "provider": "schwab", "symbol": symbol,
            "interval": "1day" if daily else "1minute",
            "candles": candles, "real_orders_enabled": False,
        }
    except HTTPError as exc:
        return 502, {"ok": False, "error": "schwab_price_history_rejected", "upstream_status": exc.code}
    except ValueError as exc:
        return 503, {"ok": False, "error": str(exc)}
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        return 502, {"ok": False, "error": "schwab_price_history_unavailable"}
