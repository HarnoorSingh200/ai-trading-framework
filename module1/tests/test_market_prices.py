"""Watching a named set of stocks, which is what Module 3 does.

The board answers for the whole universe; these tests pin the other question -
the price of the stocks a caller actually holds, asked about by name. The two
facts worth defending are that a caller is never handed a row it did not ask
for, and that a symbol this module cannot price is named rather than dropped.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from module1.broker.feed import Tick
from module1.broker.kite import Candle
from module1.config import Settings
from module1.market import SOURCE_LIVE, SOURCE_NONE, SOURCE_SESSION_CLOSE, MarketBoard
from module1.service import create_app
from module1.timeutil import IST
from module1.universe import Constituent

pytestmark = pytest.mark.unit

#: A fixed "now": 2026-09-21 was a Monday, 16:00 IST is after the close.
FIXED_NOW = datetime(2026, 9, 21, 16, 0, tzinfo=IST)
TODAY_OPEN = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
PREVIOUS_OPEN = datetime(2026, 9, 18, 9, 15, tzinfo=IST)

UNIVERSE = (
    Constituent("RELIANCE", "Reliance Industries Ltd.", "Energy"),
    Constituent("TCS", "Tata Consultancy Services Ltd.", "IT"),
    Constituent("INFY", "Infosys Ltd.", "IT"),
)

TOKENS = {"RELIANCE": "738561", "TCS": "2953217", "INFY": "408065"}


class FakeClient:
    """Stands in for the broker: canned daily candles, no network."""

    def __init__(self, candles: dict[str, list[Candle]]) -> None:
        self._candles = candles

    def day_candles(self, token: str, *, sessions: int, now: datetime) -> list[Candle]:
        return self._candles.get(token, [])

    def close(self) -> None:
        return None

    def status(self) -> dict[str, object]:
        return {}


class FakeMaster:
    def resolve(self, symbols: list[str]):
        return (
            {
                symbol: type("Record", (), {"instrument_token": TOKENS[symbol]})()
                for symbol in symbols
            },
            [],
        )

    def describe(self) -> dict[str, object]:
        return {"source": "test"}

    def close(self) -> None:
        return None


class FakeSession:
    def ensure(self, *, force: bool = False):
        return type("Held", (), {"token": "t", "user_id": "AB1234"})()

    def status(self) -> dict[str, object]:
        return {"valid": True}


def _settings(tmp_path: Path, *, tick_fresh_seconds: float = 120.0) -> Settings:
    return Settings(
        environment="test",
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        secrets_file=None,
        holidays_file=tmp_path / "nse.json",
        holidays_seed=tmp_path / "seed.json",
        holidays_refresh_seconds=86400.0,
        refresh_seconds=3600.0,
        history_days=10,
        history_rate_limit_seconds=0.0,
        tick_fresh_seconds=tick_fresh_seconds,
        feed_enabled=False,
        warm_sessions=1,
        repair_seconds=180.0,
    )


def _candle(moment: datetime, close: str) -> Candle:
    price = Decimal(close)
    return Candle(
        open_time=moment,
        open=price,
        high=price + 5,
        low=price - 5,
        close=price,
        volume=Decimal("1000"),
    )


def _board(
    tmp_path: Path, candles: dict[str, list[Candle]] | None = None
) -> MarketBoard:
    board = MarketBoard(
        _settings(tmp_path),
        session=FakeSession(),
        client=FakeClient(candles or {}),
        universe=UNIVERSE,
        master=FakeMaster(),
        clock=lambda: FIXED_NOW,
    )
    board._tokens = dict(TOKENS)
    return board


def _today_sessions() -> dict[str, list[Candle]]:
    """Every symbol traded today, so each row has a stored price to fall back on."""

    return {
        TOKENS[symbol]: [
            _candle(PREVIOUS_OPEN, "1200"),
            _candle(TODAY_OPEN, "1234.5"),
        ]
        for symbol in TOKENS
    }


def _client(board: MarketBoard) -> TestClient:
    return TestClient(create_app(SimpleNamespace(board=board)))  # type: ignore[arg-type]


# -- the read ---------------------------------------------------------------- #


def test_only_the_requested_symbols_come_back(tmp_path: Path) -> None:
    board = _board(tmp_path, _today_sessions())
    board.refresh_history()

    payload = board.price_rows(["TCS"])

    assert payload["requested"] == ["TCS"]
    assert [row["symbol"] for row in payload["rows"]] == ["TCS"]
    assert payload["unavailable"] == []


def test_a_symbol_this_module_cannot_price_is_named_rather_than_dropped(
    tmp_path: Path,
) -> None:
    """Silence cannot be told apart from "flat", so it is not used."""

    board = _board(tmp_path, _today_sessions())
    board.refresh_history()

    payload = board.price_rows(["TCS", "NOT_IN_THE_INDEX"])

    assert [row["symbol"] for row in payload["rows"]] == ["TCS"]
    assert payload["unavailable"] == ["NOT_IN_THE_INDEX"]
    assert payload["count"] == 1


def test_symbols_are_normalised_and_de_duplicated(tmp_path: Path) -> None:
    board = _board(tmp_path, _today_sessions())
    board.refresh_history()

    payload = board.price_rows([" tcs ", "TCS", "", "INFY"])

    assert payload["requested"] == ["TCS", "INFY"]
    assert [row["symbol"] for row in payload["rows"]] == ["TCS", "INFY"]


def test_a_live_tick_travels_with_its_source_and_age(tmp_path: Path) -> None:
    board = _board(tmp_path, _today_sessions())
    board.refresh_history()
    board.on_tick(
        Tick(
            instrument_token=TOKENS["TCS"],
            price=Decimal("1260.25"),
            received_at=FIXED_NOW - timedelta(seconds=4),
            symbol="TCS",
        )
    )

    row = board.price_rows(["TCS"])["rows"][0]

    assert row["price_source"] == SOURCE_LIVE
    assert row["last_price"] == 1260.25
    assert row["price_age_seconds"] == 4.0
    assert row["price_at"] is not None


def test_a_stored_close_is_never_offered_as_a_live_price(tmp_path: Path) -> None:
    """A filled order must not be built on a price nobody could have traded at."""

    board = _board(tmp_path, _today_sessions())
    board.refresh_history()

    row = board.price_rows(["TCS"])["rows"][0]

    assert row["price_source"] == SOURCE_SESSION_CLOSE
    assert row["last_price"] == 1234.5
    # The daily candle is stamped at midnight, so a seconds-old age would be a
    # very precise number about nothing.
    assert row["price_age_seconds"] is None


def test_a_symbol_with_no_data_says_so_rather_than_inventing_a_price(
    tmp_path: Path,
) -> None:
    board = _board(tmp_path)

    row = board.price_rows(["RELIANCE"])["rows"][0]

    assert row["price_source"] == SOURCE_NONE
    assert row["last_price"] is None


def test_the_market_line_travels_with_the_answer(tmp_path: Path) -> None:
    board = _board(tmp_path, _today_sessions())
    board.refresh_history()

    payload = board.price_rows(["TCS"])

    assert payload["market"]["exchange"] == "NSE"
    assert payload["market"]["is_open"] is False


# -- the HTTP surface -------------------------------------------------------- #


def test_the_price_endpoint_answers_for_the_watched_rows(tmp_path: Path) -> None:
    board = _board(tmp_path, _today_sessions())
    board.refresh_history()

    response = _client(board).get("/v1/market/prices?symbols=TCS,INFY")

    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["symbol"] for row in data["rows"]] == ["TCS", "INFY"]
    assert data["unavailable"] == []


def test_the_price_endpoint_without_symbols_is_refused_with_the_reason(
    tmp_path: Path,
) -> None:
    response = _client(_board(tmp_path)).get("/v1/market/prices")

    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "SYMBOLS_REQUIRED"
    assert "symbols=" in error["message"]
