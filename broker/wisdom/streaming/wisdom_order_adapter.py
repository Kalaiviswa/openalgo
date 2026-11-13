"""
Wisdom Capital order-update adapter: the XTS Interactive socket's "order" event.

Wisdom Capital runs Symphony Fintech XTS. The Interactive API pushes order, trade
and position changes over Socket.IO (XTS Interactive API v2, section "Socket
Streaming"; https://symphonyfintech.com/xts-trading-front-end-api-v2/ and the
broker-hosted copy at <root>/doc/interactive):

    URL     {root}/?token=<interactive token>&userID=<user id>&apiType=INTERACTIVE
    path    /interactive/socket.io
    events  joined, error, order, trade, position, tradeConversion, logout, ...

Only "order" is used: the doc describes it as the order lifecycle (placed,
modified, filled, cancelled, rejected), and each "trade" is followed by an
"order" carrying the same cumulative fill, so publishing both would double
every fill.

Why raw Engine.IO v3 framing over websocket-client instead of socketio.Client:
the XTS servers are Socket.IO 2.x (Engine.IO v3). Probed 2026-11-13: the
polling handshake answers EIO=4 with v3 length-prefixed framing
('97:0{...}2:40'). In Engine.IO v3 the CLIENT sends the pings ("2") and the
server closes a client that is silent for pingInterval + pingTimeout (45s).
python-socketio 5.x (python-engineio 4) speaks v4 only, where the server pings,
so on an order socket that never sends anything it would neither ping nor
expect pings correctly (broker/rmoney/streaming/rmoney_websocket.py carries a
timer floor to survive the same mismatch on market data). Speaking v3
directly is small (open "0", connect "40", event "42[...]", ping "2", pong
"3") and keeps BaseOrderUpdateAdapter's reconnect backoff, generation guard,
auth stand-down and event publishing unchanged.

Handshake observed on every reachable XTS host with a dead token:
    0{"sid":...,"pingInterval":25000,"pingTimeout":20000}
    40
    42["socketError","{\"type\":\"error\",\"code\":\"e-socket-0003\",
        \"description\":\"Your session has been expired\"}"]
    41   (about 10s later)
Event payloads arrive as JSON *strings* inside the Socket.IO array, so they
are decoded twice. A session refusal stands the adapter down until the next
login rather than retrying a token XTS has already refused.

Credentials: the interactive token is the stored auth token (order_api.py
sends it as the REST "authorization" header). The socket's userID is the
account stored at login when the login stores one; otherwise it is read from
GET {INTERACTIVE_URL}/user/profile (ClientId), authorised by the same token,
on the adapter thread so the order-update service lock never waits on the
network.
"""

import json
import re
from urllib.parse import urlencode, urlparse

from broker.wisdom.baseurl import INTERACTIVE_URL
from database.auth_db import get_auth_token, get_user_id
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger
from websocket_proxy.order_adapter import BaseOrderUpdateAdapter, to_openalgo_symbol

logger = get_logger(__name__)

# Engine.IO v3: the client pings. The servers advertise pingInterval 25s and
# drop a client silent for 45s, so ping inside the interval.
XTS_PING_INTERVAL_SECONDS = 20

# Same reverse map as broker/wisdom/mapping/order_data.py::transform_order_data.
_EXCHANGE_MAP = {
    "NSECM": "NSE",
    "BSECM": "BSE",
    "NSEFO": "NFO",
    "BSEFO": "BFO",
    "MCXFO": "MCX",
    "NSECD": "CDS",
}

# XTS OrderType -> OpenAlgo pricetype. The order event spells it "LIMIT" and
# the trade event "Limit" in the doc, so the lookup is case-insensitive.
_PRICETYPE_MAP = {
    "limit": "LIMIT",
    "market": "MARKET",
    "stoplimit": "SL",
    "stopmarket": "SL-M",
}

# XTS ProductType -> OpenAlgo product (mapping/transform_data.py::reverse_map_product_type).
_PRODUCT_MAP = {"CNC": "CNC", "NRML": "NRML", "MIS": "MIS"}

# XTS OrderStatus (the Order Status enum of the doc) -> OpenAlgo's lowercase
# vocabulary. Every working state is "open"; the orderbook mapping in
# mapping/order_data.py agrees on the four it maps (New, Filled, Rejected,
# Cancelled). XTS documents no separate untriggered-stop state, so a stop
# order waiting for its trigger is reported as XTS reports it ("New" -> open).
_STATUS_MAP = {
    "new": "open",
    "open": "open",
    "pendingnew": "open",
    "partiallyfilled": "open",
    "replaced": "open",
    "pendingreplace": "open",
    "pendingcancel": "open",
    "filled": "complete",
    "rejected": "rejected",
    "cancelled": "cancelled",
}

