"""US-listed stock discovery and read-only market-data adapter for Tradeboot.

Free-plan discovery uses Schwab's NASDAQ and NYSE market-movers feed. Twelve Data's
paid market-movers endpoint is deliberately disabled to keep the deployment at $0
and prevent international listings from entering the automatic scanner.
"""
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.twelvedata.com"
TIMEOUT_SECONDS = 12
_API_KEY = (os.environ.get("TWELVE_DATA_API_KEY") or "").strip()
# Hard-disable paid endpoints: this deployment must remain on free plans and US stocks only.
# Ignore TWELVE_DATA_ENABLE_PAID_ENDPOINTS even if it exists in Render's environment.
_ENABLE_PAID_ENDPOINTS = False
_COUNTRIES_RAW = os.environ.get("TWELVE_DATA_MARKET_COUNTRIES", "").strip()
_COUNTRIES = tuple(dict.fromkeys(item.strip() for item in _COUNTRIES_RAW.split(",") if item.strip()))
_FALLBACK_COUNTRIES = (
    "United States", "Canada", "United Kingdom", "India", "Australia", "Germany",
    "Japan", "France", "Hong Kong", "Singapore", "Brazil", "South Africa",
)
_CANDIDATE_LIMIT = max(1, min(50, int(os.environ.get("TWELVE_DATA_CANDIDATE_LIMIT", "25"))))
_CACHE = {}
_CACHE_LOCK = threading.RLock()
_DIAGNOSTICS = {}
_COUNTRY_ROWS = {}
_SYMBOL_META = {}
_COUNTRY_CURSOR = 0
_LAST_STATS_REQUEST = 0.0
# Last successful US discovery list; symbol discovery only, never a substitute for fresh quotes.
_LAST_GOOD_US_CANDIDATES = (0.0, [])


def _request_json(endpoint, params=None, cache_key=None, ttl_seconds=60):
    if not _API_KEY:
        _DIAGNOSTICS[endpoint] = {"status": "missing_api_key", "provider": "twelve_data"}
        return 503, None
    now = time.monotonic()
    if cache_key:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]
    query = dict(params or {})
    query["apikey"] = _API_KEY
    request = Request(
        API_BASE + "/" + endpoint.lstrip("/") + "?" + urlencode(query),
        headers={"Accept": "application/json", "User-Agent": "Tradeboot/1.0"},
        method="GET",
    )
    status, payload = 502, None
    diagnostic = {"provider": "twelve_data", "endpoint": endpoint, "status": "request_failed"}
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
        if status == 200 and isinstance(payload, dict) and payload.get("status") != "error" and not payload.get("code"):
            diagnostic.update({"status": "ok", "http_status": status})
        else:
            diagnostic.update({
                "status": "provider_error" if isinstance(payload, dict) else "unexpected_payload",
                "http_status": status,
                "provider_code": payload.get("code") if isinstance(payload, dict) else None,
            })
            if status == 200:
                status = 502
    except HTTPError as exc:
        status = int(exc.code)
        diagnostic.update({"status": "http_error", "http_status": status})
        try:
            body = json.loads(exc.read().decode("utf-8"))
            if isinstance(body, dict):
                diagnostic["provider_code"] = body.get("code")
        except Exception:
            pass
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        diagnostic["status"] = "request_failed"
    with _CACHE_LOCK:
        _DIAGNOSTICS[endpoint] = diagnostic
        if cache_key:
            ttl = ttl_seconds if status == 200 else min(ttl_seconds, 60)
            _CACHE[cache_key] = (now + ttl, (status, payload))
    return status, payload


def is_configured():
    return bool(_API_KEY)


def get_diagnostics():
    with _CACHE_LOCK:
        return {key: dict(value) for key, value in _DIAGNOSTICS.items()}


def get_symbol_metadata(symbol):
    with _CACHE_LOCK:
        return dict(_SYMBOL_META.get(str(symbol).upper(), {}))


def _valid_symbol(value):
    import re
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Z0-9._-]{1,20}", value.strip().upper()))


