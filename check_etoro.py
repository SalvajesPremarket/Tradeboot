"""Run safe, read-only eToro connectivity checks; never submits orders."""
import json
import sys

from etoro_client import EtoroApiError, EtoroClient, EtoroConfig, EtoroConfigurationError


def main() -> int:
    try:
        client = EtoroClient(EtoroConfig.from_env())
        watchlist = client.get_default_watchlist()
        portfolio = client.get_demo_portfolio()
    except EtoroConfigurationError as exc:
        print(f"CONFIGURATION ERROR: {exc}")
        return 2
    except EtoroApiError as exc:
        status = f"HTTP {exc.status_code}" if exc.status_code else "network error"
        print(f"eToro check failed ({status}): {exc}")
        return 1

    watchlist_count = len(watchlist) if isinstance(watchlist, list) else "unknown"
    portfolio_count = len(portfolio) if isinstance(portfolio, list) else "response received"
    print(json.dumps({
        "provider": "eToro",
        "api_connection": "ok",
        "watchlist_items": watchlist_count,
        "demo_portfolio": "ok",
        "demo_portfolio_items": portfolio_count,
        "orders_sent": False,
        "mode": "read-only verification",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
