# Dhan Forever Order REST integration.
# Dhan v2 reference: https://dhanhq.co/docs/v2/forever/

import json
import os

from broker.dhan.api.baseurl import get_url
from broker.dhan.mapping.gtt_data import (
    map_gtt_book,
    transform_modify_gtt,
    transform_place_gtt,
)
from database.auth_db import get_user_id, verify_api_key
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


class _FakeResponse:
    """Minimal stand-in so the service layer's ``res.status`` access keeps working
    when we short-circuit before issuing the HTTP call."""

    def __init__(self, status_code):
        self.status_code = status_code
        self.status = status_code
        self.text = ""


def _resolve_client_id(api_key):
    """Resolve dhanClientId from BROKER_API_KEY env (``client_id:::api_key``) or DB."""
    broker_api_key = os.getenv("BROKER_API_KEY", "")
    if ":::" in broker_api_key:
        return broker_api_key.split(":::")[0]
    if api_key:
        user_id = verify_api_key(api_key)
        if user_id:
            return get_user_id(user_id)
    return None


def _headers(auth, client_id=None):
    headers = {
        "access-token": auth,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if client_id:
        headers["client-id"] = client_id
    return headers


def place_gtt_order(data, auth):
    """Create a Forever Order on Dhan. Returns ``(response, response_dict, trigger_id)``.

    Mirrors ``place_order_api``: the dhanClientId is resolved from
    ``BROKER_API_KEY`` (or DB fallback) and injected before the mapper builds
    the JSON body.
    """
    client_id = _resolve_client_id(data.get("apikey"))
    if not client_id:
        return (
            _FakeResponse(401),
            {"status": "error", "message": "Could not resolve Dhan client id"},
            None,
        )
    data["dhan_client_id"] = client_id

    payload = json.dumps(transform_place_gtt(data))
    logger.info(f"Dhan place_gtt payload: {payload}")

    client = get_httpx_client()
    response = client.post(
        get_url("/v2/forever/orders"),
        headers=_headers(auth, client_id=client_id),
        content=payload,
    )
    response.status = response.status_code  # parity with other order APIs
    logger.info(
        f"Dhan place_gtt raw: status={response.status_code}, body={response.text}"
    )

    try:
        response_data = json.loads(response.text)
    except json.JSONDecodeError:
        return (
            response,
            {"status": "error", "message": response.text or "Invalid response"},
            None,
        )

    trigger_id = None
    if response.status_code in (200, 201) and isinstance(response_data, dict):
        trigger_id = str(response_data.get("orderId") or "") or None

    return response, response_data, trigger_id


def modify_gtt_order(data, auth):
    """Modify a Forever Order on Dhan. Returns ``(response_dict, status_code)``.

    Dhan's PUT modifies one leg at a time. For SINGLE we send a single PUT
    (``legName=ENTRY_LEG``); for OCO we send two sequential PUTs
    (``STOP_LOSS_LEG`` then ``TARGET_LEG``) and report the first failure if
    any.
    """
    trigger_id = data.get("trigger_id")
    if not trigger_id:
        return {"status": "error", "message": "trigger_id is required"}, 400

    client_id = _resolve_client_id(data.get("apikey"))
    if not client_id:
        return {"status": "error", "message": "Could not resolve Dhan client id"}, 401
    data["dhan_client_id"] = client_id

    trigger_type = (data.get("trigger_type") or "").upper()
    leg_names = (
        ["STOP_LOSS_LEG", "TARGET_LEG"] if trigger_type == "OCO" else ["ENTRY_LEG"]
    )

    headers = _headers(auth, client_id=client_id)
    client = get_httpx_client()
    url = get_url(f"/v2/forever/orders/{trigger_id}")

    last_response_data = {}
    last_status = 200
    for leg_name in leg_names:
        payload = json.dumps(transform_modify_gtt(data, leg_name))
        logger.info(f"Dhan modify_gtt ({trigger_id}, {leg_name}) payload: {payload}")

        response = client.put(url, headers=headers, content=payload)
        logger.info(
            f"Dhan modify_gtt ({leg_name}) raw: status={response.status_code}, body={response.text}"
        )

        try:
            response_data = json.loads(response.text)
        except json.JSONDecodeError:
            return (
                {"status": "error", "message": f"{leg_name}: invalid response"},
                response.status_code,
            )

        if response.status_code != 200 or not (
            isinstance(response_data, dict) and response_data.get("orderId")
        ):
            msg = (
                response_data.get("errorMessage")
                or response_data.get("message")
                or f"Failed to modify {leg_name}"
            )
            return {"status": "error", "message": msg}, response.status_code

        last_response_data = response_data
        last_status = response.status_code

    return (
        {
            "status": "success",
            "trigger_id": str(last_response_data.get("orderId", trigger_id)),
        },
        last_status,
    )


def cancel_gtt_order(trigger_id, auth):
    """Cancel a Forever Order on Dhan. Returns ``(response_dict, status_code)``."""
    if not trigger_id:
        return {"status": "error", "message": "trigger_id is required"}, 400

    client = get_httpx_client()
    response = client.delete(
        get_url(f"/v2/forever/orders/{trigger_id}"),
        headers=_headers(auth),
    )
    logger.info(
        f"Dhan cancel_gtt raw: status={response.status_code}, body={response.text}"
    )

    try:
        response_data = json.loads(response.text)
    except json.JSONDecodeError:
        return (
            {"status": "error", "message": response.text or "Invalid response"},
            response.status_code,
        )

    if (
        response.status_code == 200
        and isinstance(response_data, dict)
        and response_data.get("orderId")
    ):
        return {"status": "success", "trigger_id": str(response_data["orderId"])}, 200

    msg = (
        response_data.get("errorMessage")
        or response_data.get("message")
        or "Failed to cancel GTT"
    )
    return {"status": "error", "message": msg}, response.status_code


def get_gtt_book(auth):
    """List all Forever Orders for the user. Returns ``(response_dict, status_code)``.

    The returned dict has ``status`` and ``data`` where ``data`` is the
    OpenAlgo-normalised list (see :func:`map_gtt_book`).
    """
    # Dhan's published docs say GET /v2/forever/all but their official SDK
    # and live API use GET /v2/forever/orders. /all returns 404.
    client = get_httpx_client()
    response = client.get(get_url("/v2/forever/orders"), headers=_headers(auth))
    logger.info(f"Dhan gtt_book raw: status={response.status_code}")

    try:
        raw = json.loads(response.text)
    except json.JSONDecodeError:
        return (
            {"status": "error", "message": response.text or "Invalid response"},
            response.status_code,
        )

    if response.status_code != 200:
        msg = raw.get("errorMessage") if isinstance(raw, dict) else None
        return (
            {"status": "error", "message": msg or "Failed to fetch Forever orders"},
            response.status_code,
        )

    # Dhan returns a bare list; some endpoints wrap in {data: [...]}.
    payload = raw if isinstance(raw, list) else raw.get("data", [])
    return {"status": "success", "data": map_gtt_book(payload)}, 200
