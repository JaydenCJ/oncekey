"""Allow ``python -m oncekey`` to behave exactly like the ``oncekey`` script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
