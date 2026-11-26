"""5paisa XTS live ticks reach the charts (issue #2493).

- The feed listens and subscribes on /feedmarketdata, where 5paisa streams.
- Ticks there are compressed `xts-binary-packet` frames, decoded per the XTS
  market data docs ("Binary Marketdata Event").
- Tick times are XTS time (seconds from 1980-01-01 00:00 IST), converted to
  Unix time before they reach the charts.
- LTP reads the 1501 touchline: /feedmarketdata sends no 1512.
- Daily history gets today's candle from the quote once the session trades.

The packets below are built field by field from the docs' tables, with the
5paisa sizes the issue reports: 204-byte 1501 and 432-byte 1502 payloads.
"""

import logging
import struct
import threading
import zlib
from collections import deque
from datetime import datetime, timedelta

import pytest
import pytz

import websocket_proxy  # noqa: F401  (resolves the adapter import cycle)
from broker.fivepaisaxts.streaming import fivepaisaxts_binary as binary

IST = pytz.timezone("Asia/Kolkata")
XTS_OFFSET = 315513000

# Fields of the 120-byte touchline block, in the docs' order.
_TAIL = struct.Struct("<Qdqqqqdqddddddhhhh")


def _prefix(code, segment, instrument, version=4):
    head = struct.pack("<HHHQ", code, version, 0, 0)
    if version >= 4:
        head += struct.pack("<Qi", 7, 0)
    return head + struct.pack("<hiQ", segment, instrument, 1_448_000_000)


def _row(size, price, orders):
    return struct.pack("<qdIh", size, price, orders, 0)


def _tail(ltp, ltt, open_=100.0, high=110.0, low=95.0, close=99.0, volume=5000):
    return _TAIL.pack(
        ltt, ltp, 25, 1000, 2000, volume, 104.5, ltt, 1.5, open_, high, low, close, 0.0, 0, 0, 1, 1
    )


def touchline_payload(segment, instrument, ltp, ltt, version=4):
    return (
        _prefix(1501, segment, instrument, version)
        + _row(10, ltp - 0.05, 2)
        + _row(15, ltp + 0.05, 3)
        + _tail(ltp, ltt)
    )


def depth_payload(segment, instrument, ltp, ltt):
    bids = b"".join(_row(100 + i, ltp - 0.05 * (i + 1), i + 1) for i in range(5))
    asks = b"".join(_row(200 + i, ltp + 0.05 * (i + 1), i + 1) for i in range(5))
    return (
        _prefix(1502, segment, instrument)
        + struct.pack("<i", 5)
        + bids
        + struct.pack("<i", 5)
        + asks
        + b"\x00" * 44  # what 5paisa sends between the ask rows and the touchline
        + _tail(ltp, ltt)
    )


def frame(code, segment, instrument, payload, compress=True, wbits=-15):
    if compress:
        packer = zlib.compressobj(wbits=wbits)
        data = packer.compress(payload) + packer.flush()
        return (
            struct.pack("<BHhihhH", 1, code, segment, instrument, 1, 1, len(payload))
            + struct.pack("<H", len(data))
            + data
        )
    return struct.pack("<BHhihhH", 0, code, segment, instrument, 1, 1, len(payload)) + payload


def packet(*frames):
    return b"\x04" + b"".join(frames)


# --------------------------------------------------------------- decoder


def test_payload_sizes_match_what_5paisa_sends():
    assert len(touchline_payload(1, 2885, 1300.0, 1)) == 204
    assert len(depth_payload(2, 35001, 24000.0, 1)) == 432


def test_touchline_frame_decodes_at_the_documented_offsets():
    ltt = 1_448_000_123
    [message] = binary.decode_packet(
        packet(frame(1501, 1, 2885, touchline_payload(1, 2885, 1305.45, ltt)))
    )

    touchline = message["Touchline"]
    assert (
        message["MessageCode"],
        message["ExchangeSegment"],
        message["ExchangeInstrumentID"],
    ) == (
        1501,
        1,
        2885,
    )
    assert touchline["LastTradedPrice"] == 1305.45
    assert touchline["LastTradedTime"] == ltt  # XTS time, as sent
    assert (touchline["Open"], touchline["High"], touchline["Low"], touchline["Close"]) == (
        100.0,
        110.0,
        95.0,
        99.0,
    )
    assert touchline["TotalTradedQuantity"] == 5000
    assert touchline["BidInfo"] == {"Size": 10, "Price": 1305.4, "TotalOrders": 2}
    assert touchline["AskInfo"]["Price"] == 1305.5


