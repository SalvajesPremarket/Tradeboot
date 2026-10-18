"""Minimal, isolated eToro Public API client for Tradeboot.

Defaults to demo mode. This module deliberately does not submit orders.
Never put API credentials in source control; use environment variables.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

import requests


BASE_URL = "https://public-api.etoro.com/api/v1"


class EtoroConfigurationError(RuntimeError):
    """Raised when eToro credentials or mode configuration is invalid."""


class EtoroAPIError(RuntimeError):
    """Raised for non-successful or invalid eToro API responses."""


@dataclass(frozen=True)
class EtoroConfig:
    api_key: str
    user_key: str
    mode: str = "demo"
    timeout_seconds: float = 10.0
    live_trading_enabled: bool = False

    @classmethod
    def from_env(cls) -> "EtoroConfig":
        mode = os.getenv("ETORO_MODE", "demo").strip().lower()
        if mode not in {"demo", "real"}:
            raise EtoroConfigurationError("ETORO_MODE must be 'demo' or 'real'.")
        live_enabled = os.getenv("ETORO_LIVE_TRADING_ENABLED", "false").strip().lower()
        if live_enabled not in {"true", "false", "1", "0", "yes", "no"}:
            raise EtoroConfigurationError(
                "ETORO_LIVE_TRADING_ENABLED must be a boolean value."
            )
        return cls(
            api_key=os.getenv("ETORO_API_KEY", "").strip(),
            user_key=os.getenv("ETORO_USER_KEY", "").strip(),
            mode=mode,
            live_trading_enabled=live_enabled in {"true", "1", "yes"},
        )


class EtoroClient:
    """Read-only eToro client; order execution is intentionally not implemented."""

    def __init__(
        self,
        config: EtoroConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or EtoroConfig.from_env()
        if not self.config.api_key or not self.config.user_key:
            raise EtoroConfigurationError(
                "Set ETORO_API_KEY and ETORO_USER_KEY as environment variables."
            )
        if self.config.mode == "real" and not self.config.live_trading_enabled:
            raise EtoroConfigurationError(
                "Real mode is locked. Keep ETORO_MODE=demo until live use is explicitly enabled."
            )
        if self.config.mode not in {"demo", "real"}:
            raise EtoroConfigurationError("Mode must be 'demo' or 'real'.")
        self.session = session or requests.Session()

    @property
    def mode(self) -> str:
        return self.config.mode

    def _request(self, method: str, path: str) -> Any:
        url = f"{BASE_URL}{path}"
        headers = {
            "x-api-key": self.config.api_key,
            "x-user-key": self.config.user_key,
            "x-request-id": str(uuid.uuid4()),
            "accept": "application/json",
        }
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            # Do not include request headers or credentials in surfaced errors.
            raise EtoroAPIError(f"Could not reach eToro API ({type(exc).__name__}).") from None

        if not 200 <= response.status_code < 300:
            raise EtoroAPIError(f"eToro API returned HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError:
            raise EtoroAPIError("eToro API returned an invalid JSON response.") from None

    def get_default_watchlist(self) -> Any:
        """Read the account's default watchlist; does not change it."""
        return self._request("GET", "/watchlists/default-watchlists/items")

    def get_account_pnl(self) -> Any:
        """Read P&L for the configured demo or real account."""
        return self._request("GET", f"/trading/info/{self.mode}/pnl")

    def status(self) -> dict[str, Any]:
        """Return safe configuration status without exposing credentials."""
        return {
            "module": "Tradeboot",
            "provider": "eToro",
            "configured": bool(self.config.api_key and self.config.user_key),
            "mode": self.mode,
            "live_trading_enabled": self.config.live_trading_enabled,
            "order_execution_enabled": False,
        }
