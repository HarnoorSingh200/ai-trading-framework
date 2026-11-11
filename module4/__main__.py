"""Run Module 4 as a service: ``python -m module4``.

One process. It holds no position and makes no decision; it keeps the long
record of what the other modules did and answers questions about it.
"""

from __future__ import annotations

import argparse

from common.logging import setup_logging
from module4.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="module4",
        description="Trading Framework v3 - Module 4 reporting",
    )
    parser.parse_args(argv)
    settings = load_settings()
    setup_logging("module4")

    import uvicorn

    uvicorn.run(
        "module4.service:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
