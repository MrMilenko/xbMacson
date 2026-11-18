#!/usr/bin/env python3
"""xbMacson launcher.

    python3 xbmacson.py                 terminal UI
    python3 xbmacson.py <console>       terminal UI, connect right away
    python3 xbmacson.py <command> ...   scriptable commands (see --help)

The code lives in the xbmacson/ package next to this file.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xbmacson.cli import COMMANDS, main  # noqa: E402


def _entry(argv):
    # Old style: a single host argument opens the TUI on it.
    if len(argv) == 1 and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        argv = ["tui", argv[0]]
    return main(argv)


if __name__ == "__main__":
    sys.exit(_entry(sys.argv[1:]))
