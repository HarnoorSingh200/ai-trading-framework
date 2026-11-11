"""Module 4's local archive and its S3 mirror."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from module4.ledger import Ledger
from module4.s3 import S3Archive
from module4.store import HistoryStore

pytestmark = pytest.mark.unit


def an_order(order_id: str, *, status: str = "open") -> dict[str, Any]:
    return {
        "order_id": order_id,
        "agent_id": "alpha",
        "symbol": "TCS",
        "status": status,
        "session_date": "2026-09-29",
        "received_at": "2026-09-29T04:30:00.000Z",
        "pnl": None,
    }


class FakeS3:
    """The two S3 calls the archive makes, without a network."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: Any) -> None:
        self.objects[Key] = Body
        self.puts.append(Key)


class FakeRemote:
    """A remote archive with the same small interface as S3Archive."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}
        self.saved = 0

    def load(self) -> dict[str, Any] | None:
        return self.payload or None

    def save(self, orders: Any) -> None:
        self.saved += 1
        self.payload = {"orders": list(orders.values())}

    def describe(self) -> dict[str, Any]:
        return {"bucket": "test", "key": "stock market v3/history.json"}


class FakeBook:
    def days(self) -> list[str]:
        return ["2026-09-29"]

    def for_day(self, day: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        return [an_order("ord_1")]


def test_the_archive_uses_only_the_configured_s3_prefix() -> None:
    client = FakeS3()
    archive = S3Archive(
        "your-archive-bucket",
        prefix="stock market v3",
        region="ap-southeast-1",
        client=client,
    )

    archive.save({"ord_1": an_order("ord_1")})

    assert archive.key == "stock market v3/history.json"
    assert client.puts == ["stock market v3/history.json"]


def test_the_s3_archive_round_trips() -> None:
    client = FakeS3()
    archive = S3Archive("your-archive-bucket", client=client)
    archive.save({"ord_1": an_order("ord_1")})

    loaded = archive.load()

    assert loaded is not None
    assert loaded["count"] == 1
    assert loaded["orders"][0]["order_id"] == "ord_1"
    assert json.loads(client.objects[archive.key])["count"] == 1


def test_a_missing_s3_archive_is_empty_not_an_error() -> None:
    archive = S3Archive("your-archive-bucket", client=FakeS3())

    assert archive.load() is None


def test_startup_merges_the_remote_archive_into_the_local_one(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.save({"ord_1": an_order("ord_1")})
    remote = FakeRemote(
        {
            "orders": [
                an_order("ord_1", status="closed"),
                an_order("ord_2"),
            ]
        }
    )
    ledger = Ledger(store=store, remote=remote)

    report = ledger.load_remote()

    assert report["loaded"] is True
    assert report["loaded_count"] == 2
    assert {row["order_id"] for row in ledger.orders()} == {"ord_1", "ord_2"}
    merged = {row["order_id"]: row for row in ledger.orders()}
    assert merged["ord_1"]["status"] == "closed"
    assert store.load()["ord_2"]["order_id"] == "ord_2"


def test_a_local_archive_is_uploaded_after_a_sync(tmp_path: Path) -> None:
    remote = FakeRemote()
    ledger = Ledger(store=HistoryStore(tmp_path), book=FakeBook(), remote=remote)

    report = ledger.sync()

    assert report["added"] == 1
    assert report["s3"]["uploaded"] is True
    assert remote.saved == 1
    assert remote.payload["orders"][0]["order_id"] == "ord_1"


def test_a_remote_failure_does_not_lose_the_local_sync(tmp_path: Path) -> None:
    class BrokenRemote(FakeRemote):
        def load(self) -> dict[str, Any] | None:
            raise RuntimeError("S3 is down")

        def save(self, orders: Any) -> None:
            raise RuntimeError("S3 is still down")

    ledger = Ledger(store=HistoryStore(tmp_path), book=FakeBook(), remote=BrokenRemote())

    report = ledger.sync()

    assert report["added"] == 1
    assert "S3 is still down" in report["s3"]["error"]
    assert ledger.orders()[0]["order_id"] == "ord_1"
