"""Indicator values for one symbol and timeframe.

Ported from v2's pandas engine into plain Python, with the same formulas, the
same parameters and the same rule about warm-up: **a value is ``None`` until
its window is complete**, never guessed. That rule is the whole reason a
downstream reader can trust the number - an RSI of 50 because there was not
enough history looks exactly like an RSI of 50 because the market is balanced.

Two things are deliberately different from v2:

* ``vwap`` and ``cumulative_volume`` reset at the exchange session open. v2
  accumulated them across the series it was handed, which was one session;
  ours spans about six, so carrying the accumulation would be meaningless.
* the recursive indicators (``ema``, ``rsi``, ``atr``) are smoothed over a
  bounded window rather than the entire stored series. They are seeded
  averages, so the seed's influence decays geometrically: over
  :data:`WARMUP_BARS` bars the worst difference against a whole-series run is
  2.5e-9. This is what keeps the cost bounded however deep the warm-up goes.

Price-derived fields (body, wicks, ranges, pivots) stay decimal-exact;
statistical indicators use floating point, as the design package allows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from module1.candles import Bar, session_open_for

#: Bumped when a formula changes. Never changed in place.
INDICATOR_VERSION: Final[str] = "1.0.0"

#: A candle whose volume is this many times its average is unusual.
UNUSUAL_VOLUME_RATIO: Final[float] = 2.0

#: Momentum lookbacks the design package asks for.
MOMENTUM_WINDOWS: Final[tuple[int, ...]] = (5, 15, 30)

#: How many bars the recursive indicators are smoothed over: a convergence
#: bound, not a truncation of the answer. Measured against a 600-bar run, the
#: worst absolute difference in RSI is 3.2e-3 at 100 bars, 2.3e-4 at 150,
#: 3.9e-6 at 200 and 2.5e-9 at 300. Three hundred it is: the cost stays bounded
#: per timeframe however deep the warm-up goes, and the answer is exact to
#: within a rounding error nobody will ever see.
WARMUP_BARS: Final[int] = 300

DEFAULT_PARAMETERS: Final[dict[str, float]] = {
    "sma": 20,
    "ema": 20,
    "bollinger": 20,
    "bollinger_deviation": 2,
    "atr": 14,
    "rsi": 14,
    "roc": 5,
    "volume": 20,
    "rolling": 20,
}

_QUANTUM: Final[Decimal] = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class IndicatorParameters:
    """Explicit, versioned indicator parameters."""

    sma_window: int = int(DEFAULT_PARAMETERS["sma"])
    ema_window: int = int(DEFAULT_PARAMETERS["ema"])
    bollinger_window: int = int(DEFAULT_PARAMETERS["bollinger"])
    bollinger_deviation: float = float(DEFAULT_PARAMETERS["bollinger_deviation"])
    atr_window: int = int(DEFAULT_PARAMETERS["atr"])
    rsi_window: int = int(DEFAULT_PARAMETERS["rsi"])
    roc_window: int = int(DEFAULT_PARAMETERS["roc"])
    volume_window: int = int(DEFAULT_PARAMETERS["volume"])
    rolling_window: int = int(DEFAULT_PARAMETERS["rolling"])
    unusual_volume_ratio: float = UNUSUAL_VOLUME_RATIO
    momentum_windows: tuple[int, ...] = MOMENTUM_WINDOWS

    @property
    def version(self) -> str:
        return INDICATOR_VERSION

    @property
    def longest_window(self) -> int:
        return max(
            self.sma_window,
            self.ema_window,
            self.bollinger_window,
            self.atr_window + 1,
            self.rsi_window + 1,
            self.roc_window,
            self.volume_window,
            self.rolling_window,
            *self.momentum_windows,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "sma_window": self.sma_window,
            "ema_window": self.ema_window,
            "bollinger_window": self.bollinger_window,
            "bollinger_deviation": self.bollinger_deviation,
            "atr_window": self.atr_window,
            "rsi_window": self.rsi_window,
            "roc_window": self.roc_window,
            "volume_window": self.volume_window,
            "rolling_window": self.rolling_window,
            "unusual_volume_ratio": self.unusual_volume_ratio,
            "momentum_windows": list(self.momentum_windows),
        }


@dataclass(frozen=True, slots=True)
class DayLevels:
    """The previous completed session's high, low and close."""

    high: Decimal
    low: Decimal
    close: Decimal
    session_date: str | None = None

    def pivots(self) -> dict[str, Decimal]:
        """Classic floor-trader pivots."""

        pivot = (self.high + self.low + self.close) / Decimal("3")
        span = self.high - self.low
        return {
            "pivot": _round(pivot),
            "r1": _round(Decimal("2") * pivot - self.low),
            "r2": _round(pivot + span),
            "r3": _round(self.high + Decimal("2") * (pivot - self.low)),
            "s1": _round(Decimal("2") * pivot - self.high),
            "s2": _round(pivot - span),
            "s3": _round(self.low - Decimal("2") * (self.high - pivot)),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date,
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "pivots": {name: float(value) for name, value in self.pivots().items()},
        }


