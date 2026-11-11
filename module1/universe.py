"""Which symbols Module 1 covers.

NIFTY 50, taken from the exchange's own constituent file rather than typed by
hand. Index membership changes when NSE rebalances, so the list is treated as
data with a fetch date attached, and it is checked in so that a start-up does
not depend on niftyindices.com being reachable.

The company name and industry travel with each symbol: the UI shows them, and
they are what makes a symbol like ``TMPV`` or ``ETERNAL`` readable to a person
without a lookup.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from module1.config import MODULE1_DIR

#: Where the list came from, and when. Refetch it when NSE rebalances.
NIFTY50_SOURCE_URL: Final[str] = (
    "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv"
)
NIFTY50_FETCHED: Final[str] = "2026-09-21"

#: The checked-in copy, used when the network is not available at start-up.
NIFTY50_FILE: Final[Path] = MODULE1_DIR / "data" / "nifty50.csv"

#: The index this module is built around.
UNIVERSE_NAME: Final[str] = "NIFTY 50"


class UniverseError(RuntimeError):
    """The universe could not be read."""


@dataclass(frozen=True, slots=True)
class Constituent:
    """One member of the index."""

    symbol: str
    name: str
    industry: str

    def describe(self) -> dict[str, str]:
        return {"symbol": self.symbol, "name": self.name, "industry": self.industry}


def load_universe(path: Path | None = None) -> tuple[Constituent, ...]:
    """Read the checked-in constituent list."""

    source = path or NIFTY50_FILE
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise UniverseError(f"the constituent list could not be read: {source}") from exc
    return parse_universe(text, source=str(source))


def parse_universe(text: str, *, source: str = "text") -> tuple[Constituent, ...]:
    """Parse the exchange's constituent CSV.

    Blank rows are ignored, so the file can be annotated. Symbols are
    upper-cased and de-duplicated in file order, which keeps the resulting list
    stable enough to hash and to compare across runs.
    """

    reader = csv.DictReader(io.StringIO(text))
    seen: dict[str, Constituent] = {}
    for row in reader:
        symbol = str(row.get("Symbol") or "").strip().upper()
        if not symbol:
            continue
        seen.setdefault(
            symbol,
            Constituent(
                symbol=symbol,
                name=str(row.get("Company Name") or symbol).strip(),
                industry=str(row.get("Industry") or "Unclassified").strip(),
            ),
        )
    if not seen:
        raise UniverseError(f"no constituents were found in {source}")
    return tuple(seen.values())
