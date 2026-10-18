# Tradeboot

Tradeboot is a standalone service designed to use **eToro as its only broker**. It does not import or share the TradeScanner engine. The initial integration is read-only: connection check, default watchlist, and portfolio retrieval. Live order execution remains disabled until the API connection and demo workflows have been verified.

## Environment variables

Set these in the hosting provider's secret/environment settings (never commit them):

- `ETORO_API_KEY`: eToro Public API Key
- `ETORO_USER_KEY`: user-specific eToro key
- `ETORO_ENVIRONMENT`: `demo` by default; `real` is allowed for read-only portfolio access

Generate keys in eToro under **Settings → Trading → API Key Management**. Create a Demo key for initial validation. The key must have the necessary Read permissions. Do not paste keys into chat or source files.

## Run locally

```bash
python -m pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Endpoints

- `GET /health` — service health
- `GET /api/status` — configuration state, without revealing credentials
- `GET /api/etoro/connection` — verifies API access by reading the default watchlist
- `GET /api/etoro/watchlist` — reads default watchlist items
- `GET /api/etoro/portfolio` — reads portfolio for `ETORO_ENVIRONMENT`
- `GET /api/etoro/portfolio?environment=demo` or `real` — read-only portfolio selection

A successful `/health` only means the service is running. A successful `/api/etoro/connection` is the actual authentication check. Trading/order placement is intentionally not exposed yet.

## Deployment

For Render, use a **new Web Service** pointed at `SalvajesPremarket/Tradeboot`, branch `main` after the integration is reviewed and merged. Build command: `pip install -r requirements.txt`. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`. Configure the three environment variables in Render; never reuse TradeScanner credentials or its service.

## Safety

The API client sanitizes upstream errors and never logs or returns API keys. Begin with a Demo key and read-only permissions. No orders are sent by this version.
