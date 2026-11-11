"""Every order the framework has taken, and what it came to.

Module 3's book is per day, and a day is the right size for running one. This
is the long one, for the questions a day cannot answer: is this agent any good
over a month, does it do better on some days than others, what does its average
trade actually make.

The arithmetic lives in one place - :func:`totals` - so the dashboard, the
per-agent table and the per-day table cannot disagree about what "realised"
means.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from module4.analytics import build_analytics, totals
from module4.store import HistoryStore, durable, merge_orders, orders_from_payload
from module4.timeutil import format_utc, utc_now

#: The newest days are still being written to, so they are read again on every
#: sync. A day older than these is finished: every order in it is closed by the
#: time the session ends, so reading it once is reading it for good.
DAYS_STILL_OPEN = 2


class Ledger:
    """The archive, and the answers drawn from it."""

    def __init__(
        self,
        *,
        store: HistoryStore,
        book: Any = None,
        remote: Any = None,
        clock: Any = utc_now,
    ) -> None:
        self._store = store
        self._book = book
        self._remote = remote
        self._clock = clock
        self._lock = threading.RLock()
        self._orders: dict[str, dict[str, Any]] = store.load()
        self.synced_at: Any = None
        self.syncs = 0
        self.failures = 0
        self.last_error: str | None = None
        self.added = 0
        self.updated = 0
        self._remote_loaded = False
        self._remote_loaded_count = 0
        self._remote_dirty = False
        self._remote_error: str | None = None
        self._remote_synced_at: Any = None

    # -- bringing the archive up to date ------------------------------------- #

    def sync(self, *, force_remote: bool = False) -> dict[str, Any]:
        """Read Module 3's book and keep whatever is new.

        Idempotent, so running it twice costs a request and changes nothing.
        """

        self._ensure_remote_loaded()
        book = self._book
        if book is None:
            return {
                "skipped": "no order book is configured",
                "s3": self.publish_remote(force=force_remote),
            }
        self.syncs += 1
        days: list[str] = []
        try:
            days = book.days()
            rows: list[dict[str, Any]] = []
            for day in self._wanted(days):
                rows.extend(book.for_day(day))
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return {"error": self.last_error, "days": len(days)}
        self.last_error = None
        report = self.ingest(rows)
        report["days"] = len(days)
        report["s3"] = self.publish_remote(force=force_remote)
        self.synced_at = self._clock()
        return report

    def _wanted(self, days: Sequence[str]) -> list[str]:
        """Which days are worth asking for.

        A finished day never changes, so reading it once is reading it for good.
        The newest couple are still being written to and are read again; and any
        day the archive has never seen is read whatever its age, which is what
        makes a dashboard that was switched off for a week catch up instead of
        quietly missing the days in between.
        """

        if not days:
            return []
        with self._lock:
            archived = {
                str(row["session_date"])
                for row in self._orders.values()
                if row.get("session_date")
            }
        newest = set(days[:DAYS_STILL_OPEN])
        return sorted({*newest, *(day for day in days if day not in archived)})

    def ingest(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Keep every order that is new, and every one that has changed."""

        added = updated = 0
        with self._lock:
            for row in rows:
                order_id = str(row.get("order_id") or "").strip()
                if not order_id:
                    continue
                record = durable(row)
                held = self._orders.get(order_id)
                if held is None:
                    added += 1
                elif held != record:
                    updated += 1
                else:
                    continue
                self._orders[order_id] = record
            if added or updated:
                self._store.save(self._orders)
                self._remote_dirty = True
            self.added += added
            self.updated += updated
        return {"seen": len(rows), "added": added, "updated": updated}

    # -- the durable mirror -------------------------------------------------- #

    def load_remote(self) -> dict[str, Any]:
        """Load the durable archive once, before the first sync."""

        return self._ensure_remote_loaded()

    def _ensure_remote_loaded(self) -> dict[str, Any]:
        """Merge the S3 archive into the local one once per process."""

        remote = self._remote
        if remote is None:
            return {"configured": False}
        if self._remote_loaded:
            return self.remote_status()
        self._remote_loaded = True
        try:
            payload = remote.load()
        except Exception as exc:  # noqa: BLE001 - local archive remains usable
            self._remote_error = f"{type(exc).__name__}: {exc}"[:200]
            return self.remote_status()
        if not isinstance(payload, Mapping):
            self._remote_dirty = bool(self._orders)
            return self.remote_status()
        remote_orders = orders_from_payload(payload)
        with self._lock:
            merged = merge_orders(self._orders, remote_orders)
            if merged != self._orders:
                self._orders = merged
                self._store.save(self._orders)
                self._remote_dirty = True
            self._remote_loaded_count = len(remote_orders)
        return self.remote_status()

    def publish_remote(self, *, force: bool = False) -> dict[str, Any]:
        """Write the local archive to S3 when it has changed."""

        remote = self._remote
        if remote is None:
            return {"configured": False, "uploaded": False}
        if not force and not self._remote_dirty:
            return {
                **self.remote_status(),
                "uploaded": False,
                "reason": "unchanged",
            }
        try:
            remote.save(self._orders)
        except Exception as exc:  # noqa: BLE001 - the local archive remains current
            self._remote_error = f"{type(exc).__name__}: {exc}"[:200]
            return {**self.remote_status(), "uploaded": False, "error": self._remote_error}
        self._remote_dirty = False
        self._remote_error = None
        self._remote_synced_at = self._clock()
        return {**self.remote_status(), "uploaded": True}

    def remote_status(self) -> dict[str, Any]:
        """Safe status for the dashboard: no credentials, only the location."""

        remote = self._remote
        if remote is None:
            return {"configured": False}
        described = remote.describe() if hasattr(remote, "describe") else {}
        return {
            "configured": True,
            **described,
            "loaded": self._remote_loaded,
            "loaded_count": self._remote_loaded_count,
            "pending": self._remote_dirty,
            "synced_at": None
            if self._remote_synced_at is None
            else format_utc(self._remote_synced_at),
            "last_error": self._remote_error,
        }

    # -- reading it ----------------------------------------------------------- #

    def orders(
        self,
        *,
        agent_id: str | None = None,
        day: str | None = None,
        status: str | None = None,
        symbol: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._orders.values())
        if agent_id:
            rows = [row for row in rows if row.get("agent_id") == agent_id]
        if day:
            rows = [row for row in rows if row.get("session_date") == day]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if symbol:
            rows = [row for row in rows if row.get("symbol") == symbol.strip().upper()]
        rows.sort(
            key=lambda row: (str(row.get("received_at") or ""), str(row.get("order_id"))),
            reverse=True,
        )
        return rows[: max(1, min(int(limit), 2000))]

    def days(self, *, agent_id: str | None = None) -> list[dict[str, Any]]:
        """One row per exchange day, newest first."""

        with self._lock:
            rows = list(self._orders.values())
        if agent_id:
            rows = [row for row in rows if row.get("agent_id") == agent_id]
        found = sorted(
            {str(row["session_date"]) for row in rows if row.get("session_date")},
            reverse=True,
        )
        return [
            {"day": day, **totals([row for row in rows if row.get("session_date") == day])}
            for day in found
        ]

    def agents(self) -> list[dict[str, Any]]:
        """One row per agent, best first by what it has actually made."""

        with self._lock:
            rows = list(self._orders.values())
        found = sorted({str(row.get("agent_id")) for row in rows if row.get("agent_id")})
        table = [
            {
                "agent_id": agent_id,
                **totals([row for row in rows if row.get("agent_id") == agent_id]),
            }
            for agent_id in found
        ]
        table.sort(key=lambda row: (-(row["realised"] or 0.0), row["agent_id"]))
        return table

    def performance(
        self, *, agent_id: str | None = None, day: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            rows = list(self._orders.values())
        if agent_id:
            rows = [row for row in rows if row.get("agent_id") == agent_id]
        if day:
            rows = [row for row in rows if row.get("session_date") == day]
        return {**totals(rows), "agent_id": agent_id, "day": day}

    def analytics(
        self, *, agent_id: str | None = None, day: str | None = None
    ) -> dict[str, Any]:
        """The visual dashboard's one answer, over the whole archive."""

        with self._lock:
            rows = list(self._orders.values())
        return build_analytics(rows, agent_id=agent_id, day=day)

    def status(self) -> dict[str, Any]:
        with self._lock:
            rows = list(self._orders.values())
        days = sorted(
            {str(row["session_date"]) for row in rows if row.get("session_date")}
        )
        return {
            "orders": len(rows),
            "agents": sorted({str(row.get("agent_id")) for row in rows if row.get("agent_id")}),
            "days": len(days),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "syncs": self.syncs,
            "failures": self.failures,
            "last_error": self.last_error,
            "added": self.added,
            "updated": self.updated,
            "synced_at": None if self.synced_at is None else format_utc(self.synced_at),
            "archive": self._store.meta(),
            "s3": self.remote_status(),
        }
