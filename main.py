"""File entry point for hosting services that run `python main.py`."""

import sys

from studio_bot.__main__ import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("run")
    sys.exit(main())
