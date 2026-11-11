"""The live price feed.

Kite's website API has no quote route, so live prices come over the same
websocket the website itself uses: ``wss://ws.zerodha.com`` with the API key
``kite3``, the user id and the session token. Each binary message carries a
big-endian packet count, the size of one packet, and then the packets.

The decoder here is deliberately suspicious of its own input. An earlier
iteration of this framework let one malformed packet raise out of the tick
callback, which ended the websocket session and cost the feed for *every*
symbol at once. So a packet is only accepted when it names an instrument that
was actually subscribed *and* carries a price that is plausible for it;
anything else is counted and dropped, and no decoding failure is allowed to
escape into the connection loop.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote

from module1.broker.session import SessionManager
from module1.timeutil import utc_now

logger = logging.getLogger("module1.feed")

#: Kite's website is itself a client of this key.
WEB_API_KEY: Final[str] = "kite3"
WEB_WS_URL: Final[str] = "wss://ws.zerodha.com"

#: Packet sizes, per subscription mode.
PACKET_SIZES: Final[dict[str, int]] = {"ltp": 8, "quote": 44, "full": 184}

#: A price outside this band is not a price. NSE equities trade between a few
#: paise and a few lakh rupees; anything else is a misdecoded packet.
MIN_PLAUSIBLE_PRICE: Final[Decimal] = Decimal("0.05")
MAX_PLAUSIBLE_PRICE: Final[Decimal] = Decimal("5000000")

#: A jump this large from the last accepted price for the same instrument is
#: treated as a decode error rather than a market event: no NIFTY 50
#: constituent moves 25% inside one session.
MAX_JUMP_RATIO: Final[Decimal] = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class Tick:
    """One accepted price update."""

    instrument_token: str
    price: Decimal
    received_at: datetime
    exchange_time: datetime | None = None
    volume: Decimal | None = None
    symbol: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "instrument_token": self.instrument_token,
            "symbol": self.symbol,
            "price": format(self.price, "f"),
            "exchange_time": (
                None if self.exchange_time is None else self.exchange_time.isoformat()
            ),
            "received_at": self.received_at.isoformat(),
            "volume": None if self.volume is None else format(self.volume, "f"),
        }


@dataclass(slots=True)
class FeedStatus:
    """What the feed is doing, safe to serve to the UI."""

    state: str = "not_started"
    connected: bool = False
    subscribed: int = 0
    ticks: int = 0
    dropped: int = 0
    decode_errors: int = 0
    reconnects: int = 0
    last_tick_at: datetime | None = None
    last_message_at: datetime | None = None
    last_error: str | None = None
    started_at: datetime | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "connected": self.connected,
            "subscribed": self.subscribed,
            "ticks": self.ticks,
            "dropped": self.dropped,
            "decode_errors": self.decode_errors,
            "reconnects": self.reconnects,
            "last_tick_at": (
                None if self.last_tick_at is None else self.last_tick_at.isoformat()
            ),
            "last_message_at": (
                None if self.last_message_at is None else self.last_message_at.isoformat()
            ),
            "last_error": self.last_error,
            "started_at": (
                None if self.started_at is None else self.started_at.isoformat()
            ),
        }


@dataclass(slots=True)
class DecodeResult:
    """What one binary message turned into."""

    ticks: list[Tick] = field(default_factory=list)
    dropped: int = 0
    error: str | None = None


def decode_message(
    payload: bytes,
    *,
    expected_tokens: set[int] | None = None,
    last_prices: dict[int, Decimal] | None = None,
    now: datetime | None = None,
) -> DecodeResult:
    """Decode one binary message into ticks, dropping anything implausible."""

    result = DecodeResult()
    moment = now or utc_now()
    try:
        if len(payload) < 4:
            return result
        count, size = struct.unpack_from(">HH", payload, 0)
        if size <= 0:
            return result
        offset = 4
        for index in range(count):
            if index > 0:
                # Every packet after the first is preceded by its own two-byte
                # length word. The header's ``size`` describes the packet that
                # follows it directly; it is *not* the stride between packets.
                # Walking by ``index * size`` puts every packet after the first
                # two bytes out of step, which decodes a token or a volume as
                # though it were a price - the failure this decoder exists to
                # prevent. Verified against a captured seven-packet message:
                # offsets 4, 190, 376, 562, 748, 934, 1120.
                offset, width = _read_length_word(payload, offset, fallback=size)
                if width is None:
                    result.dropped += 1
                    break
            else:
                width = size
            packet = payload[offset : offset + width]
            if len(packet) < width:
                # A short trailing packet is a truncated message, not a price.
                result.dropped += 1
                break
            offset += width
            tick = _decode_packet(
                packet,
                size=width,
                expected_tokens=expected_tokens,
                last_prices=last_prices,
                now=moment,
            )
            if tick is None:
                result.dropped += 1
                continue
            result.ticks.append(tick)
    except Exception as exc:  # noqa: BLE001 - a bad frame is data, not a crash
        result.error = f"{type(exc).__name__}: {exc}"[:200]
    return result


def _decode_packet(
    packet: bytes,
    *,
    size: int,
    expected_tokens: set[int] | None,
    last_prices: dict[int, Decimal] | None,
    now: datetime,
) -> Tick | None:
    token = _unsigned32(packet, 0)
    if expected_tokens is not None and token not in expected_tokens:
        return None
    price = Decimal(_signed32(packet, 4)) / 100
    if price < MIN_PLAUSIBLE_PRICE or price > MAX_PLAUSIBLE_PRICE:
        return None
    if last_prices is not None:
        previous = last_prices.get(token)
        if previous and previous > 0 and abs(price - previous) / previous > MAX_JUMP_RATIO:
            return None
    volume: Decimal | None = None
    if size >= PACKET_SIZES["quote"]:
        volume = Decimal(_unsigned32(packet, 16))
    exchange_time: datetime | None = None
    if size >= PACKET_SIZES["full"]:
        # Two clocks arrive: when the last trade happened, and when the
        # exchange stamped the snapshot. The candle view cares about the
        # second, so a quiet minute still belongs to its own bucket.
        traded_at = _signed32(packet, 44)
        stamped_at = _signed32(packet, 60)
        moment_source = stamped_at if stamped_at > 0 else traded_at
        if moment_source > 0:
            try:
                exchange_time = datetime.fromtimestamp(moment_source, tz=UTC)
            except (OverflowError, OSError, ValueError):
                exchange_time = None
    return Tick(
        instrument_token=str(token),
        price=price,
        received_at=now,
        exchange_time=exchange_time,
        volume=volume,
    )


def _unsigned32(view: bytes, offset: int) -> int:
    return struct.unpack_from(">I", view, offset)[0]


def _signed32(view: bytes, offset: int) -> int:
    return struct.unpack_from(">i", view, offset)[0]


def _read_length_word(payload: bytes, offset: int, *, fallback: int) -> tuple[int, int | None]:
    """Consume the two-byte length word that precedes a packet.

    Returns the new offset and the width to read. A length word that is not a
    plausible packet size is not trusted: the header's size is used instead,
    and a word that cannot be read at all ends the message.
    """

    if offset + 2 > len(payload):
        return offset, None
    declared = struct.unpack_from(">H", payload, offset)[0]
    offset += 2
    if declared >= PACKET_SIZES["ltp"] and offset + declared <= len(payload):
        return offset, declared
    if offset + fallback <= len(payload):
        return offset, fallback
    return offset, None


class KiteFeed:
    """A reconnecting websocket subscription to Kite's price stream."""

    def __init__(
        self,
        session: SessionManager,
        user_id: str,
        *,
        on_tick: Callable[[Tick], None] | None = None,
        mode: str = "full",
        url: str = WEB_WS_URL,
        clock: Callable[[], datetime] = utc_now,
        backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 60.0,
        sleeper: Callable[[float], None] = time.sleep,
        connector: Callable[..., Any] | None = None,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._on_tick = on_tick
        self._mode = mode if mode in PACKET_SIZES else "full"
        self._url = url.rstrip("/")
        self._clock = clock
        self._backoff = backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._sleep = sleeper
        self._connector = connector
        self._tokens: set[int] = set()
        self._symbol_by_token: dict[int, str] = {}
        self._last_prices: dict[int, Decimal] = {}
        self._status = FeedStatus()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._ws: Any = None

    # -- lifecycle ---------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return self._status.describe()

    def subscribe(self, tokens: Iterable[int | str]) -> None:
        """Replace the subscription set."""

        with self._lock:
            self._tokens = {int(token) for token in tokens}
            self._status.subscribed = len(self._tokens)
        self._apply_subscription()

    def register_symbols(self, token_to_symbol: dict[int, str]) -> None:
        """Let accepted ticks carry the symbol they belong to."""

        with self._lock:
            self._symbol_by_token = dict(token_to_symbol)

    def last_price(self, token: int | str) -> Decimal | None:
        with self._lock:
            return self._last_prices.get(int(token))

    def last_tick_at(self) -> datetime | None:
        with self._lock:
            return self._status.last_tick_at

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        with self._lock:
            self._status.state = "starting"
            self._status.started_at = utc_now()
        self._thread = threading.Thread(target=self._run, name="kite-feed", daemon=True)
        self._thread.start()
        return True

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        ws = self._ws
        try:
            if ws is not None:
                ws.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not an error
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._status.connected = False
            self._status.state = "stopped"

    # -- connection --------------------------------------------------------- #

    def connection_url(self) -> str:
        return (
            f"{self._url}/?api_key={WEB_API_KEY}"
            f"&user_id={quote(self._user_id, safe='')}"
            f"&enctoken={quote(self._session.token(), safe='')}"
        )

    def _run(self) -> None:
        attempts = 0
        while not self._stop.is_set():
            try:
                self._session_loop()
                attempts = 0
            except Exception as exc:  # noqa: BLE001 - the loop outlives any one socket
                if self._stop.is_set():
                    break
                attempts += 1
                with self._lock:
                    self._status.connected = False
                    self._status.reconnects += 1
                    self._status.state = f"reconnecting:{attempts}"
                    self._status.last_error = f"{type(exc).__name__}: {str(exc)[:150]}"
                if attempts > 50:
                    with self._lock:
                        self._status.state = "reconnect_exhausted"
                    return
                self._stop.wait(min(self._backoff * attempts, self._max_backoff))

    def _session_loop(self) -> None:
        connect = self._connector
        if connect is None:
            from websockets.sync.client import connect as websocket_connect

            connect = websocket_connect
        with connect(self.connection_url(), max_size=None, open_timeout=25) as ws:
            self._ws = ws
            with self._lock:
                self._status.connected = True
                self._status.state = "connected"
                self._status.last_error = None
            self._apply_subscription()
            while not self._stop.is_set():
                try:
                    message = ws.recv(timeout=5)
                except TimeoutError:
                    continue
                except ConnectionError as exc:
                    raise RuntimeError(f"connection lost: {type(exc).__name__}") from exc
                self._handle(message)
        self._ws = None
        with self._lock:
            self._status.connected = False
            self._status.state = "disconnected"

    def _apply_subscription(self) -> None:
        ws = self._ws
        if ws is None:
            return
        with self._lock:
            tokens = sorted(self._tokens)
        if not tokens:
            return
        try:
            ws.send(json.dumps({"a": "subscribe", "v": tokens}))
            ws.send(json.dumps({"a": "mode", "v": [self._mode, tokens]}))
        except Exception as exc:  # noqa: BLE001 - a send failure ends the session
            raise RuntimeError(f"subscribe failed: {type(exc).__name__}") from exc

    def _handle(self, message: Any) -> None:
        with self._lock:
            self._status.last_message_at = self._clock()
        if not isinstance(message, (bytes, bytearray)):
            # Text frames are control messages: the connection is alive, but
            # there is no price in them.
            return
        with self._lock:
            expected = set(self._tokens)
            last = dict(self._last_prices)
            symbols = dict(self._symbol_by_token)
        result = decode_message(
            bytes(message), expected_tokens=expected, last_prices=last, now=self._clock()
        )
        with self._lock:
            self._status.dropped += result.dropped
            if result.error:
                self._status.decode_errors += 1
                self._status.last_error = result.error
            accepted: list[Tick] = []
            for tick in result.ticks:
                token = int(tick.instrument_token)
                self._last_prices[token] = tick.price
                self._status.ticks += 1
                self._status.last_tick_at = tick.received_at
                accepted.append(
                    tick
                    if tick.symbol is not None
                    else Tick(
                        instrument_token=tick.instrument_token,
                        price=tick.price,
                        received_at=tick.received_at,
                        exchange_time=tick.exchange_time,
                        volume=tick.volume,
                        symbol=symbols.get(token),
                    )
                )
        for tick in accepted:
            if self._on_tick is not None:
                try:
                    self._on_tick(tick)
                except Exception as exc:  # noqa: BLE001 - a consumer bug is not fatal
                    logger.warning("tick consumer raised %s", type(exc).__name__)
