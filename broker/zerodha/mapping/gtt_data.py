# Zerodha GTT payload transforms (OpenAlgo ⇄ Kite).
# Kite Connect GTT API reference: https://kite.trade/docs/connect/v3/gtt/

from database.token_db import get_br_symbol, get_oa_symbol


def _leg_to_order(leg, tradingsymbol, exchange):
    """Map one OpenAlgo leg to a Kite `orders[]` entry."""
    return {
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "transaction_type": leg["action"].upper(),
        "quantity": int(leg["quantity"]),
        "order_type": leg.get("pricetype", "LIMIT"),
        "product": leg["product"],
        "price": float(leg["price"]),
    }


def transform_place_gtt(data):
    """Transform an OpenAlgo place-GTT payload into Kite's `{type, condition, orders}`.

    Expected ``data`` keys:
        symbol, exchange, trigger_type ("single" | "two-leg"),
        trigger_prices (list[float], len 1 or 2), last_price (float),
        legs (list of {action, quantity, price, pricetype, product}, matching legs).

    Returns a dict with the three Kite top-level keys. Caller is responsible
    for JSON-encoding ``condition`` and ``orders`` and URL-encoding the form.
    """
    tradingsymbol = get_br_symbol(data["symbol"], data["exchange"])
    exchange = data["exchange"]

    condition = {
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "trigger_values": [float(p) for p in data["trigger_prices"]],
        "last_price": float(data["last_price"]),
    }

    orders = [_leg_to_order(leg, tradingsymbol, exchange) for leg in data["legs"]]

    return {
        "type": data["trigger_type"],  # "single" | "two-leg"
        "condition": condition,
        "orders": orders,
    }


def transform_modify_gtt(data):
    """Transform an OpenAlgo modify-GTT payload into Kite's `{type, condition, orders}`."""
    # Kite's PUT /gtt/triggers/:id takes the same shape as POST.
    return transform_place_gtt(data)


def map_gtt_book(gtt_data):
    """Normalise Kite's GET /gtt/triggers response into an OpenAlgo-shaped list.

    Kite returns ``{"status": "success", "data": [{...}, ...]}``. Each GTT has
    ``id``, ``user_id``, ``type``, ``status``, ``condition``, ``orders``, ``created_at``, ``updated_at``,
    ``expires_at``, ``meta``. We flatten to a broker-neutral shape and translate the
    Kite tradingsymbol back to OpenAlgo's symbol.
    """
    if not isinstance(gtt_data, dict):
        return []

    data = gtt_data.get("data") or []
    normalised = []

    for gtt in data:
        condition = gtt.get("condition") or {}
        orders = gtt.get("orders") or []
        exchange = condition.get("exchange", "")
        br_symbol = condition.get("tradingsymbol", "")
        oa_symbol = get_oa_symbol(brsymbol=br_symbol, exchange=exchange) if br_symbol else ""

        legs = []
        for order in orders:
            legs.append(
                {
                    "action": order.get("transaction_type", ""),
                    "quantity": order.get("quantity", 0),
                    "price": order.get("price", 0),
                    "pricetype": order.get("order_type", "LIMIT"),
                    "product": order.get("product", ""),
                }
            )

        normalised.append(
            {
                "trigger_id": str(gtt.get("id", "")),
                "trigger_type": gtt.get("type", ""),
                "status": gtt.get("status", ""),
                "symbol": oa_symbol or br_symbol,
                "exchange": exchange,
                "trigger_prices": condition.get("trigger_values", []),
                "last_price": condition.get("last_price", 0),
                "legs": legs,
                "created_at": gtt.get("created_at", ""),
                "updated_at": gtt.get("updated_at", ""),
                "expires_at": gtt.get("expires_at", ""),
            }
        )

    return normalised
