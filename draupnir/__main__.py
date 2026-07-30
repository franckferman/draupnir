"""`python -m draupnir`."""

from __future__ import annotations

import sys

from .cli import EXIT_INTERRUPTED, main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPTED)