def test_depth_frame_reads_five_levels_and_the_touchline_after_the_gap():
    [message] = binary.decode_packet(
        packet(frame(1502, 2, 35001, depth_payload(2, 35001, 24000.0, 1_448_000_000)))
    )

    assert [b["Size"] for b in message["Bids"]] == [100, 101, 102, 103, 104]
    assert [a["TotalOrders"] for a in message["Asks"]] == [1, 2, 3, 4, 5]
    assert message["Asks"][0]["Price"] == pytest.approx(24000.05)
    assert message["Touchline"]["LastTradedPrice"] == 24000.0
    assert message["Touchline"]["High"] == 110.0


def test_one_packet_carries_frames_for_several_instruments_in_any_encoding():
    raw = packet(
        frame(1501, 1, 11, touchline_payload(1, 11, 10.0, 1)),
        frame(1501, 1, 12, touchline_payload(1, 12, 20.0, 1), compress=False),
        frame(1501, 1, 13, touchline_payload(1, 13, 30.0, 1), wbits=15),  # zlib-wrapped
        frame(1510, 2, 14, b"\x00" * 60),  # open interest: not decoded, skipped
    )

    messages = binary.decode_packet(raw)

    assert [(m["ExchangeInstrumentID"], m["Touchline"]["LastTradedPrice"]) for m in messages] == [
        (11, 10.0),
        (12, 20.0),
        (13, 30.0),
    ]


def test_older_message_versions_have_no_sequence_number():
    [message] = binary.decode_packet(
        packet(frame(1501, 1, 5, touchline_payload(1, 5, 50.0, 1, version=3), compress=False))
    )
    assert message["Touchline"]["BidInfo"]["Size"] == 10
    assert message["Touchline"]["LastTradedPrice"] == 50.0


def test_a_cut_off_packet_keeps_the_frames_before_the_cut():
    whole = frame(1501, 1, 21, touchline_payload(1, 21, 1.0, 1))
    cut = frame(1501, 1, 22, touchline_payload(1, 22, 2.0, 1))[:-10]

    messages = binary.decode_packet(packet(whole, cut))

    assert [m["ExchangeInstrumentID"] for m in messages] == [21]


def test_garbage_and_non_bytes_decode_to_nothing():
    assert binary.decode_packet(b"\x04\x01\x02") == []
    assert binary.decode_packet("t:1_22,110:1") == []
    assert binary.decode_packet(None) == []


def test_xts_time_converts_to_unix_time():
    # The docs' sample quote: LastTradedTime 1453476469 is 2026, not 2016.
    unix = binary.xts_to_unix(1453476469)
    assert unix == 1453476469 + XTS_OFFSET
    assert datetime.fromtimestamp(unix, IST).year == 2026
    assert binary.xts_to_unix(0) == 0
    assert binary.xts_to_unix(None) == 0


# ---------------------------------------------------------------- client


def _client():
    from broker.fivepaisaxts.streaming.fivepaisaxts_websocket import FivepaisaXTSWebSocketClient

    client = FivepaisaXTSWebSocketClient("k", "s", "admin", feed_token_provider=lambda: None)
    client.connected = True
    client.market_data_token = "T"
    return client


def test_feed_listens_and_subscribes_on_feedmarketdata():
    from broker.fivepaisaxts.baseurl import BASE_URL
    from broker.fivepaisaxts.streaming.fivepaisaxts_websocket import FivepaisaXTSWebSocketClient

    assert FivepaisaXTSWebSocketClient.SOCKET_PATH == "/feedmarketdata/socket.io"
    assert FivepaisaXTSWebSocketClient.API_BASE_URL == (
        f"{BASE_URL}/feedmarketdata/instruments/subscription"
    )
    assert FivepaisaXTSWebSocketClient.API_UNSUBSCRIBE_URL == (
        FivepaisaXTSWebSocketClient.API_BASE_URL
    )


def test_ltp_mode_subscribes_to_the_touchline(monkeypatch):
    from broker.fivepaisaxts.streaming import fivepaisaxts_websocket as ws

    sent = []

    class _Ok:
        status_code = 200

        def json(self):
            return {"type": "success", "result": {"listQuotes": []}}

    monkeypatch.setattr(ws.requests, "post", lambda url, json=None, **k: sent.append(json) or _Ok())
    client = _client()

    client.subscribe("batch_mode_1_1", 1, [{"exchangeSegment": 1, "exchangeInstrumentID": "26000"}])

    assert sent[0]["xtsMessageCode"] == 1501


