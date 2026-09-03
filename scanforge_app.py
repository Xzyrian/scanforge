"""Frozen-application entry point.

PyInstaller needs a real script to analyse, not a package run with -m, so this
is the thin wrapper it builds around.  Running it directly works too and is
identical to `python -m scanforge`.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Harmless when the app only uses threads, but it costs nothing and stops a
    # frozen build from re-launching itself if a dependency ever spawns a pool.
    multiprocessing.freeze_support()

    if "--selftest" in sys.argv:
        from tools.freeze_check import selftest
        return selftest()

    from scanforge.__main__ import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())
