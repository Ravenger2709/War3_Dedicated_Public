#!/usr/bin/env python3
"""Prepare/inspect one separate local AMP staging instance. Never connects remotely."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "amp"))
from production import cli

if __name__ == "__main__":
    raise SystemExit(cli())
