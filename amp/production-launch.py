#!/usr/bin/env python3
"""AMP foreground owner: adopt a healthy survivor or perform a gated start."""
import sys
import production
from runtime_attachment import launch

if __name__ == '__main__':
    raise SystemExit(launch(production, sys.argv[1:]))
