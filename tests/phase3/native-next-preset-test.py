#!/usr/bin/env python3
"""Event-loop preset transaction against synthetic CMaps and live W3GS peers."""
import importlib.util
import socket
import sys
from pathlib import Path
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from runtime.config import gameplay_config
spec = importlib.util.spec_from_file_location('native_gameplay', ROOT / 'tests/phase3/native-test.py')
gameplay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gameplay)
wait = gameplay.wait

class NextPresetTests(unittest.TestCase):
    setUp = gameplay.GameplayTests.setUp
    tearDown = gameplay.GameplayTests.tearDown
    native = gameplay.GameplayTests.native

    def apply(self, n, path):
        cmd = 'preset ' + str(path)
        n.proc.stdin.write(cmd + '\n'); n.proc.stdin.flush()
        result = n.marker('TEST PRESET ')
        n.marker('TEST COMMAND ' + cmd)
        return result.endswith(' 1')

    def test_pending_preset_does_not_depend_on_old_autostart_threshold(self):
        n = self.native({'autohost_maxgames': '2', 'autohost_startplayers': '0',
                         'autohost_gamename': '', 'autohost_owner': 'Owner'})
        try:
            n.start()
            path = n.root / 'next.cfg'
            path.write_text((n.root / 'synthetic.cfg').read_text() + '\ntest_max = 4\n')
            self.assertTrue(self.apply(n, path))
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
            self.assertEqual(n.host_state()['lobby']['game_name'], 'Next preset')
        finally:
            for peer in n.peers: peer.close()
            n.stop()

    def test_ai_total_target_counts_fixed_rows_in_pending_and_catalog_validation(self):
        for teams, fixed in ((2, set(range(8, 12))), (3, {3, 7, 11}), (4, set())):
            with self.subTest(teams=teams, fixed=len(fixed)):
                n = self.native(slots=12, map_players=12, teams=teams)
                try:
                    n.start()
                    head = (n.root / 'synthetic.cfg').read_text().split('map_slot1')[0]
                    rows = ''.join(f'map_slot{sid+1} = 0 {100 if sid in fixed else 255} '
                                   f'{2 if sid in fixed else 0} {int(sid in fixed)} '
                                   f'{(0 if sid < 8 else 1) if teams == 2 else sid // (12 // teams)} '
                                   f'{sid} 32 1 100\n' for sid in range(12))
                    path = n.root / 'fixed-ai.cfg'
                    path.write_text(head + rows + '\ntest_max = 12\ntest_auto_enabled = 0\n'
                                    'test_autostart = 4\ntest_ai = 1\ntest_ai_target = 11\n'
                                    'test_min_humans = 1\ntest_vote_min = 2\n')
                    command = 'presetvalidate ' + str(path)
                    n.proc.stdin.write(command + '\n'); n.proc.stdin.flush()
                    self.assertTrue(n.marker('TEST PRESETVALIDATE ').endswith(' 1'))
                    n.marker('TEST COMMAND ' + command)
                    self.assertTrue(self.apply(n, path))
                    self.assertIsNone(n.host_state()['lobby'])
                finally:
                    n.stop()

    def test_missing_local_map_is_rejected_despite_complete_metadata(self):
        n = self.native()
        try:
            n.start()
            path = n.root / 'missing.cfg'
            path.write_text((n.root / 'synthetic.cfg').read_text() +
                            '\nmap_localpath = missing.w3x\ntest_max = 4\n')
            self.assertFalse(self.apply(n, path))
        finally:
            n.stop()

    def test_startup_accepts_zero_vote_cooldown(self):
        n = self.native({'bot_vote_cooldown': '0'})
        try:
            n.start()
            self.assertIsNone(n.host_state()['lobby'])
        finally:
            n.stop()

    def test_preset_number_boundaries(self):
        n = self.native()
        try:
            n.start()
            source = (n.root / 'synthetic.cfg').read_text()
            cases = [
                ({'test_vote_cooldown': 0}, True), ({'test_vote_cooldown': 600}, True),
                ({'test_vote_cooldown': 601}, False), ({'test_vote_cooldown': -1}, False),
                ({'test_auto_enabled': 0, 'test_autostart': 12}, True),
                ({'test_auto_enabled': 1, 'test_autostart': 12}, False),
                ({'test_auto_enabled': 0, 'test_autostart': 13}, False),
                ({'test_auto_enabled': 0, 'test_autostart': 1}, False),
                ({'test_vote_timeout': 4}, False), ({'test_vote_timeout': 5}, True),
                ({'test_vote_timeout': 600}, True), ({'test_vote_timeout': 601}, False),
                ({'test_refresh_interval': 9}, False), ({'test_refresh_interval': 10}, True),
                ({'test_refresh_interval': 3600}, True), ({'test_refresh_interval': 3601}, False),
                ({'test_vote_percent': 0}, False), ({'test_vote_percent': 1}, True),
                ({'test_vote_percent': 100}, True), ({'test_vote_percent': 101}, False),
                ({'test_countdown': 0}, True), ({'test_countdown': 60}, True),
                ({'test_countdown': 61}, False),
                ({'test_vote_min': 1}, False), ({'test_vote_min': 4}, True),
                ({'test_vote_min': 5}, False),
                ({'test_ai': 1, 'test_ai_target': 4, 'test_min_humans': 1,
                  'test_vote_min': 1, 'test_ai_skill': 0}, True),
                ({'test_ai': 1, 'test_ai_target': 4, 'test_min_humans': 1,
                  'test_vote_min': 1, 'test_ai_skill': 2}, True),
                ({'test_ai': 1, 'test_ai_target': 4, 'test_min_humans': 1,
                  'test_vote_min': 1, 'test_ai_skill': 3}, False),
                ({'test_ai': 1, 'test_ai_target': 1}, False),
                ({'test_ai': 1, 'test_ai_target': 5}, False),
                ({'test_max': 1}, False), ({'test_max': 13}, False),
                ({'test_max': 2, 'test_autostart': 2}, True),
                ({'test_max': 12, 'test_autostart': 12, 'test_vote_min': 12}, True),
                ({'test_ai': 1, 'test_ai_target': 2}, True),
                ({'test_ai': 1, 'test_ai_target': 0}, False),
                ({'test_game_name': 'x' * 24}, True),
                ({'test_game_name': 'x' * 25}, False),
                ({'test_game_name': 'bad[name'}, False),
            ]
            for index, (overrides, expected) in enumerate(cases):
                with self.subTest(overrides=overrides):
                    path = n.root / ('boundary-%d.cfg' % index)
                    path.write_text(source + '\n' + ''.join(
                        '%s = %s\n' % item for item in overrides.items()))
                    keys = {'test_max': 'max_players', 'test_autostart': 'autostart_players',
                            'test_auto_enabled': 'autostart_enabled', 'test_countdown': 'countdown_seconds',
                            'test_vote_percent': 'vote_required_percent', 'test_vote_min': 'vote_min_players',
                            'test_vote_timeout': 'vote_timeout', 'test_vote_cooldown': 'vote_cooldown',
                            'test_refresh_interval': 'lobby_refresh_interval'}
                    if set(overrides) <= keys.keys():
                        values = {'max_players': 4, 'autostart_players': 4}
                        values.update({keys[k]: bool(v) if k == 'test_auto_enabled' else v
                                       for k, v in overrides.items()})
                        try:
                            gameplay_config(values)
                            python_accepts = True
                        except ValueError:
                            python_accepts = False
                        self.assertEqual(expected, python_accepts)
                    self.assertEqual(self.apply(n, path), expected)
        finally:
            for peer in n.peers: peer.close()
            n.stop()

    def test_failed_bind_after_preset_never_publishes(self):
        n = self.native({'bot_maxgames': '2', 'autohost_maxgames': '2',
                         'autohost_startplayers': '4', 'autohost_gamename': 'Preset lobby',
                         'autohost_owner': 'Owner'})
        try:
            n.start()
            candidate = n.root / 'next.cfg'
            candidate.write_text((n.root / 'synthetic.cfg').read_text().replace(
                'Synthetic.w3x', 'BindFailure.w3x') + '\ntest_max = 4\n')
            self.assertTrue(self.apply(n, candidate))
            before = n.snapshot()
            self.assertEqual(before['operation'], 'unpublish')
            with socket.socket() as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(('127.0.0.1', 16113))
                occupied.listen()
                n.command('autohost')
                wait(lambda: any('error listening on port' in line for line in n.lines))
                time.sleep(.2)
                after = n.snapshot()
                self.assertEqual(after['operation'], 'unpublish')
                self.assertEqual(after['sequence'], before['sequence'])
                self.assertEqual(n.host_state()['active_games'], 0)
                self.assertIsNone(n.host_state()['lobby'])
        finally:
            for peer in n.peers: peer.close()
            n.stop()

    def test_failed_bind_does_not_stop_running_match(self):
        n = self.native({'bot_maxgames': '2', 'autohost_maxgames': '2',
                         'autohost_startplayers': '4', 'autohost_gamename': 'Preset lobby',
                         'autohost_owner': 'Owner'})
        try:
            n.start().create()
            first = n.peer('First', game_keepalive=True)
            n.command('startlobby')
            wait(lambda: n.host_state()['active_games'] == 1)
            first.sock.sendall(gameplay.frame(0x23))
            wait(lambda: any(p[1] == 0x0c for p in first.frames))
            candidate = n.root / 'next.cfg'
            candidate.write_text((n.root / 'synthetic.cfg').read_text().replace(
                'Synthetic.w3x', 'BindFailure.w3x') + '\ntest_max = 4\n')
            self.assertTrue(self.apply(n, candidate))
            before = n.snapshot()
            self.assertEqual(before['operation'], 'unpublish')
            with socket.socket() as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(('127.0.0.1', 16113))
                occupied.listen()
                n.command('autohost')
                wait(lambda: any('error listening on port' in line for line in n.lines))
                self.assertIsNone(n.host_state()['lobby'])
                self.assertEqual(n.host_state()['active_games'], 1)
                self.assertEqual(n.snapshot()['operation'], 'unpublish')
                self.assertEqual(n.snapshot()['sequence'], before['sequence'])
                count = sum(p[1] == 0x0c for p in first.frames)
                wait(lambda: sum(p[1] == 0x0c for p in first.frames) > count)
                self.assertFalse(first.disconnected)
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
            self.assertEqual(n.host_state()['lobby']['map_name'], 'BindFailure.w3x')
        finally:
            for peer in n.peers: peer.close()
            n.stop()

    def test_map_layout_rejection_then_small_non_ai_preset(self):
        n = self.native({'bot_maxgames': '2', 'autohost_maxgames': '2',
                         'autohost_startplayers': '4', 'autohost_gamename': 'Preset lobby',
                         'autohost_owner': 'Owner'})
        try:
            n.start()
            source = (n.root / 'synthetic.cfg').read_text()
            bad = n.root / 'bad-ai.cfg'
            bad.write_text(source.replace('map_slot2 = 0 255 0 0 1 1',
                                          'map_slot2 = 0 255 0 0 1 0') +
                           '\ntest_ai = 1\ntest_ai_target = 4\n')
            self.assertFalse(self.apply(n, bad))
            small = n.root / 'small.cfg'
            small.write_text(source.replace('Synthetic.w3x', 'Small.w3x') +
                             '\ntest_max = 2\ntest_autostart = 2\ntest_vote = 1\n')
            self.assertTrue(self.apply(n, small))
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
            lobby = n.host_state()['lobby']
            self.assertEqual(lobby['map_name'], 'Small.w3x')
            self.assertEqual(lobby['max_players'], 2)
            self.assertFalse(lobby['ai_supported'])
            self.assertEqual(lobby['autostart_players'], 2)
        finally:
            for peer in n.peers: peer.close()
            n.stop()

    def test_switch_preserves_lobby_and_running_game_and_rejects_invalid(self):
        n = self.native({'bot_maxgames': '2', 'autohost_maxgames': '2',
                         'autohost_startplayers': '4', 'autohost_gamename': 'Preset lobby',
                         'autohost_owner': 'Owner', 'bot_autostartplayers': '0'})
        try:
            n.start().create()
            first_state = n.host_state()['lobby']
            source = (n.root / 'synthetic.cfg').read_text()
            nextmap = n.root / 'next.cfg'
            nextmap.write_text(source.replace('Synthetic.w3x', 'Second.w3x') +
                               '\ntest_max = 4\ntest_autostart = 4\ntest_vote = 0\n'
                               'test_ai = 1\ntest_ai_target = 4\ntest_ai_skill = 2\n')
            self.assertTrue(self.apply(n, nextmap))
            self.assertEqual(n.host_state()['lobby'], first_state)
            first = n.peer('First', game_keepalive=True)
            n.command('startlobby')
            wait(lambda: n.host_state()['active_games'] == 1)
            first.sock.sendall(gameplay.frame(0x23))
            wait(lambda: any(p[1] == 0x0c for p in first.frames))
            before = n.host_state()['games'][0]
            bad = n.root / 'bad.cfg'
            bad.write_text(source.replace('Synthetic.w3x', 'Bad.w3x') + '\ntest_max = 1\n')
            self.assertFalse(self.apply(n, bad))
            invalid = n.root / 'invalid.cfg'; invalid.write_text('map_path = \n')
            self.assertFalse(self.apply(n, invalid))
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
            host = n.host_state()
            self.assertEqual(host['active_games'], 1)
            self.assertEqual(host['games'][0]['map_name'], before['map_name'])
            self.assertEqual(host['lobby']['map_name'], 'Second.w3x')
            self.assertEqual(host['lobby']['game_name'], 'Next preset')
            self.assertEqual(host['lobby']['max_players'], 4)
            self.assertTrue(host['lobby']['ai_supported'])
            self.assertEqual(host['lobby']['ai_target'], 4)
            self.assertNotEqual(host['lobby']['host_counter'], before['host_counter'])
            second = n.peer('Second')
            second.chat('!vote')
            wait(lambda: any('Voting is disabled' in x for x in second.chats()))
            count = sum(p[1] == 0x0c for p in first.frames)
            wait(lambda: sum(p[1] == 0x0c for p in first.frames) > count)
            n.command('trycreate'); n.command('autohost')
            self.assertEqual(n.host_state()['active_games'], 1)
            self.assertEqual(n.host_state()['lobby']['host_counter'], host['lobby']['host_counter'])
            self.assertFalse(first.disconnected)
            n.command('removehost')
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
            after = n.host_state()
            self.assertEqual(after['lobby']['map_name'], first_state['map_name'])
            self.assertEqual(after['lobby']['game_name'], 'Preset lobby')
            self.assertEqual(after['active_games'], 1)
        finally:
            for peer in n.peers: peer.close()
            n.stop()

if __name__ == '__main__': unittest.main()
