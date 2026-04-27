# Zerodha GTT REST integration.
# Kite Connect GTT API reference: https://kite.trade/docs/connect/v3/gtt/

import json
import urllib.parse

from broker.zerodha.mapping.gtt_data import (
    map_gtt_book,
    transform_modify_gtt,
    transform_place_gtt,
)
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

_BASE = "https://api.kite.trade"


def _headers(auth, form=False):
    headers = {"X-Kite-Version": "3", "Authorization": f"token {auth}"}
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


def _encode_gtt_payload(transformed):
    """Kite expects `condition` and `orders` as JSON strings inside form-urlencoded body."""
    return urllib.parse.urlencode(
        {
            "type": transformed["type"],
            "condition": json.dumps(transformed["condition"]),
            "orders": json.dumps(transformed["orders"]),
        }
    )


def _fetch_last_price(symbol, exchange, auth):
    """Fetch LTP from Kite via the broker's own data handler.

    Kite's GTT condition requires ``last_price`` — clients no longer send it,
    so the broker layer resolves it just-in-time before placing.
    """
    from broker.zerodha.api.data import BrokerData

    quotes = BrokerData(auth).get_quotes(symbol, exchange)
    if not quotes:
        return None
    ltp = quotes.get("ltp") if isinstance(quotes, dict) else None
    return float(ltp) if ltp else None


def place_gtt_order(data, auth):
    """Create a GTT on Zerodha. Returns (response, response_dict, trigger_id).

    If ``data['last_price']`` is missing, it is fetched server-side from
    Zerodha's quotes endpoint.
    """
    if not data.get("last_price"):
        ltp = _fetch_last_price(data["symbol"], data["exchange"], auth)
        if not ltp:
            class _FakeResponse:
                status_code = 502
                status = 502
                text = ""
            return (
                _FakeResponse(),
                {"status": "error", "message": "Failed to fetch last_price from Zerodha quotes"},
                None,
            )
        data["last_price"] = ltp

    transformed = transform_place_gtt(data)
    body = _encode_gtt_payload(transformed)
    logger.info(f"Zerodha place_gtt payload: type={transformed['type']}, body={body}")

    client = get_httpx_client()
    response = client.post(f"{_BASE}/gtt/triggers", headers=_headers(auth, form=True), content=body)
    logger.info(f"Zerodha place_gtt raw: status={response.status_code}, body={response.text}")

    response_data = response.json()
    response.status = response.status_code  # parity with other order APIs

    trigger_id = None
    if response_data.get("status") == "success":
        trigger_id = str(response_data.get("data", {}).get("trigger_id", "") or "")

    return response, response_data, trigger_id


def modify_gtt_order(data, auth):
    """Modify an active GTT on Zerodha. Returns (response_dict, status_code).

    ``data`` must include ``trigger_id`` plus the flat replacement body
    (trigger_type, action, product, quantity, pricetype, price, trigger_price,
    and OCO-only stoploss + target). ``last_price`` is fetched if missing.
    Kite's PUT replaces type/condition/orders atomically.
    """
    trigger_id = data.get("trigger_id")
    if not trigger_id:
        return {"status": "error", "message": "trigger_id is required"}, 400

    if not data.get("last_price"):
        ltp = _fetch_last_price(data["symbol"], data["exchange"], auth)
        if not ltp:
            return {"status": "error", "message": "Failed to fetch last_price from Zerodha quotes"}, 502
        data["last_price"] = ltp

    transformed = transform_modify_gtt(data)
    body = _encode_gtt_payload(transformed)
    logger.info(f"Zerodha modify_gtt payload ({trigger_id}): {body}")

    client = get_httpx_client()
    response = client.put(
        f"{_BASE}/gtt/triggers/{trigger_id}", headers=_headers(auth, form=True), content=body
    )
    logger.info(f"Zerodha modify_gtt raw: status={response.status_code}, body={response.text}")

    try:
        response_data = response.json()
    except Exception:
        return {"status": "error", "message": response.text or "Invalid response"}, response.status_code

    if response_data.get("status") == "success":
        returned_id = response_data.get("data", {}).get("trigger_id", trigger_id)
        return {"status": "success", "trigger_id": str(returned_id)}, 200

    return {
        "status": "error",
        "message": response_data.get("message", "Failed to modify GTT"),
    }, response.status_code


def cancel_gtt_order(trigger_id, auth):
    """Cancel an active GTT on Zerodha. Returns (response_dict, status_code)."""
    if not trigger_id:
        return {"status": "error", "message": "trigger_id is required"}, 400

    client = get_httpx_client()
    response = client.delete(f"{_BASE}/gtt/triggers/{trigger_id}", headers=_headers(auth))
    logger.info(f"Zerodha cancel_gtt raw: status={response.status_code}, body={response.text}")

    try:
        response_data = response.json()
    except Exception:
        return {"status": "error", "message": response.text or "Invalid response"}, response.status_code

    if response_data.get("status") == "success":
        returned_id = response_data.get("data", {}).get("trigger_id", trigger_id)
        return {"status": "success", "trigger_id": str(returned_id)}, 200

    return {
        "status": "error",
        "message": response_data.get("message", "Failed to cancel GTT"),
    }, response.status_code


def get_gtt_book(auth):
    """List all GTTs for the user. Returns (response_dict, status_code).

    The returned dict has ``status`` and ``data`` where ``data`` is a list of
    OpenAlgo-normalised GTT objects (see ``map_gtt_book``).
    """
    client = get_httpx_client()
    response = client.get(f"{_BASE}/gtt/triggers", headers=_headers(auth))
    logger.info(f"Zerodha gtt_book raw: status={response.status_code}")

    try:
        raw = response.json()
    except Exception:
        return {"status": "error", "message": response.text or "Invalid response"}, response.status_code

    if raw.get("status") != "success":
        return {
            "status": "error",
            "message": raw.get("message", "Failed to fetch GTT book"),
        }, response.status_code

    return {"status": "success", "data": map_gtt_book(raw)}, 200
