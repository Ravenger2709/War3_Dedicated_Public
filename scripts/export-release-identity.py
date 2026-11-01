#!/usr/bin/env python3
"""Maintainer export of public main-approved image identity, without pulling/pushing."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from amp.bootstrap import export_cli

if __name__ == "__main__":
    raise SystemExit(export_cli())
