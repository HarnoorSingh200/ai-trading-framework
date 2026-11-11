"""Time handling for Module 3.

The same shape as the other modules', and deliberately a copy rather than an
import: these are separate processes with separate lives, and a shared package
would be a coupling none of them needs. Timestamps are created and compared in
UTC, and converted to IST only to be read by a person.

Module 3 owns no calendar either. Whether a stock is tradable right now is a
market fact, and it is answered the only way it can be answered honestly - by
whether Module 1 is publishing a *live* price for that stock, rather than by a
clock this module keeps about someone else's exchange.
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


def parse_timestamp(value: str | datetime) -> datetime | None:
    """Parse an ISO-8601 timestamp, in UTC. ``None`` when it cannot be read."""

    if isinstance(value, datetime):
        return to_utc(value)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # A timestamp with no offset cannot be placed on a clock; guessing IST
        # would be inventing a fact about when something happened.
        return None
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
