#!/usr/bin/env python3
"""Prepare one new private application directory; never starts Docker/AMP/auth."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from amp.bootstrap import cli

if __name__ == "__main__":
    raise SystemExit(cli())
