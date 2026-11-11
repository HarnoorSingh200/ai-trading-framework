"""Paths, environment and tunables for Module 4.

Held as a frozen :class:`Settings` the same way the other modules do it. There
is nothing secret here and nothing to keep secret: this module reads an order
book and adds it up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: This package, and the repository root that contains it.
MODULE4_DIR: Final[Path] = Path(__file__).resolve().parent
PROJECT_ROOT: Final[Path] = MODULE4_DIR.parent

DEFAULT_HOST: Final[str] = "127.0.0.1"
#: The dashboard. Module 1 is 5201, Module 2 is 5202, Module 3 is 5203.
DEFAULT_PORT: Final[int] = 5204
#: Where the order book being archived lives.
DEFAULT_MODULE3_URL: Final[str] = "http://127.0.0.1:5203"
#: Where the agent engine's token-usage route lives. The logs belong to Module
#: 2; this module asks its HTTP surface for the totals rather than reading its
#: files.
DEFAULT_MODULE2_URL: Final[str] = "http://127.0.0.1:5202"
#: Where the index the agents are measured against is read from. A benchmark is
#: a market fact, so it comes from Module 1, not from this module's own idea of
#: the market.
DEFAULT_MODULE1_URL: Final[str] = "http://127.0.0.1:5201"
#: The notional the benchmark line is drawn for: "what a buy-and-hold of this
#: much in the index would have made". The agents' own capital is 50,000.
DEFAULT_BENCHMARK_NOTIONAL: Final[float] = 50_000.0

#: How often to ask Module 3 whether anything new has happened. Cheap: a
#: finished day never changes, so a sync is usually one or two requests.
DEFAULT_SYNC_SECONDS: Final[float] = 30.0
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
#: The S3 archive is optional. When a bucket is configured, the local archive
#: is the fast read path and S3 is its durable mirror.
DEFAULT_S3_BUCKET: Final[str] = "your-archive-bucket"
DEFAULT_S3_PREFIX: Final[str] = "stock market v3"
DEFAULT_S3_REGION: Final[str] = "ap-southeast-1"
#: How much of the archive one answer carries when the caller does not say.
DEFAULT_ORDER_LIMIT: Final[int] = 200
MAX_ORDER_LIMIT: Final[int] = 2000


def _text(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _number(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    """Module 4's runtime configuration."""

    environment: str
    host: str
    port: int
    data_dir: Path
    module3_url: str
    sync_seconds: float
    http_timeout_seconds: float
    module2_url: str = DEFAULT_MODULE2_URL
    module1_url: str = DEFAULT_MODULE1_URL
    benchmark_notional: float = DEFAULT_BENCHMARK_NOTIONAL
    s3_bucket: str = DEFAULT_S3_BUCKET
    s3_prefix: str = DEFAULT_S3_PREFIX
    s3_region: str = DEFAULT_S3_REGION

    @property
    def s3_enabled(self) -> bool:
        return bool(self.s3_bucket.strip())

    def describe(self) -> dict[str, object]:
        """Safe for logs and for the UI's status panel."""

        return {
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "module3_url": self.module3_url,
            "module2_url": self.module2_url,
            "module1_url": self.module1_url,
            "benchmark_notional": self.benchmark_notional,
            "sync_seconds": self.sync_seconds,
            "s3_enabled": self.s3_enabled,
            "s3_bucket": self.s3_bucket or None,
            "s3_prefix": self.s3_prefix,
            "s3_region": self.s3_region,
        }


def load_settings() -> Settings:
    """Build the settings for this process."""

    data_dir = Path(_text("MODULE4_DATA_DIR", "data/history"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    return Settings(
        environment=_text("TRADING_ENV", "development"),
        host=_text("MODULE4_HOST", DEFAULT_HOST),
        port=int(_number("MODULE4_PORT", DEFAULT_PORT)),
        data_dir=data_dir,
        module3_url=_text("MODULE3_URL", DEFAULT_MODULE3_URL).rstrip("/"),
        module2_url=_text("MODULE2_URL", DEFAULT_MODULE2_URL).rstrip("/"),
        module1_url=_text("MODULE1_URL", DEFAULT_MODULE1_URL).rstrip("/"),
        benchmark_notional=_number(
            "MODULE4_BENCHMARK_NOTIONAL", DEFAULT_BENCHMARK_NOTIONAL
        ),
        sync_seconds=_number("MODULE4_SYNC_SECONDS", DEFAULT_SYNC_SECONDS),
        http_timeout_seconds=_number(
            "MODULE4_HTTP_TIMEOUT_SECONDS", DEFAULT_HTTP_TIMEOUT_SECONDS
        ),
        s3_bucket=_text("MODULE4_S3_BUCKET", DEFAULT_S3_BUCKET),
        s3_prefix=_text("MODULE4_S3_PREFIX", DEFAULT_S3_PREFIX).strip("/"),
        s3_region=_text("MODULE4_S3_REGION", DEFAULT_S3_REGION),
    )
