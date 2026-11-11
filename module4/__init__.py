"""Module 4 - the record, and what it says.

Module 3 keeps the book of the day: what is open, what needs watching, what
today has made. That is an operational thing and a day is the right size for it.
This is the long one - every order, across every day - because performance that
can only be seen one day at a time is not performance.

It holds no credential, opens no broker connection, and places nothing. It
reads Module 3's book through Module 3's own API, keeps what it has seen, and
answers questions about it.

Layout:

* :mod:`module4.store` - the archive: every order ever seen, atomically kept.
* :mod:`module4.clients` - Module 3's book, read day by day.
* :mod:`module4.ledger` - what the archive adds up to.
* :mod:`module4.service` - the HTTP surface the dashboard reads.
"""

from module4.config import Settings, load_settings

__all__ = ["Settings", "__version__", "load_settings"]

__version__ = "0.1.0"
