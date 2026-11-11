"""The central process's own HTTP surface: what the Control tab reads.

The runner is where the four services are started, watched and stopped, so it
is also the only place that can answer the operator's three questions in one
look: what is running, what did it say, and restart it. None of that is market
data or a trading decision - this process holds none - so the endpoints stay
read-mostly and the one write is a restart of a service that already exists.

Like every other module, the answers are snapshots: the status carries the
moment it was taken, so a screen can say how old it is instead of implying it
is live. The log reader is a tail, not a stream: the operator asks for the last
N lines and gets them, and the site can ask again on its own clock.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from common.window import IST
from runner.config import RunnerSettings
from runner.supervisor import SERVICES, Supervisor

logger = logging.getLogger("runner.control")

#: What each service is, for a screen that should not have to know the module
#: numbers by heart.
SERVICE_LABELS: dict[str, str] = {
    "module1": "Market data",
    "module2": "Agent engine",
    "module3": "Order manager",
    "module4": "Reporting",
}

#: The two files a service writes: its own rotating log (what it decided) and
#: the process output the supervisor captures (what the interpreter printed).
LOG_KINDS: tuple[str, ...] = ("log", "process")

DEFAULT_LOG_LINES: int = 300
MAX_LOG_LINES: int = 2000


def log_path(settings: RunnerSettings, service: str, kind: str) -> Path:
    """The file one service writes, by kind."""

    suffix = ".process.log" if kind == "process" else ".log"
    return settings.log_dir / f"{service}{suffix}"


def tail_lines(path: Path, lines: int) -> list[str]:
    """The last ``lines`` lines of a file, oldest first.

    Read from the end in blocks rather than streaming the whole file: a log
    that has been rotating all month is not worth loading to answer what
    happened in the last minute.
    """

    wanted = max(1, min(int(lines), MAX_LOG_LINES))
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = 8192
            chunks: list[bytes] = []
            newlines = 0
            position = size
            while position > 0 and newlines <= wanted:
                step = min(block, position)
                position -= step
                handle.seek(position)
                chunk = handle.read(step)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
            content = b"".join(reversed(chunks))
    except OSError:
        return []
    text = content.decode("utf-8", errors="replace")
    found = text.splitlines()
    if len(found) > wanted:
        found = found[-wanted:]
    return found


@dataclass(slots=True)
class ControlState:
    """What the control app answers from.

    ``snapshot`` and ``restart`` are supplied by the runner's main loop: the
    status includes the day's own state, and a restart must go through the same
    supervisor the loop uses - not a second, parallel one.
    """

    settings: RunnerSettings
    supervisor: Supervisor
    snapshot: Callable[[], dict[str, Any]]
    restart: Callable[[str], dict[str, Any]]


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _fail(code: str, message: str, *, status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"ok": False, "error": {"code": code, "message": message}},
    )


def _checked(service: str) -> str:
    name = str(service or "").strip().lower()
    if name not in SERVICES:
        raise _fail(
            "SERVICE_UNKNOWN",
            f"{service!r} is not one of {', '.join(SERVICES)}",
            status_code=404,
        )
    return name


def create_control_app(state: ControlState) -> FastAPI:
    """Build the control surface the Control tab talks to."""

    app = FastAPI(
        title="Trading Framework v3 - Runner Control",
        version="0.1.0",
    )
    app.state.control = state

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "framework_control",
            "checked_at": datetime.now(IST).isoformat(),
            "services": state.supervisor.running(),
        }

    @app.get("/v1/status")
    def status() -> dict[str, Any]:
        """The whole framework in one answer: the day, then each service."""

        return ok(state.snapshot())

    @app.get("/v1/logs/{service}")
    def logs(
        service: str,
        kind: str = "log",
        lines: int = DEFAULT_LOG_LINES,
    ) -> dict[str, Any]:
        name = _checked(service)
        wanted_kind = str(kind or "log").strip().lower()
        if wanted_kind not in LOG_KINDS:
            raise _fail(
                "LOG_KIND_UNKNOWN",
                f"{kind!r} is not one of {', '.join(LOG_KINDS)}",
            )
        path = log_path(state.settings, name, wanted_kind)
        found = tail_lines(path, lines)
        return ok(
            {
                "service": name,
                "label": SERVICE_LABELS.get(name, name),
                "kind": wanted_kind,
                "file": str(path),
                "exists": path.exists(),
                "returned": len(found),
                "lines": found,
            }
        )

    @app.post("/v1/services/{service}/restart")
    def restart(service: str) -> dict[str, Any]:
        """Stop one service and start it again, now.

        The operator's escape hatch. It works whether the service is running,
        crashed, or resting for the market: starting Module 3 on a closed day
        is harmless - it holds the day's book - and starting Module 2 brings
        its registry back even if its agents are between sessions.
        """

        name = _checked(service)
        report = state.restart(name)
        logger.info("operator restarted %s from the control surface", name)
        return ok({"service": name, "label": SERVICE_LABELS.get(name, name), **report})

    @app.get("/")
    def root() -> dict[str, Any]:
        return ok(
            {
                "service": "framework_control",
                "endpoints": [
                    "/health",
                    "/v1/status",
                    "/v1/logs/{service}",
                    "/v1/services/{service}/restart",
                ],
                "services": list(SERVICES),
            }
        )

    return app


def serve(app: FastAPI, settings: RunnerSettings) -> Any:
    """A configured uvicorn server for the control app.

    Returned rather than started so the caller owns the thread and can ask it
    to exit; the runner keeps running even if this surface cannot be reached.
    """

    import uvicorn

    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.control_host,
            port=settings.control_port,
            log_level="warning",
            log_config=None,
            access_log=False,
        )
    )


def start_in_thread(server: Any) -> threading.Thread:
    """Run a server in a daemon thread and return it."""

    thread = threading.Thread(target=server.run, name="runner-control", daemon=True)
    thread.start()
    return thread
