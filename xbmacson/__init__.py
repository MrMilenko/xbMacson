"""xbMacson: a debug monitor and scriptable client for XBDM.

The package is a small library (``xbmacson.connection``, ``xbmacson.client``,
``xbmacson.notify``) with a command line front end (``xbmacson.cli``) and a
curses terminal UI (``xbmacson.tui``). Pure Python 3 standard library.
"""

__version__ = "2.0.0"

from .protocol import Reply, XbdmError, ConnectError  # noqa: F401
from .profiles import PROFILES, get_profile  # noqa: F401
from .client import Console  # noqa: F401
