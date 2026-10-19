"""Tradeboot HTTP API. eToro is the only supported broker integration."""
import hmac
import os

from fastapi import Depends, FastAPI, HTTPException, Query, Header
from etoro_client import EtoroClient, EtoroError

app = FastAPI(title="Tradeboot", version="0.1.0", description="Tradeboot connected to eToro only")


def client() -> EtoroClient:
    return EtoroClient()


def require_access_token(authorization: str | None = Header(default=None)) -> None:
    """Protect account-data endpoints on a publicly reachable deployment."""
    expected = os.getenv("TRADEBOOT_ACCESS_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Tradeboot access token is not configured.")
    supplied = authorization or ""
    if not supplied.startswith("Bearer ") or not hmac.compare_digest(
        supplied.removeprefix("Bearer ").strip(), expected
    ):
        raise HTTPException(status_code=401, detail="Unauthorized.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "Tradeboot", "broker": "eToro"}


@app.get("/api/status")
def status() -> dict:
    etoro = client()
    return {
        "service": "Tradeboot",
        "broker": "eToro",
        "credentials_configured": etoro.configured,
        "environment": etoro.environment,
        "market_data_connected": False,
        "trading_enabled": False,
        "message": "Credentials are configured but the eToro connection has not been tested." if etoro.configured else "Set ETORO_API_KEY and ETORO_USER_KEY in the service environment.",
    }


@app.get("/api/etoro/connection")
def connection(_: None = Depends(require_access_token)) -> dict:
    etoro = client()
    if not etoro.configured:
        raise HTTPException(status_code=503, detail="eToro credentials are not configured.")
    try:
        return etoro.connection_check()
    except EtoroError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from None


@app.get("/api/etoro/watchlist")
def watchlist(_: None = Depends(require_access_token)) -> dict:
    etoro = client()
    if not etoro.configured:
        raise HTTPException(status_code=503, detail="eToro credentials are not configured.")
    try:
        return {"provider": "eToro", "items": etoro.get_watchlist()}
    except EtoroError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from None


@app.get("/api/etoro/portfolio", dependencies=[Depends(require_access_token)])
def portfolio(environment: str | None = Query(default=None, pattern="^(demo|real)$")) -> dict:
    # Read-only portfolio retrieval. Trading/order endpoints are deliberately absent.
    etoro = client()
    if environment:
        etoro.environment = environment
    try:
        return {"provider": "eToro", "environment": etoro.environment, "portfolio": etoro.get_portfolio()}
    except EtoroError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from None
