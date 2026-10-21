import json
from urllib.error import HTTPError

import pytest

import etoro_execution as execution


def configure_demo(monkeypatch):
    monkeypatch.setenv("ETORO_API_KEY", "public-key")
    monkeypatch.setenv("ETORO_USER_KEY", "user-key")
    monkeypatch.setenv("ETORO_TRADING_ENABLED", "true")
    monkeypatch.setenv("ETORO_TRADING_MODE", "demo")
    monkeypatch.setenv("ETORO_MAX_ORDER_AMOUNT_USD", "100")


def test_execution_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ETORO_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("ETORO_TRADING_MODE", raising=False)
    with pytest.raises(execution.EtoroExecutionError, match="trading_mode_disabled"):
        execution.open_market_order(123, 10, True)


def test_open_order_uses_demo_path_and_caps_amount(monkeypatch):
    configure_demo(monkeypatch)
    seen = {}

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, _):
            return b'{"orderId":42}'

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["body"] = json.loads(request.data.decode())
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(execution, "urlopen", fake_urlopen)
    result = execution.open_market_order(123, 25, True, stop_loss_rate=9.5)
    assert result["ok"] is True
    assert "/trading/execution/demo/market-open-orders/by-amount" in seen["url"]
    assert seen["method"] == "POST"
    assert seen["body"] == {
        "InstrumentId": 123, "Amount": 25.0, "Leverage": 1,
        "IsBuy": True, "StopLossRate": 9.5,
    }


def test_order_above_cap_is_rejected(monkeypatch):
    configure_demo(monkeypatch)
    with pytest.raises(execution.EtoroExecutionError, match="order_amount_outside_configured_limit"):
        execution.open_market_order(123, 101, True)


def test_live_mode_requires_explicit_acknowledgement(monkeypatch):
    configure_demo(monkeypatch)
    monkeypatch.setenv("ETORO_TRADING_MODE", "real")
    monkeypatch.delenv("ETORO_LIVE_TRADING_ACK", raising=False)
    with pytest.raises(execution.EtoroExecutionError, match="live_trading_acknowledgement_required"):
        execution.open_market_order(123, 10, True)


def test_close_uses_position_id_and_demo_path(monkeypatch):
    configure_demo(monkeypatch)
    seen = {}

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, _):
            return b'{"accepted":true}'

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setattr(execution, "urlopen", fake_urlopen)
    result = execution.close_market_position(4567)
    assert result["ok"] is True
    assert seen["url"].endswith("/trading/execution/demo/market-close-orders/positions/4567")
    assert seen["body"] == {"UnitsToDeduct": None}
