"""Allow ``python -m airfryer`` to reach the same entry point as the console script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
