"""Module 2's control surface: the registry, the process launcher and the feed.

This is a control plane, not a data plane. The operator site defines an agent
here, starts it here and watches it here; the agent itself runs as its own child
process and writes what it does to its own directory. Nothing important lives in
this process's memory, so restarting it neither stops the agents nor loses the
feed: the site reconnects and reads the same files from where it left off.

Endpoints, under ``/v1/agents``: the registry (list, create, get, edit, retire),
``start`` and ``stop``, the two logs, and one server-sent stream per agent plus
one for all of them.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess  # noqa: S404 - launching the agent processes is the point
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from common.telegram import Notifier, NullNotifier, escape, notifier_from_env
from module2 import notes as notebook
from module2.config import PROJECT_ROOT, Settings, load_settings
from module2.definition import AgentDefinition, DefinitionError
from module2.notes import NoteError
from module2.store import AgentStore
from module2.timeutil import utc_now
from module2.usage import usage_report

logger = logging.getLogger("module2.service")

#: How often the feed looks for new lines. Fast enough to feel live, cheap
#: enough that ten watched agents cost nothing.
TAIL_INTERVAL_SECONDS: float = 0.5
STREAM_HEARTBEAT_SECONDS: float = 15.0
#: How much of an existing log a newly connected reader is shown. Enough that
#: the operator sees what an agent was just doing, not so much that opening the
#: tab replays a whole session.
TAIL_BACKLOG_BYTES: int = 64 * 1024

_CENT: Decimal = Decimal("0.01")


class Supervisor:
    """One child process per running agent."""

    def __init__(self, *, project_root: Path | None = None, python: str | None = None) -> None:
        self._root = Path(project_root or PROJECT_ROOT)
        self._python = python or sys.executable
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()

    def start(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            self._reap()
            held = self._processes.get(agent_id)
            if held is not None and held.poll() is None:
                return {"agent_id": agent_id, "pid": held.pid, "running": True, "started": False}
            process = subprocess.Popen(  # noqa: S603 - a fixed argv, no shell
                [self._python, "-m", "module2", "--agent", agent_id],
                cwd=str(self._root),
                start_new_session=os.name != "nt",
            )
            self._processes[agent_id] = process
            return {"agent_id": agent_id, "pid": process.pid, "running": True, "started": True}

    def stop(self, agent_id: str) -> bool:
        with self._lock:
            process = self._processes.pop(agent_id, None)
            if process is None or process.poll() is not None:
                return False
            _stop_tree(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - a stuck child
                process.kill()
            return True

    def status(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            self._reap()
            process = self._processes.get(agent_id)
            running = process is not None and process.poll() is None
            return {
                "running": running,
                "pid": None if not running or process is None else process.pid,
            }

    def running(self) -> dict[str, int]:
        with self._lock:
            self._reap()
            return {
                agent_id: process.pid
                for agent_id, process in self._processes.items()
                if process.poll() is None
            }

    def shutdown(self) -> None:
        """Stop every child this process started.

        A worker whose supervisor died keeps running until its next candle: it
        is a loop over a log file, and it is safe to leave one behind.
        """

        for agent_id in list(self._processes):
            self.stop(agent_id)

    def _reap(self) -> None:
        for agent_id, process in list(self._processes.items()):
            if process.poll() is not None:
                self._processes.pop(agent_id, None)


def _stop_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop an agent *and its interpreter*, however it was launched.

    On a uv-managed Windows environment ``python`` is a launcher that spawns
    the real interpreter, so terminating the process we hold would leave the
    one doing the work behind. Module 2 learned this the same way the runner
    did: by watching a stopped agent keep a port.
    """

    if os.name == "nt":
        with suppress(OSError):
            subprocess.run(  # noqa: S603 - a fixed argv, no shell
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        return
    process.terminate()


# -- the live feed ----------------------------------------------------------- #


@dataclass
class _Tail:
    """One log file, and how far it has been read."""

    path: Path
    kind: str
    agent_id: str
    offset: int = 0
    buffer: str = ""
    seen: bool = False


def _tail_once(tail: _Tail) -> list[str]:
    """New whole lines since last time. A half-written line waits for its end."""

    if not tail.path.exists():
        return []
    try:
        size = tail.path.stat().st_size
        if not tail.seen:
            # First look at an existing log: start near the end rather than at
            # the beginning, or connecting would replay the agent's whole life.
            tail.seen = True
            tail.offset = max(0, size - TAIL_BACKLOG_BYTES)
        if size < tail.offset:
            # The file was replaced or truncated under us; start again.
            tail.offset, tail.buffer = 0, ""
        with tail.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(tail.offset)
            chunk = handle.read()
            tail.offset = handle.tell()
    except OSError:
        return []
    if not chunk:
        return []
    tail.buffer += chunk
    lines = tail.buffer.split("\n")
    tail.buffer = lines.pop()
    return [line for line in lines if line.strip()]


def tail_events(
    store: AgentStore,
    agent_ids: Sequence[str],
    *,
    interval: float = TAIL_INTERVAL_SECONDS,
    stop: threading.Event | None = None,
) -> Iterator[str]:
    """Yield server-sent events for the agents' logs as they are written."""

    tails: list[_Tail] = []
    for agent_id in agent_ids:
        for kind, path in (
            ("activity", store.activity_path(agent_id)),
            ("decision", store.decisions_path(agent_id)),
        ):
            # An agent started before the site connected must not be invisible:
            # the existing tail is sent once, then only what is new.
            tails.append(_Tail(path=path, kind=kind, agent_id=agent_id))
    last_sent = time.monotonic()
    while stop is None or not stop.is_set():
        wrote = False
        for tail in tails:
            for line in _tail_once(tail):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                wrote = True
                last_sent = time.monotonic()
                yield (
                    f"event: {tail.kind}\n"
                    f"data: {json.dumps({'agent_id': tail.agent_id, **payload}, default=str)}\n\n"
                )
        now = time.monotonic()
        if not wrote and now - last_sent >= STREAM_HEARTBEAT_SECONDS:
            last_sent = now
            yield ": keepalive\n\n"
        time.sleep(interval)


# -- the app ----------------------------------------------------------------- #


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def fail(
    code: str, message: str, *, status_code: int = 400, details: dict[str, Any] | None = None
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(status_code=status_code, detail={"ok": False, "error": error})


@dataclass
class ControlState:
    """What the control service holds: a store, a supervisor and its settings."""

    settings: Settings
    store: AgentStore
    supervisor: Any = field(default_factory=Supervisor)
    notifier: Notifier = field(default_factory=NullNotifier)
    _closer_stop: threading.Event = field(
        default_factory=threading.Event, repr=False
    )

    def start_all(self) -> list[dict[str, Any]]:
        """Bring up every defined agent that is not already running."""

        started: list[dict[str, Any]] = []
        for definition in self.store.list_definitions():
            if not definition.enabled:
                # Off in production, not deleted: the definition and its history
                # stay here, and nothing starts it until the operator turns it on.
                continue
            if self.supervisor.status(definition.agent_id)["running"]:
                continue
            started.append(
                {
                    **self.supervisor.start(definition.agent_id),
                    "timeframe": definition.timeframe,
                }
            )
        return started

    def stop_all(self) -> list[str]:
        """Stop every agent this service knows about. Returns what was stopped."""

        stopped: list[str] = []
        for definition in self.store.list_definitions():
            if self.supervisor.stop(definition.agent_id):
                stopped.append(definition.agent_id)
        return stopped

    def watch_the_clock(self) -> None:
        """Stop every agent once the decision window has closed for the day.

        A belt to the worker's own braces: each agent stops itself when its
        window is over, but a worker stuck inside a model call has no way to
        notice - and a decision taken after 14:30 is a decision nobody may act
        on. The one-minute grace after the last decision moment lets the final
        candle's cycle finish before the fleet is taken down.
        """

        window = self.settings.window
        while not self._closer_stop.is_set():
            now = utc_now()
            deadline = window.last_decision_moment(now) + timedelta(seconds=60)
            wait = (deadline - now).total_seconds()
            if wait <= 0:
                break
            self._closer_stop.wait(min(60.0, wait))
        if self._closer_stop.is_set():
            return
        stopped = self.stop_all()
        self.notifier.notify(
            "🌙 <b>Module 2</b> closed for the day: stopped "
            f"{len(stopped)} agent{'s' if len(stopped) != 1 else ''}"
            + (f" ({escape(', '.join(stopped))})" if stopped else "")
            + ".",
            key="module2.day_closed",
            dedupe_seconds=0.0,
        )

    def describe_agent(self, definition: AgentDefinition) -> dict[str, Any]:
        state = self.store.load_state(definition.agent_id)
        return {
            **definition.describe(),
            "derived": {
                "buying_power": str(definition.buying_power),
                "risk_per_trade": str(definition.risk_per_trade.quantize(_CENT)),
                "max_notional": str(definition.max_notional.quantize(_CENT)),
                "allowed_intervals": sorted(definition.allowed_intervals),
                "definition_hash": definition.definition_hash(),
            },
            "state": {
                "last_attempted_bar": state.get("last_attempted_bar"),
                "last_status": state.get("last_status"),
                "last_run_at": state.get("last_run_at"),
                "updated_at": state.get("updated_at"),
            },
            **self.supervisor.status(definition.agent_id),
        }


def create_app(
    *,
    store: AgentStore | None = None,
    supervisor: Any | None = None,
    settings: Settings | None = None,
    notifier: Notifier | None = None,
) -> FastAPI:
    """Build the control service."""

    resolved = settings or load_settings()
    state = ControlState(
        settings=resolved,
        store=store or AgentStore(resolved.data_dir),
        supervisor=supervisor or Supervisor(),
        notifier=notifier or notifier_from_env("module2"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        logger.info("module 2 control starting", extra=resolved.describe())
        if state.settings.autostart:
            started = state.start_all()
            state.notifier.notify(
                f"🚀 <b>Module 2</b> started {len(started)} agent"
                f"{'s' if len(started) != 1 else ''}"
                + (
                    f" ({escape(', '.join(str(row['agent_id']) for row in started))})"
                    if started
                    else ""
                )
                + f". Decisions {state.settings.window.decision_open.strftime('%H:%M')}-"
                f"{state.settings.window.decision_close.strftime('%H:%M')} IST.",
                key="module2.started",
                dedupe_seconds=0.0,
            )
        closer = threading.Thread(
            target=state.watch_the_clock, name="module2-closer", daemon=True
        )
        closer.start()
        try:
            yield
        finally:
            state._closer_stop.set()
            state.supervisor.shutdown()
            state.notifier.close()

    app = FastAPI(
        title="Trading Framework v3 - Module 2 Agent Engine",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.control = state

    def _definition_or_404(agent_id: str) -> AgentDefinition:
        definition = state.store.load(agent_id)
        if definition is None:
            raise fail("AGENT_NOT_FOUND", f"no agent is defined with id {agent_id!r}", status_code=404)
        return definition

    def _with_defaults(body: dict[str, Any]) -> dict[str, Any]:
        """The process defaults apply only where the caller did not choose.

        Materialised into the definition rather than left implicit, so the form
        that edits an agent later shows the model and the reasoning effort that
        will actually be used, not two blank fields.
        """

        filled = dict(body)
        if not str(filled.get("model") or "").strip():
            filled["model"] = resolved.model
        if not str(filled.get("reasoning_effort") or "").strip():
            filled["reasoning_effort"] = resolved.reasoning_effort
        return filled

    @app.get("/health")
    def health() -> dict[str, Any]:
        running = state.supervisor.running()
        return {
            "ok": True,
            "service": "module_2_agent_engine",
            "checked_at": utc_now().isoformat(),
            "agents": {"defined": len(state.store.list_definitions()), "running": len(running)},
        }

    @app.get("/v1/usage")
    def token_usage(day: str | None = None) -> dict[str, Any]:
        """What the models were asked, and what each cycle cost, on one day.

        Read from the activity logs this module owns; Module 4 asks this route
        rather than reading another module's files. The day defaults to today
        in IST, because that is the day a token bill arrives on. Per-cycle wall
        time uses the explicit ``cycle_finished`` event when it exists.
        """

        state: ControlState = app.state.control
        try:
            report = usage_report(state.store, day=day)
        except ValueError as exc:
            raise fail("USAGE_INVALID", str(exc), status_code=400) from exc
        return ok(report)

    @app.get("/v1/agents")
    def list_agents() -> dict[str, Any]:
        return ok(
            {
                "agents": [
                    state.describe_agent(definition)
                    for definition in state.store.list_definitions()
                ],
                "settings": {
                    "module1_url": resolved.module1_url,
                    "llm_base_url": resolved.llm_base_url,
                    "model": resolved.model,
                    "llm_api": resolved.llm_api,
                    "reasoning_effort": resolved.reasoning_effort,
                    "data_dir": str(resolved.data_dir),
                    "window": resolved.window.describe(),
                    "notifications": state.notifier.describe(),
                },
            }
        )

    @app.post("/v1/agents")
    def create_agent(body: dict[str, Any]) -> dict[str, Any]:
        try:
            definition = AgentDefinition.from_dict(_with_defaults(body))
        except DefinitionError as exc:
            raise fail("AGENT_INVALID", str(exc), status_code=400) from exc
        if state.store.exists(definition.agent_id):
            raise fail(
                "AGENT_EXISTS",
                f"an agent called {definition.agent_id!r} already exists; edit it instead",
                status_code=409,
            )
        state.store.save(definition)
        return ok({"agent": state.describe_agent(definition)})

    # A literal path, registered before ``/{agent_id}``: the other way round,
    # "stream" would be read as an agent id and answered with a 404.
    @app.get("/v1/agents/stream")
    def stream_all() -> StreamingResponse:
        ids = [definition.agent_id for definition in state.store.list_definitions()]
        return _stream(state.store, ids)

    @app.get("/v1/agents/{agent_id}")
    def get_agent(agent_id: str) -> dict[str, Any]:
        return ok({"agent": state.describe_agent(_definition_or_404(agent_id))})

    @app.put("/v1/agents/{agent_id}")
    def update_agent(agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _definition_or_404(agent_id)
        if state.supervisor.status(agent_id)["running"]:
            raise fail(
                "AGENT_RUNNING",
                f"{agent_id} is running; stop it before changing what it is",
                status_code=409,
            )
        try:
            definition = AgentDefinition.from_dict(
                {**_with_defaults(body), "agent_id": agent_id}
            )
        except DefinitionError as exc:
            raise fail("AGENT_INVALID", str(exc), status_code=400) from exc
        state.store.save(definition)
        return ok({"agent": state.describe_agent(definition)})

    @app.delete("/v1/agents/{agent_id}")
    def retire_agent(agent_id: str) -> dict[str, Any]:
        _definition_or_404(agent_id)
        state.supervisor.stop(agent_id)
        retired = state.store.retire(agent_id)
        return ok(
            {
                "agent_id": agent_id,
                "retired_to": None if retired is None else str(retired),
                "note": "the agent is no longer listed; its log and decisions are kept",
            }
        )

    @app.post("/v1/agents/{agent_id}/start")
    def start_agent(agent_id: str) -> dict[str, Any]:
        definition = _definition_or_404(agent_id)
        if not definition.enabled:
            raise fail(
                "AGENT_DISABLED",
                f"{agent_id} is disabled; enable it in its definition before starting it",
                status_code=409,
            )
        return ok(
            {
                **state.supervisor.start(agent_id),
                "agent_id": definition.agent_id,
                "timeframe": definition.timeframe,
            }
        )

    @app.post("/v1/agents/{agent_id}/stop")
    def stop_agent(agent_id: str) -> dict[str, Any]:
        _definition_or_404(agent_id)
        stopped = state.supervisor.stop(agent_id)
        return ok({"agent_id": agent_id, "stopped": stopped, "running": False})

    @app.get("/v1/agents/{agent_id}/activity")
    def activity(agent_id: str, limit: int = 100) -> dict[str, Any]:
        _definition_or_404(agent_id)
        return ok({"agent_id": agent_id, "events": state.store.read_activity(agent_id, limit=limit)})

    @app.get("/v1/agents/{agent_id}/decisions")
    def decisions(agent_id: str, limit: int = 50) -> dict[str, Any]:
        _definition_or_404(agent_id)
        return ok(
            {
                "agent_id": agent_id,
                "decisions": state.store.read_decisions(agent_id, limit=limit),
            }
        )

    # -- the notebook -------------------------------------------------------- #

    @app.get("/v1/agents/{agent_id}/notes")
    def notes(agent_id: str) -> dict[str, Any]:
        """The reasoning this agent carries forward, key by key.

        Read-only from here on purpose: the agent writes its own notes. The
        operator may delete one - a lesson that is wrong is worse than no
        lesson - but the model is the author.
        """

        _definition_or_404(agent_id)
        return ok({"agent_id": agent_id, **notebook.document(state.store, agent_id)})

    @app.delete("/v1/agents/{agent_id}/notes")
    def clear_notes(agent_id: str) -> dict[str, Any]:
        _definition_or_404(agent_id)
        removed = notebook.clear(state.store, agent_id)
        return ok({"agent_id": agent_id, "removed": removed})

    @app.delete("/v1/agents/{agent_id}/notes/{key}")
    def delete_note(agent_id: str, key: str) -> dict[str, Any]:
        _definition_or_404(agent_id)
        try:
            outcome = notebook.apply(
                state.store, agent_id, action="delete", key=key
            )
        except NoteError as exc:
            raise fail("NOTE_NOT_FOUND", str(exc), status_code=404) from exc
        return ok({"agent_id": agent_id, **outcome})

    @app.get("/v1/agents/{agent_id}/stream")
    def stream_one(agent_id: str) -> StreamingResponse:
        _definition_or_404(agent_id)
        return _stream(state.store, [agent_id])

    @app.get("/")
    def root() -> dict[str, Any]:
        return ok(
            {
                "service": "module_2_agent_engine",
                "endpoints": [
                    "/health",
                    "/v1/usage",
                    "/v1/agents",
                    "/v1/agents/{agent_id}",
                    "/v1/agents/{agent_id}/start",
                    "/v1/agents/{agent_id}/stop",
                    "/v1/agents/{agent_id}/activity",
                    "/v1/agents/{agent_id}/decisions",
                    "/v1/agents/{agent_id}/notes",
                    "/v1/agents/{agent_id}/stream",
                    "/v1/agents/stream",
                ],
                "exchange_time": utc_now().isoformat(),
            }
        )

    return app


def _stream(store: AgentStore, agent_ids: Sequence[str]) -> StreamingResponse:
    return StreamingResponse(
        tail_events(store, agent_ids),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


app = create_app()
