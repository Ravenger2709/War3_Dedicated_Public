import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from src.runtime.cycle_coordinator import CycleCoordinator
from src.runtime.cycle_state import CycleState
from src.runtime.lifecycle import scan
from src.runtime.native_session import NativeSessionReader, NativeSessionError, _parse
from src.runtime.process_ownership import OwnedChild, ProcessOwnershipError, inspect_saved_identity
from src.runtime.reservation_marker import publish
from src.runtime.session_lease import SessionLease, SessionLeaseError
from src.runtime.writer_observation import inspect_recorded_writer, WriterObservationError
from src.runtime.journal_audit import audit, JournalAuditError


class ManagedSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'session'
        self.lease = SessionLease.create(self.root, enabled=True)
        self.children = []

    def tearDown(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdout.close()
        self.lease.close()
        self.temp.cleanup()

    def spawn(self, updates=None):
        script = '''import json, os, sys, time
info = os.stat(sys.argv[1])
obj = {'version': 1, 'pid': os.getpid(), 'session_generation': 'a'*32,
       'journal_device': info.st_dev, 'journal_inode': info.st_ino}
obj.update(json.loads(sys.argv[2]))
os.write(1, b'boot\\n[HOSTBOT_SESSION] ' + json.dumps(obj).encode() + b'\\n')
while True: time.sleep(1)
'''
        p = subprocess.Popen([sys.executable, '-u', '-c', script, str(self.lease.journal),
                              json.dumps(updates or {})], stdout=subprocess.PIPE, bufsize=0)
        self.children.append(p)
        return p

    def reader(self, p):
        owner = OwnedChild(p)
        self.addCleanup(owner.close)
        reader = NativeSessionReader(owner, self.lease, os.path.realpath(sys.executable))
        self.addCleanup(reader.close)
        return owner, reader

    def test_catalog_session_v2_is_strict_and_requires_ipc_descriptor(self):
        base={'version':2,'pid':123,'session_generation':'a'*32,
              'journal_device':1,'journal_inode':2,'ipc_device':3,'ipc_inode':4}
        encoded=lambda v: b'[HOSTBOT_SESSION] '+json.dumps(v).encode('ascii')
        self.assertEqual(_parse(encoded(base)),base)
        for change in ({'version':1},{'ipc_inode':0},{'ipc_device':True},
                       {'ipc_inode':-1},{'ipc_device':'3'},{'extra':1}):
            with self.subTest(change=change), self.assertRaises(NativeSessionError):
                _parse(encoded({**base,**change}))
        for key in ('ipc_device','ipc_inode'):
            value=dict(base);del value[key]
            with self.subTest(missing=key), self.assertRaises(NativeSessionError):
                _parse(encoded(value))
        p=self.spawn({'version':2,'ipc_device':3,'ipc_inode':4})
        _,reader=self.reader(p)
        with self.assertRaisesRegex(NativeSessionError,'IPC descriptor required'):
            reader.read_and_persist()
        self.assertEqual(list(self.lease.journal.iterdir()),[])

    def test_feature_flag_is_required_without_creating_root(self):
        other = self.root.with_name('disabled')
        for setting in (False, None, 1, 'true'):
            with self.assertRaises(SessionLeaseError):
                SessionLease.create(other, enabled=setting)
            self.assertFalse(other.exists())
        with self.assertRaises(SessionLeaseError):
            SessionLease(self.root)

    def test_binding_is_from_owned_pipe_and_survives_restart(self):
        p = self.spawn()
        owner, reader = self.reader(p)
        binding = reader.read_and_persist()
        self.assertEqual(binding['version'], 2)
        self.assertEqual(binding['native']['pid_namespace_inode'], os.stat(f'/proc/{p.pid}/ns/pid').st_ino)
        self.assertEqual(binding['native']['boot_id'], owner.identity.boot_id)
        self.assertEqual(binding['native']['start_ticks'], owner.identity.start_ticks)
        self.assertEqual(inspect_recorded_writer(self.lease, 'a'*32), 'running')
        self.assertEqual(binding['session']['pid'], p.pid)
        self.assertEqual(binding['process']['start_ticks'], owner.identity.start_ticks)
        path = self.lease.journal / ('session-' + 'a'*32 + '.json')
        self.assertEqual(json.loads(path.read_bytes()), binding)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        p.kill(); self.assertEqual(owner.wait(5), -signal.SIGKILL)
        self.lease.close()
        self.lease = SessionLease(self.root, enabled=True)
        self.assertEqual(json.loads(path.read_bytes()), binding)
        self.assertEqual(inspect_saved_identity(owner.identity), 'absent_unverified')
        self.assertEqual(inspect_recorded_writer(self.lease, 'a'*32), 'absent_pid')
        self.assertRaises(Exception, CycleCoordinator(CycleState(self.lease.cursor, ['a'], lease=self.lease),
                                                       self.lease.journal).reconcile_after_native_exit,
                          owner, 'a'*32)

    def test_spoofed_identity_and_invalid_fields_do_not_persist(self):
        for updates in ({'pid': os.getpid()}, {'journal_inode': 1},
                        {'journal_device': -1}, {'session_generation': 'b'*31},
                        {'version': True}, {'extra': 1}):
            p = self.spawn(updates)
            _, reader = self.reader(p)
            with self.subTest(updates=updates), self.assertRaises(NativeSessionError):
                reader.read_and_persist()
            self.assertFalse(list(self.lease.journal.glob('session-*.json')))
            p.kill(); p.wait()
            self.lease.close()
            self.root = self.root.with_name(self.root.name + 'x')
            self.lease = SessionLease.create(self.root, enabled=True)

    def test_recorded_identity_reuse_permission_namespace_and_boot(self):
        from dataclasses import replace
        from src.runtime import writer_observation as observation
        p = self.spawn()
        owner, reader = self.reader(p)
        reader.read_and_persist()
        real = observation._identity
        def reused(pid):
            identity, parent = real(pid)
            if pid == p.pid:
                identity = replace(identity, start_ticks=identity.start_ticks + 1)
            return identity, parent
        with mock.patch.object(observation, '_identity', side_effect=reused):
            self.assertEqual(inspect_recorded_writer(self.lease, 'a'*32), 'pid_reused')
        with mock.patch.object(observation.os, 'pidfd_open', side_effect=PermissionError()):
            with self.assertRaises(WriterObservationError):
                inspect_recorded_writer(self.lease, 'a'*32)
        with mock.patch.object(observation, '_namespace', return_value=(0, 42)):
            with self.assertRaises(WriterObservationError):
                inspect_recorded_writer(self.lease, 'a'*32)
        with mock.patch.object(observation, '_boot_id', return_value='f'*8+'-'+ 'f'*4+'-'+ 'f'*4+'-'+ 'f'*4+'-'+ 'f'*12):
            self.assertEqual(inspect_recorded_writer(self.lease, 'a'*32), 'different_boot')
        # All results are observations: no reservation can be cleared by them.
        self.assertIsNone(p.poll())

    def test_journal_audit_detects_partial_foreign_and_conflicting_records(self):
        p = self.spawn()
        _, reader = self.reader(p)
        reader.read_and_persist()
        gen, match = 'a'*32, 'b'*32
        name = f'start-{gen}-{match}.json'
        path = self.lease.journal / name
        event = {'version': 1, 'event': 'game_started', 'session_generation': gen,
                 'host_counter': 7, 'match_id': match}
        path.write_text(json.dumps(event)); path.chmod(0o600)
        os.link(path, path.with_suffix('.ack'))
        inventory = audit(self.lease, gen)
        self.assertEqual(len(inventory), 1)
        self.assertTrue(inventory[0].pending and inventory[0].acknowledged)
        path.unlink()
        inventory = audit(self.lease, gen)
        self.assertFalse(inventory[0].pending)
        self.assertTrue(inventory[0].acknowledged)
        partial = self.lease.journal / '.start-uncertain.tmp'
        partial.write_text('partial')
        with self.assertRaises(JournalAuditError): audit(self.lease, gen)
        partial.unlink()
        foreign = self.lease.journal / ('reservation-' + 'c'*32 + '-7.ready')
        foreign.write_text('foreign')
        with self.assertRaises(JournalAuditError): audit(self.lease, gen)
        foreign.unlink()
        close = self.lease.journal / f'close-{gen}-7.json'
        close.write_text(json.dumps({'version':1, 'event':'lobby_closed',
                                     'session_generation':gen, 'host_counter':7}))
        close.chmod(0o600)
        with self.assertRaises(JournalAuditError): audit(self.lease, gen)
        self.assertIsNone(p.poll())

    def test_inconsistent_or_legacy_binding_rejected(self):
        p = self.spawn()
        _, reader = self.reader(p)
        reader.read_and_persist()
        path = self.lease.journal / ('session-' + 'a'*32 + '.json')
        original = json.loads(path.read_bytes())
        for mutate in ('version', 'namespace', 'journal'):
            value = json.loads(json.dumps(original))
            if mutate == 'version': value['version'] = 1
            if mutate == 'namespace': value['native']['pid_namespace_inode'] += 1
            if mutate == 'journal': value['session']['journal_inode'] += 1
            path.write_text(json.dumps(value))
            with self.subTest(mutate=mutate), self.assertRaises(WriterObservationError):
                inspect_recorded_writer(self.lease, 'a'*32)

    def test_duplicate_json_and_bad_numeric_types_rejected(self):
        for value in (b'{"version":1,"version":1}', b'{"version":NaN}', b'{}'):
            with self.assertRaises(NativeSessionError):
                _parse(b'[HOSTBOT_SESSION] ' + value)

    def test_pipe_swap_executable_and_pid_reuse_fail_closed(self):
        p = self.spawn()
        owner, reader = self.reader(p)
        original = reader.stdout
        fake_r, fake_w = os.pipe()
        try:
            reader.stdout = os.fdopen(fake_r, 'rb', buffering=0)
            with self.assertRaises(NativeSessionError):
                reader._verify()
            reader.stdout.close()
            reader.stdout = original
            with mock.patch('src.runtime.native_session._identity', return_value=(
                    type(owner.identity)(**{**owner.identity.__dict__, 'start_ticks': owner.identity.start_ticks + 1}), os.getpid())):
                with self.assertRaises(NativeSessionError):
                    reader._verify()
        finally:
            os.close(fake_w)
        with self.assertRaises(NativeSessionError):
            NativeSessionReader(owner, self.lease, '/bin/true')

    def test_wrong_executable_and_second_session_fail_closed(self):
        p = self.spawn()
        owner = OwnedChild(p)
        self.addCleanup(owner.close)
        with self.assertRaises(NativeSessionError):
            NativeSessionReader(owner, self.lease, '/bin/true')
        with self.assertRaises(NativeSessionError):
            NativeSessionReader(owner, self.lease, os.path.realpath(sys.executable))
        self.assertFalse(list(self.lease.journal.glob('session-*.json')))

    def test_replaced_journal_fd_is_detected(self):
        p = self.spawn()
        _, reader = self.reader(p)
        original = self.lease.journal_fd
        other = Path(self.temp.name) / 'other'
        other.mkdir(mode=0o700)
        self.lease.journal_fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises((NativeSessionError, SessionLeaseError)):
                reader.read_and_persist()
        finally:
            os.close(self.lease.journal_fd)
            self.lease.journal_fd = original
        self.assertFalse(list(other.glob('session-*.json')))
        self.assertFalse(list(self.lease.journal.glob('session-*.json')))

    def test_replaced_journal_detected_before_binding(self):
        p = self.spawn()
        _, reader = self.reader(p)
        original = self.lease.journal.with_name('old-journal')
        self.lease.journal.rename(original)
        self.lease.journal.mkdir(mode=0o700)
        with self.assertRaises((NativeSessionError, SessionLeaseError)):
            reader.read_and_persist()
        self.assertFalse(list(original.glob('session-*.json')))
        self.assertFalse(list(self.lease.journal.glob('session-*.json')))

    @unittest.skipUnless(hasattr(signal, 'pidfd_send_signal'), 'pidfd signalling unavailable')
    def test_real_supervisor_crash_then_restart_observes_exact_writer(self):
        self.lease.close()
        writer = """import json, os, sys, time
info = os.stat(sys.argv[1])
obj = {'version': 1, 'pid': os.getpid(), 'session_generation': 'a'*32,
       'journal_device': info.st_dev, 'journal_inode': info.st_ino}
os.write(1, b'[HOSTBOT_SESSION] ' + json.dumps(obj).encode() + bytes([10]))
while True: time.sleep(1)
"""
        supervisor_code = """import os, subprocess, sys, time
from src.runtime.session_lease import SessionLease
from src.runtime.process_ownership import OwnedChild
from src.runtime.native_session import NativeSessionReader
from src.runtime.cycle_state import CycleState
from src.runtime.cycle_coordinator import CycleCoordinator
lease=SessionLease(sys.argv[1], enabled=True)
p=subprocess.Popen([sys.executable, '-u', '-c', sys.argv[2], str(lease.journal)], stdout=subprocess.PIPE, bufsize=0)
owner=OwnedChild(p)
reader=NativeSessionReader(owner, lease, os.path.realpath(sys.executable))
reader.read_and_persist()
state=CycleState(lease.cursor, ['a', 'b'], lease=lease)
CycleCoordinator(state, lease.journal).allow_lobby('a'*32, 7)
os.write(1, (str(p.pid)+chr(10)).encode())
while True: time.sleep(1)
"""
        supervisor = subprocess.Popen([sys.executable, '-u', '-c', supervisor_code, str(self.root), writer],
                                      stdout=subprocess.PIPE, bufsize=0)
        pidfd = None
        try:
            self.assertTrue(select.select([supervisor.stdout], [], [], 5)[0])
            writer_pid = int(supervisor.stdout.readline())
            pidfd = os.pidfd_open(writer_pid)
            supervisor.kill()
            self.assertEqual(supervisor.wait(timeout=5), -signal.SIGKILL)
            self.lease = SessionLease(self.root, enabled=True)
            self.assertEqual(inspect_recorded_writer(self.lease, 'a'*32), 'running')
            state = CycleState(self.lease.cursor, ['a', 'b'], lease=self.lease)
            self.assertEqual(state.state['pending']['host_counter'], 7)
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            poller = select.poll(); poller.register(pidfd, select.POLLIN)
            self.assertTrue(poller.poll(5000))
            self.assertIn(inspect_recorded_writer(self.lease, 'a'*32), ('exited', 'absent_pid'))
            self.assertEqual(audit(self.lease, 'a'*32), ())
            from src.runtime.cycle_coordinator import ReconciliationUnavailable
            with self.assertRaises(ReconciliationUnavailable):
                CycleCoordinator(state, self.lease.journal).reconcile_after_native_exit(None, 'a'*32)
            self.assertEqual(state.state['index'], 0)
            self.assertEqual(state.state['pending']['host_counter'], 7)
        finally:
            if supervisor.poll() is None:
                supervisor.kill(); supervisor.wait()
            supervisor.stdout.close()
            if pidfd is not None:
                try: signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                except ProcessLookupError: pass
                os.close(pidfd)
        self.lease.close()
        self.lease = SessionLease(self.root, enabled=True)
        self.assertIn(inspect_recorded_writer(self.lease, 'a'*32), ('exited', 'absent_pid'))

    def test_supervisor_sigkill_releases_lease_and_restart_keeps_reservation(self):
        self.lease.close()
        code = """import os, sys, time
from src.runtime.session_lease import SessionLease
from src.runtime.cycle_state import CycleState
lease = SessionLease(sys.argv[1], enabled=True)
state = CycleState(lease.cursor, ['a', 'b'], lease=lease)
state.reserve_external('a'*32, 7)
os.write(1, b'ready\\n')
while True: time.sleep(1)
"""
        supervisor = subprocess.Popen([sys.executable, '-u', '-c', code, str(self.root)],
                                      stdout=subprocess.PIPE, bufsize=0)
        try:
            self.assertTrue(select.select([supervisor.stdout], [], [], 5)[0])
            self.assertEqual(supervisor.stdout.readline(), b'ready\n')
            with self.assertRaises(BlockingIOError):
                SessionLease(self.root, enabled=True)
            supervisor.kill()
            self.assertEqual(supervisor.wait(timeout=5), -signal.SIGKILL)
        finally:
            if supervisor.poll() is None:
                supervisor.kill(); supervisor.wait()
            supervisor.stdout.close()
        self.lease = SessionLease(self.root, enabled=True)
        state = CycleState(self.lease.cursor, ['a', 'b'], lease=self.lease)
        self.assertEqual(state.state['pending']['host_counter'], 7)
        self.assertEqual(state.state['index'], 0)
        with self.assertRaises(SessionLeaseError):
            CycleCoordinator(state, self.lease.journal).allow_lobby('a'*32, 7)

    def test_scan_does_not_ack_if_callback_loses_lease(self):
        generation, match = 'a'*32, 'b'*32
        name = f'start-{generation}-{match}.json'
        path = self.lease.journal / name
        path.write_text(json.dumps({'version': 1, 'event': 'game_started',
                                    'session_generation': generation, 'match_id': match,
                                    'host_counter': 1}))
        path.chmod(0o600)
        def callback(_):
            self.lease.close()
            return True
        with self.assertRaises(SessionLeaseError):
            scan(self.lease.journal, callback, lease=self.lease)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.ack').exists())
        self.lease = SessionLease(self.root, enabled=True)

    def test_directory_fsync_failure_never_authorizes_marker(self):
        p = self.spawn()
        _, reader = self.reader(p)
        sync = os.fsync
        def fail_journal(fd):
            if fd == self.lease.journal_fd:
                raise OSError('injected directory fsync failure')
            return sync(fd)
        with mock.patch('src.runtime.native_session.os.fsync', side_effect=fail_journal):
            with self.assertRaises(OSError):
                reader.read_and_persist()
        self.assertEqual(len(list(self.lease.journal.glob('session-*.json'))), 1)
        state = CycleState(self.lease.cursor, ['a'], lease=self.lease)
        with self.assertRaises(SessionLeaseError):
            CycleCoordinator(state, self.lease.journal).allow_lobby('a'*32, 7)
        self.assertIsNone(state.state['pending'])

    def test_replaced_or_unlocked_lease_fd_cannot_authorize_mutation(self):
        import fcntl
        state = CycleState(self.lease.cursor, ['a'], lease=self.lease)
        fcntl.flock(self.lease.fd, fcntl.LOCK_UN)
        with self.assertRaises(SessionLeaseError):
            state.reserve_external('a'*32, 1)
        self.assertIsNone(state.state['pending'])

    def test_other_process_is_excluded_and_can_acquire_after_release(self):
        code = ('import sys; from src.runtime.session_lease import SessionLease; '
                'lease=SessionLease(sys.argv[1], enabled=True); lease.close()')
        first = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True)
        self.assertNotEqual(first.returncode, 0)
        self.lease.close()
        second = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.lease = SessionLease(self.root, enabled=True)

    def test_lease_covers_cursor_marker_and_scan(self):
        state = CycleState(self.lease.cursor, ['a', 'b'], lease=self.lease)
        coord = CycleCoordinator(state, self.lease.journal)
        with self.assertRaises(SessionLeaseError):
            coord.allow_lobby('a'*32, 7)
        self.assertIsNone(state.state['pending'])
        p = self.spawn()
        _, reader = self.reader(p)
        reader.read_and_persist()
        with self.assertRaises(SessionLeaseError):
            coord.allow_lobby('b'*32, 7)
        coord.allow_lobby('a'*32, 7)
        self.assertTrue((self.lease.journal / ('reservation-' + 'a'*32 + '-7.ready')).exists())
        self.assertEqual(coord.consume().processed, 0)
        with self.assertRaises(SessionLeaseError):
            CycleState(self.lease.cursor, ['a', 'b'])
        with self.assertRaises(SessionLeaseError):
            scan(self.lease.journal, lambda _: True)
        with self.assertRaises(ValueError):
            CycleState(self.lease.cursor, ['a', 'b'], lease=self.lease)
        with self.assertRaises(BlockingIOError):
            SessionLease(self.root, enabled=True)
        self.lease.close()
        with self.assertRaises(SessionLeaseError):
            state.consume_close({'version': 1, 'event': 'lobby_closed', 'session_generation': 'a'*32, 'host_counter': 7})
        with self.assertRaises(SessionLeaseError):
            publish(self.root / 'journal', state, 'a'*32, 7)
        self.lease = SessionLease(self.root, enabled=True)
        recovered = CycleState(self.lease.cursor, ['a', 'b'], lease=self.lease)
        self.assertEqual(recovered.state['pending']['host_counter'], 7)


if __name__ == '__main__':
    unittest.main()
