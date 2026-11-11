"""Decoding Kite's binary packets, and refusing to be fooled by them.

The first fixture is a real message captured from ``wss://ws.zerodha.com`` on
2026-09-21 for RELIANCE (token 738561) while the market was open, with the
values checked against the same minute's candles from the historical API.

The rest of the module is about the packets that are *not* fine. An earlier
iteration of this framework decoded a price of about minus one crore rupees
from a malformed packet, raised out of the tick callback, and lost the
websocket for all 200 symbols. These tests pin the behaviour that replaced it:
implausible data is dropped and counted, and nothing escapes into the loop.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from module1.broker.feed import decode_message

pytestmark = pytest.mark.unit

DATA_DIR = Path(__file__).with_name("data")

#: The seven symbols in the captured batch, in the order they arrive.
BATCH_TOKENS = [738561, 2953217, 341249, 408065, 779521, 424961, 4598529]
BATCH_PRICES = [
    Decimal("1247.40"),
    Decimal("2128.70"),
    Decimal("739.50"),
    Decimal("1038.50"),
    Decimal("996.00"),
    Decimal("267.00"),
    Decimal("1385.00"),
]

#: One full-mode message: 1 packet of 184 bytes, RELIANCE.
REAL_FULL_MESSAGE = (
    "000100b8000b45010001e334000000010001e464001db07500098cd9000994e8"
    "0001e2120001e6180001e2120001df106ab0b2ef000000000000000000000000"
    "6ab0b2f5000001f30001e35200010000000004100001e3480003000000000580"
    "0001e33e00110000000013e40001e33400580000000003790001e32a00080000"
    "000005a00001e35c0009000000000bf00001e366000f0000000002f10001e370"
    "000c0000000005590001e37a00140000000005920001e38400120000"
)

RELIANCE_TOKEN = 738561


def _message(token: int, price_paise: int, *, size: int = 184) -> bytes:
    """Build one packet the way the wire format does, so a test can lie."""

    packet = bytearray(size)
    struct.pack_into(">I", packet, 0, token)
    struct.pack_into(">i", packet, 4, price_paise)
    return struct.pack(">HH", 1, size) + bytes(packet)


def test_a_real_packet_decodes_to_the_exchange_price() -> None:
    result = decode_message(bytes.fromhex(REAL_FULL_MESSAGE), expected_tokens={RELIANCE_TOKEN})

    assert result.error is None
    assert result.dropped == 0
    assert len(result.ticks) == 1
    tick = result.ticks[0]
    assert tick.instrument_token == str(RELIANCE_TOKEN)
    assert tick.price == Decimal("1237.00")
    assert tick.exchange_time == datetime.fromtimestamp(1789965045, tz=UTC)


def test_a_packet_for_an_unsubscribed_instrument_is_dropped() -> None:
    result = decode_message(bytes.fromhex(REAL_FULL_MESSAGE), expected_tokens={999999})

    assert result.ticks == []
    assert result.dropped == 1


def test_an_impossible_price_is_dropped_rather_than_raised() -> None:
    """The exact shape of the packet that used to kill the whole feed.

    A packet whose price field holds something that is not a price - here the
    negative integer that produced ``low = -11284643.79`` - must be counted
    and discarded. Nothing about it may reach the caller as an exception.
    """

    poisoned = _message(RELIANCE_TOKEN, -1128464379)

    result = decode_message(poisoned, expected_tokens={RELIANCE_TOKEN})

    assert result.error is None
    assert result.ticks == []
    assert result.dropped == 1


def test_a_price_that_jumps_far_from_the_last_one_is_dropped() -> None:
    result = decode_message(
        _message(RELIANCE_TOKEN, 123700),
        expected_tokens={RELIANCE_TOKEN},
        last_prices={RELIANCE_TOKEN: Decimal("500")},
    )

    assert result.ticks == []
    assert result.dropped == 1


def test_a_rubbish_header_is_ignored_rather_than_fatal() -> None:
    assert decode_message(b"").ticks == []
    assert decode_message(b"\x00").ticks == []
    # A header that promises more packets than the body carries.
    assert decode_message(b"\x00\x02\x00\xb8" + b"\x00" * 10).ticks == []
    # A packet size of zero would divide nothing and repeat for ever.
    assert decode_message(b"\x00\x01\x00\x00").ticks == []


def test_a_short_trailing_packet_is_dropped_and_the_good_one_kept() -> None:
    good = _message(RELIANCE_TOKEN, 123700)
    # Two packets: the first as the header describes it, the second announced
    # by its length word but cut short.
    truncated = struct.pack(">HH", 2, 184) + good[4:] + struct.pack(">H", 184) + b"\x00" * 10

    result = decode_message(truncated, expected_tokens={RELIANCE_TOKEN})

    assert len(result.ticks) == 1
    assert result.dropped == 1


def test_a_real_batch_decodes_every_packet() -> None:
    """The captured seven-packet message, decoded end to end.

    This is the case that single-packet fixtures never covered: it only decodes
    correctly when each packet after the first is found by its own length word.
    """

    payload = bytes.fromhex((DATA_DIR / "ws_full_batch_7packets.hex").read_text().strip())

    result = decode_message(payload, expected_tokens=set(BATCH_TOKENS))

    assert result.error is None
    assert result.dropped == 0
    assert [int(tick.instrument_token) for tick in result.ticks] == BATCH_TOKENS
    assert [tick.price for tick in result.ticks] == BATCH_PRICES


def test_the_header_size_is_not_the_stride_between_packets() -> None:
    """Pin the mistake that produced the bad prices.

    Reading the second packet at ``4 + size`` finds the two-byte length word
    rather than a token, so the "token" it reports is a shifted number and the
    "price" that follows is a field from the middle of the real packet.
    """

    payload = bytes.fromhex((DATA_DIR / "ws_full_batch_7packets.hex").read_text().strip())
    count, size = struct.unpack_from(">HH", payload, 0)

    naive_token = struct.unpack_from(">I", payload, 4 + size)[0]
    naive_price = Decimal(struct.unpack_from(">i", payload, 4 + size + 4)[0]) / 100

    assert naive_token != BATCH_TOKENS[1]
    assert naive_price > Decimal("100000"), "the naive offset reads something that is not a price"

    aligned = decode_message(payload, expected_tokens=set(BATCH_TOKENS))
    assert [int(tick.instrument_token) for tick in aligned.ticks] == BATCH_TOKENS


def test_a_batch_of_unknown_instruments_yields_nothing_but_is_not_an_error() -> None:
    payload = bytes.fromhex((DATA_DIR / "ws_full_batch_7packets.hex").read_text().strip())

    result = decode_message(payload, expected_tokens={111111})

    assert result.error is None
    assert result.ticks == []
    assert result.dropped == 7
