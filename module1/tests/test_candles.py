"""Timeframe aggregation, and the change and volume each candle carries."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from module1.broker.kite import Candle
from module1.candles import (
    DEFAULT_LIMIT,
    TIMEFRAME_BY_VALUE,
    TIMEFRAMES,
    CandleService,
    SymbolUnresolved,
    TimeframeError,
    aggregate,
    last_closed_bucket,
    session_open_for,
)
from module1.timeutil import IST

pytestmark = pytest.mark.unit

#: A Monday, and the session open that day.
SESSION_DAY = datetime(2026, 9, 21, 9, 15, tzinfo=IST)


def minute(offset: int, *, close: str = "100", volume: str = "1000") -> Candle:
    """One 1-minute candle ``offset`` minutes after the session open."""

    opened = SESSION_DAY + timedelta(minutes=offset)
    price = Decimal(close)
    return Candle(
        open_time=opened,
        open=price,
        high=price + Decimal("2"),
        low=price - Decimal("2"),
        close=price,
        volume=Decimal(volume),
    )


class FakeClient:
    """Stands in for the broker and remembers what it was asked for."""

    def __init__(self, minutes: list[Candle] | None = None, days: list[Candle] | None = None):
        self.minutes = minutes or []
        self.days = days or []
        self.calls: list[tuple[str, str]] = []

    def historical(self, token, interval, *, start, end, interactive=False):
        self.calls.append(("historical", interval))
        return list(self.minutes)

    def day_candles(self, token, *, sessions, now=None, interactive=False):
        self.calls.append(("day_candles", token))
        return list(self.days)


def test_the_offered_timeframes_are_the_locked_set() -> None:
    assert [spec.value for spec in TIMEFRAMES] == [
        "1m",
        "5m",
        "10m",
        "15m",
        "20m",
        "30m",
        "60m",
        "1d",
    ]
    assert TIMEFRAME_BY_VALUE["15m"].derived is True
    assert TIMEFRAME_BY_VALUE["1d"].derived is False


def test_buckets_align_to_the_session_open_not_the_clock_hour() -> None:
    """The first 15-minute bar of the day is 09:15-09:30."""

    base = [minute(index) for index in range(30)]
    now = SESSION_DAY + timedelta(minutes=31)

    bars = aggregate(base, TIMEFRAME_BY_VALUE["15m"], symbol="TCS", now=now)

    assert [bar.open_time.strftime("%H:%M") for bar in bars] == ["09:15", "09:30"]
    assert bars[0].minutes_present == 15
    assert bars[0].is_complete is True


def test_change_percent_is_measured_against_the_previous_close() -> None:
    base = [minute(0, close="100"), minute(15, close="105"), minute(30, close="99")]
    now = SESSION_DAY + timedelta(minutes=46)

    bars = aggregate(base, TIMEFRAME_BY_VALUE["15m"], symbol="TCS", now=now)

    assert bars[0].change_percent is None  # nothing precedes the first bar
    assert bars[1].change == Decimal("5")
    assert bars[1].change_percent == Decimal("5")
    assert bars[2].change_percent == pytest.approx(Decimal("-5.7142857"), abs=1e-6)


def test_move_percent_is_the_candle_body() -> None:
    body = Candle(
        open_time=SESSION_DAY,
        open=Decimal("200"),
        high=Decimal("212"),
        low=Decimal("198"),
        close=Decimal("210"),
        volume=Decimal("500"),
    )

    bars = aggregate([body], TIMEFRAME_BY_VALUE["15m"], symbol="TCS", now=SESSION_DAY)

    assert bars[0].move_percent == Decimal("5")
    assert bars[0].range_percent == pytest.approx(Decimal("7.0707"), abs=1e-4)


def test_volume_is_summed_per_bar_and_rated_against_the_window() -> None:
    base = []
    for index in range(30):
        volume = "3000" if index < 15 else "1000"
        base.append(minute(index, volume=volume))
    now = SESSION_DAY + timedelta(minutes=31)

    bars = aggregate(base, TIMEFRAME_BY_VALUE["15m"], symbol="TCS", now=now)

    assert bars[0].volume == Decimal("45000")
    assert bars[1].volume == Decimal("15000")
    # 45000 and 15000 average 30000, so the ratios are 1.5 and 0.5.
    assert bars[0].volume_ratio == Decimal("1.5")
    assert bars[1].volume_ratio == Decimal("0.5")


def test_a_bucket_that_is_still_forming_is_not_called_complete() -> None:
    now = SESSION_DAY + timedelta(minutes=5)

    bars = aggregate([minute(0)], TIMEFRAME_BY_VALUE["15m"], symbol="TCS", now=now)

    assert bars[0].is_complete is False
    assert bars[0].is_forming is True
    assert bars[0].is_gappy is False
    assert bars[0].minutes_present == 1
    assert bars[0].minutes_expected == 15


def test_a_missing_minute_leaves_the_bucket_incomplete() -> None:
    """A gap must show up as a short bucket rather than a confident candle."""

    base = [minute(0), minute(3)]
    now = SESSION_DAY + timedelta(minutes=20)

    bars = aggregate(base, TIMEFRAME_BY_VALUE["15m"], symbol="TCS", now=now)

    assert bars[0].minutes_present == 2
    assert bars[0].is_complete is False
    # The period is over; what is missing is data, not time.
    assert bars[0].is_forming is False
    assert bars[0].is_gappy is True


def _daily(session_date: datetime, close: str = "100") -> Candle:
    """A daily candle the way the broker stamps one: midnight, IST."""

    midnight = datetime(session_date.year, session_date.month, session_date.day, tzinfo=IST)
    price = Decimal(close)
    return Candle(
        open_time=midnight,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
    )


def test_a_closed_session_daily_candle_is_not_left_forming() -> None:
    """A daily bar closes with the session, not at midnight."""

    friday = datetime(2026, 9, 18, tzinfo=IST)
    monday = datetime(2026, 9, 21, tzinfo=IST)
    after_close = datetime(2026, 9, 21, 16, 0, tzinfo=IST)

    bars = aggregate(
        [_daily(friday, "100"), _daily(monday, "105")],
        TIMEFRAME_BY_VALUE["1d"],
        symbol="TCS",
        now=after_close,
    )

    assert [bar.is_complete for bar in bars] == [True, True]
    assert [bar.minutes_expected for bar in bars] == [1, 1]
    # The day's own close is the session close, which is what gets shown.
    assert bars[1].close_time.hour == 15 and bars[1].close_time.minute == 30


def test_todays_daily_candle_is_still_forming_before_the_close() -> None:
    midday = datetime(2026, 9, 21, 12, 0, tzinfo=IST)
    friday = datetime(2026, 9, 18, tzinfo=IST)
    monday = datetime(2026, 9, 21, tzinfo=IST)

    bars = aggregate(
        [_daily(friday), _daily(monday)],
        TIMEFRAME_BY_VALUE["1d"],
        symbol="TCS",
        now=midday,
    )

    assert bars[0].is_complete is True
    assert bars[1].is_forming is True
    assert bars[1].is_complete is False


def test_the_one_minute_timeframe_is_passed_through() -> None:
    bars = aggregate(
        [minute(0), minute(1)],
        TIMEFRAME_BY_VALUE["1m"],
        symbol="TCS",
        now=SESSION_DAY + timedelta(minutes=5),
    )

    assert len(bars) == 2
    assert bars[0].open_time == SESSION_DAY
    assert bars[1].change_percent == Decimal("0")


def test_a_bar_says_where_its_minutes_came_from() -> None:
    """A bar built from ticks must be distinguishable from a stored one."""

    from dataclasses import replace

    built = replace(minute(0), source="live")
    stored = minute(15)

    bars = aggregate(
        [built, stored],
        TIMEFRAME_BY_VALUE["15m"],
        symbol="TCS",
        now=SESSION_DAY + timedelta(minutes=30),
    )

    assert bars[0].source == "live"
    assert bars[1].source == "broker"


def test_a_derived_bar_is_mixed_when_it_spans_both() -> None:
    from dataclasses import replace

    minutes = [minute(0, close="100"), replace(minute(1, close="101"), source="live")]

    bars = aggregate(
        minutes,
        TIMEFRAME_BY_VALUE["5m"],
        symbol="TCS",
        now=SESSION_DAY + timedelta(minutes=10),
    )

    assert bars[0].source == "mixed"


def test_the_session_open_helper_ignores_a_utc_offset() -> None:
    # 03:45 UTC is 09:15 IST, the session open.
    assert session_open_for(datetime(2026, 9, 21, 3, 45, tzinfo=IST)).hour == 9


def test_a_series_carries_its_summary_and_window() -> None:
    minutes = [minute(index, close=str(100 + index)) for index in range(30)]
    service = CandleService(
        FakeClient(minutes), resolve_token=lambda symbol: "1234",
        clock=lambda: SESSION_DAY + timedelta(minutes=31),
    )

    payload = service.series("tcs", "15m", limit=2)

    assert payload["symbol"] == "TCS"
    assert payload["interval"] == "15m"
    assert payload["source"] == "derived from 1m"
    assert payload["count"] == 2
    assert len(payload["candles"]) == 2
    # The first bar of any window has no predecessor, so it has no change to
    # classify; only the second one counts as an up-bar here.
    assert payload["candles"][0]["change_percent"] is None
    assert payload["summary"]["up"] == 1
    assert payload["summary"]["down"] == 0
    assert payload["candles"][0]["open_time_ist"].endswith("IST")


def test_the_daily_timeframe_asks_the_broker_instead_of_deriving() -> None:
    client = FakeClient(days=[minute(0), minute(1440), minute(2880)])
    service = CandleService(
        client, resolve_token=lambda symbol: "1234",
        clock=lambda: SESSION_DAY + timedelta(minutes=10),
    )

    payload = service.series("TCS", "1d", limit=10)

    assert client.calls == [("day_candles", "1234")]
    assert payload["source"] == "broker"


def test_the_minute_series_is_fetched_once_and_then_cached() -> None:
    client = FakeClient([minute(0)])
    service = CandleService(
        client, resolve_token=lambda symbol: "1234",
        clock=lambda: SESSION_DAY + timedelta(minutes=5),
    )

    service.series("TCS", "5m")
    service.series("TCS", "15m")
    service.series("TCS", "30m")

    assert [call[1] for call in client.calls] == ["1m"]
    assert service.status()["cache_hits"] == 2


def test_an_unknown_timeframe_is_refused_with_the_supported_list() -> None:
    service = CandleService(FakeClient(), resolve_token=lambda symbol: "1234")

    with pytest.raises(TimeframeError) as caught:
        service.series("TCS", "7m")

    assert "15m" in caught.value.supported


def test_an_unresolved_symbol_is_refused() -> None:
    service = CandleService(FakeClient(), resolve_token=lambda symbol: None)

    with pytest.raises(SymbolUnresolved):
        service.series("NOPE", "15m")


def test_the_window_is_bounded_to_the_documented_limit() -> None:
    minutes = [minute(index) for index in range(60)]
    service = CandleService(
        FakeClient(minutes), resolve_token=lambda symbol: "1234",
        clock=lambda: SESSION_DAY + timedelta(minutes=61),
    )

    payload = service.series("TCS", "1m", limit=10_000)

    assert payload["count"] <= 300
    assert DEFAULT_LIMIT == 60


def test_the_timeframe_board_serves_every_held_symbol_from_memory() -> None:
    """The timeframe tabs must not cost a broker call to switch."""

    minutes = [minute(index, close=str(100 + index), volume="1000") for index in range(30)]
    client = FakeClient(minutes)
    service = CandleService(
        client,
        resolve_token=lambda symbol: "1234",
        symbol_meta=lambda symbol: {"name": "Tata Consultancy", "industry": "IT"},
        clock=lambda: SESSION_DAY + timedelta(minutes=46),
    )
    service.book.seed("TCS", minutes)
    calls_before = list(client.calls)

    board = service.timeframe_board("15m")

    assert client.calls == calls_before, "switching timeframe should not call the broker"
    assert board["interval"] == "15m"
    assert board["count"] == 1
    assert board["unavailable"] == []
    row = board["rows"][0]
    assert row["symbol"] == "TCS"
    assert row["name"] == "Tata Consultancy"
    assert row["industry"] == "IT"
    # The latest bar is the second 15-minute bucket: minutes 15..29.
    assert row["open"] == 115.0
    assert row["close"] == 129.0
    assert row["previous_close"] == 114.0
    assert row["volume"] == 15000.0
    assert row["open_time_ist"].endswith("IST")
    assert board["summary"]["advancing"] == 1
    assert board["summary"]["forming"] == 0
    # The row carries a shape as well as numbers: one close per bar of *this*
    # timeframe, so thirty minutes of data is two 15-minute points.
    assert row["spark"] == [114.0, 129.0]


def test_the_daily_timeframe_board_uses_the_brokers_own_series() -> None:
    client = FakeClient(days=[minute(0, close="100"), minute(1440, close="105")])
    service = CandleService(
        client,
        resolve_token=lambda symbol: "1234",
        clock=lambda: SESSION_DAY + timedelta(days=1, minutes=10),
    )
    service.book.seed("TCS", [minute(0)])

    board = service.timeframe_board("1d")

    assert ("day_candles", "1234") in client.calls
    assert board["source"] == "broker"
    assert board["rows"][0]["close"] == 105.0


def test_the_daily_series_is_fetched_once_at_one_depth_for_every_caller() -> None:
    """A shallow first caller must not shrink the series for a deep one."""

    days = [minute(index * 1440, close=str(100 + index)) for index in range(40)]
    client = FakeClient(days=days)
    service = CandleService(
        client,
        resolve_token=lambda symbol: "1234",
        clock=lambda: SESSION_DAY + timedelta(days=45),
    )
    service.book.seed("TCS", [minute(0)])

    service.timeframe_board("1d")          # asks for two bars
    deep = service.series("TCS", "1d", limit=30)   # asks for thirty

    assert [call[0] for call in client.calls].count("day_candles") == 1
    assert deep["count"] == 30


def test_an_unknown_timeframe_board_is_refused() -> None:
    service = CandleService(FakeClient(), resolve_token=lambda symbol: "1234")

    with pytest.raises(TimeframeError):
        service.timeframe_board("9m")


# -- which bucket has just finished ----------------------------------------- #
#
# This is the same question an agent asks when it wakes, and it must be
# answered from the exchange clock rather than from whatever a cache happens to
# hold: the bucket that just closed is the one everybody is deciding on.


def test_the_last_closed_bucket_is_the_one_that_just_ended() -> None:
    now = SESSION_DAY + timedelta(hours=1, minutes=32)  # 10:47

    opened, closed = last_closed_bucket(now, TIMEFRAME_BY_VALUE["15m"])

    assert opened.strftime("%H:%M") == "10:30"
    assert closed.strftime("%H:%M") == "10:45"


def test_a_bucket_that_ends_exactly_now_counts_as_closed() -> None:
    now = SESSION_DAY + timedelta(minutes=90)  # 10:45 on the dot

    opened, closed = last_closed_bucket(now, TIMEFRAME_BY_VALUE["15m"])

    assert (opened.strftime("%H:%M"), closed.strftime("%H:%M")) == ("10:30", "10:45")


def test_before_the_first_bucket_ends_the_answer_is_the_previous_session() -> None:
    """At 09:20 there is nothing finished today, and yesterday is not a guess."""

    now = SESSION_DAY + timedelta(minutes=5)

    opened, closed = last_closed_bucket(now, TIMEFRAME_BY_VALUE["15m"])

    assert opened.date() == date(2026, 9, 18)  # the Friday before
    assert (opened.strftime("%H:%M"), closed.strftime("%H:%M")) == ("15:00", "15:15")


def test_the_weekend_is_skipped() -> None:
    # 09:17 is inside the first 5m bucket, so nothing has closed today yet and
    # the answer has to come from the previous - Friday's - session.
    monday = datetime(2026, 9, 21, 9, 17, tzinfo=IST)

    opened, _ = last_closed_bucket(monday, TIMEFRAME_BY_VALUE["5m"])

    assert opened.date() == date(2026, 9, 18)
    assert opened.strftime("%H:%M") == "15:10"  # the last 5m bucket with minutes


def test_the_last_bucket_of_a_day_is_the_one_the_exchange_publishes() -> None:
    """The broker's minute series stops at 15:14, so 15:15 is already the close.

    20-minute buckets do not divide the published 360 minutes evenly, so the
    last one is short - and it is still a real candle, built from the minutes
    that exist.
    """

    now = SESSION_DAY + timedelta(hours=6, minutes=25)  # 15:40

    opened, closed = last_closed_bucket(now, TIMEFRAME_BY_VALUE["20m"])

    assert (opened.strftime("%H:%M"), closed.strftime("%H:%M")) == ("14:55", "15:15")


def test_after_the_close_the_answer_stays_the_last_published_bucket() -> None:
    now = SESSION_DAY + timedelta(hours=6, minutes=30)  # 15:45

    opened, closed = last_closed_bucket(now, TIMEFRAME_BY_VALUE["15m"])

    assert (opened.strftime("%H:%M"), closed.strftime("%H:%M")) == ("15:00", "15:15")


def test_the_one_minute_bucket_is_the_minute_that_just_ended() -> None:
    now = SESSION_DAY + timedelta(minutes=1)

    opened, closed = last_closed_bucket(now, TIMEFRAME_BY_VALUE["1m"])

    assert (opened.strftime("%H:%M"), closed.strftime("%H:%M")) == ("09:15", "09:16")


def test_a_daily_candle_closes_with_its_session() -> None:
    during = SESSION_DAY + timedelta(hours=2)
    after = SESSION_DAY + timedelta(hours=7)

    open_during, close_during = last_closed_bucket(during, TIMEFRAME_BY_VALUE["1d"])
    open_after, close_after = last_closed_bucket(after, TIMEFRAME_BY_VALUE["1d"])

    assert open_during.date() == date(2026, 9, 18)
    assert open_during.strftime("%H:%M") == "00:00"
    assert close_during.strftime("%H:%M") == "15:30"
    assert open_after.date() == date(2026, 9, 21)


def test_a_weekend_has_no_candle_of_its_own() -> None:
    """Saturday cannot close a 15-minute candle: the last one is Friday's.

    The session clock already knows the weekend, so a bucket named for a
    Saturday is a bucket that does not exist - and every symbol would be
    reported as missing from it.
    """

    saturday = datetime(2026, 9, 26, 20, 36, tzinfo=IST)

    opened, closed = last_closed_bucket(saturday, TIMEFRAME_BY_VALUE["15m"])

    assert opened.date() == date(2026, 9, 25)
    assert (opened.strftime("%H:%M"), closed.strftime("%H:%M")) == ("15:00", "15:15")


def test_a_weekend_has_no_daily_candle_either() -> None:
    sunday = datetime(2026, 9, 27, 20, 36, tzinfo=IST)

    opened, _ = last_closed_bucket(sunday, TIMEFRAME_BY_VALUE["1d"])

    assert opened.date() == date(2026, 9, 25)


def test_a_holiday_has_no_candle_of_its_own() -> None:
    """Diwali is not a session: the last closed bucket is the previous one.

    Module 1 keeps running on a holiday, so without this the board would name
    a bucket on a day the exchange was shut and every symbol would look
    missing from a bar that never existed.
    """

    from common.holidays import Holiday, HolidayCalendar
    from module1 import session_clock

    session_clock.install_calendar(
        HolidayCalendar([Holiday(date(2026, 9, 22), "Test Holiday")])
    )
    holiday = datetime(2026, 9, 22, 10, 30, tzinfo=IST)

    opened, closed = last_closed_bucket(holiday, TIMEFRAME_BY_VALUE["15m"])

    assert opened.date() == date(2026, 9, 21)
    assert (opened.strftime("%H:%M"), closed.strftime("%H:%M")) == ("15:00", "15:15")