def test_binary_handler_is_registered_and_passes_only_subscribed_frames():
    client = _client()
    client._remember(2, 1501, [{"exchangeSegment": 1, "exchangeInstrumentID": "2885"}])
    seen = []
    client.on_data = lambda _c, message: seen.append(message["ExchangeInstrumentID"])

    assert "xts-binary-packet" in client.sio.handlers["/"]
    client.sio.handlers["/"]["xts-binary-packet"](
        packet(
            frame(1501, 1, 2885, touchline_payload(1, 2885, 1.0, 1)),
            frame(1501, 1, 9999, touchline_payload(1, 9999, 2.0, 1)),
        )
    )

    assert seen == [2885]
    assert client.last_message_time is not None  # the stall watchdog sees binary ticks


# --------------------------------------------------------------- adapter


class _Adapter:
    """The real adapter, built without ZeroMQ, publishing into a list."""

    def __new__(cls, subscriptions=()):
        from broker.fivepaisaxts.streaming import fivepaisaxts_adapter as adapter_mod

        adapter = adapter_mod.FivepaisaXTSWebSocketAdapter.__new__(
            adapter_mod.FivepaisaXTSWebSocketAdapter
        )
        adapter.logger = logging.getLogger("test_fivepaisaxts_2493")
        adapter.lock = threading.Lock()
        adapter.pending_subscriptions = deque()
        adapter.connected = False
        adapter.ws_client = None
        adapter.subscriptions = {}
        for mode in subscriptions:
            adapter.subscriptions[f"RELIANCE_NSE_{mode}"] = {
                "mode": mode,
                "instruments": [{"exchangeSegment": 1, "exchangeInstrumentID": "2885"}],
            }
        adapter.published = []
        adapter.publish_market_data = lambda topic, data: adapter.published.append((topic, data))
        return adapter


@pytest.fixture
def symbol_lookup(monkeypatch):
    from broker.fivepaisaxts.streaming import fivepaisaxts_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod, "get_symbol", lambda token, exchange: "RELIANCE" if exchange == "NSE" else None
    )


def _decoded(ltp=1305.45, ltt=1_448_000_000):
    [message] = binary.decode_packet(
        packet(frame(1501, 1, 2885, touchline_payload(1, 2885, ltp, ltt)))
    )
    return message


def test_a_touchline_tick_reaches_both_ltp_and_quote_subscribers(symbol_lookup):
    adapter = _Adapter(subscriptions=(1, 2))

    adapter._on_data(None, _decoded())

    topics = [topic for topic, _ in adapter.published]
    assert topics == ["NSE_RELIANCE_LTP", "NSE_RELIANCE_QUOTE"]
    assert adapter.published[0][1]["ltp"] == 1305.45


def test_a_touchline_tick_with_no_mode_on_record_goes_to_quote(symbol_lookup):
    adapter = _Adapter()

    adapter._on_data(None, _decoded())

    assert [topic for topic, _ in adapter.published] == ["NSE_RELIANCE_QUOTE"]


def test_tick_time_reaches_the_charts_as_unix_time(symbol_lookup):
    now = datetime(2026, 11, 26, 10, 15, 3, tzinfo=IST)
    adapter = _Adapter(subscriptions=(2,))

    adapter._on_data(None, _decoded(ltt=int(now.timestamp()) - XTS_OFFSET))

    assert adapter.published[0][1]["ltt"] == int(now.timestamp())


def test_depth_frame_publishes_the_ladder(symbol_lookup):
    adapter = _Adapter(subscriptions=(3,))
    [message] = binary.decode_packet(
        packet(frame(1502, 1, 2885, depth_payload(1, 2885, 1300.0, 1_448_000_000)))
    )

    adapter._on_data(None, message)

    [(topic, data)] = adapter.published
    assert topic == "NSE_RELIANCE_DEPTH"
    assert [level["quantity"] for level in data["depth"]["buy"]] == [100, 101, 102, 103, 104]
    assert data["depth"]["sell"][0]["price"] == pytest.approx(1300.05)


class _Feed:
    connected = True

    def __init__(self):
        self.unsubscribed = []

    def unsubscribe(self, correlation_id, mode, instruments):
        self.unsubscribed.append(mode)


def _unsubscribing_adapter(monkeypatch, modes):
    from broker.fivepaisaxts.streaming import fivepaisaxts_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod.SymbolMapper,
        "get_token_from_symbol",
        lambda symbol, exchange: {"token": "2885", "brexchange": "NSECM"},
    )
    monkeypatch.setattr(
        adapter_mod.FivepaisaXTSExchangeMapper, "get_exchange_type", lambda brexchange: 1
    )
    adapter = _Adapter(subscriptions=modes)
    adapter.connected = True
    adapter.ws_client = _Feed()
    adapter._create_success_response = lambda message, **kw: {"status": "success"}
    return adapter


