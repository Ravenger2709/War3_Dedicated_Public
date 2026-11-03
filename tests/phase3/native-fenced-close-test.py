#!/usr/bin/env python3
"""Offline native fenced-close tests using real GHost and synthetic peers."""
import importlib.util
import json
import os
from unittest import mock
from pathlib import Path
import signal
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT))
from src.runtime.cycle_state import CycleState
from src.runtime.cycle_coordinator import CycleCoordinator, ReconciliationUnavailable
from src.runtime.process_ownership import OwnedChild, write_unverified_observation
spec = importlib.util.spec_from_file_location('gate', ROOT / 'tests/phase3/native-reservation-gate-test.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
wait = gate.wait


class FencedCloseTests(unittest.TestCase):
    setUp = gate.ReservationGateTests.setUp
    tearDown = gate.ReservationGateTests.tearDown
    native = gate.ReservationGateTests.native
    spool = gate.ReservationGateTests.spool
    launch = gate.ReservationGateTests.launch
    denied = gate.ReservationGateTests.denied
    kill_native = gate.ReservationGateTests.kill_native

    def test_sigkill_before_rename_after_rename_and_after_directory_fsync(self):
        library = ROOT/'build/native-fence-crash.so'
        self.assertTrue(library.is_file(), 'test-only crash interposer not built')
        for phase in ('before_rename', 'after_rename', 'after_dirsync'):
            with self.subTest(phase=phase):
                spool = Path(self.temp.name)/('crash-'+phase)
                spool.mkdir(mode=0o700)
                preload = str(library)
                if 'address' in os.environ.get('GHOST_SANITIZERS', ''):
                    asan = subprocess.check_output(['gcc', '-print-file-name=libasan.so'], text=True).strip()
                    preload = asan + ':' + preload
                with mock.patch.dict(os.environ, {'LD_PRELOAD':preload, 'FENCED_TEST_CRASH':phase, 'FENCED_TEST_DIR':str(spool)}):
                    n, gen, counter = self.launch(spool)
                n.peer('First')
                state = CycleState(Path(self.temp.name)/('cursor-'+phase+'.json'), ['one','two'])
                state.reserve_external(gen, counter)
                gate.publish(spool, gen, counter)
                n.proc.stdin.write('removehost\n'); n.proc.stdin.flush()
                self.assertEqual(n.proc.wait(timeout=6), -signal.SIGKILL)
                dead_child = n.proc
                n.thread.join(2)
                for peer in n.peers: peer.close()
                n.proc.stdin.close(); n.proc.stdout.close(); n.proc = None
                close = spool/f'close-{gen}-{counter}.json'
                if phase == 'before_rename':
                    self.assertFalse(close.exists())
                    self.assertTrue(list(spool.glob('.close-*.tmp')))
                    coordinator = CycleCoordinator(state, spool)
                    with self.assertRaises(ReconciliationUnavailable):
                        coordinator.reconcile_after_native_exit(dead_child, gen)
                    self.assertEqual(coordinator.consume().processed, 0)
                    self.assertIsNotNone(CycleState(state.path,['one','two']).state['pending'])
                else:
                    self.assertTrue(close.exists())
                    self.assertFalse(any(line.startswith('[HOSTBOT_LIFECYCLE] ') and 'lobby_closed' in line for line in n.lines))
                    coordinator = CycleCoordinator(state, spool)
                    with self.assertRaises(ReconciliationUnavailable):
                        coordinator.reconcile_after_native_exit(dead_child, gen)
                    self.assertTrue(close.exists())
                    self.assertEqual(coordinator.consume().processed, 1)
                    restored = CycleState(state.path, ['one','two'])
                    self.assertIsNone(restored.state['pending'])
                    self.assertEqual(restored.current, 'one')
                    with self.assertRaisesRegex(ValueError, 'LOBBY_REUSED'):
                        restored.reserve_external(gen, counter)

    def test_uncertain_start_dirsync_never_emits_close_for_same_pair(self):
        spool = self.spool()
        library = ROOT/'build/native-fence-crash.so'
        preload = str(library)
        if 'address' in os.environ.get('GHOST_SANITIZERS', ''):
            asan = subprocess.check_output(['gcc', '-print-file-name=libasan.so'], text=True).strip()
            preload = asan + ':' + preload
        extra = {'bot_maxgames': '2', 'autohost_maxgames': '2', 'autohost_startplayers': '6',
                 'autohost_gamename': 'Same map', 'autohost_owner': 'Owner'}
        env = {'LD_PRELOAD':preload, 'FENCED_TEST_CRASH':'start_dirsync_error', 'FENCED_TEST_DIR':str(spool)}
        with mock.patch.dict(os.environ, env):
            n, gen, first = self.launch(spool, extra)
        peer = n.peer('First', game_keepalive=True)
        gate.publish(spool, gen, first)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gate.gameplay.frame(0x23))
        wait(lambda: peer.game_keepalives_sent > 0)
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        second = n.host_state()['lobby']['host_counter']
        n.peer('Second')
        gate.publish(spool, gen, second)
        before = peer.game_keepalives_sent
        n.command('startlobby')
        wait(lambda: n.host_state()['lobby'] is None)
        self.assertTrue(any('lifecycle journal write failed' in line for line in n.lines))
        events = [json.loads(p.read_bytes()) for p in spool.glob('start-*.json')]
        self.assertEqual({event['host_counter'] for event in events}, {first, second})
        self.assertFalse((spool/f'close-{gen}-{second}.json').exists())
        self.assertEqual(n.host_state()['active_games'], 1)
        wait(lambda: peer.game_keepalives_sent > before)
        self.assertFalse(peer.disconnected)
        n.command('enablehost'); n.command('trycreate')
        self.assertIsNone(n.host_state()['lobby'])

    def test_uncertain_close_dirsync_still_fences_and_preserves_active_game(self):
        spool = self.spool()
        library = ROOT/'build/native-fence-crash.so'
        preload = str(library)
        if 'address' in os.environ.get('GHOST_SANITIZERS', ''):
            asan = subprocess.check_output(['gcc', '-print-file-name=libasan.so'], text=True).strip()
            preload = asan + ':' + preload
        extra = {'bot_maxgames': '2', 'autohost_maxgames': '2', 'autohost_startplayers': '6',
                 'autohost_gamename': 'Same map', 'autohost_owner': 'Owner'}
        env = {'LD_PRELOAD':preload, 'FENCED_TEST_CRASH':'dirsync_error', 'FENCED_TEST_DIR':str(spool)}
        with mock.patch.dict(os.environ, env):
            n, gen, first = self.launch(spool, extra)
        state = CycleState(Path(self.temp.name)/'cursor-uncertain.json', ['one','two'])
        state.reserve_external(gen, first)
        peer = n.peer('First', game_keepalive=True)
        gate.publish(spool, gen, first)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gate.gameplay.frame(0x23))
        wait(lambda: peer.game_keepalives_sent > 0)
        CycleCoordinator(state, spool).consume()
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        second = n.host_state()['lobby']['host_counter']
        state.reserve_external(gen, second)
        gate.publish(spool, gen, second)
        before = peer.game_keepalives_sent
        n.command('removehost')
        close = spool/f'close-{gen}-{second}.json'
        self.assertTrue(close.exists())
        self.assertTrue(any('lifecycle close write failed' in line for line in n.lines))
        self.assertFalse(any(line.startswith('[HOSTBOT_LIFECYCLE] ') and 'lobby_closed' in line for line in n.lines))
        self.assertEqual(CycleCoordinator(state, spool).consume().processed, 1)
        self.assertIsNone(CycleState(state.path,['one','two']).state['pending'])
        wait(lambda: peer.game_keepalives_sent > before)
        self.assertFalse(peer.disconnected)
        self.assertEqual(n.host_state()['active_games'], 1)
        n.command('enablehost'); n.command('trycreate')
        self.assertIsNone(n.host_state()['lobby'])

    def test_real_native_pidfd_wait_still_requires_verified_journal_binding(self):
        spool = self.spool()
        library = ROOT/'build/native-fence-crash.so'
        preload = str(library)
        if 'address' in os.environ.get('GHOST_SANITIZERS', ''):
            asan = subprocess.check_output(['gcc', '-print-file-name=libasan.so'], text=True).strip()
            preload = asan + ':' + preload
        env = {'LD_PRELOAD':preload, 'FENCED_TEST_CRASH':'before_rename', 'FENCED_TEST_DIR':str(spool)}
        with mock.patch.dict(os.environ, env):
            n, gen, counter = self.launch(spool)
        n.peer('First')
        state = CycleState(Path(self.temp.name)/'native-pidfd-cursor.json', ['one','two'])
        pending = state.reserve_external(gen, counter)
        gate.publish(spool, gen, counter)
        with OwnedChild(n.proc) as owned:
            observation = write_unverified_observation(owned, spool, gen)
            record = json.loads((spool/observation).read_bytes())
            self.assertIs(record['verified_binding'], False)
            self.assertEqual(record['reported_session_generation'], gen)
            n.proc.stdin.write('removehost\n'); n.proc.stdin.flush()
            self.assertEqual(owned.wait(timeout=6), -signal.SIGKILL)
            n.thread.join(2)
            for peer in n.peers: peer.close()
            n.proc.stdin.close(); n.proc.stdout.close(); n.proc = None
            coordinator = CycleCoordinator(state, spool)
            with self.assertRaises(ReconciliationUnavailable):
                coordinator.reconcile_after_native_exit(owned, gen)
            self.assertEqual(CycleState(state.path,['one','two']).state['pending'], pending)
            self.assertFalse((spool/f'close-{gen}-{counter}.json').exists())
            self.assertTrue(list(spool.glob('.close-*.tmp')))

    def test_closed_lobby_is_fenced_and_survives_native_crash(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool)
        n.peer('First')
        gate.publish(spool, gen, counter)
        n.command('removehost')
        name = spool / f'close-{gen}-{counter}.json'
        wait(name.exists)
        expected = {'version': 1, 'event': 'lobby_closed', 'session_generation': gen, 'host_counter': counter}
        self.assertEqual(json.loads(name.read_bytes()), expected)
        self.assertEqual(list(spool.glob('start-*.json')), [])
        self.kill_native(n)
        self.assertEqual(json.loads(name.read_bytes()), expected)
        n2, next_gen, next_counter = self.launch(spool)
        self.assertNotEqual(gen, next_gen)
        self.assertEqual(counter, next_counter)
        self.denied(n2, spool)
        self.assertEqual(json.loads(name.read_bytes()), expected)

    def test_temporary_snapshot_game_does_not_emit_close_for_other_lobby(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool)
        n.proc.stdin.write('snapshots\n'); n.proc.stdin.flush()
        line = n.marker('TEST SNAPSHOTS ')
        n.marker('TEST COMMAND snapshots')
        first, first_after, second, second_after = json.loads(line[len('TEST SNAPSHOTS '):])
        self.assertEqual(first, first_after); self.assertEqual(second, second_after)
        self.assertEqual(list(spool.glob('close-*.json')), [])
        self.assertEqual(n.host_state()['lobby']['host_counter'], counter)
        n.command('removehost')
        self.assertEqual(json.loads((spool/f'close-{gen}-{counter}.json').read_bytes())['host_counter'], counter)
        self.assertEqual(len(list(spool.glob('close-*.json'))), 1)

    def test_fast_start_and_close_are_mutually_exclusive(self):
        for first in ('startlobby', 'removehost'):
            with self.subTest(first=first):
                spool = Path(self.temp.name)/('spool-'+first)
                spool.mkdir(mode=0o700)
                n, gen, counter = self.launch(spool)
                n.peer('First', game_keepalive=True)
                gate.publish(spool, gen, counter)
                second = 'removehost' if first == 'startlobby' else 'startlobby'
                n.proc.stdin.write(first+'\n'+second+'\n'); n.proc.stdin.flush()
                n.marker('TEST COMMAND '+first); n.marker('TEST COMMAND '+second)
                host = n.host_state()
                starts = list(spool.glob('start-*.json'))
                closes = list(spool.glob(f'close-{gen}-{counter}.json'))
                if first == 'startlobby':
                    self.assertEqual((len(starts), len(closes), host['active_games']), (1, 0, 1))
                else:
                    self.assertEqual((len(starts), len(closes), host['active_games']), (0, 1, 0))
                for peer in n.peers: peer.close()
                n.stop()

    def test_rehost_closes_old_identity_and_new_identity_can_start(self):
        spool = self.spool()
        n, gen, old = self.launch(spool, {'autohost_maxgames': '2', 'autohost_startplayers': '6',
                                          'autohost_gamename': 'Same map', 'autohost_owner': 'Owner'}, manual=False)
        peer = n.peer('First', game_keepalive=True)
        state = CycleState(Path(self.temp.name)/'rehost-cycle.json', ['one','two'])
        state.reserve_external(gen, old)
        gate.publish(spool, gen, old)
        n.command('rehosterror')
        wait(lambda: n.host_state()['lobby']['host_counter'] != old)
        new = n.host_state()['lobby']['host_counter']
        old_close = spool/f'close-{gen}-{old}.json'
        self.assertEqual(json.loads(old_close.read_bytes())['host_counter'], old)
        self.denied(n, spool)
        self.assertFalse(peer.disconnected)
        self.assertEqual(CycleCoordinator(state, spool).consume().processed, 1)
        self.assertIsNone(state.state['pending'])
        self.assertEqual(state.current, 'one')
        with self.assertRaisesRegex(ValueError, 'LOBBY_REUSED'):
            state.reserve_external(gen, old)
        state.reserve_external(gen, new)
        gate.publish(spool, gen, new)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gate.gameplay.frame(0x23))
        wait(lambda: peer.game_keepalives_sent > 0)
        self.assertFalse((spool/f'close-{gen}-{new}.json').exists())
        self.assertEqual(json.loads(next(spool.glob('start-*.json')).read_bytes())['host_counter'], new)
        self.assertEqual(CycleCoordinator(state, spool).consume().processed, 1)
        self.assertEqual(CycleState(state.path, ['one','two']).current, 'two')

    def test_admin_public_and_private_rehosts_fence_previous_counter(self):
        for cmd in ('!pub renamed', '!priv renamed'):
            with self.subTest(cmd=cmd):
                spool = Path(self.temp.name)/('spool-'+cmd.split()[0][1:])
                spool.mkdir(mode=0o700)
                n, gen, old = self.launch(spool)
                owner = n.peer('Owner', lan=True)
                gate.publish(spool, gen, old)
                owner.chat(cmd)
                wait(lambda: n.host_state()['lobby']['host_counter'] != old)
                self.assertEqual(json.loads((spool/f'close-{gen}-{old}.json').read_bytes())['host_counter'], old)
                self.denied(n, spool)
                for peer in n.peers: peer.close()
                n.stop()

    def test_rehost_command_in_started_game_does_not_change_its_identity(self):
        spool = self.spool()
        n, gen, first_counter = self.launch(spool, {'bot_maxgames': '2', 'autohost_maxgames': '2',
                                                    'autohost_startplayers': '6', 'autohost_gamename': 'Same map',
                                                    'autohost_owner': 'Owner'})
        owner = n.peer('Owner', lan=True, game_keepalive=True)
        gate.publish(spool, gen, first_counter)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        owner.sock.sendall(gate.gameplay.frame(0x23))
        wait(lambda: owner.game_keepalives_sent > 0)
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        second = n.host_state()['lobby']['host_counter']
        n.command('rehostrunning')
        self.assertEqual(n.host_state()['games'][0]['host_counter'], first_counter)
        self.assertFalse((spool/f'close-{gen}-{first_counter}.json').exists())
        for cmd in ('!pub sabotage', '!priv sabotage'):
            owner.chat(cmd)
            state = n.host_state()
            self.assertEqual(state['games'][0]['host_counter'], first_counter)
            self.assertEqual(state['lobby']['host_counter'], second)
            self.assertFalse((spool/f'close-{gen}-{first_counter}.json').exists())
        self.assertFalse(owner.disconnected)

    def test_shutdown_closes_only_lobby_while_running_game_survives(self):
        spool = self.spool()
        n, gen, first_counter = self.launch(spool, {'bot_maxgames': '2', 'autohost_maxgames': '2',
                                                    'autohost_startplayers': '6', 'autohost_gamename': 'Same map',
                                                    'autohost_owner': 'Owner'})
        peer = n.peer('First', game_keepalive=True)
        gate.publish(spool, gen, first_counter)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gate.gameplay.frame(0x23))
        wait(lambda: peer.game_keepalives_sent > 0)
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        second = n.host_state()['lobby']['host_counter']
        gate.publish(spool, gen, second)
        before = peer.game_keepalives_sent
        n.command('niceshutdown')
        wait(lambda: n.host_state()['lobby'] is None)
        self.assertEqual(json.loads((spool/f'close-{gen}-{second}.json').read_bytes())['host_counter'], second)
        self.assertFalse((spool/f'close-{gen}-{first_counter}.json').exists())
        wait(lambda: peer.game_keepalives_sent > before)
        self.assertFalse(peer.disconnected)
        self.assertEqual(n.host_state()['active_games'], 1)

    def test_close_write_failure_fences_lobby_and_preserves_active_match(self):
        spool = self.spool()
        n, gen, first_counter = self.launch(spool, {'bot_maxgames': '2', 'autohost_maxgames': '2',
                                                    'autohost_startplayers': '6', 'autohost_gamename': 'Same map',
                                                    'autohost_owner': 'Owner'})
        peer = n.peer('First', game_keepalive=True)
        gate.publish(spool, gen, first_counter)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gate.gameplay.frame(0x23))
        wait(lambda: peer.game_keepalives_sent > 0)
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        second = n.host_state()['lobby']['host_counter']
        n.peer('Second')
        gate.publish(spool, gen, second)
        before = peer.game_keepalives_sent
        n.command('journalfault')
        n.command('rehosterror')
        n.command('journalrestore')
        wait(lambda: n.host_state()['lobby'] is None)
        self.assertTrue(any('lifecycle close write failed' in line for line in n.lines))
        self.assertFalse((spool/f'close-{gen}-{second}.json').exists())
        self.assertEqual(n.host_state()['active_games'], 1)
        wait(lambda: peer.game_keepalives_sent > before)
        self.assertIsNone(n.proc.poll())
        n.command('enablehost'); n.command('trycreate')
        self.assertIsNone(n.host_state()['lobby'])


if __name__ == '__main__':
    unittest.main()
