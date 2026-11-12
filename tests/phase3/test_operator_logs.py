"""Operator console and private machine diagnostics; synthetic Python only."""
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.runtime.config import gameplay_config, iccup_config
from src.runtime.controller import Child, ICCupSupervisor, OfflineProductionSupervisor
from src.runtime.native import native_state
from src.runtime.operator_logs import OperatorLogMixin, safe_name
from src.supervisor.events import sanitize

STAMP = r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}'
JOIN = re.compile(STAMP + r' INFO native: Player joined: (?P<username>[^\r\n]{1,64})$')
LEFT = re.compile(STAMP + r' INFO native: Player left: (?P<username>[^\r\n]{1,64})$')


def wire(**changes):
    return '[HOSTBOT_STATE] ' + json.dumps({'version': 1, 'phase': 'LOBBY',
        'game_name': 'Footman Ultimate AI', 'map_name': 'Footman.w3x',
        'host_counter': 1, 'players': 0, 'computer_players': 6,
        'active_games': 0, **changes})


class Recorder(OperatorLogMixin):
    def __init__(self, root):
        self.runtime, self.guard, self.output = root, threading.RLock(), io.StringIO()

    def feed(self, line):
        self.log('hostbot', message=sanitize(line))
        self.operator_native_line(line)
        state = native_state(line)
        if state is not None:
            self.operator_native_state(state)


