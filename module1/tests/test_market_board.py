"""The board: what a row says, and whether it admits where its price is from."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from module1.broker.feed import Tick
from module1.broker.kite import Candle
from module1.config import Settings
from module1.market import (
    SOURCE_LIVE,
    SOURCE_NONE,
    SOURCE_PREVIOUS_CLOSE,
    SOURCE_SESSION_CLOSE,
    MarketBoard,
)
from module1.timeutil import IST
from module1.universe import Constituent

pytestmark = pytest.mark.unit

#: A fixed "now": 2026-09-21 was a Monday, and 16:00 IST is after the close.
FIXED_NOW = datetime(2026, 9, 21, 16, 0, tzinfo=IST)
TODAY_OPEN = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
PREVIOUS_OPEN = datetime(2026, 9, 18, 9, 15, tzinfo=IST)
OLDER_OPEN = datetime(2026, 9, 17, 9, 15, tzinfo=IST)


class FakeClient:
    """Stands in for the broker: canned daily candles, no network."""

    def __init__(self, candles: dict[str, list[Candle]]) -> None:
        self._candles = candles
        self.closed = False

    def day_candles(self, token: str, *, sessions: int, now: datetime) -> list[Candle]:
        return self._candles.get(token, [])

    def close(self) -> None:
        self.closed = True

    def status(self) -> dict[str, object]:
        return {}


class FakeMaster:
    def resolve(self, symbols: list[str]):
        found = {
            symbol: type("Record", (), {"instrument_token": str(index + 100)})()
            for index, symbol in enumerate(symbols)
        }
        return found, []

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
    tmp_path: Path, candles: dict[str, list[Candle]], *, tick_fresh_seconds: float = 120.0
) -> MarketBoard:
    universe = [Constituent("RELIANCE", "Reliance Industries Ltd.", "Energy")]
    board = MarketBoard(
        _settings(tmp_path, tick_fresh_seconds=tick_fresh_seconds),
        session=FakeSession(),
        client=FakeClient(candles),
        universe=universe,
        master=FakeMaster(),
        clock=lambda: FIXED_NOW,
    )
    board._tokens = {"RELIANCE": "738561"}
    return board


def test_a_closed_market_reports_the_session_close(tmp_path: Path) -> None:
    board = _board(
        tmp_path,
        {
            "738561": [
                _candle(PREVIOUS_OPEN, "1200"),
                _candle(TODAY_OPEN, "1234.5"),
            ]
        },
    )
    board.refresh_history()

    row = board.snapshot()["rows"][0]

    assert row["price_source"] == SOURCE_SESSION_CLOSE
    assert row["last_price"] == 1234.5
    assert row["previous_close"] == 1200.0
    assert row["change"] == 34.5
    assert row["change_percent"] == 2.88


def test_when_today_has_not_traded_the_previous_close_is_the_reference(
    tmp_path: Path,
) -> None:
    """Before the open, the last close is both the price and the baseline."""

    board = _board(
        tmp_path,
        {"738561": [_candle(OLDER_OPEN, "1200"), _candle(PREVIOUS_OPEN, "1250")]},
    )
    board.refresh_history()

    row = board.snapshot()["rows"][0]

    assert row["price_source"] == SOURCE_PREVIOUS_CLOSE
    assert row["last_price"] == 1250.0
    assert row["previous_close"] == 1250.0
    assert row["change"] == 0.0


def test_during_a_session_the_move_is_measured_from_the_last_close(
    tmp_path: Path,
) -> None:
    """The case that matters intraday: today's candle does not exist yet."""

    board = _board(
        tmp_path,
        {"738561": [_candle(OLDER_OPEN, "1200"), _candle(PREVIOUS_OPEN, "1250")]},
    )
    board.refresh_history()
    board.on_tick(
        Tick(
            instrument_token="738561",
            price=Decimal("1287.50"),
            received_at=FIXED_NOW,
            symbol="RELIANCE",
        )
    )

    row = board.snapshot()["rows"][0]

    assert row["price_source"] == SOURCE_LIVE
    assert row["last_price"] == 1287.5
    assert row["previous_close"] == 1250.0
    assert row["change"] == 37.5
    assert row["change_percent"] == 3.0


def test_a_live_tick_wins_over_stored_history(tmp_path: Path) -> None:
    board = _board(
        tmp_path,
        {
            "738561": [
                _candle(PREVIOUS_OPEN, "1200"),
                _candle(TODAY_OPEN, "1234.5"),
            ]
        },
    )
    board.refresh_history()
    board.on_tick(
        Tick(
            instrument_token="738561",
            price=Decimal("1260.25"),
            received_at=FIXED_NOW,
            symbol="RELIANCE",
        )
    )

    row = board.snapshot()["rows"][0]

    assert row["price_source"] == SOURCE_LIVE
    assert row["last_price"] == 1260.25


def test_a_stale_tick_is_not_shown_as_live(tmp_path: Path) -> None:
    board = _board(
        tmp_path,
        {
            "738561": [
                _candle(PREVIOUS_OPEN, "1200"),
                _candle(TODAY_OPEN, "1234.5"),
            ]
        },
        tick_fresh_seconds=1.0,
    )
    board.refresh_history()
    board.on_tick(
        Tick(
            instrument_token="738561",
            price=Decimal("1260.25"),
            received_at=FIXED_NOW - timedelta(seconds=600),
            symbol="RELIANCE",
        )
    )

    row = board.snapshot()["rows"][0]

    assert row["price_source"] == SOURCE_SESSION_CLOSE
    assert row["last_price"] == 1234.5


def test_a_symbol_with_no_data_says_so_instead_of_inventing_a_price(tmp_path: Path) -> None:
    board = _board(tmp_path, {})

    row = board.snapshot()["rows"][0]

    assert row["price_source"] == SOURCE_NONE
    assert row["last_price"] is None
    assert row["change_percent"] is None
