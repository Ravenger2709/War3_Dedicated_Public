#!/usr/bin/env python3
"""Native journal tests with real SIGKILL; peers and paths are synthetic/offline."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_gameplay', ROOT / 'tests/phase3/native-test.py')
gameplay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gameplay)
wait = gameplay.wait


def syncdir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def acknowledge(event):
    # Link first: an existing ack is success, never replace one. Directory fsync
    # makes the ack durable before removing the event's pending name.
    ack = event.with_suffix('.ack')
    try:
        os.link(event, ack, follow_symlinks=False)
    except FileExistsError:
        if ack.is_symlink() or ack.read_bytes() != event.read_bytes():
            raise AssertionError('conflicting ack')
    syncdir(event.parent)
    event.unlink(missing_ok=True)
    syncdir(event.parent)
    return ack


class LifecycleTests(unittest.TestCase):
    setUp = gameplay.GameplayTests.setUp
    tearDown = gameplay.GameplayTests.tearDown
    native = gameplay.GameplayTests.native

    def spool(self):
        path = Path(self.temp.name) / 'spool'
        path.mkdir(mode=0o700)
        return path

    def pending(self, spool):
        return sorted(p for p in spool.glob('start-*.json') if not p.with_suffix('.ack').exists())

    def started(self, n, spool, name):
        n.start().create()
        p = n.peer(name, game_keepalive=True)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        p.sock.sendall(gameplay.frame(0x23))
        wait(lambda: p.game_keepalives_sent > 0)
        wait(lambda: len(self.pending(spool)) > 0)
        return p

    def kill_native(self, n):
        for peer in n.peers:
            peer.close()
        n.proc.kill()
        self.assertEqual(n.proc.wait(timeout=5), -signal.SIGKILL)
        n.thread.join(2)
        n.proc.stdin.close(); n.proc.stdout.close()
        n.proc = None

    def test_production_controller_reader_binds_real_native_child(self):
        sys.path.insert(0, str(ROOT))
        import io
        import queue
        import time
        from src.runtime.session_lease import SessionLease
        from src.runtime.controller import ICCupSupervisor
        from src.runtime.config import gameplay_config, iccup_config
        root=Path(self.temp.name)
        session=root/'controller-session'
        lease=SessionLease.create(session,enabled=True);lease.close()
        n=self.native({'bot_lifecycle_event_dir': str(session/'journal'), 'bot_start_reservation_gate':'1'})
        binary=ROOT/'build/ghost/ghost++'
        class Backend:
            def prepare(self): (root/'bridge').mkdir(mode=0o700)
            def command(self,name,generation): return [str(binary),str(n.cfg)]
            def listener(self,pid): return False
            def cleanup_prefix(self): return {'cleaned':True}
            def close(self): pass
        cfg={'mode':'iccup','runtime_dir':str(root/'supervisor'),
             'http':{'host':'127.0.0.1','port':0},
             'hostbot':{'command':['/unused'],'cwd':str(n.root),'startup_timeout':2,'stop_timeout':.5,'lobby_timeout':1},
             'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':30},
             'iccup':{**iccup_config({}),'bridge_dir':str(root/'bridge')},'gameplay':gameplay_config({})}
        s=ICCupSupervisor(cfg,output=io.StringIO(),backend=Backend(),
                          cycle_session={'root':str(session),'cycle':['a','b']})
        try:
            s._cycle_initialize();child=s._spawn('native')
            deadline=time.monotonic()+6
            while time.monotonic()<deadline and child.session_reader is None and not s.cycle_failed:
                try: event=s.events.get(timeout=.2)
                except queue.Empty:continue
                if event[0]=='component_line' and event[1]=='native':s._native_line(event[2],event[3],*event[4:])
            self.assertFalse(s.cycle_failed)
            self.assertIsNotNone(child.session_reader)
            binding=list((session/'journal').glob('session-*.json'))
            self.assertEqual(len(binding),1)
            self.assertEqual(json.loads(binding[0].read_bytes())['session']['pid'],child.process.pid)
            self.assertFalse(s._cycle_permits_native_spawn())
            old_journal=s.cycle_control.lease.journal
            s._stop_component('native',controlled_cycle=True)
            self.assertIsNotNone(child.session_owner._verified_exit)
            self.assertEqual(child.session_owner._verified_exit[1],0,s.output.getvalue())
            s.cycle_next_scan=0.;s._cycle_tick()
            self.assertFalse(s.cycle_failed)
            self.assertNotEqual(s.cycle_control.lease.journal,old_journal)
            self.assertEqual(s.cycle_control.state.current,'a')
        finally:
            s._stop_component('native')
            if s.cycle_control:s.cycle_control.close()
            s.running=False;s.backend.close();s.lockfile.close()

    def test_owned_reader_binds_real_native_startup_and_sigkill(self):
        sys.path.insert(0, str(ROOT))
        from src.runtime.session_lease import SessionLease
        from src.runtime.native_session import NativeSessionReader
        from src.runtime.process_ownership import OwnedChild
        lease = SessionLease.create(Path(self.temp.name) / 'managed', enabled=True)
        n = self.native({'bot_lifecycle_event_dir': str(lease.journal),
                         'bot_start_reservation_gate': '1'})
        binary = ROOT / 'build/native-driver'
        proc = subprocess.Popen([str(binary), str(n.cfg)], cwd=n.root,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0)
        try:
            with OwnedChild(proc) as child:
                reader = NativeSessionReader(child, lease, binary)
                try:
                    binding = reader.read_and_persist(timeout=6)
                    self.assertEqual(binding['session']['pid'], proc.pid)
                    self.assertEqual(binding['session']['journal_inode'], lease.journal.stat().st_ino)
                    from src.runtime.cycle_state import CycleState
                    from src.runtime.cycle_coordinator import CycleCoordinator, ReconciliationUnavailable
                    state = CycleState(lease.cursor, ['a', 'b'], lease=lease)
                    coordinator = CycleCoordinator(state, lease.journal)
                    generation = binding['session']['session_generation']
                    from src.runtime.writer_observation import inspect_recorded_writer
                    self.assertEqual(inspect_recorded_writer(lease, generation), 'running')
                    self.assertEqual(binding['native']['start_ticks'], child.identity.start_ticks)
                    self.assertEqual(binding['native']['pid_namespace_inode'], os.stat(f'/proc/{proc.pid}/ns/pid').st_ino)
                    coordinator.allow_lobby(generation, 7)
                    self.assertTrue((lease.journal / f'reservation-{generation}-7.ready').exists())
                    proc.kill()
                    self.assertEqual(child.wait(5), -signal.SIGKILL)
                    self.assertEqual(inspect_recorded_writer(lease, generation), 'absent_pid')
                    with self.assertRaises(ReconciliationUnavailable):
                        coordinator.reconcile_after_native_exit(child, generation)
                    self.assertEqual(state.state['pending']['host_counter'], 7)
                finally:
                    reader.close()
            name = 'session-' + binding['session']['session_generation'] + '.json'
            self.assertEqual(json.loads((lease.journal / name).read_bytes()), binding)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            proc.stdin.close(); proc.stdout.close()
            lease.close()

    def test_startup_session_reports_exact_child_and_pinned_directory(self):
        spool = self.spool()
        n = self.native({'bot_lifecycle_event_dir': str(spool)}).start()
        lines = [line for line in n.lines if line.startswith('[HOSTBOT_SESSION] ')]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0].split(' ', 1)[1])
        info = spool.stat()
        self.assertEqual(set(record), {'version', 'pid', 'session_generation', 'journal_device', 'journal_inode'})
        self.assertEqual((record['version'], record['pid'], record['journal_device'], record['journal_inode']),
                         (1, n.proc.pid, info.st_dev, info.st_ino))
        wait(lambda: n.structured_states())
        self.assertEqual(record['session_generation'], n.structured_states()[-1]['session_generation'])

    def test_no_startup_session_without_journal(self):
        n = self.native().start()
        self.assertFalse(any(line.startswith('[HOSTBOT_SESSION] ') for line in n.lines))

    def test_state_generation_is_available_before_lobby_and_matches_journal(self):
        spool = self.spool()
        n = self.native({'bot_lifecycle_event_dir': str(spool)})
        n.start()
        wait(lambda: n.structured_states())
        idle = n.structured_states()[-1]
        self.assertEqual(idle['phase'], 'IDLE')
        generation = idle['session_generation']
        self.assertRegex(generation, r'^[0-9a-f]{32}$')
        n.create()
        wait(lambda: any(s['phase'] == 'LOBBY' for s in n.structured_states()))
        lobby = next(s for s in reversed(n.structured_states()) if s['phase'] == 'LOBBY')
        self.assertEqual(lobby['session_generation'], generation)
        peer = n.peer('First', game_keepalive=True)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gameplay.frame(0x23))
        wait(lambda: self.pending(spool))
        event = json.loads(self.pending(spool)[0].read_bytes())
        self.assertEqual((event['session_generation'], event['host_counter']),
                         (generation, lobby['host_counter']))
        wait(lambda: any(s['phase'] == 'IN_GAME' for s in n.structured_states()))
        self.assertEqual(n.structured_states()[-1]['session_generation'], generation)
        self.kill_native(n)
        n2 = self.native({'bot_lifecycle_event_dir': str(spool)})
        n2.start()
        wait(lambda: n2.structured_states())
        new_generation = n2.structured_states()[-1]['session_generation']
        self.assertRegex(new_generation, r'^[0-9a-f]{32}$')
        self.assertNotEqual(new_generation, generation)
        self.assertEqual(json.loads(self.pending(spool)[0].read_bytes()), event)

    def test_crash_replay_restart_and_idempotent_ack(self):
        spool = self.spool()
        n = self.native({'bot_lifecycle_event_dir': str(spool), 'bot_maxgames': '2'})
        self.started(n, spool, 'First')
        event = self.pending(spool)[0]
        original = event.read_bytes()
        data = json.loads(original)
        self.assertEqual(set(data), {'version', 'event', 'session_generation', 'match_id', 'host_counter'})
        self.assertEqual((data['version'], data['event'], data['host_counter']), (1, 'game_started', 1))
        for field in ('session_generation', 'match_id'):
            self.assertRegex(data[field], r'^[0-9a-f]{32}$')
        self.assertEqual(event.name, f"start-{data['session_generation']}-{data['match_id']}.json")
        self.assertEqual(event.stat().st_mode & 0o777, 0o600)
        self.assertTrue(any(line.startswith('[HOSTBOT_LIFECYCLE] ') for line in n.lines))
        n.command('load')  # duplicate callback must not publish twice
        self.assertEqual(len(self.pending(spool)), 1)
        # A consumer reads the event and crashes before it can ack.
        result = subprocess.run([sys.executable, '-c',
            'import os,signal,sys; open(sys.argv[1],"rb").read(); os.kill(os.getpid(),signal.SIGKILL)', str(event)])
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertEqual(event.read_bytes(), original)
        self.kill_native(n)
        self.assertEqual(event.read_bytes(), original)
        # A new native process must not overwrite the unacknowledged event.
        n2 = self.native({'bot_lifecycle_event_dir': str(spool)})
        self.started(n2, spool, 'Second')
        events = self.pending(spool)
        self.assertEqual(len(events), 2)
        second = next(p for p in events if p != event)
        other = json.loads(second.read_bytes())
        self.assertEqual(other['host_counter'], 1)
        self.assertNotEqual(other['session_generation'], data['session_generation'])
        self.assertNotEqual(other['match_id'], data['match_id'])
        self.assertEqual(event.read_bytes(), original)
        ack = event.with_suffix('.ack')
        # The consumer is killed after durably creating ack but before unlink.
        result = subprocess.run([sys.executable, '-c',
            'import os,signal,sys; os.link(sys.argv[1],sys.argv[2]); d=os.open(sys.argv[3],os.O_RDONLY|os.O_DIRECTORY); os.fsync(d); os.close(d); os.kill(os.getpid(),signal.SIGKILL)',
            str(event), str(ack), str(spool)])
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertTrue(event.exists())
        self.assertEqual(ack.read_bytes(), original)
        self.assertEqual(acknowledge(event), ack)
        self.assertEqual(self.pending(spool), [second])
        self.assertEqual(ack.read_bytes(), original)

    def test_two_starts_same_process_preserve_unacked_events(self):
        spool = self.spool()
        n = self.native({'bot_lifecycle_event_dir': str(spool), 'bot_maxgames': '2',
                         'autohost_maxgames': '2', 'autohost_startplayers': '6',
                         'autohost_gamename': 'Same map', 'autohost_owner': 'Owner'})
        self.started(n, spool, 'First')
        first = self.pending(spool)[0]
        old = first.read_bytes()
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        second_peer = n.peer('Second', game_keepalive=True)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 2)
        second_peer.sock.sendall(gameplay.frame(0x23))
        wait(lambda: len(self.pending(spool)) == 2)
        second = next(p for p in self.pending(spool) if p != first)
        a, b = json.loads(old), json.loads(second.read_bytes())
        self.assertEqual(first.read_bytes(), old)
        self.assertEqual(a['session_generation'], b['session_generation'])
        wait(lambda: n.structured_states())
        self.assertTrue(all(state['session_generation'] == a['session_generation']
                            for state in n.structured_states()))
        self.assertNotEqual(a['match_id'], b['match_id'])
        self.assertNotEqual(a['host_counter'], b['host_counter'])

    def test_snapshot_survives_settings_change_and_journaled_start(self):
        spool = self.spool()
        n = self.native({'bot_lifecycle_event_dir': str(spool), 'bot_maxgames': '2',
                         'bot_maxplayers': '10', 'bot_autostartplayers': '7',
                         'bot_autostart_enabled': 'true', 'bot_vote_required_percent': '60'})
        n.start().create()
        n.proc.stdin.write('snapshots\n'); n.proc.stdin.flush()
        line = n.marker('TEST SNAPSHOTS ')
        n.marker('TEST COMMAND snapshots')
        before, after, other_before, other_after = json.loads(line[len('TEST SNAPSHOTS '):])
        self.assertEqual(before, after)
        self.assertEqual(other_before, other_after)
        self.assertNotEqual(before, other_before)
        peer = n.peer('First', game_keepalive=True)
        counter = n.host_state()['lobby']['host_counter']
        n.command('startlobby')
        state = wait(lambda: (s if (s := n.host_state())['active_games'] == 1 else None))
        self.assertEqual((state['games'][0]['max_players'], state['games'][0]['autostart_players']), (10, 7))
        peer.sock.sendall(gameplay.frame(0x23))
        wait(lambda: peer.game_keepalives_sent > 0)
        event = wait(lambda: self.pending(spool))
        self.assertEqual(len(event), 1)
        self.assertEqual(json.loads(event[0].read_bytes())['host_counter'], counter)

    def test_legacy_disabled_and_invalid_directory_fail_closed(self):
        n = self.native()
        n.start()
        wait(lambda: n.structured_states())
        self.assertNotIn('session_generation', n.structured_states()[-1])
        n.create()
        wait(lambda: any(state['phase'] == 'LOBBY' for state in n.structured_states()))
        self.assertTrue(all('session_generation' not in state for state in n.structured_states()))
        peer = n.peer('First', game_keepalive=True)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gameplay.frame(0x23))
        wait(lambda: any(state['phase'] == 'IN_GAME' for state in n.structured_states()))
        self.assertTrue(all('session_generation' not in state for state in n.structured_states()))
        self.assertFalse(any(line.startswith('[HOSTBOT_LIFECYCLE] ') for line in n.lines))
        n.stop()
        n2 = self.native({'bot_lifecycle_event_dir': str(Path(self.temp.name) / 'missing')})
        n2.start(ready=False)
        self.assertEqual(n2.proc.wait(timeout=6), 78)
        n2.thread.join(2)
        self.assertTrue(any('lifecycle journal initialization failed' in line for line in n2.lines))
        self.assertFalse(any('[GHOST] offline initialization complete' in line for line in n2.lines))

    def test_directory_failure_before_start_closes_only_lobby(self):
        spool = self.spool()
        n = self.native({'bot_lifecycle_event_dir': str(spool)})
        try:
            n.start().create()
            n.peer('First')
            spool.chmod(0o500)
            n.command('startlobby')
            wait(lambda: n.host_state()['lobby'] is None)
            self.assertIsNone(n.proc.poll())
            n.command('trycreate')
            self.assertIsNone(n.host_state()['lobby'])
            self.assertEqual(self.pending(spool), [])
            self.assertFalse(any(line.startswith('[HOSTBOT_LIFECYCLE] ') for line in n.lines))
            self.assertTrue(any('lifecycle journal write failed' in line for line in n.lines))
        finally:
            spool.chmod(0o700)


if __name__ == '__main__':
    unittest.main()
