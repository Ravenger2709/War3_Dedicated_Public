#!/usr/bin/env python3
"""Stage only public open source, then run native tests in local network=none Docker.

Requires an existing raw/patched pinned local vendor checkout and RO public deps.
No downloads, remote Docker, game/account artifacts or production mounts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
BUILDER = 'sha256:c6e84f827e7b3483a56f67755ff1cbca3f5bb7a90e0253c4379a504ef5e7e62f'
REVISION = '6ba90f426e96f80fe013a468d1e07151a510adbe'
STORM_REVISION = '3846f0b8e2c47320c6b499492496f3e3f2e76821'
FILES = ['scripts/build-ghost.sh', 'scripts/ghost-check-source.py',
         'tests/offline/test_ghost_source_guard.py', 'tests/phase3/native-build.sh',
         'tests/phase3/native-driver.cpp', 'tests/phase3/native-test.py',
         'tests/phase3/native-vote-reminder-test.py',
         'tests/phase3/native-multimatch-test.py', 'tests/phase3/native-lifecycle-test.py',
         'tests/phase3/native-reservation-gate-test.py', 'tests/phase3/native-fenced-close-test.py', 'tests/phase3/native-fence-crash.c',
         'tests/bridge/native-publication-driver.cpp', 'tests/bridge/native-publication-test.py', 'tests/bridge/test_publication_file_race.py',
         'third_party/ghost.lock.json', 'patches/ghostplusplus-modern-linux.patch',
         'src/hostbot/NOTICE.md', 'src/hostbot/mpq-map-info.cpp',
         'src/bridge/__init__.py', 'src/bridge/bncs.py', 'src/bridge/star_relay.py',
         'src/runtime/__init__.py', 'src/runtime/files.py', 'src/runtime/lifecycle.py',
         'src/runtime/cycle_state.py', 'src/runtime/cycle_coordinator.py', 'src/runtime/reservation_marker.py',
         'src/runtime/process_ownership.py',
         'tests/phase3/native-next-preset-test.py', 'tests/phase3/native-map-bounds-test.py',
         'tests/phase3/native-sealed-map-test.py', 'tests/phase3/native-preset-fence-test.py',
         'tests/phase3/test_map_presets.py', 'amp/map_discovery.py',
         'tests/phase4/test_preload_catalog.py', 'tests/phase4/native-manual-preload-test.py',
         'tests/phase4/native-preload-ipc-test.py', 'tests/phase4/native-startup-ipc-test.py',
         'tests/phase4/native-status-cycle-test.py',
         'tests/phase4/native-controller-cycle-e2e-test.py', 'tests/phase4/test_native_match_telemetry.py',
         'tests/phase4/native-controller-countdown-e2e-test.py',
         'tests/phase4/native-controller-capacity3-test.py',
         'tests/phase4/native-runtime-backend-e2e-test.py',
         'tests/phase4/native-controlled-restart-test.py',
         'tests/phase4/native-startup-catalog-test.py', 'tests/phase4/native-manifest-envelope-test.py',
         'tests/phase4/native-sha256-test.py',
         'tests/phase4/native-kick-vote-test.py',
         'tests/phase4/native-catalog-close-recovery-test.py',
         'src/connector/__init__.py']
# Lifecycle and sealed-map suites import the real controller and runtime modules.
# Stage public Python sources only; no runtime state or vendor artifacts.
FILES += [str(path.relative_to(ROOT)) for directory in ('src/runtime','src/supervisor','src/bridge','src/connector')
          for path in sorted((ROOT/directory).glob('*.py'))
          if str(path.relative_to(ROOT)) not in FILES]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'vendor/ghostplusplus')
    parser.add_argument('--storm-source', type=Path, default=ROOT/'vendor/stormlib')
    parser.add_argument('--builder', default=BUILDER, help='local test builder image containing CMake and a compiler')
    parser.add_argument('--deps', type=Path, required=True)
    parser.add_argument('--sanitizers', choices=['address,undefined'], default=None)
    parser.add_argument('--full-cgroup', action='store_true', help='use host cgroup namespace for offline admission tests')
    args = parser.parse_args()
    source, storm_source, deps = args.source.resolve(), args.storm_source.resolve(), args.deps.resolve()
    if subprocess.check_output(['git', '-C', str(storm_source), 'rev-parse', 'HEAD'], text=True).strip() != STORM_REVISION:
        parser.error('StormLib source must have the pinned upstream HEAD')
    if not (deps/'usr/include/boost').is_dir():
        parser.error('deps must be the existing public native dependency directory')
    head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if head != REVISION:
        parser.error('source must have the unchanged pinned upstream HEAD')
    build = ROOT/'build'; build.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='native-ci-', dir=build))
    for item in FILES:
        dst=stage/item; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(ROOT/item, dst)
    vendor=stage/'vendor/ghostplusplus'; vendor.parent.mkdir()
    subprocess.run(['git', 'clone', '--quiet', '--no-checkout', '--no-hardlinks', str(source), str(vendor)], check=True)
    subprocess.run(['git', '-C', str(vendor), 'config', 'core.sparseCheckout', 'true'], check=True)
    (vendor/'.git/info/sparse-checkout').write_text('/*\n!*.exe\n!*.dll\n!*.w3x\n!*.w3m\n!*.mpq\n!*.icc\n')
    subprocess.run(['git', '-C', str(vendor), 'checkout', '--quiet', '--detach', REVISION], check=True)
    subprocess.run(['git', '-C', str(vendor), 'apply', str(stage/'patches/ghostplusplus-modern-linux.patch')], check=True)
    storm=stage/'vendor/stormlib'
    subprocess.run(['git', 'clone', '--quiet', '--no-checkout', '--no-hardlinks', str(storm_source), str(storm)], check=True)
    subprocess.run(['git', '-C', str(storm), 'config', 'core.sparseCheckout', 'true'], check=True)
    (storm/'.git/info/sparse-checkout').write_text('/*\n!*.exe\n!*.dll\n!*.w3x\n!*.w3m\n!*.mpq\n!*.icc\n')
    subprocess.run(['git', '-C', str(storm), 'checkout', '--quiet', '--detach', STORM_REVISION], check=True)
    forbidden={'.exe','.dll','.w3x','.w3m','.mpq','.icc'}
    if any(p.suffix.lower() in forbidden for tree in (vendor, storm) for p in tree.rglob('*') if '.git' not in p.parts):
        raise SystemExit('refusing binary/game artifacts in source mount')
    log=stage/'native-test.log'
    cmd=['docker','--host','unix:///var/run/docker.sock','run','--rm','--pull=never','--network=none',
         '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true',
         '--user',f'{os.getuid()}:{os.getgid()}','--cpus=2','--memory=3g','--pids-limit=128','--ulimit','core=0:0',
         '--tmpfs','/tmp:rw,nosuid,nodev,size=512m',
         '--mount',f'type=bind,src={stage},dst=/repo',
         '--mount',f'type=bind,src={deps},dst=/deps,readonly','--workdir','/repo','--entrypoint','/bin/bash']
    if args.full_cgroup: cmd += ['--cgroupns=host']
    if args.sanitizers: cmd += ['-e',f'GHOST_SANITIZERS={args.sanitizers}']
    cmd += [args.builder,'tests/phase3/native-build.sh']
    started = time.monotonic()
    with log.open('w') as out:
        code=subprocess.run(cmd,stdout=out,stderr=subprocess.STDOUT).returncode
    log_bytes = log.read_bytes()
    summaries = re.findall(rb'Ran (\d+) tests in ([\d.]+)s', log_bytes)
    result=dict(version=2, builder=args.builder, upstream_revision=REVISION, stormlib_revision=STORM_REVISION,
                patch_sha256=hashlib.sha256((stage/'patches/ghostplusplus-modern-linux.patch').read_bytes()).hexdigest(),
                staged_inputs_sha256={name:hashlib.sha256((stage/name).read_bytes()).hexdigest() for name in FILES},
                network='none',sanitizers=args.sanitizers or '',exit_code=code,log=str(log),
                log_sha256=hashlib.sha256(log_bytes).hexdigest(),
                elapsed_seconds=round(time.monotonic()-started,3),
                test_summaries=[dict(tests=int(n), seconds=float(seconds)) for n,seconds in summaries],
                source_guard_passed=b'PASS: pinned source + exact declared patch, no unexpected inputs' in log_bytes
                    and b'PASS: exact patch accepted; unexpected untracked header and tracked source edit rejected' in log_bytes,
                real_client_tested=False,slash_client_delivery_tested=False,iccup_ack_or_bump_tested=False)
    binary=stage/'build/ghost/ghost++'
    if binary.is_file():
        result['native_binary_sha256']=hashlib.sha256(binary.read_bytes()).hexdigest()
    (stage/'native-result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    raise SystemExit(code)


if __name__=='__main__':
    main()
