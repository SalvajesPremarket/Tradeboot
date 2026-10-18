"""Minimal, safe eToro Public API client for Tradeboot.

Credentials are read only from environment variables and never logged.
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any

BASE_URL = "https://public-api.etoro.com"


class EtoroError(RuntimeError):
    """Sanitized eToro API error; never includes request headers or secrets."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class EtoroClient:
    def __init__(self, api_key: str | None = None, user_key: str | None = None,
                 environment: str | None = None, timeout: float = 12.0):
        self.api_key = (api_key if api_key is not None else os.getenv("ETORO_API_KEY", "")).strip()
        self.user_key = (user_key if user_key is not None else os.getenv("ETORO_USER_KEY", "")).strip()
        self.environment = (environment or os.getenv("ETORO_ENVIRONMENT", "demo")).strip().lower()
        self.timeout = timeout
        if self.environment not in {"demo", "real"}:
            raise ValueError("ETORO_ENVIRONMENT must be 'demo' or 'real'.")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.user_key)

    def _get(self, path: str) -> Any:
        if not self.configured:
            raise EtoroError("eToro credentials are not configured.")
        request = Request(
            f"{BASE_URL}{path}",
            headers={
                "Accept": "application/json",
                "x-api-key": self.api_key,
                "x-user-key": self.user_key,
                "x-request-id": str(uuid.uuid4()),
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except HTTPError as exc:
            # Do not include response body/headers: they may contain sensitive diagnostics.
            raise EtoroError(f"eToro API returned HTTP {exc.code}.", exc.code) from None
        except URLError:
            raise EtoroError("Could not reach eToro API.") from None
        except TimeoutError:
            raise EtoroError("eToro API request timed out.") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EtoroError("eToro API returned an invalid JSON response.") from None

    def get_watchlist(self) -> Any:
        return self._get("/api/v1/watchlists/default-watchlists/items")

    def get_portfolio(self) -> Any:
        return self._get(f"/api/v1/trading/info/{self.environment}/portfolio")

    def connection_check(self) -> dict[str, Any]:
        data = self.get_watchlist()
        return {"connected": True, "provider": "eToro", "environment": self.environment,
                "watchlist_items": len(data) if isinstance(data, list) else None}
