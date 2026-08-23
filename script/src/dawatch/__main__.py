"""Allow ``python -m dawatch``."""

import sys

from dawatch.cli import main

if __name__ == "__main__":
    sys.exit(main())