def test_dropping_ltp_keeps_the_touchline_a_quote_still_uses(monkeypatch):
    adapter = _unsubscribing_adapter(monkeypatch, modes=(1, 2))

    adapter.unsubscribe("RELIANCE", "NSE", 1)

    assert adapter.ws_client.unsubscribed == []  # 1501 stays for the quote
    assert "RELIANCE_NSE_1" not in adapter.subscriptions


def test_dropping_the_last_touchline_mode_unsubscribes_it(monkeypatch):
    adapter = _unsubscribing_adapter(monkeypatch, modes=(1, 2))

    adapter.unsubscribe("RELIANCE", "NSE", 1)
    adapter.unsubscribe("RELIANCE", "NSE", 2)

    assert adapter.ws_client.unsubscribed == [2]


def test_depth_unsubscribes_on_its_own_code(monkeypatch):
    adapter = _unsubscribing_adapter(monkeypatch, modes=(2, 3))

    adapter.unsubscribe("RELIANCE", "NSE", 3)

    assert adapter.ws_client.unsubscribed == [3]


# ------------------------------------------------------- daily history


def _history_handler(monkeypatch, quote_ltt, daily_rows):
    import json

    from broker.fivepaisaxts.api import data as fp_data

    calls = []

    def fake_api(endpoint, auth, method="GET", payload="", feed_token=None, params=None):
        calls.append(endpoint)
        if endpoint == "/instruments/ohlc":
            return {"type": "success", "result": {"dataReponse": daily_rows}}
        quote = {
            "Touchline": {
                "Open": 1300.0,
                "High": 1320.0,
                "Low": 1295.0,
                "LastTradedPrice": 1310.0,
                "TotalTradedQuantity": 777,
                "LastTradedTime": quote_ltt,
            }
        }
        return {"type": "success", "result": {"listQuotes": [json.dumps(quote)]}}

    class _Row:
        token = "2885"

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def query(self, model):
            return self

        def filter(self, *args):
            return self

        def first(self):
            return _Row()

    monkeypatch.setattr(fp_data, "get_api_response", fake_api)
    monkeypatch.setattr(fp_data, "get_br_symbol", lambda symbol, exchange: "RELIANCE")
    monkeypatch.setattr(fp_data, "db_session", lambda: _Session())
    return fp_data.BrokerData("auth", feed_token="F"), calls


def _utc_midnight(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=pytz.utc).timestamp())


def _xts(moment):
    return int(moment.timestamp()) - XTS_OFFSET


def test_daily_history_gets_todays_candle_once_the_session_trades(monkeypatch):
    today = datetime.now(IST)
    yesterday = (today - timedelta(days=1)).date()
    rows = f"{_utc_midnight(yesterday)}|1290|1300|1280|1295|1000|0|"
    handler, calls = _history_handler(monkeypatch, _xts(today), rows)

    df = handler.get_history("RELIANCE", "NSE", "D", str(yesterday), str(today.date()))

    assert list(df["timestamp"]) == [_utc_midnight(yesterday), _utc_midnight(today.date())]
    assert (df["open"].iloc[-1], df["high"].iloc[-1], df["close"].iloc[-1]) == (1300, 1320, 1310)
    assert calls[-1] == "/instruments/quotes"


def test_a_quote_from_the_previous_session_adds_no_candle(monkeypatch):
    today = datetime.now(IST)
    yesterday = (today - timedelta(days=1)).date()
    rows = f"{_utc_midnight(yesterday)}|1290|1300|1280|1295|1000|0|"
    handler, _ = _history_handler(monkeypatch, _xts(today - timedelta(days=1)), rows)

    df = handler.get_history("RELIANCE", "NSE", "D", str(yesterday), str(today.date()))

    assert list(df["timestamp"]) == [_utc_midnight(yesterday)]


def test_history_that_already_has_today_asks_for_no_quote(monkeypatch):
    today = datetime.now(IST)
    rows = f"{_utc_midnight(today.date())}|1290|1300|1280|1295|1000|0|"
    handler, calls = _history_handler(monkeypatch, _xts(today), rows)

    df = handler.get_history("RELIANCE", "NSE", "D", str(today.date()), str(today.date()))

    assert len(df) == 1 and "/instruments/quotes" not in calls


def test_history_ending_before_today_is_left_alone(monkeypatch):
    today = datetime.now(IST)
    earlier = (today - timedelta(days=10)).date()
    rows = f"{_utc_midnight(earlier)}|1290|1300|1280|1295|1000|0|"
    handler, calls = _history_handler(monkeypatch, _xts(today), rows)

    df = handler.get_history("RELIANCE", "NSE", "D", str(earlier), str(earlier))

    assert len(df) == 1 and "/instruments/quotes" not in calls
