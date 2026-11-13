"""Order-update adapters for the XTS (Symphony Fintech) brokers.

All seven speak the XTS Interactive socket (Socket.IO 2.x, Engine.IO v3) and
share the same adapter shape, one copy per broker under broker/<name>/. These
tests pin the wire format (URL, framing, the session-refusal frame observed on
the live hosts) and the mapping of the "order" event to OpenAlgo's vocabulary.
"""

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# websocket_proxy must load before broker/*/streaming (see order_update_service).
importlib.import_module("websocket_proxy")

from websocket_proxy.order_adapter import _is_auth_rejection  # noqa: E402

BROKERS = {
    "compositedge": "CompositedgeOrderUpdateAdapter",
    "fivepaisaxts": "FivepaisaxtsOrderUpdateAdapter",
    "ibulls": "IbullsOrderUpdateAdapter",
    "iifl": "IiflOrderUpdateAdapter",
    "jainamxts": "JainamxtsOrderUpdateAdapter",
    "wisdom": "WisdomOrderUpdateAdapter",
    "rmoney": "RmoneyOrderUpdateAdapter",
}


def _module(broker):
    return importlib.import_module(f"broker.{broker}.streaming.{broker}_order_adapter")


def _adapter(broker, xts_user_id="SYMP1"):
    mod = _module(broker)
    cls = getattr(mod, BROKERS[broker])
    return cls(user_id="admin", interactive_token="tok.en", xts_user_id=xts_user_id)


def _event(name, payload, as_string=True):
    """A Socket.IO v2 event frame as XTS sends it: data JSON-encoded as a string."""
    data = json.dumps(payload) if as_string else payload
    return "42" + json.dumps([name, data])


# The order event example from the XTS Interactive doc ("Socket Streaming" ->
# "Orders"), plus the OrderStopPrice/TradingSymbol fields the orderbook carries.
DOC_ORDER = {
    "LoginID": "SYMP1",
    "ClientID": "SYMP1",
    "AppOrderID": 648468730,
    "OrderReferenceID": "",
    "GeneratedBy": "WSAPI",
    "ExchangeOrderID": "1005239196374108",
    "OrderCategoryType": "NORMAL",
    "ExchangeSegment": "NSECM",
    "ExchangeInstrumentID": 16921,
    "OrderSide": "BUY",
    "OrderType": "LIMIT",
    "ProductType": "NRML",
    "TimeInForce": "DAY",
    "OrderPrice": 0,
    "OrderQuantity": 1,
    "OrderStatus": "New",
    "OrderAverageTradedPrice": 2615.8,
    "LeavesQuantity": 1,
    "CumulativeQuantity": 0,
    "OrderDisclosedQuantity": 0,
    "CancelRejectReason": "",
    "OrderUniqueIdentifier": "454845",
    "MessageCode": 4,
}


