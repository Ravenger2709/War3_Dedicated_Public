"""Persistent original-Star service. Authentication belongs exclusively to Star/relay."""
import argparse
import configparser
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import resource
import shutil
import signal
import stat
import subprocess
import time

from .processes import (OwnedLock, PrivateLogBudget, _stop_locked_prefix, atomic_json,
                        checked_path, private_directory, process_identity,
                        read_file, snapshot, stop_group, guard_parent)
from src.bridge.star_relay import profile_username, require_loopback_profile

LAUNCHER_SHA256 = '9ce99d541aaef4cb1b2a60a0837580678f81ed0494795efc720fe84c99a6253e'
REDIST_SHA256 = 'fdd1e1f0dcae2d0aa0720895eff33b927d13076e64464bb7c7e5843b7667cd14'
VENDOR_NAMES = frozenset((
    'Launcher.exe', 'QtWebEngineProcess.exe', 'iccwc3.icc', 'libeay32.dll',
    'ssleay32.dll', 'RepAnalyser.dll', 'Storm.dll', 'w3lh.dll',
    'chaticons.zip', 'flags.zip', 'mapicons.zip',
    'Qt5Core.dll', 'Qt5Gui.dll', 'Qt5Multimedia.dll', 'Qt5Network.dll',
    'Qt5Positioning.dll', 'Qt5Qml.dll', 'Qt5Quick.dll', 'Qt5Svg.dll',
    'Qt5WebChannel.dll', 'Qt5WebEngineCore.dll', 'Qt5WebEngineWidgets.dll',
    'Qt5Widgets.dll', 'Qt5Xml.dll', 'imageformats/qgif.dll',
    'imageformats/qsvg.dll', 'platforms/qwindows.dll', 'resources/icudtl.dat',
    'resources/qtwebengine_resources.pak', 'resources/qtwebengine_resources_100p.pak',
    'resources/qtwebengine_resources_200p.pak'))
VC_NAMES = ('vcruntime140.dll', 'msvcp140.dll', 'concrt140.dll')


class StarError(ValueError):
    """Only fixed public reason codes may be used as messages."""


class StopRequested(Exception):
    pass


@dataclass(frozen=True, repr=False)
class Settings:
    vendor: Path
    profile: Path
    redist: Path
    game_metadata: Path
    prefix: Path
    session: Path
    status: Path
    manifest: Path
    quit_helper: Path
    relay_port: int
    startup_timeout: int
    generation: int


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise StarError('CONFIG_REJECTED')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def load_config(path):
    """Exact version-1 schema. Paths may vary for CI; no runtime test switches."""
    try:
        obj = _json(read_file(path, 16384, private=True))
        fields = set(Settings.__dataclass_fields__)
        if not isinstance(obj, dict) or set(obj) != fields | {'version'}:
            raise StarError('CONFIG_REJECTED')
        version = obj.pop('version')
        if type(version) is not int or version != 1:
            raise StarError('CONFIG_REJECTED')
        for name in fields - {'relay_port', 'startup_timeout', 'generation'}:
            value = obj[name]
            if not isinstance(value, str) or not value or '\x00' in value:
                raise StarError('CONFIG_REJECTED')
            obj[name] = checked_path(value)
        for name, low, high in (('relay_port', 1, 65535), ('startup_timeout', 1, 3600),
                                ('generation', 1, 2**63 - 1)):
            if type(obj[name]) is not int or not low <= obj[name] <= high:
                raise StarError('CONFIG_REJECTED')
        # Writable destinations cannot alias vendor/profile/metadata or one another.
        roots = [obj[k] for k in ('vendor', 'profile', 'game_metadata', 'prefix', 'session')]
        if any(a == b or a in b.parents or b in a.parents
               for index, a in enumerate(roots) for b in roots[index + 1:]):
            raise StarError('CONFIG_REJECTED')
        if any(obj['status'] == root or root in obj['status'].parents for root in roots):
            raise StarError('CONFIG_REJECTED')
        return Settings(**obj)
    except Exception:
        raise StarError('CONFIG_REJECTED') from None


class Runner:
    def spawn(self, argv, **kwargs):
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                close_fds=True, preexec_fn=partial(guard_parent, os.getpid()), **kwargs)


