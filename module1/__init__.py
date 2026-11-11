"""Module 1 - market data.

This package is the whole of Module 1: the broker session, the instrument
master, the traded universe, candle history and the live price feed. It owns
every conversation with the broker, and it serves what it knows over HTTP to
the UI. Nothing else in v3 talks to Zerodha.

Layout:

* :mod:`module1.broker` - the session, the HTTP client and the tick feed.
* :mod:`module1.universe` - which symbols this module covers.
* :mod:`module1.instruments` - symbol to instrument-token resolution.
* :mod:`module1.market` - the in-memory board the API reads.
* :mod:`module1.service` - the HTTP surface.
"""

from module1.config import Settings, load_settings

__all__ = ["Settings", "__version__", "load_settings"]

__version__ = "0.1.0"
