"""
Firstock order-update adapter - the Order Update Feed on a dedicated V2 socket.

Docs: broker-api-docs/firstock-api-docs/43-websocket-getting-started-v2.md
(connection string and auth answer) and 45-websocket-order-update-feed-v2.md
(order frames). Endpoint: wss://socket.firstock.in/V2/ws

Protocol, as documented:
    1. connect with userId, jKey (the login susertoken) and source=developer-api
       as query parameters - there is no login frame and no subscribe frame
    2. the server answers {"status":"success","message":"Authentication successful"}
       or {"status":"failed","message":"unauthenticated"} and then disconnects
    3. order updates arrive on their own as flat JSON objects; the doc says to
       tell them apart by "norenordno" (order feed) versus "brkname" (position
       feed), and market ticks never carry either
    4. the server sends Ping frames and drops a socket that does not Pong within
       10s; websocket-client answers those automatically

The order frame is Noren-shaped, but the documented field list differs from
Shoonya/Flattrade's t="om" frame: there is no "t" key at all, the product is in
"pcode" (not "prd"), and the doc lists no fill quantity, average price or
trigger price. Those three are read under their Noren names when present and
left out of the event otherwise, so an update the broker sent without them is
not reported as "0 filled".

This opens its own socket instead of sharing the market-data one, which runs
in the websocket_proxy subprocess under gunicorn (an event published there
would land on the wrong process's event bus). The doc's own examples open a
separate connection per feed. Whether socket.firstock.in keeps two sockets for
one userId side by side is not stated in the docs and has not been verified
live: if it evicts the market-data socket the way Flattrade's PiConnect does
(see broker/flattrade/streaming/flattrade_order_adapter.py), switch the factory
to REST polling.

Credentials: jKey = get_auth_token(user_id) (Firstock stores the susertoken as
the auth token); userId = BROKER_API_KEY minus its last four characters (the
"_API" suffix), the same as broker/firstock/api/order_api.py.
"""

import json
import os
from urllib.parse import urlencode

from database.auth_db import get_auth_token
from utils.logging import get_logger
from websocket_proxy.order_adapter import BaseOrderUpdateAdapter, to_openalgo_symbol

logger = get_logger(__name__)

FIRSTOCK_WS_URL = "wss://socket.firstock.in/V2/ws"
FIRSTOCK_WS_SOURCE = "developer-api"

# Firstock order status -> OpenAlgo's lowercase push vocabulary. The doc lists
# the transition states ORDER ACK, ORDER PENDING, OPEN, TRIGGER_PENDING,
# AMO OPEN, AMO MODIFIED, AMO CANCELED, PENDING and the end states COMPLETE,
# REJECTED, CANCELED. Every in-flight state collapses to "open".
#
# The push path uses "trigger pending" (docs/api/websocket-streaming/
# order-updates.md), while broker/firstock/mapping/order_data.py emits
# "trigger_pending" for the REST order book; the two consumers differ, as the
# Shoonya adapter explains, so they are deliberately not unified here.
_STATUS_MAP = {
    "order ack": "open",
    "order pending": "open",
    "open": "open",
    "pending": "open",
    "amo open": "open",
    "amo modified": "open",
    "trigger pending": "trigger pending",
    "complete": "complete",
    "rejected": "rejected",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "amo canceled": "cancelled",
    "amo cancelled": "cancelled",
}

# Same table as broker/firstock/mapping/order_data.py::calculate_order_statistics,
# keyed on the upper-cased code with "_" folded to "-".
_PRICETYPE_MAP = {
    "MKT": "MARKET",
    "MARKET": "MARKET",
    "LMT": "LIMIT",
    "LIMIT": "LIMIT",
    "SL-MKT": "SL-M",
    "SL-MARKET": "SL-M",
    "SL-M": "SL-M",
    "SL-LMT": "SL",
    "SL-LIMIT": "SL",
    "SL": "SL",
}

_ACTION_MAP = {"B": "BUY", "S": "SELL"}


def _status_text(value) -> str:
    """Lowercase a status, folding "_" to " " so TRIGGER_PENDING and
    "TRIGGER PENDING" compare equal."""
    return str(value or "").strip().lower().replace("_", " ")


def _product(pcode: str, exchange: str) -> str:
    """Mirror broker/firstock/mapping/order_data.py: C is CNC on the cash
    segments, I is MIS anywhere, M is NRML on the derivative segments. Anything
    else is passed through as the broker sent it."""
    if pcode == "C" and exchange in ("NSE", "BSE"):
        return "CNC"
    if pcode == "I":
        return "MIS"
    if pcode == "M" and exchange in ("NFO", "MCX", "BFO", "CDS"):
        return "NRML"
    return pcode


def _present(data: dict, key: str) -> bool:
    value = data.get(key)
    return value is not None and str(value).strip() != ""