class StarWorker:
    """Subprocess/clock injection is internal Python API, never configuration."""
    def __init__(self, settings, runner=None, observer=snapshot, clock=time.monotonic,
                 sleep=time.sleep, identity=process_identity, group_stop=stop_group):
        self.config = settings
        self.runner = runner or Runner()
        self.observer, self.clock, self.sleep = observer, clock, sleep
        self.identity, self.group_stop = identity, group_stop
        self.stopping = False
        self.launcher = None
        self.xvfb = None
        self.active_commands = []
        self.deadline = None
        self.last_heartbeat = -float('inf')
        self.last_sample = None
        self.prefix_owned = False
        self.record = None
        self.status = {'version': 1, 'worker_pid': os.getpid(), 'phase': 'STARTING',
                       'launcher_pid': None, 'launcher_alive': False,
                       'game_engine_observed': False, 'observed_monotonic_seconds': 0,
                       'generation': settings.generation, 'exit_code': None,
                       'normal_close_helper_exit': None, 'forced_stop': False,
                       'reason': 'STARTUP', 'renderer_count': 0,
                       'renderer_health': 'NOT_OBSERVED'}
        self.renderer_seen = False
        self.renderer_lost = False
        self.log_budget = None

    def request_stop(self, *_):
        self.stopping = True

    def heartbeat(self, force=False, observe=True, enforce_logs=True):
        now = self.clock()
        if not force and now - self.last_heartbeat < 0.5:
            return
        if enforce_logs and self.log_budget is not None:
            try:
                self.log_budget.enforce()
            except (OSError, ValueError):
                raise StarError('LOG_BUDGET_FAILED') from None
        if observe:
            sample = self.observer()
            self.last_sample = sample
            self.status['monitor_error_codes'] = sorted(set(self.status.get('monitor_error_codes',[])) | set(sample.get('monitor_error_codes',[])))
            self.status['container_init_observations'] = self.status.get('container_init_observations',0)+sample.get('container_initializations',0)
            self.status['game_engine_observed'] |= sample['game_engine_observed']
            self.status['observed_monotonic_seconds'] = now
            self.status['renderer_count'] = sample['renderer_count']
            if sample['renderer_count']:
                self.renderer_seen = True
            elif self.renderer_seen:
                self.renderer_lost = True
            self.status['renderer_health'] = ('LOST' if self.renderer_lost else
                                              'PRESENT' if sample['renderer_count'] else 'NOT_OBSERVED')
            if self.launcher is not None:
                rows = sample['processes']
                owned = {self.launcher.pid}
                while True:
                    expanded = owned | {p['pid'] for p in rows if p['ppid'] in owned}
                    if expanded == owned:
                        break
                    owned = expanded
                launchers = [p for p in rows if p['name'].lower() == 'launcher.exe'
                             and p['state'] not in ('Z', 'X')
                             and (p['pid'] in owned or p['pgid'] == self.launcher.pid)]
                alive = self.launcher.poll() is None and bool(launchers)
                self.status['launcher_alive'] = alive
                if alive:
                    self.status['launcher_pid'] = launchers[0]['pid']
            if sample['monitor_error']:
                raise StarError('PROCESS_MONITOR_FAILED')
        atomic_json(self.config.status, self.status)
        self.last_heartbeat = now

    def check(self, bootstrap=True):
        self.heartbeat()
        if self.status['game_engine_observed']:
            raise StarError('GAME_ENGINE_OBSERVED')
        if self.stopping:
            raise StopRequested()
        if bootstrap and self.deadline is not None and self.clock() >= self.deadline:
            raise StarError('STARTUP_TIMEOUT')
        if self.xvfb is not None and self.xvfb.poll() is not None:
            raise StarError('XVFB_EXITED')

    def environment(self):
        # Deliberately do not inherit arbitrary variables, credentials or Qt bypasses.
        return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                'HOME': str(self.config.session / 'home'), 'DISPLAY': ':99',
                'TMPDIR': str(self.config.session / 'tmp'),
                'WINEPREFIX': str(self.config.prefix), 'WINEARCH': 'win32',
                'WINEDEBUG': '-all',
                'WINEDLLOVERRIDES': 'winemenubuilder.exe=d;mscoree,mshtml=d;vcruntime140,msvcp140,concrt140=n,b',
                'LIBGL_ALWAYS_SOFTWARE': '1', 'QT_OPENGL': 'software',
                'QTWEBENGINE_CHROMIUM_FLAGS': '--disable-gpu'}

    def command(self, argv, timeout, bootstrap=True, stopping=False, env=None, **_):
        command = self.runner.spawn(argv, env=env or self.environment(), cwd=self.config.session)
        self.active_commands.append(command)
        deadline = self.clock() + timeout
        try:
            while command.poll() is None:
                if stopping:
                    self.shutdown_heartbeat()
                else:
                    self.check(bootstrap)
                if self.clock() >= deadline:
                    raise subprocess.TimeoutExpired(argv[0], timeout)
                self.sleep(0.2)
            return subprocess.CompletedProcess(argv[0], command.wait(timeout=0))
        finally:
            if command.poll() is None:
                self.group_stop(self.identity(command.pid), timeout=0)
                command.kill()
            command.wait(timeout=2)
            self.active_commands.remove(command)

    def shutdown_heartbeat(self):
        # Unsafe logs or inaccessible maps must never prevent prefix cleanup.
        try:
            self.heartbeat()
        except (OSError, ValueError):
            try:
                self.heartbeat(force=True, observe=False, enforce_logs=False)
            except (OSError, ValueError):
                pass

    def digest(self, path, expected_size=None):
        path = checked_path(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (expected_size is not None and info.st_size != expected_size)):
                raise StarError('INPUT_REJECTED')
            sha = hashlib.sha256()
            size = 0
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                while chunk := stream.read(1024 * 1024):
                    sha.update(chunk)
                    size += len(chunk)
                    self.check()
            after = os.fstat(fd)
            if size != info.st_size or (info.st_mtime_ns, info.st_size) != (after.st_mtime_ns, after.st_size):
                raise StarError('INPUT_CHANGED')
            return sha.hexdigest(), size
        finally:
            os.close(fd)

    def validate_inputs(self):
        try:
            manifest = _json(read_file(self.config.manifest, 32768))
            if (not isinstance(manifest, dict) or set(manifest) != {'version', 'files'}
                    or type(manifest['version']) is not int or manifest['version'] != 1
                    or not isinstance(manifest['files'], list) or len(manifest['files']) != 31):
                raise StarError('VENDOR_MANIFEST_REJECTED')
            rows = {}
            for row in manifest['files']:
                if (not isinstance(row, dict) or set(row) != {'path', 'bytes', 'sha256'}
                        or row['path'] not in VENDOR_NAMES or row['path'] in rows
                        or type(row['bytes']) is not int or not 0 < row['bytes'] <= 128 * 1024**2
                        or not isinstance(row['sha256'], str) or len(row['sha256']) != 64
                        or any(c not in '0123456789abcdef' for c in row['sha256'])):
                    raise StarError('VENDOR_MANIFEST_REJECTED')
                rows[row['path']] = row
            if set(rows) != VENDOR_NAMES or rows['Launcher.exe']['sha256'] != LAUNCHER_SHA256:
                raise StarError('VENDOR_MANIFEST_REJECTED')
            checked_path(self.config.vendor)
            directories = {str(parent) for name in VENDOR_NAMES for parent in PurePosixPath(name).parents
                           if str(parent) != '.'}
            for root, dirs, files in os.walk(self.config.vendor, followlinks=False):
                self.check()
                for name in dirs + files:
                    path = Path(root) / name
                    checked_path(path)
                    relative = path.relative_to(self.config.vendor).as_posix()
                    if path.is_dir():
                        if relative not in directories:
                            raise StarError('VENDOR_INPUT_REJECTED')
                    elif relative not in VENDOR_NAMES and relative != 'input-manifest.json':
                        raise StarError('VENDOR_INPUT_REJECTED')
            inventory = self.config.vendor / 'input-manifest.json'
            if inventory.exists() and _json(read_file(inventory, 32768)) != manifest['files']:
                raise StarError('VENDOR_MANIFEST_REJECTED')
            for name, row in rows.items():
                if self.digest(self.config.vendor / name, row['bytes'])[0] != row['sha256']:
                    raise StarError('VENDOR_HASH_MISMATCH')
            if self.digest(self.config.redist)[0] != REDIST_SHA256:
                raise StarError('REDIST_HASH_MISMATCH')
            # Metadata PEs are only checked for regular files on a read-only mount.
            # Neither is copied, loaded, launched or hashed into account diagnostics.
            for name in ('war3.exe', 'game.dll'):
                path = checked_path(self.config.game_metadata / name)
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or not os.statvfs(path).f_flag & os.ST_RDONLY:
                    raise StarError('GAME_METADATA_NOT_READ_ONLY')
            helper = checked_path(self.config.quit_helper).lstat()
            if not stat.S_ISREG(helper.st_mode):
                raise StarError('QUIT_HELPER_REJECTED')
            private_directory(self.config.profile)
            for name in ('launcher.ini', 'iccup.ini'):
                read_file(self.config.profile / name, 16384, private=True)
            require_loopback_profile(self.config.profile / 'launcher.ini', self.config.relay_port)
            profile_username(self.config.profile / 'launcher.ini')  # validation only; never retained/logged
            return rows
        except StarError:
            raise
        except Exception:
            raise StarError('INPUT_REJECTED') from None

    def prepare_vendor(self, rows):
        target = self.config.session / 'launcher'
        if target.exists():
            private_directory(target)
            # Do not follow a leftover vendor-created link during reconstruction.
            for root, dirs, files in os.walk(target, followlinks=False):
                for name in dirs + files:
                    checked_path(Path(root) / name)
            shutil.rmtree(target)
        target.mkdir(mode=0o700)
        for name, row in rows.items():
            self.check()
            source = checked_path(self.config.vendor / name)
            output = target / name
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Revalidate the copied bytes before any Wine process starts.
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            try:
                with os.fdopen(source_fd, 'rb', closefd=False) as stream, output.open('xb') as dest:
                    os.chmod(output, 0o600)
                    while chunk := stream.read(1024 * 1024):
                        dest.write(chunk)
                        self.check()
            finally:
                os.close(source_fd)
            if self.digest(output, row['bytes'])[0] != row['sha256']:
                raise StarError('VENDOR_HASH_MISMATCH')

    def provision_profile(self):
        # Called only after all debugger/dump suppression registry writes succeed.
        try:
            require_loopback_profile(self.config.profile / 'launcher.ini', self.config.relay_port)
            profile_username(self.config.profile / 'launcher.ini')
            launcher = read_file(self.config.profile / 'launcher.ini', 16384, private=True)
            cfg = configparser.RawConfigParser()
            cfg.optionxform = str
            cfg.read_string(read_file(self.config.profile / 'iccup.ini', 16384, private=True).decode('utf-8'))
            for section in ('Common', 'Warcraft III'):
                if not cfg.has_section(section):
                    cfg.add_section(section)
            for key in ('AutoUpdate', 'AutoUpdateMap', 'RunAtStart'):
                cfg.set('Common', key, '0')
            cfg.set('Warcraft III', 'MainExe', 'Z:' + str(self.config.game_metadata / 'war3.exe'))
            cfg.set('Warcraft III', 'AutoLoginOn', '1')
            output = io.StringIO()
            cfg.write(output, space_around_delimiters=False)
            for name, data in (('launcher.ini', launcher), ('iccup.ini', output.getvalue().encode('utf-8'))):
                path = checked_path(self.config.session / 'launcher' / name)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(fd, 'wb') as dest:
                    dest.write(data)
        except Exception:
            raise StarError('PROFILE_REJECTED') from None

    def setup_prefix(self):
        existing = checked_path(self.config.prefix / 'system.reg').exists()
        if self.command(['wineboot', '--restart' if existing else '--init'], 75).returncode:
            raise StarError('PREFIX_BOOT_FAILED')
        registry = [
            (r'HKCU\Software\Wine\WineDbg', 'ShowCrashDialog', 'REG_DWORD', '0'),
            (r'HKLM\Software\Microsoft\Windows NT\CurrentVersion\AeDebug', 'Debugger', 'REG_SZ', 'cmd /c exit 1'),
            (r'HKLM\Software\Microsoft\Windows NT\CurrentVersion\AeDebug', 'Auto', 'REG_SZ', '0'),
            (r'HKCU\Software\Microsoft\Windows\Windows Error Reporting', 'Disabled', 'REG_DWORD', '1'),
            (r'HKCU\Software\Microsoft\Windows\Windows Error Reporting', 'DontShowUI', 'REG_DWORD', '1')]
        for key, name, kind, value in registry:
            if self.command(['wine', 'reg', 'add', key, '/v', name, '/t', kind, '/d', value, '/f'], 15).returncode:
                raise StarError('DEBUGGER_SUPPRESSION_FAILED')
        marker = checked_path(self.config.prefix / '.star-vc2015.json')
        if marker.exists():
            try:
                installed = _json(read_file(marker, 8192, private=True))
                if (set(installed) != {'version', 'redist_sha256', 'arch', 'dlls'}
                        or installed['version'] != 1 or installed['redist_sha256'] != REDIST_SHA256
                        or installed['arch'] != 'win32' or set(installed['dlls']) != set(VC_NAMES)):
                    raise StarError('VC_MARKER_REJECTED')
                for name, digest in installed['dlls'].items():
                    if self.digest(self.config.prefix / 'drive_c/windows/system32' / name)[0] != digest:
                        raise StarError('VC_MARKER_REJECTED')
                return
            except Exception:
                raise StarError('VC_MARKER_REJECTED') from None
        result = self.command(['wine', str(self.config.redist), '/install', '/quiet', '/norestart'], 120)
        if result.returncode not in (0, 3010):
            raise StarError('VC_INSTALL_FAILED')
        dlls = {name: self.digest(self.config.prefix / 'drive_c/windows/system32' / name)[0] for name in VC_NAMES}
        atomic_json(marker, {'version': 1, 'redist_sha256': REDIST_SHA256, 'arch': 'win32', 'dlls': dlls})

    def start(self):
        self.deadline = self.clock() + self.config.startup_timeout
        self.check()
        if any(p['name'].lower() == 'launcher.exe' and p['state'] not in ('Z', 'X')
               for p in self.last_sample['processes']):
            raise StarError('STARTUP_PROCESS_CONFLICT')
        rows = self.validate_inputs()
        self.prepare_vendor(rows)
        for name in ('home', 'tmp'):
            private_directory(self.config.session / name, create=True)
        self.xvfb = self.runner.spawn(['Xvfb', ':99', '-screen', '0', '1280x1024x24',
                                      '-nolisten', 'tcp', '-noreset'],
                                     env=self.environment(), cwd=self.config.session)
        self.record['xvfb'] = self.identity(self.xvfb.pid)
        if self.record['xvfb'] is None:
            raise StarError('XVFB_IDENTITY_FAILED')
        atomic_json(self.config.prefix / '.star-owner.json', self.record)
        while True:
            self.check()
            try:
                if not self.command(['xdpyinfo', '-display', ':99'], 2).returncode:
                    break
            except subprocess.TimeoutExpired:
                pass
            self.sleep(0.2)
        self.setup_prefix()
        self.provision_profile()
        self.launcher = self.runner.spawn(['wine', 'Launcher.exe'], env=self.environment(),
                                          cwd=self.config.session / 'launcher')
        self.record['launcher'] = self.identity(self.launcher.pid)
        atomic_json(self.config.prefix / '.star-owner.json', self.record)
        while not self.status['launcher_alive']:
            self.check()
            if self.launcher.poll() is not None:
                raise StarError('LAUNCHER_EXITED')
            self.sleep(0.2)
        self.status.update(phase='RUNNING', reason='PROCESS_RUNNING')
        self.heartbeat(force=True)
        self.deadline = None  # No session-duration/authentication deadline.

    def shutdown(self, graceful):
        self.deadline = None
        if not self.prefix_owned:
            return True
        if graceful and self.launcher is not None and self.launcher.poll() is None:
            try:
                helper = self.command(['wine', str(self.config.quit_helper)], 12, stopping=True)
                self.status['normal_close_helper_exit'] = helper.returncode
            except (OSError, ValueError, subprocess.SubprocessError):
                self.status['normal_close_helper_exit'] = None
            deadline = self.clock() + 2
            while self.launcher.poll() is None and self.clock() < deadline:
                self.shutdown_heartbeat()
                self.sleep(0.2)
        self.status['forced_stop'] = bool(self.launcher is not None and self.launcher.poll() is None)
        # Uses the already-held prefix lock. All Wine traffic is scoped by WINEPREFIX.
        result = _stop_locked_prefix(self.config.prefix, timeout=12,
                                     run=lambda argv, **kwargs: self.command(argv, stopping=True, **kwargs))
        for child in (self.launcher, self.xvfb):
            if child is None:
                continue
            try:
                if child.poll() is None:
                    self.status['forced_stop'] = True
                    self.group_stop(self.identity(child.pid), timeout=0)
                    child.kill()
                child.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                result['cleaned'] = False
        self.status['launcher_alive'] = False
        return result['cleaned']

    def run(self):
        exit_code = 70
        reason = 'WORKER_FAILED'
        with ExitStack() as stack:
            try:
                private_directory(self.config.status.parent)
                private_directory(self.config.prefix, create=True)
                private_directory(self.config.session, create=True)
                stack.enter_context(OwnedLock(self.config.prefix))
                stack.enter_context(OwnedLock(self.config.session))
                owner = self.config.prefix / '.star-owner.json'
                if owner.exists():
                    recovered = _stop_locked_prefix(self.config.prefix, timeout=12,
                                                    run=lambda argv, **kwargs: self.command(argv, stopping=True, **kwargs))
                    if not recovered['cleaned']:
                        raise StarError('PREFIX_RECOVERY_FAILED')
                elif any(p.name != '.star-worker.lock' for p in self.config.prefix.iterdir()):
                    raise StarError('PREFIX_NOT_OWNED')
                info = self.config.prefix.stat()
                self.record = {'version': 1, 'uid': os.geteuid(), 'device': info.st_dev,
                               'inode': info.st_ino, 'xvfb': None, 'launcher': None}
                atomic_json(owner, self.record)
                self.prefix_owned = True
                self.log_budget = PrivateLogBudget((self.config.prefix, self.config.session))
                self.start()
                while True:
                    self.check(bootstrap=False)
                    if self.launcher.poll() is not None or not self.status['launcher_alive']:
                        raise StarError('LAUNCHER_EXITED')
                    self.sleep(0.2)
            except StopRequested:
                exit_code, reason = 0, 'SIGNAL_STOP'
            except StarError as error:
                reason = str(error)
            except (OSError, ValueError, subprocess.SubprocessError):
                reason = 'WORKER_FAILED'
            finally:
                try:
                    clean = self.shutdown(graceful=exit_code == 0)
                except Exception:
                    clean = False
                if not clean:
                    exit_code, reason = 70, 'SHUTDOWN_FAILED'
                elif self.status['game_engine_observed']:
                    exit_code, reason = 70, 'GAME_ENGINE_OBSERVED'
                elif exit_code == 0 and self.status['forced_stop']:
                    reason = 'SIGNAL_STOP_FORCED'
                self.status.update(phase='STOPPED' if exit_code == 0 else 'ERROR',
                                   reason=reason, exit_code=exit_code)
                if self.prefix_owned:
                    try:
                        self.heartbeat(force=True)
                    except Exception:
                        # A monitoring error must still allow fixed-code terminal status.
                        try:
                            self.heartbeat(force=True, observe=False, enforce_logs=False)
                        except Exception:
                            exit_code = 70
                    if self.status['game_engine_observed'] and exit_code == 0:
                        exit_code = 70
                        self.status.update(phase='ERROR', reason='GAME_ENGINE_OBSERVED', exit_code=70)
                        try:
                            self.heartbeat(force=True, observe=False, enforce_logs=False)
                        except Exception:
                            pass
        return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        config = load_config(args.config)
        if os.geteuid() == 0:
            raise StarError('NON_ROOT_REQUIRED')
    except Exception:
        # Configuration/profile parser diagnostics can contain secrets.
        print('{"event":"STAR_WORKER_REJECTED"}')
        return 78
    worker = StarWorker(config)
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    return worker.run()


if __name__ == '__main__':
    raise SystemExit(main())
