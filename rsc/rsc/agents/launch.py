"""Exec a private launch spec, preserving terminal ownership and native OSC status."""

import json
import os
import sys
from pathlib import Path


def main():
    spec = json.loads(Path(sys.argv[1]).read_text())
    os.chdir(spec["cwd"])
    os.execvpe(spec["argv"][0], spec["argv"], {**os.environ, **spec["env"]})


if __name__ == "__main__":
    main()