def nearest_level(
    close: float, levels: dict[str, Decimal]
) -> tuple[str, float, float] | None:
    """The closest pivot level to ``close``, with the signed distance to it."""

    if not levels:
        return None
    name, value = min(levels.items(), key=lambda item: abs(float(item[1]) - close))
    price = float(value)
    if price == 0:
        return None
    return name, price, (close - price) / price * 100.0


# -- series helpers --------------------------------------------------------- #


def _shift(values: Sequence[float], count: int) -> list[float | None]:
    """``values`` moved ``count`` places later, padded with ``None``.

    The result is always the same length as the input: a lookback longer than
    the series is all padding, never a longer list.
    """

    if count <= 0:
        return list(values)
    if count >= len(values):
        return [None] * len(values)
    return [None] * count + list(values[: len(values) - count])


def _sma(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if window < 1 or len(values) < window:
        return out
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        if index >= window - 1:
            out[index] = running / window
    return out


def _ema(values: Sequence[float], window: int) -> list[float | None]:
    """EMA seeded with the simple average of the first ``window`` values."""

    out: list[float | None] = [None] * len(values)
    if window < 1 or len(values) < window:
        return out
    alpha = 2.0 / (window + 1.0)
    previous = sum(values[:window]) / window
    out[window - 1] = previous
    for index in range(window, len(values)):
        previous = alpha * values[index] + (1.0 - alpha) * previous
        out[index] = previous
    return out


def _wilder(values: Sequence[float | None], window: int) -> list[float | None]:
    """Wilder's smoothing: seeded from the average, then smoothed."""

    out: list[float | None] = [None] * len(values)
    if window < 1 or len(values) <= window:
        return out
    seed_values = [value for value in values[1 : window + 1] if value is not None]
    if len(seed_values) < window:
        return out
    previous = sum(seed_values) / len(seed_values)
    out[window] = previous
    for index in range(window + 1, len(values)):
        value = values[index]
        if value is None:
            out[index] = previous
            continue
        previous = (previous * (window - 1) + value) / window
        out[index] = previous
    return out


def _rsi(values: Sequence[float], window: int) -> list[float | None]:
    """Wilder's RSI. 100 when there are no losses, 50 when nothing moved."""

    out: list[float | None] = [None] * len(values)
    if window < 1 or len(values) <= window:
        return out
    gains: list[float | None] = [None]
    losses: list[float | None] = [None]
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(change if change > 0 else 0.0)
        losses.append(-change if change < 0 else 0.0)
    average_gain = sum(gains[1 : window + 1]) / window  # type: ignore[misc]
    average_loss = sum(losses[1 : window + 1]) / window  # type: ignore[misc]
    out[window] = _rsi_value(average_gain, average_loss)
    for index in range(window + 1, len(values)):
        average_gain = (average_gain * (window - 1) + gains[index]) / window
        average_loss = (average_loss * (window - 1) + losses[index]) / window
        out[index] = _rsi_value(average_gain, average_loss)
    return out


def _rsi_value(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + average_gain / average_loss))


def _rolling(
    values: Sequence[float], window: int, *, pick: str
) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if window < 1 or len(values) < window:
        return out
    for index in range(window - 1, len(values)):
        chunk = values[index - window + 1 : index + 1]
        out[index] = max(chunk) if pick == "max" else min(chunk)
    return out


def _stddev(values: Sequence[float], window: int) -> list[float | None]:
    """Population standard deviation, matching pandas' ``ddof=0``."""

    out: list[float | None] = [None] * len(values)
    if window < 1 or len(values) < window:
        return out
    for index in range(window - 1, len(values)):
        chunk = values[index - window + 1 : index + 1]
        mean = sum(chunk) / window
        variance = sum((value - mean) ** 2 for value in chunk) / window
        out[index] = variance**0.5
    return out


def _percent_of(
    numerator: Sequence[float | None], denominator: Sequence[float | None]
) -> list[float | None]:
    out: list[float | None] = []
    for top, bottom in zip(numerator, denominator, strict=True):
        if top is None or bottom is None or bottom == 0:
            out.append(None)
        else:
            out.append(top / bottom * 100.0)
    return out


