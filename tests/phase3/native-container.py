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
FILES = ['scripts/build-ghost.sh', 'scripts/ghost-check-source.py',
         'tests/offline/test_ghost_source_guard.py', 'tests/phase3/native-build.sh',
         'tests/phase3/native-driver.cpp', 'tests/phase3/native-test.py',
         'tests/bridge/native-publication-driver.cpp', 'tests/bridge/native-publication-test.py',
         'third_party/ghost.lock.json', 'patches/ghostplusplus-modern-linux.patch',
         'src/hostbot/NOTICE.md', 'src/hostbot/mpq-map-info.cpp']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'vendor/ghostplusplus')
    parser.add_argument('--deps', type=Path, required=True)
    parser.add_argument('--sanitizers', choices=['address,undefined'], default=None)
    args = parser.parse_args()
    source, deps = args.source.resolve(), args.deps.resolve()
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
    forbidden={'.exe','.dll','.w3x','.w3m','.mpq','.icc'}
    if any(p.suffix.lower() in forbidden for p in vendor.rglob('*') if '.git' not in p.parts):
        raise SystemExit('refusing binary/game artifacts in source mount')
    log=stage/'native-test.log'
    cmd=['docker','--host','unix:///var/run/docker.sock','run','--rm','--pull=never','--network=none',
         '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true',
         '--user',f'{os.getuid()}:{os.getgid()}','--cpus=2','--memory=3g','--pids-limit=128','--ulimit','core=0:0',
         '--tmpfs','/tmp:rw,nosuid,nodev,size=128m',
         '--mount',f'type=bind,src={stage},dst=/repo',
         '--mount',f'type=bind,src={deps},dst=/deps,readonly','--workdir','/repo','--entrypoint','/bin/bash']
    if args.sanitizers: cmd += ['-e',f'GHOST_SANITIZERS={args.sanitizers}']
    cmd += [BUILDER,'tests/phase3/native-build.sh']
    started = time.monotonic()
    with log.open('w') as out:
        code=subprocess.run(cmd,stdout=out,stderr=subprocess.STDOUT).returncode
    log_bytes = log.read_bytes()
    summaries = re.findall(rb'Ran (\d+) tests in ([\d.]+)s', log_bytes)
    result=dict(version=2, builder=BUILDER, upstream_revision=REVISION,
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
