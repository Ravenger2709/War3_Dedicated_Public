"""Network-none container tests. Synthetic files/processes only; no vendor execution."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
import contextlib

from src.runtime import processes as proc
from src.runtime import star


class Clock:
    def __init__(self):
        self.now = 10.0
        self.on_sleep = lambda: None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


class FakeProcess:
    def __init__(self, runner, pid, argv, delay=0.4):
        self.runner, self.pid, self.argv = runner, pid, argv
        self.ready = runner.clock() + delay
        self.code = None
        self.waited = False

    def poll(self):
        if self.code is not None:
            return self.code
        if self.argv[0] == 'Xvfb':
            return None
        if self.argv == ['wine', 'Launcher.exe']:
            if self.runner.crash_after is not None and self.runner.clock() >= self.ready + self.runner.crash_after:
                self.code = 23
            return self.code
        if self.runner.hang_boot and self.argv[0] == 'wineboot':
            return None
        if self.runner.clock() < self.ready:
            return None
        self.code = 0
        if self.argv == ['wine', str(self.runner.config.quit_helper)]:
            self.code = self.runner.helper_exit
            if self.code == 0 and self.runner.launcher:
                self.runner.launcher.code = 0
        if self.argv == ['wineserver', '-k'] and self.runner.launcher:
            self.runner.launcher.code = -9
        return self.code

    def wait(self, timeout=None):
        value = self.poll()
        if value is None:
            raise subprocess.TimeoutExpired('synthetic', timeout)
        self.waited = True
        return value

    def kill(self):
        self.code = -9


class FakeRunner:
    def __init__(self, config, clock):
        self.config, self.clock = config, clock
        self.children = []
        self.calls = []
        self.launcher = None
        self.crash_after = None
        self.hang_boot = False
        self.helper_exit = 0
        self.registry_before_profile = []

    def spawn(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        child = FakeProcess(self, 700000 + len(self.children), argv)
        self.children.append(child)
        if argv[0] == 'wineboot':
            (self.config.prefix / 'system.reg').write_text('#arch=win32\n')
        if argv[:2] == ['wine', 'reg']:
            self.registry_before_profile.append(not (self.config.session / 'launcher/launcher.ini').exists())
        if argv[:2] == ['wine', str(self.config.redist)]:
            target = self.config.prefix / 'drive_c/windows/system32'
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name in star.VC_NAMES:
                (target / name).write_bytes(b'synthetic native runtime ' + name.encode())
        if argv == ['wine', 'Launcher.exe']:
            self.launcher = child
        return child

    def identity(self, pid):
        return {'pid': pid, 'pgid': pid, 'start_ticks': 42}

    def stop_group(self, identity, timeout=2):
        if identity:
            for child in self.children:
                if child.pid == identity['pid']:
                    child.code = -15
        return True

    def observe(self):
        rows = []
        if self.launcher and self.launcher.poll() is None:
            rows.append({'pid': self.launcher.pid, 'pgid': self.launcher.pid,
                         'ppid': os.getpid(), 'name': 'Launcher.exe', 'state': 'S', 'start_ticks': 42})
        return {'processes': rows, 'game_engine_observed': False,
                'renderer_count': 0, 'monitor_error': False}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('vendor', 'profile', 'state'):
            (self.root / name).mkdir(mode=0o700)
        self.rows = []
        for name in sorted(star.VENDOR_NAMES):
            data = b'SYNTHETIC_NOT_PE ' + name.encode()
            path = self.root / 'vendor' / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            self.rows.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'version': 1, 'files': self.rows}))
        (self.root / 'vendor/input-manifest.json').write_text(json.dumps(self.rows))
        launcher_hash = next(row['sha256'] for row in self.rows if row['path'] == 'Launcher.exe')
        self.redist = self.root / 'redist.exe'
        self.redist.write_bytes(b'SYNTHETIC_NOT_PE REDIST')
        self.helper = self.root / 'quit.exe'
        self.helper.write_bytes(b'SYNTHETIC_NOT_PE QUIT')
        self.addCleanup(patch.stopall)
        patch.object(star, 'LAUNCHER_SHA256', launcher_hash).start()
        patch.object(star, 'REDIST_SHA256', hashlib.sha256(self.redist.read_bytes()).hexdigest()).start()
        user = json.dumps('@ByteArray(' + json.dumps({'Synthetic': '127.0.0.1:16112'}) + ')')
        launcher = '[General]\nAutoLogin=true\nUsername="SyntheticUser"\nPassword=\nListLogins="synthetic-private-cache"\nConnections=\nCurrConnections=0\nSiteConnections=' + user + '\nUserConnections=' + user + '\n'
        for name, text in [('launcher.ini', launcher), ('iccup.ini', '[Common]\nAutoUpdate=1\nAutoUpdateMap=1\nRunAtStart=1\n[Warcraft III]\nMainExe=unsafe.exe\nAutoLoginOn=0\n')]:
            path = self.root / 'profile' / name
            path.write_text(text)
            path.chmod(0o600)
        self.config = star.Settings(vendor=self.root / 'vendor', profile=self.root / 'profile',
                                    redist=self.redist, game_metadata=Path('/fixtures/game'),
                                    prefix=self.root / 'prefix', session=self.root / 'session',
                                    status=self.root / 'state/star-status.json', manifest=self.manifest,
                                    quit_helper=self.helper, relay_port=16112, startup_timeout=180, generation=7)
        self.clock = Clock()
        self.runner = FakeRunner(self.config, self.clock)
        self.worker = star.StarWorker(self.config, runner=self.runner, observer=self.runner.observe,
                                      clock=self.clock, sleep=self.clock.sleep,
                                      identity=self.runner.identity, group_stop=self.runner.stop_group)
        patch.object(proc, 'stop_group', self.runner.stop_group).start()
        self.documents = []
        original = star.atomic_json

        def capture(path, document):
            original(path, document)
            if path == self.config.status:
                self.documents.append(json.loads(path.read_text()))
        patch.object(star, 'atomic_json', capture).start()

    def run_worker(self, seconds=5):
        started = [None]

        def progress():
            if self.worker.status['phase'] == 'RUNNING':
                if started[0] is None:
                    started[0] = self.clock()
                if self.clock() - started[0] >= seconds:
                    self.worker.request_stop()
        self.clock.on_sleep = progress
        code = self.worker.run()
        return code, json.loads(self.config.status.read_text())


class ConfigTests(Fixture):
    def config_file(self, **changes):
        data = {key: str(value) if isinstance(value, Path) else value
                for key, value in self.config.__dict__.items()}
        data.update(version=1)
        data.update(changes)
        path = self.root / 'state/worker.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def test_exact_config_schema(self):
        self.assertEqual(star.load_config(self.config_file()), self.config)
        for changes in ({'generation': True}, {'version': 2}, {'startup_timeout': 0},
                        {'assume_auth': True}, {'vendor': '/tmp/../secret'}, {'session': str(self.config.prefix)}):
            with self.subTest(changes=changes), self.assertRaisesRegex(star.StarError, '^CONFIG_REJECTED$'):
                star.load_config(self.config_file(**changes))

    def test_config_mode_and_symlinks_rejected(self):
        path = self.config_file()
        path.chmod(0o644)
        with self.assertRaises(star.StarError):
            star.load_config(path)
        path.chmod(0o600)
        link = self.root / 'link.json'
        link.symlink_to(path)
        with self.assertRaises(star.StarError):
            star.load_config(link)
        link_dir = self.root / 'linked'
        link_dir.symlink_to(self.root / 'state', target_is_directory=True)
        with self.assertRaises(star.StarError):
            star.load_config(link_dir / 'worker.json')

    def test_duplicate_json_keys_rejected(self):
        path = self.config_file()
        path.write_text(path.read_text().replace('"version": 1', '"version": 1, "version": 1'))
        with self.assertRaises(star.StarError):
            star.load_config(path)


class WorkerTests(Fixture):
    def test_persistent_beyond_research_limit_and_private_heartbeat(self):
        code, status = self.run_worker(seconds=240)
        self.assertEqual((code, status['phase'], status['reason']), (0, 'STOPPED', 'SIGNAL_STOP'))
        self.assertEqual(status['normal_close_helper_exit'], 0)
        self.assertFalse(status['forced_stop'])
        running = [row for row in self.documents if row['phase'] == 'RUNNING']
        self.assertGreater(running[-1]['observed_monotonic_seconds'] - running[0]['observed_monotonic_seconds'], 240)
        self.assertTrue(all(row['launcher_alive'] for row in running[:-1]))
        gaps = [b['observed_monotonic_seconds'] - a['observed_monotonic_seconds']
                for a, b in zip(self.documents, self.documents[1:])]
        self.assertLessEqual(max(gaps), 1.0)
        for row in self.documents:
            self.assertEqual(row['generation'], 7)
            self.assertNotIn('authenticated', row)
            self.assertNotIn('SyntheticUser', json.dumps(row))
            self.assertNotIn('synthetic-private-cache', json.dumps(row))
        self.assertEqual(stat.S_IMODE(self.config.status.stat().st_mode), 0o600)
        self.assertTrue(all(child.waited for child in self.runner.children))

    def test_monitor_failure_codes_survive_healthy_shutdown_samples(self):
        injected=False
        original=self.worker.observer
        def observe():
            nonlocal injected
            sample=original();sample['monitor_error_codes']=[]
            if not injected and any(row['phase']=='RUNNING' for row in self.documents):
                injected=True;sample['monitor_error']=True
                sample['monitor_error_codes']=['PROCESS_METADATA_PERMISSION']
            return sample
        self.worker.observer=observe
        code,status=self.run_worker()
        self.assertTrue(injected);self.assertEqual(code,70)
        self.assertEqual(status['reason'],'PROCESS_MONITOR_FAILED')
        self.assertEqual(status['monitor_error_codes'],['PROCESS_METADATA_PERMISSION'])

    def test_copy_rewrite_registry_and_environment(self):
        with patch.dict(os.environ, {'PASSWORD': 'NEVER-INHERIT', 'QTWEBENGINE_DISABLE_SANDBOX': '1'}):
            self.assertEqual(self.run_worker()[0], 0)
        copied = self.config.session / 'launcher'
        contents = (copied / 'iccup.ini').read_text()
        for expected in ('AutoUpdate=0', 'AutoUpdateMap=0', 'RunAtStart=0', 'AutoLoginOn=1',
                         'MainExe=Z:/fixtures/game/war3.exe'):
            self.assertIn(expected, contents)
        self.assertEqual((copied / 'launcher.ini').read_bytes(), (self.config.profile / 'launcher.ini').read_bytes())
        self.assertNotIn('AutoUpdate=0', (self.config.profile / 'iccup.ini').read_text())
        self.assertTrue(all(self.runner.registry_before_profile))
        for argv, kwargs in self.runner.calls:
            env = kwargs['env']
            self.assertNotIn('PASSWORD', env)
            self.assertNotIn('QTWEBENGINE_DISABLE_SANDBOX', env)
            self.assertEqual(env['WINEPREFIX'], str(self.config.prefix))
            self.assertEqual(env['WINEARCH'], 'win32')
            self.assertEqual(env['WINEDEBUG'], '-all')
            self.assertNotIn('synthetic-private-cache', repr((argv, kwargs)))
        launch = next(call for call in self.runner.calls if call[0] == ['wine', 'Launcher.exe'])
        self.assertEqual(launch[1]['cwd'], copied)
        self.assertIn('vcruntime140,msvcp140,concrt140=n,b', launch[1]['env']['WINEDLLOVERRIDES'])
        xvfb = next(call[0] for call in self.runner.calls if call[0][0] == 'Xvfb')
        self.assertIn('-nolisten', xvfb)
        self.assertEqual(xvfb[xvfb.index('-nolisten') + 1], 'tcp')
        self.assertEqual(stat.S_IMODE((copied / 'launcher.ini').stat().st_mode), 0o600)

    def test_native_runtime_installed_once_and_prefix_preserved(self):
        self.assertEqual(self.run_worker()[0], 0)
        marker = self.config.prefix / '.star-vc2015.json'
        before = marker.read_bytes()
        sentinel = self.config.prefix / 'persistent-sentinel'
        sentinel.write_text('retained')
        first_installs = sum(argv[:2] == ['wine', str(self.redist)] for argv, _ in self.runner.calls)
        self.worker = star.StarWorker(self.config, self.runner, self.runner.observe, self.clock,
                                      self.clock.sleep, self.runner.identity, self.runner.stop_group)
        self.assertEqual(self.run_worker()[0], 0)
        installs = sum(argv[:2] == ['wine', str(self.redist)] for argv, _ in self.runner.calls)
        self.assertEqual((first_installs, installs), (1, 1))
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(sentinel.read_text(), 'retained')
        self.assertIn(['wineboot', '--restart'], [argv for argv, _ in self.runner.calls])

    def test_tampered_installed_dll_rejected(self):
        self.assertEqual(self.run_worker()[0], 0)
        (self.config.prefix / 'drive_c/windows/system32/vcruntime140.dll').write_bytes(b'damaged')
        self.worker = star.StarWorker(self.config, self.runner, self.runner.observe, self.clock,
                                      self.clock.sleep, self.runner.identity, self.runner.stop_group)
        code, status = self.run_worker()
        self.assertNotEqual(code, 0)
        self.assertEqual(status['reason'], 'VC_MARKER_REJECTED')
        self.assertEqual(sum(argv == ['wine', 'Launcher.exe'] for argv, _ in self.runner.calls), 1)

    def test_launcher_crash_exits_nonzero_without_retry(self):
        self.runner.crash_after = 4
        code, status = self.run_worker(seconds=100)
        self.assertNotEqual(code, 0)
        self.assertEqual((status['phase'], status['reason']), ('ERROR', 'LAUNCHER_EXITED'))
        self.assertEqual(sum(argv == ['wine', 'Launcher.exe'] for argv, _ in self.runner.calls), 1)

    def test_bounded_bootstrap(self):
        self.runner.hang_boot = True
        self.worker.config = replace(self.config, startup_timeout=2)
        code, status = self.run_worker()
        self.assertNotEqual(code, 0)
        self.assertEqual(status['reason'], 'STARTUP_TIMEOUT')
        self.assertLess(self.clock(), 20)
        self.assertTrue(all(child.waited for child in self.runner.children))

    def test_unobserved_launcher_does_not_become_running(self):
        self.worker.config = replace(self.config, startup_timeout=15)
        self.worker.observer = lambda: dict(self.runner.observe(), processes=[])
        code, status = self.run_worker()
        self.assertNotEqual(code, 0)
        self.assertEqual(status['reason'], 'STARTUP_TIMEOUT')
        self.assertFalse(any(row['phase'] == 'RUNNING' for row in self.documents))

    def test_engine_observation_fails_closed(self):
        def observe():
            return dict(self.runner.observe(), game_engine_observed=self.worker.status['phase'] == 'RUNNING')
        self.worker.observer = observe
        code, status = self.run_worker()
        self.assertNotEqual(code, 0)
        self.assertEqual(status['reason'], 'GAME_ENGINE_OBSERVED')
        self.assertTrue(status['game_engine_observed'])

    def test_renderer_loss_is_separate_from_launcher_and_auth(self):
        def observe():
            sample = self.runner.observe()
            sample['renderer_count'] = int(self.runner.launcher is not None and self.clock() < 25)
            return sample
        self.worker.observer = observe
        self.assertEqual(self.run_worker(seconds=100)[0], 0)
        self.assertTrue(any(row['phase'] == 'RUNNING' and row['launcher_alive']
                            and row['renderer_health'] == 'LOST' for row in self.documents))

    def test_helper_failure_records_forced_stop(self):
        self.runner.helper_exit = 70
        code, status = self.run_worker()
        self.assertEqual(code, 0)
        self.assertEqual(status['normal_close_helper_exit'], 70)
        self.assertTrue(status['forced_stop'])
        self.assertEqual(status['reason'], 'SIGNAL_STOP_FORCED')

    def test_corrupted_vendor_before_wine(self):
        (self.config.vendor / 'Qt5Core.dll').write_bytes(b'bad')
        code, _ = self.run_worker()
        self.assertNotEqual(code, 0)
        self.assertFalse(any(argv[0] in ('wine', 'wineboot', 'Xvfb') for argv, _ in self.runner.calls))

    def test_engine_input_and_symlink_rejected(self):
        (self.config.vendor / 'war3.exe').write_bytes(b'synthetic forbidden')
        self.assertNotEqual(self.run_worker()[0], 0)
        self.assertFalse(any(argv[0] == 'wineboot' for argv, _ in self.runner.calls))

    def test_profile_mode_and_parser_errors_never_leak(self):
        profile = self.config.profile / 'launcher.ini'
        profile.write_text('private-parser-data-without-section')
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code, status = self.run_worker()
        self.assertNotEqual(code, 0)
        self.assertNotIn('private-parser-data', stdout.getvalue() + stderr.getvalue() + json.dumps(status))
        self.assertFalse(any(argv[0] == 'wineboot' for argv, _ in self.runner.calls))

    def test_writable_metadata_rejected(self):
        game = self.root / 'writable-game'
        game.mkdir()
        for name in ('war3.exe', 'game.dll'):
            (game / name).write_bytes(b'synthetic')
        self.worker.config = replace(self.config, game_metadata=game)
        code, status = self.run_worker()
        self.assertNotEqual(code, 0)
        self.assertEqual(status['reason'], 'GAME_METADATA_NOT_READ_ONLY')

    def test_live_worker_lock_does_not_overwrite_status(self):
        proc.private_directory(self.config.prefix, create=True)
        self.config.status.write_text('{"existing":"sentinel"}')
        self.config.status.chmod(0o600)
        with proc.OwnedLock(self.config.prefix):
            code = self.worker.run()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.config.status.read_text(), '{"existing":"sentinel"}')
        self.assertFalse(self.runner.calls)

    def test_unsafe_vendor_log_fails_closed_and_still_cleans_prefix(self):
        def observe():
            if self.worker.status['phase'] == 'RUNNING':
                log = self.config.session / 'launcher/debug.log'
                if not log.is_symlink():
                    log.symlink_to(self.config.profile / 'launcher.ini')
            return self.runner.observe()
        self.worker.observer = observe
        code, status = self.run_worker(seconds=100)
        self.assertNotEqual(code, 0)
        self.assertEqual(status['reason'], 'LOG_BUDGET_FAILED')
        self.assertEqual(status['phase'], 'ERROR')
        self.assertIn(['wineserver', '-k'], [argv for argv, _ in self.runner.calls])
        self.assertTrue(all(child.waited for child in self.runner.children))
        self.assertIn('synthetic-private-cache', (self.config.profile / 'launcher.ini').read_text())


class ProcessTests(unittest.TestCase):
    def test_pinned_manifest_inventory(self):
        path = Path(__file__).resolve().parents[2] / 'config/star-vendor-files.json'
        manifest = json.loads(path.read_text())
        self.assertEqual(manifest['version'], 1)
        self.assertEqual(len(manifest['files']), 31)
        self.assertEqual({row['path'] for row in manifest['files']}, star.VENDOR_NAMES)
        self.assertEqual(next(row['sha256'] for row in manifest['files'] if row['path'] == 'Launcher.exe'), star.LAUNCHER_SHA256)

    def test_runner_vendor_output_is_discarded(self):
        with patch.object(subprocess, 'Popen') as popen:
            star.Runner().spawn(['wine', 'Launcher.exe'], env={}, cwd='/tmp')
        self.assertEqual(popen.call_args.kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertTrue(popen.call_args.kwargs['close_fds'])

    def test_stat_and_maps_only_detect_game_and_renderer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def process(pid, name, maps=''):
                path = root / str(pid)
                path.mkdir(exist_ok=True)
                fields = ['S', '1', str(pid)] + ['0'] * 16 + ['42']
                (path / 'stat').write_text(f'{pid} ({name}) ' + ' '.join(fields))
                (path / 'maps').write_text(maps)
                # A read of either secret-bearing file would fail decoding.
                for forbidden in ('cmdline', 'environ', 'mem'):
                    (path / forbidden).write_bytes(b'\xffNEVER_READ')
            process(1, 'Launcher.exe')
            process(2, 'QtWebEngineProc')
            self.assertFalse(proc.snapshot(root)['game_engine_observed'])
            self.assertEqual(proc.snapshot(root)['renderer_count'], 1)
            process(1, 'Launcher.exe', '1000-2000 r-xp 0000 00:00 1 /secret/path/Game.DLL (deleted)\n')
            self.assertTrue(proc.snapshot(root)['game_engine_observed'])
            for name in ('Warcraft III.ex', 'war3.exe', 'WarcraftIII.exe'):
                process(1, name)
                self.assertTrue(proc.snapshot(root)['game_engine_observed'])
            self.assertNotIn('secret', json.dumps(proc.snapshot(root)))

    def test_non_utf8_mapping_paths_still_detect_engine_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);entry=root/'10';entry.mkdir()
            fields=['S','1','10']+['0']*16+['42']
            (entry/'stat').write_text('10 (Launcher.exe) '+' '.join(fields))
            for module,expected in ((b'kernel32.dll',False),(b'Game.DLL',True)):
                (entry/'maps').write_bytes(b'1000-2000 r-xp 0000 00:00 1 /private/\xff/'+module+b'\n')
                result=proc.snapshot(root)
                self.assertFalse(result['monitor_error']);self.assertEqual(result['game_engine_observed'],expected)
                self.assertNotIn('private',json.dumps(result))

    def test_process_exit_between_stat_and_maps_is_rechecked(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);entry=root/'10';entry.mkdir()
            live={'pid':10,'name':'Launcher.exe','state':'S','ppid':1,'pgid':10,'start_ticks':42}
            dead={**live,'state':'Z'}
            original=Path.open
            def denied(path,*args,**kwargs):
                if path==entry/'maps':raise PermissionError()
                return original(path,*args,**kwargs)
            with patch.object(proc,'process_stat',side_effect=[live,dead]),patch.object(Path,'open',denied):
                self.assertFalse(proc.snapshot(root)['monitor_error'])
            with patch.object(proc,'process_stat',return_value=live),patch.object(Path,'open',denied):
                result=proc.snapshot(root)
                self.assertTrue(result['monitor_error']);self.assertEqual(result['monitor_error_codes'],['PROCESS_METADATA_PERMISSION'])

    def test_container_runtime_init_does_not_disable_application_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);entry=root/'10';entry.mkdir()
            original=Path.open
            def denied(path,*args,**kwargs):
                if path==entry/'maps':raise PermissionError()
                return original(path,*args,**kwargs)
            for name,parent,group,allowed in (('runc:[2:INIT]',0,10,True),('runc:[2:INIT]',1,10,False),('runc:[2:INIT]',0,1,False),('Launcher.exe',0,10,False),('war3.exe',0,10,False)):
                row={'pid':10,'name':name,'state':'R','ppid':parent,'pgid':group,'start_ticks':42}
                with patch.object(proc,'process_stat',return_value=row),patch.object(Path,'open',denied):
                    result=proc.snapshot(root)
                    self.assertEqual(result['monitor_error'],not allowed)
                    self.assertEqual(result['container_initializations'],int(allowed))
                    if name=='war3.exe':self.assertTrue(result['game_engine_observed'])

    def test_runtime_init_exemption_never_reuses_a_previous_attempt_row(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);entry=root/'10';entry.mkdir()
            row={'pid':10,'name':'runc:[2:INIT]','state':'R','ppid':0,'pgid':10,'start_ticks':42}
            original=Path.open
            def failed_map(path,*args,**kwargs):
                if path==entry/'maps':raise OSError(5,'synthetic maps failure')
                return original(path,*args,**kwargs)
            with patch.object(proc,'process_stat',side_effect=[row,PermissionError()]),patch.object(Path,'open',failed_map):
                result=proc.snapshot(root)
                self.assertTrue(result['monitor_error']);self.assertEqual(result['container_initializations'],0)

    def test_atomic_status_refuses_symlink_and_is_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'status'
            proc.atomic_json(path, {'version': 1})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            target = root / 'target'
            target.write_text('unchanged')
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(proc.ProcessError):
                proc.atomic_json(path, {'version': 2})
            self.assertEqual(target.read_text(), 'unchanged')

    def test_orphan_cleanup_only_marked_unlocked_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp)
            with patch.object(proc.subprocess, 'run') as run:
                self.assertFalse(proc.stop_owned_prefix(prefix)['cleaned'])
                run.assert_not_called()
            info = prefix.stat()
            proc.atomic_json(prefix / '.star-owner.json', {'version': 1, 'uid': os.geteuid(),
                             'device': info.st_dev, 'inode': info.st_ino, 'xvfb': None, 'launcher': None})
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0)
            with proc.OwnedLock(prefix):
                self.assertFalse(proc.stop_owned_prefix(prefix)['cleaned'])
                result = proc._stop_locked_prefix(prefix, run=run)
            self.assertTrue(result['cleaned'])
            self.assertEqual([argv for argv, _ in calls], [['wineserver', '-k'], ['wineserver', '-w']])
            self.assertTrue(all(kwargs['env']['WINEPREFIX'] == str(prefix) for _, kwargs in calls))
            self.assertTrue(all(kwargs['stderr'] == subprocess.DEVNULL for _, kwargs in calls))

    def test_pid_reuse_never_kills_group(self):
        with patch.object(proc, 'process_identity', return_value={'pid': 999, 'pgid': 999, 'start_ticks': 9}), patch.object(proc.os, 'killpg') as kill:
            self.assertFalse(proc.stop_group({'pid': 999, 'pgid': 999, 'start_ticks': 8}))
            kill.assert_not_called()

    def test_already_stopped_wineserver_is_idempotent_but_wait_must_succeed(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp)
            info = prefix.stat()
            proc.atomic_json(prefix / '.star-owner.json', {'version': 1, 'uid': os.geteuid(),
                             'device': info.st_dev, 'inode': info.st_ino, 'xvfb': None, 'launcher': None})
            for kill_code, wait_code, expected in ((1, 0, True), (1, 1, False), (2, 0, False)):
                def run(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, kill_code if argv[-1] == '-k' else wait_code)
                with proc.OwnedLock(prefix):
                    result = proc._stop_locked_prefix(prefix, run=run)
                self.assertEqual(result['cleaned'], expected)

    def test_private_log_rotation_preserves_open_writer_and_three_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'messages.log'
            budget = proc.PrivateLogBudget((root,), file_limit=1024, archives=3, directory_budget=8192)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                inode = os.fstat(fd).st_ino
                for index in range(5):
                    os.write(fd, bytes([65 + index]) * 1500)
                    budget.enforce()
                    self.assertEqual(path.stat().st_ino, inode)
                    self.assertEqual(path.stat().st_size, 0)
                self.assertEqual({p.name for p in root.iterdir()},
                                 {'messages.log', 'messages.log.1', 'messages.log.2', 'messages.log.3'})
                self.assertEqual((root / 'messages.log.1').stat().st_size, 1024)
                self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in root.iterdir()))
                os.write(fd, b'writer still attached')
                self.assertEqual(path.stat().st_size, 21)
            finally:
                os.close(fd)

    def test_combined_log_budget_and_dump_removal_without_reading_dump(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(8):
                (root / f'{index}.log').write_bytes(b'x' * 900)
            (root / 'crash.dmp').write_bytes(b'NEVER_INSPECT_DUMP')
            (root / 'debug.log.99').write_bytes(b'old archive')
            budget = proc.PrivateLogBudget((root,), file_limit=1024, archives=3, directory_budget=2500)
            with patch.object(proc.os, 'read', side_effect=AssertionError('no payload read')):
                budget.enforce()
            self.assertFalse((root / 'crash.dmp').exists())
            self.assertFalse((root / 'debug.log.99').exists())
            self.assertLessEqual(sum(p.stat().st_size for p in root.iterdir()), 2500)

    def test_log_rotation_never_follows_file_or_prefix_directory_symlink(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            secret = Path(outside) / 'messages.log'
            secret.write_bytes(b'unchanged private outside')
            (root / 'dosdevices').mkdir()
            (root / 'dosdevices/z:').symlink_to(Path(outside), target_is_directory=True)
            budget = proc.PrivateLogBudget((root,), file_limit=4, directory_budget=10)
            budget.enforce()
            self.assertEqual(secret.read_bytes(), b'unchanged private outside')
            (root / 'debug.log').symlink_to(secret)
            with self.assertRaises(OSError):
                budget.enforce()
            self.assertEqual(secret.read_bytes(), b'unchanged private outside')

    def test_sigkill_releases_worker_lock_and_cleanup_reaps_owned_group(self):
        # Real Linux processes, but neither Wine nor any PE/GUI/network is run.
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp)
            owner_code = """
