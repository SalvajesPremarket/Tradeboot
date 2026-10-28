"""US-listed stock discovery and read-only market-data adapter for Tradeboot.

Free-plan discovery uses Schwab's NASDAQ and NYSE market-movers feed. Twelve Data's
paid market-movers endpoint is deliberately disabled to keep the deployment at $0
and prevent international listings from entering the automatic scanner.
"""
import json
import os
import threading
import time
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
    global _COUNTRY_CURSOR
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
            with _CACHE_LOCK:
                for item in candidates:
                    _SYMBOL_META[item["symbol"]] = item
                _DIAGNOSTICS["market_movers/stocks"] = {
                    "status": "ok" if candidates else ("schwab_unavailable" if failures else "no_usable_candidates"),
                    "provider": "schwab",
                    "detail": "us_exchange_movers_fallback",
                    "candidate_count": len(candidates),
                    "twelve_data_status": "skipped_plan_basic",
                    "schwab_errors": failures[:2],
                }
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


def _get_free_float(symbol, now=None):
    """Resolve actual free-float shares from a configured free-tier provider.

    Prefer Massive's free Stocks float endpoint, then try FMP when configured.
    Never substitute shares outstanding or an estimated dollar float for float shares.
    Results are cached to stay within free API quotas.
    """
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
    fmp_key = (
        os.environ.get("FMP_API_KEY")
        or os.environ.get("FINANCIAL_MODELING_PREP_API_KEY")
        or ""
    ).strip()
    sources = []
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
                "effective_date": effective.date().isoformat(),
                "status": "ok",
            }
            with _CACHE_LOCK:
                _CACHE[cache_key] = (current + 21600, result)
                _DIAGNOSTICS["float"] = {
                    "status": "ok", "provider": "massive",
                    "effective_date": result["effective_date"],
                }
            return result
        sources.append("massive_no_valid_float" if status == 200 else "massive_http_" + str(status))

    if fmp_key:
        url = "https://financialmodelingprep.com/stable/shares-float?" + urlencode({
            "symbol": symbol, "apikey": fmp_key,
        })
        status, payload = _provider_json(url, fmp_key, "fmp")
        rows = payload if isinstance(payload, list) else []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        try:
            shares = int(float(row.get("floatShares")))
        except (TypeError, ValueError, OverflowError):
            shares = 0
        if status == 200 and shares > 0:
            result = {
                "float_shares": shares, "provider": "fmp",
                "effective_date": str(row.get("date") or "")[:10] or None,
                "status": "ok",
            }
            with _CACHE_LOCK:
                _CACHE[cache_key] = (current + 21600, result)
                _DIAGNOSTICS["float"] = {
                    "status": "ok", "provider": "fmp",
                    "effective_date": result["effective_date"],
                }
            return result
        sources.append("fmp_no_valid_float" if status == 200 else "fmp_http_" + str(status))

    if not massive_key and not fmp_key:
        result = {"float_shares": None, "provider": None, "status": "provider_key_missing"}
    else:
        result = {"float_shares": None, "provider": None, "status": "unavailable", "attempts": sources}
    with _CACHE_LOCK:
        _CACHE[cache_key] = (current + 600, result)
        _DIAGNOSTICS["float"] = dict(result)
    return result


def get_scanner_context(symbol, now=None, metadata=None):
    """Fetch fresh catalyst headlines and actual free-float shares without paid endpoints.

    Twelve Data press releases are available on Basic. Float lookup uses Massive's free
    Stocks float endpoint when MASSIVE_API_KEY/POLYGON_API_KEY is configured, then FMP
    if its key is configured. Missing or stale float is reported, never guessed.
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
                related = item.get("relatedTickers")
                if isinstance(related, list) and related and str(symbol).upper() not in [str(x).upper() for x in related]:
                    continue
                headline = item.get("title")
                stamp = _parse_timestamp(item.get("providerPublishTime") or item.get("published_at"))
                if isinstance(headline, str) and stamp:
                    news_rows.append({
                        "published_at": stamp.isoformat(),
                        "headline": headline[:500],
                        "provider": "yahoo_finance",
                    })

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
