"""The benchmark index: one broker read, cached, and resolved from the dump."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from module1.benchmark import BenchmarkError, BenchmarkService
from module1.broker.kite import Candle
from module1.instruments import InstrumentError, InstrumentMaster, InstrumentRecord
from module1.timeutil import IST, format_utc

pytestmark = pytest.mark.unit

SYMBOL = "NIFTY 50"


def clock() -> datetime:
    return datetime(2026, 10, 22, 15, 0, tzinfo=IST)


def candle(day: int, close: float) -> Candle:
    return Candle(
        open_time=datetime(2026, 10, day, 0, 0, tzinfo=IST),
        open=Decimal(str(close - 10)),
        high=Decimal(str(close + 5)),
        low=Decimal(str(close - 15)),
        close=Decimal(str(close)),
        volume=Decimal("0"),
    )


class FakeMaster:
    def __init__(self, *, token: str = "256265", error: Exception | None = None) -> None:
        self.token = token
        self.error = error
        self.lookups: list[str] = []

    def index_record(self, symbol: str) -> InstrumentRecord:
        self.lookups.append(symbol)
        if self.error is not None:
            raise self.error
        return InstrumentRecord(
            instrument_token=self.token,
            symbol=symbol,
            name=symbol,
            tick_size=None,
            lot_size=None,
            instrument_type="INDICES",
            segment="INDICES",
        )


class FakeClient:
    def __init__(self, rows: list[Candle]) -> None:
        self.rows = rows
        self.calls = 0

    def historical(self, token, interval="1m", *, start, end):  # type: ignore[no-untyped-def]
        self.calls += 1
        return list(self.rows)


def test_daily_closes_are_fetched_once_and_cached() -> None:
    client = FakeClient([candle(20, 25000.0), candle(21, 25100.5)])
    found = BenchmarkService(
        master=FakeMaster(), client=client, clock=clock  # type: ignore[arg-type]
    )

    first = found.daily()
    second = found.daily()

    assert client.calls == 1
    assert first["symbol"] == SYMBOL
    assert first["candles"] == [
        {"date": "2026-10-20", "close": 25000.0},
        {"date": "2026-10-21", "close": 25100.5},
    ]
    assert second["candles"] == first["candles"]


def test_the_cache_expires_after_its_refresh_window() -> None:
    times = [clock()]

    def moving_clock() -> datetime:
        return times[0]

    client = FakeClient([candle(21, 25100.0)])
    found = BenchmarkService(
        master=FakeMaster(),  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        refresh_seconds=60.0,
        clock=moving_clock,
    )

    found.daily()
    times[0] = times[0] + timedelta(seconds=61)
    found.daily()

    assert client.calls == 2


def test_an_unresolved_index_is_a_benchmark_error_not_an_http_500() -> None:
    found = BenchmarkService(
        master=FakeMaster(error=InstrumentError("no index")),  # type: ignore[arg-type]
        client=FakeClient([]),  # type: ignore[arg-type]
        clock=clock,
    )

    with pytest.raises(BenchmarkError) as failure:
        found.daily()

    assert "no index" in str(failure.value)
    assert found.describe()["last_error"] == "no index"


def test_an_index_row_is_resolved_from_the_same_dump(tmp_path: Path) -> None:
    """The EQ map drops indices on purpose; the benchmark asks for one explicitly."""

    rows = (
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,"
        "strike,tick_size,lot_size,instrument_type,segment,exchange\n"
        "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
        "408065,319,NIFTY 50,NIFTY 50 BANK,0,,0,0,0,EQ,INDICES,NSE\n"
        "738561,356,RELIANCE,Reliance Industries,0,,0,0,0,EQ,NSE,NSE\n"
    )
    store = tmp_path / "data"
    store.mkdir()
    (store / "instruments_nse.csv").write_text(rows, encoding="utf-8")
    (store / "instruments_nse.meta.json").write_text(
        json.dumps({"fetched_at": format_utc(clock())}), encoding="utf-8"
    )
    master = InstrumentMaster(store, url="http://example.test", clock=clock)

    record = master.index_record("nifty 50")

    assert record.instrument_token == "256265"
    assert record.segment == "INDICES"
    # The EQ map still has only the equity, as it should.
    assert "RELIANCE" in master.load()
    assert SYMBOL not in master.load()

    with pytest.raises(InstrumentError):
        master.index_record("SENSEX")
