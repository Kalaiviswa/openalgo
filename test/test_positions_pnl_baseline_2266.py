"""Position rows the Positions page can measure a live move from (issue #2266).

The page now keeps each row's backend P&L and adds
quantity * (live LTP - row ltp), instead of rebuilding P&L from
average_price. That is only right when the row's ``pnl`` is the whole P&L at
the row's ``ltp``. These pin the two plugins that broke it.
"""

from broker.dhan_sandbox.mapping.order_data import (
    transform_positions_data as dhan_sandbox_positions,
)
from broker.iiflcapital.mapping import order_data as iifl_order_data


def _iifl_row(**overrides):
    # Buy 100 @ 10, sell 100 @ 12, buy 50 @ 11. IIFL's documented
    # netAveragePrice = (overnight cost + dayBuyValue - daySellValue) / netQty
    # = (0 + 1550 - 1200) / 50 = 7.0, and realizedPnl = 200.
    row = {
        "exchange": "NSEEQ",
        "tradingSymbol": "SBIN",
        "netQuantity": 50,
        "netAveragePrice": 7.0,
        "realizedPnl": 200.0,
        "ltp": 11.0,
    }
    row.update(overrides)
    return row


def _iifl(rows, monkeypatch):
    # No symbol database in a unit test: keep the broker's own symbol.
    monkeypatch.setattr(iifl_order_data, "get_symbol", lambda *_: None)
    return iifl_order_data.transform_positions_data(rows)


def test_iifl_open_row_counts_realised_once(monkeypatch):
    # Cash flows: -1000 + 1200 - 550 = -350, plus 50 held at 11 = 550: P&L 200.
    # Adding realizedPnl to (ltp - netAveragePrice) * netQty reported 400.
    [row] = _iifl([_iifl_row()], monkeypatch)
    assert row["pnl"] == 200.0


def test_iifl_open_row_moves_with_ltp(monkeypatch):
    [row] = _iifl([_iifl_row(ltp=12.0)], monkeypatch)
    assert row["pnl"] == 250.0


def test_iifl_closed_row_reports_realised(monkeypatch):
    [row] = _iifl([_iifl_row(netQuantity=0, netAveragePrice=0, ltp=12.0)], monkeypatch)
    assert row["pnl"] == 200.0


def test_iifl_open_row_without_ltp_keeps_realised(monkeypatch):
    [row] = _iifl([_iifl_row(ltp=None, previousDayClose=None)], monkeypatch)
    assert row["pnl"] == 200.0


def _dhan_row(**overrides):
    row = {
        "tradingSymbol": "SBIN",
        "exchangeSegment": "NSE",
        "productType": "INTRADAY",
        "netQty": 10,
        "costPrice": 800.0,
        "closePrice": 790.0,
        "realizedProfit": 0.0,
        "unrealizedProfit": 50.0,
    }
    row.update(overrides)
    return row


def test_dhan_sandbox_does_not_report_cost_or_close_as_ltp():
    # Neither is the price unrealizedProfit was computed at, so the page would
    # measure the live move from the wrong place and count it twice.
    [row] = dhan_sandbox_positions([_dhan_row()])
    assert row["ltp"] == 0.0
    assert row["pnl"] == 50.0


def test_dhan_sandbox_reports_a_last_price_when_sent():
    [row] = dhan_sandbox_positions([_dhan_row(lastTradedPrice=805.0)])
    assert row["ltp"] == 805.0