def _country_list():
    """Use all countries in Twelve Data's exchange catalog unless explicitly restricted."""
    if _COUNTRIES:
        return _COUNTRIES
    status, payload = _request_json("exchanges", {"type": "Common Stock"}, "exchanges:common_stock", 86400)
    rows = payload.get("data") if status == 200 and isinstance(payload, dict) else None
    countries = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("country"), str):
                country = row["country"].strip()
                if country and country not in countries:
                    countries.append(country)
    if countries:
        # Begin with the US, then rotate across the rest of the provider's exchange countries.
        us = [country for country in countries if country.lower() in ("us", "usa", "united states")]
        rest = sorted(country for country in countries if country not in us)
        return tuple(us[:1] + rest)
    return _FALLBACK_COUNTRIES


def get_global_candidates(now=None, limit=None):
    """Return bounded US-listed candidates from Schwab NASDAQ/NYSE market movers."""
    global _COUNTRY_CURSOR, _LAST_GOOD_US_CANDIDATES
    if not _API_KEY and _ENABLE_PAID_ENDPOINTS:
        _DIAGNOSTICS["market_movers/stocks"] = {"status": "missing_api_key", "provider": "twelve_data"}
        return []
    if not _ENABLE_PAID_ENDPOINTS:
        # Basic Free cannot call Twelve Data market_movers. Use Schwab's read-only
        # US exchange movers feed when OAuth/market-data access is available.
        cache_key = "schwab:movers:us"
        current = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            # Keep a non-empty result available for a short outage grace period.
            if cached and isinstance(cached[1], list) and cached[1]:
                cached_fetched_at = cached[0] - 60
                if current - cached_fetched_at <= 900:
                    _LAST_GOOD_US_CANDIDATES = (cached_fetched_at, list(cached[1]))
        if cached and cached[0] > current:
            # Apply the same bound to cached and freshly fetched candidates.
            # Otherwise a full cache can bypass the free-plan cycle limit.
            cached_symbols = [
                item["symbol"] for item in cached[1]
                if isinstance(item, dict) and item.get("symbol")
            ]
            return cached_symbols[:max(1, min(50, int(limit or _CANDIDATE_LIMIT)))]
        try:
            from schwab_integration import get_market_movers
            merged = {}
            failures = []
            for universe in ("NASDAQ", "NYSE"):
                status, payload = get_market_movers(universe=universe, sort="PERCENT_CHANGE_UP", frequency=0)
                rows = payload.get("screeners") if status == 200 and isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    failures.append(str(payload.get("error", "unavailable")) if isinstance(payload, dict) else "unavailable")
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    symbol = str(row.get("symbol") or "").strip().upper()
                    try:
                        price = float(row.get("lastPrice"))
                        change = float(row.get("netPercentChange"))
                    except (TypeError, ValueError):
                        continue
                    if not _valid_symbol(symbol) or not 1.0 <= price <= 20.0:
                        continue
                    item = {
                        "symbol": symbol, "country": "United States",
                        "exchange": universe, "price": price,
                        "percent_change": change, "provider": "schwab",
                    }
                    merged[symbol] = item
            candidates = sorted(merged.values(), key=lambda item: item["percent_change"], reverse=True)
            scanner_provider = "schwab"
            yahoo_error = None

            # If Schwab OAuth is expired/rejected, keep the US-only scanner alive with
            # Yahoo Finance's US day-gainers screener. This is discovery data only:
            # the trading engine still requires its separate fresh quote/tick checks.
            if not candidates:
                yahoo_url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?" + urlencode({
                    "formatted": "false", "scrIds": "day_gainers", "count": 100,
                    "region": "US", "lang": "en-US",
                })
                try:
                    request = Request(
                        yahoo_url,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": "Mozilla/5.0 (compatible; Tradeboot/1.0; US stock scanner)",
                        },
                        method="GET",
                    )
                    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                        yahoo_payload = json.loads(response.read().decode("utf-8"))
                    finance = yahoo_payload.get("finance", {}) if isinstance(yahoo_payload, dict) else {}
                    result_rows = finance.get("result") if isinstance(finance, dict) else None
                    quotes = result_rows[0].get("quotes", []) if isinstance(result_rows, list) and result_rows and isinstance(result_rows[0], dict) else []
                    if not isinstance(quotes, list):
                        quotes = []
                    for row in quotes:
                        if not isinstance(row, dict):
                            continue
                        symbol = str(row.get("symbol") or "").strip().upper()
                        if not _valid_symbol(symbol) or row.get("quoteType") not in (None, "EQUITY"):
                            continue
                        try:
                            price = float(row.get("regularMarketPrice"))
                            change = float(row.get("regularMarketChangePercent"))
                        except (TypeError, ValueError):
                            continue
                        if not (1.0 <= price <= 20.0):
                            continue
                        item = {
                            "symbol": symbol, "country": "United States",
                            "exchange": str(row.get("exchange") or ""),
                            "price": price, "percent_change": change,
                            "provider": "yahoo_finance",
                            "market_time": row.get("regularMarketTime"),
                        }
                        merged[symbol] = item
                    candidates = sorted(merged.values(), key=lambda item: item["percent_change"], reverse=True)
                    scanner_provider = "yahoo_finance"
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError) as exc:
                    yahoo_error = type(exc).__name__
                except Exception as exc:
                    yahoo_error = type(exc).__name__

            stale_candidates_used = False
            if candidates:
                _LAST_GOOD_US_CANDIDATES = (current, list(candidates))
            else:
                last_good_at, last_good = _LAST_GOOD_US_CANDIDATES
                if last_good and current - last_good_at <= 900:
                    candidates = list(last_good)
                    scanner_provider = "last_known_us_candidates"
                    stale_candidates_used = True

            with _CACHE_LOCK:
                for item in candidates:
                    _SYMBOL_META[item["symbol"]] = item
                if candidates and stale_candidates_used:
                    diag = {
                        "status": "stale_fallback",
                        "provider": scanner_provider,
                        "detail": "last_known_us_candidates_after_temporary_provider_failure",
                        "candidate_count": len(candidates),
                        "candidate_age_seconds": int(current - _LAST_GOOD_US_CANDIDATES[0]),
                        "twelve_data_status": "skipped_plan_basic",
                        "schwab_errors": failures[:2],
                        "yahoo_error": yahoo_error,
                    }
                elif candidates:
                    diag = {
                        "status": "ok",
                        "provider": scanner_provider,
                        "detail": "us_day_gainers_fallback" if scanner_provider == "yahoo_finance" else "us_exchange_movers",
                        "candidate_count": len(candidates),
                        "twelve_data_status": "skipped_plan_basic",
                        "schwab_errors": failures[:2],
                    }
                else:
                    diag = {
                        "status": "all_candidate_sources_unavailable" if failures and yahoo_error else ("schwab_unavailable" if failures else "no_usable_candidates"),
                        "provider": scanner_provider,
                        "detail": "us_exchange_movers_fallback",
                        "candidate_count": 0,
                        "twelve_data_status": "skipped_plan_basic",
                        "schwab_errors": failures[:2],
                        "yahoo_error": yahoo_error,
                    }
                _DIAGNOSTICS["market_movers/stocks"] = diag
                _CACHE[cache_key] = (current + 60, candidates)
            return [item["symbol"] for item in candidates[:max(1, min(50, int(limit or _CANDIDATE_LIMIT)))]]
        except Exception as exc:
            _DIAGNOSTICS["market_movers/stocks"] = {
                "status": "schwab_unavailable",
                "provider": "schwab",
                "detail": type(exc).__name__,
                "twelve_data_status": "skipped_plan_basic",
            }
            return []
    countries = _country_list()
    if not countries:
        _DIAGNOSTICS["market_movers/stocks"] = {"status": "no_countries_configured", "provider": "twelve_data"}
        return []

    with _CACHE_LOCK:
        country = countries[_COUNTRY_CURSOR % len(countries)]
        _COUNTRY_CURSOR += 1
    cache_key = "movers:" + country.lower()
    status, payload = _request_json(
        "market_movers/stocks",
        {"direction": "gainers", "outputsize": 50, "country": country},
        cache_key=cache_key,
        ttl_seconds=300,
    )
    rows = payload.get("values") if status == 200 and isinstance(payload, dict) else None
    if isinstance(rows, list):
        cleaned = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not _valid_symbol(symbol):
                continue
            try:
                price = float(row.get("last"))
                change = float(row.get("percent_change"))
            except (TypeError, ValueError):
                continue
            if not (price > 0):
                continue
            meta = {
                "symbol": symbol,
                "country": str(row.get("country") or country),
                "exchange": str(row.get("exchange") or ""),
                "mic_code": str(row.get("mic_code") or ""),
                "price": price,
                "percent_change": change,
                "datetime": row.get("datetime"),
                "provider": "twelve_data",
            }
            cleaned.append(meta)
        with _CACHE_LOCK:
            _COUNTRY_ROWS[country] = (time.monotonic(), cleaned)
            for item in cleaned:
                # Prefer US listing metadata when the same ticker appears in multiple markets.
                prior = _SYMBOL_META.get(item["symbol"])
                is_us = item["country"].strip().lower() in ("us", "usa", "united states")
                prior_us = prior and str(prior.get("country", "")).strip().lower() in ("us", "usa", "united states")
                if prior is None or (is_us and not prior_us):
                    _SYMBOL_META[item["symbol"]] = item

    current = time.monotonic()
    merged = {}
    with _CACHE_LOCK:
        for country_name, (fetched_at, cached_rows) in list(_COUNTRY_ROWS.items()):
            if current - fetched_at > 7200:
                continue
            for item in cached_rows:
                symbol = item["symbol"]
                if symbol not in merged:
                    merged[symbol] = item
    candidates = [
        item for item in merged.values()
        if 1.0 <= float(item.get("price", 0)) <= 20.0
    ]
    candidates.sort(key=lambda item: float(item.get("percent_change", 0)), reverse=True)
    return [item["symbol"] for item in candidates[:max(1, min(50, int(limit or _CANDIDATE_LIMIT)))] ]


