"""Module 3 - the order manager.

Module 2 decides; this module is what makes a decision a position. It takes an
order from Module 2, watches the price of that order's stock through Module 1,
and keeps answering the only four questions anyone asks about a trade: has it
been executed, did it reach its target, did it hit its stop, or has it simply
run out of the time it was given.

It holds no credential and opens no connection to the broker. Nothing here can
place a real order, and the module is built so that nothing here could: there is
no adapter to add one to, and "executed" means the framework took the position
itself, at the first live price Module 1 published for that stock.

Layout:

* :mod:`module3.orders` - what an order is, and the only ways it may change.
* :mod:`module3.store` - the ledger: a checkpoint and an append-only event log.
* :mod:`module3.clients` - Module 1's prices and Module 2's decisions, as HTTP.
* :mod:`module3.tracker` - intake, fills, exits, expiry and reconciliation.
* :mod:`module3.service` - the HTTP surface the operator site talks to.
"""

from module3.config import Settings, load_settings

__all__ = ["Settings", "__version__", "load_settings"]

__version__ = "0.1.0"
