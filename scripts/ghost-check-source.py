#!/usr/bin/env python3
"""Reject source changes beyond our exact pinned patch without resetting files."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def check(source):
    lock = json.loads((ROOT / 'third_party/ghost.lock.json').read_text())
    patch = ROOT / lock['patches'][0]
    if hashlib.sha256(patch.read_bytes()).hexdigest() != lock['patch_sha256'][lock['patches'][0]]:
        raise ValueError('compatibility patch differs from lock SHA256')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), '-c', 'core.abbrev=7',
                                        '-c', 'diff.context=3', *args])
    if git('rev-parse', 'HEAD').decode().strip() != lock['revision']:
        raise ValueError('upstream revision differs from lock')
    diff = git('diff', '--no-ext-diff', '--binary', '--no-color', '--no-renames',
               '--src-prefix=a/', '--dst-prefix=b/', 'HEAD', '--')
    if diff != patch.read_bytes():
        raise ValueError('unexpected tracked source changes (exact patch comparison failed)')
    tracked = set(git('ls-files', '-z').decode().split('\0'))
    products = {'ghost/ghost++', 'bncsutil/src/bncsutil/libbncsutil.so', 'StormLib/stormlib/libStorm.so'}
    for path in git('ls-files', '--others', '-z').decode().split('\0'):
        if not path or path in products:
            continue
        if path.endswith('.o') and (path[:-2] + '.c' in tracked or path[:-2] + '.cpp' in tracked):
            continue
        raise ValueError('unexpected untracked source/build input: ' + path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    try:
        check(parser.parse_args().source.resolve())
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
    print('PASS: pinned source + exact declared patch, no unexpected inputs')
