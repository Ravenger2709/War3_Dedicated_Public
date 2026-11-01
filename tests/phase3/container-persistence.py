#!/usr/bin/env python3
"""Real offline Docker stop/start against a pinned production image.

Example (run as uid 1000, with access to the local Docker daemon)::

    python3 tests/phase3/container-persistence.py --image sha256:<64 lowercase hex> --assets-root /path/to/main

Only synthetic credentials are generated. The production entrypoint and default
command are retained; this file is mounted read-only for in-container observers.
Receipts and persistent state remain in a new artifacts/phase3/persistence-agent-*
directory. No existing container, prefix, profile, or source file is modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen


DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
MARKER = 'synthetic-container-persistence-v1'


def save(path, value):
    with path.open('w') as output:
        json.dump(value, output, indent=2, allow_nan=False)
        output.write('\n')
    path.chmod(0o600)


def secure_artifacts(root):
    """Lock down retained vendor-created files without following Wine symlinks.

    Called only after container removal. Changing copied PE executable modes
    during startup would make the observer alter the behavior under test.
    """
    counts = {'directories_700': 0, 'files_600': 0, 'symlinks_skipped': 0}
    for directory, directories, files in os.walk(root, followlinks=False):
        path = Path(directory)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000:
            raise RuntimeError('ARTIFACT_DIRECTORY_NOT_OWNED')
        path.chmod(0o700)
        counts['directories_700'] += 1
        children = directories + files
        directories[:] = [n for n in directories if not (path / n).is_symlink()]
        for name in children:
            entry = path / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                counts['symlinks_skipped'] += 1
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    current = os.fstat(fd)
                    if current.st_uid != 1000 or current.st_nlink != 1:
                        raise RuntimeError('ARTIFACT_FILE_NOT_EXCLUSIVELY_OWNED')
                    os.fchmod(fd, 0o600)
                    counts['files_600'] += 1
                finally:
                    os.close(fd)
    return counts


def observe(phase):
    """All application checks execute inside the actual application container."""
    from src.runtime.processes import process_identity, snapshot

    result = {'phase': phase}
    sample = snapshot()
    result.update(sample)
    owner = Path('/data/prefix/.star-owner.json')
    if owner.exists():
        record = json.loads(owner.read_text())
        old = record.get('xvfb')
        result['recorded_xvfb'] = old
        result['current_recorded_pid'] = process_identity(old['pid']) if old else None
    worker = Path('/data/state/star-status.json')
    if worker.exists():
        data = json.loads(worker.read_text())
        result['star'] = {k: data.get(k) for k in (
            'phase', 'reason', 'exit_code', 'launcher_alive', 'forced_stop',
            'game_engine_observed', 'monitor_error_codes', 'container_init_observations')}
    try:
        with urlopen('http://127.0.0.1:8080/status', timeout=2) as response:
            data = json.load(response)
        result['status'] = {k: data.get(k) for k in (
            'state', 'reason', 'ready', 'iccup_authenticated', 'mode',
            'lobby_publication_accepted', 'components', 'star_process_alive',
            'native_healthy', 'relay_healthy', 'player_listener_available',
            'permanent_errors', 'game_engine_observed', 'component_restarts')}
    except (OSError, ValueError):
        result['status'] = None
    if phase in ('cold', 'warm'):
        db = Path('/data/sqlite/ghost.dbs')
        with sqlite3.connect(db, timeout=3) as connection:
            if phase == 'cold':
                connection.execute('CREATE TABLE persistence_agent_marker (value TEXT PRIMARY KEY)')
                connection.execute('INSERT INTO persistence_agent_marker VALUES (?)', (MARKER,))
            result['sqlite_marker'] = connection.execute(
                'SELECT value FROM persistence_agent_marker').fetchall() == [(MARKER,)]
            result['sqlite_integrity'] = connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        result['sqlite_inode'] = db.stat().st_ino
        result['directory_markers'] = {}
        for name in ('prefix', 'session'):
            marker = Path('/data') / name / '.container-persistence-marker'
            if phase == 'cold':
                marker.write_text(MARKER)
                marker.chmod(0o600)
            result['directory_markers'][name] = marker.read_text() == MARKER
        # Different supervisor/bridge directories must not bypass account flock.
        cfg = json.loads(Path('/data/config/supervisor.json').read_text())
        cfg['runtime_dir'] = '/data/state/duplicate-supervisor'
        cfg['iccup']['bridge_dir'] = '/data/state/duplicate-bridge'
        target = Path('/data/state/duplicate-config.json')
        save(target, cfg)
        duplicate = subprocess.run(
            [sys.executable, '-m', 'src.supervisor', '--config', str(target)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=12, check=False)
        result['duplicate_owner_exit'] = duplicate.returncode
        # Observe again after the competing supervisor has exited and been reaped.
        result.update(snapshot())
    print(json.dumps(result), flush=True)
    return 0


def host(args):
    started = time.monotonic()
    if os.geteuid() != 1000 or os.getegid() != 1000:
        raise ValueError('UID_GID_1000_REQUIRED')
    os.umask(0o077)
    assets = args.assets_root.absolute()
    # Resolve only explicit public inputs; never enumerate account/private trees.
    game = assets / 'Warcraft 3 Frozen Throne'
    sources = [assets / 'artifacts/phase2/star/updated-input',
               assets / 'artifacts/phase2/star/redist/app/Launcher/vc_redist.x86.exe',
               game / 'war3.exe', game / 'game.dll', game / 'War3Patch.mpq',
               game / 'maps/download/iCCup DotA 501.w3x']
    for source in sources:
        if source.is_symlink() or not source.exists():
            raise ValueError('PUBLIC_ASSET_MISSING_OR_SYMLINK')
    root = assets / 'artifacts/phase3' / ('persistence-agent-' + uuid.uuid4().hex[:12])
    root.mkdir(mode=0o700)
    for name in ('config', 'config/maps', 'state', 'sqlite', 'logs', 'replays',
                 'prefix', 'session', 'profile'):
        (root / name).mkdir(mode=0o700)
    repo = Path(__file__).resolve().parents[2]

    def write(name, value):
        path = root / name
        path.write_text(value)
        path.chmod(0o600)

    cfg = json.loads((repo / 'config/supervisor-iccup.example.json').read_text())
    cfg['mode'] = 'iccup'
    cfg['hostbot']['lobby_timeout'] = 7200
    cfg['iccup']['auth_timeout'] = 600
    cfg['gameplay']['game_name'] = 'Synthetic persistence QA'
    write('config/supervisor.json', json.dumps(cfg))
    write('config/ghost.cfg', (repo / 'docker/ghost.cfg').read_text().replace(
        'bot_lobbytimelimit = 10', 'bot_lobbytimelimit = 120'))
    write('config/maps/selected.cfg', 'map_path = Maps\\Download\\iCCup DotA 501.w3x\n'
          'map_localpath = selected.w3x\nmap_speed = 3\nmap_visibility = 4\n'
          'map_observers = 1\nmap_flags = 3\nmap_loadInGame = 0\n')
    servers = json.dumps('@ByteArray(' + json.dumps({'Offline QA': '127.0.0.1:16112'}) + ')')
    write('profile/launcher.ini', '[General]\nAutoLogin=false\nUsername="OfflineOnly"\n'
          'Password=\nListLogins=\nConnections=\nCurrConnections=0\n'
          'SiteConnections=' + servers + '\nUserConnections=' + servers + '\n')
    write('profile/iccup.ini', '[Common]\nAutoUpdate=0\nRunAtStart=0\n'
          '[Warcraft III]\nAutoLoginOn=0\n')
    profile_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (root / 'profile').iterdir()}
    mounts = [(root / name, '/data/' + name, name == 'config') for name in
              ('config', 'state', 'sqlite', 'logs', 'replays', 'prefix', 'session')]
    mounts += [(root / 'profile', '/vendor/profile', True),
               (sources[0], '/vendor/star', True),
               (sources[1], '/vendor/redist/vc_redist.x86.exe', True),
               (sources[2], '/vendor/game/war3.exe', True),
               (sources[3], '/vendor/game/game.dll', True),
               (sources[4], '/data/warcraft/War3Patch.mpq', True),
               (sources[5], '/data/maps/selected.w3x', True),
               (Path(__file__).resolve(), '/persistence-probe.py', True)]
    env = os.environ.copy()
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS',
                'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(key, None)
    name = 'war3-' + root.name
    created = False
    result = {'result': 'FAIL', 'image_id': args.image, 'checks': [],
              'network': 'none', 'synthetic_profile_only': True,
              'canonical_credentials_mounted': False, 'receipts': str(root)}

    def docker(*argv, timeout=25, check=True):
        completed = subprocess.run([*DOCKER, *argv], env=env, capture_output=True,
                                   text=True, timeout=timeout, check=False)
        if check and completed.returncode:
            raise RuntimeError('DOCKER_' + argv[0].upper() + '_FAILED')
        return completed

    def inspect():
        return json.loads(docker('inspect', name).stdout)[0]

    def bind_identity(meta):
        return sorted((m['Source'], m['Destination'], m['RW'])
                      for m in meta['Mounts'] if m['Type'] == 'bind')

    def probe(phase='sample'):
        response = docker('exec', name, 'python3', '/persistence-probe.py',
                          '--observe', phase, timeout=25)
        return json.loads(response.stdout)

    def safe(sample):
        s = sample.get('status')
        if sample['game_engine_observed'] or sample['monitor_error']:
            raise RuntimeError('ENGINE_OR_PROCESS_MONITOR_FAILURE')
        if s and (s['ready'] is not False or s['iccup_authenticated'] is not False
                  or s['lobby_publication_accepted'] is not False
                  or s['game_engine_observed']):
            raise RuntimeError('OFFLINE_AUTH_OR_ENGINE_VIOLATION')

    def healthy(sample):
        safe(sample)
        s = sample.get('status')
        return bool(s and not s['permanent_errors'] and s['mode'] == 'iccup'
                    and set(s['components']) == {'native', 'star', 'relay'}
                    and all(c['alive'] for c in s['components'].values())
                    and len({c['pid'] for c in s['components'].values()}) == 3
                    and all(s[k] for k in ('star_process_alive', 'native_healthy',
                                          'relay_healthy', 'player_listener_available')))

    def await_cohort(phase):
        end = min(started + 265, time.monotonic() + args.phase_timeout)
        last = None
        consecutive = 0
        while time.monotonic() < end:
            if not inspect()['State']['Running']:
                raise RuntimeError(phase.upper() + '_CONTAINER_EXITED')
            last = probe()
            save(root / (phase + '-latest.json'), last)
            if healthy(last):
                consecutive += 1
                if consecutive >= 3:
                    return last
            else:
                consecutive = 0
            if last.get('status') and last['status']['permanent_errors']:
                raise RuntimeError(phase.upper() + '_PERMANENT_RUNTIME_ERROR')
            time.sleep(1)
        raise RuntimeError(phase.upper() + '_COHORT_TIMEOUT')

    def verify_cohort(phase):
        await_cohort(phase)
        sample = probe(phase)
        save(root / (phase + '.json'), sample)
        if not healthy(sample):
            raise RuntimeError(phase.upper() + '_COHORT_UNHEALTHY')
        if sample['duplicate_owner_exit'] != 2:
            raise RuntimeError('DUPLICATE_PROFILE_OWNER_NOT_REJECTED')
        if not sample['sqlite_marker'] or not sample['sqlite_integrity']:
            raise RuntimeError('SQLITE_MARKER_OR_INTEGRITY_FAILED')
        if not all(sample['directory_markers'].values()):
            raise RuntimeError('PREFIX_OR_SESSION_MARKER_LOST')
        rows = sample['processes']
        if any(row['state'] in ('Z', 'X') for row in rows):
            raise RuntimeError('ZOMBIE_OR_DEAD_PROCESS')
        for comm in ('ghost++', 'Launcher.exe', 'Xvfb'):
            if sum(row['name'] == comm for row in rows) != 1:
                raise RuntimeError('DUPLICATE_OR_MISSING_VENDOR_PROCESS')
        result['checks'].append(phase + '_native_star_relay_alive_no_auth_no_engine_no_zombies')
        result['checks'].append(phase + '_duplicate_account_owner_rejected')
        return sample

    def stop(phase):
        # Record host process identities, so PID reuse cannot hide a leftover.
        top = docker('top', name, '-eo', 'pid', check=False)
        identities = {}
        if not top.returncode:
            for item in top.stdout.splitlines()[1:]:
                try:
                    pid = int(item.strip())
                    fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
                    identities[pid] = fields[19]
                except (OSError, ValueError, IndexError):
                    pass
        docker('stop', '--time', '60', name, timeout=70)
        state = inspect()['State']
        receipt = {k: state[k] for k in ('Running', 'Pid', 'ExitCode', 'OOMKilled',
                                        'StartedAt', 'FinishedAt')}
        remaining = []
        for pid, ticks in identities.items():
            try:
                fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
                if fields[19] == ticks:
                    remaining.append(pid)
            except (OSError, IndexError):
                pass
        receipt['old_namespace_processes_remaining'] = remaining
        receipt['old_namespace_processes_checked'] = len(identities)
        receipt['top_rejected_when_stopped'] = docker('top', name, check=False).returncode != 0
        terminal = json.loads((root / 'state/supervisor/status.json').read_text())
        worker = json.loads((root / 'state/star-status.json').read_text())
        receipt['supervisor'] = {k: terminal.get(k) for k in (
            'state', 'components', 'permanent_errors', 'game_engine_observed',
            'ready', 'iccup_authenticated')}
        receipt['star'] = {k: worker.get(k) for k in (
            'phase', 'reason', 'exit_code', 'launcher_alive', 'game_engine_observed',
            'monitor_error_codes')}
        save(root / (phase + '-stopped.json'), receipt)
        if state['Running'] or state['Pid'] or state['ExitCode'] or state['OOMKilled'] or remaining:
            raise RuntimeError('DOCKER_GRACEFUL_STOP_OR_PROCESS_CLEANUP_FAILED')
        if (not identities or not receipt['top_rejected_when_stopped']
                or terminal['state'] != 'STOPPED' or terminal['permanent_errors']
                or terminal['ready'] or terminal['iccup_authenticated']
                or terminal['game_engine_observed']
                or set(terminal['components']) != {'native', 'star', 'relay'}
                or any(c['alive'] or c['last_exit'] != 0 for c in terminal['components'].values())
                or worker['phase'] != 'STOPPED' or worker['exit_code'] != 0
                or worker['launcher_alive'] or worker['game_engine_observed']
                or worker.get('monitor_error_codes')):
            raise RuntimeError('TERMINAL_COMPONENT_EXIT_OR_MONITOR_FAILURE')
        result['checks'].append(phase + '_docker_sigterm_exit_zero_all_namespace_processes_gone')

    def interrupted(*_):
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        cmd = ['create', '--pull=never', '--name', name, '--network', 'none',
               '--read-only', '--user', '1000:1000', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', '--init',
               '--ulimit', 'core=0:0', '--memory', '2g', '--memory-swap', '2g',
               '--cpus', '2', '--pids-limit', '256', '--stop-timeout', '60',
               '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
               '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777', '--shm-size', '128m']
        for source, destination, ro in mounts:
            cmd += ['--mount', f'type=bind,src={source},dst={destination}' + (',readonly' if ro else '')]
        docker(*cmd, args.image)
        created = True
        meta = inspect()
        container_id = meta['Id']
        h = meta['HostConfig']
        bindings = {m['Destination']: m for m in meta['Mounts'] if m['Type'] == 'bind'}
        isolated = (meta['Image'] == args.image and h['NetworkMode'] == 'none'
                    and h['ReadonlyRootfs'] and h['CapDrop'] == ['ALL']
                    and 'no-new-privileges:true' in h['SecurityOpt']
                    and h['Init'] and not h['Privileged'] and not h.get('PidMode')
                    and not h.get('PortBindings') and meta['Config']['User'] == '1000:1000'
                    and h['Memory'] == h['MemorySwap'] == 2 * 1024**3
                    and h['NanoCpus'] == 2_000_000_000 and h['PidsLimit'] == 256
                    and any(u['Name'] == 'core' and u['Soft'] == u['Hard'] == 0 for u in h['Ulimits'])
                    and meta['Config']['Entrypoint'] == ['/opt/war3/entrypoint.sh']
                    and meta['Config']['Cmd'] == ['python3', '-m', 'src.supervisor', '--config', '/data/config/supervisor.json']
                    and len(bindings) == len(mounts)
                    and all(bindings[d]['Source'] == str(s) and bindings[d]['RW'] == (not ro)
                            for s, d, ro in mounts))
        save(root / 'isolation.json', {'passed': isolated, 'container_id': container_id,
             'image_id': meta['Image'], 'host_config': {k: h[k] for k in (
                 'NetworkMode', 'ReadonlyRootfs', 'CapDrop', 'SecurityOpt', 'Init',
                 'Memory', 'MemorySwap', 'NanoCpus', 'PidsLimit', 'Ulimits')},
             'mounts': [{'source': str(s), 'destination': d, 'read_only': ro} for s, d, ro in mounts],
             'synthetic_profile_only': True})
        if not isolated:
            raise RuntimeError('CONTAINER_ISOLATION_REJECTED')
        result['checks'].append('pinned_image_default_production_entrypoint_isolation')
        docker('start', name)
        cold = verify_cohort('cold')
        stop('cold')
        before = {n: (root / n).stat().st_ino for n in ('prefix', 'session', 'sqlite')}
        owner = json.loads((root / 'prefix/.star-owner.json').read_text())
        save(root / 'stopped-prefix-record.json', {k: owner.get(k) for k in ('version', 'uid', 'device', 'inode', 'xvfb', 'launcher')})
        if (root / 'sqlite/ghost.dbs').stat().st_ino != cold['sqlite_inode']:
            raise RuntimeError('STOP_REPLACED_SQLITE')
        docker('start', name)
        warm = verify_cohort('warm')
        after_meta = inspect()
        save(root / 'warm-container-identity.json', {
            'initial_id': container_id, 'warm_id': after_meta['Id'],
            'initial_binds': bind_identity(meta), 'warm_binds': bind_identity(after_meta)})
        if after_meta['Id'] != container_id or bind_identity(after_meta) != bind_identity(meta):
            raise RuntimeError('CONTAINER_OR_MOUNTS_CHANGED')
        if warm['sqlite_inode'] != cold['sqlite_inode'] or any(
                (root / n).stat().st_ino != inode for n, inode in before.items()):
            raise RuntimeError('PERSISTENT_INODE_CHANGED')
        if profile_hashes != {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (root / 'profile').iterdir()}:
            raise RuntimeError('SYNTHETIC_PROFILE_CHANGED')
        result['checks'].append('same_container_profile_prefix_session_sqlite_inodes_and_markers_persist')
        stop('warm')
        result['result'] = 'PASS'
    except (Exception, KeyboardInterrupt) as error:
        result['reason'] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        if created:
            try:
                if inspect()['State']['Running']:
                    save(root / 'failure-sample.json', probe())
            except Exception:
                pass
    finally:
        if created:
            try:
                if inspect()['State']['Running']:
                    stop('cleanup')
                docker('rm', name)
                result['container_removed'] = True
            except Exception:
                result['container_removed'] = False
                result['result'] = 'FAIL'
                result.setdefault('reason', 'CONTAINER_CLEANUP_FAILED')
        if not created or result.get('container_removed'):
            try:
                result['artifact_modes'] = secure_artifacts(root)
            except Exception as error:
                result['result'] = 'FAIL'
                result.setdefault('reason', str(error) if isinstance(error, RuntimeError)
                                  else 'ARTIFACT_PERMISSION_FAILED')
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        save(root / 'result.json', result)
        print(json.dumps(result), flush=True)
    return 0 if result['result'] == 'PASS' else 1


def pinned_image(value):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise argparse.ArgumentTypeError('an exact local sha256 image ID is required')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=pinned_image, help='required host-run local image ID; never a tag or approval gate')
    parser.add_argument('--assets-root', type=Path)
    parser.add_argument('--phase-timeout', type=int, default=130)
    parser.add_argument('--observe', choices=('sample', 'cold', 'warm'), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.observe:
        return observe(args.observe)
    if args.image is None or args.assets_root is None or not 10 <= args.phase_timeout <= 240:
        parser.error('--image, --assets-root and a phase timeout between 10 and 240 seconds are required')
    return host(args)


if __name__ == '__main__':
    raise SystemExit(main())
