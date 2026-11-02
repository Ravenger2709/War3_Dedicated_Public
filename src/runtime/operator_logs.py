"""Human production console output with separate, redacted JSONL diagnostics.

The native wire protocol is consumed by controller.py, never by this formatter.
Named events come only from anchored native peer lifecycle messages.
"""
from datetime import datetime, timezone
import json
import os
import re
import time

from src.supervisor.events import SENSITIVE, sanitize

JOURNAL_LIMIT = 10 * 1024 * 1024
REFRESH_INTERVAL = 300
FAILURE_INTERVAL = 60
JOIN = re.compile(r'\[GAME: (?P<game>[^\]\r\n]{1,256})\] player \[(?P<name>[^\]|\r\n]{1,64})\|[^\]|\r\n]{1,64}\] joined the game')
LEAVE = re.compile(r'\[GAME: (?P<game>[^\]\r\n]{1,256})\] deleting player \[(?P<name>[^\]\r\n]{1,64})\]: [^\r\n]*')
DELETED = re.compile(r'\[GHOST\] deleting (?:current )?game \[(?P<game>[^\]\r\n]{1,256})\]')
LOCAL = re.compile(r'(?:\[GAME: [^\]\r\n]{1,256}\] \[Local\]:|\[LOCAL: [^\]\r\n]{1,256}\]) (?P<message>[^\r\n]{1,4096})')
LOBBY_CHAT = re.compile(r'\[GAME: [^\]\r\n]{1,256}\] \[Lobby\] \[(?P<name>[^\]\r\n]{1,64})\]: (?P<message>[^\r\n]{1,4096})')
STATES = {
    'STARTING': 'Starting production components.', 'CONNECTING': 'Starting the native host.',
    'AUTHENTICATING': 'Waiting for iCCup authentication.', 'CONNECTED': 'Realm connection is ready.',
    'PUBLISHING': 'Waiting for lobby publication confirmation.', 'READY': 'Native host is ready.',
    'LOBBY': 'Lobby is ready.', 'COUNTDOWN': 'Countdown is running.', 'IN_GAME': 'Game is running.',
    'RECONNECTING': 'Recovering the runtime connection.', 'STOPPING': 'Stopping the runtime.',
    'STOPPED': 'Runtime stopped.', 'ERROR': 'Runtime needs attention.',
}


def safe_fields(fields):
    return {key: '[redacted sensitive diagnostic]' if SENSITIVE.search(key)
            else sanitize(value) if isinstance(value, str) else value
            for key, value in fields.items()}


def visible(value, limit=256):
    return ''.join(c if c.isprintable() else ' ' for c in sanitize(str(value)))[:limit]


def safe_name(name):
    # Preserve identities verbatim. Never collapse different names to a replacement.
    return (isinstance(name, str) and name == name.strip()
            and all(c.isprintable() and c not in '[]|<>"\\\ufffd' for c in name)
            and 0 < len(name.encode('utf-8')) <= 64 and sanitize(name) == name)


