"""Minimal, read-only eToro API client for Tradeboot.

This module deliberately supports only safe account checks and demo reads.
It does not place, modify, or close orders.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = "https://public-api.etoro.com"


class EtoroConfigurationError(RuntimeError):
    """Raised when required eToro API credentials are not configured."""


class EtoroApiError(RuntimeError):
    """Safe API error that never includes request headers or credentials."""

    def __init__(self, status_code: int | None, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class EtoroConfig:
    api_key: str
    user_key: str
    timeout_seconds: float = 10.0
    api_base: str = API_BASE

    @classmethod
    def from_env(cls) -> "EtoroConfig":
        api_key = os.getenv("ETORO_API_KEY", "").strip()
        user_key = os.getenv("ETORO_USER_KEY", "").strip()
        if not api_key or not user_key:
            raise EtoroConfigurationError(
                "Set ETORO_API_KEY and ETORO_USER_KEY in the environment."
            )
        try:
            timeout = float(os.getenv("ETORO_TIMEOUT_SECONDS", "10"))
        except ValueError as exc:
            raise EtoroConfigurationError(
                "ETORO_TIMEOUT_SECONDS must be a number."
            ) from exc
        if timeout <= 0 or timeout > 60:
            raise EtoroConfigurationError(
                "ETORO_TIMEOUT_SECONDS must be greater than 0 and at most 60."
            )
        return cls(api_key=api_key, user_key=user_key, timeout_seconds=timeout)


class EtoroClient:
    """Read-only client for basic eToro connectivity checks."""

    def __init__(self, config: EtoroConfig) -> None:
        self._config = config

    def _get(self, path: str) -> Any:
        url = f"{self._config.api_base.rstrip('/')}/{path.lstrip('/')}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "x-api-key": self._config.api_key,
                "x-user-key": self._config.user_key,
                "x-request-id": str(uuid.uuid4()),
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._config.timeout_seconds) as response:
                raw = response.read()
                status = response.status
        except HTTPError as exc:
            # Do not expose response body or request headers: either could contain
            # account details or sensitive diagnostic information.
            raise EtoroApiError(exc.code, f"eToro API returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise EtoroApiError(None, "Could not reach the eToro API.") from None

        if status < 200 or status >= 300:
            raise EtoroApiError(status, f"eToro API returned HTTP {status}.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EtoroApiError(status, "eToro API returned an invalid JSON response.") from None

    def get_default_watchlist(self) -> Any:
        """Read the authenticated account's default watchlist."""
        return self._get("/api/v1/watchlists/default-watchlists/items")

    def get_demo_portfolio(self) -> Any:
        """Read the demo portfolio only; no order is sent."""
        return self._get("/api/v1/trading/info/demo/portfolio")

    def health_check(self) -> dict[str, Any]:
        """Verify credentials and API access using a read-only endpoint."""
        watchlist = self.get_default_watchlist()
        count = len(watchlist) if isinstance(watchlist, list) else None
        return {
            "provider": "eToro",
            "connected": True,
            "mode": "read-only",
            "default_watchlist_items": count,
            "orders_enabled": False,
        }