# socketError codes that mean the token or session is dead.
_SESSION_ERROR_CODES = {"e-socket-0003"}


class XtsSessionRejected(Exception):
    """XTS refused the interactive token; carries a 401 so the base class
    stands down instead of retrying (see order_adapter._is_auth_rejection)."""

    status_code = 401


def _decode(value):
    """XTS sends event data as a JSON string inside the Socket.IO array."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _number(value, cast):
    """Cast a numeric XTS field, keeping 0 when the broker leaves it empty."""
    try:
        return cast(float(value)) if value not in (None, "") else cast(0)
    except (TypeError, ValueError):
        return cast(0)


def _parse_event_packet(packet: str):
    """Split an Engine.IO v3 message packet '42[...]' into (event, data).

    Returns None for any other packet. Tolerates an optional namespace
    ('42/ns,[...]') and ack id ('4212[...]'), which XTS does not send today.
    """
    if not packet.startswith("42"):
        return None
    body = packet[2:]
    if body.startswith("/"):
        comma = body.find(",")
        if comma < 0:
            return None
        body = body[comma + 1 :]
    body = body.lstrip("0123456789")
    try:
        frame = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(frame, list) or not frame:
        return None
    return frame[0], (frame[1] if len(frame) > 1 else None)


def _socket_endpoint() -> tuple[str, str]:
    """(origin, socket.io path) for this broker's interactive root.

    INTERACTIVE_URL carries the interactive path (/interactive); the socket is
    served under it at /socket.io. Repeated slashes are folded, since a base
    URL with a trailing slash produces '//interactive'.
    """
    parsed = urlparse(INTERACTIVE_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    return f"{scheme}://{parsed.netloc}", f"{path}/socket.io/"


def fetch_xts_client_id(interactive_token: str) -> str:
    """The account of an interactive token, from XTS GET /user/profile.

    Raises XtsSessionRejected when XTS refuses the token, RuntimeError on any
    other failure (network, unexpected body) so the caller retries later.
    """
    response = get_httpx_client().get(
        f"{INTERACTIVE_URL}/user/profile",
        headers={"authorization": interactive_token, "Content-Type": "application/json"},
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise RuntimeError(f"user profile answered HTTP {response.status_code} without JSON")
    if data.get("type") != "success":
        code = str(data.get("code") or "")
        if response.status_code in (401, 403) or code.startswith(("e-token", "e-session")):
            raise XtsSessionRejected(f"{code} {data.get('description', '')}".strip())
        raise RuntimeError(f"user profile refused: {code} {data.get('description', '')}".strip())
    result = data.get("result")
    if isinstance(result, dict):
        for key in ("ClientId", "ClientID", "clientId", "clientID"):
            value = str(result.get(key) or "").strip()
            if value:
                return value
    raise RuntimeError("user profile did not name the account")


class WisdomOrderUpdateAdapter(BaseOrderUpdateAdapter):
    """Order-update adapter for Wisdom Capital (XTS Interactive socket, Engine.IO v3)."""

    def __init__(self, user_id: str, interactive_token: str, xts_user_id: str | None = None):
        super().__init__(broker_name="wisdom", user_id=user_id)
        self.interactive_token = interactive_token
        self.xts_user_id = (xts_user_id or "").strip() or None

    # -- connection ------------------------------------------------------

    def _resolve_xts_user_id(self) -> str:
        if not self.xts_user_id:
            self.xts_user_id = fetch_xts_client_id(self.interactive_token)
        return self.xts_user_id

    def get_ws_url(self) -> str:
        origin, path = _socket_endpoint()
        query = urlencode(
            {
                "token": self.interactive_token,
                "userID": self._resolve_xts_user_id(),
                "apiType": "INTERACTIVE",
                "EIO": "3",
                "transport": "websocket",
            }
        )
        return f"{origin}{path}?{query}"

    def get_headers(self):
        return None  # XTS authenticates the socket from the query string

    def heartbeat_interval(self):
        return XTS_PING_INTERVAL_SECONDS

    def send_heartbeat(self, ws) -> None:
        ws.send("2")  # Engine.IO v3 ping; the server answers "3"

    def _close_socket(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            ws.close()
        except Exception:
            self.logger.debug("Closing the Wisdom Capital order socket failed", exc_info=True)

    def _stand_down(self, reason: str) -> None:
        """XTS ended the session: stop until the next login instead of retrying."""
        self._auth_rejected = True
        self.logger.warning(
            f"Wisdom Capital order updates stopped for {self.user_id}: {reason}. "
            "They resume on the next broker login."
        )
        self._close_socket()

    # -- messages --------------------------------------------------------

    def normalize(self, raw_message):
        if isinstance(raw_message, (bytes, bytearray)):
            raw_message = raw_message.decode("utf-8", "replace")
        if not isinstance(raw_message, str) or not raw_message:
            return None

        if raw_message == "1" or raw_message.startswith("41"):
            # Engine.IO close / Socket.IO disconnect from the server: drop the
            # socket so the base class reconnects with backoff.
            self.logger.info(
                f"Wisdom Capital order socket closed by the server ({raw_message[:2]})"
            )
            self._close_socket()
            return None

        parsed = _parse_event_packet(raw_message)
        if parsed is None:
            return None  # "0{...}" open, "40" connect, "3" pong
        event, payload = parsed

        if event == "joined":
            self.logger.info(f"Wisdom Capital order socket joined for {self.user_id}")
            return None
        if event in ("socketError", "error"):
            data = _decode(payload)
            code = str(data.get("code") or "") if isinstance(data, dict) else ""
            description = (
                str(data.get("description") or "") if isinstance(data, dict) else str(data or "")
            )
            text = description.lower()
            if code in _SESSION_ERROR_CODES or "session" in text or "token" in text:
                self._stand_down(f"XTS refused the session ({code} {description})".strip())
            else:
                self.logger.warning(
                    f"Wisdom Capital order socket error: {code} {description}".strip()
                )
                self._close_socket()
            return None
        if event == "logout":
            self._stand_down("XTS logged the session out")
            return None
        if event != "order":
            return None

        order = _decode(payload)
        if not isinstance(order, dict):
            return None
        return self._order_fields(order)

    @staticmethod
    def _order_fields(order: dict) -> dict | None:
        app_order_id = order.get("AppOrderID")
        if app_order_id in (None, ""):
            return None
        try:
            orderid = str(int(float(app_order_id)))
        except (TypeError, ValueError):
            orderid = str(app_order_id)

        segment = str(order.get("ExchangeSegment") or "")
        exchange = _EXCHANGE_MAP.get(segment, segment)
        token = order.get("ExchangeInstrumentID")
        symbol = to_openalgo_symbol(
            str(order.get("TradingSymbol") or ""),
            exchange,
            token=str(token) if token not in (None, "") else None,
        )

        raw_status = str(order.get("OrderStatus") or "")
        order_status = _STATUS_MAP.get(raw_status.replace(" ", "").lower(), raw_status.lower())

        raw_type = str(order.get("OrderType") or "")
        raw_product = str(order.get("ProductType") or "")

        return {
            "orderid": orderid,
            "symbol": symbol,
            "exchange": exchange,
            "action": str(order.get("OrderSide") or "").upper(),
            "quantity": _number(order.get("OrderQuantity"), int),
            "price": _number(order.get("OrderPrice"), float),
            "trigger_price": _number(order.get("OrderStopPrice"), float),
            "pricetype": _PRICETYPE_MAP.get(raw_type.lower(), raw_type),
            "product": _PRODUCT_MAP.get(raw_product.upper(), raw_product),
            "order_status": order_status,
            "filled_quantity": _number(order.get("CumulativeQuantity"), int),
            "pending_quantity": _number(order.get("LeavesQuantity"), int),
            "average_price": _number(order.get("OrderAverageTradedPrice"), float),
            "rejection_reason": str(order.get("CancelRejectReason") or "")
            if order_status == "rejected"
            else "",
        }


def create_wisdom_order_adapter(user_id: str) -> "WisdomOrderUpdateAdapter | None":
    """Factory: build the Wisdom Capital order-update adapter, or None with no session."""
    interactive_token = get_auth_token(user_id, bypass_cache=True)
    if not interactive_token:
        logger.warning(
            f"No Wisdom Capital interactive token for user {user_id}; order-update adapter not started"
        )
        return None
    # The account stored at login, when the login stores one. Otherwise the
    # adapter asks XTS on its own thread (fetch_xts_client_id).
    xts_user_id = get_user_id(user_id)
    return WisdomOrderUpdateAdapter(
        user_id=user_id,
        interactive_token=interactive_token,
        xts_user_id=str(xts_user_id) if xts_user_id else None,
    )
