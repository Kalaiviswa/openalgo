"""
Rupeezy (Vortex) order-update adapter: order postbacks on a Vortex feed socket.

Docs: broker-api-docs/vortex-api-docs/18-feed.md ("Order Messages": the
websocket delivers postbacks in text mode, with the same structure as the
postbacks section; "price feed always comes in binary mode, while updates come
in text mode") and 19-postbacks.md (payload: {"type", "data", "client_code"}).
Endpoint: wss://wire.rupeezy.in/ws?auth_token=<access_token>

One access token may hold up to 3 concurrent websockets (18-feed.md
"Limits"), so this adapter opens its own connection beside the market-data
one and never subscribes to an instrument: binary frames (price ticks) are
ignored, only JSON text frames are decoded. Postbacks arrive without any
subscription.

Fields are mapped with broker/rupeezy/mapping/order_data.py, the same code
the orderbook uses, so the stream and the orderbook agree on symbol,
exchange, product, price type, status and MCX quantity units.
"""

import json

from broker.rupeezy.api.baseurl import WS_URL
from broker.rupeezy.mapping.order_data import _f, _units, map_status, transform_order_data
from database.auth_db import get_auth_token
from utils.logging import get_logger
from websocket_proxy.order_adapter import BaseOrderUpdateAdapter

logger = get_logger(__name__)

# 19-postbacks.md "type": order, trade, sl_trigger, gtt_order, position_conversion.
# The first three describe a regular order's state; GTT triggers and position
# conversions are not orderbook rows.
_ORDER_TYPES = {"order", "trade", "sl_trigger"}


class RupeezyOrderUpdateAdapter(BaseOrderUpdateAdapter):
    """Order-update adapter for Rupeezy (Vortex feed text frames)."""

    def __init__(self, user_id: str, access_token: str):
        super().__init__(broker_name="rupeezy", user_id=user_id)
        self.access_token = access_token

    def get_ws_url(self) -> str:
        return f"{WS_URL}?auth_token={self.access_token}"

    def get_headers(self):
        return None  # auth is in the URL query

    def ws_ping_interval(self) -> int:
        # Same cadence as the market-data client (rupeezy_websocket.py).
        return 10

    def normalize(self, raw_message):
        if isinstance(raw_message, (bytes, bytearray)):
            return None  # price ticks and heartbeats

        try:
            message = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(message, dict) or message.get("type") not in _ORDER_TYPES:
            return None

        data = message.get("data")
        if not isinstance(data, dict) or not data.get("order_id"):
            return None

        row = transform_order_data([data])[0]
        order_status = map_status(data.get("status"))
        return {
            "orderid": str(row["orderid"]),
            "symbol": row["symbol"],
            "exchange": row["exchange"],
            "action": row["action"],
            "quantity": row["quantity"],
            "price": row["price"],
            "trigger_price": row["trigger_price"],
            "pricetype": row["pricetype"],
            "product": row["product"],
            "order_status": order_status,
            "filled_quantity": _units(data, "traded_quantity"),
            "pending_quantity": _units(data, "pending_quantity"),
            "average_price": _f(data.get("traded_price")),
            "rejection_reason": (data.get("status_message") or "")
            if order_status == "rejected"
            else "",
        }


def create_rupeezy_order_adapter(user_id: str) -> "RupeezyOrderUpdateAdapter | None":
    """Build a RupeezyOrderUpdateAdapter for user_id, or None without a session.

    The stored auth token is the Vortex access token, which the feed takes as
    its auth_token query parameter (as broker/rupeezy/streaming/
    rupeezy_websocket.py does).
    """
    access_token = get_auth_token(user_id, bypass_cache=True)
    if not access_token:
        logger.warning(
            f"No Rupeezy auth token found for user {user_id}; order-update adapter not started"
        )
        return None
    return RupeezyOrderUpdateAdapter(user_id=user_id, access_token=access_token)
