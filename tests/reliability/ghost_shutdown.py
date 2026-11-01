#!/usr/bin/env python3
"""Native shutdown regressions with synthetic W3GS peers, never a game engine.

Use an already prepared offline runtime as a read-only fixture. Each case gets
its own config, DB, log and replay directory. No public endpoint or account.
"""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/offline'))
from ghost_lifecycle import Peer, discover, pump


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_for(proc, log, marker, timeout=12):
    deadline = time.monotonic() + timeout
    while marker not in log.read_text(errors='replace'):
        assert proc.poll() is None, f'host exited {proc.returncode} before {marker}'
        assert time.monotonic() < deadline, f'timed out waiting for {marker}'
        time.sleep(0.01)


def snapshot(proc, runtime, label):
    """Save only this process's metadata; no machine-wide environment/secrets."""
    process = Path('/proc') / str(proc.pid)
    data = {'pid': proc.pid, 'returncode': proc.poll()}
    for name in ('status', 'task', 'fd'):
        path = process / name
        try:
            if name == 'status':
                data[name] = path.read_text()
            elif name == 'task':
                data['tids'] = sorted(int(p.name) for p in path.iterdir())
                data['children'] = (path / str(proc.pid) / 'children').read_text()
            else:
                data['fds'] = {p.name: os.readlink(p) for p in path.iterdir()}
        except FileNotFoundError:
            pass
    inodes = {value[8:-1] for value in data.get('fds', {}).values() if value.startswith('socket:[')}
    data['sockets'] = {}
    for kind in ('tcp', 'udp', 'tcp6', 'udp6'):
        lines = (process / 'net' / kind).read_text().splitlines()
        data['sockets'][kind] = [line for line in lines[1:] if line.split()[9] in inodes]
    (runtime / f'process-{label}.json').write_text(json.dumps(data, indent=2) + '\n')
    return data


def prepare(fixture, runtime, port, idle, discovery_address):
    runtime.mkdir(mode=0o700)
    for name in ('game', 'maps', 'mapcfgs'):
        shutil.copytree(fixture / name, runtime / name, symlinks=True)
    (runtime / 'replays').mkdir()
    shutil.copyfile(fixture / 'language.cfg', runtime / 'language.cfg')
    text = (fixture / 'ghost.cfg').read_text()
    # These are test-owned configs; never alter the supplied fixture or game.
    lines = [line for line in text.splitlines() if not line.startswith('bot_hostport =')]
    lines.append(f'bot_hostport = {port}')
    lines = [line for line in lines if not line.startswith('udp_broadcasttarget =')]
    lines.append(f'udp_broadcasttarget = {discovery_address}')
    if idle:
        lines = [line for line in lines if not line.startswith('autohost_maxgames =')]
        lines.append('autohost_maxgames = 0')
    (runtime / 'ghost.cfg').write_text('\n'.join(lines) + '\n')


def leave(peers):
    for peer in peers:
        peer.send(0x21, struct.pack('<I', 7))
        peer.sock.shutdown(socket.SHUT_WR)
    for peer in peers:
        peer.sock.settimeout(3)
        try:
            while peer.sock.recv(65536):
                pass
        except (ConnectionResetError, socket.timeout):
            pass
        peer.close()
    peers.clear()


def check_replay(runtime):
    replays = list((runtime / 'replays').glob('*.w3g'))
    assert len(replays) == 1, f'expected one saved replay: {replays}'
    data = replays[0].read_bytes()
    version, build = struct.unpack_from('<IH', data, 52)
    assert (version, build) == (26, 6401), (version, build)
    return {'bytes': len(data), 'version': version, 'build': build, 'sha256': digest(replays[0])}


