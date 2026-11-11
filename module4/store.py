"""The archive: every order this module has ever seen.

One file, rewritten atomically. Unlike the other modules' ledgers there is no
second append-only log beside it, and that is deliberate: this module does not
*decide* anything, it copies what Module 3 decided, and Module 3's own log is
already the truth. This is a reading of that truth, kept long after the day it
was written on is over.

An order is stored exactly as Module 3 described it, one description of an
order living in one module. If Module 3 learns a new field, this carries it
without a change here - which is the whole reason not to re-cut the shape.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from module4.timeutil import format_utc, utc_now

HISTORY_FILE: Final[str] = "history.json"

#: The fields that move while an order is open. They describe the moment, not
#: what happened: keeping them would rewrite the archive on every sync and make
#: "has this order changed?" unanswerable, because a price always has.
LIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "last_price",
        "last_price_at",
        "last_price_source",
        "price_age_seconds",
        "unrealised_pnl",
    }
)


def durable(row: Mapping[str, Any]) -> dict[str, Any]:
    """The part of an order that is a record rather than a reading."""

    return {key: value for key, value in row.items() if key not in LIVE_FIELDS}


def orders_from_payload(payload: Any) -> dict[str, dict[str, Any]]:
    """Read an archive payload into the order-id map used everywhere here."""

    if not isinstance(payload, Mapping):
        return {}
    rows = payload.get("orders")
    if not isinstance(rows, list):
        return {}
    kept: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping) and str(row.get("order_id") or "").strip():
            kept[str(row["order_id"])] = dict(row)
    return kept


def archive_payload(orders: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The one representation written both locally and to S3."""

    return {
        "written_at": format_utc(utc_now()),
        "count": len(orders),
        "orders": list(orders.values()),
    }


def merge_orders(
    local: Mapping[str, Mapping[str, Any]],
    remote: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Union two archives without losing a locally advanced order.

    Module 4 never deletes an archived order. A local row is therefore either
    the same record, a newer one (for example it has moved from open to
    closed), or a row S3 has not seen yet. Prefer the more settled record when
    both copies exist, and otherwise prefer the local copy.
    """

    merged = {order_id: dict(row) for order_id, row in remote.items()}
    for order_id, row in local.items():
        held = merged.get(order_id)
        if held is None or _record_rank(row) >= _record_rank(held):
            merged[order_id] = dict(row)
    return merged


def _record_rank(row: Mapping[str, Any]) -> tuple[int, str]:
    """How settled a record is, for merging two copies of one order."""

    status = str(row.get("status") or "")
    settled = 1 if status in {"closed", "expired"} else 0
    ended = str(row.get("ended_at") or row.get("filled_at") or row.get("received_at") or "")
    return settled, ended


class HistoryStore:
    """Reads and writes the long archive under one directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def history_path(self) -> Path:
        return self._root / HISTORY_FILE

    def load(self) -> dict[str, dict[str, Any]]:
        """Every order in the archive, keyed by its id."""

        return orders_from_payload(read_json(self.history_path))

    def save(self, orders: Mapping[str, Mapping[str, Any]]) -> None:
        """Write the archive atomically: a reader sees the old one or the new."""

        write_json(self.history_path, archive_payload(orders))

    def meta(self) -> dict[str, Any]:
        """What the last write said, without reading the whole archive."""

        payload = read_json(self.history_path)
        if payload is None:
            return {"present": False, "written_at": None, "count": 0}
        return {
            "present": True,
            "written_at": payload.get("written_at"),
            "count": int(payload.get("count") or 0),
        }

    def days(self) -> Sequence[str]:
        """The exchange days the archive holds anything for."""

        return sorted(
            {
                str(row["session_date"])
                for row in self.load().values()
                if row.get("session_date")
            }
        )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON file atomically, so a reader never sees half of one."""

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


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
