#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
GAME_DIR=${GHOST_GAME_DIR:-"$ROOT/Warcraft 3 Frozen Throne"}
MAP=${GHOST_MAP:-"$GAME_DIR/maps/Ather/(4)HearthglenHills.w3x"}
RUNTIME=${GHOST_TEST_RUNTIME:-"$ROOT/runtime/ghost-test-$(date -u +%Y%m%dT%H%M%S)-$$"}
python3 scripts/prepare-map.py --game-dir "$GAME_DIR" --map "$MAP" \
    --runtime "$RUNTIME" --host-port "${GHOST_TEST_PORT:-16112}" --max-players 2 --countdown-seconds 2
exec python3 tests/offline/ghost_lifecycle.py --runtime "$RUNTIME"
