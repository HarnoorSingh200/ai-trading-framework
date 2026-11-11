"""The hours the framework itself works in.

These are *operator policy*, not market facts, and the distinction matters:

* the exchange trades 09:15-15:30 IST, and Module 1 owns that calendar;
* this framework opens at **09:30**, stops taking decisions at **14:30**, and
  holds nothing past **14:50** - a deliberately shorter day, set by the
  operator, that leaves the last twenty minutes of an order's life inside the
  session.

Module 2 uses the decision window to know when it may wake an agent; Module 3
uses the close to know the latest moment a position may still be open. Both
read the same three environment variables, so the two can never disagree about
whose turn it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Final

from common import env

logger = logging.getLogger("common.window")

IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))

DEFAULT_DECISION_OPEN: Final[time] = time(9, 30)
DEFAULT_DECISION_CLOSE: Final[time] = time(14, 30)
DEFAULT_FRAMEWORK_CLOSE: Final[time] = time(14, 50)
#: How long after the last bar's close a decision may still be taken. The bar
#: that closes at 14:30 is read at 14:30:05 (boundary plus the settle buffer),
#: so the window has to allow the moment just after its own last boundary -
#: otherwise the final candle of the day could never be answered at all.
DEFAULT_DECISION_GRACE_SECONDS: Final[int] = 90


def ist_now() -> datetime:
    """The current moment, in IST."""

    return datetime.now(IST)


def parse_clock(value: object, fallback: time) -> time:
    """Read ``"14:30"`` (or ``"14:30:00"``), or fall back to the default."""

    text = str(value or "").strip()
    if not text:
        return fallback
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return time(hour, minute)
    except (TypeError, ValueError, IndexError):
        return fallback


@dataclass(frozen=True, slots=True)
class TradingWindow:
    """When decisions may be taken, and when everything must be flat."""

    decision_open: time = DEFAULT_DECISION_OPEN
    decision_close: time = DEFAULT_DECISION_CLOSE
    close: time = DEFAULT_FRAMEWORK_CLOSE
    decision_grace_seconds: int = DEFAULT_DECISION_GRACE_SECONDS

    # -- moments on a given day ---------------------------------------------- #

    def opens_at(self, moment: datetime) -> datetime:
        """Today's first decision moment, as a datetime in IST."""

        return datetime.combine(_day(moment), self.decision_open, tzinfo=IST)

    def decision_closes_at(self, moment: datetime) -> datetime:
        """Today's last decision moment, as a datetime in IST."""

        return datetime.combine(_day(moment), self.decision_close, tzinfo=IST)

    def closes_at(self, moment: datetime) -> datetime:
        """The moment the whole framework stops, as a datetime in IST."""

        return datetime.combine(_day(moment), self.close, tzinfo=IST)

    def last_decision_moment(self, moment: datetime) -> datetime:
        """The last instant a cycle may start: the last bar's close, plus grace."""

        return self.decision_closes_at(moment) + timedelta(
            seconds=max(0, self.decision_grace_seconds)
        )

    # -- questions ------------------------------------------------------------ #

    def allows_decision(self, moment: datetime) -> bool:
        """Whether a decision may be taken at ``moment``."""

        local = _local(moment)
        return self.decision_open <= local.time() <= self.last_decision_moment(
            local
        ).time()

    def is_closed(self, moment: datetime) -> bool:
        """Whether the framework's own day is over (nothing may stay open)."""

        return _local(moment).time() >= self.close

    def minutes_until_close(self, moment: datetime) -> int:
        """Whole minutes from ``moment`` to the framework close, never negative."""

        remaining = (self.closes_at(moment) - _local(moment)).total_seconds()
        return max(0, int(remaining // 60))

    def last_order_minutes(self) -> int:
        """Whole minutes an order taken on the day's last candle may hold.

        The decision close is when the last candle *ends*; the framework close
        is when everything must be flat. The difference is the holding time the
        last order of the day is allowed, and stating it in the prompt is what
        stops a model asking for an hour at 14:30 and being refused for it.
        """

        end = self.close.hour * 60 + self.close.minute
        last = self.decision_close.hour * 60 + self.decision_close.minute
        return max(0, end - last)

    def seconds_until_open(self, moment: datetime) -> float:
        """Seconds until today's decision window opens (0.0 while open)."""

        remaining = (self.opens_at(moment) - _local(moment)).total_seconds()
        return max(0.0, remaining)

    # -- presentation --------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        return {
            "decision_open_ist": self.decision_open.strftime("%H:%M"),
            "decision_close_ist": self.decision_close.strftime("%H:%M"),
            "framework_close_ist": self.close.strftime("%H:%M"),
            "last_decision_moment_ist": (
                self.last_decision_moment(datetime.now(IST)).strftime("%H:%M:%S")
            ),
            "note": (
                "Operator policy, not exchange hours: decisions run from the "
                "open to the decision close, and nothing stays open past the "
                "framework close. The last bar of the day is answered in the "
                "seconds just after the decision close."
            ),
        }


def _day(moment: datetime) -> date:
    return moment.astimezone(IST).date()


def _local(moment: datetime) -> datetime:
    return moment.astimezone(IST)


def load() -> TradingWindow:
    """Read the window from the environment, falling back to the defaults.

    A window with its parts out of order is refused as a whole and the defaults
    are used, with a warning: half-applying a broken schedule would be a way to
    trade hours nobody chose.
    """

    window = TradingWindow(
        decision_open=parse_clock(
            env.text("FRAMEWORK_DECISION_OPEN", ""), DEFAULT_DECISION_OPEN
        ),
        decision_close=parse_clock(
            env.text("FRAMEWORK_DECISION_CLOSE", ""), DEFAULT_DECISION_CLOSE
        ),
        close=parse_clock(env.text("FRAMEWORK_CLOSE", ""), DEFAULT_FRAMEWORK_CLOSE),
        decision_grace_seconds=max(
            0,
            min(
                3600,
                env.integer(
                    "FRAMEWORK_DECISION_GRACE_SECONDS", DEFAULT_DECISION_GRACE_SECONDS
                ),
            ),
        ),
    )
    if not (window.decision_open < window.decision_close <= window.close):
        logger.warning(
            "the configured trading window is out of order (%s); using the defaults",
            window.describe(),
        )
        return TradingWindow()
    return window
