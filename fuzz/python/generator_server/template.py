"""Removed production path.

The generator server forks case workers itself. This module remains so an old
command fails with an explicit error instead of starting a second process.
"""

import sys


def main() -> None:
    sys.stderr.write("generator_server.template is not a production entrypoint\n")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
