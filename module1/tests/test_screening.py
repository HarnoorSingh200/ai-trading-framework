"""Screening: the vocabulary, the comparisons, and the projection.

Module 2's agents filter the universe through this, so the rules here are
load-bearing rather than cosmetic: a field the catalogue does not name cannot
be filtered on, a value that is ``null`` because the history is too short is
never treated as a number, and the rows that come back carry exactly the fields
that were asked for and nothing else.
"""

from __future__ import annotations

from typing import Any

import pytest

from module1.indicators import FIELDS
from module1.screening import (
    MAX_LIMIT,
    ScreenError,
    catalogue,
    screen_rows,
)

pytestmark = pytest.mark.unit


def row(symbol: str, **values: Any) -> dict[str, Any]:
    """One screening row: the closed bar's fields plus its indicator values."""

    base: dict[str, Any] = {
        "symbol": symbol,
        "name": f"{symbol} Ltd.",
        "industry": "IT",
        "close": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.5,
        "volume": 10_000.0,
        "change_percent": 1.0,
        "volume_ratio": 1.0,
        "rsi_14": 50.0,
        "candle_colour": "green",
        "unusual_volume": False,
    }
    base.update(values)
    return base


def test_the_catalogue_names_every_published_indicator_field() -> None:
    """A field the model cannot discover is a field it will guess."""

    payload = catalogue("15m")
    named = {field["name"] for field in payload["fields"]}

    assert named >= {spec.name for spec in FIELDS}
    # The closed bar's own numbers are filterable too.
    assert {"close", "volume", "change_percent", "volume_ratio"} <= named
    # A list-valued field is not a filter.
    assert "spark" not in named


def test_the_catalogue_says_how_each_field_may_be_compared() -> None:
    payload = catalogue("15m")
    fields = {field["name"]: field for field in payload["fields"]}

    assert fields["rsi_14"]["unit"] == "number"
    assert fields["rsi_14"]["ops"] == ["<", "<=", ">", ">=", "==", "!="]
    # A text field compares for equality; ordering a colour is meaningless.
    assert fields["candle_colour"]["ops"] == ["==", "!="]
    assert fields["unusual_volume"]["ops"] == ["==", "!="]
    assert payload["limit"]["max"] == MAX_LIMIT


def test_a_condition_selects_the_rows_it_describes() -> None:
    rows = [
        row("AAA", rsi_14=22.0),
        row("BBB", rsi_14=48.0),
        row("CCC", rsi_14=71.0),
    ]

    result = screen_rows(rows, where=[{"field": "rsi_14", "op": "<", "value": 30}])

    assert [item["symbol"] for item in result["rows"]] == ["AAA"]
    assert result["matched"] == 1
    assert result["screened"] == 3


def test_null_means_not_enough_history_and_never_matches() -> None:
    """An RSI that is null is not an RSI of zero, and not a buy either."""

    rows = [row("AAA", rsi_14=None), row("BBB", rsi_14=25.0)]

    result = screen_rows(rows, where=[{"field": "rsi_14", "op": "<", "value": 30}])

    assert [item["symbol"] for item in result["rows"]] == ["BBB"]


def test_a_row_missing_the_field_entirely_is_treated_the_same_way() -> None:
    rows = [{"symbol": "AAA", "close": 10.0}, row("BBB", rsi_14=25.0)]

    result = screen_rows(rows, where=[{"field": "rsi_14", "op": "<", "value": 30}])

    assert [item["symbol"] for item in result["rows"]] == ["BBB"]


def test_all_is_the_default_and_any_is_an_or() -> None:
    rows = [
        row("AAA", rsi_14=25.0, volume_ratio=0.5),
        row("BBB", rsi_14=55.0, volume_ratio=3.0),
        row("CCC", rsi_14=25.0, volume_ratio=3.0),
    ]
    conditions = [
        {"field": "rsi_14", "op": "<", "value": 30},
        {"field": "volume_ratio", "op": ">", "value": 2.0},
    ]

    every = screen_rows(rows, where=conditions)
    either = screen_rows(rows, where=conditions, match="any")

    assert [item["symbol"] for item in every["rows"]] == ["CCC"]
    assert [item["symbol"] for item in either["rows"]] == ["AAA", "BBB", "CCC"]


def test_text_and_flag_fields_compare_for_equality() -> None:
    rows = [
        row("AAA", candle_colour="red", industry="Energy"),
        row("BBB", candle_colour="green", industry="IT", unusual_volume=True),
    ]

    colour = screen_rows(rows, where=[{"field": "candle_colour", "op": "==", "value": "green"}])
    flag = screen_rows(rows, where=[{"field": "unusual_volume", "op": "==", "value": True}])
    sector = screen_rows(rows, where=[{"field": "industry", "op": "!=", "value": "IT"}])

    assert [item["symbol"] for item in colour["rows"]] == ["BBB"]
    assert [item["symbol"] for item in flag["rows"]] == ["BBB"]
    assert [item["symbol"] for item in sector["rows"]] == ["AAA"]