def run_case(args, name, index):
    runtime = args.output / f'{index:03d}-{name}'
    port = args.port + index
    prepare(args.fixture_runtime, runtime, port, name == 'idle-storm', args.discovery_address)
    result = {'case': name, 'runtime': str(runtime), 'checks': []}
    peers = []
    proc = None
    started = time.monotonic()
    blocker = None
    log = runtime / 'stdout.log'
    env = dict(os.environ, LD_LIBRARY_PATH=str(args.binary.parent / 'lib'))
    # Full UBSan output is preserved. A recovering UBSan run is evidence of
    # legacy diagnostics, never a claim of a clean undefined-behavior result.
    env.setdefault('UBSAN_OPTIONS', 'print_stacktrace=1')
    env.setdefault('ASAN_OPTIONS', 'detect_leaks=1:halt_on_error=1')
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, log.open('w') as output:
            udp.bind((args.discovery_address, 6112)); udp.settimeout(0.25)
            if name == 'busy-port':
                blocker = socket.socket()
                blocker.bind(('127.0.0.1', port)); blocker.listen(1)
            proc = subprocess.Popen([str(args.binary), 'ghost.cfg'], cwd=runtime, env=env,
                                    stdout=output, stderr=subprocess.STDOUT)
            wait_for(proc, log, '[GHOST] offline initialization complete')
            result['checks'].append('cold start initialized real native server')
            if name == 'busy-port':
                wait_for(proc, log, f'error listening on port {port}', 45)
                assert '] listening on port ' + str(port) + '\n' not in log.read_text()
                proc.send_signal(signal.SIGTERM)
            elif name == 'idle-storm':
                # Deliver a burst while logging/cleanup may be in progress.
                for j in range(256):
                    if proc.poll() is not None:
                        break
                    proc.send_signal(signal.SIGTERM if j % 2 else signal.SIGINT)
                    time.sleep(0.0005)
            else:
                info = discover(udp, 45, port)
                assert info['port'] == port
                result['checks'].append('genuine lobby + loopback UDP advertisement')
                snap = snapshot(proc, runtime, 'lobby')
                assert len(snap['tids']) == 1 and snap['children'].strip() == ''
                if name == 'lobby':
                    proc.send_signal(signal.SIGTERM)
                else:
                    manifest = json.loads((args.fixture_runtime / 'input-manifest.json').read_text())
                    one = Peer(info, 'ReliabilityOne', download=True); peers.append(one)
                    pump(peers, lambda: one.mapsize is not None and len(one.mapdata) == one.mapsize, 15)
                    assert hashlib.sha256(one.mapdata).hexdigest() == manifest['map_sha256']
                    peers.append(Peer(info, 'ReliabilityTwo'))
                    pump(peers, lambda: all(p.loaded and p.counts[0x0C] >= 3 for p in peers), 45)
                    one.send(0x26, struct.pack('<I', zlib.crc32(b'')))
                    pump(peers, lambda: all(p.forwarded for p in peers), 5)
                    assert 'finished loading with 2 players' in log.read_text()
                    result['checks'].append('map SHA256 + loading + action relay with two synthetic peers')
                    snap = snapshot(proc, runtime, 'game')
                    assert len(snap['tids']) == 1 and snap['children'].strip() == ''
                    if name == 'normal':
                        leave(peers)
                        discover(udp, 40, port, after=info['counter'])
                        assert 'is over (no players left)' in log.read_text()
                        result['checks'].append('game ended, SQLite saved and next lobby created')
                        proc.send_signal(signal.SIGTERM)
                    else:
                        proc.send_signal(signal.SIGTERM)
                        wait_for(proc, log, 'exiting nicely')
                        until = time.monotonic() + 0.2
                        pump(peers, lambda: time.monotonic() >= until, 2)
                        assert proc.poll() is None, 'first signal aborted active match'
                        result['checks'].append('first signal kept active match alive')
                        if name == 'game-graceful':
                            leave(peers)
                        else:
                            for j in range(1 if name == 'game-force' else 256):
                                if proc.poll() is not None:
                                    break
                                proc.send_signal(signal.SIGINT if j % 2 == 0 else signal.SIGTERM)
                                time.sleep(0.0005)
                            result['checks'].append('repeated signal requested immediate game cleanup')
            assert proc.wait(timeout=12) == 0, f'exit code {proc.returncode}'
            text = log.read_text(errors='replace')
            assert '[GHOST] shutting down' in text
            assert text.count('[SQLITE3] closing database') == 2
            if name in ('normal', 'game-graceful'):
                assert 'all threads finished, exiting nicely' in text
            if name in ('game-force', 'game-storm'):
                assert 'exiting NOW' in text
            assert not Path(f'/proc/{proc.pid}').exists(), 'host not reaped'
            result['checks'].append('exit 0, both SQLite handles closed, child reaped')
            with sqlite3.connect(runtime / 'ghost.dbs') as db:
                assert db.execute('pragma integrity_check').fetchone() == ('ok',)
                result['database_games'] = db.execute('select count(*) from games').fetchone()[0]
                if name in ('normal', 'game-graceful'):
                    assert result['database_games'] == 1
                    assert db.execute('select count(*) from gameplayers').fetchone()[0] == 2
            if name.startswith('game-') or name == 'normal':
                result['replay'] = check_replay(runtime)
            diagnostics = [line for line in text.splitlines() if 'runtime error:' in line]
            result['ubsan_diagnostics'] = sorted(set(diagnostics))
            assert 'ERROR: AddressSanitizer' not in text and 'ERROR: LeakSanitizer' not in text
            result['result'] = 'PASS_WITH_UBSAN_DIAGNOSTICS' if diagnostics else 'PASS'
    except BaseException as exc:
        result.update(result='FAIL', error=f'{type(exc).__name__}: {exc}')
    finally:
        for peer in peers:
            peer.close()
        if proc is not None:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            result['exit_code'] = proc.returncode
        if blocker is not None:
            blocker.close()
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (runtime / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-runtime', required=True, type=Path)
    parser.add_argument('--binary', type=Path, default=ROOT / 'build/ghost/ghost++')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--port', type=int, default=26120)
    parser.add_argument('--discovery-address', default='127.0.0.2', help='private IPv4 loopback address for UDP discovery')
    parser.add_argument('--cases', nargs='+', default=['lobby', 'normal', 'game-graceful', 'game-force', 'game-storm', 'idle-storm', 'busy-port'],
                        choices=['lobby', 'normal', 'game-graceful', 'game-force', 'game-storm', 'idle-storm', 'busy-port'])
    args = parser.parse_args()
    if not ipaddress.IPv4Address(args.discovery_address).is_loopback:
        parser.error('discovery-address must be IPv4 loopback')
    for name in ('fixture_runtime', 'binary', 'output'):
        setattr(args, name, getattr(args, name).resolve())
    if args.rounds < 1 or not 1024 <= args.port <= 65535 - args.rounds * len(args.cases):
        parser.error('invalid rounds or unprivileged port range')
    args.output.mkdir(mode=0o700)
    manifest = json.loads((args.fixture_runtime / 'input-manifest.json').read_text())
    inputs = {str(Path(manifest['map'])): digest(Path(manifest['map'])),
              str(args.fixture_runtime / 'game/War3Patch.mpq'): digest(args.fixture_runtime / 'game/War3Patch.mpq')}
    assert list(inputs.values()) == [manifest['map_sha256'], manifest['patch_sha256']]
    summary = {'binary': str(args.binary), 'binary_sha256': digest(args.binary),
               'scope': 'real native server + synthetic peers; no game engine, no external endpoints',
               'patch_sha256': digest(ROOT / 'patches/ghostplusplus-modern-linux.patch'), 'cases': []}
    # UDP discovery uses legacy port 6112; serialize this harness's invocations.
    with open(f'/tmp/war3-reliability-{args.discovery_address}.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for _ in range(args.rounds):
            for name in args.cases:
                result = run_case(args, name, len(summary['cases']))
                summary['cases'].append(result)
                print(f"{result['result']}: {name} exit={result.get('exit_code')} ({result['elapsed_seconds']}s)", flush=True)
    summary['original_inputs_unchanged'] = all(digest(Path(path)) == sha for path, sha in inputs.items())
    failed = any(case['result'] == 'FAIL' for case in summary['cases']) or not summary['original_inputs_unchanged']
    diagnostics = any(case.get('ubsan_diagnostics') for case in summary['cases'])
    summary['result'] = 'FAIL' if failed else 'PASS_WITH_UBSAN_DIAGNOSTICS' if diagnostics else 'PASS'
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return int(failed or diagnostics)


if __name__ == '__main__':
    raise SystemExit(main())
