"""The indicator port, and the rules it must not bend.

The reference values in :data:`GOLDEN` were produced by v2's pandas engine
(``framework/market_data/indicators.py``) for the same 60-bar series this file
builds, using ``v3/logs/gen_golden.py``. They are the contract: the port is a
reimplementation, and a reimplementation that drifts quietly is worse than no
port at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from module1.candles import Bar
from module1.indicators import (
    FIELDS,
    DayLevels,
    IndicatorParameters,
    compute,
    compute_latest,
    nearest_level,
)
from module1.timeutil import IST

pytestmark = pytest.mark.unit

START = datetime(2026, 9, 24, 9, 15, tzinfo=IST)

#: v2's newest frame row for this series, verbatim.
GOLDEN: dict[str, object] = {
    "above_ema": False,
    "above_sma": False,
    "atr_14": 1.8230273567932929,
    "atr_pct": 1.8688132822073735,
    "bb_20_lower": 96.85390282377182,
    "bb_20_middle": 98.61749999999999,
    "bb_20_upper": 100.38109717622817,
    "bb_20_width_pct": 3.57664142008908,
    "body": 0.0,
    "candle_colour": "doji",
    "change_pct": 0.0,
    "cumulative_volume": 86490.0,
    "day_change_pct": -5.975903614457835,
    "distance_from_high_pct": 3.031809145129222,
    "distance_from_low_pct": 0.4634397528321348,
    "ema_20": 98.47668845564087,
    "gap_pct": 0.0,
    "lower_wick": 0.45000000000000284,
    "momentum_15": -0.7000000000000028,
    "momentum_30": -0.7000000000000028,
    "momentum_5": -2.450000000000003,
    "momentum_pct_15": -0.7124681933842254,
    "momentum_pct_30": -0.7124681933842254,
    "momentum_pct_5": -2.450000000000003,
    "previous_close": 97.55,
    "previous_day_close": 103.75,
    "previous_day_high": 105.5,
    "previous_day_low": 98.25,
    "range_pct": 1.0763710917478333,
    "roc_5": -2.450000000000003,
    "rolling_high_20": 100.6,
    "rolling_low_20": 97.1,
    "rsi_14": 46.01596686066039,
    "sma_20": 98.61749999999999,
    "stddev_20": 0.8817985881140846,
    "true_range": 1.0500000000000114,
    "unusual_volume": False,
    "upper_wick": 0.6000000000000085,
    "volume_ratio_20": 1.3108249216846501,
    "volume_sma_20": 1436.5,
    "volume_vs_previous": 1.0784650630011454,
    "vwap": 98.58149227270977,
}

#: v2 emitted the pivot ladder as frame columns; the port publishes it from
#: DayLevels instead.
GOLDEN_PIVOTS = {
    "pivot": 102.5,
    "r1": 106.75,
    "r2": 109.75,
    "r3": 114.0,
    "s1": 99.5,
    "s2": 95.25,
    "s3": 92.25,
}

DAY_LEVELS = DayLevels(
    high=Decimal("105.50"), low=Decimal("98.25"), close=Decimal("103.75")
)


def series(count: int = 60) -> list[tuple[float, float, float, float, float]]:
    """The deterministic series both engines are fed."""

    rows = []
    price = 100.0
    for index in range(count):
        drift = ((index * 37) % 11 - 5) * 0.35
        opened = price
        closed = round(price + drift, 2)
        high = round(max(opened, closed) + 0.4 + (index % 3) * 0.1, 2)
        low = round(min(opened, closed) - 0.35 - (index % 2) * 0.1, 2)
        volume = 1000 + (index * 137) % 900
        rows.append((opened, high, low, closed, volume))
        price = closed
    return rows


def bars(count: int = 60, *, start: datetime = START, minutes: int = 1) -> list[Bar]:
    out: list[Bar] = []
    for index, (o, h, low, c, v) in enumerate(series(count)):
        opened = start + timedelta(minutes=index * minutes)
        out.append(
            Bar(
                open_time=opened,
                close_time=opened + timedelta(minutes=minutes),
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(low)),
                close=Decimal(str(c)),
                volume=Decimal(str(v)),
                is_complete=True,
                is_forming=False,
                is_gappy=False,
                minutes_present=1,
                minutes_expected=1,
                symbol="TEST",
                interval="1m",
            )
        )
    return out


def test_the_port_reproduces_v2s_values() -> None:
    latest = compute(bars(), day_levels=DAY_LEVELS)[-1]

    for name, expected in GOLDEN.items():
        actual = latest[name]
        if isinstance(expected, bool):
            assert actual is expected, name
        elif isinstance(expected, str):
            assert actual == expected, name
        else:
            assert actual == pytest.approx(expected, abs=1e-9), name


def test_the_pivot_ladder_matches_the_classic_formulas() -> None:
    pivots = DAY_LEVELS.pivots()

    for name, expected in GOLDEN_PIVOTS.items():
        assert float(pivots[name]) == pytest.approx(expected, abs=1e-9), name


def test_a_value_is_none_until_its_window_is_complete() -> None:
    """The rule the whole contract rests on: never guess."""

    rows = compute(bars(10))

    assert rows[-1]["sma_20"] is None
    assert rows[-1]["ema_20"] is None
    assert rows[-1]["rsi_14"] is None
    assert rows[-1]["atr_14"] is None
    assert rows[-1]["bb_20_middle"] is None
    assert rows[-1]["above_sma"] is None
    assert rows[-1]["unusual_volume"] is None
    # The candle's own shape needs no history at all.
    assert rows[-1]["candle_colour"] in {"green", "red", "doji"}
    assert rows[-1]["body"] is not None


def test_vwap_and_cumulative_volume_reset_at_the_session_open() -> None:
    """v2 accumulated across its whole series; ours spans several sessions."""

    first = bars(6)
    second_start = START + timedelta(days=1)
    second = bars(6, start=second_start)

    rows = compute(first + second)

    # The last bar of the first session carries six bars of accumulation.
    assert rows[5]["cumulative_volume"] == pytest.approx(
        sum(float(bar.volume) for bar in first)
    )
    # The first bar of the next session starts again from itself.
    assert rows[6]["cumulative_volume"] == pytest.approx(float(second[0].volume))
    assert rows[6]["vwap"] == pytest.approx(
        (
            float(second[0].high) + float(second[0].low) + float(second[0].close)
        )
        / 3.0
    )


def test_the_recursive_indicators_are_converged_within_the_warmup_window() -> None:
    """The bounded smoothing must not change the answer.

    Measured against a 600-bar run the worst absolute difference in RSI is
    3.2e-3 at 100 bars, 2.3e-4 at 150, 3.9e-6 at 200 and 2.5e-9 at 300 - which
    is why WARMUP_BARS is 300.
    """

    long_run = compute(bars(600))[-1]
    bounded = compute_latest(bars(600))

    assert bounded is not None
    for name in ("ema_20", "rsi_14", "atr_14"):
        assert bounded[name] == pytest.approx(long_run[name], abs=1e-8), name


def test_the_nearest_level_carries_its_name_and_sign() -> None:
    pivots = DAY_LEVELS.pivots()

    above = nearest_level(106.0, pivots)
    below = nearest_level(100.0, pivots)

    assert above is not None and above[0] == "r1"
    assert above[2] == pytest.approx((106.0 - 106.75) / 106.75 * 100.0)
    assert below is not None and below[0] == "s1"
    assert below[2] > 0  # 100.0 sits above s1 at 99.5


def test_parameters_are_reported_with_the_version() -> None:
    described = IndicatorParameters().describe()

    assert described["rsi_window"] == 14
    assert described["momentum_windows"] == [5, 15, 30]
    assert IndicatorParameters().version == "1.0.0"


def test_an_empty_series_produces_nothing() -> None:
    assert compute([]) == []
    assert compute_latest([]) is None


def test_a_lookback_longer_than_the_series_is_all_padding() -> None:
    """Short series are the common case at a session open, not an edge case."""

    rows = compute(bars(3))

    assert len(rows) == 3
    for row in rows:
        assert row["roc_5"] is None
        assert row["momentum_30"] is None


# -- the catalogue and the engine cannot drift apart -------------------------- #


def test_every_value_the_engine_computes_is_published_in_the_catalogue() -> None:
    """``FIELDS`` is the one place a field's name lives.

    A value the engine produces but nobody lists is a value nobody can filter
    on, render as a column, or even discover: the model sees it in a stock's
    values and then cannot name it in a screen, so it guesses and loses a
    round. Three of them were sitting there unlisted when this test was
    written, which is exactly why it exists.
    """

    latest = compute(bars(), day_levels=DAY_LEVELS)[-1]
    published = {spec.name for spec in FIELDS}

    assert set(latest) - published == set()  # computed but never published
    assert published - set(latest) == set()  # published but never computed


def test_above_vwap_is_a_fact_because_a_filter_cannot_compare_two_fields() -> None:
    """A filter compares a field against a value, never against another field.

    So "close above VWAP" cannot be written as a condition, and has to be
    computed here or not asked for at all.
    """

    held = bars()
    latest = compute(held, day_levels=DAY_LEVELS)[-1]

    assert latest["above_vwap"] is (float(held[-1].close) > latest["vwap"])
