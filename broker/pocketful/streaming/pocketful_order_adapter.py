"""
Pocketful order-update adapter: the "updates" channel of the Pocketful feed.

Docs: https://api.pocketful.in/docs/curl/order-updates (payload fields and
sample) and https://api.pocketful.in/docs/curl/connecting-websockets. The
pages show only the Python SDK calls (subscribe_order_update), so the wire
format below follows the official SDK, pocketful 0.1.2 on PyPI
(pocketful/wsclient.py and packetDecoder.py):

- connect: wss://trade.pocketful.in/ws/v1/feeds?login_id=<client_id>&access_token=<token>
- subscribe: {"a": "subscribe", "v": [<client_id>, "web"], "m": "updates"}
- heartbeat: {"a": "h", "v": [], "m": ""} every 8 seconds
- frames are binary; the first byte (signed) is the packet mode. Mode 50 is an
  order update, 51 a trade update; their body is UTF-8 JSON from byte 5 on.

This adapter opens its own connection and subscribes to nothing but order
updates. Pocketful does not document a per-token connection limit; OpenAlgo
already holds two feed connections on one token (the streaming adapter and
the quote socket in broker/pocketful/api/pocketfulwebsocket.py).

Fields are mapped with broker/pocketful/mapping/order_data.py, the same code
the orderbook uses.
"""

import json

from broker.pocketful.mapping.order_data import transform_order_data
from database.auth_db import get_auth_token, get_user_id
from utils.logging import get_logger
from websocket_proxy.order_adapter import BaseOrderUpdateAdapter, to_openalgo_symbol

logger = get_logger(__name__)

POCKETFUL_FEED_URL = "wss://trade.pocketful.in/ws/v1/feeds"
_ORDER_UPDATE_MODE = 50
_JSON_OFFSET = 5  # packetDecoder.decodeOrderUpdate: json.loads(packet[5:])
_HEARTBEAT = json.dumps({"a": "h", "v": [], "m": ""})


def _order_records(body):
    """The order dicts in one decoded update.

    The SDK hands the decoded JSON over as is; the docs show it wrapped as
    {"error": {...}, "result": [order, ...]}. Both shapes are accepted.
    """
    if isinstance(body, dict) and isinstance(body.get("result"), list):
        return [r for r in body["result"] if isinstance(r, dict)]
    if isinstance(body, dict):
        return [body]
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    return []


class PocketfulOrderUpdateAdapter(BaseOrderUpdateAdapter):
    """Order-update adapter for Pocketful (feed "updates" channel)."""

    def __init__(self, user_id: str, client_id: str, access_token: str):
        super().__init__(broker_name="pocketful", user_id=user_id)
        self.client_id = client_id
        self.access_token = access_token

    def get_ws_url(self) -> str:
        return f"{POCKETFUL_FEED_URL}?login_id={self.client_id}&access_token={self.access_token}"

    def get_headers(self):
        return None  # auth is in the URL query

    def on_open_extra(self, ws) -> None:
        ws.send(json.dumps({"a": "subscribe", "v": [self.client_id, "web"], "m": "updates"}))

    def heartbeat_interval(self) -> int:
        return 8

    def send_heartbeat(self, ws) -> None:
        ws.send(_HEARTBEAT)

    def normalize_all(self, raw_message) -> list[dict]:
        """Every order update carried by one frame, as OrderUpdateEvent kwargs."""
        if isinstance(raw_message, str):
            raw_message = raw_message.encode("utf-8")
        if not isinstance(raw_message, (bytes, bytearray)) or len(raw_message) <= _JSON_OFFSET:
            return []
        if int.from_bytes(raw_message[0:1], "big", signed=True) != _ORDER_UPDATE_MODE:
            return []  # market data, trade updates
        try:
            body = json.loads(bytes(raw_message[_JSON_OFFSET:]).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []

        updates = []
        for order in _order_records(body):
            if not order.get("oms_order_id"):
                continue
            order = dict(order)
            # As map_order_data does for the orderbook: OpenAlgo symbol from
            # Pocketful's trading_symbol and exchange.
            exchange = order.get("exchange", "")
            order["trading_symbol"] = to_openalgo_symbol(order.get("trading_symbol", ""), exchange)
            row = transform_order_data([order])[0]
            order_status = row["order_status"]
            updates.append(
                {
                    "orderid": str(row["orderid"]),
                    "symbol": row["symbol"],
                    "exchange": row["exchange"],
                    "action": row["action"],
                    "quantity": int(row["quantity"] or 0),
                    "price": float(row["price"] or 0),
                    "trigger_price": float(row["trigger_price"] or 0),
                    "pricetype": row["pricetype"],
                    "product": row["product"],
                    "order_status": order_status,
                    "filled_quantity": int(row["filled_quantity"] or 0),
                    "pending_quantity": int(row["pending_quantity"] or 0),
                    "average_price": float(row["average_price"] or 0),
                    "rejection_reason": (order.get("rejection_reason") or "")
                    if order_status == "rejected"
                    else "",
                }
            )
        return updates

    def normalize(self, raw_message):
        updates = self.normalize_all(raw_message)
        return updates[0] if updates else None

    def _handle_message(self, raw_message) -> None:
        # One frame may carry several orders ("result" is a list).
        try:
            updates = self.normalize_all(raw_message)
        except Exception as e:
            self.logger.debug(f"Failed to normalize order-update message: {e}")
            return
        for fields in updates:
            self._publish_event_fields(fields)


def create_pocketful_order_adapter(user_id: str) -> "PocketfulOrderUpdateAdapter | None":
    """Build a PocketfulOrderUpdateAdapter for user_id, or None without a session.

    The access token is the stored auth token; the client ID is the one the
    login stored from /api/v1/user/trading_info (broker/pocketful/api/
    auth_api.py), which order_api.get_client_id also reads.
    """
    access_token = get_auth_token(user_id, bypass_cache=True)
    if not access_token:
        logger.warning(
            f"No Pocketful auth token found for user {user_id}; order-update adapter not started"
        )
        return None
    client_id = get_user_id(user_id)
    if not client_id:
        logger.warning(
            f"No Pocketful client ID stored for user {user_id}; order-update adapter not started"
        )
        return None
    return PocketfulOrderUpdateAdapter(
        user_id=user_id, client_id=client_id, access_token=access_token
    )