import os,sys,time
from pathlib import Path
from src.runtime.processes import OwnedLock
lock=OwnedLock(Path(sys.argv[1]))
print('ready',flush=True)
time.sleep(60)
"""
            owner = subprocess.Popen([sys.executable, '-c', owner_code, str(prefix)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, start_new_session=True)
            group = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'ready')
                info = prefix.stat()
                proc.atomic_json(prefix / '.star-owner.json', {'version': 1, 'uid': os.geteuid(),
                                 'device': info.st_dev, 'inode': info.st_ino,
                                 'xvfb': proc.process_identity(group.pid), 'launcher': None})
                self.assertFalse(proc.stop_owned_prefix(prefix)['cleaned'])
                owner.kill()
                owner.wait(timeout=3)
                calls = []
                def fake_wineserver(argv, **kwargs):
                    calls.append((argv, kwargs['env']))
                    return subprocess.CompletedProcess(argv, 0)
                with patch.object(proc.subprocess, 'run', side_effect=fake_wineserver):
                    result = proc.stop_owned_prefix(prefix, timeout=2)
                self.assertTrue(result['cleaned'])
                self.assertEqual(group.wait(timeout=3), -signal.SIGTERM)
                self.assertEqual([argv for argv, _ in calls], [['wineserver', '-k'], ['wineserver', '-w']])
                self.assertTrue(all(env['WINEPREFIX'] == str(prefix) for _, env in calls))
            finally:
                for child in (owner, group):
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=3)
                owner.stdout.close()


if __name__ == '__main__':
    unittest.main()
