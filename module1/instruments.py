"""Symbol to instrument-token resolution.

Every broker call needs an instrument token, and NSE reissues tokens when an
instrument changes, so no token is ever written into source. The public
instrument dump is the source of truth: it needs no authentication at all, so
it is fetched directly, cached on disk with the time it was fetched, and
re-read at start-up.

The cache exists for two reasons: a restart should not spend a megabyte on
something that changes once a day, and the module should still start when the
network is down.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx

from module1.timeutil import parse_timestamp, to_utc, utc_now

#: The public dump. No API key, no session, no authentication of any kind.
INSTRUMENT_DUMP_URL: Final[str] = "https://api.kite.trade/instruments/NSE"

#: How long a cached dump is trusted before it is refetched.
DEFAULT_MAX_AGE_HOURS: Final[float] = 12.0

DUMP_FILENAME: Final[str] = "instruments_nse.csv"
META_FILENAME: Final[str] = "instruments_nse.meta.json"


class InstrumentError(RuntimeError):
    """The instrument master could not be resolved."""


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    """One tradable instrument, as the broker describes it."""

    instrument_token: str
    symbol: str
    name: str
    tick_size: str | None
    lot_size: int | None
    instrument_type: str
    segment: str

    def describe(self) -> dict[str, Any]:
        return {
            "instrument_token": self.instrument_token,
            "symbol": self.symbol,
            "name": self.name,
            "tick_size": self.tick_size,
            "lot_size": self.lot_size,
            "instrument_type": self.instrument_type,
            "segment": self.segment,
        }


class InstrumentMaster:
    """Fetch, cache and query the broker's instrument dump."""

    def __init__(
        self,
        data_dir: Path,
        *,
        url: str = INSTRUMENT_DUMP_URL,
        client: httpx.Client | None = None,
        max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._url = url
        self._client = client
        self._owns_client = client is None
        self._max_age = timedelta(hours=max(0.0, float(max_age_hours)))
        self._clock = clock
        self._records: dict[str, InstrumentRecord] | None = None
        self._fetched_at: datetime | None = None
        self._last_error: str | None = None
        self._source: str | None = None

    # -- paths -------------------------------------------------------------- #

    @property
    def dump_path(self) -> Path:
        return self._data_dir / DUMP_FILENAME

    @property
    def meta_path(self) -> Path:
        return self._data_dir / META_FILENAME

    # -- loading ------------------------------------------------------------ #

    def load(self, *, force: bool = False) -> dict[str, InstrumentRecord]:
        """The instrument map, refetching only when the cache is stale."""

        if self._records is not None and not force:
            return self._records

        if force or self._is_stale():
            try:
                self._fetch()
            except Exception as exc:  # noqa: BLE001 - a stale cache beats no data
                self._last_error = f"{type(exc).__name__}: {str(exc)[:150]}"
                if not self.dump_path.exists():
                    raise InstrumentError(
                        f"the instrument dump could not be fetched ({self._last_error}) "
                        "and no cached copy exists"
                    ) from exc
                self._read_cache(note="stale")
                return self._records or {}

        if self._records is None:
            if not self.dump_path.exists():
                raise InstrumentError("no instrument dump is cached")
            self._read_cache()
        return self._records or {}

    def resolve(
        self, symbols: Iterable[str]
    ) -> tuple[dict[str, InstrumentRecord], list[str]]:
        """Map symbols to instruments, reporting the ones that did not resolve."""

        records = self.load()
        found: dict[str, InstrumentRecord] = {}
        missing: list[str] = []
        for symbol in symbols:
            key = str(symbol).strip().upper()
            record = records.get(key)
            if record is None:
                missing.append(key)
            else:
                found[key] = record
        return found, missing

    def index_record(self, symbol: str) -> InstrumentRecord:
        """Resolve an index from the same dump.

        The symbol map is deliberately EQ-only: an index, a future and an
        equity can share a trading symbol, and subscribing to the wrong one
        would be a market-data bug with no error. A benchmark is the one caller
        that wants an index, so it says so by segment and gets a row the map
        would never hand out.
        """

        self.load()
        key = str(symbol).strip().upper()
        try:
            text = self.dump_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InstrumentError("the cached instrument dump is unreadable") from exc
        for row in csv.DictReader(io.StringIO(text)):
            if str(row.get("segment") or "").strip().upper() != "INDICES":
                continue
            if str(row.get("tradingsymbol") or "").strip().upper() != key:
                continue
            lot_size: int | None
            try:
                lot_size = int(str(row.get("lot_size") or "").strip())
            except ValueError:
                lot_size = None
            return InstrumentRecord(
                instrument_token=str(row.get("instrument_token") or "").strip(),
                symbol=key,
                name=str(row.get("name") or key).strip(),
                tick_size=str(row.get("tick_size") or "").strip() or None,
                lot_size=lot_size,
                instrument_type="INDICES",
                segment="INDICES",
            )
        raise InstrumentError(f"the instrument dump has no index called {symbol!r}")

    # -- status ------------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        return {
            "source": self._source,
            "url": self._url,
            "dump_path": str(self.dump_path),
            "cached": self.dump_path.exists(),
            "fetched_at": None if self._fetched_at is None else self._fetched_at.isoformat(),
            "symbols": 0 if self._records is None else len(self._records),
            "last_error": self._last_error,
        }

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    # -- internals ---------------------------------------------------------- #

    def _is_stale(self) -> bool:
        meta = self._read_meta()
        if meta is None:
            return True
        try:
            fetched_at = parse_timestamp(str(meta["fetched_at"]))
        except (KeyError, ValueError):
            return True
        return to_utc(self._clock()) - fetched_at > self._max_age

    def _read_meta(self) -> dict[str, Any] | None:
        if not self.meta_path.exists():
            return None
        try:
            payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _fetch(self) -> None:
        client = self._client
        owns = False
        if client is None:
            client = httpx.Client(timeout=60.0, follow_redirects=True)
            owns = True
        try:
            response = client.get(self._url)
            response.raise_for_status()
            text = response.text
        finally:
            if owns:
                client.close()

        records = parse_instruments(text)
        if not records:
            raise InstrumentError("the instrument dump parsed to nothing")

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.dump_path.write_text(text, encoding="utf-8")
        fetched_at = to_utc(self._clock())
        self.meta_path.write_text(
            json.dumps(
                {
                    "source": self._url,
                    "fetched_at": fetched_at.isoformat(),
                    "symbols": len(records),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self._records = records
        self._fetched_at = fetched_at
        self._source = "broker"
        self._last_error = None

    def _read_cache(self, *, note: str = "cache") -> None:
        try:
            text = self.dump_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InstrumentError("the cached instrument dump is unreadable") from exc
        records = parse_instruments(text)
        if not records:
            raise InstrumentError("the cached instrument dump parsed to nothing")
        self._records = records
        meta = self._read_meta() or {}
        try:
            self._fetched_at = parse_timestamp(str(meta["fetched_at"]))
        except (KeyError, ValueError):
            self._fetched_at = None
        self._source = note


def parse_instruments(text: str) -> dict[str, InstrumentRecord]:
    """Parse the NSE instrument dump into a symbol-keyed map.

    Only ``EQ`` instruments on the ``NSE`` segment are kept: an index, a future
    and an equity can share a trading symbol, and almost every one of those
    duplicates is something this module must not accidentally subscribe to.
    """

    reader = csv.DictReader(io.StringIO(text))
    records: dict[str, InstrumentRecord] = {}
    for row in reader:
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        if not symbol:
            continue
        if str(row.get("instrument_type") or "").strip().upper() != "EQ":
            continue
        if str(row.get("segment") or "").strip().upper() != "NSE":
            continue
        lot_size: int | None
        try:
            lot_size = int(str(row.get("lot_size") or "").strip())
        except ValueError:
            lot_size = None
        tick_size = str(row.get("tick_size") or "").strip() or None
        records[symbol] = InstrumentRecord(
            instrument_token=str(row.get("instrument_token") or "").strip(),
            symbol=symbol,
            name=str(row.get("name") or symbol).strip(),
            tick_size=tick_size,
            lot_size=lot_size,
            instrument_type="EQ",
            segment="NSE",
        )
    return records
