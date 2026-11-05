#!/usr/bin/env python3
"""Real Docker foreground-owner loss, without native/Wine/assets or host ports.

Complements test_runtime_attachment.py's strict adoption-contract regressions.
The payload is a synthetic heartbeat process, not a gameplay/realm proof.
"""
import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from amp.runtime_attachment import AttachmentError, owner

DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']


def output(args):
    return subprocess.check_output([*DOCKER, *args], stderr=subprocess.DEVNULL, timeout=15)


def line(process):
    if not select.select([process.stdout], [], [], 10)[0]:
        raise RuntimeError('FIXTURE_OUTPUT_TIMEOUT')
    return process.stdout.readline().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id', required=True)
    args = parser.parse_args()
    if re.fullmatch(r'sha256:[0-9a-f]{64}', args.image_id) is None:
        raise ValueError('exact image ID required')
    name = 'war3-amp-owner-repro-' + uuid.uuid4().hex
    processes, identity = [], None
    result = {'fixture': 'synthetic Docker heartbeat only', 'image_id': args.image_id,
              'network': 'none', 'host_ports': False, 'native_or_wine_executed': False}
    with tempfile.TemporaryDirectory(prefix='war3-amp-owner-') as temp:
        instance = Path(temp);(instance / 'control').mkdir(mode=0o700)
        heartbeat = "import time,signal,sys;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));print('fixture-ready',flush=True)\nwhile True:time.sleep(.2);print('heartbeat',flush=True)"
        controller = ("import os,sys;sys.path.insert(0,sys.argv.pop(1));from amp.runtime_attachment import owner;"
                      "from pathlib import Path;\nwith owner(Path(sys.argv[1])) as fd:\n os.set_inheritable(fd,True);"
                      "os.execvp(sys.argv[2],[sys.argv[2],*sys.argv[3:]])")
        try:
            initial = subprocess.Popen([*DOCKER, 'run', '--rm', '--pull=never', '--name', name,
                        '--label', 'war3.test=amp-owner-repro', '--init', '--interactive', '--network', 'none',
                        '--read-only', '--user', f'{os.geteuid()}:{os.getegid()}', '--cap-drop', 'ALL',
                        '--security-opt', 'no-new-privileges:true', '--pids-limit', '32', '--memory', '128m',
                        '--log-driver', 'none', '--entrypoint', 'python3', args.image_id, '-I', '-B', '-c', heartbeat],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            processes.append(initial)
            if line(initial) != b'fixture-ready':raise RuntimeError('FIXTURE_NOT_READY')
            meta = json.loads(output(['inspect', name]))[0];identity = meta['Id']
            initial.kill();initial.wait(timeout=10)
            if not json.loads(output(['inspect', identity]))[0]['State']['Running']:
                raise RuntimeError('SURVIVOR_NOT_RUNNING')
            result['old_launcher_loss_leaves_running_container'] = True
            for round_number in (1, 2):
                attached = subprocess.Popen([sys.executable, '-B', '-c', controller, str(ROOT), str(instance),
                                DOCKER[0], *DOCKER[1:], 'attach', '--sig-proxy=true', identity],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                processes.append(attached)
                if line(attached) != b'heartbeat':raise RuntimeError('ATTACH_NOT_STREAMING')
                try:
                    with owner(instance):raise RuntimeError('DUPLICATE_OWNER_ACCEPTED')
                except AttachmentError as error:
                    if error.args != ('OWNER_BUSY',):raise
                current = json.loads(output(['inspect', identity]))[0]
                if current['Id'] != identity or not current['State']['Running']:
                    raise RuntimeError('RUNTIME_REPLACED')
                if round_number == 1:
                    attached.kill();attached.wait(timeout=10)
                    with owner(instance):pass
                else:
                    attached.send_signal(signal.SIGTERM)
                    if attached.wait(timeout=15) != 0:raise RuntimeError('ATTACHED_STOP_FAILED')
            result.update(recovered_same_container_twice=True, owner_lease_exclusive=True,
                          owner_lease_released_after_sigkill=True, attached_sigterm_stops_runtime=True)
            # AutoRemove can finish shortly after attach has observed process exit.
            end = time.monotonic() + 5
            while output(['ps', '-aq', '--filter', 'name=^/' + name + '$']).strip() and time.monotonic() < end:
                time.sleep(.1)
            if output(['ps', '-aq', '--filter', 'name=^/' + name + '$']).strip():
                raise RuntimeError('FIXTURE_NOT_REMOVED')
            result['result'] = 'PASS'
            print(json.dumps(result))
        finally:
            for process in processes:
                if process.poll() is None:process.kill();process.wait(timeout=10)
                if process.stdout:process.stdout.close()
            remaining = output(['ps', '-aq', '--no-trunc', '--filter', 'name=^/' + name + '$']).decode().strip()
            if remaining:
                meta = json.loads(output(['inspect', remaining]))[0]
                if (meta['Image'] != args.image_id or meta['Config']['Labels'].get('war3.test') != 'amp-owner-repro'
                        or identity is not None and remaining != identity):
                    raise RuntimeError('CLEANUP_OWNER_REJECTED')
                output(['rm', '--force', remaining])


if __name__ == '__main__':
    main()
