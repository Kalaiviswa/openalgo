"""Pocketful order-update adapter: mode-50 packets on the feed's "updates" channel.

Packet framing from the official SDK (pocketful 0.1.2, packetDecoder.
decodeOrderUpdate: byte 0 is the mode, JSON from byte 5); payload fields from
https://api.pocketful.in/docs/curl/order-updates.
"""

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest

importlib.import_module("websocket_proxy")
adapter_mod = importlib.import_module("broker.pocketful.streaming.pocketful_order_adapter")


def _order(**overrides):
    order = {
        "average_price": 0,
        "average_trade_price": 0,
        "client_id": "SB3179",
        "exchange": "NSE",
        "exchange_order_id": "1200000008791186",
        "filled_quantity": 0,
        "instrument_token": 10666,
        "mode": "MODIFY",
        "oms_order_id": "20220103-1157",
        "order_entry_time": 1641191331,
        "order_side": "BUY",
        "order_status": "MODCONFIRMED",
        "order_type": "LIMIT",
        "price": 36.95,
        "product": "MIS",
        "quantity": 1,
        "rejection_code": "NONE",
        "rejection_reason": "NONE",
        "remaining_quantity": 1,
        "trading_symbol": "PNB-EQ",
        "trigger_price": 0,
    }
    order.update(overrides)
    return order


def _packet(body, mode=50):
    return bytes([mode]) + b"\x00\x00\x00\x00" + json.dumps(body).encode("utf-8")


@pytest.fixture
def adapter():
    with patch.object(
        adapter_mod, "to_openalgo_symbol", side_effect=lambda s, e: s.replace("-EQ", "")
    ):
        yield adapter_mod.PocketfulOrderUpdateAdapter(
            user_id="trader", client_id="SB3179", access_token="tok"
        )


def test_url_and_subscription_follow_the_sdk(adapter):
    assert adapter.get_ws_url() == (
        "wss://trade.pocketful.in/ws/v1/feeds?login_id=SB3179&access_token=tok"
    )
    ws = MagicMock()
    adapter.on_open_extra(ws)
    assert json.loads(ws.send.call_args[0][0]) == {
        "a": "subscribe",
        "v": ["SB3179", "web"],
        "m": "updates",
    }
    adapter.send_heartbeat(ws)
    assert json.loads(ws.send.call_args[0][0]) == {"a": "h", "v": [], "m": ""}


def test_documented_sample_modify_is_open(adapter):
    fields = adapter.normalize(_packet(_order()))
    assert fields == {
        "orderid": "20220103-1157",
        "symbol": "PNB",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 1,
        "price": 36.95,
        "trigger_price": 0.0,
        "pricetype": "LIMIT",
        "product": "MIS",
        "order_status": "open",
        "filled_quantity": 0,
        "pending_quantity": 1,
        "average_price": 0.0,
        "rejection_reason": "",
    }


def test_wrapped_result_list_publishes_every_order(adapter):
    body = {
        "error": {"code": 0, "message": ""},
        "result": [
            _order(
                order_status="COMPLETE", filled_quantity=1, remaining_quantity=0, average_price=36.9
            ),
            _order(
                oms_order_id="20220103-1158",
                order_status="REJECTED",
                rejection_reason="RMS: insufficient funds",
                order_side="SELL",
            ),
        ],
    }
    published = []
    adapter._publish_event_fields = published.append
    adapter._handle_message(_packet(body))

    assert [p["orderid"] for p in published] == ["20220103-1157", "20220103-1158"]
    assert published[0]["order_status"] == "complete"
    assert published[0]["average_price"] == 36.9
    assert published[1]["order_status"] == "rejected"
    assert published[1]["action"] == "SELL"
    assert published[1]["rejection_reason"] == "RMS: insufficient funds"


def test_cancel_confirmed(adapter):
    fields = adapter.normalize(_packet(_order(order_status="CANCEL_CONFIRMED", mode="CANCEL")))
    assert fields["order_status"] == "cancelled"
    assert fields["rejection_reason"] == ""


@pytest.mark.parametrize(
    "message",
    [
        _packet(_order(), mode=51),  # trade update
        b"\x01" + b"\x00" * 40,  # detailed market data
        b"\x32\x00",  # too short
        bytes([50]) + b"\x00" * 4 + b"not json",
        _packet({"error": {"code": 0, "message": ""}, "result": []}),
    ],
)
def test_other_frames_are_ignored(adapter, message):
    assert adapter.normalize(message) is None


def test_factory_without_session_returns_none():
    with patch.object(adapter_mod, "get_auth_token", return_value=None):
        assert adapter_mod.create_pocketful_order_adapter("trader") is None


def test_factory_without_client_id_returns_none():
    with (
        patch.object(adapter_mod, "get_auth_token", return_value="tok"),
        patch.object(adapter_mod, "get_user_id", return_value=None),
    ):
        assert adapter_mod.create_pocketful_order_adapter("trader") is None


def test_factory_with_session_builds_the_adapter():
    with (
        patch.object(adapter_mod, "get_auth_token", return_value="tok"),
        patch.object(adapter_mod, "get_user_id", return_value="SB3179"),
    ):
        a = adapter_mod.create_pocketful_order_adapter("trader")
    assert isinstance(a, adapter_mod.PocketfulOrderUpdateAdapter)
    assert (a.client_id, a.access_token) == ("SB3179", "tok")
