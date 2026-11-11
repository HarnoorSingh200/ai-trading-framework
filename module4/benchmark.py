"""The NIFTY 50 benchmark, mirrored from Module 1 and kept with the archive.

A benchmark is a market fact, so it belongs to Module 1; this module reads the
index's daily closes over Module 1's HTTP route and keeps them next to the order
archive, so the dashboard can compare what an agent made with what a buy-and-hold
of the index would have made over the same days.

Like the token totals, the benchmark is local-only and last-known-wins: a failed
read leaves what is already stored untouched, and the archive never invents a
market number of its own.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from module4.store import read_json, write_json
from module4.timeutil import format_utc, utc_now

BENCHMARK_FILE: Final[str] = "benchmark.json"


class BenchmarkStore:
    """Reads and writes the benchmark record under one directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def path(self) -> Path:
        return self._root / BENCHMARK_FILE

    def load(self) -> dict[str, Any]:
        payload = read_json(self.path)
        return dict(payload) if isinstance(payload, Mapping) else {}

    def save(self, payload: Mapping[str, Any]) -> None:
        write_json(self.path, dict(payload))


class Benchmark:
    """The index's daily closes, and the sync that keeps them current."""

    def __init__(
        self,
        *,
        store: BenchmarkStore,
        client: Any = None,
        clock: Any = utc_now,
    ) -> None:
        self._store = store
        self._client = client
        self._clock = clock
        self._lock = threading.RLock()
        self._payload: dict[str, Any] = store.load()
        self.synced_at: Any = None
        self.syncs = 0
        self.failures = 0
        self.last_error: str | None = None

    def sync(self) -> dict[str, Any]:
        """Ask Module 1 for the benchmark's closes now."""

        client = self._client
        if client is None:
            return {"skipped": "no benchmark index is configured"}
        try:
            report = client.benchmark()
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return {"error": self.last_error, "candles": 0}
        if not isinstance(report, Mapping):
            self.failures += 1
            self.last_error = "the benchmark route returned no object"
            return {"error": self.last_error, "candles": 0}
        rows = _rows(report.get("candles"))
        if not rows:
            self.failures += 1
            self.last_error = "the benchmark route returned no candles"
            return {"error": self.last_error, "candles": 0}
        payload = {
            "symbol": str(report.get("symbol") or "NIFTY 50"),
            "interval": str(report.get("interval") or "1d"),
            "source": str(report.get("source") or "Module 1"),
            "fetched_at": report.get("fetched_at"),
            "synced_at": format_utc(self._clock()),
            "candles": rows,
        }
        with self._lock:
            self._payload = payload
            self._store.save(payload)
            self.synced_at = self._clock()
            self.syncs += 1
            self.last_error = None
        return {
            "symbol": payload["symbol"],
            "candles": len(rows),
            "fetched_at": payload["fetched_at"],
        }

    def report(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._payload)
            payload["candles"] = list(payload.get("candles") or [])
            payload["updated_at"] = payload.get("synced_at")
        return payload

    def status(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._payload)
        return {
            "symbol": payload.get("symbol"),
            "candles": len(payload.get("candles") or []),
            "updated_at": payload.get("synced_at"),
            "syncs": self.syncs,
            "failures": self.failures,
            "last_error": self.last_error,
            "file": str(self._store.path),
        }


def _rows(found: Any) -> list[dict[str, Any]]:
    """Normalise a benchmark's candles to dated closes, oldest first."""

    if not isinstance(found, Sequence) or isinstance(found, (str, bytes)):
        return []
    closes: dict[str, float] = {}
    for row in found:
        if not isinstance(row, Mapping):
            continue
        day = str(row.get("date") or "").strip()
        close = row.get("close")
        if not day:
            continue
        try:
            closes[day] = float(close)
        except (TypeError, ValueError):
            continue
    return [{"date": day, "close": closes[day]} for day in sorted(closes)]
