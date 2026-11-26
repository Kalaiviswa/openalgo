"""Decoder for 5paisa XTS `xts-binary-packet` market data (issue #2493).

5paisa streams ticks on /feedmarketdata as `xts-binary-packet` events, every
one of them compressed, whatever publishFormat asks for. Layout per the XTS
market data docs ("Binary Marketdata Event", broker-api-docs
xts-api-docs/24-marketdata-socket-streaming.md), little-endian:

    packet = [1 byte, value 4,] then frames
    frame  = isGzipCompressed  int8   (bit 0 = compressed)
             messageCode       uint16
             exchangeSegment   int16
             exchangeInstrumentID int32
             bookType          int16
             marketType        int16
             uncompressedPacketSize uint16
             compressedPacketSize   uint16  (only when compressed)
             payload: raw deflate when compressed (zlib accepted too)

The payload repeats the message code, then messageVersion, applicationType,
tokenID, sequenceNumber and SkipBytes (the last two only from version 4),
exchangeSegment, exchangeInstrumentId and exchangeTimestamp. 1501 then has
one bid and one ask row; 1502 has bidCount, the bid rows, askCount and the
ask rows. Both end with the same 120-byte touchline block (LastUpdateTime to
MarketType). It is read from the end of the payload: 5paisa's 1502 payloads
carry 44 more bytes between the ask rows and that block than the docs list.

Times are XTS time: seconds from 1980-01-01 00:00 IST. They are returned as
sent; xts_to_unix() converts them.
"""

from __future__ import annotations

import struct
import zlib

# Seconds from the Unix epoch to 1980-01-01 00:00 IST, the XTS time origin.
XTS_EPOCH_OFFSET = 315513000

_FRAME = struct.Struct("<BHhihhH")  # flag, code, segment, instrument, book, market, size
_U16 = struct.Struct("<H")
_I32 = struct.Struct("<i")
_ROW = struct.Struct("<qdIh")  # size, price, orders, market maker flag: 22 bytes
_TAIL = struct.Struct("<Qdqqqqdqddddddhhhh")  # 120 bytes, LastUpdateTime..MarketType

TOUCHLINE = 1501
DEPTH = 1502


def xts_to_unix(value) -> int:
    """XTS time (seconds from 1980-01-01 00:00 IST) as Unix seconds; 0 stays 0."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 0
    return seconds + XTS_EPOCH_OFFSET if seconds > 0 else 0


def _inflate(data: bytes, size: int) -> bytes | None:
    # The frame states its uncompressed size in a uint16, so no payload is
    # larger than 65535 bytes; never inflate past that.
    limit = size or 0xFFFF
    for wbits in (-15, 47):  # raw deflate as 5paisa sends it, then zlib/gzip
        try:
            out = zlib.decompressobj(wbits).decompress(data, limit)
        except zlib.error:
            continue
        return out if len(out) >= size else None
    return None


def iter_frames(packet: bytes):
    """Yield (message code, segment, instrument id, payload) for each frame.

    A frame that cannot be read ends the packet: what came before it is kept.
    """
    if not isinstance(packet, (bytes, bytearray, memoryview)):
        return
    packet = bytes(packet)
    pos = 1 if packet[:1] == b"\x04" else 0
    while pos + _FRAME.size <= len(packet):
        flag, code, segment, instrument, _book, _market, size = _FRAME.unpack_from(packet, pos)
        pos += _FRAME.size
        compressed = flag & 1
        if compressed:
            if pos + _U16.size > len(packet):
                return
            (length,) = _U16.unpack_from(packet, pos)
            pos += _U16.size
        else:
            length = size
        data = packet[pos : pos + length]
        pos += length
        if len(data) < length:
            return
        payload = _inflate(data, size) if compressed else data
        if payload is None:
            continue
        yield code, segment, instrument, payload


def _rows(payload: bytes, pos: int, count: int) -> list[dict]:
    rows = []
    for _ in range(count):
        size, price, orders, _flag = _ROW.unpack_from(payload, pos)
        rows.append({"Size": size, "Price": price, "TotalOrders": orders})
        pos += _ROW.size
    return rows


def _touchline(payload: bytes) -> dict:
    (
        last_update,
        ltp,
        ltq,
        total_buy,
        total_sell,
        volume,
        average,
        ltt,
        percent,
        open_,
        high,
        low,
        close,
        value,
        _bb_buy,
        _bb_sell,
        _book,
        _market,
    ) = _TAIL.unpack_from(payload, len(payload) - _TAIL.size)
    return {
        "LastUpdateTime": last_update,
        "LastTradedPrice": ltp,
        "LastTradedQuantity": ltq,
        "TotalBuyQuantity": total_buy,
        "TotalSellQuantity": total_sell,
        "TotalTradedQuantity": volume,
        "AverageTradedPrice": average,
        "LastTradedTime": ltt,
        "PercentChange": percent,
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "TotalValueTraded": value,
    }


def _rows_start(payload: bytes) -> int:
    """Offset just past exchangeTimestamp, where the bid/ask data begins."""
    (version,) = _U16.unpack_from(payload, 2)
    pos = 2 + 2 + 2 + 8  # messageCode, messageVersion, applicationType, tokenID
    if version >= 4:
        pos += 8 + 4  # sequenceNumber, SkipBytes
    return pos + 2 + 4 + 8  # exchangeSegment, exchangeInstrumentId, exchangeTimestamp


def decode_frame(code: int, segment: int, instrument: int, payload: bytes) -> dict | None:
    """One frame as the dictionary the JSON events carry, or None.

    Only 1501 (touchline) and 1502 (depth) are decoded; other codes, and a
    payload too short for its code, give None.
    """
    try:
        if code == TOUCHLINE:
            touchline = _touchline(payload)
            pos = _rows_start(payload)
            bid, ask = _rows(payload, pos, 2)
            touchline["BidInfo"], touchline["AskInfo"] = bid, ask
            return {
                "MessageCode": TOUCHLINE,
                "ExchangeSegment": segment,
                "ExchangeInstrumentID": instrument,
                "Touchline": touchline,
            }
        if code == DEPTH:
            pos = _rows_start(payload)
            (bid_count,) = _I32.unpack_from(payload, pos)
            bids = _rows(payload, pos + 4, max(0, min(bid_count, 20)))
            pos += 4 + _ROW.size * len(bids)
            (ask_count,) = _I32.unpack_from(payload, pos)
            asks = _rows(payload, pos + 4, max(0, min(ask_count, 20)))
            return {
                "MessageCode": DEPTH,
                "ExchangeSegment": segment,
                "ExchangeInstrumentID": instrument,
                "Bids": bids,
                "Asks": asks,
                "Touchline": _touchline(payload),
            }
    except struct.error:
        return None
    return None


def decode_packet(packet: bytes) -> list[dict]:
    """Every 1501 and 1502 frame in an `xts-binary-packet`, decoded."""
    messages = []
    for code, segment, instrument, payload in iter_frames(packet):
        message = decode_frame(code, segment, instrument, payload)
        if message is not None:
            messages.append(message)
    return messages
