#!/usr/bin/env python3
"""Cross-feature regression: next preset, durable gate, rehost and live W3GS."""
import importlib.util
import json
from pathlib import Path
import socket
import unittest

ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

gate = load('reservation_gate', ROOT / 'tests/phase3/native-reservation-gate-test.py')
wait = gate.wait

class PresetFenceTests(unittest.TestCase):
    setUp = gate.ReservationGateTests.setUp
    tearDown = gate.ReservationGateTests.tearDown
    native = gate.ReservationGateTests.native
    spool = gate.ReservationGateTests.spool
    launch = gate.ReservationGateTests.launch
    denied = gate.ReservationGateTests.denied

    def stage(self, n):
        path = n.root / 'next.cfg'
        path.write_text((n.root / 'synthetic.cfg').read_text().replace('Synthetic.w3x', 'Next.w3x') +
                        '\ntest_max = 4\ntest_game_name = Named preset\n')
        command = 'preset ' + str(path)
        n.proc.stdin.write(command + '\n'); n.proc.stdin.flush()
        self.assertEqual(n.marker('TEST PRESET '), 'TEST PRESET 1')
        n.marker('TEST COMMAND ' + command)

    def start_first(self):
        spool = self.spool()
        settings = {'bot_maxgames': '2', 'autohost_maxgames': '2', 'autohost_startplayers': '6',
                    'autohost_gamename': 'Original', 'autohost_owner': 'Owner'}
        n, generation, counter = self.launch(spool, settings)
        first = n.peer('First', game_keepalive=True)
        gate.publish(spool, generation, counter)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        first.sock.sendall(gate.gameplay.frame(0x23))
        wait(lambda: first.game_keepalives_sent > 0)
        return n, spool, generation, first

    def test_named_preset_rehost_fences_old_counter_and_first_game_continues(self):
        n, spool, generation, first = self.start_first()
        self.stage(n)
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        lobby = n.host_state()['lobby']
        self.assertEqual((lobby['map_name'], lobby['game_name']), ('Next.w3x', 'Named preset'))
        old = lobby['host_counter']
        self.denied(n, spool)
        gate.publish(spool, generation, old)
        n.command('rehosterror')
        wait(lambda: n.host_state()['lobby']['host_counter'] != old)
        lobby = n.host_state()['lobby']; new = lobby['host_counter']
        self.assertEqual((lobby['map_name'], lobby['game_name']), ('Next.w3x', 'Named preset'))
        self.assertEqual(json.loads((spool / f'close-{generation}-{old}.json').read_bytes())['host_counter'], old)
        self.denied(n, spool)
        second = n.peer('Second', game_keepalive=True)
        gate.publish(spool, generation, new)
        before = first.game_keepalives_sent
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 2)
        second.sock.sendall(gate.gameplay.frame(0x23))
        wait(lambda: first.game_keepalives_sent > before)
        self.assertFalse(first.disconnected)
        self.assertFalse((spool / f'close-{generation}-{new}.json').exists())

    def test_preset_rehosts_and_fences_with_empty_old_autohost_defaults(self):
        spool = self.spool()
        n, generation, first = self.launch(spool, {'bot_maxgames': '2', 'autohost_maxgames': '2',
                                                   'autohost_startplayers': '0', 'autohost_gamename': '',
                                                   'autohost_owner': 'Owner'})
        self.stage(n)
        n.command('removehost')
        self.assertTrue((spool / f'close-{generation}-{first}.json').exists())
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        old = n.host_state()['lobby']['host_counter']
        gate.publish(spool, generation, old)
        n.command('rehosterror')
        wait(lambda: n.host_state()['lobby']['host_counter'] != old)
        lobby = n.host_state()['lobby']
        self.assertEqual((lobby['map_name'], lobby['game_name']), ('Next.w3x', 'Named preset'))
        self.assertTrue((spool / f'close-{generation}-{old}.json').exists())
        self.denied(n, spool)  # The old marker cannot start the new counter.
        gate.publish(spool, generation, lobby['host_counter'])
        n.peer('Second', game_keepalive=True)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)

    def test_failed_bind_emits_fenced_close_and_pending_preset_can_retry(self):
        n, spool, generation, first = self.start_first()
        self.stage(n)
        before = n.snapshot()
        self.assertEqual(before['operation'], 'unpublish')
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(('127.0.0.1', 16113)); occupied.listen()
            n.command('autohost')
            wait(lambda: any('error listening on port' in line for line in n.lines))
            host = n.host_state()
            self.assertIsNone(host['lobby'])
            self.assertEqual(host['active_games'], 1)
            self.assertEqual(n.snapshot()['operation'], 'unpublish')
            self.assertEqual(n.snapshot()['sequence'], before['sequence'])
            closes = list(spool.glob(f'close-{generation}-*.json'))
            self.assertEqual(len(closes), 1)
            closed_counter = json.loads(closes[0].read_bytes())['host_counter']
            actions = first.game_keepalives_sent
            wait(lambda: first.game_keepalives_sent > actions)
            self.assertFalse(first.disconnected)
        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        lobby = n.host_state()['lobby']
        self.assertEqual((lobby['map_name'], lobby['game_name']), ('Next.w3x', 'Named preset'))
        self.assertNotEqual(lobby['host_counter'], closed_counter)
        self.denied(n, spool)

if __name__ == '__main__': unittest.main()