def test_ranking_puts_the_biggest_first_and_null_last() -> None:
    rows = [
        row("AAA", volume_ratio=1.0),
        row("BBB", volume_ratio=None),
        row("CCC", volume_ratio=4.0),
    ]

    ranked = screen_rows(rows, where=[], rank_by="volume_ratio", direction="desc", limit=2)
    ascending = screen_rows(rows, where=[], rank_by="volume_ratio", direction="asc")

    assert [item["symbol"] for item in ranked["rows"]] == ["CCC", "AAA"]
    assert [item["symbol"] for item in ascending["rows"]] == ["AAA", "CCC", "BBB"]


def test_a_row_carries_only_the_fields_that_were_asked_for() -> None:
    rows = [row("AAA", rsi_14=25.0)]

    result = screen_rows(
        rows,
        where=[{"field": "rsi_14", "op": "<", "value": 30}],
        fields=["rsi_14", "volume_ratio"],
    )

    assert result["rows"][0] == {
        "symbol": "AAA",
        "name": "AAA Ltd.",
        "industry": "IT",
        "rsi_14": 25.0,
        "volume_ratio": 1.0,
    }


def test_the_projection_defaults_to_what_the_question_was_about() -> None:
    """A filtered field the model cannot see back is a field it will mis-trust."""

    rows = [row("AAA", atr_pct=0.4)]

    result = screen_rows(
        rows,
        where=[{"field": "atr_pct", "op": "<", "value": 0.5}],
        rank_by="volume_ratio",
    )

    assert {"atr_pct", "volume_ratio", "close"} <= set(result["rows"][0])
    assert "cumulative_volume" not in result["rows"][0]


def test_an_unknown_field_is_refused_with_the_reason() -> None:
    with pytest.raises(ScreenError) as failure:
        screen_rows([row("AAA")], where=[{"field": "rsi", "op": "<", "value": 30}])

    assert "rsi" in str(failure.value)
    assert failure.value.field == "rsi"


def test_a_guessed_field_name_is_refused_with_the_nearest_names() -> None:
    """The caller is usually a model that guessed, and a hint costs nothing."""

    with pytest.raises(ScreenError) as failure:
        screen_rows([row("AAA")], where=[{"field": "range", "op": "<", "value": 1}])

    message = str(failure.value)
    assert "range_pct" in message or "range_percent" in message
    assert "the closest are" in message


def test_an_operator_a_field_does_not_take_is_refused() -> None:
    with pytest.raises(ScreenError) as failure:
        screen_rows([row("AAA")], where=[{"field": "candle_colour", "op": "<", "value": "green"}])

    assert failure.value.op == "<"
    assert "candle_colour" in str(failure.value)


def test_a_value_of_the_wrong_kind_is_refused() -> None:
    with pytest.raises(ScreenError):
        screen_rows([row("AAA")], where=[{"field": "rsi_14", "op": "<", "value": "low"}])
    with pytest.raises(ScreenError):
        screen_rows([row("AAA")], where=[{"field": "unusual_volume", "op": "==", "value": "yes"}])
    with pytest.raises(ScreenError):
        # A boolean is not a number, however much Python wants it to be one.
        screen_rows([row("AAA")], where=[{"field": "rsi_14", "op": "<", "value": True}])


def test_a_malformed_condition_is_refused() -> None:
    with pytest.raises(ScreenError):
        screen_rows([row("AAA")], where=[{"field": "rsi_14", "op": "<"}])
    with pytest.raises(ScreenError):
        screen_rows([row("AAA")], where=[{"op": "<", "value": 1}])
    with pytest.raises(ScreenError):
        screen_rows([row("AAA")], where="rsi_14<30")
    with pytest.raises(ScreenError):
        screen_rows([row("AAA")], where=[], match="some")


def test_the_limit_is_bounded() -> None:
    rows = [row(f"S{index:02d}") for index in range(40)]

    assert len(screen_rows(rows, where=[], limit=10_000)["rows"]) == MAX_LIMIT
    assert len(screen_rows(rows, where=[], limit=3)["rows"]) == 3
    with pytest.raises(ScreenError):
        screen_rows(rows, where=[], limit=0)


def test_an_empty_screen_is_an_answer_not_an_error() -> None:
    result = screen_rows([row("AAA", rsi_14=70.0)], where=[{"field": "rsi_14", "op": "<", "value": 30}])

    assert result["matched"] == 0
    assert result["rows"] == []
