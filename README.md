# Tradeboot

Tradeboot is a standalone service designed to use **eToro as its only broker**. It does not import or share the TradeScanner engine. The initial integration is read-only: connection check, default watchlist, and portfolio retrieval. Order execution is disabled.

## Environment variables

Set these in the hosting provider's secret/environment settings. Never commit actual values:

- `ETORO_API_KEY`: eToro Public API Key
- `ETORO_USER_KEY`: user-specific eToro key
- `ETORO_ENVIRONMENT`: `demo` by default; `real` currently only selects a read-only portfolio endpoint
- `ETORO_TIMEOUT_SECONDS`: request timeout (default 12 seconds)
- `TRADEBOOT_ACCESS_TOKEN`: a long random token that protects account-data endpoints on a public deployment

Generate eToro keys under **Settings → Trading → API Key Management**. Start with a Demo key with Read permission. The public API key and user key must be for the same environment. Do not paste keys or the Tradeboot access token into chat or source files. `.env.example` lists names only.

## Run locally

```bash
python -m pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Endpoints

- `GET /health` — public process health only
- `GET /api/status` — configuration state, without revealing credentials
- `GET /api/etoro/connection` — verifies API access by reading the default watchlist
- `GET /api/etoro/watchlist` — reads default watchlist items
- `GET /api/etoro/portfolio` — reads portfolio for `ETORO_ENVIRONMENT`
- `GET /api/etoro/portfolio?environment=demo` or `real` — read-only portfolio selection

The account-data endpoints require `Authorization: Bearer <TRADEBOOT_ACCESS_TOKEN>`. Without this token, a public deployment will not expose account data. A successful `/health` only means the service is running; `/api/etoro/connection` is the authentication check against eToro. No orders are sent.

## Tests

```bash
python -m pip install -r requirements.txt
PYTHONPATH=. python -m pytest -q
```

GitHub Actions runs the tests on pushes and pull requests.

## Deployment

After this integration is reviewed and CI passes, deploy as a **new Render Web Service** using repository `SalvajesPremarket/Tradeboot`, branch `main` after merge. Build command: `pip install -r requirements.txt`. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`. Configure the five variables in Render's environment settings. Do not reuse TradeScanner's service or credentials.

## Safety and remaining work

- Begin with eToro Demo credentials and read-only permissions.
- Verify a real API response in Demo before considering any live integration.
- Define strategy, maximum position size, maximum daily loss, stop loss, and rules for long/short trades.
- Implement and test the decision loop, logging, and duplicate-order prevention.
- Only after separate validation, design an order execution module with explicit Demo/Real separation and a default-off switch.

**Live trading and automated order placement are not implemented in this version.**