def _percent_change(
    values: Sequence[float], reference: Sequence[float | None]
) -> list[float | None]:
    """Change relative to a reference series, as a percentage.

    Matches v2's helper, which is ``value / reference * 100 - 100``. Dropping
    the trailing subtraction turns a percentage change into a ratio of 100,
    which is exactly the kind of quiet wrongness the golden fixture exists to
    catch.
    """

    return [
        None if value is None else value - 100.0
        for value in _percent_of(values, reference)
    ]


def _above(
    values: Sequence[float], reference: Sequence[float | None]
) -> list[bool | None]:
    return [
        None if other is None else value > other
        for value, other in zip(values, reference, strict=True)
    ]


# -- the frame -------------------------------------------------------------- #


def compute(
    bars: Sequence[Bar],
    *,
    parameters: IndicatorParameters | None = None,
    day_levels: DayLevels | None = None,
) -> list[dict[str, Any]]:
    """Every indicator value for every bar, oldest first.

    One dict per bar mirroring v2's frame rows. ``None`` means "not enough
    history yet", never a guessed value.
    """

    params = parameters or IndicatorParameters()
    count = len(bars)
    if count == 0:
        return []

    opens = [float(bar.open) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    closes = [float(bar.close) for bar in bars]
    volumes = [float(bar.volume) for bar in bars]

    previous_close = _shift(closes, 1)
    true_range: list[float | None] = []
    for index in range(count):
        prior = previous_close[index]
        if prior is None:
            # The first bar has nothing to gap against, so its range is the bar.
            true_range.append(highs[index] - lows[index])
        else:
            true_range.append(
                max(
                    highs[index] - lows[index],
                    abs(highs[index] - prior),
                    abs(lows[index] - prior),
                )
            )

    sma = _sma(closes, params.sma_window)
    ema = _ema(closes, params.ema_window)
    rolling_high = _rolling(highs, params.rolling_window, pick="max")
    rolling_low = _rolling(lows, params.rolling_window, pick="min")
    middle = _sma(closes, params.bollinger_window)
    deviation = _stddev(closes, params.bollinger_window)
    atr = _wilder(true_range, params.atr_window)
    rsi = _rsi(closes, params.rsi_window)
    roc = _percent_change(closes, _shift(closes, params.roc_window))
    volume_average = _sma(volumes, params.volume_window)

    upper: list[float | None] = []
    lower: list[float | None] = []
    width: list[float | None] = []
    for index in range(count):
        mid, dev = middle[index], deviation[index]
        if mid is None or dev is None:
            upper.append(None)
            lower.append(None)
            width.append(None)
            continue
        top = mid + params.bollinger_deviation * dev
        bottom = mid - params.bollinger_deviation * dev
        upper.append(top)
        lower.append(bottom)
        width.append(None if mid == 0 else (top - bottom) / mid * 100.0)

    # VWAP and cumulative volume are session-scoped: ours spans several
    # sessions, so carrying them across the whole series would be nonsense.
    cumulative: list[float | None] = []
    vwap: list[float | None] = []
    session_volume = 0.0
    session_value = 0.0
    current_session = None
    for index, bar in enumerate(bars):
        session = session_open_for(bar.open_time)
        if session != current_session:
            current_session = session
            session_volume = 0.0
            session_value = 0.0
        typical = (highs[index] + lows[index] + closes[index]) / 3.0
        session_volume += volumes[index]
        session_value += typical * volumes[index]
        cumulative.append(session_volume)
        vwap.append(session_value / session_volume if session_volume else None)

    levels = day_levels.pivots() if day_levels is not None else {}

    rows: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        close = closes[index]
        open_price = opens[index]
        if close > open_price:
            colour = "green"
        elif close < open_price:
            colour = "red"
        else:
            colour = "doji"

        row: dict[str, Any] = {
            "body": float(bar.close - bar.open),
            "upper_wick": float(bar.high - max(bar.open, bar.close)),
            "lower_wick": float(min(bar.open, bar.close) - bar.low),
            "candle_colour": colour,
            "previous_close": previous_close[index],
            "gap_pct": (
                None
                if previous_close[index] in (None, 0)
                else (open_price - previous_close[index]) / previous_close[index] * 100.0  # type: ignore[operator]
            ),
            "change_pct": (
                None
                if previous_close[index] in (None, 0)
                else (close - previous_close[index]) / previous_close[index] * 100.0  # type: ignore[operator]
            ),
            "range_pct": None if close == 0 else (highs[index] - lows[index]) / close * 100.0,
            "true_range": true_range[index],
            "sma_20": sma[index],
            "ema_20": ema[index],
            "above_sma": _above(closes, sma)[index],
            "above_ema": _above(closes, ema)[index],
            "rolling_high_20": rolling_high[index],
            "rolling_low_20": rolling_low[index],
            "distance_from_high_pct": _percent_of(
                [
                    None if rolling_high[index] is None else rolling_high[index] - close  # type: ignore[operator]
                ],
                [rolling_high[index]],
            )[0],
            "distance_from_low_pct": _percent_of(
                [None if rolling_low[index] is None else close - rolling_low[index]],  # type: ignore[operator]
                [rolling_low[index]],
            )[0],
            "bb_20_middle": middle[index],
            "bb_20_upper": upper[index],
            "bb_20_lower": lower[index],
            "bb_20_width_pct": width[index],
            "stddev_20": deviation[index],
            "atr_14": atr[index],
            "atr_pct": None if close == 0 or atr[index] is None else atr[index] / close * 100.0,  # type: ignore[operator]
            "rsi_14": rsi[index],
            "roc_5": roc[index],
            "volume_sma_20": volume_average[index],
            "volume_ratio_20": (
                None
                if volume_average[index] in (None, 0)
                else volumes[index] / volume_average[index]  # type: ignore[operator]
            ),
            "volume_vs_previous": (
                None
                if index == 0 or volumes[index - 1] == 0
                else volumes[index] / volumes[index - 1]
            ),
            "cumulative_volume": cumulative[index],
            "vwap": vwap[index],
            # The first thing anyone asks about an intraday tape, and the one
            # comparison the filter vocabulary cannot express: a condition
            # compares a field against a value, never against another field, so
            # "close above VWAP" has to be published as a fact of its own.
            "above_vwap": (None if vwap[index] is None else close > vwap[index]),
        }
        for window in params.momentum_windows:
            shifted = _shift(closes, window)
            row[f"momentum_{window}"] = (
                None if shifted[index] is None else close - shifted[index]  # type: ignore[operator]
            )
            row[f"momentum_pct_{window}"] = (
                None
                if shifted[index] in (None, 0)
                else (close - shifted[index]) / shifted[index] * 100.0  # type: ignore[operator]
            )
        ratio = row["volume_ratio_20"]
        row["unusual_volume"] = None if ratio is None else ratio >= params.unusual_volume_ratio

        if day_levels is not None:
            previous_day_close = float(day_levels.close)
            row["previous_day_high"] = float(day_levels.high)
            row["previous_day_low"] = float(day_levels.low)
            row["previous_day_close"] = previous_day_close
            row["day_change_pct"] = (
                None
                if previous_day_close == 0
                else (close - previous_day_close) / previous_day_close * 100.0
            )
            found = nearest_level(close, levels)
            row["level_distance_pct"] = None if found is None else found[2]
            row["level_distance_name"] = None if found is None else found[0]
        else:
            row["previous_day_high"] = None
            row["previous_day_low"] = None
            row["previous_day_close"] = None
            row["day_change_pct"] = None
            row["level_distance_pct"] = None
            row["level_distance_name"] = None

        rows.append(row)
    return rows


def compute_latest(
    bars: Sequence[Bar],
    *,
    parameters: IndicatorParameters | None = None,
    day_levels: DayLevels | None = None,
    warmup_bars: int = WARMUP_BARS,
) -> dict[str, Any] | None:
    """Indicator values for the newest bar only.

    This is the entry point a live service uses: it smooths over at most
    ``warmup_bars`` recent bars, which bounds the work no matter how much
    history is held.
    """

    params = parameters or IndicatorParameters()
    depth = max(warmup_bars, params.longest_window + 1)
    window = bars[-depth:] if len(bars) > depth else bars
    rows = compute(window, parameters=params, day_levels=day_levels)
    return rows[-1] if rows else None


# -- what the fields are ---------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One published field: what it is called and how to render it."""

    name: str
    label: str
    group: str
    decimals: int = 2
    unit: str = "number"
    #: "table" fields are columns; "inspector" fields are drill-down only.
    surface: str = "table"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "group": self.group,
            "decimals": self.decimals,
            "unit": self.unit,
            "surface": self.surface,
        }


