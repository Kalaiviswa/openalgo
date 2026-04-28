# Dhan Forever Order payload transforms (OpenAlgo ⇄ Dhan).
# Dhan v2 reference: https://dhanhq.co/docs/v2/forever/

from broker.dhan.mapping.transform_data import (
    map_exchange,
    map_exchange_type,
    map_product_type,
    reverse_map_product_type,
)
from database.token_db import get_oa_symbol, get_token


# Dhan Forever Order status → OpenAlgo GTT status.
_STATUS_MAP = {
    "TRANSIT": "active",
    "PENDING": "active",
    "CONFIRM": "active",
    "TRADED": "triggered",
    "EXPIRED": "expired",
    "CANCELLED": "cancelled",
    "REJECTED": "rejected",
}


def transform_place_gtt(data):
    """Transform an OpenAlgo flat place-GTT payload into Dhan's POST /forever/orders body.

    SINGLE → ``triggerPrice`` + ``price`` + ``quantity`` from the flat fields.
    OCO    → primary leg uses ``price`` + ``triggerPrice`` (= ``stoploss``);
             target leg uses ``price1`` (= ``target``) + ``triggerPrice1``
             (= ``trigger_price``) + ``quantity1`` (same qty).

    Caller must populate ``data['dhan_client_id']`` before calling.
    """
    security_id = get_token(data["symbol"], data["exchange"])
    trigger_type = (data.get("trigger_type") or "").upper()  # SINGLE | OCO

    # For OCO the primary leg is the stoploss leg (lower trigger).
    primary_trigger = (
        float(data["trigger_price"]) if trigger_type == "SINGLE" else float(data["stoploss"])
    )

    body = {
        "dhanClientId": str(data["dhan_client_id"]),
        "orderFlag": trigger_type,  # SINGLE | OCO — Dhan's exact spelling
        "transactionType": data["action"].upper(),
        "exchangeSegment": map_exchange_type(data["exchange"]),
        "productType": map_product_type(data["product"]),
        "orderType": data.get("pricetype", "LIMIT"),  # LIMIT | MARKET
        "validity": "DAY",
        "securityId": str(security_id),
        "quantity": int(data["quantity"]),
        "price": float(data["price"]),
        "triggerPrice": primary_trigger,
    }

    if trigger_type == "OCO":
        body["price1"] = float(data["target"])
        body["triggerPrice1"] = float(data["trigger_price"])
        body["quantity1"] = int(data["quantity"])

    # OpenAlgo's ``strategy`` doubles as Dhan's correlationId (max 30 chars).
    correlation_id = data.get("correlation_id") or data.get("strategy") or ""
    if correlation_id:
        body["correlationId"] = str(correlation_id)[:30]

    return body


def transform_modify_gtt(data, leg_name):
    """Transform an OpenAlgo modify-GTT payload into Dhan's PUT /forever/orders/{id} body.

    Dhan modifies one leg at a time. ``leg_name`` selects which slice of the
    flat fields drives the body:
        - ``ENTRY_LEG`` (SINGLE) → ``trigger_price`` + ``price``.
        - ``STOP_LOSS_LEG`` (OCO) → ``stoploss`` + ``price``.
        - ``TARGET_LEG``    (OCO) → ``trigger_price`` + ``target``.

    Caller is responsible for invoking this once for SINGLE and twice for OCO.
    """
    trigger_type = (data.get("trigger_type") or "").upper()

    if leg_name == "TARGET_LEG":
        leg_price = float(data["target"])
        leg_trigger = float(data["trigger_price"])
    elif leg_name == "STOP_LOSS_LEG":
        leg_price = float(data["price"])
        leg_trigger = float(data["stoploss"])
    else:  # ENTRY_LEG — SINGLE has only one leg
        leg_price = float(data["price"])
        leg_trigger = float(data["trigger_price"])

    return {
        "dhanClientId": str(data["dhan_client_id"]),
        "orderId": str(data["trigger_id"]),
        "orderFlag": trigger_type,
        "orderType": data.get("pricetype", "LIMIT"),
        "legName": leg_name,
        "quantity": int(data["quantity"]),
        "price": leg_price,
        "triggerPrice": leg_trigger,
        "validity": "DAY",
    }


def map_gtt_book(gtt_list):
    """Normalise Dhan's GET /forever/all response into an OpenAlgo-shaped list.

    Dhan returns a flat list of legs (one row per leg). SINGLE has one leg
    (``ENTRY_LEG``); OCO has two (``STOP_LOSS_LEG`` + ``TARGET_LEG``) sharing
    one ``orderId``. We group by orderId, sort triggers ascending, and emit
    one OpenAlgo entry per order. ``last_price`` is not returned by Dhan, so
    it is left as 0 — the frontend will display "₹0.00".
    """
    if not isinstance(gtt_list, list):
        return []

    grouped = {}
    for item in gtt_list:
        oid = str(item.get("orderId", "") or "")
        if not oid:
            continue
        grouped.setdefault(oid, []).append(item)

    result = []
    for oid, legs in grouped.items():
        first = legs[0]
        ex = map_exchange(first.get("exchangeSegment", "")) or ""
        br_sym = first.get("tradingSymbol", "")
        oa_sym = (
            get_oa_symbol(brsymbol=br_sym, exchange=ex) if br_sym and ex else br_sym
        )

        # Sort legs so STOP_LOSS_LEG (lower trigger) comes first for OCO.
        sorted_legs = sorted(
            legs, key=lambda l: float(l.get("triggerPrice", 0) or 0)
        )
        trigger_prices = [float(l.get("triggerPrice", 0) or 0) for l in sorted_legs]

        out_legs = []
        for leg in sorted_legs:
            out_legs.append({
                "action": (leg.get("transactionType", "") or "").upper(),
                "quantity": leg.get("quantity", 0),
                "price": leg.get("price", 0),
                # Dhan's GET response doesn't include per-leg LIMIT/MARKET; default LIMIT.
                "pricetype": "LIMIT",
                "product": reverse_map_product_type(leg.get("productType", "")) or "CNC",
            })

        # Dhan reuses the ``orderType`` field in the GET response for the SINGLE/OCO flag.
        flag = (first.get("orderType") or "").upper()
        trigger_type_oa = "two-leg" if flag == "OCO" else "single"
        status_raw = (first.get("orderStatus") or "").upper()

        result.append({
            "trigger_id": oid,
            "trigger_type": trigger_type_oa,
            "status": _STATUS_MAP.get(status_raw, status_raw.lower()),
            "symbol": oa_sym or br_sym,
            "exchange": ex,
            "trigger_prices": trigger_prices,
            "last_price": 0,  # Dhan does not return LTP in this response
            "legs": out_legs,
            "created_at": first.get("createTime", "") or "",
            "updated_at": first.get("updateTime", "") or "",
            # Dhan Forever Orders have no explicit expiry — leave blank.
            "expires_at": "",
        })

    return result