class OperatorEvents:
    def __init__(self):
        self.previous = None
        self.peers = set()
        self.last_messages = {}
        self.local_message_at = None
        self.last_lobby = 0
        self.refresh_count = 0
        self.failure_count = 0
        self.refresh_at = None
        self.failure_at = None

    def clear_peers(self, game=None):
        removed = sorted(peer for peer in self.peers if game is None or peer[0] == game)
        for peer in removed:
            self.peers.remove(peer)
        # A name occurring in another live game still represents one Analytics user.
        remaining = {name for _, name in self.peers}
        return [('player_left', {'name': name}) for name in sorted({name for _, name in removed} - remaining)]

    def reset(self):
        events = self.clear_peers()
        self.previous = None
        self.last_lobby = 0
        self.refresh_count = self.failure_count = 0
        self.refresh_at = self.failure_at = None
        return events

    def peer_line(self, line):
        line = line.rstrip('\r\n')
        joined, left = JOIN.fullmatch(line), LEAVE.fullmatch(line)
        if joined or left:
            event = joined or left
            name, game = event['name'], event['game']
            if not safe_name(name):
                return []
            peer = (game, name)
            if joined and peer not in self.peers and len(self.peers) < 144:
                already = any(other == name for _, other in self.peers)
                self.peers.add(peer)
                return [] if already else [('player_joined', {'name': name})]
            if left and peer in self.peers:
                self.peers.remove(peer)
                return [] if any(other == name for _, other in self.peers) else [('player_left', {'name': name})]
        deleted = DELETED.fullmatch(line)
        return self.clear_peers(deleted['game']) if deleted else []

    def state(self, state, now):
        old, events = self.previous, []
        new_lobby = (state['phase'] == 'LOBBY' and
                     (old is None or old['host_counter'] != state['host_counter']))
        ended = old is not None and (old['active_games'] > 0 and state['active_games'] == 0
                                    or old['phase'] == 'IN_GAME' and state['phase'] not in ('IN_GAME', 'COUNTDOWN'))
        if ended:
            events.append(('game_ended', {'name': old['game_name']}))
            events.extend(self.clear_peers(old['game_name']))
        if new_lobby:
            if old is not None:
                events.extend(self.clear_peers())
            events.append(('lobby_rehosted' if self.last_lobby else 'lobby_opened',
                           {'name': state['game_name'], 'humans': state['players'], 'computers': state['computer_players']}))
            self.last_lobby = state['host_counter']
        elif old is None or (state['phase'], state['players'], state['computer_players']) != (
                old['phase'], old['players'], old['computer_players']):
            if state['phase'] != 'IDLE' and (old is None or (state['players'], state['computer_players']) != (
                    old['players'], old['computer_players'])):
                events.append(('population', {'humans': state['players'], 'computers': state['computer_players']}))
        if old is not None and state['phase'] != old['phase']:
            if state['phase'] == 'COUNTDOWN':
                events.append(('countdown_started', {'seconds': state['countdown']}))
            elif state['phase'] == 'IN_GAME':
                events.append(('game_started', {'name': state['game_name']}))
            elif old['phase'] == 'COUNTDOWN' and state['phase'] == 'LOBBY':
                events.append(('countdown_cancelled', {}))
        successes, failures = state['publication_refresh_successes'], state['publication_refresh_failures']
        if successes < self.refresh_count:
            self.refresh_count, self.refresh_at = successes, None
        if failures < self.failure_count:
            self.failure_count, self.failure_at = failures, None
        if (state['phase'] == 'LOBBY' and successes > self.refresh_count
                and (self.refresh_at is None or now - self.refresh_at >= REFRESH_INTERVAL)):
            events.append(('refresh_confirmed', {'count': successes - self.refresh_count, 'total': successes}))
            self.refresh_count, self.refresh_at = successes, now
        if failures > self.failure_count and (self.failure_at is None or now - self.failure_at >= FAILURE_INTERVAL):
            events.append(('refresh_failed', {'count': failures - self.failure_count, 'total': failures}))
            self.failure_count, self.failure_at = failures, now
        self.previous = dict(state)
        return events

    def prose(self, kind, fields):
        component, level, message = fields.get('component', 'supervisor'), 'INFO', None
        if component not in ('supervisor', 'native', 'star', 'relay', 'realm'):
            component = 'supervisor'
        if kind in ('player_joined', 'player_left'):
            if not safe_name(fields.get('name')):
                return None
            return 'INFO', 'native', ('Player joined: ' if kind == 'player_joined' else 'Player left: ') + fields['name']
        if kind == 'state':
            message = STATES.get(fields.get('state'), 'Runtime state changed.')
            if fields.get('state') in ('ERROR', 'RECONNECTING'):
                level = 'ERROR' if fields['state'] == 'ERROR' else 'WARN'
                message += ' ' + visible(fields.get('reason', '')).replace('_', ' ').capitalize().rstrip('.') + '.'
        elif kind == 'cycle_monitor':
            active = fields.get('active')
            maximum = fields.get('maximum')
            counts = f'{active}/{maximum}' if type(active) is int and type(maximum) is int else 'unavailable'
            suffix = '; capacity full' if fields.get('capacity_full') is True else ''
            maps = fields.get('cycle')
            maps = ','.join(visible(item,32) for item in maps) if type(maps) is list else 'unavailable'
            lobby = fields.get('lobby')
            if type(lobby) is dict:
                lobby_text = visible(lobby.get('preset_id',''),32) + ' #' + str(lobby.get('host_counter'))
            else:
                lobby_text = 'none' if fields.get('lobby_availability')=='none' else 'unavailable'
            transition = fields.get('last_transition') or 'unavailable'
            finished=fields.get('finished'); errors=fields.get('rotation_errors')
            finished=str(finished) if type(finished) is int else 'unavailable'
            errors=str(errors) if type(errors) is int else 'unavailable'
            message = (f"Cycle [{maps}]; position {fields['position']}/{fields['total']}; "
                       f"current {visible(fields['current'])}; next {visible(fields['next_preset'])}; "
                       f"active matches {counts}{suffix}; lobby {lobby_text}; "
                       f"retired matches {finished}; rotation errors {errors}; last transition {visible(transition)}.")
        elif kind in ('lobby_opened', 'lobby_rehosted'):
            component = 'native'
            message = ('Lobby opened: ' if kind == 'lobby_opened' else 'Lobby rehosted: ') + visible(fields['name'])
            message += f" ({fields['humans']} humans, {fields['computers']} computers)."
        elif kind == 'population':
            component, message = 'native', f"Participants: {fields['humans']} humans, {fields['computers']} computers."
        elif kind in ('game_started', 'game_ended'):
            component = 'native'
            message = ('Game started: ' if kind == 'game_started' else 'Game ended: ') + visible(fields['name']) + '.'
        elif kind == 'countdown_started':
            component, message = 'native', f"Countdown started ({fields['seconds']} seconds)."
        elif kind == 'countdown_cancelled':
            component, message = 'native', 'Countdown cancelled; waiting in the lobby.'
        elif kind == 'refresh_confirmed':
            component, message = 'native', f"Lobby refresh acknowledged ({fields['count']} new acknowledgements; {fields['total']} total)."
        elif kind == 'refresh_failed':
            level, component, message = 'WARN', 'native', f"Lobby refresh failed ({fields['count']} new failures; {fields['total']} total); see diagnostics."
        elif kind in ('component_started', 'child_started'):
            component = fields.get('component', 'native')
            message = 'Started.'
        elif kind == 'component_stopped':
            code = fields.get('exit_code')
            level = 'INFO' if code == 0 else 'WARN'
            message = f'Stopped (exit code {code}).'
        elif kind == 'retry_scheduled':
            level, message = 'WARN', f"Restart attempt {fields.get('attempt', 0)} in {fields.get('delay_seconds', 0):g} seconds."
        elif kind == 'forced_stop':
            level, message = 'WARN', 'Process did not stop in time; forced shutdown.'
        elif kind == 'component_event':
            event = fields.get('worker_event', '')
            if set(event.upper().split('_')) & {'ACK', 'REFRESH', 'PUBLICATION', 'PUBLISH', 'ADVERTISEMENT', 'DIRECTORY', 'HEARTBEAT', 'STATUS'}:
                return None  # Periodic records stay in diagnostics; native counters summarize acknowledgements.
            if any(token in event.upper() for token in ('FAILED', 'REJECTED', 'ERROR')):
                level = 'WARN'
            message = visible(event).replace('_', ' ').capitalize() + '.'
        elif kind == 'hostbot':
            text = fields.get('message', '')
            local = LOCAL.fullmatch(text)
            chat = LOBBY_CHAT.fullmatch(text)
            if local:
                component, message = 'native', 'Lobby message: ' + visible(local['message'], 512)
            elif chat and safe_name(chat['name']):
                # Keep each player message, including repeated commands. The
                # fixed prefix cannot be confused with Analytics join/leave.
                return 'INFO', 'native', 'Player chat [' + chat['name'] + ']: ' + visible(chat['message'], 512)
            elif text.startswith('[GHOST] ') and re.search(r'\berror\b|\bfailed\b|\binvalid\b', text, re.I):
                level, component, message = 'WARN', 'native', visible(text[8:])
        elif kind in ('native_metadata_rejected', 'prefix_cleanup_failed', 'runtime_control_failed', 'launch_error'):
            if kind == 'native_metadata_rejected':
                component = 'native'
            level = 'ERROR' if kind in ('runtime_control_failed', 'launch_error') else 'WARN'
            message = {'native_metadata_rejected': 'Rejected invalid native telemetry; see diagnostics.',
                       'prefix_cleanup_failed': 'Could not clean up the worker processes.',
                       'runtime_control_failed': 'Runtime control failed; see diagnostics.',
                       'launch_error': 'Could not start the process; see diagnostics.'}[kind]
        elif kind == 'circuit_half_open':
            message = 'Recovery cooldown ended; trying again.'
        if message is None:
            return None
        signature = (level, component, message)
        if kind in ('lobby_opened', 'lobby_rehosted', 'population', 'game_started', 'game_ended',
                    'countdown_started', 'countdown_cancelled', 'refresh_confirmed', 'refresh_failed',
                    'component_started', 'component_stopped', 'child_started', 'retry_scheduled', 'forced_stop'):
            return signature
        key = (kind, component)
        if self.last_messages.get(key) == signature:
            if not (kind == 'hostbot' and LOCAL.fullmatch(fields.get('message', ''))
                    and self.local_message_at is not None and time.monotonic() - self.local_message_at >= 5):
                return None
        if kind == 'hostbot' and LOCAL.fullmatch(fields.get('message', '')):
            self.local_message_at = time.monotonic()
        if len(self.last_messages) >= 64:
            self.last_messages.clear()
        self.last_messages[key] = signature
        return signature