#: Groups in the order the table shows them, with their default visibility.
GROUP_ORDER: Final[tuple[tuple[str, str, bool], ...]] = (
    ("trend", "Trend", True),
    ("momentum", "Momentum", True),
    ("volatility", "Volatility", True),
    ("volume", "Volume", True),
    ("levels", "Levels", True),
    ("shape", "Candle shape", False),
)

#: Every field ``compute`` produces, with its presentation. Kept beside the
#: code that produces the names so the two cannot drift apart.
FIELDS: Final[tuple[FieldSpec, ...]] = (
    FieldSpec("sma_20", "SMA 20", "trend", unit="price"),
    FieldSpec("ema_20", "EMA 20", "trend", unit="price"),
    FieldSpec("above_sma", "Above SMA", "trend", unit="bool"),
    FieldSpec("above_ema", "Above EMA", "trend", unit="bool"),
    FieldSpec("above_vwap", "Above VWAP", "trend", unit="bool"),
    FieldSpec("rolling_high_20", "High 20", "trend", unit="price"),
    FieldSpec("rolling_low_20", "Low 20", "trend", unit="price"),
    FieldSpec("distance_from_high_pct", "From high %", "trend", unit="percent"),
    FieldSpec("distance_from_low_pct", "From low %", "trend", unit="percent"),
    FieldSpec("rsi_14", "RSI 14", "momentum", 1, "number"),
    FieldSpec("roc_5", "ROC 5 %", "momentum", unit="percent"),
    FieldSpec("momentum_5", "Mom 5", "momentum", unit="price"),
    FieldSpec("momentum_15", "Mom 15", "momentum", unit="price"),
    FieldSpec("momentum_30", "Mom 30", "momentum", unit="price"),
    FieldSpec("momentum_pct_5", "Mom 5 %", "momentum", unit="percent"),
    FieldSpec("momentum_pct_15", "Mom 15 %", "momentum", unit="percent"),
    FieldSpec("momentum_pct_30", "Mom 30 %", "momentum", unit="percent"),
    FieldSpec("change_pct", "Change %", "momentum", unit="percent"),
    FieldSpec("atr_14", "ATR 14", "volatility", unit="price"),
    FieldSpec("atr_pct", "ATR %", "volatility", unit="percent"),
    FieldSpec("range_pct", "Range %", "volatility", unit="percent"),
    FieldSpec("true_range", "True range", "volatility", unit="price"),
    FieldSpec("stddev_20", "Std dev 20", "volatility", unit="price"),
    FieldSpec("bb_20_middle", "BB mid", "volatility", unit="price"),
    FieldSpec("bb_20_upper", "BB upper", "volatility", unit="price"),
    FieldSpec("bb_20_lower", "BB lower", "volatility", unit="price"),
    FieldSpec("bb_20_width_pct", "BB width %", "volatility", unit="percent"),
    FieldSpec("volume_sma_20", "Vol SMA 20", "volume", 0, "volume"),
    FieldSpec("volume_ratio_20", "Vol ×", "volume", unit="ratio"),
    FieldSpec("volume_vs_previous", "Vol vs prev", "volume", unit="ratio"),
    FieldSpec("cumulative_volume", "Cum volume", "volume", 0, "volume"),
    FieldSpec("unusual_volume", "Unusual vol", "volume", unit="bool"),
    FieldSpec("vwap", "VWAP", "volume", unit="price"),
    # Session constants rather than per-bar values, so they are published for
    # the filter vocabulary and the inspector but not as table columns - three
    # columns repeating one number down the screen is noise, and not being able
    # to filter on "within reach of the previous day's high" is not.
    FieldSpec("previous_day_high", "Prev day high", "levels", unit="price", surface="inspector"),
    FieldSpec("previous_day_low", "Prev day low", "levels", unit="price", surface="inspector"),
    FieldSpec("previous_day_close", "Prev day close", "levels", unit="price", surface="inspector"),
    FieldSpec("level_distance_pct", "To level %", "levels", unit="percent"),
    FieldSpec("level_distance_name", "Nearest", "levels", unit="text"),
    FieldSpec("day_change_pct", "Day %", "levels", unit="percent"),
    FieldSpec("body", "Body", "shape", unit="price"),
    FieldSpec("upper_wick", "Upper wick", "shape", unit="price"),
    FieldSpec("lower_wick", "Lower wick", "shape", unit="price"),
    FieldSpec("candle_colour", "Colour", "shape", unit="text"),
    FieldSpec("previous_close", "Prev close", "shape", unit="price"),
    FieldSpec("gap_pct", "Gap %", "shape", unit="percent"),
)


def catalogue() -> dict[str, Any]:
    """The published field list, so the UI never hard-codes forty names."""

    groups = []
    for key, label, default_on in GROUP_ORDER:
        groups.append(
            {
                "key": key,
                "label": label,
                "default": default_on,
                "fields": [spec.describe() for spec in FIELDS if spec.group == key],
            }
        )
    return {
        "version": INDICATOR_VERSION,
        "warmup_bars": WARMUP_BARS,
        "parameters": IndicatorParameters().describe(),
        "groups": groups,
    }
