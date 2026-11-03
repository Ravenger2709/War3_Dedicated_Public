#!/usr/bin/env python3
"""Offline native reservation gate, real process and synthetic clients."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_gameplay', ROOT / 'tests/phase3/native-test.py')
gameplay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gameplay)
wait = gameplay.wait


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def marker_path(spool, generation, counter):
    return spool / f'reservation-{generation}-{counter}.ready'


def marker_bytes(generation, counter):
    return f'version=1\nsession_generation={generation}\nhost_counter={counter}\n'.encode()


def publish(spool, generation, counter, contents=None):
    path = marker_path(spool, generation, counter)
    temp = spool / ('.' + path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = marker_bytes(generation, counter) if contents is None else contents
        assert os.write(fd, data) == len(data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.link(temp, path)
    temp.unlink()
    fsync_dir(spool)
    return path


class ReservationGateTests(unittest.TestCase):
    setUp = gameplay.GameplayTests.setUp
    tearDown = gameplay.GameplayTests.tearDown
    native = gameplay.GameplayTests.native

    def spool(self):
        path = Path(self.temp.name) / 'journal'
        path.mkdir(mode=0o700)
        return path

    def launch(self, spool, extra=None, manual=True):
        settings = {'bot_lifecycle_event_dir': str(spool), 'bot_start_reservation_gate': '1'}
        settings.update(extra or {})
        n = self.native(settings).start()
        wait(lambda: n.structured_states())
        if manual:
            n.create()
        else:
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
        wait(lambda: any(s['phase'] == 'LOBBY' for s in n.structured_states()))
        state = next(s for s in reversed(n.structured_states()) if s['phase'] == 'LOBBY')
        return n, state['session_generation'], state['host_counter']

    def denied(self, n, spool):
        previous = n.host_state()['active_games']
        before = len(list(spool.glob('start-*.json')))
        n.command('startlobby')
        state = n.host_state()
        self.assertEqual(state['active_games'], previous)
        self.assertIsNotNone(state['lobby'])
        self.assertEqual(len(list(spool.glob('start-*.json'))), before)
        self.assertIsNone(n.proc.poll())

    def kill_native(self, n):
        for peer in n.peers:
            peer.close()
        n.proc.kill()
        self.assertEqual(n.proc.wait(timeout=5), -signal.SIGKILL)
        n.thread.join(2)
        n.proc.stdin.close(); n.proc.stdout.close(); n.proc = None

    def test_replaced_journal_directory_cannot_authorize_start(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool)
        n.peer('First', game_keepalive=True)
        original = spool.with_name('original-journal')
        spool.rename(original)
        spool.mkdir(mode=0o700)
        publish(spool, gen, counter)
        self.denied(n, spool)
        self.assertFalse(list(original.glob('start-*.json')))
        self.assertFalse(list(spool.glob('start-*.json')))

    def test_missing_wrong_and_malformed_markers_fail_closed(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool)
        peer = n.peer('First', game_keepalive=True)
        self.denied(n, spool)
        publish(spool, 'f' * 32, counter)
        publish(spool, gen, counter + 1)
        self.denied(n, spool)
        path = marker_path(spool, gen, counter)
        bad = [marker_bytes('f'*32, counter), marker_bytes(gen, counter+1),
               marker_bytes(gen, counter) + b'extra=1\n', b'version=2\n', b'X'*129]
        for content in bad:
            with self.subTest(content=content[:70]):
                publish(spool, gen, counter, content)
                self.denied(n, spool)
                path.unlink()
        target = self.root if hasattr(self, 'root') else Path(self.temp.name)
        path.symlink_to(target / 'missing')
        self.denied(n, spool)
        path.unlink()
        publish(spool, gen, counter)
        path.chmod(0o644)
        self.denied(n, spool)
        path.chmod(0o600)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gameplay.frame(0x23))
        wait(lambda: peer.game_keepalives_sent > 0)
        event = json.loads(next(spool.glob('start-*.json')).read_bytes())
        self.assertEqual((event['session_generation'], event['host_counter']), (gen, counter))

    def test_gate_uses_same_lobby_after_gameplay_snapshot_changes(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool)
        n.proc.stdin.write('snapshots\n'); n.proc.stdin.flush()
        line = n.marker('TEST SNAPSHOTS ')
        n.marker('TEST COMMAND snapshots')
        a_before, a_after, b_before, b_after = json.loads(line[len('TEST SNAPSHOTS '):])
        self.assertEqual(a_before, a_after)
        self.assertEqual(b_before, b_after)
        self.assertNotEqual(a_before, b_before)
        self.assertEqual(n.host_state()['lobby']['host_counter'], counter)
        peer = n.peer('First', game_keepalive=True)
        self.denied(n, spool)
        publish(spool, gen, counter)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gameplay.frame(0x23))
        wait(lambda: peer.game_keepalives_sent > 0)
        event = json.loads(next(spool.glob('start-*.json')).read_bytes())
        self.assertEqual((event['session_generation'], event['host_counter']), (gen, counter))

    def test_real_countdown_is_cancelled_and_can_be_retried(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool, {'bot_countdownseconds': '1'})
        owner = n.peer('Owner', lan=True, game_keepalive=True)
        n.peer('Second', game_keepalive=True)
        owner.chat('!forcestart')
        wait(lambda: n.state()['phase'] == 'COUNTDOWN')
        wait(lambda: any('waiting for match reservation' in chat for chat in owner.chats()), 5)
        self.assertEqual(n.state()['phase'], 'LOBBY')
        self.assertEqual(n.host_state()['active_games'], 0)
        self.assertEqual(list(spool.glob('start-*.json')), [])
        publish(spool, gen, counter)
        n.command('unlimit')
        owner.chat('!forcestart')
        wait(lambda: n.host_state()['active_games'] == 1, 5)
        self.assertEqual(json.loads(next(spool.glob('start-*.json')).read_bytes())['host_counter'], counter)

    def test_rehost_invalidates_old_counter(self):
        spool = self.spool()
        n, gen, old_counter = self.launch(spool, {'autohost_maxgames': '2', 'autohost_startplayers': '6',
                                                  'autohost_gamename': 'Same map', 'autohost_owner': 'Owner'}, manual=False)
        peer = n.peer('First', game_keepalive=True)
        publish(spool, gen, old_counter)
        n.command('rehosterror')
        wait(lambda: n.host_state()['lobby']['host_counter'] != old_counter)
        counter = n.host_state()['lobby']['host_counter']
        self.denied(n, spool)
        self.assertFalse(peer.disconnected)
        publish(spool, gen, counter)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        self.assertEqual(json.loads(next(spool.glob('start-*.json')).read_bytes())['host_counter'], counter)

    def test_denied_next_lobby_keeps_running_game_alive(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool, {'bot_maxgames': '2', 'autohost_maxgames': '2',
                                               'autohost_startplayers': '6', 'autohost_gamename': 'Same map',
                                               'autohost_owner': 'Owner'})
        first = n.peer('First', game_keepalive=True)
        publish(spool, gen, counter)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        first.sock.sendall(gameplay.frame(0x23))
        wait(lambda: first.game_keepalives_sent > 0)
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        n.peer('Second', game_keepalive=True)
        before = first.game_keepalives_sent
        self.denied(n, spool)
        second_counter = n.host_state()['lobby']['host_counter']
        publish(spool, gen, second_counter)
        spool.chmod(0o500)
        try:
            self.denied(n, spool)
        finally:
            spool.chmod(0o700)
        wait(lambda: first.game_keepalives_sent > before)
        self.assertFalse(first.disconnected)
        self.assertEqual(n.host_state()['active_games'], 1)

    def test_journal_write_failure_does_not_kill_active_game(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool, {'bot_maxgames': '2', 'autohost_maxgames': '2',
                                               'autohost_startplayers': '6', 'autohost_gamename': 'Same map',
                                               'autohost_owner': 'Owner'})
        first = n.peer('First', game_keepalive=True)
        publish(spool, gen, counter)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        first.sock.sendall(gameplay.frame(0x23))
        wait(lambda: first.game_keepalives_sent > 0)
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        counter2 = n.host_state()['lobby']['host_counter']
        n.peer('Second', game_keepalive=True)
        publish(spool, gen, counter2)
        before = first.game_keepalives_sent
        # Limit only the driver's file writes; marker validation/read/fsync is
        # still allowed. SIGXFSZ is ignored in the test driver so the journal
        # write fails with EFBIG, exercising the real native error path.
        n.command('journalfault')
        n.command('startlobby')
        n.command('journalrestore')
        wait(lambda: n.host_state()['lobby'] is None)
        self.assertTrue(any('lifecycle journal write failed' in line for line in n.lines))
        self.assertTrue(any('external publication stop failed' in line for line in n.lines))
        self.assertIsNone(n.proc.poll())
        self.assertEqual(n.host_state()['active_games'], 1)
        self.assertEqual(len(list(spool.glob('start-*.json'))), 1)
        # An uncertain start write must never produce proof of no start.
        self.assertFalse((spool/f'close-{gen}-{counter2}.json').exists())
        wait(lambda: first.game_keepalives_sent > before)
        self.assertFalse(first.disconnected)
        # A legacy enable command must not clear the latched journal failure.
        n.command('enablehost')
        n.command('trycreate')
        n.command('autohost')
        self.assertIsNone(n.host_state()['lobby'])
        self.assertEqual(n.host_state()['active_games'], 1)

    def test_gate_counter_exhaustion_never_wraps_to_old_reservation(self):
        spool = self.spool()
        n, gen, old_counter = self.launch(spool, {'autohost_maxgames': '2', 'autohost_startplayers': '6',
                                                  'autohost_gamename': 'Same map', 'autohost_owner': 'Owner'}, manual=False)
        n.peer('First')
        publish(spool, gen, old_counter)
        n.command('counteroverflow')
        n.command('rehosterror')
        wait(lambda: n.host_state()['lobby']['host_counter'] == 0)
        self.denied(n, spool)
        n.command('rehosterror')
        self.assertEqual(n.host_state()['lobby']['host_counter'], 0)
        self.denied(n, spool)

    def test_supervisor_crash_before_and_after_rename_and_native_restart(self):
        spool = self.spool()
        n, gen, counter = self.launch(spool)
        peer = n.peer('First', game_keepalive=True)
        marker = marker_path(spool, gen, counter)
        temp = spool / '.gate-test.tmp'
        state = Path(self.temp.name) / 'reservation-state'
        code = '''
import os,signal,sys
state,temp,marker,generation,counter,phase=sys.argv[1:]
fd=os.open(state,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); os.write(fd,b'durable reservation'); os.fsync(fd); os.close(fd)
d=os.open(os.path.dirname(state),os.O_RDONLY|os.O_DIRECTORY); os.fsync(d); os.close(d)
fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
os.write(fd,('version=1\\nsession_generation='+generation+'\\nhost_counter='+counter+'\\n').encode()); os.fsync(fd); os.close(fd)
if phase=='rename': os.rename(temp,marker)
os.kill(os.getpid(),signal.SIGKILL)
'''
        def child(phase):
            result = subprocess.run([sys.executable, '-c', code, str(state), str(temp), str(marker), gen, str(counter), phase], timeout=5)
            self.assertEqual(result.returncode, -signal.SIGKILL)
        child('temp')
        self.assertTrue(temp.exists()); self.assertFalse(marker.exists())
        self.denied(n, spool)
        child('rename')
        self.assertEqual(state.read_bytes(), b'durable reservation')
        self.assertTrue(marker.exists())
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        peer.sock.sendall(gameplay.frame(0x23))
        self.kill_native(n)
        n2, new_gen, new_counter = self.launch(spool)
        self.assertNotEqual(new_gen, gen)
        self.assertEqual(new_counter, counter)
        n2.peer('Next')
        self.denied(n2, spool)
        self.assertTrue(marker.exists())

    def test_gate_config_requires_valid_journal_and_legacy_default_starts(self):
        for value in ('1', 'true', 'broken'):
            with self.subTest(value=value):
                n = self.native({'bot_start_reservation_gate': value}).start(ready=False)
                self.assertEqual(n.proc.wait(timeout=6), 78)
                n.thread.join(2)
                self.assertTrue(any('invalid start reservation gate configuration' in line for line in n.lines))
        n = self.native().start().create()
        n.peer('First')
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)


if __name__ == '__main__':
    unittest.main()
