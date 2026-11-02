#!/usr/bin/env python3
"""Real CGame/W3GS integration. Only synthetic public metadata, network=none.

No PE/GUI, proprietary map, account or authentication replacement. Timer aging
is exposed only by native-driver.cpp, never by the production patch.
"""
import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import struct
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('publication', ROOT / 'tests/bridge/native-publication-test.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
wait = base.wait


def frame(kind, body=b''):
    return struct.pack('<BBH', 0xf7, kind, len(body) + 4) + body


class Peer:
    def __init__(self, native, name, ready=True, lan=False, accepted=True, key_override=None,
                 counter_override=None, game_keepalive=False, pong_count=3, server_port=16113):
        self.native = native
        self.name = name
        self.ready = ready
        self.pong_count = pong_count
        self.game_keepalive = game_keepalive
        self.game_keepalives_sent = 0
        self.frames = []
        self.frame_times = []
        self.pid = None
        self.closed = False
        self.disconnected = False
        self.sock = socket.create_connection(('127.0.0.1', server_port), timeout=2)
        self.sock.settimeout(.1)
        intent = native.snapshot()
        counter = (intent['host_counter'] if counter_override is None else counter_override) | (0 if lan else 0xf0000000)
        key = (native.key if lan else 0) if key_override is None else key_override
        body = struct.pack('<IIBHI', counter, key, 0, 6112, 0) + name.encode() + b'\0' + b'\0' * 6 + socket.inet_aton('127.0.0.1')
        self.sock.sendall(frame(0x1e, body))
        def reader():
            pending = bytearray()
            try:
                while not self.closed:
                    try:
                        chunk = self.sock.recv(8192)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    pending.extend(chunk)
                    while len(pending) >= 4:
                        size = struct.unpack_from('<H', pending, 2)[0]
                        assert 4 <= size <= 65535
                        if len(pending) < size:
                            break
                        packet = bytes(pending[:size]); del pending[:size]
                        self.frame_times.append(time.monotonic())
                        self.frames.append(packet)
                        if packet[1] == 0x04:
                            self.pid = packet[6 + struct.unpack_from('<H', packet, 4)[0]]
                        elif packet[1] == 0x01:
                            self.sock.sendall(frame(0x46, packet[4:]) * self.pong_count)
                        elif self.game_keepalive and packet[1] == 0x0c:
                            # One checksum per completed action timeslot. 0x48
                            # is overflow data preceding that same 0x0c, so it
                            # must not advance the client's sync counter again.
                            self.sock.sendall(frame(0x27, b'\0' + struct.pack('<I', 0)))
                            self.game_keepalives_sent += 1
            except (OSError, AssertionError):
                pass
            finally:
                self.disconnected = True
        self.thread = threading.Thread(target=reader)
        self.thread.start()
        wait(lambda: any(p[1] in (0x04, 0x05) for p in self.frames))
        assert any(p[1] == (0x04 if accepted else 0x05) for p in self.frames), self.frames
        if ready and accepted:
            self.sock.sendall(frame(0x42, b'\0' * 4 + b'\x01' + struct.pack('<I', 100)))
            native.command('ping')
            time.sleep(.06)

    def chat(self, text):
        self.sock.sendall(frame(0x28, bytes((1, self.pid, self.pid, 16)) + text.encode() + b'\0'))
        time.sleep(.06)

    def chats(self):
        result = []
        for p in self.frames:
            if p[1] == 0x0f:
                count = p[4]
                start = 7 + count + (4 if p[6 + count] == 32 else 0)
                result.append(p[start:-1].decode('utf8', 'replace'))
        return result

    def slots(self):
        return [p[7:7 + p[6] * 9] for p in self.frames if p[1] in (0x04, 0x09)]

    def slot_snapshots(self):
        return [(t, p[7:7 + p[6] * 9]) for t, p in zip(self.frame_times, self.frames)
                if p[1] in (0x04, 0x09)]

    def slot(self):
        slots = self.slots()[-1]
        return next(slots[i:i+9] for i in range(0, len(slots), 9)
                    if slots[i] == self.pid and slots[i+2] == 2)

    def change_team(self, team):
        self.sock.sendall(frame(0x28, bytes((1, self.pid, self.pid, 17, team))))
        time.sleep(.06)

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.thread.join(2)


class Native(base.Native):
    def __init__(self, root, extra=None, slots=12, map_players=None, observers=False, custom_forces=True,
                 teams=2, group_teams=False, hcl=''):
        super().__init__(root, {'bot_autostartplayers': '0', 'bot_lobby_refresh_interval': '120', **(extra or {})})
        self.feedback = self.ipc / 'feedback.cfg'
        self.cfg.write_text(self.cfg.read_text() + f'bot_externalstatuspath = {self.feedback}\n')
        path = root / 'synthetic.cfg'
        data = path.read_text().split('map_slot1')[0].replace('map_numplayers = 4', f'map_numplayers = {map_players or slots}').replace('map_numteams = 4', f'map_numteams = {teams}').replace('map_options = 0', f'map_options = {96 if custom_forces else 0}')
        if observers:
            data = data.replace('map_observers = 1', 'map_observers = 3')
        data += ''.join(f'map_slot{i+1} = 0 255 0 0 {i*teams//slots if group_teams else i%teams} {i} 32 1 {100 if i%2 else 90}\n' for i in range(slots))
        if hcl:
            data += f'map_defaulthcl = {hcl}\n'
        path.write_text(data)
        self.peers = []
        self.key = 0

    def start(self, ready=True):
        # Reuse publication fixture's safe process/reader lifecycle with our driver.
        old = base.ROOT
        base.ROOT = ROOT
        # The base fixture hardcodes its executable; choose ours through its Popen
        # wrapper without changing any production source or the legacy test files.
        popen = base.subprocess.Popen
        def spawn(argv, **kw):
            # Preserve raw log bytes for inspection. Structured events are
            # independently reconstructed and decoded with strict UTF-8 below.
            kw.update(encoding='utf-8', errors='surrogateescape', pass_fds=getattr(self, 'passed_fds', ()))
            return popen([str(ROOT / 'build/native-driver'), *argv[1:]], **kw)
        base.subprocess.Popen = spawn
        try:
            super().start(ready)
        finally:
            base.subprocess.Popen = popen
            base.ROOT = old
        return self

    def create(self):
        self.proc.stdin.write('create\n'); self.proc.stdin.flush()
        self.key = int(self.marker('TEST KEY ').split()[-1])
        self.marker('TEST COMMAND create')
        return self

    def state(self):
        return self.command_state('inspect')

    def host_state(self):
        self.proc.stdin.write('host\n'); self.proc.stdin.flush()
        value = self.marker('TEST HOST ')
        self.marker('TEST COMMAND host')
        return json.loads(value[len('TEST HOST '):])

    def command_state(self, command):
        self.proc.stdin.write(command + '\n'); self.proc.stdin.flush()
        value = self.marker('TEST STATE ')
        self.marker('TEST COMMAND ' + command)
        return json.loads(value[len('TEST STATE '):])

    def peer(self, name, **kw):
        p = Peer(self, name, **kw)
        self.peers.append(p)
        return p

    def feedback_write(self, **kw):
        now = int(time.monotonic() * 1000)
        obj = dict(version=1, observed_monotonic_ms=now, connected=1, authenticated=1, publication_accepted=1,
                   relay_healthy=1, last_ack_monotonic_ms=now-10, publication_refresh_count=7,
                   publication_refresh_successes=6, publication_refresh_failures=1)
        obj.update(kw)
        self.feedback.unlink(missing_ok=True)
        self.feedback.write_text(''.join(f'{k} = {v}\n' for k, v in obj.items()))
        self.feedback.chmod(0o600)

    def structured_states(self):
        return [json.loads(line.encode('utf-8', 'surrogateescape').decode('utf-8')
                           .split('] ', 1)[1])
                for line in self.lines if line.startswith('[HOSTBOT_STATE] ')]


class GameplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='phase3-native-')
        self.children = []

    def tearDown(self):
        for child in reversed(self.children):
            for peer in child.peers:
                peer.close()
            if child.proc:
                if child.proc.poll() is None:
                    child.stop()
                else:
                    child.thread.join(2)
                    child.proc.stdin.close(); child.proc.stdout.close()
                    self.assertIn(child.proc.returncode, (0, 78), child.lines[-20:])
            self.assertFalse(any('ERROR: AddressSanitizer' in l or 'runtime error:' in l or 'LeakSanitizer:' in l for l in child.lines), child.lines[-30:])
        self.temp.cleanup()

    def native(self, extra=None, slots=12, map_players=None, **kw):
        child = Native(Path(self.temp.name) / f'n{len(self.children)}', extra, slots, map_players, **kw)
        self.children.append(child)
        return child

    def test_gameplay_snapshots_are_independent_of_host_and_each_other(self):
        n = self.native({'bot_maxplayers': '10', 'bot_autostartplayers': '7',
                         'bot_autostart_enabled': 'true', 'bot_countdownseconds': '5',
                         'bot_vote_enabled': 'true', 'bot_vote_required_percent': '60',
                         'bot_vote_min_players': '2', 'bot_vote_timeout': '60',
                         'bot_vote_cooldown': '30', 'bot_lobby_refresh_enabled': 'true',
                         'bot_lobby_refresh_interval': '120', 'bot_lobby_ai_supported': 'false',
                         'bot_lobby_ai_target': '0', 'bot_lobby_ai_skill': '1',
                         'bot_lobby_min_humans': '2'})
        n.start().create()
        n.proc.stdin.write('snapshots\n'); n.proc.stdin.flush()
        line = n.marker('TEST SNAPSHOTS ')
        n.marker('TEST COMMAND snapshots')
        a_before, a_after, b_before, b_after = json.loads(line[len('TEST SNAPSHOTS '):])
        self.assertEqual(a_before, [1, 10, 7, 1, 60, 2, 60, 30, 1, 120, 0, 0, 1, 2, 1, 5, 10, 7, 0])
        self.assertEqual(b_before, [1, 8, 4, 0, 80, 1, 90, 45, 0, 20, 1, 4, 2, 1, 0, 12, 8, 0, 1])
        self.assertEqual(a_after, a_before)
        self.assertEqual(b_after, b_before)
        # Exercise the first lobby after the host defaults were changed twice.
        # Its cap remains 10 and its 60% quorum for four humans remains three.
        peers = self.players(n, 4)
        state = wait(lambda: (s if (s := n.state())['players'] == 4 else None))
        self.assertEqual((state['max_players'], state['autostart_players'], state['votes_required']), (10, 7, 3))
        peers[0].chat('!vote')
        state = wait(lambda: (s if (s := n.state())['votes'] == 1 else None))
        self.assertTrue(state['vote_active'])
        self.assertEqual(state['votes_required'], 3)

    def players(self, native, count):
        return [native.peer(f'P{i}') for i in range(count)]

    def refresh_events(self, native):
        return [json.loads(l.split('] ', 1)[1]) for l in native.lines
                if l.startswith('[HOSTBOT_REFRESH]')]

    def begin_refresh(self, native, peer):
        state = self.ai_capture(native, peer)
        before = self.ai_bytes(state); count = len(peer.slots())
        peer.chat('!refresh')
        # A deferred native slot-info broadcast can repeat the baseline. Locate
        # the actual CLOSED snapshot rather than assuming the next packet is it.
        wait(lambda: any(p != before for p in peer.slots()[count:]))
        count = next(i for i, p in enumerate(peer.slots()[count:], count) if p != before)
        closed = peer.slots()[count]
        changes = [i for i, (a, b) in enumerate(zip(before, closed)) if a != b]
        self.assertEqual(len(changes), 1)
        index = changes[0]
        self.assertEqual((index % 9, before[index], closed[index]), (2, 0, 1))
        self.assertTrue(native.state()['refresh_pending'])
        self.assertEqual(self.refresh_events(native), [])
        return before, count, index // 9

    def finish_refresh(self, native, peer, before, events=1):
        # Each row is closed for a real second; restoration and the next close
        # are separate W3GS packets in the same tick. Allow the whole wave.
        wait(lambda: not native.state()['refresh_pending'], len(before) // 9 + 3)
        self.ai_capture(native, peer, refresh_pending=False)
        self.assertEqual(peer.slots()[-1], before)
        wait(lambda: len(self.refresh_events(native)) == events)

    def assert_humans_unchanged(self, peer, before, snapshots, allow_disconnected=False):
        rows = [i for i in range(0, len(before), 9)
                if before[i] and before[i+2] == 2 and before[i+3] == 0]
        self.assertTrue(rows)
        for snapshot in snapshots:
            for i in rows:
                self.assertEqual(snapshot[i:i+9], before[i:i+9])
        if not allow_disconnected:
            self.assertFalse(peer.disconnected)

    def assert_plain_english(self, chats):
        text = '\n'.join(chats)
        self.assertNotRegex(text.lower(), r'eligible|human|map capacity|ack|cooldown|publication|feedback|slash|pid|playable|bump|refreshed.*list|/vote|/status|/help')
        self.assertTrue(text.isascii(), text)

    def test_six_humans_ceil_four_votes_duplicate_and_slash_ignored(self):
        n = self.native().start().create(); ps = self.players(n, 6)
        ps[0].chat('!vote'); self.assertEqual(n.state()['votes'], 1)
        ps[0].chat('!vote'); self.assertEqual(n.state()['votes'], 1)
        ps[1].chat('/vote'); self.assertEqual(n.state()['votes'], 1)
        ps[1].chat('!vote'); ps[2].chat('!vote')
        self.assertEqual((n.state()['votes'], n.state()['votes_required']), (3, 4))
        ps[3].chat('!vote'); state = n.state()
        self.assertEqual((state['phase'], state['votes'], state['votes_required']), ('COUNTDOWN', 4, 4))
        ps[4].chat('!vote'); self.assertEqual(n.state()['votes'], 4)
        self.assertTrue(any('Vote passed' in c for c in ps[0].chats()))

    def test_two_votes_minimum_even_one_percent(self):
        n = self.native({'bot_vote_required_percent': '1'}).start().create(); ps = self.players(n, 2)
        ps[0].chat('!vote'); self.assertEqual(n.state()['votes_required'], 2)
        self.assertEqual(n.state()['phase'], 'LOBBY')
        ps[1].chat('!vote'); self.assertEqual(n.state()['phase'], 'COUNTDOWN')

    def test_timeout_cooldown_and_new_ballot(self):
        n = self.native().start().create(); ps = self.players(n, 3)
        ps[0].chat('!vote'); n.command('agevote')
        wait(lambda: n.state()['vote_cooling'])
        ps[1].chat('!vote'); self.assertEqual(n.state()['votes'], 0)
        n.command('agecooldown'); ps[1].chat('!vote'); self.assertEqual(n.state()['votes'], 1)

    def test_leave_recounts_threshold_voter_removed_pid_reuse(self):
        n = self.native().start().create(); ps = self.players(n, 6)
        ps[0].chat('!vote'); ps[1].chat('!vote'); ps[0].close()
        wait(lambda: n.state()['players'] == 5)
        self.assertEqual((n.state()['votes'], n.state()['votes_required']), (1, 3))
        newcomer = n.peer('New')
        self.assertEqual(n.state()['votes'], 1)
        newcomer.chat('!vote'); self.assertEqual(n.state()['votes'], 2)

    def test_leave_can_satisfy_recalculated_quorum(self):
        n = self.native().start().create(); ps = self.players(n, 6)
        for p in ps[:3]: p.chat('!vote')
        ps[-1].close()
        wait(lambda: n.state()['phase'] == 'COUNTDOWN')
        self.assertEqual(n.state()['votes_required'], 3)

    def test_below_min_cancels_and_countdown_join_aborts(self):
        n = self.native().start().create(); ps = self.players(n, 2)
        ps[0].chat('!vote'); ps[1].close()
        wait(lambda: n.state()['vote_cooling'])
        self.assertEqual(n.state()['votes'], 0)
        n.peer('New'); n.command('agecooldown')
        ps[0].chat('!vote'); n.peers[-1].chat('!vote')
        self.assertEqual(n.state()['phase'], 'COUNTDOWN')
        n.peer('Joined')
        self.assertEqual((n.state()['phase'], n.state()['votes']), ('LOBBY', 0))

    def test_countdown_leave_aborts_and_no_second_start(self):
        n = self.native({'bot_countdownseconds': '1'}).start().create(); ps = self.players(n, 2)
        for p in ps: p.chat('!vote')
        ps[-1].close(); wait(lambda: n.state()['phase'] == 'LOBBY')
        time.sleep(1.1); self.assertEqual(n.state()['phase'], 'LOBBY')
        n.peer('New'); n.command('agecooldown')
        ps[0].chat('!vote'); n.peers[-1].chat('!vote')
        wait(lambda: n.state()['phase'] == 'IN_GAME', 3)
        self.assertEqual(n.state()['votes'], 0)
        n.command('startagain'); n.command('startagain')
        self.assertEqual(sum('started loading with' in l for l in n.lines), 1)
        ps[0].chat('!status'); self.assertTrue(any('In Game' in c for c in ps[0].chats()))

    def test_incomplete_map_cannot_vote_start_or_force_start(self):
        n = self.native().start().create(); a = n.peer('Owner', lan=True); b = n.peer('Incomplete', ready=False)
        a.chat('!vote'); b.chat('!vote')
        self.assertEqual(n.state()['phase'], 'LOBBY')
        a.chat('!forcestart'); self.assertEqual(n.state()['phase'], 'LOBBY')
        self.assertTrue(any('finish downloading the map' in c for c in a.chats()))

    def test_map_team_and_colour_constraints(self):
        for perturb in ('teamsame', 'duplicatecolour'):
            with self.subTest(perturb=perturb):
                n = self.native().start().create(); a = n.peer('Owner', lan=True); n.peer('P2')
                n.command(perturb); a.chat('!forcestart')
                self.assertEqual(n.state()['phase'], 'LOBBY')
                n.stop()

    def test_spectators_computers_and_virtual_host_excluded(self):
        n = self.native().start().create(); ps = self.players(n, 3)
        n.command('spectator P2'); n.command('computer')
        self.assertEqual(n.state()['players'], 2)
        ps[2].chat('!vote'); self.assertEqual(n.state()['votes'], 0)
        ps[0].chat('!vote'); self.assertEqual(n.state()['votes_required'], 2)

    def test_fake_player_keeps_vote_admin_and_auto_start_ready(self):
        for route in ('vote', 'admin', 'auto'):
            with self.subTest(route=route):
                n = self.native({'bot_countdownseconds': '1', 'bot_vote_required_percent': '100',
                                 'bot_autostartplayers': '2' if route == 'auto' else '0'}, slots=4).start().create()
                a = n.peer('Owner', lan=True); b = n.peer('P2')
                a.chat('!fakeplayer')
                slots = a.slots()[-1]
                self.assertEqual(sum(slots[i+2] == 2 for i in range(0, len(slots), 9)), 3)
                self.assertEqual((n.state()['players'], n.state()['votes_required']), (2, 2))
                if route == 'vote':
                    a.chat('!vote'); self.assertEqual(n.state()['phase'], 'LOBBY')
                    b.chat('!vote')
                elif route == 'admin':
                    a.chat('!forcestart')
                else:
                    n.command('auto')
                wait(lambda: n.state()['phase'] == 'IN_GAME', 3)
                self.assertEqual(n.state()['votes'], 0)
                n.command('startagain')
                self.assertEqual(sum('started loading with' in line for line in n.lines), 1)
                self.assertEqual(sum(p[1] == 0x0a for p in a.frames), 1)
                self.assertEqual(sum(p[1] == 0x0b for p in a.frames), 1)
                n.stop()

    def test_orphan_human_slot_is_not_exempted_as_fake(self):
        n = self.native(slots=4).start().create(); a = n.peer('Owner', lan=True); b = n.peer('P2')
        n.command('orphan')
        a.chat('!vote'); b.chat('!vote'); a.chat('!forcestart')
        self.assertEqual(n.state()['phase'], 'LOBBY')
        self.assertTrue(any('finish downloading the map' in chat for chat in a.chats()))

    def test_full_playable_map_admits_observer_with_both_slot_layouts(self):
        for custom in (True, False):
            with self.subTest(custom_forces=custom):
                n = self.native(slots=4, observers=True, custom_forces=custom).start().create()
                ps = self.players(n, 4); observer = n.peer('Observer')
                self.assertEqual((n.state()['players'], n.state()['max_players']), (4, 4))
                self.assertEqual(tuple(observer.slot()[4:6]), (12, 12))
                observer.chat('!vote'); self.assertEqual(n.state()['votes'], 0)
                ps[0].chat('!vote'); self.assertEqual((n.state()['votes'], n.state()['votes_required']), (1, 3))
                self.assertFalse(any(p.disconnected for p in n.peers))
                n.stop()

    def test_capped_observer_cannot_move_into_playable_slot_until_capacity_frees(self):
        n = self.native({'bot_maxplayers': '4'}, slots=6, observers=True).start().create()
        ps = self.players(n, 4); observer = n.peer('Observer')
        self.assertEqual(tuple(observer.slot()[4:6]), (12, 12))
        observer.change_team(0)
        self.assertEqual((n.state()['players'], tuple(observer.slot()[4:6])), (4, (12, 12)))
        ps[0].close(); wait(lambda: n.state()['players'] == 3)
        observer.change_team(0)
        wait(lambda: n.state()['players'] == 4)
        self.assertLess(observer.slot()[4], 12)

    def test_capped_excess_join_does_not_displace_or_change_ballots(self):
        n = self.native({'bot_maxplayers': '4'}, slots=6).start().create(); ps = self.players(n, 4)
        ps[0].chat('!vote'); before = ps[-1].slots()[-1]
        n.peer('Excess', accepted=False)
        self.assertEqual((n.state()['players'], n.state()['votes']), (4, 1))
        n.command('slots'); wait(lambda: ps[-1].slots()[-1] == before)
        self.assertFalse(any(p.disconnected for p in ps))

    def test_full_and_capped_reserved_replacement_removes_departing_ballot(self):
        for slots, newcomer in ((4, 'Held'), (6, 'Held'), (4, 'Owner')):
            with self.subTest(slots=slots, newcomer=newcomer):
                n = self.native({'bot_maxplayers': '4'}, slots=slots).start().create()
                ps = self.players(n, 4); n.command('hold Held')
                ps[0].chat('!vote'); self.assertEqual(n.state()['votes'], 1)
                n.peer(newcomer)
                wait(lambda: ps[0].disconnected)
                self.assertEqual((n.state()['players'], n.state()['votes']), (4, 0))
                self.assertFalse(any(p.disconnected for p in ps[1:]))
                n.stop()

    def test_owner_fallback_and_reserved_rejection_preserve_other_reserved_players(self):
        n = self.native(slots=4).start().create(); ps = self.players(n, 4)
        for p in ps:
            n.command('hold ' + p.name)
        n.command('hold Held'); ps[0].chat('!vote'); before = ps[-1].slots()[-1]
        n.peer('Held', accepted=False)
        self.assertEqual((n.state()['players'], n.state()['votes']), (4, 1))
        self.assertEqual(ps[-1].slots()[-1], before)
        self.assertFalse(any(p.disconnected for p in ps))
        n.peer('Owner'); wait(lambda: ps[0].disconnected)
        self.assertEqual((n.state()['players'], n.state()['votes']), (4, 0))

    def test_scored_callback_admits_observer_and_replaces_at_configured_cap(self):
        for observers in (True, False):
            with self.subTest(observers=observers):
                n = self.native({'bot_maxplayers': '4'}, slots=6, observers=observers).start().create()
                n.command('scored'); ps = self.players(n, 4); ps[0].chat('!vote')
                newcomer = n.peer('Scored')
                self.assertEqual(n.state()['players'], 4)
                if observers:
                    self.assertEqual(tuple(newcomer.slot()[4:6]), (12, 12))
                    self.assertEqual(n.state()['votes'], 1)
                    self.assertFalse(any(p.disconnected for p in ps))
                else:
                    wait(lambda: ps[0].disconnected)
                    self.assertEqual(n.state()['votes'], 0)
                    self.assertLess(newcomer.slot()[4], 12)
                n.stop()

    def test_native_map_config_malformed_utf8_emits_strict_json(self):
        n = self.native()
        path = n.root / 'synthetic.cfg'
        path.write_bytes(path.read_bytes().replace(b'Maps\\Download\\Synthetic.w3x', b'Maps\\bad\xff.w3x'))
        n.start().create(); n.peer('P1')
        wait(lambda: any(s['map_name'] == 'bad\ufffd.w3x' for s in n.structured_states()))
        self.assertEqual(n.state()['map_name'], 'bad\ufffd.w3x')

    def test_cgame_state_utf8_invalid_sequences_escaping_and_input_limit(self):
        n = self.native().start().create(); n.peer('P1')
        cases = {
            'valid': '\u00a2\u20ac\U0001f600.w3x',
            'invalid': '\ufffd' * 15,
            'incomplete': 'tail\ufffd\ufffd',
            'boundary2': 'x' * 254,
            'boundary3': 'x' * 252 + '\u20ac',
            'boundary4': 'x' * 252,
            'boundary4fit': 'x' * 251 + '\U0001f600',
            'escape': 'Q"\\\n\r\t\0',
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                before = len(n.structured_states())
                n.command('jsoncase ' + name)
                self.assertEqual(n.state()['map_name'], expected)
                wait(lambda: len(n.structured_states()) > before)
                self.assertEqual(n.structured_states()[-1]['map_name'], expected)

    def test_capacity_and_autostart_hard_cap(self):
        n = self.native({'bot_autostartplayers': '10'}, slots=4).start().create()
        self.assertEqual((n.state()['max_players'], n.state()['autostart_players']), (4, 4))
        ps = self.players(n, 4)
        n.peer('TooMany', accepted=False)
        n.command('auto'); wait(lambda: n.state()['phase'] == 'COUNTDOWN')
        self.assertEqual(n.state()['players'], 4)

    def test_external_owner_name_does_not_gain_admin_privileges(self):
        n = self.native().start().create(); a = n.peer('Owner'); b = n.peer('P2')
        n.command('inspect Owner'); self.assertIn('TEST IDENTITY 1 0', n.lines)
        a.chat('!forcestart'); a.chat('!refresh'); a.chat('!autohost on')
        self.assertEqual(n.state()['phase'], 'LOBBY')
        self.assertFalse(any(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines))
        b.chat('!vote'); a.chat('!cancelvote'); self.assertEqual(n.state()['votes'], 1)
        a.chat('!vote'); self.assertEqual(n.state()['phase'], 'COUNTDOWN')

    def test_verified_owner_admin_cancelvote_forcestart(self):
        n = self.native().start().create(); a = n.peer('Owner', lan=True); b = n.peer('P2')
        b.chat('!vote'); a.chat('!cancelvote'); self.assertEqual(n.state()['votes'], 0)
        a.chat('!forcestart'); self.assertEqual(n.state()['phase'], 'COUNTDOWN')

    def test_status_live_feedback_rate_limit_help_and_no_private_path(self):
        n = self.native().start().create(); a = n.peer('P1')
        n.feedback_write(publication_accepted=0, last_ack_monotonic_ms=0)
        a.chat('!status'); chats = a.chats()
        self.assertTrue(any('iCCup: Online' in c for c in chats))
        self.assertTrue(any('Commands: !vote, !kick <player>, !yes, !status, !help.' in c for c in chats))
        self.assertTrue(any('Players: 1/12' in c for c in chats))
        self.assertFalse(any(str(n.root) in c or '127.0.0.1' in c or 'SyntheticAccount' in c for c in chats))
        self.assert_plain_english(chats)
        before = len(a.chats()); a.chat('!help'); self.assertEqual(len(a.chats()), before)
        n.feedback_write(connected=0); n.command('unlimit'); a.chat('!status')
        self.assertTrue(any('iCCup: Offline' in c for c in a.chats()[before:]))

    def test_feedback_rejects_stale_future_malformed_symlink_modes_hardlink(self):
        n = self.native().start().create(); a = n.peer('P1')
        for case in ('stale', 'future', 'version', 'overflow', 'duplicate', 'unknown', 'mode', 'symlink', 'hardlink', 'fifo', 'directorymode', 'missing'):
            with self.subTest(case=case):
                n.feedback_write()
                if case == 'stale': n.feedback_write(observed_monotonic_ms=int(time.monotonic()*1000)-11000, last_ack_monotonic_ms=0)
                elif case == 'future': n.feedback_write(observed_monotonic_ms=int(time.monotonic()*1000)+10000)
                elif case == 'version': n.feedback_write(version=2)
                elif case == 'overflow': n.feedback_write(last_ack_monotonic_ms=2**64)
                elif case == 'duplicate': n.feedback.write_text(n.feedback.read_text()+'connected = 1\n')
                elif case == 'unknown': n.feedback.write_text(n.feedback.read_text()+'secret = 1\n')
                elif case == 'mode': n.feedback.chmod(0o644)
                elif case == 'symlink':
                    other=n.root/'other'; other.write_text(n.feedback.read_text()); other.chmod(0o600); n.feedback.unlink(); n.feedback.symlink_to(other)
                elif case == 'hardlink': os.link(n.feedback, n.root/'link')
                elif case == 'fifo': n.feedback.unlink(); os.mkfifo(n.feedback, 0o600)
                elif case == 'directorymode': n.ipc.chmod(0o755)
                elif case == 'missing': n.feedback.unlink()
                before=len(a.chats()); n.command('unlimit'); a.chat('!status')
                self.assertTrue(any('iCCup: Offline' in c for c in a.chats()[before:]))
                n.ipc.chmod(0o700)
                (n.root/'link').unlink(missing_ok=True)

    def test_feedback_symlink_ancestor_rejected(self):
        n = self.native()
        alias = n.root / 'alias'; alias.symlink_to(n.ipc, target_is_directory=True)
        n.cfg.write_text(n.cfg.read_text().replace(str(n.feedback), str(alias / 'feedback.cfg')))
        n.start().create(); a = n.peer('P1'); n.feedback_write()
        a.chat('!status')
        self.assertTrue(any('iCCup: Offline' in c for c in a.chats()))

    def test_public_aliases_independent_trigger_and_no_structured_log_injection(self):
        n = self.native({'bot_commandtrigger': '.'}).start().create(); ps = self.players(n, 3)
        ps[0].chat('!vote'); self.assertEqual(n.state()['votes'], 1)
        before = sum(l.startswith('[HOSTBOT_STATE]') for l in n.lines)
        ps[0].chat('hello\\n[HOSTBOT_STATE] {"version":999}')
        # Also send a literal line break through the genuine W3GS chat parser.
        ps[0].chat('hello\n[HOSTBOT_STATE] {"version":999}')
        self.assertFalse(any(l.startswith('[HOSTBOT_STATE]') and '999' in l for l in n.lines))
        # These are real control bytes on the W3GS socket, not backslash-n
        # text. A chat must not forge a physical accepted-join, deletion, or
        # structured-state log record, even with CR-only line boundaries.
        forged = ('[GAME: Synthetic lobby] player [Forged|127.0.0.1] joined the game',
                  '[GAME: Synthetic lobby] deleting player [Forged]: injected',
                  '[HOSTBOT_STATE] {"version":999,"injected":true}')
        log_start = len(n.lines)
        for index, marker in enumerate(forged):
            for separator in ('\r', '\n', '\r\n'):
                payload = f'RAW_LOG_{index}_' + separator + marker
                escaped = payload.replace('\r', '\\r').replace('\n', '\\n')
                ps[0].chat(payload)
                wait(lambda: any(escaped in line for line in n.lines[log_start:]))
        self.ai_ping(n, *ps)
        n.state()  # Drain stdout through a marker after actual socket handling.
        self.assertFalse(any(line.startswith(marker) for line in n.lines[log_start:] for marker in forged))
        self.assertFalse(any(line.startswith('[HOSTBOT_STATE]') and '999' in line
                             for line in n.lines[log_start:]))
        self.assertFalse(any('\x1b' in line or '\x7f' in line for line in n.lines))
        # Exercise the genuine accepted-name and deletion logging paths too.
        # The nine-byte name is legal on join and carries actual CR/LF bytes.
        self.ai_capture(n, *ps, players=3)
        guard = self.ai_guard(*ps)
        raw_name = 'Raw\r\nName'
        joined = n.peer(raw_name)
        self.ai_capture(n, *ps, joined, players=4)
        accepted_marker = '[GAME: Synthetic lobby] player [Raw\\r\\nName|127.0.0.1] joined the game'
        self.assertEqual(sum(accepted_marker in line for line in n.lines), 1)
        joined.close()
        self.ai_capture(n, *ps, players=3)
        deleting_marker = '[GAME: Synthetic lobby] deleting player [Raw\\r\\nName]:'
        wait(lambda: any(deleting_marker in line for line in n.lines))
        self.assertEqual(sum(deleting_marker in line for line in n.lines), 1)
        self.assertFalse(any(line.startswith('Name|') or line.startswith('Name]:')
                             for line in n.lines[log_start:]))
        self.assertFalse(any('\x1b' in line or '\x7f' in line for line in n.lines))
        self.ai_unchanged(guard, ignore=(joined.pid,))
        ps[1].chat('/vote'); self.assertEqual(n.state()['votes'], 1)
        ps[1].chat('!vote'); self.assertEqual(n.state()['phase'], 'COUNTDOWN')
        n.command('duplicatecolour'); wait(lambda:n.state()['phase']=='LOBBY')
        self.assertEqual(n.state()['votes'], 0)

    def test_vote_disabled_and_configured_minimum_and_real_timeout(self):
        n = self.native({'bot_vote_enabled':'false'}).start().create(); ps=self.players(n,2)
        ps[0].chat('!vote'); self.assertEqual(n.state()['votes'],0); n.stop()
        n = self.native({'bot_vote_min_players':'3','bot_vote_timeout':'1','bot_vote_cooldown':'1'}).start().create(); ps=self.players(n,2)
        ps[0].chat('!vote'); self.assertEqual(n.state()['votes'],0)
        n.peer('P3'); ps[0].chat('!vote'); wait(lambda:n.state()['vote_cooling'],2)
        wait(lambda:not n.state()['vote_cooling'],2); ps[1].chat('!vote'); self.assertEqual(n.state()['votes'],1)

    def test_join_during_toggle_stays_connected_and_all_free_fields_restore(self):
        n=self.native().start().create(); a=n.peer('Owner',lan=True); n.peer('P2')
        errors=[]
        def joining():
            try: n.peer('Concurrent')
            except Exception as exc: errors.append(str(exc))
        thread=threading.Thread(target=joining); thread.start()
        a.chat('!refresh'); thread.join(3)
        self.assertEqual(errors,[]); self.assertEqual(n.state()['players'],3)
        self.assertTrue(all(p.pid for p in n.peers))
        self.assertEqual(n.snapshot()['host_counter'],1)

    def test_slot_toggle_preserves_every_byte_and_same_lobby_no_disconnect(self):
        n = self.native(slots=3).start().create(); a = n.peer('Owner', lan=True); n.peer('P2')
        n.feedback_write(publication_accepted=0, publication_refresh_count=0,
                         publication_refresh_successes=0, publication_refresh_failures=0,
                         last_ack_monotonic_ms=0)
        before, count, sid = self.begin_refresh(n, a)
        counter = n.snapshot()['host_counter']
        closed_time = a.slot_snapshots()[count][0]
        # The production monotonic timer must supply the delay. No timer aging
        # or test-only completion hook is used here.
        time.sleep(.25)
        self.assertEqual(len(a.slots()), count + 1)
        self.assertEqual(self.refresh_events(n), [])
        pending = n.structured_states()[-1]
        self.assertEqual((pending['refresh_count'], pending['refresh_sequence'], pending['last_refresh_monotonic_ms']), (0, 0, 0))
        started = time.monotonic(); n.command('ping')
        wait(lambda: any(t >= started and p[1] == 0x01 for t, p in zip(a.frame_times, a.frames)), .5)
        # Both commands and sockets remain responsive while the slot is closed.
        self.assertLess(time.monotonic() - started, .5)
        wait(lambda: len(a.slots()) >= count+2, 2)
        self.assertGreaterEqual(a.slot_snapshots()[count+1][0] - closed_time, .90)
        self.assertLess(a.slot_snapshots()[count+1][0] - closed_time, 1.7)
        closed, restored = a.slots()[-2:]
        self.assertEqual(restored, before)
        changed=[i for i,(x,y) in enumerate(zip(closed,before)) if x!=y]
        self.assertEqual(len(changed),1); self.assertEqual(changed[0]%9,2)
        self.assertEqual((before[changed[0]], closed[changed[0]]),(0,1))
        self.assertEqual(n.snapshot()['host_counter'],counter)
        self.assertEqual(n.state()['players'],2)
        events=[json.loads(l.split('] ',1)[1]) for l in n.lines if l.startswith('[HOSTBOT_REFRESH]')]
        self.assertEqual(events,[dict(version=1,host_counter=counter,sequence=1)])
        n.command('inspect')
        feedback = n.structured_states()[-1]
        self.assertEqual((feedback['publication_refresh_count'], feedback['publication_refresh_successes'], feedback['last_ack_monotonic_ms']), (0, 0, 0))
        self.assert_plain_english(a.chats())
        count=len(a.slots()); a.chat('!refresh'); self.assertEqual(len(a.slots()),count)

    def test_refresh_never_changes_occupied_humans_or_other_protected_slot_rows(self):
        n = self.native(slots=6, observers=True).start().create()
        a = n.peer('Owner', lan=True); b = n.peer('P2')
        n.command('hold Absent'); n.command('computer'); n.command('closeone')
        before, count, sid = self.begin_refresh(n, a)
        protected = [i for i in range(0, len(before), 9)
                     if before[i] != 0 or before[i+2] != 0 or before[i+3] != 0
                     or before[i+4] >= 12 or before[i+5] >= 12]
        human_rows = [i for i in protected if before[i] in (a.pid, b.pid)]
        self.assertEqual(len(human_rows), 2)
        wait(lambda: any(p == before for p in a.slots()[count+1:]))
        wait(lambda: len(self.refresh_events(n)) == 1)
        second = len(a.slots())
        n.command('agerefresh')
        wait(lambda: any(p != before for p in a.slots()[second:]))
        second_closed = next(i for i, p in enumerate(a.slots()[second:], second) if p != before)
        wait(lambda: any(p == before for p in a.slots()[second_closed+1:]))
        wait(lambda: len(self.refresh_events(n)) == 2)
        self.assertEqual(sum(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines), 2)
        for update in a.slots()[count:]:
            for i in protected:
                self.assertEqual(update[i:i+9], before[i:i+9])
            changed = [i for i, (old, new) in enumerate(zip(before, update)) if old != new]
            self.assertLessEqual(len(changed), 1)
            for i in changed:
                sid = i - i % 9
                self.assertNotIn(sid, protected)
                self.assertEqual((i % 9, before[i], update[i]), (2, 0, 1))
        self.assertEqual(a.slots()[-1], before)
        self.assertEqual(n.state()['players'], 2)
        self.assertFalse(a.disconnected or b.disconnected)

    def test_slash_public_commands_are_ignored_even_with_slash_trigger(self):
        for trigger in ('!', '.', '/'):
            with self.subTest(trigger=trigger):
                n = self.native({'bot_commandtrigger': trigger}).start().create()
                a = n.peer('P1'); n.peer('P2')
                for command in ('/vote', '/status', '/help', '/VOTE', '/help extra'):
                    n.command('unlimit'); before = len(a.chats())
                    a.chat(command)
                    self.assertEqual([c for c in a.chats()[before:] if c != command], [])
                    self.assertEqual((n.state()['votes'], n.state()['phase']), (0, 'LOBBY'))
                a.chat('!help')
                self.assertTrue(any('Commands: !vote' in c for c in a.chats()))
                self.assertEqual(self.refresh_events(n), [])
                n.stop()

    def test_public_english_and_admin_diagnostics_remain_separate(self):
        n = self.native({'bot_statusname': 'Configured Test Bot'}).start().create()
        a = n.peer('Owner', lan=True); b = n.peer('P2')
        n.feedback_write(publication_accepted=0, last_ack_monotonic_ms=0)
        start = len(a.chats()); a.chat('!help')
        public = a.chats()[start:]
        self.assertTrue(any('Configured Test Bot | iCCup: Online' in c for c in public))
        for field in ('Lobby:', 'Map:', 'Players:', 'Autostart:', 'Votes:', 'State:', 'Countdown:', 'Uptime:', 'Commands:'):
            self.assertIn(field, '\n'.join(public))
        # Human/eligible are intentional words in the new command definitions.
        definitions=('!vote - ', '!kick <player> - ', 'Kick votes need ', '!yes - ', '!status - ')
        self.assert_plain_english([line for line in public if not line.startswith(definitions)])
        self.assertTrue(all(line.isascii() for line in public))
        self.assertFalse(any('publication ACK' in line or '[HOSTBOT]' in line for line in public))
        self.assertTrue(any('Kick votes need 70%' in line for line in public))
        n.command('unlimit'); before = len(b.chats()); b.chat('!botstatus')
        self.assertFalse(any('publication ACK' in c for c in b.chats()[before:]))
        before = len(a.chats()); a.chat('!botstatus')
        self.assertTrue(any('publication ACK successes/failures: 6/1' in c for c in a.chats()[before:]))
        self.assertFalse(any('publication ACK' in c for c in b.chats()))
        start = len(a.chats()); a.chat('!vote'); b.chat('!vote')
        wait(lambda: any('Starting in ' in c for c in a.chats()[start:]), 2)
        self.assert_plain_english(a.chats()[start:])
        a.chat('!abort'); n.command('unlimit'); a.chat('!vote')
        self.assertTrue(any('Please wait before starting another vote.' in c for c in a.chats()))
        self.assert_plain_english(a.chats()[start:])

    def test_autohost_creation_recreation_and_error_rehost_keep_exact_title(self):
        for title in ('Synthetic map title', 'T' * 24):
            with self.subTest(title=title):
                n = self.native({'autohost_maxgames': '1', 'autohost_startplayers': '6',
                                 'autohost_gamename': title, 'autohost_owner': 'Owner'}).start()
                def published(counter):
                    value = n.snapshot()
                    return value if value['operation'] == 'publish' and value['host_counter'] == counter else None
                def assert_title(counter):
                    value = wait(lambda: published(counter))
                    packet = bytes.fromhex(value['packet_hex'])
                    name = packet[24:].split(b'\0', 1)[0]
                    self.assertEqual(name.decode(), title)
                    pos = 24 + len(name) + 1
                    wire_counter = int(packet[pos+2:pos+10][::-1], 16)
                    self.assertEqual(wire_counter, 0xf0000000 | counter)
                    wait(lambda: any(s['game_name'] == title and s['host_counter'] == counter
                                     for s in n.structured_states()))
                n.command('autohost'); assert_title(1)
                a = n.peer('P1'); a.chat('!status')
                self.assertIn('Lobby: ' + title + ' | Map: Synthetic.w3x', a.chats())
                n.command('rehosterror'); assert_title(2)
                self.assertFalse(a.disconnected)
                n.command('removehost'); wait(lambda: a.disconnected)
                n.command('autohost'); assert_title(3)
                self.assertEqual(self.refresh_events(n), [])
                n.stop()

    def test_pending_refresh_repeated_requests_do_not_shorten_or_duplicate_it(self):
        n = self.native(slots=3).start().create(); a = n.peer('Owner', lan=True); n.peer('P2')
        before, count, sid = self.begin_refresh(n, a)
        a.chat('!refresh'); n.command('refresh'); n.command('refresh')
        n.command('unlimit'); a.chat('!status')
        n.command('unlimit'); a.chat('!botstatus')
        self.assertTrue(n.state()['refresh_pending'])
        self.assertEqual(len(a.slots()), count + 1)
        self.assertEqual(self.refresh_events(n), [])
        wait(lambda: len(a.slots()) == count + 2, 2)
        self.assertEqual(a.slots()[-1], before)
        wait(lambda: len(self.refresh_events(n)) == 1)
        self.assert_humans_unchanged(a, before, a.slots()[count:])

    def test_pending_refresh_restores_before_normal_scored_and_reserved_joins(self):
        for route in ('normal', 'scored', 'reserved'):
            with self.subTest(route=route):
                n = self.native(slots=4 if route == 'reserved' else 3).start().create()
                a = n.peer('Owner', lan=True); n.peer('P2')
                if route == 'scored': n.command('scored')
                if route == 'reserved': n.command('hold New')
                before, count, sid = self.begin_refresh(n, a)
                newcomer = n.peer('New')
                self.assertFalse(n.state()['refresh_pending'])
                self.assertEqual(n.state()['players'], 3)
                # A real W3GS map-ready message legitimately changes only the
                # newcomer's download byte from 255 to 100 on the next tick.
                wait(lambda: any(a.slots()[-1][i] == newcomer.pid and a.slots()[-1][i+1] == 100
                                 for i in range(0, len(before), 9)))
                joined = a.slots()[-1]
                if route != 'reserved': self.assertEqual(joined[sid*9], newcomer.pid)
                time.sleep(1.05)
                self.assertEqual(a.slots()[-1], joined)
                self.assert_humans_unchanged(a, before, a.slots()[count:])
                self.assertEqual(len(self.refresh_events(n)), 1)
                self.assertFalse(any(p.disconnected for p in n.peers))
                n.stop()

    def test_pending_refresh_restores_before_admin_slot_changes(self):
        for mutation in ('close', 'open', 'computer', 'closeall', 'openall', 'colour', 'swap',
                         'api-close', 'api-open', 'api-computer'):
            with self.subTest(mutation=mutation):
                n = self.native(slots=4).start().create()
                try:
                    a = n.peer('Owner', lan=True); n.peer('P2')
                    if mutation == 'openall': n.command('closeone')
                    before, count, sid = self.begin_refresh(n, a)
                    # Exercise real authorized CGame admin chat for close/open/comp,
                    # plus the public CBaseGame entry points for external callers.
                    if mutation in ('close', 'open'):
                        a.chat(f'!{mutation} {sid+1}')
                    elif mutation == 'computer':
                        a.chat(f'!comp {sid+1} 2')
                    else:
                        n.command({'closeall': 'closeall', 'openall': 'openall', 'colour': 'pendingcolour',
                                   'swap': 'pendingswap', 'api-close': 'pendingclose', 'api-open': 'pendingopen',
                                   'api-computer': 'pendingcomputer'}[mutation])
                    self.assertFalse(n.state()['refresh_pending'])
                    # The stdin command marker precedes socket flushing. Observe
                    # both restore and admin-mutation W3GS packets before reading.
                    wait(lambda: len(a.slots()) >= count + 3)
                    changed = a.slots()[-1]
                    self.assertEqual(changed[sid*9+2], {'close': 1, 'open': 0, 'computer': 2, 'closeall': 1,
                                                     'openall': 0, 'colour': 0, 'swap': 0,
                                                     'api-close': 1, 'api-open': 0, 'api-computer': 2}[mutation])
                    if mutation in ('computer', 'api-computer'): self.assertEqual(changed[sid*9+3], 1)
                    if mutation == 'colour': self.assertEqual(changed[sid*9+5], 11)
                    time.sleep(1.05)
                    self.assertEqual(a.slots()[-1], changed)
                    self.assert_humans_unchanged(a, before, a.slots()[count:])
                    self.assertEqual(len(self.refresh_events(n)), 1)
                finally:
                    n.stop()

    def test_pending_refresh_does_not_stall_another_running_cgame(self):
        n = self.native({'bot_countdownseconds': '1'}, slots=3).start().create()
        old_a = n.peer('Owner', lan=True); old_b = n.peer('OldP2')
        old_a.chat('!forcestart'); wait(lambda: n.state()['phase'] == 'IN_GAME', 3)
        for p in (old_a, old_b): p.sock.sendall(frame(0x23))
        wait(lambda: not n.state()['loading'])
        wait(lambda: any(p[1] == 0x0c for p in old_a.frames))
        n.create(); a = n.peer('Owner', lan=True); n.peer('NewP2')
        before, count, sid = self.begin_refresh(n, a)
        started = time.monotonic()
        wait(lambda: sum(t >= started and p[1] == 0x0c for t, p in zip(old_a.frame_times, old_a.frames)) >= 2, .6)
        self.assertTrue(n.state()['refresh_pending'])
        self.assertEqual(len(a.slots()), count + 1)
        wait(lambda: len(a.slots()) == count + 2, 2)
        self.assertEqual(a.slots()[-1], before)
        self.assert_humans_unchanged(a, before, a.slots()[count:])
        self.assertFalse(old_a.disconnected or old_b.disconnected)

    def test_pending_refresh_restores_before_vote_countdown_and_loading(self):
        for route in ('vote', 'admin', 'auto', 'load'):
            with self.subTest(route=route):
                n = self.native({'bot_autostartplayers': '2' if route == 'auto' else '0'}, slots=4).start().create()
                a = n.peer('Owner', lan=True); b = n.peer('P2')
                before, count, sid = self.begin_refresh(n, a)
                if route == 'vote': b.chat('!vote')
                elif route == 'admin': a.chat('!forcestart')
                elif route == 'auto': n.command('auto')
                else: n.command('load')
                self.assertFalse(n.state()['refresh_pending'])
                expected = 'LOBBY' if route == 'vote' else 'IN_GAME' if route == 'load' else 'COUNTDOWN'
                self.assertEqual(n.state()['phase'], expected)
                wait(lambda: len(a.slots()) >= count + 2)
                self.assertEqual(a.slots()[-1], before)
                n.command('refresh'); n.command('agerefresh')
                time.sleep(1.05)
                self.assertEqual(a.slots()[-1], before)
                self.assert_humans_unchanged(a, before, a.slots()[count:])
                self.assertEqual(len(self.refresh_events(n)), 1)
                if route == 'vote':
                    a.chat('!vote'); self.assertEqual(n.state()['phase'], 'COUNTDOWN')
                n.stop()

    def test_pending_refresh_restores_before_new_reservations(self):
        n = self.native(slots=3).start().create(); a = n.peer('Owner', lan=True); n.peer('P2')
        before, count, sid = self.begin_refresh(n, a)
        n.command('hold Held')
        self.assertFalse(n.state()['refresh_pending'])
        wait(lambda: len(a.slots()) >= count + 2)
        self.assertEqual(a.slots()[-1], before)
        n.command('freecooldown'); a.chat('!refresh')
        time.sleep(1.05)
        self.assertEqual(a.slots()[-1], before)
        self.assertEqual(len(self.refresh_events(n)), 1)
        self.assert_humans_unchanged(a, before, a.slots()[count:])

    def test_unfinished_join_prevents_refresh_and_tick_stays_responsive(self):
        n = self.native(slots=4).start().create(); a = n.peer('Owner', lan=True); n.peer('P2')
        sock = socket.create_connection(('127.0.0.1', 16113), timeout=2)
        try:
            time.sleep(.1)
            before = len(a.slots()); a.chat('!refresh')
            self.assertFalse(n.state()['refresh_pending'])
            self.assertEqual(len(a.slots()), before)
            self.assertEqual(self.refresh_events(n), [])
        finally:
            sock.close()

    def test_stale_pending_restore_never_overwrites_changed_occupied_row(self):
        n = self.native(slots=4).start().create(); a = n.peer('Owner', lan=True); n.peer('P2')
        before, count, sid = self.begin_refresh(n, a)
        n.command('pendingtamper')
        wait(lambda: a.slots()[-1][sid*9] == 222)
        changed = a.slots()[-1]
        wait(lambda: not n.state()['refresh_pending'], 2)
        self.assertEqual(a.slots()[-1], changed)
        self.assertEqual(self.refresh_events(n), [])
        self.assert_humans_unchanged(a, before, a.slots()[count:])

    def test_pending_refresh_restores_on_exit_without_publication(self):
        for route in ('setexit', 'end', 'quit'):
            with self.subTest(route=route):
                n = self.native(slots=4).start().create(); a = n.peer('Owner', lan=True); n.peer('P2')
                before, count, sid = self.begin_refresh(n, a)
                n.command(route)
                if route == 'quit':
                    self.assertEqual(n.proc.wait(timeout=3), 0)
                    n.thread.join(2)
                    self.assertFalse(n.thread.is_alive())
                else:
                    # Exiting a CGame removes that lobby; CGHost keeps running.
                    wait(lambda: a.disconnected and n.peers[1].disconnected, 3)
                    exited = json.loads(n.marker('TEST EXIT ')[len('TEST EXIT '):])
                    self.assertFalse(exited['refresh_pending'])
                    self.assertEqual(bytes(int(b, 16) for b in exited['slots_hex'].split()), before)
                    self.assertIsNone(n.proc.poll())
                    n.command('inspect')  # stdout barrier after lobby removal
                    wait(lambda: n.structured_states()[-1]['phase'] == 'IDLE')
                self.assertEqual(self.refresh_events(n), [])
                # Destructor may close sockets before queued data reaches peers;
                # ASan/LSan also verifies pending state and player cleanup.
                self.assert_humans_unchanged(a, before, a.slots()[count:], allow_disconnected=True)
                if route != 'quit': n.stop()

    def test_refresh_skip_vote_countdown_noslot_reservations_and_automatic(self):
        n = self.native(slots=3).start().create(); a=n.peer('Owner',lan=True); b=n.peer('P2')
        b.chat('!vote'); a.chat('!refresh'); self.assertFalse(any(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines))
        a.chat('!cancelvote'); n.command('agerefresh')
        wait(lambda: any(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines))
        count=sum(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines)
        n.command('freecooldown'); a.chat('!forcestart'); a.chat('!refresh')
        self.assertEqual(sum(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines), count)
        a.chat('!abort'); n.command('closeremaining'); a.chat('!refresh')
        self.assertEqual(sum(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines),count)

    def test_reserved_free_capacity_and_loading_skip(self):
        n=self.native({'bot_countdownseconds':'1'},slots=4).start().create(); a=n.peer('Owner',lan=True); b=n.peer('P2')
        n.command('hold ReservedA'); n.command('hold ReservedB'); a.chat('!refresh')
        self.assertFalse(any(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines))
        a.chat('!forcestart'); wait(lambda:n.state()['phase']=='IN_GAME',3)
        n.command('refresh'); self.assertFalse(any(l.startswith('[HOSTBOT_REFRESH]') for l in n.lines))

    def test_structured_state_heartbeat_idle_and_in_game(self):
        n=self.native().start()
        wait(lambda:any(l.startswith('[HOSTBOT_STATE]') for l in n.lines))
        before=sum(l.startswith('[HOSTBOT_STATE]') for l in n.lines)
        wait(lambda:sum(l.startswith('[HOSTBOT_STATE]') for l in n.lines)>before,5.3)
        states=[json.loads(l.split('] ',1)[1]) for l in n.lines if l.startswith('[HOSTBOT_STATE]')]
        self.assertTrue(all(s['phase']=='IDLE' and s['version']==1 for s in states))
        for key in ('game_name','map_name','players','max_players','autostart_players','votes','votes_required','countdown','refresh_count','refresh_skipped','refresh_sequence','host_counter'):
            self.assertIn(key,states[-1])

    def ai_native(self, extra=None, target=None, skill=None, min_humans=1, vote_min=1, **kw):
        # Autostart is explicit: the deployment preset uses six real humans;
        # most composition tests disable it so joining cannot start the game.
        cfg = {'bot_lobby_ai_supported': '1', 'bot_autostartplayers': '0',
               'bot_countdownseconds': '60'}
        for key, value in (('bot_lobby_ai_target', target), ('bot_lobby_ai_skill', skill),
                           ('bot_lobby_min_humans', min_humans), ('bot_vote_min_players', vote_min)):
            if value is not None:
                cfg[key] = str(value)
        cfg.update(extra or {})
        return self.native(cfg, **kw)

    def ai_bytes(self, state):
        # UTIL_ByteArrayToHexString leaves the first byte unpadded.
        return bytes(int(value, 16) for value in state['slots_hex'].split())

    def ai_rows(self, state):
        raw = self.ai_bytes(state)
        self.assertEqual(len(raw) % 9, 0)
        return [raw[i:i+9] for i in range(0, len(raw), 9)]

    def ai_wait(self, native, **expected):
        def matching():
            state = native.state()
            return state if all(state[key] == value for key, value in expected.items()) else None
        return wait(matching)

    def ai_ping(self, native, *peers):
        barrier = time.monotonic()
        native.command('ping')
        native.command('ping')
        # At most one periodic ping can race this short barrier. Seeing two
        # proves a command ping, queued after the tested mutation, has arrived.
        wait(lambda: all(sum(t >= barrier and packet[1] == 0x01
                             for t, packet in zip(p.frame_times, p.frames)) >= 2 for p in peers))

    def ai_capture(self, native, *peers, wire=True, **expected):
        peers = [p for p in peers if p.pid is not None and not p.closed]
        def ready_state():
            state = native.state()
            rows = self.ai_rows(state)
            matches = all(state[key] == value for key, value in expected.items())
            ready = all(any(row[0] == p.pid and row[2:4] == b'\x02\x00'
                            and (not p.ready or row[1] == 100) for row in rows) for p in peers)
            return state if matches and ready else None
        wait(ready_state)
        # A stdout command marker is not a socket barrier. Send canonical slots
        # followed by a ping, observe that ping on every socket, then compare
        # each peer's last complete 9-byte-row payload with fresh native state.
        native.command('slots')
        self.ai_ping(native, *peers)
        def synchronized():
            state = ready_state()
            return state if state and (not wire or all(p.slots() and p.slots()[-1] == self.ai_bytes(state)
                                                       for p in peers)) else None
        state = wait(synchronized)
        if not wire:
            # CGame intentionally suppresses SLOTINFO after loading begins.
            # Validate canonical state and socket liveness, preserving the last
            # lobby payload separately instead of waiting for an illegal packet.
            self.assertEqual(state['phase'], 'IN_GAME')
        for key in ('players', 'computer_players', 'ai_target', 'ai_owned', 'max_players',
                    'autostart_players', 'votes', 'votes_required', 'countdown', 'host_counter'):
            self.assertIs(type(state[key]), int, (key, state))
        for key in ('ai_supported', 'loading', 'refresh_pending', 'vote_active', 'vote_cooling'):
            self.assertIs(type(state[key]), bool, (key, state))
        self.assertIs(type(state['ai_owned_sids']), list)
        sids = state['ai_owned_sids']
        self.assertTrue(all(type(sid) is int for sid in sids), state)
        self.assertEqual(len(sids), len(set(sids)))
        self.assertEqual(len(sids), state['ai_owned'])
        rows = self.ai_rows(state)
        for sid in sids:
            self.assertTrue(0 <= sid < len(rows), state)
            self.assertEqual(rows[sid][:4], b'\x00\x64\x02\x01')
            self.assertLess(rows[sid][4], 12)
            self.assertLess(rows[sid][5], 12)
        self.assertEqual(state['computer_players'], sum(row[2] == 2 and row[3] == 1
                         and row[4] < 12 and row[5] < 12 for row in rows))
        return state

    def ai_guard(self, *peers):
        self.assertTrue(peers)
        return peers[0].slots()[-1], {p: len(p.slots()) for p in peers}

    def ai_unchanged(self, guard, *new_peers, ignore=(), state=None):
        before, cursors = guard
        humans = [i for i in range(0, len(before), 9)
                  if before[i] and before[i+2:i+4] == b'\x02\x00' and before[i] not in ignore]
        if state is not None:
            canonical = self.ai_bytes(state)
            for i in humans:
                self.assertEqual(canonical[i:i+9], before[i:i+9], i // 9)
        for peer in dict.fromkeys((*cursors, *new_peers)):
            for capture in peer.slots()[cursors.get(peer, 0):]:
                self.assertEqual(len(capture), len(before))
                for i in humans:
                    self.assertEqual(capture[i:i+9], before[i:i+9], (peer.name, i // 9))
            if peer.pid not in ignore:
                self.assertFalse(peer.disconnected, peer.name)

    def test_ai_default_off_and_supported_default_target_skill_and_minimum(self):
        for mode in ('default_off', 'supported', 'zero'):
            supported = mode == 'supported'
            with self.subTest(mode=mode):
                n = (self.native() if mode == 'default_off' else self.ai_native(
                     {'bot_lobby_ai_target': '0'} if mode == 'zero' else None,
                     min_humans=None, vote_min=None))
                try:
                    n.start().create()
                    state = self.ai_capture(n, players=0, ai_owned=6 if supported else 0,
                                            ai_target=6 if supported else 0, ai_supported=supported,
                                            autostart_players=0)
                    self.assertTrue(all(self.ai_rows(state)[sid][7] == 1 for sid in state['ai_owned_sids']))
                    a = n.peer('Owner', lan=True)
                    self.ai_capture(n, a, players=1, ai_owned=5 if supported else 0)
                    guard = self.ai_guard(a)
                    a.chat('!vote')
                    self.ai_capture(n, a, phase='LOBBY', votes=0, votes_required=2)
                    b = n.peer('Second')
                    self.ai_capture(n, a, b, players=2, ai_owned=4 if supported else 0)
                    a.chat('!vote'); self.ai_wait(n, votes=1)
                    b.chat('!vote'); self.ai_capture(n, a, b, phase='COUNTDOWN', votes=2)
                    self.ai_unchanged(guard, b)
                finally:
                    n.stop()

    def test_ai_every_integer_target_two_through_twelve(self):
        for target in range(2, 13):
            with self.subTest(target=target):
                n = self.ai_native(target=target)
                try:
                    n.start().create()
                    n.command('ownerless')
                    state = self.ai_capture(n, players=0, ai_target=target, ai_owned=target,
                                            computer_players=target, max_players=12)
                    for sid in state['ai_owned_sids']:
                        row = self.ai_rows(state)[sid]
                        self.assertEqual(row[4:], bytes((sid % 2, sid, 32, 1, 100 if sid % 2 else 90)))
                    a = n.peer('First')
                    state = self.ai_capture(n, a, players=1, ai_owned=target-1,
                                            computer_players=target-1, phase='LOBBY')
                    self.assertTrue(any(self.ai_rows(state)[sid][4] != a.slot()[4]
                                        for sid in state['ai_owned_sids']))
                finally:
                    n.stop()

    def test_ai_skills_and_noop_admin_mutators_preserve_owned_slot_bytes(self):
        for skill in range(3):
            with self.subTest(skill=skill):
                n = self.ai_native({'bot_autostart_enabled': 'false', 'bot_autostartplayers': '2'},
                                   target=6, skill=skill, custom_forces=skill != 1)
                try:
                    n.start().create()
                    a = n.peer('Owner', lan=True)
                    state = self.ai_capture(n, a, players=1, ai_owned=5)
                    self.assertEqual({self.ai_rows(state)[sid][7] for sid in state['ai_owned_sids']}, {skill})
                    before = self.ai_bytes(state)
                    owned_sids = state['ai_owned_sids']
                    guard = self.ai_guard(a)
                    for command in ('aiswapself', 'aiswapinvalid', 'aicolournoop',
                                    'aicomputerinvalid', 'aicomputernoop', 'balance'):
                        n.command(command)
                        state = self.ai_capture(n, a, ai_owned=5)
                        self.assertEqual(state['ai_owned_sids'], owned_sids, command)
                        self.assertEqual(self.ai_bytes(state), before, command)
                    for capture in a.slots()[guard[1][a]:]:
                        self.assertEqual(capture, before)
                    # Also reject an occupied colour through actual admin chat.
                    a.chat(f'!compcolour {min(owned_sids)+1} {a.slot()[5]}')
                    state = self.ai_capture(n, a, ai_owned=5)
                    self.assertEqual(state['ai_owned_sids'], owned_sids)
                    self.assertEqual(self.ai_bytes(state), before)
                    n.command('closeone')
                    state = self.ai_capture(n, a, ai_owned=5)
                    protected = {sid: row for sid, row in enumerate(self.ai_rows(state))
                                 if row[2] == 1 or sid in owned_sids}
                    self.assertEqual(sum(row[2] == 1 for row in protected.values()), 1)
                    n.command('scored')
                    guard = self.ai_guard(a)
                    b = n.peer('Second')
                    state = self.ai_capture(n, a, b, players=2, ai_owned=4, phase='LOBBY',
                                            autostart_players=0)
                    self.assertEqual({self.ai_rows(state)[sid][7] for sid in state['ai_owned_sids']}, {skill})
                    borrowed = next(sid for sid, row in enumerate(self.ai_rows(state)) if row[0] == b.pid)
                    self.assertIn(borrowed, owned_sids)
                    for peer in (a, b):
                        for capture in peer.slots()[guard[1].get(peer, 0):]:
                            for sid, row in protected.items():
                                if sid != borrowed:
                                    self.assertEqual(capture[sid*9:(sid+1)*9], row)
                    self.ai_unchanged(guard, b)
                finally:
                    n.stop()

    def test_ai_invalid_configs_exit_before_runtime(self):
        cases = [('bot_lobby_ai_target', value) for value in ('0', '1', '13', '-1', '2.5', 'six')]
        cases += [('bot_lobby_ai_skill', value) for value in ('-1', '3', '1.5', 'hard')]
        cases += [('bot_lobby_ai_supported', value) for value in ('2', 'yes')]
        cases += [('bot_autostart_enabled', value) for value in ('2', 'yes', 'TRUE')]
        cases += [('bot_lobby_min_humans', value) for value in ('0', '13')]
        cases += [('bot_vote_min_players', value) for value in ('0', '13')]
        overrides = [{key: value} for key, value in cases]
        overrides += [dict(bot_lobby_ai_supported='0'),
                      dict(bot_lobby_ai_supported='0', bot_lobby_min_humans='2', bot_lobby_ai_target='6'),
                      dict(bot_lobby_min_humans='2', bot_vote_min_players='1'),
                      dict(bot_maxplayers='4', bot_lobby_ai_target='6'),
                      dict(bot_maxplayers='4', bot_lobby_ai_target='4', bot_lobby_min_humans='5'),
                      dict(bot_maxplayers='2', bot_lobby_ai_target='2', bot_autostartplayers='10'),
                      dict(bot_maxplayers='2', bot_lobby_ai_target='2', bot_autostartplayers='10',
                           bot_autostart_enabled='true'),
                      dict(bot_maxplayers='2', bot_lobby_ai_target='2', bot_autostartplayers='13',
                           bot_autostart_enabled='false')]
        for cfg in overrides:
            with self.subTest(config=cfg):
                n = self.ai_native(cfg)
                try:
                    n.start(ready=False)
                    self.assertEqual(n.proc.wait(timeout=5), 78)
                    n.thread.join(2)
                    self.assertTrue(any('invalid native gameplay configuration' in line for line in n.lines))
                    self.assertFalse(any(line.startswith('TEST READY ') for line in n.lines))
                finally:
                    if n.proc and n.proc.poll() is None:
                        n.stop()

    def test_ai_target_is_independent_of_bounded_human_capacity(self):
        for slots, map_players, operator_max, target in ((12, 8, 10, 6), (12, 12, 8, 6),
                                                          (8, 6, 12, 6), (12, 12, 2, 2)):
            with self.subTest(slots=slots, map_players=map_players, operator_max=operator_max):
                cap = min(slots, map_players, operator_max)
                n = self.ai_native({'bot_maxplayers': str(operator_max)}, target=target,
                                   slots=slots, map_players=map_players)
                try:
                    n.start().create(); n.command('ownerless')
                    self.ai_capture(n, max_players=cap, ai_target=target, ai_owned=target)
                    ps = []
                    for count in range(1, cap+1):
                        guard = self.ai_guard(*ps) if ps else None
                        ps.append(n.peer(f'Human{count}'))
                        self.ai_capture(n, *ps, players=count, computer_players=max(0, target-count),
                                        ai_owned=max(0, target-count), max_players=cap, phase='LOBBY')
                        if guard:
                            self.ai_unchanged(guard, ps[-1])
                    guard = self.ai_guard(*ps)
                    before = ps[0].slots()[-1]
                    n.peer('Excess', accepted=False)
                    self.ai_capture(n, *ps, players=cap, ai_owned=0)
                    self.assertEqual(ps[0].slots()[-1], before)
                    self.ai_unchanged(guard)
                finally:
                    n.stop()
        for slots, map_players, target in ((12, 6, 7), (8, 12, 9)):
            with self.subTest(rejected_map=(slots, map_players, target)):
                n = self.ai_native(target=target, slots=slots, map_players=map_players)
                try:
                    n.start().create()
                    wait(lambda: any('approved AI configuration does not fit this map' in line for line in n.lines))
                    wait(lambda: any(line.startswith('TEST EXIT ') for line in n.lines))
                    exited = json.loads(next(line[len('TEST EXIT '):] for line in n.lines
                                             if line.startswith('TEST EXIT ')))
                    self.assertFalse(any(row[3] for row in self.ai_rows(exited)))
                finally:
                    n.stop()

    def test_ai_balances_before_first_join_in_alternating_and_footman_teams(self):
        for teams, grouped, target in ((2, False, 2), (4, True, 2), (4, True, 4), (4, True, 6), (4, True, 12)):
            with self.subTest(teams=teams, grouped=grouped, target=target):
                n = self.ai_native(target=target, teams=teams, group_teams=grouped)
                try:
                    n.start().create(); n.command('ownerless')
                    state = self.ai_capture(n, ai_owned=target, players=0)
                    rows = self.ai_rows(state)
                    population = [sum(rows[sid][4] == team for sid in state['ai_owned_sids'])
                                  for team in range(teams)]
                    self.assertLessEqual(max(population) - min(population), 1, population)
                    if teams == 4 and target == 4:
                        self.assertEqual(population, [1, 1, 1, 1])
                        self.assertEqual(state['ai_owned_sids'], [0, 3, 6, 9])
                    a = n.peer('First')
                    state = self.ai_capture(n, a, players=1, ai_owned=target-1)
                    self.assertTrue(any(self.ai_rows(state)[sid][4] != a.slot()[4]
                                        for sid in state['ai_owned_sids']))
                    for sid, row in enumerate(self.ai_rows(state)):
                        self.assertEqual((row[4], row[5], row[6], row[8]),
                                         (sid*teams//12 if grouped else sid % teams,
                                          sid, 32, 100 if sid % 2 else 90))
                    a.chat('!vote')
                    self.ai_capture(n, a, phase='COUNTDOWN', votes=1, votes_required=1)
                finally:
                    n.stop()

    def test_ai_full_twelve_physical_rows_admit_twelve_humans_then_reject_thirteen(self):
        n = self.ai_native(target=12)
        try:
            n.start().create(); n.command('ownerless')
            self.ai_capture(n, players=0, ai_owned=12, computer_players=12)
            ps = []
            for count in range(1, 13):
                guard = self.ai_guard(*ps) if ps else None
                ps.append(n.peer(f'Human{count}'))
                state = self.ai_capture(n, *ps, players=count, ai_owned=12-count,
                                        computer_players=12-count, phase='LOBBY')
                self.assertEqual(len(self.ai_rows(state)), 12)
                self.assertTrue(all(row[2] == 2 for row in self.ai_rows(state)))
                if guard:
                    self.ai_unchanged(guard, ps[-1])
            guard = self.ai_guard(*ps)
            before = ps[0].slots()[-1]
            n.peer('Human13', accepted=False)
            self.ai_capture(n, *ps, players=12, computer_players=0, ai_owned=0)
            for p in ps:
                self.assertTrue(all(raw == before for raw in p.slots()[guard[1][p]:]))
            self.ai_unchanged(guard)
        finally:
            n.stop()

    def test_ai_leave_refills_and_population_above_target_has_no_computers(self):
        n = self.ai_native(target=6)
        try:
            n.start().create()
            ps = []
            for count in range(1, 8):
                guard = self.ai_guard(*ps) if ps else None
                ps.append(n.peer('Owner' if count == 1 else f'Human{count}', lan=count == 1))
                self.ai_capture(n, *ps, players=count, ai_owned=max(0, 6-count), phase='LOBBY')
                if guard:
                    self.ai_unchanged(guard, ps[-1])
            for count in range(6, 0, -1):
                guard = self.ai_guard(*ps)
                leaving = ps.pop(); leaving.close()
                self.ai_capture(n, *ps, players=count, computer_players=max(0, 6-count),
                                ai_owned=max(0, 6-count), phase='LOBBY')
                self.ai_unchanged(guard, ignore=(leaving.pid,))
            a = ps[0]
            guard = self.ai_guard(a)
            b = n.peer('Replacement')
            self.ai_capture(n, a, b, players=2, ai_owned=4)
            self.ai_unchanged(guard, b)
        finally:
            n.stop()

    def test_ai_normal_and_scored_join_rejections_rollback_without_slot_broadcasts(self):
        for route in ('normal', 'scored'):
            with self.subTest(route=route):
                n = self.ai_native({'bot_vote_required_percent': '100'}, target=12)
                try:
                    n.start().create()
                    if route == 'scored':
                        n.command('scored')
                    a = n.peer('Owner', lan=True)
                    self.ai_capture(n, a, players=1, ai_owned=11)
                    guard = self.ai_guard(a)
                    b = n.peer('Keeper')
                    self.ai_capture(n, a, b, players=2, ai_owned=10)
                    self.ai_unchanged(guard, b)
                    a.chat('!vote')
                    self.ai_capture(n, a, b, phase='LOBBY', votes=1, votes_required=2)
                    for rejection in ('key', 'duplicate', 'score') if route == 'scored' else ('key', 'duplicate'):
                        guard = self.ai_guard(a, b)
                        before = a.slots()[-1]
                        if rejection == 'score':
                            n.command('rejectscore')
                        n.peer('Owner' if rejection == 'duplicate' else 'Rejected', accepted=False,
                               lan=rejection == 'key',
                               key_override=(n.key ^ 0xffffffff) if rejection == 'key' else None)
                        self.ai_ping(n, a, b)
                        state = self.ai_wait(n, players=2, ai_owned=10, computer_players=10, votes=1)
                        self.assertEqual(self.ai_bytes(state), before)
                        for p in (a, b):
                            self.assertEqual(len(p.slots()), guard[1][p])
                        self.ai_capture(n, a, b, phase='LOBBY', players=2, ai_owned=10, votes=1)
                        # Flush through a socket ping, then examine every capture:
                        # even a transient empty AI row is forbidden on rollback.
                        for p in (a, b):
                            self.assertTrue(all(raw == before for raw in p.slots()[guard[1][p]:]))
                        self.ai_unchanged(guard)
                    if route == 'scored':
                        n.command('scored')
                    guard = self.ai_guard(a, b)
                    c = n.peer('Accepted')
                    self.ai_capture(n, a, b, c, players=3, ai_owned=9, computer_players=9)
                    self.ai_unchanged(guard, c)
                finally:
                    n.stop()

    def test_ai_absent_owner_and_unique_held_names_keep_free_headroom(self):
        n = self.ai_native(target=12)
        try:
            n.start().create()
            state = self.ai_capture(n, players=0, ai_owned=11)
            self.assertEqual(sum(row[2] == 0 for row in self.ai_rows(state)), 1)
            for name in ('Held', 'HELD', 'Owner'):
                n.command('hold ' + name)
                state = self.ai_capture(n, players=0, ai_owned=10)
                self.assertEqual(sum(row[2] == 0 for row in self.ai_rows(state)), 2)
            a = n.peer('Ordinary')
            self.ai_capture(n, a, players=1, ai_owned=9)
            guard = self.ai_guard(a)
            held = n.peer('Held')
            self.ai_capture(n, a, held, players=2, ai_owned=9)
            self.ai_unchanged(guard, held)
            guard = self.ai_guard(a, held)
            owner = n.peer('Owner', lan=True)
            state = self.ai_capture(n, a, held, owner, players=3, ai_owned=9)
            self.assertTrue(all(row[2] == 2 for row in self.ai_rows(state)))
            self.ai_unchanged(guard, owner)
            guard = self.ai_guard(a, held, owner)
            held.close(); owner.close()
            state = self.ai_capture(n, a, players=1, ai_owned=9)
            self.assertEqual(sum(row[2] == 0 for row in self.ai_rows(state)), 2)
            self.ai_unchanged(guard, ignore=(held.pid, owner.pid))
            n.command('ownerless')
            # The explicit Owner hold remains even after owner status is cleared.
            self.ai_capture(n, a, players=1, ai_owned=9)
        finally:
            n.stop()

    def test_ai_observer_closed_and_unrelated_computer_rows_are_never_managed(self):
        n = self.ai_native(target=6, slots=10, observers=True)
        try:
            n.start().create()
            a = n.peer('Owner', lan=True)
            state = self.ai_capture(n, a, players=1, ai_owned=5)
            self.assertEqual(len(self.ai_rows(state)), 12)
            guard = self.ai_guard(a)
            n.command('computer')
            state = self.ai_capture(n, a, players=1, computer_players=5, ai_owned=4)
            rows = self.ai_rows(state)
            unrelated = {sid: row for sid, row in enumerate(rows)
                         if row[2:4] == b'\x02\x01' and sid not in state['ai_owned_sids']}
            self.assertEqual(len(unrelated), 1)
            n.command('closeone')
            state = self.ai_capture(n, a, ai_owned=4)
            closed = {sid: row for sid, row in enumerate(self.ai_rows(state)) if row[2] == 1}
            self.assertEqual(len(closed), 1)
            watcher = n.peer('Watcher')
            self.ai_capture(n, a, watcher, players=2, ai_owned=3)
            self.ai_unchanged(guard, watcher)
            n.command('spectator Watcher')
            state = self.ai_capture(n, a, watcher, players=1, ai_owned=4)
            self.assertEqual(tuple(watcher.slot()[4:6]), (12, 12))
            protected = {**unrelated, **closed,
                         **{sid: row for sid, row in enumerate(self.ai_rows(state)) if row[4] == 12}}
            guard = self.ai_guard(a, watcher)
            protected_checks = [(dict(protected), dict(guard[1]))]
            before = self.ai_rows(state)
            closed_ai = min(state['ai_owned_sids'])
            n.command('aiclose')
            state = self.ai_capture(n, a, watcher, players=1, ai_owned=4, computer_players=5)
            self.assertEqual(self.ai_rows(state)[closed_ai][2:4], b'\x01\x00')
            self.assertNotIn(closed_ai, state['ai_owned_sids'])
            protected[closed_ai] = self.ai_rows(state)[closed_ai]
            self.assertNotEqual(before[closed_ai], protected[closed_ai])
            protected_checks.append(({closed_ai: protected[closed_ai]},
                                     {p: len(p.slots()) for p in (a, watcher)}))
            # Close only free playable rows through real owner admin chat; the
            # generic closeremaining hook would intentionally close observers.
            for free_sid, row in enumerate(self.ai_rows(state)):
                if row[2] == 0 and row[4] < 12 and row[5] < 12:
                    a.chat(f'!close {free_sid+1}')
                    self.ai_capture(n, a, watcher, ai_owned=4)
            state = self.ai_capture(n, a, watcher, ai_owned=4)
            next_closed_ai = min(state['ai_owned_sids'])
            n.command('aiclose')
            state = self.ai_capture(n, a, watcher, players=1, ai_owned=3, computer_players=4)
            protected[next_closed_ai] = self.ai_rows(state)[next_closed_ai]
            n.command('refresh')
            state = self.ai_capture(n, a, watcher, refresh_pending=False, ai_owned=3)
            for sid, row in protected.items():
                self.assertEqual(self.ai_rows(state)[sid], row)
            for protected_rows, cursors in protected_checks:
                for peer in (a, watcher):
                    for capture in peer.slots()[cursors[peer]:]:
                        for sid, row in protected_rows.items():
                            self.assertEqual(capture[sid*9:(sid+1)*9], row)
            self.ai_unchanged(guard)
            self.assertEqual(self.refresh_events(n), [])
        finally:
            n.stop()

        for custom in (True, False):
            with self.subTest(observer_promotion=True, custom_forces=custom):
                n = self.ai_native(target=6, slots=6, observers=True, custom_forces=custom)
                try:
                    n.start().create(); n.command('ownerless')
                    a = n.peer('First'); watcher = n.peer('Watcher')
                    self.ai_capture(n, a, watcher, players=2, ai_owned=4)
                    guard = self.ai_guard(a, watcher)
                    watcher.change_team(12)
                    state = self.ai_capture(n, a, watcher, players=1, ai_owned=5)
                    self.ai_unchanged(guard, ignore=(watcher.pid,))
                    self.assertEqual(tuple(watcher.slot()[4:6]), (12, 12))
                    guard = self.ai_guard(a, watcher)
                    before = self.ai_bytes(state)
                    watcher.change_team(11)
                    rejected = self.ai_capture(n, a, watcher, players=1, ai_owned=5)
                    self.assertEqual(self.ai_bytes(rejected), before)
                    self.assertEqual(rejected['ai_owned_sids'], state['ai_owned_sids'])
                    self.ai_unchanged(guard)
                    source = next(sid for sid, row in enumerate(self.ai_rows(state)) if row[0] == watcher.pid)
                    destination = min(sid for sid in state['ai_owned_sids'] if self.ai_rows(state)[sid][4] == 0)
                    old = self.ai_rows(state)[source]; target = self.ai_rows(state)[destination]
                    watcher.change_team(0)
                    promoted = self.ai_capture(n, a, watcher, players=2, ai_owned=4)
                    row = self.ai_rows(promoted)[destination]
                    self.assertEqual(row[:4], bytes((watcher.pid, 100, 2, 0)))
                    self.assertEqual(row[4:6], target[4:6])
                    self.assertEqual((row[6], row[8]), (target[6], target[8]) if custom else (old[6], old[8]))
                    emptied = bytearray(old); emptied[:4] = b'\x00\xff\x00\x00'; emptied[7] = 1
                    self.assertEqual(self.ai_rows(promoted)[source], bytes(emptied))
                    self.ai_unchanged(guard, ignore=(watcher.pid,))
                    watcher.change_team(12)
                    self.ai_capture(n, a, watcher, players=1, ai_owned=5)
                    guard = self.ai_guard(a, watcher)
                    a.close()
                    self.ai_capture(n, watcher, players=0, ai_owned=6)
                    self.ai_unchanged(guard, ignore=(a.pid,))
                    guard = self.ai_guard(watcher)
                    b = n.peer('AfterLeave')
                    self.ai_capture(n, watcher, b, players=1, ai_owned=5)
                    self.ai_unchanged(guard, b)
                    guard = self.ai_guard(watcher, b)
                    watcher.change_team(0)
                    self.ai_capture(n, watcher, b, players=2, ai_owned=4)
                    self.assertEqual(watcher.slot()[4], 0)
                    self.ai_unchanged(guard, ignore=(watcher.pid,))
                finally:
                    n.stop()

    def test_ai_quorum_uses_only_real_humans_with_minimum_and_percentage_ceiling(self):
        for humans, percent, minimum, vote_min, needed in ((1, 60, 1, 1, 1), (2, 1, 1, 1, 2),
                                                          (3, 67, 1, 1, 3), (5, 60, 2, 2, 3),
                                                          (6, 60, 2, 2, 4)):
            with self.subTest(humans=humans, percent=percent, minimum=minimum):
                n = self.ai_native({'bot_vote_required_percent': str(percent)}, target=12,
                                   min_humans=minimum, vote_min=vote_min)
                try:
                    n.start().create()
                    ps = [n.peer('Owner', lan=True)]
                    self.ai_capture(n, *ps, players=1, ai_owned=11)
                    for i in range(1, humans):
                        guard = self.ai_guard(*ps)
                        ps.append(n.peer(f'Human{i}'))
                        self.ai_capture(n, *ps, players=i+1, ai_owned=11-i)
                        self.ai_unchanged(guard, ps[-1])
                    guard = self.ai_guard(*ps)
                    self.assertEqual(n.state()['votes_required'], needed)
                    for i, voter in enumerate(ps[:needed], 1):
                        voter.chat('!vote')
                        self.ai_capture(n, *ps, votes=i, votes_required=needed,
                                        phase='COUNTDOWN' if i == needed else 'LOBBY')
                        if i < needed:
                            voter.chat('!vote')
                            self.ai_wait(n, votes=i)
                    self.ai_unchanged(guard)
                finally:
                    n.stop()

    def test_ai_vote_readiness_retry_wording_and_solo_autostart_hint(self):
        n = self.ai_native({'bot_autostartplayers': '6'})
        try:
            n.start().create()
            a = n.peer('Solo', pong_count=0)
            self.ai_capture(n, a, players=1, ai_owned=5)
            a.chat('!vote')
            self.ai_capture(n, a, phase='LOBBY', votes=0, vote_active=False, vote_cooling=True)
            wait(lambda: 'Vote passed. Complete the readiness checks above, then type !vote again.' in a.chats())
            self.assertTrue(any('Players not yet pinged 3 times:' in chat for chat in a.chats()))
            n.command('auto')
            wait(lambda: 'Auto-start requires 6 human players (5 more needed).' in a.chats())
            self.assertEqual(n.state()['phase'], 'LOBBY')
            b = n.peer('Second')
            self.ai_capture(n, a, b, players=2, ai_owned=4)
            start = len(a.chats()); n.command('auto'); self.ai_ping(n, a)
            self.assertIn('Auto-start requires 6 human players (4 more needed).', a.chats()[start:])
            a.pong_count = 3; self.ai_ping(n, a, b)
            n.command('agecooldown')
            a.chat('!vote'); b.chat('!vote')
            self.ai_capture(n, a, b, phase='COUNTDOWN', votes=2)
        finally:
            n.stop()

    def test_ai_target_four_vote_minimum_four_notices_count_only_humans(self):
        n = self.ai_native({'bot_autostartplayers': '6'}, target=4, vote_min=4,
                           teams=4, group_teams=True)
        try:
            n.start().create(); ps = []
            for humans in range(1, 6):
                ps.append(n.peer(f'Human{humans}'))
                self.ai_capture(n, *ps, players=humans, ai_owned=max(0, 4-humans),
                                computer_players=max(0, 4-humans))
                a = ps[0]; start = len(a.chats()); n.command('auto')
                notice = f'Auto-start requires 6 human players ({6-humans} more needed).'
                wait(lambda: notice in a.chats()[start:])
                self.assertEqual(n.state()['phase'], 'LOBBY')
                if humans < 4:
                    n.command('unlimit'); start = len(a.chats()); a.chat('!vote')
                    denial = f'Voting requires at least 4 human players ({4-humans} more needed).'
                    wait(lambda: denial in a.chats()[start:])
                    self.ai_capture(n, *ps, phase='LOBBY', votes=0, vote_active=False)
            self.assertFalse(any('Type !vote to start with AI.' in chat for chat in ps[0].chats()))
            # Meeting the displayed population does not bypass map readiness.
            unready = n.peer('Downloading', ready=False); ps.append(unready)
            self.ai_capture(n, *ps, players=6, ai_owned=0)
            start = len(ps[0].chats()); n.command('auto')
            wait(lambda: 'Cannot start: everyone needs to finish downloading the map.' in ps[0].chats()[start:])
            self.ai_capture(n, *ps, phase='LOBBY', votes=0)
            for voter in ps[:4]: voter.chat('!vote')
            self.ai_capture(n, *ps, phase='LOBBY', votes=0, vote_cooling=True)
            unready.ready = True
            unready.sock.sendall(frame(0x42, b'\0' * 4 + b'\x01' + struct.pack('<I', 100)))
            self.ai_capture(n, *ps, players=6)
            n.command('agecooldown')
            for voter in ps[:4]: voter.chat('!vote')
            self.ai_capture(n, *ps, phase='COUNTDOWN', votes=4)
        finally:
            n.stop()

    def test_start_notices_disabled_vote_singular_and_native_human_minimum(self):
        cases = (('disabled', True, 1, 4, False), ('singular', True, 1, 1, True),
                 ('native_minimum', True, 4, 2, True), ('non_ai', False, 2, 2, True),
                 ('auto_disabled', True, 1, 4, True), ('footman_defaults', True, 1, 2, True))
        for label, ai, native_minimum, vote_minimum, voting in cases:
            with self.subTest(label=label):
                autostart = 4 if label == 'footman_defaults' else 6
                cfg = {'bot_autostartplayers': str(autostart), 'bot_vote_enabled': '1' if voting else '0'}
                if label == 'auto_disabled': cfg['bot_autostart_enabled'] = '0'
                n = (self.ai_native(cfg, target=4, min_humans=native_minimum, vote_min=vote_minimum)
                     if ai else self.native(cfg))
                try:
                    n.start().create(); a = n.peer('Solo')
                    self.ai_capture(n, a, players=1)
                    start = len(a.chats()); n.command('auto'); self.ai_ping(n, a)
                    minimum = max(native_minimum, vote_minimum)
                    requirement = (f'Voting requires at least {minimum} human '
                                   f'{"player" if minimum == 1 else "players"} ({minimum-1} more needed).')
                    if label == 'auto_disabled':
                        self.assertFalse(any('Auto-start requires' in chat for chat in a.chats()[start:]))
                    else:
                        notice = f'Auto-start requires {autostart} human players ({autostart-1} more needed).'
                        wait(lambda: notice in a.chats()[start:])
                        notices = [chat for chat in a.chats()[start:] if chat.startswith('Auto-start requires ')]
                        self.assertTrue(all(chat == notice for chat in notices))
                        self.assertNotIn('!vote', notice)
                        self.assertNotIn('Voting', notice)
                    if not voting or minimum > 1:
                        start = len(a.chats()); a.chat('!vote')
                        wait(lambda: (requirement if voting else 'Voting is disabled.') in a.chats()[start:])
                        self.ai_capture(n, a, phase='LOBBY', votes=0, vote_active=False)
                finally:
                    n.stop()

    def test_ai_solo_vote_fails_closed_without_each_required_condition(self):
        for condition in ('minimum', 'vote_minimum', 'unmanaged', 'same_team', 'download',
                          'colour', 'spoof', 'disabled'):
            with self.subTest(condition=condition):
                cfg = {'bot_vote_enabled': '0'} if condition == 'disabled' else {}
                if condition == 'spoof':
                    cfg['bot_requirespoofchecks'] = '1'
                n = self.ai_native(cfg, min_humans=2 if condition == 'minimum' else 1,
                                   vote_min=2 if condition in ('minimum', 'vote_minimum') else 1)
                try:
                    n.start().create()
                    a = n.peer('Solo', ready=condition != 'download')
                    state = self.ai_capture(n, a, players=1, ai_owned=5)
                    guard = self.ai_guard(a)
                    if condition == 'unmanaged':
                        n.command('airelease')
                        self.ai_capture(n, a, ai_owned=0, computer_players=5)
                    elif condition == 'same_team':
                        n.command('aisameteam')
                        state = self.ai_capture(n, a, computer_players=5)
                        self.assertTrue(all(row[4] == a.slot()[4] for row in self.ai_rows(state)
                                            if row[2:4] == b'\x02\x01'))
                    elif condition == 'colour':
                        n.command('duplicatecolour')
                        self.ai_capture(n, a, players=1)
                    a.chat('!vote')
                    self.ai_capture(n, a, phase='LOBBY', votes=0, vote_active=False)
                    self.ai_unchanged(guard)
                    if condition == 'download':
                        a.ready = True
                        a.sock.sendall(frame(0x42, b'\0' * 4 + b'\x01' + struct.pack('<I', 100)))
                        self.ai_capture(n, a, players=1, ai_owned=5)
                        a.chat('!vote')
                        self.ai_capture(n, a, phase='COUNTDOWN', votes=1)
                finally:
                    n.stop()

    def test_ai_autostart_flag_preserves_votes_and_autohost_with_real_human_thresholds(self):
        n = self.ai_native({'bot_autostartplayers': '6'}, target=12)
        try:
            n.start().create(); n.command('ownerless')
            self.ai_capture(n, players=0, computer_players=12, autostart_players=6)
            ps = []
            for count in range(1, 7):
                guard = self.ai_guard(*ps) if ps else None
                ps.append(n.peer(f'Human{count}'))
                self.ai_capture(n, *ps, players=count, ai_owned=12-count, autostart_players=6)
                n.command('auto')
                self.ai_capture(n, *ps, phase='COUNTDOWN' if count == 6 else 'LOBBY',
                                players=count, computer_players=12-count, votes=0)
                if guard:
                    self.ai_unchanged(guard, ps[-1])
        finally:
            n.stop()

        for humans, cap, threshold in ((2, 12, 2), (6, 12, 6), (12, 12, 12), (2, 2, 10)):
            with self.subTest(disabled=True, humans=humans, cap=cap, threshold=threshold):
                n = self.ai_native({'bot_autostart_enabled': '0' if humans == 6 else 'false',
                                    'bot_autostartplayers': str(threshold), 'bot_maxplayers': str(cap),
                                    'bot_vote_required_percent': '1'}, target=cap)
                try:
                    n.start().create(); n.command('ownerless')
                    self.ai_capture(n, players=0, ai_owned=cap, autostart_players=0, max_players=cap)
                    ps = []
                    for count in range(1, humans+1):
                        guard = self.ai_guard(*ps) if ps else None
                        ps.append(n.peer(f'Human{count}'))
                        self.ai_capture(n, *ps, players=count, ai_owned=cap-count,
                                        phase='LOBBY', autostart_players=0)
                        if guard:
                            self.ai_unchanged(guard, ps[-1])
                    guard = self.ai_guard(*ps)
                    n.command('auto')
                    self.ai_capture(n, *ps, phase='LOBBY', votes=0, autostart_players=0)
                    ps[0].chat('!status')
                    wait(lambda: any('Autostart: off' in text for text in ps[0].chats()))
                    ps[0].chat('!vote')
                    self.ai_capture(n, *ps, phase='LOBBY', votes=1, votes_required=2)
                    ps[1].chat('!vote')
                    self.ai_capture(n, *ps, phase='COUNTDOWN', votes=2, votes_required=2,
                                    autostart_players=0)
                    self.ai_unchanged(guard)
                finally:
                    n.stop()

        title = 'Disabled auto lobby'
        n = self.ai_native({'bot_autostart_enabled': 'false', 'bot_autostartplayers': '6',
                            'autohost_maxgames': '1', 'autohost_startplayers': '6',
                            'autohost_gamename': title, 'autohost_owner': 'Owner'})
        try:
            n.start(); n.command('autohost')
            def published():
                intent = n.snapshot()
                return intent if intent['operation'] == 'publish' and intent['host_counter'] == 1 else None
            intent = wait(published)
            packet = bytes.fromhex(intent['packet_hex'])
            self.assertEqual(packet[24:].split(b'\0', 1)[0].decode(), title)
            wait(lambda: any(state['phase'] == 'LOBBY' and state['game_name'] == title
                             and state['autostart_players'] == 0 and state['ai_supported']
                             for state in n.structured_states()))
        finally:
            n.stop()

    def test_ai_countdown_freezes_reconciliation_and_aborted_lobby_can_refill(self):
        for route in ('reservation', 'leave'):
            with self.subTest(route=route):
                target = 12 if route == 'reservation' else 6
                n = self.ai_native({'bot_vote_required_percent': '100'}, target=target)
                try:
                    n.start().create()
                    a = n.peer('Owner', lan=True); b = n.peer('Second')
                    self.ai_capture(n, a, b, players=2, ai_owned=target-2)
                    guard = self.ai_guard(a, b)
                    a.chat('!vote'); self.ai_wait(n, votes=1)
                    b.chat('!vote')
                    self.ai_capture(n, a, b, phase='COUNTDOWN', ai_owned=target-2)
                    before = a.slots()[-1]
                    if route == 'reservation':
                        # No free rows: honoring this new reservation would remove
                        # an AI if reconciliation incorrectly ran during countdown.
                        n.command('hold Late')
                        n.command('refresh'); n.command('agerefresh')
                        self.ai_capture(n, a, b, phase='COUNTDOWN', ai_owned=10,
                                        computer_players=10, refresh_pending=False)
                        self.assertEqual(a.slots()[-1], before)
                        a.chat('!abort')
                        state = self.ai_capture(n, a, b, phase='LOBBY', ai_owned=9)
                        self.assertEqual(sum(row[2] == 0 for row in self.ai_rows(state)), 1)
                        self.ai_unchanged(guard)
                    else:
                        b.close()
                        self.ai_capture(n, a, phase='LOBBY', players=1, ai_owned=5, votes=0)
                        self.ai_unchanged(guard, ignore=(b.pid,))
                        n.command('agecooldown')
                        a.chat('!vote')
                        self.ai_capture(n, a, phase='COUNTDOWN', votes=1)
                        guard = self.ai_guard(a)
                        c = n.peer('AfterAbort')
                        self.ai_capture(n, a, c, phase='LOBBY', players=2, ai_owned=4, votes=0)
                        self.ai_unchanged(guard, c)
                    self.assertEqual(self.refresh_events(n), [])
                finally:
                    n.stop()

    def test_ai_loading_and_loaded_game_never_refill_closed_owned_rows(self):
        n = self.ai_native()
        try:
            n.start().create()
            a = n.peer('Owner', lan=True); b = n.peer('Second')
            state = self.ai_capture(n, a, b, players=2, ai_owned=4)
            guard = self.ai_guard(a, b)
            n.command('load')
            wait(lambda: all(any(packet[1] == 0x0b for packet in p.frames) for p in (a, b)))
            state = self.ai_capture(n, a, b, wire=False, phase='IN_GAME', loading=True, ai_owned=4)
            self.ai_unchanged(guard, state=state)
            lobby_capture_counts = {p: len(p.slots()) for p in (a, b)}
            for loading in (True, False):
                if not loading:
                    for p in (a, b):
                        p.sock.sendall(frame(0x23))
                    state = self.ai_capture(n, a, b, wire=False, phase='IN_GAME', loading=False, ai_owned=3)
                    self.ai_unchanged(guard, state=state)
                state = self.ai_capture(n, a, b, wire=False, loading=loading)
                sid = min(state['ai_owned_sids'])
                other = {i: row for i, row in enumerate(self.ai_rows(state)) if i != sid}
                n.command('aiclose')
                n.command('refresh'); n.command('agerefresh')
                expected = 3 if loading else 2
                state = self.ai_capture(n, a, b, wire=False, phase='IN_GAME', loading=loading,
                                        ai_owned=expected, computer_players=expected, refresh_pending=False)
                self.assertEqual(self.ai_rows(state)[sid][2:4], b'\x01\x00')
                for i, row in other.items():
                    self.assertEqual(self.ai_rows(state)[i], row)
                self.ai_unchanged(guard, state=state)
                for p in (a, b):
                    self.assertEqual(len(p.slots()), lobby_capture_counts[p])
            self.assertEqual(self.refresh_events(n), [])
        finally:
            n.stop()

    def test_ai_one_second_refresh_protects_owned_rows_and_restores_before_joins(self):
        for route in ('delay', 'normal', 'scored', 'reserved', 'close'):
            with self.subTest(route=route):
                n = self.ai_native(slots=7 if route == 'delay' else 12)
                try:
                    n.start().create()
                    a = n.peer('Owner', lan=True); b = n.peer('Second')
                    if route == 'scored':
                        n.command('scored')
                    if route == 'reserved':
                        n.command('hold New')
                    state = self.ai_capture(n, a, b, players=2, ai_owned=4)
                    before = self.ai_bytes(state)
                    owned = {sid: self.ai_rows(state)[sid] for sid in state['ai_owned_sids']}
                    guard = self.ai_guard(a, b)
                    start = len(a.slots())
                    n.command('refresh')
                    state = self.ai_capture(n, a, b, refresh_pending=True, ai_owned=4)
                    closed = self.ai_bytes(state)
                    changes = [i for i, (old, new) in enumerate(zip(before, closed)) if old != new]
                    self.assertEqual(len(changes), 1)
                    index = changes[0]; sid = index // 9
                    self.assertEqual((index % 9, before[index], closed[index]), (2, 0, 1))
                    self.assertNotIn(sid, owned)
                    closed_time = next(t for t, raw in a.slot_snapshots()[start:] if raw == closed)
                    self.assertEqual(self.refresh_events(n), [])
                    if route == 'delay':
                        self.ai_wait(n, refresh_pending=False)
                        state = self.ai_capture(n, a, b, ai_owned=4)
                        self.assertEqual(self.ai_bytes(state), before)
                        restored_time = next(t for t, raw in a.slot_snapshots()[start:]
                                             if t >= closed_time and raw == before)
                        self.assertGreaterEqual(restored_time - closed_time, .90)
                        self.assertLess(restored_time - closed_time, 2.0)
                        n.command('agerefresh')
                        self.ai_capture(n, a, b, refresh_pending=True)
                        self.ai_wait(n, refresh_pending=False)
                        self.ai_capture(n, a, b, ai_owned=4)
                    elif route == 'close':
                        n.command('pendingclose')
                        state = self.ai_capture(n, a, b, refresh_pending=False, ai_owned=4)
                        self.assertEqual(self.ai_rows(state)[sid][2], 1)
                    else:
                        newcomer = n.peer('New')
                        state = self.ai_capture(n, a, b, newcomer, players=3, ai_owned=3,
                                                computer_players=3, refresh_pending=False)
                        self.assertNotEqual(self.ai_rows(state)[sid][2], 1)
                        self.ai_unchanged(guard, newcomer)
                    self.ai_unchanged(guard)
                    if route in ('delay', 'close'):
                        for peer in (a, b):
                            for capture in peer.slots()[guard[1][peer]:]:
                                for owned_sid, row in owned.items():
                                    self.assertEqual(capture[owned_sid*9:(owned_sid+1)*9], row)
                    expected_events = 2 if route == 'delay' else 1
                    wait(lambda: len(self.refresh_events(n)) == expected_events)
                    self.assertEqual(self.refresh_events(n), [dict(version=1,
                                     host_counter=state['host_counter'], sequence=i)
                                     for i in range(1, expected_events+1)])
                finally:
                    n.stop()

    def test_ai_telemetry_exact_types_counts_and_public_human_computer_wording(self):
        n = self.ai_native({'bot_autostartplayers': '6'})
        try:
            n.start()
            wait(lambda: any(s['phase'] == 'IDLE' for s in n.structured_states()))
            idle = next(s for s in n.structured_states() if s['phase'] == 'IDLE')
            self.assertEqual((idle['players'], idle['computer_players'], idle['ai_target'], idle['ai_supported']),
                             (0, 0, 0, False))
            n.create()
            a = n.peer('Owner', lan=True)
            state = self.ai_capture(n, a, players=1, computer_players=5, ai_owned=5,
                                    ai_target=6, ai_supported=True, autostart_players=6, max_players=12)
            guard = self.ai_guard(a)
            a.chat('!status')
            wait(lambda: any('Players: 1 humans, 5 computers | Autostart: 6 humans' in c for c in a.chats()))
            b = n.peer('Second')
            state = self.ai_capture(n, a, b, players=2, computer_players=4, ai_owned=4)
            b.chat('!status')
            wait(lambda: any('Players: 2 humans, 4 computers | Autostart: 6 humans' in c for c in b.chats()))
            wait(lambda: any(s['phase'] == 'LOBBY' and s['players'] == 2 and s['computer_players'] == 4
                             for s in n.structured_states()))
            for event in n.structured_states():
                for key in ('version', 'players', 'computer_players', 'ai_target', 'max_players',
                            'autostart_players', 'votes', 'votes_required', 'countdown', 'host_counter',
                            'refresh_count', 'refresh_skipped', 'refresh_sequence', 'last_refresh_monotonic_ms',
                            'publication_refresh_count', 'publication_refresh_successes',
                            'publication_refresh_failures', 'last_ack_monotonic_ms', 'active_games', 'uptime_seconds'):
                    self.assertIs(type(event[key]), int, (key, event))
                for key in ('ai_supported', 'loading', 'realm_online', 'feedback_valid'):
                    self.assertIs(type(event[key]), bool, (key, event))
                for key in ('phase', 'game_name', 'map_name'):
                    self.assertIs(type(event[key]), str, (key, event))
                if event['phase'] == 'LOBBY':
                    self.assertEqual(event['ai_target'], 6)
                    self.assertIs(event['ai_supported'], True)
                    self.assertEqual(event['players'] + event['computer_players'], 6)
                    self.assertEqual((event['max_players'], event['autostart_players']), (12, 6))
            self.assertNotRegex('\n'.join(a.chats() + b.chats()),
                                r'ai_owned|ai_supported|ai_target|bot_lobby|HOSTBOT_STATE|managed SIDs')
            self.ai_unchanged(guard, b)
        finally:
            n.stop()

    def test_ai_refresh_wave_distinct_rows_one_hint_and_abort_remaining_rows(self):
        for route in ('complete', 'join', 'admin', 'vote'):
            with self.subTest(route=route):
                n = self.ai_native({'bot_autostartplayers': '6'}, slots=11, observers=True)
                try:
                    n.start().create()
                    a = n.peer('Owner', lan=True); b = n.peer('Second')
                    self.ai_capture(n, a, b, players=2, ai_owned=4)
                    n.command('computer'); n.command('closeone'); n.command('hold Absent')
                    state = self.ai_capture(n, a, b, players=2, computer_players=4, ai_owned=3)
                    before = self.ai_bytes(state)
                    rows = self.ai_rows(state)
                    free = [sid for sid, row in enumerate(rows) if row[:1] == b'\0'
                            and row[2:4] == b'\0\0' and row[4] < 12 and row[5] < 12]
                    # Owner is present; the first free row stays open for Absent.
                    order = list(reversed(free[1:]))
                    self.assertEqual(len(order), 3)
                    protected = {sid: row for sid, row in enumerate(rows) if sid not in order}
                    guard = self.ai_guard(a, b); cursor = len(a.slots())
                    n.command('refresh')
                    pending = self.ai_capture(n, a, b, refresh_pending=True, refresh_sid=order[0])
                    self.assertEqual((pending['refresh_count'], pending['refresh_sequence']), (0, 0))
                    started_ms = pending['refresh_started_ms']
                    n.command('auto')  # Below six humans: must leave the wave intact.
                    n.command('refresh')  # No overlapping or restarted wave.
                    self.ai_wait(n, refresh_pending=True, refresh_sid=order[0])
                    self.assertEqual(self.refresh_events(n), [])
                    extra = ()
                    if route == 'complete':
                        self.finish_refresh(n, a, before)
                        self.ai_ping(n, b)
                        wait(lambda: b.slots()[-1] == before)
                        transitions = []
                        for timestamp, raw in a.slot_snapshots()[cursor:]:
                            if not transitions or transitions[-1][1] != raw:
                                transitions.append((timestamp, raw))
                        expected = []
                        for sid in order:
                            closed = bytearray(before); closed[sid*9+2] = 1
                            expected.extend((bytes(closed), before))
                        self.assertEqual([raw for _, raw in transitions], expected)
                        for i in range(0, len(transitions), 2):
                            self.assertGreaterEqual(transitions[i+1][0] - transitions[i][0], .90)
                            self.assertLess(transitions[i+1][0] - transitions[i][0], 3)
                    else:
                        self.ai_wait(n, refresh_pending=True, refresh_sid=order[1])
                        self.ai_capture(n, a, b, refresh_sid=order[1], refresh_count=0, refresh_sequence=0)
                        self.assertEqual(self.refresh_events(n), [])
                        if route == 'join':
                            c = n.peer('Interleaved'); extra = (c,)
                            after = self.ai_capture(n, a, b, c, players=3, ai_owned=2, refresh_pending=False)
                            borrowed = next(sid for sid, row in enumerate(self.ai_rows(after)) if row[0] == c.pid)
                            protected.pop(borrowed, None)
                        elif route == 'admin':
                            n.command('pendingclose')
                            after = self.ai_capture(n, a, b, refresh_pending=False, ai_owned=3)
                            self.assertEqual(self.ai_rows(after)[order[1]][2], 1)
                        else:
                            a.chat('!vote')
                            after = self.ai_capture(n, a, b, refresh_pending=False, vote_active=True, votes=1)
                        settled = self.ai_bytes(after); quiet_until = time.monotonic() + 2.2
                        def quiet():
                            current = n.state()
                            self.assertFalse(current['refresh_pending'])
                            self.assertEqual(self.ai_bytes(current), settled)
                            self.assertEqual(len(self.refresh_events(n)), 1)
                            return time.monotonic() >= quiet_until
                        wait(quiet, 3)
                        self.ai_capture(n, a, b, *extra, refresh_pending=False)
                    final = n.state()
                    self.assertEqual((final['refresh_count'], final['refresh_sequence']), (1, 1))
                    self.assertEqual(final['refresh_started_ms'], started_ms)
                    self.assertEqual(self.refresh_events(n), [dict(version=1, host_counter=state['host_counter'], sequence=1)])
                    for peer in (a, b):
                        for raw in peer.slots()[guard[1][peer]:]:
                            for sid, row in protected.items():
                                self.assertEqual(raw[sid*9:(sid+1)*9], row)
                            # Relative to the baseline, at most one eligible row
                            # is closed, including the administrator's final close.
                            self.assertLessEqual(sum(raw[sid*9+2] == 1 for sid in order), 1)
                    self.ai_unchanged(guard, *extra)
                finally:
                    n.stop()

    def test_ai_refresh_omitted_interval_starts_next_wave_at_ten_seconds_not_completion_plus_ten(self):
        n = self.ai_native({'bot_autostartplayers': '6'}, slots=8)
        # Omit the fixture override to exercise the native ten-second default.
        n.cfg.write_text(''.join(line for line in n.cfg.read_text().splitlines(keepends=True)
                                if not line.startswith('bot_lobby_refresh_interval =')))
        try:
            n.start().create()
            a = n.peer('Owner', lan=True); b = n.peer('Second')
            before = self.ai_capture(n, a, b, players=2, ai_owned=4)
            baseline = self.ai_bytes(before); guard = self.ai_guard(a, b)
            n.command('refresh')
            first = self.ai_wait(n, refresh_pending=True, refresh_remaining=1)
            n.command('auto')
            self.ai_wait(n, refresh_pending=True, refresh_count=0, refresh_sequence=0)
            self.finish_refresh(n, a, baseline, events=1)
            def next_wave():
                state = n.state()
                return state if state['refresh_pending'] else None
            second = wait(next_wave, 12)
            elapsed_ms = second['refresh_started_ms'] - first['refresh_started_ms']
            self.assertGreaterEqual(elapsed_ms, 10000)
            self.assertLess(elapsed_ms, 11500)
            self.assertEqual((second['refresh_count'], second['refresh_sequence']), (1, 1))
            self.finish_refresh(n, a, baseline, events=2)
            self.ai_capture(n, a, b, phase='LOBBY', players=2, ai_owned=4,
                            refresh_count=2, refresh_sequence=2)
            self.ai_unchanged(guard)
            for peer in (a, b):
                for raw in peer.slots()[guard[1][peer]:]:
                    for sid in before['ai_owned_sids']:
                        self.assertEqual(raw[sid*9:(sid+1)*9], baseline[sid*9:(sid+1)*9])
            self.assertEqual([event['sequence'] for event in self.refresh_events(n)], [1, 2])
        finally:
            n.stop()

    def lifecycle_loaded(self, native, *peers):
        for peer in peers:
            peer.chat('!vote')
        self.ai_wait(native, phase='COUNTDOWN')
        wait(lambda: all(any(packet[1] == 0x0b for packet in peer.frames) for peer in peers))
        loading = self.ai_capture(native, *peers, wire=False, phase='IN_GAME', loading=True,
                                  game_loaded=False)
        for peer in peers:
            self.assertTrue(peer.game_keepalive)
            peer.sock.sendall(frame(0x23))
        loaded = self.ai_wait(native, phase='IN_GAME', loading=False, game_loaded=True)
        wait(lambda: all(peer.game_keepalives_sent > 0 for peer in peers))
        self.ai_wait(native, lagging=False)
        wait(lambda: native.host_state()['active_games'] == 1)
        return loading, loaded

    def lifecycle_empty_host(self, native):
        def empty():
            state = native.host_state()
            return state if state['active_games'] == 0 and state['lobby'] is None else None
        return wait(empty, 8)

    def test_ai_solo_match_survives_sixty_real_seconds_then_rehosts_and_checks_counters(self):
        n = self.ai_native({'bot_countdownseconds': '1', 'bot_autostart_enabled': 'false',
                            'bot_autostartplayers': '6', 'bot_maxgames': '1',
                            'autohost_maxgames': '1', 'autohost_startplayers': '6',
                            'autohost_gamename': 'Solo AI rehost', 'autohost_owner': 'Owner'})
        try:
            n.start().create()
            a = n.peer('Owner', lan=True, game_keepalive=True)
            before = self.ai_capture(n, a, players=1, ai_owned=5, computer_players=5)
            old_counter = before['host_counter']
            loading, loaded = self.lifecycle_loaded(n, a)
            self.assertFalse(loading['gameover_started'])
            self.assertFalse(loaded['gameover_started'])
            started = time.monotonic()
            # Survival is measured from observed GAMELOADED, using the real
            # event loop and real 0x27 keepalives. No native clock is aged here.
            while time.monotonic() - started < 66:
                state = n.state()
                self.assertEqual((state['phase'], state['players'], state['ai_owned'],
                                  state['computer_players'], state['game_loaded'], state['loading']),
                                 ('IN_GAME', 1, 5, 5, True, False))
                self.assertFalse(state['gameover_started'], state)
                self.assertFalse(state['lagging'], state)
                self.assertFalse(a.disconnected)
                self.assertIsNone(n.proc.poll())
                time.sleep(.5)  # This test intentionally proves real wall-clock survival.
            state = n.state()
            self.assertGreater(time.monotonic() - started, 65)
            self.assertGreater(a.game_keepalives_sent, 100)
            self.assertGreater(state['game_sync_counter'], loaded['game_sync_counter'] + 100)
            self.assertFalse(state['gameover_started'])
            self.assertFalse(any('gameover timer started' in line or 'gameover timer finished' in line
                                 for line in n.lines))
            host = n.host_state()
            self.assertEqual(host['active_games'], 1)
            self.assertIsNone(host['lobby'])
            self.assertEqual(host['games'][0]['host_counter'], old_counter)
            a.close()
            wait(lambda: any(line.startswith('TEST EXIT ') for line in n.lines), 8)
            exited = json.loads(next(line[len('TEST EXIT '):] for line in n.lines
                                     if line.startswith('TEST EXIT ')))
            # Empty loaded games clean up, retaining the frozen five computers
            # until destruction rather than refilling a sixth without humans.
            self.assertEqual((exited['host_counter'], exited['players_total'],
                              exited['computer_players'], exited['ai_owned']), (old_counter, 0, 5, 5))
            wait(lambda: n.host_state()['active_games'] == 0, 8)
            # Only the subsequent autohost scan may be accelerated.
            n.command('autohost')
            def rehosted():
                state = n.host_state()
                lobby = state['lobby']
                return lobby if lobby and lobby['host_counter'] == old_counter + 1 else None
            lobby = wait(rehosted)
            self.assertEqual((lobby['phase'], lobby['players'], lobby['computer_players'],
                              lobby['ai_owned'], lobby['ai_target']), ('LOBBY', 0, 6, 6, 6))
            wait(lambda: n.snapshot()['operation'] == 'publish'
                 and n.snapshot()['host_counter'] == lobby['host_counter'])
            n.peer('Stale', counter_override=old_counter, accepted=False)
            self.assertEqual(n.host_state()['lobby']['players'], 0)
            b = n.peer('Rejoined')
            def joined():
                state = n.host_state()['lobby']
                return state if state and state['players'] == 1 and state['ai_owned'] == 5 else None
            wait(joined)
            n.command('slots'); self.ai_ping(n, b)
            wait(lambda: b.slots()[-1] == self.ai_bytes(n.host_state()['lobby']))
            self.assertFalse(b.disconnected)
            self.assertEqual(n.host_state()['lobby']['host_counter'], old_counter + 1)
        finally:
            n.stop()

    def test_gameover_timer_expires_for_non_ai_and_unapproved_single_human_matches(self):
        for route in ('non_ai', 'minimum_two', 'unmanaged', 'same_team'):
            with self.subTest(route=route):
                cfg = {'bot_countdownseconds': '1'}
                n = (self.native(cfg) if route == 'non_ai' else self.ai_native(
                     cfg, min_humans=2 if route == 'minimum_two' else 1,
                     vote_min=2 if route == 'minimum_two' else 1))
                try:
                    n.start().create()
                    a = n.peer('Owner', lan=True, game_keepalive=True)
                    b = n.peer('Second', game_keepalive=True)
                    self.ai_capture(n, a, b, players=2)
                    _, loaded = self.lifecycle_loaded(n, a, b)
                    self.assertFalse(loaded['gameover_started'])
                    if route == 'unmanaged':
                        n.command('airelease')
                        self.ai_wait(n, ai_owned=0, computer_players=4)
                    elif route == 'same_team':
                        n.command('aisameteam')
                    b.close()
                    state = self.ai_wait(n, players=1, players_total=1, gameover_started=True,
                                          game_loaded=True, loading=False)
                    self.assertLess(state['gameover_age'], 60)
                    self.assertEqual(state['computer_players'], 0 if route == 'non_ai' else 4)
                    if route == 'same_team':
                        human_team = next(row[4] for row in self.ai_rows(state) if row[0] == a.pid)
                        self.assertTrue(all(self.ai_rows(state)[sid][4] == human_team
                                            for sid in state['ai_owned_sids']))
                    self.assertTrue(any('gameover timer started (one player left)' in line for line in n.lines))
                    n.command('agegameover')
                    wait(lambda: a.disconnected)
                    self.lifecycle_empty_host(n)
                    self.assertTrue(any('is over (gameover timer finished)' in line for line in n.lines))
                finally:
                    n.stop()

    def test_ai_existing_gameover_timer_expires_after_owned_opponent_returns_including_hcl(self):
        for hcl in ('', 'abc'):
            with self.subTest(hcl=hcl):
                n = self.ai_native({'bot_countdownseconds': '1'}, hcl=hcl)
                try:
                    n.start().create()
                    a = n.peer('Owner', lan=True, game_keepalive=True)
                    before = self.ai_capture(n, a, players=1, ai_owned=5)
                    loading, loaded = self.lifecycle_loaded(n, a)
                    self.assertFalse(loading['gameover_started'])
                    self.assertFalse(loaded['gameover_started'])
                    self.assertEqual(loaded['ai_owned_sids'], before['ai_owned_sids'])
                    self.assertEqual(loaded['ai_owned'], 5)
                    if hcl:
                        self.assertNotEqual(self.ai_bytes(loaded), self.ai_bytes(before))
                        self.assertTrue(any('successfully encoded HCL' in line for line in n.lines))
                    original_rows = self.ai_bytes(loaded)
                    # Make CGame arm its real one-player timer, then restore
                    # the exact pre-existing managed opponents. The exemption
                    # must not clear a timer that has already started.
                    n.command('aisameteam')
                    self.ai_wait(n, gameover_started=True, game_loaded=True, players=1)
                    n.command('airestoreteams')
                    state = self.ai_wait(n, gameover_started=True, ai_owned=5, computer_players=5)
                    self.assertEqual(self.ai_bytes(state), original_rows)
                    self.assertLess(state['gameover_age'], 60)
                    n.command('agegameover')
                    wait(lambda: a.disconnected)
                    self.lifecycle_empty_host(n)
                    self.assertTrue(any('is over (gameover timer finished)' in line for line in n.lines))
                finally:
                    n.stop()

    def test_config_validation_before_runtime(self):
        for key,value in [('bot_maxplayers','13'),('bot_maxplayers','-1'),('bot_autostartplayers','1'),('bot_vote_required_percent','0'),('bot_vote_min_players','1'),('bot_vote_timeout','no'),('bot_vote_cooldown','-1'),('bot_lobby_refresh_interval','9'),('bot_lobby_refresh_mode','rehost'),('bot_vote_enabled','maybe'),('bot_countdownseconds','61')]:
            with self.subTest(key=key,value=value):
                n=self.native({key:value}).start(ready=False)
                self.assertEqual(n.proc.wait(timeout=5),78)


if __name__ == '__main__':
    unittest.main(verbosity=2)
