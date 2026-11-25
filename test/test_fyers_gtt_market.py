"""Fyers GTT legs carry their order type (Fyers change log, 18 Nov 2026).

GTT Single and GTT OCO legs take ``type`` (1 Limit, the default; 2 Market,
price 0), Modify GTT takes it per leg (missing means Limit), the GTT Order
Book returns ``ord_type`` and ``ord_type2``, and Market is refused for BFO.
Before that change OpenAlgo turned every MARKET GTT into a protected LIMIT.
"""

import broker.fyers.api.gtt_api as gtt_api
import broker.fyers.mapping.gtt_data as gtt_data


def _place(monkeypatch, **extra):
    monkeypatch.setattr(gtt_data, "get_br_symbol", lambda s, e: f"{e}:{s}-EQ")
    data = {
        "symbol": "SBIN",
        "exchange": "NSE",
        "action": "BUY",
        "product": "CNC",
        "quantity": 3,
        "trigger_type": "SINGLE",
        "trigger_price": 800,
        "price": 805,
        "pricetype": "LIMIT",
    } | extra
    return gtt_data.transform_place_gtt(data)


def test_a_limit_leg_says_limit(monkeypatch):
    leg = _place(monkeypatch)["orderInfo"]["leg1"]
    assert leg == {"price": 805.0, "triggerPrice": 800.0, "qty": 3, "type": 1}


def test_a_market_leg_is_sent_as_market_with_price_zero(monkeypatch):
    leg = _place(monkeypatch, pricetype="MARKET")["orderInfo"]["leg1"]
    assert leg == {"price": 0.0, "triggerPrice": 800.0, "qty": 3, "type": 2}


def test_both_oco_legs_follow_the_order_type(monkeypatch):
    info = _place(
        monkeypatch,
        trigger_type="OCO",
        pricetype="MARKET",
        triggerprice_tg=900,
        triggerprice_sl=700,
        target=905,
        stoploss=695,
    )["orderInfo"]
    assert info["leg1"]["type"] == info["leg2"]["type"] == 2
    assert info["leg1"]["triggerPrice"] == 900.0 and info["leg2"]["triggerPrice"] == 700.0


def test_modify_names_the_type_so_a_market_leg_can_go_back_to_limit(monkeypatch):
    body = gtt_data.transform_modify_gtt(
        {"trigger_id": "25012400002099", "quantity": 3, "trigger_price": 800, "price": 805}
    )
    assert body["orderInfo"]["leg1"]["type"] == 1


def test_market_is_sent_natively_outside_bfo(monkeypatch):
    monkeypatch.setattr(
        gtt_api, "_fetch_last_price", lambda *a: (_ for _ in ()).throw(AssertionError("no LTP"))
    )
    data = {"pricetype": "MARKET", "exchange": "NSE", "symbol": "SBIN", "action": "BUY"}

    gtt_api._apply_mpp_if_market(data, "token")

    assert data["pricetype"] == "MARKET"


def test_bfo_market_is_still_a_protected_limit(monkeypatch):
    monkeypatch.setattr(gtt_api, "get_symbol_info", lambda s, e: None)
    monkeypatch.setattr(gtt_api, "_fetch_last_price", lambda *a: 100.0)
    monkeypatch.setattr(gtt_api, "calculate_protected_price", lambda **kw: 102.0)
    data = {
        "pricetype": "MARKET",
        "exchange": "BFO",
        "symbol": "SENSEX25DEC85000CE",
        "action": "BUY",
        "trigger_type": "SINGLE",
    }

    gtt_api._apply_mpp_if_market(data, "token")

    assert data["pricetype"] == "LIMIT" and data["price"] == 102.0


def test_the_book_reads_each_legs_order_type(monkeypatch):
    monkeypatch.setattr(gtt_data, "get_exchange", lambda exchange, segment: "NSE")
    monkeypatch.setattr(gtt_data, "get_oa_symbol", lambda brsymbol, exchange: "SBIN")
    row = {
        "id": "1",
        "ord_status": 6,
        "gtt_oco_ind": 2,
        "symbol": "NSE:SBIN-EQ",
        "tran_side": 1,
        "product_type": "CNC",
        "price_limit": 0,
        "price_trigger": 900,
        "qty": 3,
        "ord_type": 2,
        "price2_limit": 695,
        "price2_trigger": 700,
        "qty2": 3,
        "ord_type2": 1,
    }
    legs = gtt_data.map_gtt_book({"orderBook": [row]})[0]["legs"]
    assert [leg["pricetype"] for leg in legs] == ["MARKET", "LIMIT"]

    # A book from before the field existed reads as Limit.
    del row["ord_type"], row["ord_type2"]
    legs = gtt_data.map_gtt_book({"orderBook": [row]})[0]["legs"]
    assert [leg["pricetype"] for leg in legs] == ["LIMIT", "LIMIT"]
