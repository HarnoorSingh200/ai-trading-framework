"""Time handling for Module 2.

Deliberately the same shape as Module 1's, and deliberately a copy rather than
an import: the two modules are separate processes with separate lives, and a
shared package would be a coupling neither of them needs. Timestamps are
created and compared in UTC and converted to IST only to be read by a person.

Module 2 does not own a session calendar - that is a market fact, and market
facts come from Module 1 over HTTP. What it needs locally is the offset, so a
deadline written in exchange time ("no exit after 14:50 IST") can be compared.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Final
from zoneinfo import ZoneInfo

#: Exchange time. A fixed offset, so no tz database lookup is required.
IST: Final[tzinfo] = timezone(timedelta(hours=5, minutes=30), "IST")

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
    return value.astimezone(IST)


def parse_timestamp(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp, in UTC."""

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
    """Format a timestamp as ``2026-09-27T07:16:23.543Z``."""

    moment = to_utc(value)
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        f".{moment.microsecond // 1000:03d}Z"
    )


def format_ist(value: datetime) -> str:
    """Format a timestamp for a person: ``2026-09-27 15:30:00 IST``."""

    return to_ist(value).strftime("%Y-%m-%d %H:%M:%S IST")


def ist_now() -> datetime:
    return to_ist(utc_now())


def ist_today() -> date:
    return ist_now().date()