@pytest.mark.parametrize("broker", BROKERS)
class TestXtsOrderAdapter:
    def test_url_follows_the_interactive_socket_doc(self, broker):
        url = urlparse(_adapter(broker).get_ws_url())
        assert url.scheme == "wss"
        assert url.path == "/interactive/socket.io/"
        query = parse_qs(url.query)
        assert query["token"] == ["tok.en"]
        assert query["userID"] == ["SYMP1"]
        assert query["apiType"] == ["INTERACTIVE"]
        assert query["EIO"] == ["3"]
        assert query["transport"] == ["websocket"]

    def test_client_pings_engineio_v3(self, broker):
        a = _adapter(broker)
        ws = MagicMock()
        assert 0 < a.heartbeat_interval() < 25
        a.send_heartbeat(ws)
        ws.send.assert_called_once_with("2")

    def test_order_event_maps_to_openalgo_vocabulary(self, broker):
        a = _adapter(broker)
        mod = _module(broker)
        with patch.object(mod, "to_openalgo_symbol", return_value="RELIANCE") as sym:
            fields = a.normalize(_event("order", DOC_ORDER))
        sym.assert_called_once_with("", "NSE", token="16921")
        assert fields == {
            "orderid": "648468730",
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "action": "BUY",
            "quantity": 1,
            "price": 0.0,
            "trigger_price": 0.0,
            "pricetype": "LIMIT",
            "product": "NRML",
            "order_status": "open",
            "filled_quantity": 0,
            "pending_quantity": 1,
            "average_price": 2615.8,
            "rejection_reason": "",
        }

    def test_payload_already_decoded_is_accepted(self, broker):
        a = _adapter(broker)
        with patch.object(_module(broker), "to_openalgo_symbol", return_value="X"):
            fields = a.normalize(_event("order", DOC_ORDER, as_string=False))
        assert fields["orderid"] == "648468730"

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("New", "open"),
            ("PendingNew", "open"),
            ("Open", "open"),
            ("PartiallyFilled", "open"),
            ("Replaced", "open"),
            ("PendingReplace", "open"),
            ("PendingCancel", "open"),
            ("Filled", "complete"),
            ("Rejected", "rejected"),
            ("Cancelled", "cancelled"),
        ],
    )
    def test_status_map(self, broker, status, expected):
        a = _adapter(broker)
        with patch.object(_module(broker), "to_openalgo_symbol", return_value="X"):
            fields = a.normalize(_event("order", {**DOC_ORDER, "OrderStatus": status}))
        assert fields["order_status"] == expected

    def test_stop_order_fill_and_segments(self, broker):
        order = {
            **DOC_ORDER,
            "ExchangeSegment": "NSEFO",
            "OrderSide": "SELL",
            "OrderType": "StopLimit",
            "ProductType": "MIS",
            "OrderPrice": "101.5",
            "OrderStopPrice": 100,
            "OrderQuantity": 75,
            "CumulativeQuantity": 75,
            "LeavesQuantity": 0,
            "OrderAverageTradedPrice": "101.45",
            "OrderStatus": "Filled",
            "TradingSymbol": "NIFTY 25NOV2025 CE 26000",
        }
        a = _adapter(broker)
        with patch.object(
            _module(broker), "to_openalgo_symbol", return_value="NIFTY25NOV2526000CE"
        ):
            fields = a.normalize(_event("order", order))
        assert fields["exchange"] == "NFO"
        assert fields["action"] == "SELL"
        assert fields["pricetype"] == "SL"
        assert fields["product"] == "MIS"
        assert fields["price"] == 101.5
        assert fields["trigger_price"] == 100.0
        assert fields["filled_quantity"] == 75
        assert fields["pending_quantity"] == 0
        assert fields["average_price"] == 101.45
        assert fields["order_status"] == "complete"

    def test_rejection_reason_only_on_rejected(self, broker):
        a = _adapter(broker)
        mod = _module(broker)
        with patch.object(mod, "to_openalgo_symbol", return_value="X"):
            rejected = a.normalize(
                _event(
                    "order",
                    {**DOC_ORDER, "OrderStatus": "Rejected", "CancelRejectReason": "RMS: margin"},
                )
            )
            cancelled = a.normalize(
                _event(
                    "order",
                    {**DOC_ORDER, "OrderStatus": "Cancelled", "CancelRejectReason": "user"},
                )
            )
        assert rejected["rejection_reason"] == "RMS: margin"
        assert cancelled["rejection_reason"] == ""

    def test_empty_broker_fields_are_not_filled_in(self, broker):
        order = {"AppOrderID": "5", "ExchangeSegment": "BSECD", "OrderType": "", "OrderStatus": ""}
        a = _adapter(broker)
        with patch.object(_module(broker), "to_openalgo_symbol", return_value=""):
            fields = a.normalize(_event("order", order))
        assert fields["exchange"] == "BSECD"  # unmapped segment passes through
        assert fields["pricetype"] == ""
        assert fields["product"] == ""
        assert fields["order_status"] == ""
        assert fields["average_price"] == 0.0

    @pytest.mark.parametrize(
        "frame",
        [
            '0{"sid":"abc","upgrades":[],"pingInterval":25000,"pingTimeout":20000}',
            "40",
            "3",
            _event("joined", "Socket joined"),
            _event("trade", DOC_ORDER),
            _event("position", {"x": 1}),
            _event("order", "not json"),
            _event("order", {"OrderStatus": "New"}),  # no AppOrderID
            "42not json",
        ],
    )
    def test_non_order_frames_are_ignored(self, broker, frame):
        a = _adapter(broker)
        assert a.normalize(frame) is None
        assert a._auth_rejected is False

    def test_session_refusal_stands_down(self, broker):
        # The exact frame every reachable XTS host sent for a dead token.
        frame = (
            '42["socketError","{\\"type\\":\\"error\\",\\"code\\":\\"e-socket-0003\\",'
            '\\"description\\":\\"Your session has been expired\\"}"]'
        )
        a = _adapter(broker)
        ws = MagicMock()
        a._ws = ws
        assert a.normalize(frame) is None
        assert a._auth_rejected is True
        ws.close.assert_called_once()

    def test_logout_stands_down(self, broker):
        a = _adapter(broker)
        a._ws = MagicMock()
        assert a.normalize(_event("logout", "")) is None
        assert a._auth_rejected is True

    def test_other_socket_error_reconnects(self, broker):
        a = _adapter(broker)
        ws = MagicMock()
        a._ws = ws
        a.normalize(_event("socketError", {"type": "error", "code": "e-x", "description": "busy"}))
        assert a._auth_rejected is False
        ws.close.assert_called_once()

    def test_handle_message_publishes_order_update_event(self, broker):
        a = _adapter(broker)
        with (
            patch.object(_module(broker), "to_openalgo_symbol", return_value="RELIANCE"),
            patch("websocket_proxy.order_adapter.bus") as bus,
        ):
            a._handle_message(_event("order", {**DOC_ORDER, "OrderStatus": "Filled"}))
        event = bus.publish.call_args.args[0]
        assert event.topic == "order.update"
        assert event.broker == broker
        assert event.mode == "live"
        assert event.orderid == "648468730"
        assert event.order_status == "complete"
        assert event.api_type == f"{broker}.order_update"

    def test_user_id_from_profile_when_login_stored_none(self, broker):
        a = _adapter(broker, xts_user_id=None)
        response = MagicMock(status_code=200)
        response.json.return_value = {"type": "success", "result": {"ClientId": "AB123"}}
        client = MagicMock()
        client.get.return_value = response
        with patch.object(_module(broker), "get_httpx_client", return_value=client):
            url = a.get_ws_url()
            a.get_ws_url()  # cached: one profile call
        assert parse_qs(urlparse(url).query)["userID"] == ["AB123"]
        assert client.get.call_count == 1
        called_url = client.get.call_args.args[0]
        assert called_url.endswith("/interactive/user/profile")
        assert client.get.call_args.kwargs["headers"]["authorization"] == "tok.en"
        assert client.get.call_args.kwargs["timeout"]

    def test_profile_token_refusal_is_an_auth_rejection(self, broker):
        a = _adapter(broker, xts_user_id=None)
        response = MagicMock(status_code=400)
        response.json.return_value = {
            "type": "error",
            "code": "e-token-0001",
            "description": "Invalid Token",
        }
        client = MagicMock()
        client.get.return_value = response
        mod = _module(broker)
        with patch.object(mod, "get_httpx_client", return_value=client):
            with pytest.raises(mod.XtsSessionRejected) as exc:
                a.get_ws_url()
        assert _is_auth_rejection(exc.value)

    def test_profile_outage_is_retried_not_rejected(self, broker):
        a = _adapter(broker, xts_user_id=None)
        response = MagicMock(status_code=502)
        response.json.side_effect = ValueError("not json")
        client = MagicMock()
        client.get.return_value = response
        with patch.object(_module(broker), "get_httpx_client", return_value=client):
            with pytest.raises(RuntimeError) as exc:
                a.get_ws_url()
        assert not _is_auth_rejection(exc.value)

    def test_factory_without_session_returns_none(self, broker):
        mod = _module(broker)
        with (
            patch.object(mod, "get_auth_token", return_value=None),
            patch.object(mod, "get_user_id", return_value=None),
        ):
            assert getattr(mod, f"create_{broker}_order_adapter")("admin") is None

    def test_factory_with_session(self, broker):
        mod = _module(broker)
        with (
            patch.object(mod, "get_auth_token", return_value="tok.en") as tok,
            patch.object(mod, "get_user_id", return_value="CL01"),
        ):
            adapter = getattr(mod, f"create_{broker}_order_adapter")("admin")
        tok.assert_called_once_with("admin", bypass_cache=True)
        assert isinstance(adapter, getattr(mod, BROKERS[broker]))
        assert adapter.broker_name == broker
        assert adapter.xts_user_id == "CL01"
        assert adapter.interactive_token == "tok.en"

    def test_factory_leaves_user_id_unresolved_when_not_stored(self, broker):
        mod = _module(broker)
        with (
            patch.object(mod, "get_auth_token", return_value="tok.en"),
            patch.object(mod, "get_user_id", return_value=None),
        ):
            adapter = getattr(mod, f"create_{broker}_order_adapter")("admin")
        assert adapter.xts_user_id is None
