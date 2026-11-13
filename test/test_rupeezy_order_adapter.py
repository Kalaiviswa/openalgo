"""Rupeezy (Vortex) order-update adapter: postback text frames on the feed socket.

Payload shape from broker-api-docs/vortex-api-docs/19-postbacks.md; the feed
carries the same structure in text mode (18-feed.md "Order Messages").
"""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

importlib.import_module("websocket_proxy")
adapter_mod = importlib.import_module("broker.rupeezy.streaming.rupeezy_order_adapter")
order_data = importlib.import_module("broker.rupeezy.mapping.order_data")


def _postback(msg_type="order", **overrides):
    data = {
        "order_id": "NXAAE0001AC4",
        "order_number": "1100000014117098",
        "status": "COMPLETED",
        "status_message": "",
        "symbol": "ITC",
        "ticker": "NSE:ITC",
        "token": 1660,
        "exchange": "NSE_EQ",
        "transaction_type": "BUY",
        "product": "INTRADAY",
        "variety": "RL",
        "total_quantity": 1,
        "pending_quantity": 0,
        "traded_quantity": 1,
        "order_price": 400.2,
        "trigger_price": 0,
        "traded_price": 400.2,
    }
    data.update(overrides)
    return json.dumps({"type": msg_type, "data": data, "client_code": "DEMO"})


@pytest.fixture
def adapter():
    with (
        patch.object(order_data, "get_oa_symbol", side_effect=lambda brsymbol, exchange: brsymbol),
        patch.object(order_data, "get_symbol_info", return_value=None),
    ):
        yield adapter_mod.RupeezyOrderUpdateAdapter(user_id="trader", access_token="tok")


def test_url_carries_the_token():
    a = adapter_mod.RupeezyOrderUpdateAdapter(user_id="trader", access_token="tok")
    assert a.get_ws_url() == "wss://wire.rupeezy.in/ws?auth_token=tok"
    assert a.get_headers() is None


def test_completed_order_postback(adapter):
    fields = adapter.normalize(_postback())
    assert fields == {
        "orderid": "NXAAE0001AC4",
        "symbol": "ITC",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 1,
        "price": 400.2,
        "trigger_price": 0.0,
        "pricetype": "LIMIT",
        "product": "MIS",
        "order_status": "complete",
        "filled_quantity": 1,
        "pending_quantity": 0,
        "average_price": 400.2,
        "rejection_reason": "",
    }


def test_rejection_carries_the_reason(adapter):
    fields = adapter.normalize(
        _postback(
            status="REJECTED",
            status_message="RMS: margin exceeds",
            traded_quantity=0,
            pending_quantity=0,
            traded_price=0,
        )
    )
    assert fields["order_status"] == "rejected"
    assert fields["rejection_reason"] == "RMS: margin exceeds"


def test_stop_loss_on_derivatives(adapter):
    fields = adapter.normalize(
        _postback(
            msg_type="sl_trigger",
            status="TRIGGER PENDING",
            ticker="NSE:NIFTY26OCT25000CE",
            exchange="NSE_FO",
            product="DELIVERY",
            variety="SL-MKT",
            trigger_price=120.5,
            traded_quantity=0,
            pending_quantity=75,
            total_quantity=75,
        )
    )
    assert fields["exchange"] == "NFO"
    assert fields["symbol"] == "NIFTY26OCT25000CE"
    assert fields["product"] == "NRML"
    assert fields["pricetype"] == "SL-M"
    assert fields["order_status"] == "trigger pending"
    assert fields["pending_quantity"] == 75


def test_mcx_quantities_are_converted_to_units(adapter):
    with patch.object(order_data, "get_symbol_info", return_value=SimpleNamespace(lotsize=100)):
        fields = adapter.normalize(
            _postback(
                ticker="MCX:CRUDEOIL28MARFUT",
                exchange="MCX_FO",
                product="DELIVERY",
                total_quantity=2,
                traded_quantity=1,
                pending_quantity=1,
            )
        )
    assert fields["exchange"] == "MCX"
    assert (fields["quantity"], fields["filled_quantity"], fields["pending_quantity"]) == (
        200,
        100,
        100,
    )


@pytest.mark.parametrize(
    "message",
    [
        b"\x01\x00",  # binary price frame
        "not json",
        json.dumps({"type": "gtt_order", "data": {"order_id": "X"}}),
        json.dumps({"type": "position_conversion", "data": {"order_id": "X"}}),
        json.dumps({"type": "order", "data": {}}),
    ],
)
def test_non_order_frames_are_ignored(adapter, message):
    assert adapter.normalize(message) is None


def test_factory_without_session_returns_none():
    with patch.object(adapter_mod, "get_auth_token", return_value=None):
        assert adapter_mod.create_rupeezy_order_adapter("trader") is None


def test_factory_with_session_builds_the_adapter():
    with patch.object(adapter_mod, "get_auth_token", return_value="tok"):
        a = adapter_mod.create_rupeezy_order_adapter("trader")
    assert isinstance(a, adapter_mod.RupeezyOrderUpdateAdapter)
    assert a.access_token == "tok"
