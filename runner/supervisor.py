"""The child processes, the health probes and the day's two shutdowns.

Everything here is deliberately testable without starting a service: the
process bookkeeping is a thin dictionary around :class:`subprocess.Popen`, and
the decisions - is Module 1 stable yet, which shutdown is due - are pure
functions over the status payloads and the clock.
"""

from __future__ import annotations

import logging
import math
import os
import signal
import subprocess  # noqa: S404 - supervising the services is the whole job
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from common.window import IST, TradingWindow
from runner.config import DEFAULT_INDICATOR_READY_FRACTION

logger = logging.getLogger("runner.supervisor")

#: The order services are started in, and the reverse order they stop in.
SERVICES: tuple[str, ...] = ("module1", "module2", "module3", "module4")


@dataclass(slots=True)
class Child:
    """One supervised service process and the file its output is appended to."""

    name: str
    process: subprocess.Popen[bytes]
    stream: Any = None
    started_at: float = 0.0
    #: The wall clock the child was started at. The monotonic ``started_at`` is
    #: for arithmetic; this is for the operator, who reads a timestamp.
    started_wall: str = ""


class Supervisor:
    """Starts, stops and reaps the framework's four services."""

    def __init__(
        self,
        *,
        root: Path,
        python: str,
        log_dir: Path,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._root = Path(root)
        self._python = python
        self._log_dir = Path(log_dir)
        self._environment = dict(environment or os.environ)
        self._children: dict[str, Child] = {}
        #: How many times each service has been started at all, across stops
        #: and crashes. The count outlives one child record, so "started 3
        #: times" survives the restart that caused it.
        self._starts: dict[str, int] = {}
        #: The main loop reaps and starts; the Control tab stops and starts.
        #: One lock keeps the two from racing over the same child.
        self._lock = threading.RLock()

    # -- lifecycle ------------------------------------------------------------ #

    def start(self, name: str, *, argv: Sequence[str] | None = None) -> dict[str, Any]:
        """Start one service, unless it is already running.

        ``argv`` overrides the default ``python -m <name>``; it exists for the
        supervisor's own tests and for a service that will one day need an
        argument.
        """

        with self._lock:
            held = self._children.get(name)
            if held is not None and held.process.poll() is None:
                return {
                    "name": name,
                    "pid": held.process.pid,
                    "started": False,
                    "restarts": max(0, self._starts.get(name, 1) - 1),
                }
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stream = (self._log_dir / f"{name}.process.log").open("ab")
            command = list(argv) if argv is not None else [self._python, "-m", name]
            # On POSIX the child gets its own process group, so stopping a
            # service stops everything it started (Module 2's agent processes,
            # for one) and a runner restart cannot take the services down with
            # it.
            process = subprocess.Popen(  # noqa: S603 - a fixed argv, no shell
                command,
                cwd=str(self._root),
                env=self._environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
            self._starts[name] = self._starts.get(name, 0) + 1
            restarts = self._starts[name] - 1
            self._children[name] = Child(
                name=name,
                process=process,
                stream=stream,
                started_at=time.monotonic(),
                started_wall=datetime.now(IST).isoformat(),
            )
            logger.info("%s started (pid %s)", name, process.pid)
            return {
                "name": name,
                "pid": process.pid,
                "started": True,
                "restarts": restarts,
            }

    def stop(self, name: str, *, timeout: float = 8.0) -> bool:
        """Stop one service and everything it started."""

        with self._lock:
            held = self._children.pop(name, None)
            if held is None:
                return False
            process = held.process
            if process.poll() is None:
                self._signal_tree(process, force=False)
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.warning("%s did not stop in %ss; killing it", name, timeout)
                    self._signal_tree(process, force=True)
                    process.wait(timeout=5)
            self._close(held)
            logger.info("%s stopped", name)
            return True

    def restart(self, name: str, *, timeout: float = 8.0) -> dict[str, Any]:
        """Stop one service and start it again, keeping its restart count.

        This is the operator's button and the morning's fresh start: Module 2's
        agents finish for the day on their own clock, so the only way to have a
        living fleet the next morning is to start the control service anew.
        """

        with self._lock:
            self.stop(name, timeout=timeout)
            return self.start(name)

    def stop_all(self, *, order: Sequence[str] | None = None) -> list[str]:
        with self._lock:
            stopped: list[str] = []
            for name in order or tuple(reversed(SERVICES)):
                if self.stop(name):
                    stopped.append(name)
            return stopped

    def reap(self) -> list[tuple[str, int]]:
        """Names whose process has exited since the last check, with the code."""

        with self._lock:
            gone: list[tuple[str, int]] = []
            for name, held in list(self._children.items()):
                code = held.process.poll()
                if code is None:
                    continue
                gone.append((name, int(code)))
                self._children.pop(name, None)
                self._close(held)
            return gone

    def running(self) -> dict[str, int]:
        with self._lock:
            return {
                name: held.process.pid
                for name, held in self._children.items()
                if held.process.poll() is None
            }

    def is_running(self, name: str) -> bool:
        with self._lock:
            held = self._children.get(name)
            return held is not None and held.process.poll() is None

    def restarts(self, name: str) -> int:
        with self._lock:
            return max(0, self._starts.get(name, 1) - 1)

    def describe(self, name: str) -> dict[str, Any]:
        """One service's bookkeeping, for the control surface.

        ``restarts`` survives a stop, so a service that failed five times this
        morning still says so after the operator stops and starts it once more.
        """

        with self._lock:
            held = self._children.get(name)
            running = held is not None and held.process.poll() is None
            if held is None:
                return {
                    "name": name,
                    "running": False,
                    "pid": None,
                    "restarts": max(0, self._starts.get(name, 1) - 1),
                    "started_at": None,
                    "uptime_seconds": None,
                }
            uptime = max(0.0, time.monotonic() - held.started_at) if running else None
            return {
                "name": name,
                "running": running,
                "pid": held.process.pid if running else None,
                "restarts": max(0, self._starts.get(name, 1) - 1),
                "started_at": held.started_wall or None,
                "uptime_seconds": uptime,
            }

    def _close(self, child: Child) -> None:
        stream = child.stream
        if stream is not None:
            with suppress(OSError):
                stream.close()

    @staticmethod
    def _signal_tree(process: subprocess.Popen[bytes], *, force: bool) -> None:
        """Stop a service *and its children*, on whichever platform we are.

        A service is not always one process. On this machine the interpreter is
        reached through a launcher, so ``python -m module1`` is two processes;
        and Module 2 deliberately starts one process per agent. Terminating the
        parent alone would leave a child holding the port or the position book.
        """

        if os.name == "nt":
            # ``taskkill /T`` without ``/F`` only posts a window message, which
            # a console service never reads - waiting for it just costs the
            # shutdown eight seconds. The tree is killed outright instead:
            # every module writes its state synchronously and atomically, so
            # there is nothing buffered to lose.
            with suppress(OSError):
                subprocess.run(  # noqa: S603 - a fixed argv, no shell
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            return
        try:
            group = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill() if force else process.terminate()
            return
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(group, signal.SIGKILL if force else signal.SIGTERM)


def probe(url: str, *, client: httpx.Client | None = None, timeout: float = 3.0) -> tuple[bool, str]:
    """Ask one service for its health. ``(ok, detail)`` and never an exception."""

    owns = client is None
    holder = client or httpx.Client(timeout=timeout)
    try:
        response = holder.get(f"{url.rstrip('/')}/health")
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
    finally:
        if owns:
            holder.close()
    if response.status_code >= 400:
        return False, f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return False, "non-JSON answer"
    if isinstance(payload, Mapping) and payload.get("ok") is False:
        return False, str(payload.get("detail") or "not ready")
    return True, "ok"


def stabilization_reason(
    status: Mapping[str, Any],
    *,
    indicator_fraction: float = DEFAULT_INDICATOR_READY_FRACTION,
) -> str | None:
    """Why Module 1 is not ready to be depended on, or ``None`` when it is.

    "Ready" is deliberately strict, because the price of being wrong is agents
    deciding on a half-built book: the history has to be there, the symbols
    resolved, the websocket connected when it is enabled, and the warm-up
    finished (its report is only written once the pass returns). And the
    indicators have to be computed: the rest of the framework is started
    *after* Module 1 has turned the fetched data into values, not while it is
    still working through the universe.

    The payload is Module 1's own status, where ``board`` is the board worker's
    whole report - its inner ``board``, its ``feed`` and its token count. The
    nesting is part of the contract; flattening it here would make a busy
    service look like a broken one.
    """

    worker = status.get("board") or {}
    board = worker.get("board") or {}
    feed = worker.get("feed")
    tokens = worker.get("tokens")
    settings = status.get("settings") or {}
    if settings.get("feed_enabled") and not tokens:
        return "no instrument tokens resolved yet"
    if not board.get("resolved"):
        return "no symbols resolved yet"
    if not board.get("history_ready"):
        return "history is still loading"
    if settings.get("feed_enabled"):
        if not feed:
            return "the price feed is not attached"
        if not feed.get("connected"):
            return f"the price feed is {feed.get('state') or 'not connected'}"
    warm = status.get("warm")
    if warm is None:
        return "the warm-up pass has not finished"
    if isinstance(warm, Mapping) and warm.get("error"):
        return f"the warm-up pass failed ({warm['error']})"
    indicators = (status.get("candles") or {}).get("indicators")
    if not isinstance(indicators, Mapping) or not indicators:
        return "the indicators have not been computed yet"
    symbols = int(indicators.get("symbols") or 0)
    done = int(indicators.get("symbols_done") or 0)
    if done < required_indicator_symbols(symbols, fraction=indicator_fraction):
        return f"indicators are computed for {done} of {symbols} symbols"
    return None


def required_indicator_symbols(
    symbols: int, *, fraction: float = DEFAULT_INDICATOR_READY_FRACTION
) -> int:
    """How many symbols must carry computed indicators before the rest starts.

    A fraction, because one symbol whose history did not come back must not
    hold the whole morning hostage; a minimum of one, because an empty book has
    computed nothing and is not "nearly ready".
    """

    bounded = min(1.0, max(0.0, float(fraction)))
    return max(1, math.ceil(max(0, int(symbols)) * bounded))


def module2_stop_at(moment: datetime, window: TradingWindow, grace_seconds: float) -> datetime:
    """When the agent fleet is taken down: after the last cycle has finished."""

    return window.last_decision_moment(moment) + timedelta(seconds=max(0.0, grace_seconds))


def framework_stop_at(moment: datetime, window: TradingWindow, grace_seconds: float) -> datetime:
    """When the rest of the framework is taken down: after the close settles."""

    return window.closes_at(moment) + timedelta(seconds=max(0.0, grace_seconds))


def wait_until_ready(
    fetch_status: Callable[[], Mapping[str, Any]],
    *,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None] = time.sleep,
    minimum_seconds: float,
    timeout_seconds: float,
    poll_seconds: float = 5.0,
    indicator_fraction: float = DEFAULT_INDICATOR_READY_FRACTION,
    should_stop: Callable[[], bool] | None = None,
    report: Callable[[str], None] | None = None,
) -> tuple[bool, str | None, float]:
    """Wait for Module 1 to stabilise. ``(ready, why not, waited)``.

    The minimum is the operator's "give it a minute": a service that answers in
    five seconds has not proved anything about the next five minutes, and the
    whole point of the wait is to start Module 2 against a book that is already
    warm rather than one that is about to be.

    ``should_stop`` is asked before every poll, so an operator who changes
    their mind during the minute does not have to wait out the timeout.
    """

    started = time.monotonic()
    minimum = max(0.0, minimum_seconds)
    deadline = started + max(minimum, timeout_seconds)
    reason: str | None = "the status route has not answered yet"
    announced: str | None = None
    while True:
        if should_stop is not None and should_stop():
            return False, "stopped on request", time.monotonic() - started
        elapsed = time.monotonic() - started
        try:
            status = fetch_status()
        except Exception as exc:  # noqa: BLE001 - a missing service is not ready
            reason = f"{type(exc).__name__}: {exc}"[:120]
        else:
            reason = stabilization_reason(status, indicator_fraction=indicator_fraction)
            if reason is None and elapsed >= minimum:
                return True, None, elapsed
        if report is not None and reason != announced:
            # A runner that is waiting silently is indistinguishable from one
            # that is stuck; say what it is waiting for, once per change.
            announced = reason
            report(reason or "waiting for the minimum stabilisation time")
        if time.monotonic() >= deadline:
            return False, reason, time.monotonic() - started
        sleeper(min(poll_seconds, max(0.5, deadline - time.monotonic())))