class FirstockOrderUpdateAdapter(BaseOrderUpdateAdapter):
    """Order-update adapter for Firstock (V2 socket, dedicated connection)."""

    def __init__(self, user_id: str, firstock_uid: str, jkey: str):
        super().__init__(broker_name="firstock", user_id=user_id)
        self.firstock_uid = firstock_uid
        self.jkey = jkey

    def _current_jkey(self) -> str:
        """The stored susertoken, re-read on every (re)connect: the token is
        baked into the URL and rolls over daily at ~3 AM IST. Keeps the last
        known token when the read fails."""
        try:
            fresh = get_auth_token(self.user_id, bypass_cache=True)
        except Exception:
            self.logger.debug("Re-reading the Firstock token failed", exc_info=True)
            fresh = None
        if fresh:
            self.jkey = fresh
        return self.jkey

    def get_ws_url(self) -> str:
        params = {
            "userId": self.firstock_uid,
            "jKey": self._current_jkey(),
            "source": FIRSTOCK_WS_SOURCE,
        }
        return f"{FIRSTOCK_WS_URL}?{urlencode(params)}"

    def get_headers(self):
        return None  # authenticated by the query parameters

    def _stand_down_after_auth_failure(self, data: dict) -> None:
        """The broker refused the jKey. Retrying cannot fix that before the
        next login, so flag the rejection and close: _run_forever then idles
        instead of reconnecting (the same path as a 401 handshake)."""
        self.logger.warning(
            f"Firstock order-update socket refused the session "
            f"({self.user_id}): {data.get('message', '')}. "
            "Not retrying until the next broker login."
        )
        self._auth_rejected = True
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                self.logger.debug("Closing the refused Firstock socket failed", exc_info=True)

    def normalize(self, raw_message):
        if isinstance(raw_message, (bytes, bytearray)):
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            data = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None

        if "norenordno" not in data:
            # Not an order frame: the connect answer, a position update
            # ("brkname"), or a market tick. Only the auth answer matters here.
            status = str(data.get("status") or "").strip().lower()
            message = str(data.get("message") or "").strip().lower()
            if status == "failed" or message == "unauthenticated":
                self._stand_down_after_auth_failure(data)
            elif status == "success":
                self.logger.info(f"Firstock order-update socket authenticated ({self.user_id})")
            return None

        raw_status = _status_text(data.get("status"))
        order_status = _STATUS_MAP.get(raw_status, raw_status)

        exchange = str(data.get("exch") or "")
        symbol = to_openalgo_symbol(str(data.get("tsym") or ""), exchange, token=data.get("token"))

        raw_prctyp = str(data.get("prctyp") or "").upper().replace("_", "-").strip()
        trantype = str(data.get("trantype") or "")
        pcode = str(data.get("pcode") or data.get("prd") or "")

        fields = {
            "orderid": str(data.get("norenordno") or ""),
            "symbol": symbol,
            "exchange": exchange,
            "action": _ACTION_MAP.get(trantype, trantype),
            "pricetype": _PRICETYPE_MAP.get(raw_prctyp, raw_prctyp),
            "product": _product(pcode, exchange),
            "order_status": order_status,
        }

        if _present(data, "qty"):
            fields["quantity"] = int(float(data["qty"]))
        if _present(data, "prc"):
            fields["price"] = float(data["prc"])
        # Not in the documented order-feed field list; read under their Noren
        # names when the broker includes them.
        if _present(data, "trgprc"):
            fields["trigger_price"] = float(data["trgprc"])
        if _present(data, "avgprc"):
            fields["average_price"] = float(data["avgprc"])
        if _present(data, "fillshares"):
            fields["filled_quantity"] = int(float(data["fillshares"]))
            if "quantity" in fields:
                fields["pending_quantity"] = max(fields["quantity"] - fields["filled_quantity"], 0)

        if order_status == "rejected":
            fields["rejection_reason"] = str(data.get("rejreason") or "").strip()

        return fields


def create_firstock_order_adapter(user_id: str) -> "FirstockOrderUpdateAdapter | None":
    """Factory: build a FirstockOrderUpdateAdapter for user_id, or None when
    there is no session to connect with."""
    jkey = get_auth_token(user_id, bypass_cache=True)
    if not jkey:
        logger.warning(
            f"No Firstock session token for user {user_id}; order-update adapter not started"
        )
        return None

    # broker/firstock/api/order_api.py: userId = BROKER_API_KEY[:-4] (drops "_API").
    broker_api_key = os.getenv("BROKER_API_KEY", "")
    firstock_uid = broker_api_key[:-4] if broker_api_key else ""
    if not firstock_uid:
        logger.warning(
            f"BROKER_API_KEY is not set, so the Firstock user id is unknown; "
            f"order-update adapter not started for {user_id}"
        )
        return None

    return FirstockOrderUpdateAdapter(user_id=user_id, firstock_uid=firstock_uid, jkey=jkey)
