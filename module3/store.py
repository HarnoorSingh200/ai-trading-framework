"""The order ledger: one checkpoint, and the history that can rebuild it.

Two files, because they answer two different questions:

* ``orders.json`` - what every order *is* right now, rewritten atomically. A
  reader either sees the previous version or the new one, never half of either.
* ``events.jsonl`` - what happened, one JSON object per line, appended and never
  rewritten. Every event carries the whole order as it stood after the change,
  so the log is readable on its own and the checkpoint can be rebuilt from it.

The checkpoint is the fast path and the event log is the truth. If the
checkpoint is missing or unreadable, :meth:`OrderStore.load` replays the log and
reconstructs it, so a process killed between the two writes does not lose a
trade. A torn final line is skipped rather than raised - a log is always caught
mid-write by someone.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from module3.orders import Order
from module3.timeutil import format_utc, utc_now

ORDERS_FILE: Final[str] = "orders.json"
EVENTS_FILE: Final[str] = "events.jsonl"


class OrderStore:
    """Reads and writes Module 3's ledger under one directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def orders_path(self) -> Path:
        return self._root / ORDERS_FILE

    @property
    def events_path(self) -> Path:
        return self._root / EVENTS_FILE

    # -- reading ------------------------------------------------------------- #

    def load(self) -> dict[str, Order]:
        """Every order, from the checkpoint, or rebuilt from the event log."""

        payload = _read_json(self.orders_path)
        if payload is not None:
            orders: dict[str, Order] = {}
            for item in payload.get("orders") or []:
                order = _order_or_none(item)
                if order is not None:
                    orders[order.order_id] = order
            return orders
        return self.replay()

    def replay(self) -> dict[str, Order]:
        """Rebuild the ledger from the event log alone.

        Later events win, which is exactly right: the log is in order, and each
        event is the whole order as it stood after that change.
        """

        orders: dict[str, Order] = {}
        for event in self.read_events(limit=None):
            order = _order_or_none(event.get("order"))
            if order is not None:
                orders[order.order_id] = order
        return orders

    def read_events(self, *, limit: int | None = 200) -> list[dict[str, Any]]:
        """The last ``limit`` whole events, oldest first. ``None`` reads all."""

        if not self.events_path.exists():
            return []
        try:
            text = self.events_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            body = line.strip()
            if not body:
                continue
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                # A line being written right now, or one that was interrupted.
                continue
            if isinstance(payload, dict):
                events.append(payload)
        if limit is None:
            return events
        return events[-max(1, int(limit)) :]

    # -- writing ------------------------------------------------------------- #

    def write_checkpoint(self, orders: Mapping[str, Order]) -> None:
        """Write the current state of every order, atomically."""

        _write_json(
            self.orders_path,
            {
                "written_at": format_utc(utc_now()),
                "count": len(orders),
                "orders": [order.describe() for order in orders.values()],
            },
        )

    def append_event(self, kind: str, order: Order) -> dict[str, Any]:
        """Append one change, stamped with when it happened."""

        event = {
            "ts": format_utc(utc_now()),
            "kind": kind,
            "order_id": order.order_id,
            "symbol": order.symbol,
            "status": order.status,
            "order": order.describe(),
        }
        _append(self.events_path, event)
        return event

    def append_note(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Append one event about the book itself rather than about an order."""

        event = {"ts": format_utc(utc_now()), "kind": kind, **fields}
        _append(self.events_path, event)
        return event

    def rewrite_events(self, forget: set[str], *, note: dict[str, Any]) -> dict[str, int]:
        """Drop the history of orders the book no longer holds.

        The one operation that rewrites an append-only log, and the reason it
        exists is that a ledger which keeps everything is not a book of the day.
        It is written to a temporary file and swapped in, so a reader sees the
        whole old log or the whole new one - never half of either - and the note
        recording what was dropped is appended to the new one, so the file still
        says what happened to it.
        """

        kept: list[dict[str, Any]] = []
        dropped = 0
        for event in self.read_events(limit=None):
            if str(event.get("order_id") or "") in forget:
                dropped += 1
                continue
            kept.append(event)
        kept.append(note)
        body = "".join(json.dumps(event, default=str) + "\n" for event in kept)
        _replace_text(self.events_path, body)
        return {"events_kept": len(kept) - 1, "events_dropped": dropped}


def _order_or_none(payload: Any) -> Order | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        return Order.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        # A record this version cannot read is not worth failing the whole
        # ledger over; the event log still holds it.
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a small JSON file atomically, so a reader never sees half of one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _append(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")
        handle.flush()


def _replace_text(path: Path, body: str) -> None:
    """Replace a file's contents atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise
