#!/usr/bin/env python3
"""AMP foreground launcher; local-only, gate checked before Docker start."""
import sys
from production import cli

if __name__ == "__main__":
    raise SystemExit(cli(["run", *sys.argv[1:]]))
