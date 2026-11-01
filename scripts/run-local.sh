#!/usr/bin/env bash
# Run a separately prepared offline runtime. Never modify original game files.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -ne 1 ]]; then
    echo 'Usage: scripts/run-local.sh PREPARED_RUNTIME_DIR' >&2
    exit 2
fi
RUNTIME=$(cd -- "$1" && pwd)
[[ -f "$RUNTIME/ghost.cfg" ]] || { echo 'Prepare the runtime with prepare-map.py first.' >&2; exit 2; }
cd "$ROOT"
python3 - "$ROOT" "$RUNTIME" "${WAR3_STATUS_PORT:-18081}" <<'PY'
import json
from pathlib import Path
import sys
root, runtime = map(Path, sys.argv[1:3])
port = int(sys.argv[3])
if not 1 <= port <= 65535:
    raise SystemExit('Invalid WAR3_STATUS_PORT')
path = runtime / 'supervisor.json'
if not path.exists():
    cfg = {'mode': 'offline', 'runtime_dir': str(runtime / 'supervisor'),
           'hostbot': {'command': [str(root / 'scripts/ghost-run.sh'), str(runtime / 'ghost.cfg')],
                       'cwd': str(runtime), 'startup_timeout': 90, 'stop_timeout': 15,
                       'lobby_timeout': 3600},
           'http': {'host': '127.0.0.1', 'port': port}}
    with path.open('x') as target:
        json.dump(cfg, target, indent=2)
        target.write('\n')
    path.chmod(0o600)
PY
python3 -m src.supervisor --config "$RUNTIME/supervisor.json" --check
exec python3 -m src.supervisor --config "$RUNTIME/supervisor.json"