def _request_params_for_symbol(symbol, metadata=None):
    meta = metadata or get_symbol_metadata(symbol)
    params = {"symbol": symbol, "timezone": "UTC"}
    if meta.get("exchange"):
        params["exchange"] = meta["exchange"]
    if meta.get("country"):
        params["country"] = meta["country"]
    return params


def get_price_history(symbol, daily=False, metadata=None):
    """Return candles normalized to the Schwab adapter's payload shape."""
    if not _valid_symbol(symbol):
        return 400, {"ok": False, "error": "invalid_symbol", "provider": "twelve_data"}
    interval = "1day" if daily else "1min"
    params = _request_params_for_symbol(symbol, metadata)
    params.update({"interval": interval, "outputsize": 1000 if daily else 4000})
    meta = metadata or get_symbol_metadata(symbol)
    if not daily and str(meta.get("country", "")).strip().lower() in ("us", "usa", "united states"):
        params["prepost"] = "true"
    cache_key = "history:" + str(symbol).upper() + ":" + interval + ":" + str(params.get("exchange", "")) + ":" + str(params.get("country", ""))
    status, payload = _request_json("time_series", params, cache_key, 60 if not daily else 3600)
    values = payload.get("values") if status == 200 and isinstance(payload, dict) else None
    if not isinstance(values, list):
        return status if status != 200 else 502, {"ok": False, "error": "twelve_data_history_unavailable", "provider": "twelve_data"}
    candles = []
    for row in values:
        if not isinstance(row, dict) or not row.get("datetime"):
            continue
        candles.append({
            "datetime": row.get("datetime"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume", 0),
        })
    return 200, {"ok": True, "provider": "twelve_data", "symbol": symbol, "interval": interval, "candles": candles}


def get_quote(symbol, metadata=None):
    """Return a quote normalized to the fields consumed by the paper scanner."""
    if not _valid_symbol(symbol):
        return 400, {"ok": False, "error": "invalid_symbol", "provider": "twelve_data"}
    params = _request_params_for_symbol(symbol, metadata)
    cache_key = "quote:" + str(symbol).upper() + ":" + str(params.get("exchange", "")) + ":" + str(params.get("country", ""))
    status, payload = _request_json("quote", params, cache_key, 10)
    if status != 200 or not isinstance(payload, dict):
        return status if status != 200 else 502, {"ok": False, "error": "twelve_data_quote_unavailable", "provider": "twelve_data"}
    raw_price = payload.get("price") or payload.get("close") or payload.get("last")
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return 502, {"ok": False, "error": "twelve_data_quote_price_missing", "provider": "twelve_data"}
    stamp = payload.get("datetime") or payload.get("timestamp")
    stamp_ms = None
    if stamp is not None:
        try:
            if isinstance(stamp, (int, float)):
                stamp_ms = int(float(stamp) * 1000 if float(stamp) < 100000000000 else float(stamp))
            elif isinstance(stamp, str):
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                stamp_ms = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
        except (TypeError, ValueError, OverflowError, OSError):
            stamp_ms = None
    quote = {"lastPrice": price, "mark": price}
    if stamp_ms is not None:
        quote["quoteTimeInLong"] = stamp_ms
        quote["tradeTimeInLong"] = stamp_ms
    try:
        volume = payload.get("volume")
        if volume is not None:
            quote["totalVolume"] = float(volume)
    except (TypeError, ValueError):
        pass
    return 200, {"ok": True, "provider": "twelve_data", "quote": quote}


def _parse_timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            numeric = float(value)
            stamp = datetime.fromtimestamp(
                numeric / 1000 if numeric > 100000000000 else numeric,
                timezone.utc,
            )
            return stamp
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _find_float_shares(payload):
    """Only accept explicit float fields; never confuse shares outstanding with float."""
    names = {"float_shares", "shares_float", "floatshares", "float"}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower().replace("-", "_") in names:
                try:
                    parsed = float(value)
                    if parsed > 0:
                        return int(parsed)
                except (TypeError, ValueError):
                    pass
            found = _find_float_shares(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_float_shares(value)
            if found:
                return found
    return None


def _provider_json(url, api_key, provider):
    """Small read-only JSON request helper for optional free-tier providers."""
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Tradeboot/1.0 (read-only market-data client)"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return int(response.status), payload
    except HTTPError as exc:
        return int(exc.code), None
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        return 502, None


def _get_yahoo_float(symbol):
    """Read Yahoo Finance's public floatShares statistic without a paid data plan.

    Yahoo does not publish a stable official API; failures are non-fatal and never
    replaced with shares outstanding. JeanLong remains blocked when no explicit
    floatShares value is returned.
    """
    symbol = str(symbol or "").strip().upper()
    try:
        from http.cookiejar import CookieJar
        from urllib.request import HTTPCookieProcessor, build_opener
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        # Yahoo issues a crumb bound to its cookie. The initial consent endpoint may
        # return 404 while still setting the required cookie.
        try:
            opener.open(Request("https://fc.yahoo.com", headers=headers), timeout=TIMEOUT_SECONDS).close()
        except HTTPError:
            pass
        with opener.open(Request("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=headers), timeout=TIMEOUT_SECONDS) as response:
            crumb = response.read().decode("utf-8").strip()
        if not crumb or len(crumb) > 256 or "\n" in crumb:
            return None, "yahoo_crumb_unavailable"
        query = urlencode({"modules": "defaultKeyStatistics", "crumb": crumb})
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?{query}"
        with opener.open(Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        summary = payload.get("quoteSummary") if isinstance(payload, dict) else None
        results = summary.get("result") if isinstance(summary, dict) else None
        stats = results[0].get("defaultKeyStatistics") if isinstance(results, list) and results and isinstance(results[0], dict) else None
        raw = stats.get("floatShares") if isinstance(stats, dict) else None
        if isinstance(raw, dict):
            raw = raw.get("raw")
        shares = int(float(raw))
        if shares > 0:
            return shares, None
        return None, "yahoo_float_unavailable"
    except HTTPError as exc:
        return None, "yahoo_http_" + str(exc.code)
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
        return None, "yahoo_float_unavailable"
    except Exception as exc:
        return None, "yahoo_" + type(exc).__name__


def _get_finviz_float(symbol):
    """Read only the explicitly labeled Float field from Finviz's public quote page.

    This is a no-paid-plan fallback. It never substitutes shares outstanding or
    market capitalization for float, and parsing failures simply return unavailable.
    """
    import re
    from html.parser import HTMLParser

    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.in_td = False
            self.in_tr = False
            self.cell_parts = []
            self.row = []
            self.rows = []

        def handle_starttag(self, tag, attrs):
            if tag.lower() == "tr":
                self.in_tr = True
                self.row = []
            elif tag.lower() == "td" and self.in_tr:
                self.in_td = True
                self.cell_parts = []

        def handle_data(self, data):
            if self.in_td:
                self.cell_parts.append(data)

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag == "td" and self.in_td:
                self.row.append(" ".join("".join(self.cell_parts).split()))
                self.in_td = False
                self.cell_parts = []
            elif tag == "tr" and self.in_tr:
                if self.row:
                    self.rows.append(self.row)
                self.row = []
                self.in_tr = False

    symbol = str(symbol or "").strip().upper()
    if not symbol or not _valid_symbol(symbol):
        return None, "finviz_invalid_symbol"
    request = Request(
        "https://finviz.com/quote.ashx?t=" + urlencode({"": symbol})[1:],
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Mozilla/5.0 (compatible; Tradeboot/1.0; read-only)",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            html = response.read(2_000_000).decode("utf-8", errors="replace")
        parser = _TableParser()
        parser.feed(html)
        for row in parser.rows:
            for index, cell in enumerate(row[:-1]):
                # Finviz labels this statistic "Shs Float" on its quote table.
                # Accept only explicit float labels; never use "Shs Outstand".
                label = cell.strip().lower().rstrip(":")
                if label not in {"float", "shs float", "shares float"}:
                    continue
                value = row[index + 1].strip().replace(",", "").upper()
                match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMB]?)", value)
                if not match:
                    continue
                amount = float(match.group(1))
                multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
                shares = int(amount * multiplier)
                if shares > 0:
                    return shares, None
        return None, "finviz_float_field_unavailable"
    except HTTPError as exc:
        return None, "finviz_http_" + str(exc.code)
    except (URLError, TimeoutError, OSError, UnicodeError):
        return None, "finviz_unavailable"
    except Exception as exc:
        return None, "finviz_" + type(exc).__name__


def _get_free_float(symbol, now=None):
    """Resolve actual float shares from free/read-only sources; never estimate."""
    symbol = str(symbol or "").strip().upper()
    cache_key = "free_float:" + symbol
    current = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > current:
            return cached[1]

    massive_key = (
        os.environ.get("MASSIVE_API_KEY")
        or os.environ.get("POLYGON_API_KEY")
        or ""
    ).strip()
    attempts = []
    if massive_key:
        url = "https://api.massive.com/stocks/vX/float?" + urlencode({
            "ticker": symbol, "apiKey": massive_key,
        })
        status, payload = _provider_json(url, massive_key, "massive")
        results = payload.get("results") if isinstance(payload, dict) else None
        row = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
        try:
            shares = int(float(row.get("free_float")))
        except (TypeError, ValueError, OverflowError):
            shares = 0
        effective = _parse_timestamp(row.get("effective_date")) if row.get("effective_date") else None
        age_days = (datetime.now(timezone.utc).date() - effective.date()).days if effective else None
        if status == 200 and shares > 0 and age_days is not None and -1 <= age_days <= 365:
            result = {
                "float_shares": shares, "provider": "massive",
                "effective_date": effective.date().isoformat(), "status": "ok",
            }
            with _CACHE_LOCK:
                _CACHE[cache_key] = (current + 21600, result)
                _DIAGNOSTICS["float"] = {"status": "ok", "provider": "massive", "effective_date": result["effective_date"]}
            return result
        attempts.append("massive_no_valid_float" if status == 200 else "massive_http_" + str(status))

    shares, yahoo_error = _get_yahoo_float(symbol)
    if shares:
        result = {
            "float_shares": shares, "provider": "yahoo_finance",
            "effective_date": None, "status": "ok",
        }
        with _CACHE_LOCK:
            _CACHE[cache_key] = (current + 21600, result)
            _DIAGNOSTICS["float"] = {"status": "ok", "provider": "yahoo_finance"}
        return result

    attempts.append(yahoo_error or "yahoo_float_unavailable")

    shares, finviz_error = _get_finviz_float(symbol)
    if shares:
        result = {
            "float_shares": shares, "provider": "finviz",
            "effective_date": None, "status": "ok",
        }
        with _CACHE_LOCK:
            _CACHE[cache_key] = (current + 21600, result)
            _DIAGNOSTICS["float"] = {"status": "ok", "provider": "finviz"}
        return result
    attempts.append(finviz_error or "finviz_float_unavailable")

    result = {
        "float_shares": None, "provider": None,
        "status": "unavailable",
        "attempts": attempts,
    }
    with _CACHE_LOCK:
        # Avoid repeatedly querying Yahoo when its public endpoint is unavailable.
        _CACHE[cache_key] = (current + 1800, result)
        _DIAGNOSTICS["float"] = dict(result)
    return result


def get_scanner_context(symbol, now=None, metadata=None):
    """Fetch fresh catalyst headlines and actual free-float shares without paid endpoints.

    Twelve Data press releases are available on Basic. Float lookup uses Massive when
    configured, then Yahoo Finance's explicit floatShares field as a no-paid-plan fallback.
    Twelve Data's statistics endpoint is Pro-only, so it is not called on this free deployment.
    Missing float is reported, never guessed or replaced with shares outstanding.
    """
    meta = metadata or get_symbol_metadata(symbol)
    params = _request_params_for_symbol(symbol, meta)
    float_result = _get_free_float(symbol, now)
    float_shares = float_result.get("float_shares")

    news_params = dict(params)
    news_params["outputsize"] = 10
    news_key = "press_releases:" + str(symbol).upper() + ":" + str(news_params.get("exchange", "")) + ":" + str(news_params.get("country", ""))
    news_status, news_payload = _request_json("press_releases", news_params, news_key, 1800)
    news_rows = []
    if news_status == 200 and isinstance(news_payload, dict):
        rows = news_payload.get("data") or news_payload.get("values") or news_payload.get("press_releases") or []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                headline = row.get("title") or row.get("headline") or row.get("name")
                published = row.get("published_at") or row.get("publishedDate") or row.get("datetime") or row.get("date")
                stamp = _parse_timestamp(published)
                if isinstance(headline, str) and stamp:
                    news_rows.append({"published_at": stamp.isoformat(), "headline": headline[:500], "provider": "twelve_data"})

    # Keep only headlines within JeanLong's configured 16-hour freshness window
    # before deciding whether a fallback source is needed. Old press releases must
    # not suppress a search for genuinely fresh news.
    current_time = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)
    def _fresh_rows(rows):
        fresh = []
        for row in rows:
            stamp = _parse_timestamp(row.get("published_at"))
            if stamp is None:
                continue
            age_hours = (current_time - stamp).total_seconds() / 3600
            if 0 <= age_hours <= 16:
                fresh.append(row)
        return fresh
    news_rows = _fresh_rows(news_rows)

    # If the press-release feed has no usable rows, use Massive's public company-news
    # endpoint only when a Massive/Polygon key already exists in the environment.
    massive_key = (os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY") or "").strip()
    if not news_rows and massive_key:
        news_url = "https://api.massive.com/v2/reference/news?" + urlencode({
            "ticker": str(symbol).upper(), "limit": 10, "sort": "published_utc.desc",
            "apiKey": massive_key,
        })
        news_http, news_data = _provider_json(news_url, massive_key, "massive")
        rows = news_data.get("results") if isinstance(news_data, dict) else None
        if news_http == 200 and isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                headline = row.get("title")
                published = row.get("published_utc") or row.get("published_at")
                stamp = _parse_timestamp(published)
                if isinstance(headline, str) and stamp:
                    news_rows.append({"published_at": stamp.isoformat(), "headline": headline[:500], "provider": "massive"})

    news_rows = _fresh_rows(news_rows)

    # Last-resort no-key news feed: Yahoo Finance search exposes recent headlines
    # without a paid news endpoint. Treat it as a headline source only; JeanLong still
    # applies its own freshness and catalyst-keyword checks.
    if not news_rows:
        yahoo_url = "https://query1.finance.yahoo.com/v1/finance/search?" + urlencode({
            "q": str(symbol).upper(), "newsCount": 10, "quotesCount": 0,
            "enableFuzzyQuery": "false",
        })
        yahoo_status, yahoo_payload = _provider_json(yahoo_url, "", "yahoo")
        yahoo_items = yahoo_payload.get("news") if isinstance(yahoo_payload, dict) else None
        if yahoo_status == 200 and isinstance(yahoo_items, list):
            for item in yahoo_items:
                if not isinstance(item, dict):
                    continue
                # Yahoo's search endpoint sometimes omits the queried ticker from
                # relatedTickers even when the headline is returned for an exact-symbol
                # query. Do not discard those headlines solely on that optional field;
                # JeanLong still requires a fresh timestamp and a recognized catalyst.
                headline = item.get("title")
                stamp = _parse_timestamp(item.get("providerPublishTime") or item.get("published_at"))
                if isinstance(headline, str) and headline.strip() and stamp:
                    news_rows.append({
                        "published_at": stamp.isoformat(),
                        "headline": headline[:500],
                        "provider": "yahoo_finance",
                    })

    news_rows = _fresh_rows(news_rows)

    # Final free fallback: Google News RSS can surface recent small-cap catalyst
    # coverage when provider press releases and Yahoo Finance search are empty.
    # Headlines are evidence only; JeanLong still applies its freshness and
    # catalyst-keyword rules before any paper entry is allowed.
    if not news_rows:
        rss_url = "https://news.google.com/rss/search?" + urlencode({
            "q": '"' + str(symbol).upper() + '" stock',
            "hl": "en-US", "gl": "US", "ceid": "US:en",
        })
        try:
            request = Request(rss_url, headers={
                "Accept": "application/rss+xml, application/xml, text/xml",
                "User-Agent": "Mozilla/5.0 (compatible; Tradeboot/1.0; +https://tradeboot-api.onrender.com)",
            }, method="GET")
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                rss_root = ET.fromstring(response.read())
            for item in rss_root.findall(".//item")[:10]:
                headline = item.findtext("title")
                published = item.findtext("pubDate")
                try:
                    stamp = parsedate_to_datetime(published) if published else None
                    if stamp and stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    stamp = None
                if isinstance(headline, str) and headline.strip() and stamp:
                    news_rows.append({
                        "published_at": stamp.astimezone(timezone.utc).isoformat(),
                        "headline": headline[:500],
                        "provider": "google_news_rss",
                    })
        except (HTTPError, URLError, TimeoutError, OSError, ET.ParseError, UnicodeError):
            pass
        news_rows = _fresh_rows(news_rows)

    # Most recent first; deduplicate provider overlaps.
    deduped = {}
    for row in news_rows:
        key = (row["published_at"], row["headline"].strip().lower())
        deduped[key] = row
    news_rows = sorted(deduped.values(), key=lambda row: row["published_at"], reverse=True)[:10]
    diagnostic = {
        "status": "ok" if news_rows else ("press_releases_unavailable" if news_status != 200 else "no_fresh_headlines"),
        "news_provider": news_rows[0].get("provider") if news_rows else None,
        "news_count": len(news_rows),
        "float_available": float_shares is not None,
        "float_provider": float_result.get("provider"),
        "float_status": float_result.get("status"),
        "float_effective_date": float_result.get("effective_date"),
    }
    return {
        "provider": "twelve_data", "float_shares": float_shares, "news": news_rows,
        "diagnostic": diagnostic,
    }
