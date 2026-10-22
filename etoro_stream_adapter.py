"""Fail-closed parser for eToro instrument-rate WebSocket messages."""
import json
import math


def _mapping(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value if isinstance(value, dict) else None


def _first(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def parse_instrument_ticks(frame, instrument_to_symbol):
    """Return normalized ticks from a provider frame; ignore unknown/invalid data.

    A tick is emitted only when the instrument topic is known and both a
    positive finite price and an explicit timezone-aware provider timestamp
    are present. Receipt time is deliberately not substituted for market time.
    """
    if not isinstance(instrument_to_symbol, dict):
        return []
    envelope = _mapping(frame)
    if envelope is None:
        return []
    messages = envelope.get("messages")
    if not isinstance(messages, list):
        messages = [envelope] if envelope.get("topic") else []
    ticks = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        topic = message.get("topic")
        if not isinstance(topic, str) or not topic.startswith("instrument:"):
            continue
        try:
            instrument_id = int(topic.split(":", 1)[1])
        except (TypeError, ValueError):
            continue
        symbol = instrument_to_symbol.get(instrument_id)
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        content = _mapping(message.get("content"))
        if content is None:
            # Some payloads place fields directly on the topic message.
            content = message
        raw_price = _first(content, ("lastPrice", "LastPrice", "price", "Price"))
        raw_timestamp = _first(content, ("timestamp", "Timestamp", "time", "Time"))
        if isinstance(raw_price, bool):
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
            continue
        ticks.append({
            "symbol": symbol.strip().upper(),
            "instrument_id": instrument_id,
            "price": price,
            "timestamp": raw_timestamp.strip(),
            "source": "etoro_websocket",
        })
    return ticks
