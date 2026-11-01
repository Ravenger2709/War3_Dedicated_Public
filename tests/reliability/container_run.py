#!/usr/bin/env python3
"""Run native shutdown QA in an isolated CI container with two RO game inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
SOURCE_FILES = ('tests/reliability/ghost_shutdown.py', 'tests/offline/ghost_lifecycle.py',
                'patches/ghostplusplus-modern-linux.patch')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fixture-runtime', required=True, type=Path)
    p.add_argument('--binary-dir', type=Path, default=ROOT / 'build/ghost')
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--image', default='war3-qa:phase2')
    p.add_argument('--rounds', type=int, default=2)
    p.add_argument('--cases', nargs='+', default=['lobby', 'normal', 'game-graceful', 'game-force', 'game-storm', 'idle-storm', 'busy-port'])
    a = p.parse_args()
    if os.getuid() == 0 or not 1 <= a.rounds <= 10:
        p.error('non-root owner and rounds 1..10 required')
    fixture, binaries, output = (x.resolve() for x in (a.fixture_runtime, a.binary_dir, a.output))
    output.mkdir(mode=0o700)
    staging = output / 'fixture'
    staging.mkdir(mode=0o700)
    for d in ('game', 'maps', 'mapcfgs'):
        (staging / d).mkdir(mode=0o700)
    for src in (fixture / 'mapcfgs').glob('*.cfg'):
        shutil.copyfile(src, staging / 'mapcfgs' / src.name)
    for name in ('ghost.cfg', 'language.cfg'):
        shutil.copyfile(fixture / name, staging / name)
    manifest = json.loads((fixture / 'input-manifest.json').read_text())
    mapfile, archive = Path(manifest['map']).resolve(), (fixture / 'game/War3Patch.mpq').resolve()
    inputs = [(mapfile, '/inputs/selected.w3x'), (archive, '/inputs/War3Patch.mpq')]
    before = {str(src): hashlib.sha256(src.read_bytes()).hexdigest() for src, _ in inputs}
    (staging / 'game/War3Patch.mpq').symlink_to('/inputs/War3Patch.mpq')
    (staging / 'maps/selected.w3x').symlink_to('/inputs/selected.w3x')
    manifest.update(map='/inputs/selected.w3x', runtime='/fixture')
    (staging / 'input-manifest.json').write_text(json.dumps(manifest) + '\n')
    results = output / 'results'
    results.mkdir(mode=0o700)
    name = 'war3-qa-' + uuid.uuid4().hex[:12]
    # Stage only the harness and its import/patch metadata. The primary checkout
    # can contain private credentials and the complete proprietary installation.
    bundle = tempfile.TemporaryDirectory(prefix='war3-qa-sources-')
    source_root = Path(bundle.name)
    source_hashes = {}
    for relative in SOURCE_FILES:
        source = ROOT / relative
        if any(part.is_symlink() for part in (source, *source.parents) if part == ROOT or ROOT in part.parents):
            raise ValueError('QA_SOURCE_SYMLINK_REJECTED')
        data = source.read_bytes()
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        target.chmod(0o600)
        source_hashes[relative] = hashlib.sha256(data).hexdigest()
    environment = os.environ.copy()
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_TLS'):
        environment.pop(key, None)
    cmd = [*DOCKER, 'run', '--rm', '--pull=never', '--name', name, '--label', 'war3.phase2=shutdown-qa',
           '--init', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges=true', '--user', f'{os.getuid()}:{os.getgid()}',
           '--memory', '2g', '--cpus', '2', '--pids-limit', '128', '--ulimit', 'core=0',
           '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
           '--env', 'ASAN_OPTIONS=detect_leaks=1:halt_on_error=1',
           '--env', 'UBSAN_OPTIONS=print_stacktrace=1:halt_on_error=1']
    for src, dest, ro in [(source_root, '/project', True), (binaries, '/candidate', True),
                          (staging, '/fixture', True), (results, '/results', False),
                          *[(src, dest, True) for src, dest in inputs]]:
        cmd.extend(['--mount', f'type=bind,src={src},dst={dest}' + (',readonly' if ro else '')])
    cmd.extend([a.image, '--fixture-runtime', '/fixture', '--binary', '/candidate/ghost++',
                '--output', '/results/run', '--rounds', str(a.rounds), '--cases', *a.cases])
    receipt = {'container': name, 'image': a.image, 'network': 'none', 'rounds': a.rounds,
               'cases': a.cases, 'original_sha256': before, 'source_sha256': source_hashes,
               'daemon': 'unix:///var/run/docker.sock', 'project_root_mounted': False}
    receipt['image_id'] = subprocess.check_output([*DOCKER, 'image', 'inspect', a.image, '--format', '{{.Id}}'], text=True, env=environment).strip()
    try:
        r = subprocess.run(cmd, timeout=180 * a.rounds * len(a.cases) + 60, env=environment)
        receipt['exit_code'] = r.returncode
        receipt['original_inputs_unchanged'] = all(hashlib.sha256(Path(src).read_bytes()).hexdigest() == sha for src, sha in before.items())
        receipt['result'] = 'PASS' if r.returncode == 0 and receipt['original_inputs_unchanged'] else 'FAIL'
        return int(receipt['result'] != 'PASS')
    finally:
        subprocess.run([*DOCKER, 'rm', '--force', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
        bundle.cleanup()
        (output / 'container-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
