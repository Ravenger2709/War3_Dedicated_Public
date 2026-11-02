#!/usr/bin/env python3
"""Guard regression test in a disposable local clone; never alter main checkout."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def run():
    source = Path(tempfile.mkdtemp(prefix='ghost-guard-', dir=ROOT / 'vendor'))
    try:
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(ROOT / 'vendor/ghostplusplus'), str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'apply', str(ROOT / 'patches/ghostplusplus-modern-linux.patch')], check=True)
        subprocess.run(['git', '-C', str(source), 'add', '-N', '--', 'ghost/native_sha256.h'], check=True)
        guard = ['python3', str(ROOT / 'scripts/ghost-check-source.py'), str(source)]
        assert subprocess.run(guard, capture_output=True).returncode == 0
        injected = source / 'ghost/injected.hpp'
        injected.write_text('// unexpected include\n')
        assert subprocess.run(guard, capture_output=True).returncode != 0
        injected.unlink()
        original = source / 'ghost/ghost.cpp'
        original.write_bytes(original.read_bytes() + b'\n// unexpected source edit\n')
        assert subprocess.run(guard, capture_output=True).returncode != 0
        print('PASS: exact patch accepted; unexpected untracked header and tracked source edit rejected')
    finally:
        shutil.rmtree(source)


if __name__ == '__main__':
    run()
