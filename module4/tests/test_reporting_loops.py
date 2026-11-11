"""Module 4's background loops: what is an archive fault, and what is not."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from common.telegram import NullNotifier
from module4.service import Module4Runtime
from module4.store import HistoryStore

pytestmark = pytest.mark.unit


class OneShotStop:
    """A stop flag that ends the loop after its first wait."""

    def __init__(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def wait(self, seconds: float | None = None) -> bool:
        self._set = True
        return True

    def set(self) -> None:
        self._set = True


class FakeLedger:
    def __init__(self, report: dict[str, Any] | None = None) -> None:
        self.report = report or {"s3": {}}
        self.calls = 0

    def sync(self) -> dict[str, Any]:
        self.calls += 1
        return self.report


class BrokenUsage:
    def sync(self) -> dict[str, Any]:
        raise RuntimeError("Module 2 is resting")


class WorkingUsage:
    def __init__(self) -> None:
        self.calls = 0

    def sync(self) -> dict[str, Any]:
        self.calls += 1
        return {"days": 0}


def runtime(tmp_path: Path, ledger: Any = None, usage: Any = None) -> Module4Runtime:
    store = HistoryStore(tmp_path)
    return Module4Runtime(
        settings=_settings(tmp_path),
        store=store,
        ledger=ledger or FakeLedger(),
        notifier=NullNotifier(reason="tests"),
        usage=usage,
        _stop=OneShotStop(),
    )


def _settings(tmp_path: Path):
    from module4.config import Settings

    return Settings(
        environment="test",
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        module3_url="http://module3",
        sync_seconds=1.0,
        http_timeout_seconds=5.0,
    )


def test_a_resting_agent_engine_is_not_an_archive_fault(tmp_path: Path) -> None:
    """The Reporting tab must not call a closed market a broken archive."""

    state = runtime(tmp_path, usage=BrokenUsage())

    state._usage_loop()

    assert state.last_error is None
    assert state.ledger.calls == 0


def test_a_working_agent_engine_does_not_clear_the_archive_error_either(
    tmp_path: Path,
) -> None:
    state = runtime(tmp_path, usage=WorkingUsage())

    state._usage_loop()

    assert state.last_error is None


def test_a_successful_archive_sync_clears_an_old_error(tmp_path: Path) -> None:
    state = runtime(tmp_path, ledger=FakeLedger({"s3": {}}))
    state.last_error = "sync: something broke earlier"

    state._sync_loop()

    assert state.last_error is None
    assert state.ledger.calls == 1


def test_a_failed_archive_sync_is_still_reported(tmp_path: Path) -> None:
    class FailingLedger:
        def sync(self) -> dict[str, Any]:
            raise RuntimeError("book is down")

    state = runtime(tmp_path, ledger=FailingLedger())

    state._sync_loop()

    assert "book is down" in (state.last_error or "")