class OperatorLogTests(unittest.TestCase):
    def test_rejected_selection_is_actionable_english_warning(self):
        self.recorder.log('cycle_selection_rejected', preset='bvo', next_preset='footmen', retry_seconds=30)
        output = self.recorder.output.getvalue()
        self.assertIn('WARN supervisor: Lobby creation rejected for preset bvo.', output)
        self.assertIn('Advancing to footmen; retry in 30 seconds.', output)
        self.assertIn('Active matches are preserved.', output)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recorder = Recorder(self.root)

    def lines(self):
        return self.recorder.output.getvalue().splitlines()

    def journal(self):
        return [json.loads(line) for line in (self.root / 'supervisor.jsonl').read_text().splitlines()]

    def names(self, pattern):
        return [match['username'] for line in self.lines() if (match := pattern.fullmatch(line))]

    def test_heartbeat_and_refresh_requests_remain_machine_diagnostics_only(self):
        for uptime in range(100):
            self.recorder.feed(wire(uptime_seconds=uptime))
            self.recorder.log('hostbot', message='[HOSTBOT_REFRESH] {"version":1,"host_counter":1,"sequence":1}')
        console = self.recorder.output.getvalue()
        self.assertEqual(len(self.lines()), 1)
        self.assertRegex(self.lines()[0], STAMP + r' INFO native: Lobby opened: Footman Ultimate AI \(0 humans, 6 computers\)\.')
        self.assertNotIn('HOSTBOT_STATE', console)
        self.assertNotIn('HOSTBOT_REFRESH', console)
        self.assertNotIn('"event"', console)
        raw = [item for item in self.journal() if item['event'] == 'hostbot']
        self.assertEqual(len(raw), 200)
        self.assertEqual(native_state(raw[-2]['message'])['uptime_seconds'], 99)

    def test_population_countdown_game_end_and_rehost_are_meaningful(self):
        self.recorder.feed(wire())
        self.recorder.feed('[GAME: Footman Ultimate AI] player [Alice|198.51.100.9] joined the game')
        self.recorder.feed(wire(players=1, computer_players=5))
        self.recorder.feed(wire(phase='COUNTDOWN', players=1, computer_players=5, countdown=5))
        self.recorder.feed(wire(phase='COUNTDOWN', players=1, computer_players=5, countdown=4))
        self.recorder.feed(wire(phase='IN_GAME', players=1, computer_players=5, active_games=1))
        self.recorder.feed(wire(host_counter=2))
        console = self.recorder.output.getvalue()
        for text in ('Participants: 1 humans, 5 computers.', 'Countdown started (5 seconds).',
                     'Game started: Footman Ultimate AI.', 'Game ended: Footman Ultimate AI.',
                     'Lobby rehosted: Footman Ultimate AI (0 humans, 6 computers).'):
            self.assertIn(text, console)
        self.assertEqual(console.count('Countdown started'), 1)
        self.assertEqual(self.names(JOIN), ['Alice'])
        self.assertEqual(self.names(LEFT), ['Alice'])
        self.assertNotIn('198.51.100.9', console)
        self.assertNotIn('#', console)
        self.recorder.feed(wire(phase='IN_GAME', host_counter=2, active_games=1))
        self.recorder.feed(wire(host_counter=3))
        self.assertEqual(self.recorder.output.getvalue().count('Lobby rehosted:'), 2)

    def test_countdown_cancel_and_idle_end_do_not_invent_player_identities(self):
        self.recorder.feed(wire())
        self.recorder.feed(wire(phase='COUNTDOWN', countdown=3))
        self.recorder.feed(wire())
        self.assertIn('Countdown cancelled;', self.recorder.output.getvalue())
        self.recorder.feed(wire(phase='IN_GAME', active_games=1))
        self.recorder.feed(wire(phase='IDLE', active_games=0, game_name='', host_counter=0))
        self.recorder.feed(wire(phase='IDLE', active_games=0, game_name='', host_counter=0))
        self.assertEqual(self.recorder.output.getvalue().count('Game ended:'), 1)
        self.assertEqual(self.names(JOIN), [])
        self.assertEqual(self.names(LEFT), [])

    def test_refresh_acks_are_rate_limited_and_never_claim_visibility(self):
        def at(second, successes, failures=0):
            with patch('src.runtime.operator_logs.time.monotonic', return_value=second):
                self.recorder.feed(wire(publication_refresh_successes=successes, publication_refresh_failures=failures))
        at(0, 0)
        at(10, 1)
        for second in range(20, 300, 10):
            at(second, second // 10)
        self.assertEqual(self.recorder.output.getvalue().count('Lobby refresh acknowledged'), 1)
        at(310, 31)
        at(620, 31)
        self.assertEqual(self.recorder.output.getvalue().count('Lobby refresh acknowledged'), 2)
        self.assertIn('30 new acknowledgements; 31 total', self.recorder.output.getvalue())
        at(630, 31, 1)
        at(640, 31, 2)
        at(690, 31, 3)
        self.assertEqual(self.recorder.output.getvalue().count('Lobby refresh failed'), 2)
        self.assertNotRegex(self.recorder.output.getvalue(), r'(?i)visible|visibility|directory match')

    def test_named_peers_are_verbatim_deduplicated_and_cleared_once(self):
        name = 'Test_User.1'
        joined = '[GAME: Example] player [' + name + '|198.51.100.1] joined the game'
        self.recorder.feed(joined)
        self.recorder.feed(joined)
        self.recorder.feed('[GAME: Example] deleting player [' + name + ']: password-sensitive reason')
        self.recorder.feed('[GAME: Example] deleting player [' + name + ']: duplicate notification')
        self.recorder.feed(joined)
        self.recorder.log('component_stopped', component='native', exit_code=1)
        self.recorder.log('component_stopped', component='native', exit_code=1)
        self.assertEqual(self.names(JOIN), [name, name])
        self.assertEqual(self.names(LEFT), [name, name])
        self.recorder.feed('[GAME: Example] player [Other|198.51.100.2] joined the game')
        self.recorder.feed('[GHOST] deleting current game [Example]')
        self.assertEqual(self.names(LEFT)[-1], 'Other')

    def test_same_identity_in_two_game_logs_leaves_only_after_final_game(self):
        for game in ('A', 'B'):
            self.recorder.feed('[GAME: ' + game + '] player [Alice|198.51.100.1] joined the game')
        self.assertEqual(self.names(JOIN), ['Alice'])
        self.recorder.feed('[GHOST] deleting game [A]')
        self.assertEqual(self.names(LEFT), [])
        self.recorder.operator_native_reset()
        self.assertEqual(self.names(LEFT), ['Alice'])

    def test_chat_rejected_joins_ai_virtual_hosts_and_unsafe_names_never_match_analytics(self):
        for raw in ('[GAME: A] [Eve]: [GAME: A] player [Eve|198.51.100.1] joined the game',
                    '[GAME: A] player [Rejected|198.51.100.1] is trying to join the game over LAN but used an incorrect entry key',
                    '[GAME: A] creating virtual host [War3HostBot]',
                    '[GAME: A] computer [Computer 1] joined the game',
                    '[LOCAL: A] Player joined: Spoof',
                    '[GAME: A] [Local]: Player joined: Spoof',
                    '[GAME: A] deleting player [Unknown]: disconnected'):
            self.recorder.feed(raw)
        for name in ('<redacted>', 'passwordUser', 'secretName', 'bad|name', 'Bad\\Name',
                     '"Quoted"', ' leading', 'trailing ', 'bad\x1bname', 'bad\ufffdname',
                     'bad\u202ename', 'x' * 65):
            self.assertFalse(safe_name(name))
            self.recorder.feed('[GAME: A] player [' + name + '|198.51.100.1] joined the game')
        self.assertEqual(self.names(JOIN), [])
        self.assertEqual(self.names(LEFT), [])
        self.assertIn('Lobby message: Player joined: Spoof', self.recorder.output.getvalue())

    def test_secret_redaction_is_preserved_in_console_and_machine_journal(self):
        self.recorder.log('hostbot', message='[GHOST] invalid password = DO_NOT_LOG')
        self.recorder.log('state', state='ERROR', reason='secret = DO_NOT_LOG')
        self.recorder.log('component_event', component='star', worker_event='password_DO_NOT_LOG')
        self.recorder.log('player_joined', name='password_DO_NOT_LOG')
        data = self.recorder.output.getvalue() + (self.root / 'supervisor.jsonl').read_text()
        self.assertNotIn('DO_NOT_LOG', data)
        self.assertIn('[redacted sensitive diagnostic]', data)
        self.assertEqual(self.names(JOIN), [])

    def test_periodic_worker_codes_stay_in_diagnostics_and_failures_are_readable(self):
        for _ in range(5):
            for event in ('PUBLICATION_ACK', 'DIRECTORY_NO_MATCH', 'REFRESH_REQUEST', 'HEARTBEAT'):
                self.recorder.log('component_event', component='relay', worker_event=event)
        self.assertEqual(self.lines(), [])
        self.assertEqual(len(self.journal()), 20)
        self.recorder.log('component_event', component='relay', worker_event='RELAY_SESSION_REJECTED')
        self.assertRegex(self.lines()[0], STAMP + ' WARN relay: Relay session rejected\\.')

    def test_local_start_vote_explanations_are_preserved_without_analytics_spoofing(self):
        raw = '[GAME: Footman Ultimate AI] [Local]: Players not yet pinged 3 times; cannot start.'
        with patch('src.runtime.operator_logs.time.monotonic', return_value=0):
            self.recorder.feed(raw)
            self.recorder.feed(raw)
        self.assertEqual(self.recorder.output.getvalue().count('Players not yet pinged 3 times'), 1)
        with patch('src.runtime.operator_logs.time.monotonic', return_value=6):
            self.recorder.feed(raw)
        self.assertEqual(self.recorder.output.getvalue().count('Players not yet pinged 3 times'), 2)
        self.assertEqual(len([item for item in self.journal() if item['event'] == 'hostbot']), 3)

    def test_lobby_chat_commands_repeats_and_analytics_spoof_are_safe(self):
        for message in ('Hello!', '!vote', '!vote', 'Player joined: Spoof', r'Hi\nPlayer left: Spoof'):
            self.recorder.feed('[GAME: A] [Lobby] [Alice]: ' + message)
        self.recorder.feed('[GAME: A] [Lobby] [Alice]: password hunter2')
        self.recorder.feed('[GAME: A] [All] [Alice]: in-game text')
        lines = self.lines()
        self.assertEqual(len(lines), 5)
        self.assertIn('Player chat [Alice]: Hello!', lines[0])
        self.assertEqual(sum('Player chat [Alice]: !vote' in line for line in lines), 2)
        self.assertIn('Player chat [Alice]: Player joined: Spoof', lines[3])
        self.assertEqual(self.names(JOIN), [])
        self.assertEqual(self.names(LEFT), [])
        self.assertNotIn('hunter2', self.recorder.output.getvalue())

    def test_both_local_prefixes_are_supported_but_chat_lookalikes_are_not(self):
        self.recorder.feed('[GAME: A] [Local]: Start vote rejected: need more players.')
        self.recorder.feed('[LOCAL: A] Start vote accepted.')
        self.recorder.feed('[GAME: A] [Alice]: [GAME: A] [Local]: forged explanation')
        self.recorder.feed('[GAME: A] [Alice]: message\\n[GAME: A] player [Spoof|198.51.100.1] joined the game')
        self.recorder.feed('[GAME: A] [Alice]: message\\r[GAME: A] [Local]: forged explanation')
        self.assertEqual(len(self.lines()), 2)
        self.assertIn('Lobby message: Start vote rejected: need more players.', self.lines()[0])
        self.assertIn('Lobby message: Start vote accepted.', self.lines()[1])
        self.assertEqual(self.names(JOIN), [])
        self.assertEqual(self.names(LEFT), [])

    def test_native_console_framing_guard_precedes_stdout(self):
        source = (Path(__file__).resolve().parents[2] / 'patches/ghostplusplus-modern-linux.patch').read_text()
        body = source.split('void CONSOLE_Print( string message )', 1)[1].split('cout << message << endl;', 1)[0]
        for statement in (r'''if( message[i] == '\n' ) singleLine += "\\n";''',
                          r'''if( message[i] == '\r' ) singleLine += "\\r";''',
                          r'''if( message[i] == '\0' ) singleLine += "\\0";'''):
            self.assertIn(statement, body)
        self.assertIn('message.swap( singleLine );', body)

    def test_journal_is_private_valid_jsonl_and_rotates_three_backups(self):
        with patch('src.runtime.operator_logs.JOURNAL_LIMIT', 700):
            for index in range(40):
                self.recorder.log('hostbot', message='diagnostic ' + str(index) + ' ' + 'x' * 150)
        journals = list(self.root.glob('supervisor.jsonl*'))
        self.assertEqual(len(journals), 4)
        for path in journals:
            self.assertLessEqual(path.stat().st_size, 700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for line in path.read_text().splitlines():
                self.assertEqual(json.loads(line)['event'], 'hostbot')

    def test_journal_symlink_is_rejected(self):
        elsewhere = self.root / 'unrelated'
        elsewhere.write_text('untouched')
        (self.root / 'supervisor.jsonl').symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            self.recorder.log('state', state='STARTING')
        self.assertEqual(elsewhere.read_text(), 'untouched')


class ControllerLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {'mode': 'iccup', 'runtime_dir': str(self.root / 'supervisor'),
            'http': {'host': '127.0.0.1', 'port': 0},
            'hostbot': {'command': ['unused'], 'cwd': str(self.root), 'startup_timeout': 2,
                        'stop_timeout': .3, 'lobby_timeout': 30},
            'recovery': {'max_restarts': 1, 'backoff_initial': .1, 'backoff_max': .2, 'stable_seconds': 30},
            'iccup': iccup_config({}), 'gameplay': gameplay_config({})}

    def test_production_parsing_status_and_ready_prose_remain_independent(self):
        output = io.StringIO()
        service = ICCupSupervisor(self.cfg, output=output,
                                 backend=SimpleNamespace(prepare=lambda: None, close=lambda: None))
        self.addCleanup(service.lockfile.close)
        service.children['native'] = Child(SimpleNamespace(pid=123, poll=lambda: None), 1, time.monotonic())
        service._native_line(2, '[GAME: A] player [Stale|198.51.100.1] joined the game')
        service._native_line(1, wire())
        service._native_line(1, '[GAME: A] player [Alice|198.51.100.1] joined the game')
        service._native_line(1, wire(players=1, computer_players=5))
        self.assertEqual(service.snapshot()['game']['players'], 1)
        self.assertEqual(service.snapshot()['game']['computer_players'], 5)
        service.transition('LOBBY', 'native_lobby_published')
        service.transition('COUNTDOWN', 'native_countdown_active')
        service.transition('IN_GAME', 'native_game_active')
        self.assertNotIn('Stale', output.getvalue())
        self.assertNotIn('HOSTBOT_STATE', output.getvalue())
        for prose in ('Lobby is ready.', 'Countdown is running.', 'Game is running.'):
            self.assertRegex(output.getvalue(), '(?m)^' + STAMP + ' INFO supervisor: ' + re.escape(prose) + '$')

    def test_refresh_request_contract_survives_operator_logging(self):
        bridge = self.root / 'bridge'
        bridge.mkdir(mode=0o700)
        (bridge / 'native-intent.json').write_text('{}')
        (bridge / 'native-intent.json').chmod(0o600)
        self.cfg['iccup']['bridge_dir'] = str(bridge)
        service = ICCupSupervisor(self.cfg, output=io.StringIO(),
                                 backend=SimpleNamespace(prepare=lambda: None, close=lambda: None))
        self.addCleanup(service.lockfile.close)
        service.children['native'] = Child(SimpleNamespace(pid=123, poll=lambda: None), 1, time.monotonic())
        line = '[HOSTBOT_REFRESH] {"version":1,"host_counter":1,"sequence":7}'
        intent = {'version': 1, 'pid': 123, 'generation': 1, 'host_counter': 1, 'operation': 'publish'}
        with patch('src.runtime.controller.publication_intent', return_value=(intent, b'', '')):
            service.log('hostbot', message=sanitize(line))
            service._native_line(1, line)
        request = json.loads((bridge / 'refresh-request.json').read_text())
        self.assertEqual(request['sequence'], 7)
        self.assertEqual(request['host_counter'], 1)
        self.assertEqual(service.last_refresh_request, 7)
        self.assertNotIn('HOSTBOT_REFRESH', service.output.getvalue())
        self.assertNotIn('acknowledged', service.output.getvalue())
        self.assertIn('HOSTBOT_REFRESH', (service.runtime / 'supervisor.jsonl').read_text())

    def test_offline_reader_keeps_journal_and_clears_roster_after_synthetic_process_stop(self):
        config = self.root / 'config'
        config.mkdir(mode=0o700)
        (config / 'ghost.cfg').write_text('bnet_server =\nbot_defaultmap = selected.cfg\n')
        self.cfg['mode'] = 'offline'
        self.cfg['iccup'].update(native_config=str(config / 'ghost.cfg'), bridge_dir=str(self.root / 'bridge'))
        output = io.StringIO()
        service = OfflineProductionSupervisor(self.cfg, output=output)
        code = ('import signal,time; '
                'signal.signal(signal.SIGTERM,lambda *_:exit(0)); '
                'print("[GHOST] offline initialization complete",flush=True); '
                'print(' + repr('[GAME: Example] player [Alice|198.51.100.1] joined the game') + ',flush=True); '
                'print(' + repr('[GAME: Example] [Local]: Players not yet pinged 3 times; cannot start.') + ',flush=True); '
                'print(' + repr(wire()) + ',flush=True); time.sleep(60)')
        service.config['hostbot']['command'] = [sys.executable, '-u', '-c', code]
        try:
            service.start()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and 'Lobby is ready.' not in output.getvalue():
                time.sleep(.02)
            self.assertIn('Lobby is ready.', output.getvalue())
            service.submit('quit')
            service.thread.join(5)
            self.assertFalse(service.thread.is_alive())
            lines = output.getvalue().splitlines()
            self.assertEqual([m['username'] for line in lines if (m := JOIN.fullmatch(line))], ['Alice'])
            self.assertEqual([m['username'] for line in lines if (m := LEFT.fullmatch(line))], ['Alice'])
            self.assertNotIn('HOSTBOT_STATE', output.getvalue())
            self.assertIn('Lobby message: Players not yet pinged 3 times; cannot start.', output.getvalue())
            journal = (service.runtime / 'supervisor.jsonl').read_text()
            self.assertIn('HOSTBOT_STATE', journal)
            self.assertTrue(all(isinstance(json.loads(line), dict) for line in journal.splitlines()))
            records = [json.loads(line) for line in journal.splitlines()]
            raw_index = next(i for i, item in enumerate(records) if item['event'] == 'hostbot' and item['message'].startswith('[HOSTBOT_STATE] '))
            parsed_index = next(i for i, item in enumerate(records) if item['event'] == 'lobby_opened')
            self.assertLess(raw_index, parsed_index)
        finally:
            if service.thread and service.thread.is_alive():
                service.submit('quit')
                service.thread.join(5)
            if not service.lockfile.closed:
                service.lockfile.close()


if __name__ == '__main__':
    unittest.main()
