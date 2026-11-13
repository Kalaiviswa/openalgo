"""Firstock order-update adapter: frame normalization and factory guards.

The frames are the samples from broker-api-docs/firstock-api-docs/
45-websocket-order-update-feed-v2.md, plus Noren fill fields the doc does not
list but the adapter reads when present.
"""

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# websocket_proxy and broker/*/streaming import each other; load the proxy
# package first, as services/order_update_service.py does.
importlib.import_module("websocket_proxy")
mod = importlib.import_module("broker.firstock.streaming.firstock_order_adapter")

DOC_ACK_FRAME = {
    "tsym": "VIKASLIFE-EQ",
    "rejreason": " ",
    "prcftr": "1.000000",
    "pcode": "C",
    "trantype": "B",
    "token": "9931",
    "prctyp": "MKT",
    "ret": "DAY",
    "pp": "2",
    "ti": "0.01",
    "remarks": "",
    "ntm": "960322165",
    "kidid": "1",
    "dscqty": "0",
    "norenordno": "25042400010260",
    "mult": "1",
    "uid": "NP2997",
    "exch": "NSE",
    "status": "ORDER ACK",
    "reporttype": "NewAck",
    "tm": "1745483337",
    "handlinst": "20f217e1-daf4-4860-bc12-625d4f37d402",
    "ls": "1",
    "actid": "NP2997",
    "qty": "1",
    "prc": "0.00",
}


@pytest.fixture
def adapter():
    return mod.FirstockOrderUpdateAdapter(user_id="trader", firstock_uid="NP2997", jkey="jk")


@pytest.fixture(autouse=True)
def symbol_lookup():
    with patch.object(mod, "to_openalgo_symbol", return_value="VIKASLIFE") as m:
        yield m


def _frame(**overrides):
    frame = dict(DOC_ACK_FRAME)
    frame.update(overrides)
    return json.dumps(frame)


def test_doc_ack_frame_maps_to_openalgo_vocabulary(adapter, symbol_lookup):
    fields = adapter.normalize(_frame())
    assert fields == {
        "orderid": "25042400010260",
        "symbol": "VIKASLIFE",
        "exchange": "NSE",
        "action": "BUY",
        "pricetype": "MARKET",
        "product": "CNC",
        "order_status": "open",
        "quantity": 1,
        "price": 0.0,
    }
    symbol_lookup.assert_called_once_with("VIKASLIFE-EQ", "NSE", token="9931")


def test_fields_the_doc_does_not_list_are_left_out(adapter):
    fields = adapter.normalize(_frame(status="COMPLETE"))
    for key in ("filled_quantity", "pending_quantity", "average_price", "trigger_price"):
        assert key not in fields, f"{key} must not be invented when the broker omits it"


def test_noren_fill_fields_are_read_when_present(adapter):
    fields = adapter.normalize(
        _frame(status="COMPLETE", qty="10", fillshares="10", avgprc="101.5", trgprc="")
    )
    assert fields["order_status"] == "complete"
    assert fields["filled_quantity"] == 10
    assert fields["pending_quantity"] == 0
    assert fields["average_price"] == 101.5
    assert "trigger_price" not in fields


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ORDER ACK", "open"),
        ("ORDER PENDING", "open"),
        ("OPEN", "open"),
        ("PENDING", "open"),
        ("AMO OPEN", "open"),
        ("AMO MODIFIED", "open"),
        ("TRIGGER_PENDING", "trigger pending"),
        ("COMPLETE", "complete"),
        ("REJECTED", "rejected"),
        ("CANCELED", "cancelled"),
        ("AMO CANCELED", "cancelled"),
        ("SOMETHING NEW", "something new"),
    ],
)
def test_status_mapping(adapter, raw, expected):
    assert adapter.normalize(_frame(status=raw))["order_status"] == expected


def test_rejection_reason_only_on_rejected(adapter):
    rejected = adapter.normalize(_frame(status="REJECTED", rejreason=" RMS: margin "))
    assert rejected["rejection_reason"] == "RMS: margin"
    assert "rejection_reason" not in adapter.normalize(_frame(status="OPEN", rejreason="x"))


@pytest.mark.parametrize(
    "pcode, exch, expected",
    [("C", "NSE", "CNC"), ("I", "NFO", "MIS"), ("M", "NFO", "NRML"), ("M", "NSE", "M")],
)
def test_product_mirrors_the_order_book(adapter, pcode, exch, expected):
    assert adapter.normalize(_frame(pcode=pcode, exch=exch))["product"] == expected


@pytest.mark.parametrize(
    "prctyp, expected",
    [("LMT", "LIMIT"), ("MKT", "MARKET"), ("SL-LMT", "SL"), ("SL_MKT", "SL-M")],
)
def test_pricetype(adapter, prctyp, expected):
    assert adapter.normalize(_frame(prctyp=prctyp))["pricetype"] == expected


def test_sell_side(adapter):
    assert adapter.normalize(_frame(trantype="S"))["action"] == "SELL"


@pytest.mark.parametrize(
    "frame",
    [
        {"status": "success", "message": "Authentication successful"},
        {"brkname": "FIRSTOCK", "netqty": "1", "child_orders": []},
        {"NSE:26000": {"c_symbol": "26000", "i_last_traded_price": 1}},
    ],
)
def test_non_order_frames_are_ignored(adapter, frame):
    assert adapter.normalize(json.dumps(frame)) is None
    assert adapter._auth_rejected is False


def test_garbage_is_ignored(adapter):
    assert adapter.normalize("not json") is None
    assert adapter.normalize(json.dumps([1, 2])) is None


def test_auth_failure_stands_the_adapter_down(adapter):
    ws = MagicMock()
    adapter._ws = ws
    assert adapter.normalize(json.dumps({"status": "failed", "message": "unauthenticated"})) is None
    assert adapter._auth_rejected is True
    ws.close.assert_called_once()


def test_ws_url_carries_documented_query_and_fresh_token(adapter):
    with patch.object(mod, "get_auth_token", return_value="fresh-token"):
        url = adapter.get_ws_url()
    assert url.startswith("wss://socket.firstock.in/V2/ws?")
    assert "userId=NP2997" in url
    assert "jKey=fresh-token" in url
    assert "source=developer-api" in url


def test_ws_url_keeps_last_token_when_reread_fails(adapter):
    with patch.object(mod, "get_auth_token", return_value=None):
        assert "jKey=jk" in adapter.get_ws_url()


def test_factory_returns_none_without_session():
    with patch.object(mod, "get_auth_token", return_value=None):
        assert mod.create_firstock_order_adapter("trader") is None


def test_factory_returns_none_without_broker_api_key(monkeypatch):
    monkeypatch.setenv("BROKER_API_KEY", "")
    with patch.object(mod, "get_auth_token", return_value="jk"):
        assert mod.create_firstock_order_adapter("trader") is None


def test_factory_builds_adapter_with_order_api_user_id(monkeypatch):
    monkeypatch.setenv("BROKER_API_KEY", "NP2997_API")
    with patch.object(mod, "get_auth_token", return_value="jk"):
        adapter = mod.create_firstock_order_adapter("trader")
    assert isinstance(adapter, mod.FirstockOrderUpdateAdapter)
    assert adapter.firstock_uid == "NP2997"
    assert adapter.broker_name == "firstock"
