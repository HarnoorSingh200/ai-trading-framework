"""Run Module 1 as a service: ``python -m module1``."""

from __future__ import annotations

import uvicorn

from common.logging import setup_logging
from module1.config import load_settings


def main() -> int:
    settings = load_settings()
    setup_logging("module1")
    uvicorn.run(
        "module1.service:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        # Uvicorn's own loggers propagate into the handlers set up above, so
        # one process writes one file instead of two differently-shaped ones.
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
