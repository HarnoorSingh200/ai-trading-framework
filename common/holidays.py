"""The exchange's holiday list, kept locally.

The NSE calendar is published data, not a secret and not a broker call. The list
lives in a local JSON file named by ``FRAMEWORK_HOLIDAYS_FILE``
(``data/holidays/nse.json`` by default) so a start-up does not depend on
nseindia.com being reachable. Module 1 keeps it fresh; the
*checked-in seed* - ``module1/data/nse_holidays.json`` - is what a fresh machine
reads until the first successful refresh from NSE's holiday master writes the
runtime file beside it.

The runner reads the file directly to decide whether Modules 2 and 3 get a
trading day. It does not ask Module 1, which keeps fetching market data on a
holiday just as it does on any other day. A holiday must never be mistaken for a
trading day because a service route was briefly down.

The file format, which the loader also tolerates as a bare list::

    {
      "source": "nse-holiday-master",
      "source_url": "https://www.nseindia.com/api/holiday-master?type=trading",
      "segment": "CM",
      "fetched_at": "2026-10-17T12:00:00+05:30",
      "holidays": [{"date": "2026-01-26", "name": "Republic Day"}]
    }

A ``date`` is the exchange day the cash market is closed; the timestamp says
when the list was fetched, because a list without one cannot be trusted to be
current. A holiday with a special *evening* session (Muhurat trading) is still
listed: the framework's day lives between 09:15 and 15:30, so the regular
session is what it waits for.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx

from common import env
from common.window import IST, ist_now

logger = logging.getLogger("common.holidays")

#: The live list: refreshed by Module 1, read by everything else.
DEFAULT_RELATIVE_PATH: Final[Path] = Path("data") / "holidays" / "nse.json"
#: The checked-in copy, used until the first refresh writes the live one.
DEFAULT_SEED_RELATIVE_PATH: Final[Path] = (
    Path("module1") / "data" / "nse_holidays.json"
)

#: The capital market segment is the cash market this framework trades.
SEGMENT_CM: Final[str] = "CM"
SOURCE_NSE: Final[str] = "nse-holiday-master"

NSE_HOLIDAYS_PAGE: Final[str] = (
    "https://www.nseindia.com/resources/exchange-communication-holidays"
)
NSE_HOLIDAYS_API: Final[str] = (
    "https://www.nseindia.com/api/holiday-master?type=trading"
)

#: NSE answers a request that does not look like a browser with a page, not an
#: error, so the fetch warms the site up and presents the headers it expects.
_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOLIDAYS_PAGE,
}

#: How far ahead :meth:`HolidayCalendar.next_trading_day` will look before it
#: gives up and returns the day it was given. Ten years is a sanity bound, not
#: a calendar: a list that says a decade of weekdays are all holidays is broken.
_SEARCH_DAYS: Final[int] = 3660


class HolidayError(RuntimeError):
    """The holiday list could not be read or fetched."""


class HolidayFetchError(HolidayError):
    """The list could not be fetched from NSE."""


@dataclass(frozen=True, slots=True)
class Holiday:
    """One exchange closure."""

    day: date
    name: str

    def describe(self) -> dict[str, str]:
        return {"date": self.day.isoformat(), "name": self.name}


def resolve_paths(root: Path) -> tuple[Path, Path]:
    """``(the live list, the checked-in seed)`` for a project root."""

    base = Path(root)
    configured = env.text("FRAMEWORK_HOLIDAYS_FILE", "")
    primary = Path(configured).expanduser() if configured else base / DEFAULT_RELATIVE_PATH
    if not primary.is_absolute():
        primary = base / primary
    return primary, base / DEFAULT_SEED_RELATIVE_PATH


class HolidayCalendar:
    """A read of one holiday list: the entries, and where they came from."""

    __slots__ = (
        "entries",
        "source",
        "source_url",
        "segment",
        "fetched_at",
        "path",
        "problem",
        "_by_day",
    )

    def __init__(
        self,
        holidays: Iterable[Holiday] = (),
        *,
        source: str | None = None,
        source_url: str | None = None,
        segment: str = SEGMENT_CM,
        fetched_at: datetime | None = None,
        path: Path | None = None,
        problem: str | None = None,
    ) -> None:
        self.entries: tuple[Holiday, ...] = tuple(
            sorted(holidays, key=lambda holiday: holiday.day)
        )
        self.source = source
        self.source_url = source_url
        self.segment = segment
        self.fetched_at = fetched_at
        self.path = Path(path) if path is not None else None
        self.problem = problem
        self._by_day = {holiday.day: holiday for holiday in self.entries}

    # -- questions ------------------------------------------------------------ #

    def on(self, day: date) -> Holiday | None:
        """The holiday on ``day``, if the list has one."""

        return self._by_day.get(day)

    def is_holiday(self, day: date) -> bool:
        return day in self._by_day

    def years(self) -> tuple[int, ...]:
        return tuple(sorted({holiday.day.year for holiday in self.entries}))

    def covers(self, day: date) -> bool:
        """Whether the list is *about* ``day``'s year.

        A holiday list can only say "no holiday" for a year it has heard of;
        without this, a machine that has never fetched the current year would
        present "no closures" as a fact rather than as an absence of data.
        """

        return day.year in {holiday.day.year for holiday in self.entries}

    def next_trading_day(self, day: date) -> date:
        """The first day on or after ``day`` that is not a weekend or holiday."""

        candidate = day
        for _ in range(_SEARCH_DAYS):
            if candidate.weekday() < 5 and not self.is_holiday(candidate):
                return candidate
            candidate += timedelta(days=1)
        return candidate

    def needs_refresh(self, *, now: datetime, refresh_seconds: float) -> bool:
        """Whether the list is old enough - or unknown enough - to refetch."""

        if self.fetched_at is None:
            return True
        if not self.covers(now.astimezone(IST).date()):
            return True
        age = (now - self.fetched_at).total_seconds()
        return age >= max(0.0, float(refresh_seconds))

    # -- presentation --------------------------------------------------------- #

    def describe(self, day: date) -> dict[str, Any]:
        """The holiday block a session clock publishes for ``day``."""

        entry = self.on(day)
        return {
            "holidays_modelled": self.covers(day),
            "is_holiday": entry is not None,
            "holiday_name": None if entry is None else entry.name,
            "holidays_source": self.source,
            "holidays_fetched_at": (
                None if self.fetched_at is None else self.fetched_at.isoformat()
            ),
            "holidays_file": None if self.path is None else str(self.path),
            "holidays_problem": self.problem,
        }

    def snapshot(self, *, year: int | None = None) -> dict[str, Any]:
        """The whole list, for an operator who wants to see what is known."""

        entries = [
            holiday.describe()
            for holiday in self.entries
            if year is None or holiday.day.year == year
        ]
        return {
            "source": self.source,
            "source_url": self.source_url,
            "segment": self.segment,
            "fetched_at": (
                None if self.fetched_at is None else self.fetched_at.isoformat()
            ),
            "file": None if self.path is None else str(self.path),
            "problem": self.problem,
            "years": list(self.years()),
            "holidays": entries,
        }


def load(path: Path) -> HolidayCalendar:
    """Read one JSON list.

    A missing file is an empty calendar, not an error: a machine that has never
    fetched the list simply has nothing to filter with. A file that cannot be
    parsed is also empty, but carries the reason in ``problem`` so the session
    clock can say that its "no holiday" is an absence of data rather than a
    claim.
    """

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HolidayCalendar(path=source)
    except OSError as exc:
        return HolidayCalendar(path=source, problem=f"unreadable: {type(exc).__name__}")
    try:
        payload = json.loads(text)
    except ValueError:
        return HolidayCalendar(path=source, problem="not valid JSON")
    if isinstance(payload, Mapping):
        rows = payload.get("holidays")
        source_name = payload.get("source")
        source_url = payload.get("source_url")
        segment = str(payload.get("segment") or SEGMENT_CM)
        fetched_at = _parse_timestamp(payload.get("fetched_at"))
    elif isinstance(payload, list):
        rows = payload
        source_name, source_url, segment, fetched_at = None, None, SEGMENT_CM, None
    else:
        return HolidayCalendar(path=source, problem="not a holiday list")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return HolidayCalendar(path=source, problem="no holiday rows")
    holidays: list[Holiday] = []
    skipped = 0
    for row in rows:
        holiday = _parse_row(row)
        if holiday is None:
            skipped += 1
            continue
        holidays.append(holiday)
    problem = None
    if skipped:
        problem = f"{skipped} rows could not be read"
    return HolidayCalendar(
        holidays,
        source=None if source_name is None else str(source_name),
        source_url=None if source_url is None else str(source_url),
        segment=segment,
        fetched_at=fetched_at,
        path=source,
        problem=problem,
    )


def write(
    path: Path,
    *,
    holidays: Iterable[Holiday],
    source: str = SOURCE_NSE,
    source_url: str = NSE_HOLIDAYS_API,
    segment: str = SEGMENT_CM,
    fetched_at: datetime | None = None,
) -> HolidayCalendar:
    """Write the list atomically, and answer with what was written."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = fetched_at or ist_now()
    ordered = tuple(sorted(holidays, key=lambda holiday: holiday.day))
    payload = {
        "source": source,
        "source_url": source_url,
        "segment": segment,
        "fetched_at": stamp.isoformat(),
        "holidays": [holiday.describe() for holiday in ordered],
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return HolidayCalendar(
        ordered,
        source=source,
        source_url=source_url,
        segment=segment,
        fetched_at=stamp,
        path=target,
    )


def fetch_nse_holidays(
    *,
    client: httpx.Client | None = None,
    segment: str = SEGMENT_CM,
) -> tuple[Holiday, ...]:
    """Fetch the official list from NSE's holiday master.

    The site is warmed up first because the API answers a sessionless request
    with the page rather than with JSON. Nothing here retries: the caller owns
    the policy, and a failure means "keep the list that is already on disk".
    """

    owns = client is None
    holder = client or httpx.Client(headers=_HEADERS, timeout=20.0, follow_redirects=True)
    try:
        holder.get(NSE_HOLIDAYS_PAGE)
        response = holder.get(NSE_HOLIDAYS_API)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HolidayFetchError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if owns:
            holder.close()
    rows = payload.get(segment) if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise HolidayFetchError(f"the answer carried no {segment} rows")
    holidays: list[Holiday] = []
    for row in rows:
        holiday = _parse_row(row)
        if holiday is not None:
            holidays.append(holiday)
    if not holidays:
        raise HolidayFetchError(f"no {segment} holiday rows could be read")
    return tuple(sorted(holidays, key=lambda holiday: holiday.day))


def merge(
    existing: Iterable[Holiday], fetched: Iterable[Holiday]
) -> tuple[Holiday, ...]:
    """The fetched list, plus existing years the fetch did not speak about.

    NSE publishes one year at a time; a fetch that returns 2027 must not throw
    away the 2026 rows that are still on disk.
    """

    fetched_rows = tuple(fetched)
    fetched_years = {holiday.day.year for holiday in fetched_rows}
    kept = [
        holiday for holiday in existing if holiday.day.year not in fetched_years
    ]
    return tuple(sorted([*kept, *fetched_rows], key=lambda holiday: holiday.day))


def refresh(
    path: Path,
    *,
    existing: HolidayCalendar | None = None,
    client: httpx.Client | None = None,
    segment: str = SEGMENT_CM,
    now: datetime | None = None,
) -> HolidayCalendar:
    """Fetch the NSE list, merge it over the local one, and write it out.

    Raises :class:`HolidayFetchError` on a failed fetch and leaves whatever is
    on disk and in the caller's hands untouched.
    """

    target = Path(path)
    base = existing if existing is not None else load(target)
    fetched = fetch_nse_holidays(client=client, segment=segment)
    return write(
        target,
        holidays=merge(base.entries, fetched),
        segment=segment,
        fetched_at=now,
    )


class HolidayList:
    """A file-backed list that re-reads only when a file actually changes.

    ``paths`` is tried in order: the live file first, then the checked-in seed.
    The first one that *parses* wins, so a half-written or hand-broken live file
    falls back to the seed instead of reporting "no holidays". Reads are cheap
    and thread-safe; Module 1's refresh worker can write the live file while the
    market routes are reading it.
    """

    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = tuple(Path(path) for path in paths)
        self._lock = threading.Lock()
        self._stamps: tuple[Any, ...] | None = None
        self._calendar = HolidayCalendar(path=self._paths[0] if self._paths else None)

    @property
    def paths(self) -> tuple[Path, ...]:
        return self._paths

    @property
    def primary(self) -> Path | None:
        return self._paths[0] if self._paths else None

    def current(self) -> HolidayCalendar:
        with self._lock:
            stamps = tuple(_stamp(path) for path in self._paths)
            if stamps != self._stamps:
                self._stamps = stamps
                self._calendar = self._read()
            return self._calendar

    def _read(self) -> HolidayCalendar:
        attempts = [
            load(path) for path in self._paths if path.exists()
        ]
        for calendar in attempts:
            if calendar.problem is None:
                return calendar
        if attempts:
            return attempts[0]
        return HolidayCalendar(path=self.primary)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _parse_row(row: object) -> Holiday | None:
    if isinstance(row, str):
        day = _parse_day(row)
        return None if day is None else Holiday(day, "Holiday")
    if not isinstance(row, Mapping):
        return None
    day = _parse_day(row.get("date") or row.get("tradingDate") or "")
    if day is None:
        return None
    name = str(row.get("name") or row.get("description") or "").strip()
    return Holiday(day, name or "Holiday")


def _parse_day(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%d-%b-%Y").date()
    except ValueError:
        return None


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = [
    "DEFAULT_RELATIVE_PATH",
    "DEFAULT_SEED_RELATIVE_PATH",
    "NSE_HOLIDAYS_API",
    "NSE_HOLIDAYS_PAGE",
    "SEGMENT_CM",
    "SOURCE_NSE",
    "Holiday",
    "HolidayCalendar",
    "HolidayError",
    "HolidayFetchError",
    "HolidayList",
    "fetch_nse_holidays",
    "load",
    "merge",
    "refresh",
    "resolve_paths",
    "write",
]
