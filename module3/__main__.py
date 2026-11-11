"""Run Module 3 as a service: ``python -m module3``.

One process, unlike Module 2's one-per-agent: there is exactly one order book,
and a second copy of it would be a second answer to "what do we hold".
"""

from __future__ import annotations

import argparse

from common.logging import setup_logging
from module3.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="module3",
        description="Trading Framework v3 - Module 3 order manager",
    )
    parser.parse_args(argv)
    settings = load_settings()
    setup_logging("module3")

    import uvicorn

    uvicorn.run(
        "module3.service:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