class OperatorLogMixin:
    def _operator(self):
        if not hasattr(self, '_operator_events'):
            self._operator_events = OperatorEvents()
        return self._operator_events

    def _operator_emit(self, events):
        for kind, fields in events:
            self.log(kind, **fields)

    def operator_native_line(self, line):
        with self.guard:
            self._operator_emit(self._operator().peer_line(line))

    def operator_native_state(self, state):
        with self.guard:
            self._operator_emit(self._operator().state(state, time.monotonic()))

    def operator_native_reset(self):
        with self.guard:
            self._operator_emit(self._operator().reset())

    def log(self, kind, **fields):
        fields = safe_fields(fields)
        instant = datetime.now(timezone.utc)
        item = {'time': instant.isoformat(), 'event': kind, **fields}
        data = json.dumps(item, ensure_ascii=False) + '\n'
        with self.guard:
            events = self._operator()
            if kind == 'component_stopped' and fields.get('component') == 'native':
                self._operator_emit(events.reset())
            journal = self.runtime / 'supervisor.jsonl'
            if journal.is_symlink():
                raise ValueError('OPERATOR_JOURNAL_REJECTED')
            if journal.exists() and journal.stat().st_size + len(data.encode()) > JOURNAL_LIMIT:
                for index in (2, 1):
                    old = journal.with_suffix(f'.jsonl.{index}')
                    if old.exists():
                        old.replace(journal.with_suffix(f'.jsonl.{index + 1}'))
                journal.replace(journal.with_suffix('.jsonl.1'))
            fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(data)
            rendered = events.prose(kind, fields)
            if rendered:
                level, component, message = rendered
                print(f'{instant:%Y-%m-%d %H:%M:%S} {level} {component}: {message}', file=self.output, flush=True)
