"""Time handling.

One rule, applied everywhere: a timestamp is created, stored and compared in
UTC, and converted to IST only at the moment it is shown to a person. The
Indian market lives in IST, so the exchange session boundaries are computed in
IST and then converted, never the other way around.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Final
from zoneinfo import ZoneInfo

#: Exchange time. Fixed offset, so it does not need a tz database lookup.
IST: Final[tzinfo] = timezone(timedelta(hours=5, minutes=30), "IST")

#: Kept for callers that want the named zone; falls back to the fixed offset.
try:  # pragma: no cover - depends on the platform's tz database
    IST_NAMED: Final[tzinfo] = ZoneInfo("Asia/Kolkata")
except Exception:  # noqa: BLE001 - a missing tz database must not break imports
    IST_NAMED = IST


def utc_now() -> datetime:
    """The current instant, timezone-aware, in UTC."""

    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Return ``value`` in UTC, refusing a timestamp with no offset."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timestamp without a timezone offset is ambiguous")
    return value.astimezone(UTC)


def to_ist(value: datetime) -> datetime:
    """Return ``value`` in Indian Standard Time, for display."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timestamp without a timezone offset is ambiguous")
    # Not routed through ``to_utc``: that would convert to UTC and straight
    # back out again, doubling the work for the most-called function in the
    # aggregation path.
    return value.astimezone(IST)


def parse_timestamp(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp (Kite writes ``+0530`` and ``Z``)."""

    if isinstance(value, datetime):
        return to_utc(value)
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    return to_utc(parsed)


def format_utc(value: datetime) -> str:
    """Format a timestamp as ``2026-09-21T07:16:23.543Z``."""

    moment = to_utc(value)
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        f".{moment.microsecond // 1000:03d}Z"
    )


def format_ist(value: datetime) -> str:
    """Format a timestamp for a person: ``2026-09-21 15:30:00 IST``."""

    moment = to_ist(value)
    return moment.strftime("%Y-%m-%d %H:%M:%S IST")


def ist_now() -> datetime:
    return to_ist(utc_now())


def ist_today() -> date:
    return ist_now().date()
