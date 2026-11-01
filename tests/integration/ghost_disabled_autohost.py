#!/usr/bin/env python3
"""A real native GHost++ process must stay idle when autohost is disabled."""
import argparse
import json
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def run(runtime, invalid_map=False):
    assert 'autohost_maxgames = 0' in (runtime / 'ghost.cfg').read_text()
    result = {'scope': 'actual GHost++ process; no synthetic server', 'result': 'FAIL'}
    if invalid_map:
        (runtime / 'mapcfgs/selected.cfg').write_text('map_localpath = missing.w3x\n')
    with (runtime / 'disabled-stdout.log').open('w') as output:
        proc = subprocess.Popen([str(ROOT / 'scripts/ghost-run.sh'), 'ghost.cfg'], cwd=runtime,
                                stdout=output, stderr=subprocess.STDOUT)
        try:
            idle = 2 if invalid_map else 35
            time.sleep(idle)
            assert proc.poll() is None
            text = (runtime / 'disabled-stdout.log').read_text()
            assert '[GHOST] GHost++ Version 17.2' in text
            assert '[GHOST] creating game [' not in text
            if invalid_map:
                assert '[GHOST] offline initialization failed' in text
                assert '[GHOST] offline initialization complete' not in text
            else:
                assert '[GHOST] offline initialization complete' in text
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=8) == 0
            result.update(result='PASS', idle_seconds=idle, invalid_map=invalid_map, host_exit_code=0)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            (runtime / 'disabled-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--invalid-map', action='store_true')
    args = parser.parse_args()
    run(args.runtime.resolve(), args.invalid_map)
