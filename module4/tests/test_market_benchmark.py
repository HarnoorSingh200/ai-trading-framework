"""The benchmark index: mirrored from Module 1, kept beside the archive."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from module4.benchmark import Benchmark, BenchmarkStore
from module4.timeutil import IST

pytestmark = pytest.mark.unit


class FakeBenchmarkClient:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {
            "symbol": "NIFTY 50",
            "interval": "1d",
            "source": "broker historical (1d)",
            "fetched_at": "2026-10-22T00:00:00.000Z",
            "candles": [
                {"date": "2026-10-21", "close": 25100.0},
                {"date": "2026-10-22", "close": 25200.0},
            ],
        }
        self.calls = 0

    def benchmark(self) -> dict[str, Any]:
        self.calls += 1
        return self.payload


class BrokenBenchmarkClient:
    def benchmark(self) -> dict[str, Any]:
        raise RuntimeError("Module 1 is unreachable")


def clock() -> datetime:
    return datetime(2026, 10, 22, 15, 0, tzinfo=IST)


def test_a_sync_keeps_the_daily_closes(tmp_path: Path) -> None:
    store = BenchmarkStore(tmp_path)
    client = FakeBenchmarkClient()
    bench = Benchmark(store=store, client=client, clock=clock)

    report = bench.sync()

    assert client.calls == 1
    assert report["symbol"] == "NIFTY 50"
    assert report["candles"] == 2
    found = bench.report()
    assert found["candles"] == [
        {"date": "2026-10-21", "close": 25100.0},
        {"date": "2026-10-22", "close": 25200.0},
    ]
    # It outlives the process, like the rest of the archive.
    again = Benchmark(store=BenchmarkStore(tmp_path), client=None, clock=clock)
    assert again.report()["candles"][-1]["close"] == 25200.0


def test_a_failed_read_leaves_the_last_known_benchmark(tmp_path: Path) -> None:
    good = Benchmark(
        store=BenchmarkStore(tmp_path), client=FakeBenchmarkClient(), clock=clock
    )
    good.sync()

    broken = Benchmark(
        store=BenchmarkStore(tmp_path), client=BrokenBenchmarkClient(), clock=clock
    )
    report = broken.sync()

    assert "unreachable" in (report["error"] or "")
    assert broken.report()["candles"][0]["close"] == 25100.0
    assert broken.status()["failures"] == 1


def test_bad_rows_are_skipped_rather_than_stored(tmp_path: Path) -> None:
    client = FakeBenchmarkClient(
        {
            "symbol": "NIFTY 50",
            "candles": [
                {"date": "2026-10-21", "close": 25100.0},
                {"date": "2026-10-22", "close": "not a number"},
                {"close": 1.0},
                "junk",
            ],
        }
    )
    bench = Benchmark(store=BenchmarkStore(tmp_path), client=client, clock=clock)

    report = bench.sync()

    assert report["candles"] == 1
    assert bench.report()["candles"] == [{"date": "2026-10-21", "close": 25100.0}]


def test_a_benchmark_without_a_client_is_skipped_not_raised(tmp_path: Path) -> None:
    bench = Benchmark(store=BenchmarkStore(tmp_path), client=None, clock=clock)

    assert bench.sync()["skipped"]
    assert bench.report()["candles"] == []
