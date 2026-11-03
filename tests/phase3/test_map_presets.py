"""Offline MPQ fixtures and preset compilation; no account, engine or network."""
import copy
import ctypes as C
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from src.runtime.config import GAMEPLAY_DEFAULTS, gameplay_config, iccup_config
from src.runtime.controller import Child, ICCupSupervisor
from src.runtime.mpq import LIBRARY, extract_map, storm_api
from src.runtime.native import native_state, prepare_native
from src.runtime.presets import (AI_OFF, analyze_map, load_presets, lobby_name,
                                 map_basename, map_info, resolve_preset,
                                 script_evidence, short_title, matching_ai_approval)
from amp.map_discovery import automatic_approval
from src.supervisor.__main__ import serve

ROOT = Path(__file__).resolve().parents[2]
STORM = os.environ.get('WAR3_STORM_LIBRARY', LIBRARY)
AI_SCRIPT = b'''function Computer takes nothing returns boolean
return GetPlayerController(Player(0)) == MAP_CONTROL_COMPUTER
endfunction
function Act takes nothing returns nothing
call StartMeleeAI(Player(0), "imported\\\\real.ai")
call IssueImmediateOrder(GetTriggerUnit(), "attack")
endfunction
function main takes nothing returns nothing
call TriggerAddAction(CreateTrigger(), function Act)
endfunction
'''
PLAIN_SCRIPT = b'function main takes nothing returns nothing\nendfunction\n'


class ApprovalSelectionTests(unittest.TestCase):
    def setUp(self):
        self.analysis = {'map_sha256': 'a' * 64, 'script_sha256': 'b' * 64,
                         'ai_evidence': script_evidence(AI_SCRIPT)}
        self.record = automatic_approval(self.analysis, [])

    def test_implicit_unique_match_and_explicit_legacy(self):
        self.assertEqual(matching_ai_approval({}, {'reviewed': self.record}, self.analysis), self.record)
        self.assertEqual(matching_ai_approval({'capability': 'reviewed'}, {'reviewed': self.record}, self.analysis), self.record)
        with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
            matching_ai_approval({'capability': 'other'}, {'reviewed': self.record}, self.analysis)

    def test_mismatches_and_ambiguity_fail_closed(self):
        for field in ('map_sha256', 'script_sha256'):
            changed = copy.deepcopy(self.record)
            changed[field] = 'c' * 64
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
                matching_ai_approval({}, {'reviewed': changed}, self.analysis)
        with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
            matching_ai_approval({}, {'a': self.record, 'b': self.record}, self.analysis)
        no_features = {**self.analysis, 'ai_evidence': {'features': []}}
        with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
            matching_ai_approval({}, {'a': self.record}, no_features)

    def test_record_needs_evidence_and_makes_no_runtime_claim(self):
        trigger = b'function f takes nothing returns nothing\nif GetPlayerController(Player(0)) == MAP_CONTROL_COMPUTER then\ncall IssueImmediateOrder(Player(0), "attack")\ncall TriggerAddAction(CreateTrigger(), function f)\nendif\nendfunction'
        data = {**self.analysis, 'ai_evidence': script_evidence(trigger)}
        self.assertEqual(automatic_approval(data, [])['features'], ['computer_control'])
        self.assertFalse(automatic_approval(data, [])['runtime_tested'])
        self.assertIsNone(automatic_approval({**data, 'ai_evidence': script_evidence(PLAIN_SCRIPT)}, []))

    def test_resolve_enabled_selection_and_changed_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'maps').mkdir()
            (root / 'maps' / 'test.w3x').write_bytes(b'fixture')
            source = root / 'config' / 'supervisor.json'
            source.write_text('{}')
            doc = document()
            doc['presets'][1]['ai'] = {'enabled': True, 'target': 4, 'skill': 2}
            doc['capabilities'] = [self.record]
            (root / 'config' / 'presets.json').write_text(json.dumps(doc))
            slots = [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(12)]
            analysis = {**self.analysis, 'slots': slots, 'human_capacity': 12, 'lobby_name': 'Test'}
            with patch('src.runtime.presets.analyze_map', return_value=analysis):
                _, native, _ = resolve_preset(source, {'map_preset': 'test-map'})
                self.assertEqual((native['bot_lobby_ai_supported'], native['bot_lobby_ai_target'], native['bot_lobby_ai_skill']), ('1', '4', '2'))
            with patch('src.runtime.presets.analyze_map', return_value={**analysis, 'map_sha256': 'c' * 64}):
                with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
                    resolve_preset(source, {'map_preset': 'test-map'})
            doc['presets'][1]['ai']['enabled'] = False
            (root / 'config' / 'presets.json').write_text(json.dumps(doc))
            with patch('src.runtime.presets.analyze_map', return_value=analysis):
                self.assertEqual(resolve_preset(source, {'map_preset': 'test-map'})[1], AI_OFF)

    def test_startup_emits_only_fixed_actionable_ai_error(self):
        from src.supervisor.__main__ import main
        from src.runtime.presets import AIEvidenceError
        error = io.StringIO()
        with patch('sys.argv', ['supervisor', '--config', 'ignored']), \
             patch('src.supervisor.__main__.load_config', side_effect=AIEvidenceError('secret')), \
             patch('sys.stderr', error):
            self.assertEqual(main(), 2)
        message = json.loads(error.getvalue())
        self.assertEqual(message['error'], 'PRESET_AI_EVIDENCE_REJECTED')
        self.assertIn('Disable AI', message['message'])
        self.assertNotIn('secret', error.getvalue())


def uint(*values):
    return struct.pack('<' + 'I' * len(values), *values)


def info_fixture(version=25, statuses=None, flags=32, title=b'Test Map', colours=None):
    statuses = statuses or [1] * 12
    colours = colours or list(range(len(statuses)))
    data = uint(version, 0, 0) + title + b'\0Author\0Description\0Players\0'
    data += b'\0' * 48 + uint(64, 64, flags) + b'L' + uint(0)
    if version == 25:
        data += b'\0'
    data += b'\0' * 3 + uint(0)
    if version == 25:
        data += b'\0'
    data += b'\0' * 3
    if version == 25:
        data += b'\0' * 24 + b'\0' + b'\0' * 5
    data += uint(len(statuses))
    for colour, status in zip(colours, statuses):
        data += uint(colour, status, 1, 1) + b'Player\0' + b'\0' * 16
    data += uint(2, 11, 0x3f) + b'Team A\0' + uint(11, 0xfc0) + b'Team B\0'
    return data + uint(0, 0, 0, 0)


def document():
    return {'version': 1, 'presets': [{'id': 'manual', 'enabled': True},
            {'id': 'test-map', 'enabled': True, 'map_filename': 'test.w3x',
             'lobby_name': 'Exact Test Map', 'max_players': 12,
             'start': {'autostart_players': 6}, 'vote': {'min_players': 2},
             'ai': {'enabled': False}}], 'capabilities': []}


class NativeTelemetryTests(unittest.TestCase):
    def line(self, **fields):
        return '[HOSTBOT_STATE] ' + json.dumps({'version': 1, 'phase': 'LOBBY', **fields})

    def test_old_state_defaults_are_zero_and_false(self):
        state = native_state(self.line(players=2))
        self.assertEqual(state['players'], 2)
        self.assertEqual(state['computer_players'], 0)
        self.assertEqual(state['ai_target'], 0)
        self.assertIs(state['ai_supported'], False)

    def test_ai_counts_are_bounded_integers_and_support_is_boolean(self):
        for count in (0, 1, 6, 12):
            with self.subTest(count=count):
                state = native_state(self.line(computer_players=count, ai_target=count, ai_supported=True))
                self.assertEqual(state['computer_players'], count)
                self.assertEqual(state['ai_target'], count)
                self.assertIs(state['ai_supported'], True)
        for key in ('computer_players', 'ai_target'):
            for bad in (-1, 13, 2**32, True, False, 6.0, '6', None, [], {}):
                with self.subTest(key=key, value=bad), self.assertRaises(ValueError):
                    native_state(self.line(**{key: bad}))
        for bad in (0, 1, 'true', None, [], {}):
            with self.subTest(ai_supported=bad), self.assertRaises(ValueError):
                native_state(self.line(ai_supported=bad))

    def test_status_preserves_observed_join_leave_counts_and_rejects_invalid_state(self):
        # Exercise the production parser -> observation -> real /status HTTP path.
        # Feed synthetic native lines; no native process, account or engine runs.
        with tempfile.TemporaryDirectory() as directory:
            cfg = {'mode': 'iccup', 'runtime_dir': str(Path(directory) / 'supervisor'),
                   'hostbot': {'command': ['unused'], 'cwd': directory},
                   'http': {'host': '127.0.0.1', 'port': 0},
                   'iccup': iccup_config({}), 'gameplay': gameplay_config({})}
            backend = SimpleNamespace(prepare=lambda: None, close=lambda: None)
            supervisor = ICCupSupervisor(cfg, output=io.StringIO(), backend=backend)
            self.addCleanup(supervisor.lockfile.close)
            supervisor.children['native'] = Child(SimpleNamespace(pid=123, poll=lambda: None), 1, time.monotonic())
            server = serve(supervisor)
            try:
                def status():
                    with urlopen('http://127.0.0.1:' + str(server.server_port) + '/status', timeout=2) as response:
                        self.assertEqual(response.status, 200)
                        return json.load(response)['game']

                for humans, computers in ((0, 6), (1, 5), (2, 4), (1, 5), (6, 0), (7, 0)):
                    supervisor._native_line(1, self.line(players=humans, computer_players=computers,
                                                         ai_target=6, ai_supported=True))
                    state = status()
                    self.assertEqual((state['players'], state['computer_players']), (humans, computers))
                    self.assertEqual(state['ai_target'], 6)
                    self.assertIs(state['ai_supported'], True)
                before = status()
                supervisor._native_line(1, self.line(computer_players=13))
                self.assertEqual(status(), before)
                supervisor._native_line(2, self.line(players=0, computer_players=6))
                self.assertEqual(status(), before)  # Ignore stale/unowned generation.
                supervisor._native_line(1, self.line(players=2))
                state = status()
                self.assertEqual((state['players'], state['computer_players'], state['ai_target']), (2, 0, 0))
                self.assertIs(state['ai_supported'], False)
            finally:
                server.shutdown()
                server.server_close()


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'presets.json'

    def write(self, data):
        self.path.write_text(json.dumps(data))
        return load_presets(self.path)

    def test_defaults_and_raw_minimum_never_bypassed(self):
        self.assertEqual(GAMEPLAY_DEFAULTS['map_preset'], 'manual')
        self.assertEqual(GAMEPLAY_DEFAULTS['host_name'], 'War3HostBot')
        for raw in ({'map_preset': '../x'}, {'map_preset': 'A name'}, {'map_preset': 1},
                    {'vote_min_players': 1}, {'bot_lobby_ai_supported': 1}, {'min_humans': 1}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                gameplay_config(raw)

    def test_autostart_default_boolean_and_disabled_threshold_validation(self):
        self.assertIs(gameplay_config({})['autostart_enabled'], True)
        cfg = gameplay_config({'autostart_enabled': False, 'max_players': 2, 'autostart_players': 10})
        self.assertIs(cfg['autostart_enabled'], False)
        self.assertEqual(cfg['autostart_players'], 10)
        for bad in (0, 1, 'false', None, []):
            with self.subTest(flag=bad), self.assertRaises(ValueError):
                gameplay_config({'autostart_enabled': bad})
            doc = document()
            doc['presets'][1]['start']['autostart_enabled'] = bad
            with self.assertRaises(ValueError):
                self.write(doc)
        for raw in ({'autostart_enabled': False, 'autostart_players': 0},
                    {'autostart_enabled': False, 'autostart_players': 13},
                    {'autostart_enabled': True, 'max_players': 2, 'autostart_players': 10},
                    {'autostart_enabled': False, 'max_players': 2, 'vote_min_players': 3}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                gameplay_config(raw)

    def test_names_are_verbatim_bounded_utf8_no_counter_or_cfg_injection(self):
        self.assertEqual(lobby_name('Footman Ultimate AI'), 'Footman Ultimate AI')
        for value in ('', ' ', ' x', 'x ', 'x' * 25, 'я' * 13, 'x#1', 'x\n', 'x\x7f',
                      'x\u202e', 'x=y', '[x]', '|cffff0000x', 'x\\y', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lobby_name(value)
        self.assertEqual(short_title(b'|cffff0000A Map|r'), 'A Map')
        self.assertEqual(short_title(b'TRIGSTR_041', b'STRING 41\n{\nAn Actual Title\n}'), 'An Actual Title')
        self.assertEqual(len(short_title(('я' * 20).encode()).encode()), 24)
        with self.assertRaises(ValueError):
            short_title(b'TRIGSTR_99')

    def test_basename_rejects_traversal_paths_and_injection(self):
        self.assertEqual(map_basename('A Map-v1.2.w3x'), 'A Map-v1.2.w3x')
        for value in ('../x.w3x', '/x.w3x', 'maps/x.w3x', 'C:\\x.w3x', 'x..w3x',
                      'x.w3x\n', 'x=1.w3x', 'x.cfg', 'x.w3x#1', '', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                map_basename(value)

    def test_document_limits_duplicates_unknown_fields_and_types(self):
        changes = [lambda d: d.update(extra=True),
                   lambda d: d.update(version=True),
                   lambda d: d.update(version=2),
                   lambda d: d.update(presets=[]),
                   lambda d: d.update(presets=[d['presets'][0]] * 33),
                   lambda d: d['presets'].append(copy.deepcopy(d['presets'][1])),
                   lambda d: d['presets'][1].update(unknown=1),
                   lambda d: d['presets'][1].update(enabled=1),
                   lambda d: d['presets'][1].update(max_players=True),
                   lambda d: d['presets'][1].update(lobby_name='Bad#1'),
                   lambda d: d['presets'][1]['start'].update(autostart_players=1),
                   lambda d: d['presets'][1]['start'].update(countdown_seconds=61),
                   lambda d: d['presets'][1]['vote'].update(min_players=0),
                   lambda d: d['presets'][1]['vote'].update(required_percent=101),
                   lambda d: d['presets'][1]['vote'].update(unknown=1),
                   lambda d: d['presets'][1]['ai'].update(target=1),
                   lambda d: d['presets'][1]['ai'].update(target=13),
                   lambda d: d['presets'][1]['ai'].update(target=True),
                   lambda d: d['presets'][1]['ai'].update(target=6.0),
                   lambda d: d['presets'][1]['ai'].update(skill=3),
                   lambda d: d['presets'][0].update(map_filename='x.w3x')]
        for change in changes:
            d = document()
            change(d)
            with self.subTest(document=d), self.assertRaises(ValueError):
                self.write(d)
        for text in ('{"version":1,"version":1,"presets":[]}',
                     '{"version":NaN,"presets":[]}', '{', '[' * 1100, ' ' * 65537):
            self.path.write_text(text)
            with self.subTest(text=text[:80]), self.assertRaises(ValueError):
                load_presets(self.path)

    def test_examples_and_schema(self):
        presets, caps = load_presets(ROOT / 'config/presets.example.json')
        self.assertFalse(presets['ai-template']['enabled'])
        self.assertFalse(presets['ai-template']['ai']['enabled'])
        self.assertFalse(presets['ai-template']['start']['autostart_enabled'])
        self.assertTrue(presets['footman-ultimate-ai']['start']['autostart_enabled'])
        self.assertEqual(presets['footman-ultimate-ai']['start']['autostart_players'], 4)
        self.assertEqual(presets['footman-ultimate-ai']['vote']['min_players'], 2)
        self.assertEqual(presets['footman-ultimate-ai']['ai']['target'], 4)
        self.assertFalse(caps['footman-v11-static']['runtime_tested'])
        schema = json.loads((ROOT / 'config/presets.schema.json').read_text())
        self.assertFalse(schema['additionalProperties'])

    def test_symlink_json_rejected(self):
        self.path.symlink_to(ROOT / 'config/presets.example.json')
        with self.assertRaises(ValueError):
            load_presets(self.path)

    def test_comments_literals_and_unused_ai_never_detect_capability(self):
        for text in (PLAIN_SCRIPT, b'// call StartMeleeAI(Player(0),"x.ai")',
                     b'/* call StartCampaignAI(Player(0),"x.ai") */',
                     b'call DisplayTextToPlayer(Player(0),0,0,"call StartMeleeAI( // MAP_CONTROL_COMPUTER")',
                     b'native StartMeleeAI takes player p, string s returns nothing'):
            with self.subTest(text=text):
                self.assertEqual(script_evidence(text), {'level': 'none', 'features': []})
        detected = script_evidence(b'if GetPlayerController(Player(0))==MAP_CONTROL_COMPUTER then\nendif')
        self.assertEqual(detected['level'], 'detected')
        self.assertEqual(detected['features'], [])
        self.assertEqual(script_evidence(AI_SCRIPT)['features'], ['ai_start', 'computer_control'])
        for script in (b'/* call StartMeleeAI(', b'call DisplayTextToPlayer("bad', b'\x00'):
            with self.assertRaises(ValueError):
                script_evidence(script)

    def test_classic_layout_truncation_versions_and_roles(self):
        for version in (18, 25):
            info = map_info(info_fixture(version))
            self.assertEqual(info['human_capacity'], 12)
            self.assertEqual([s[4] for s in info['slots']], [0] * 6 + [1] * 6)
        info = map_info(info_fixture(statuses=[1, 2, 3, 4, 0]))
        self.assertEqual(info['human_capacity'], 1)
        self.assertEqual(len(info['slots']), 2)
        self.assertEqual(info['slots'][1][2:4], [2, 1])
        for data in (uint(28), uint(31), info_fixture()[:90], info_fixture()[:200],
                     info_fixture(statuses=[1, 1], colours=[0, 0]),
                     info_fixture(statuses=[1], colours=[12]), info_fixture(statuses=[5])):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                map_info(data)


@unittest.skipUnless(Path(STORM).is_file(), 'StormLib required; run in the existing offline runtime image')
class MPQTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config/maps').mkdir(parents=True)
        (self.root / 'maps').mkdir()
        self.source = self.root / 'config/ghost.cfg'
        self.source.write_text('bnet_server =\nbot_defaultmap = selected.cfg\n'
                               'bot_lobby_ai_target = 12\nbot_lobby_ai_supported = 1\n'
                               'bot_lobby_min_humans = 1\nbot_vote_min_players = 1\n')
        self.operator = self.root / 'config/maps/selected.cfg'
        self.operator.write_text('map_localpath = operator.w3x\n')
        self.operator.chmod(0o444)
        self.source.chmod(0o444)
        self.cfg = {'runtime_dir': str(self.root / 'state/supervisor'),
                    'hostbot': {'cwd': str(self.root / 'hostbot')},
                    'iccup': iccup_config({'native_config': str(self.source), 'bridge_dir': str(self.root / 'bridge')}),
                    'gameplay': gameplay_config({})}
        self.doc = document()
        original = extract_map
        self.extract = patch('src.runtime.presets.extract_map',
                             side_effect=lambda p: original(p, library=STORM))
        self.extract.start()
        self.addCleanup(self.extract.stop)

    def test_missing_archive_never_closes_standard_input(self):
        code = "import ctypes as c,os,sys; from src.runtime.mpq import storm_api; api=storm_api(sys.argv[1]); h=c.c_void_p(); assert not api['open'](b'/nonexistent-war3-test.mpq',0,0x80100,c.byref(h)); os.fstat(0)"
        result = subprocess.run([sys.executable, '-c', code, str(STORM)], input=b'',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def mpq(self, script=AI_SCRIPT, info=None, members=None, name='test.w3x'):
        path = self.root / 'maps' / name
        # Production extractor only binds read APIs. Fixture creation stays in tests.
        lib = C.CDLL(STORM)
        create = getattr(lib, 'SFileCreateArchive', None)
        create_flags = 0 if create else 2
        if create is None:
            create = getattr(lib, 'SFileCreateArchiveEx', None) or getattr(lib, '_Z20SFileCreateArchiveExPKcjjPPv')
        create.argtypes = [C.c_char_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_void_p)]
        create.restype = C.c_bool
        add = getattr(lib, 'SFileAddFileEx', None) or getattr(lib, '_Z14SFileAddFileExPvPKcS1_jji')
        add.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p, C.c_uint32, C.c_uint32, C.c_int]
        add.restype = C.c_bool
        archive = C.c_void_p()
        self.assertTrue(create(os.fsencode(path), create_flags, 32, C.byref(archive)))
        api = storm_api(STORM)
        try:
            data = members if members is not None else {'war3map.w3i': info or info_fixture(), 'war3map.j': script}
            for i, (member, content) in enumerate(data.items()):
                local = self.root / ('member' + str(i))
                local.write_bytes(content)
                self.assertTrue(add(archive, os.fsencode(local), member.encode('ascii'), 0, 0, 0))
        finally:
            api['close'](archive)
        return path

    def save(self):
        (self.root / 'config/presets.json').write_text(json.dumps(self.doc))

    def approve(self, path):
        analysis = analyze_map(path)
        self.doc['capabilities'] = [{'id': 'reviewed', 'level': 'approved_preset',
                                    'map_sha256': analysis['map_sha256'],
                                    'script_sha256': analysis['script_sha256'],
                                    'features': ['ai_start', 'computer_control'],
                                    'review': 'Synthetic static fixture; no runtime claim.', 'runtime_tested': False}]
        self.doc['presets'][1]['ai'] = {'enabled': True, 'target': 6, 'skill': 1, 'capability': 'reviewed'}
        self.doc['presets'][1]['vote']['min_players'] = 1
        self.save()

    def resolve(self):
        return resolve_preset(self.source, gameplay_config({'map_preset': 'test-map'}))

    def test_real_mpq_script_fallback_wts_and_hashes(self):
        p = self.mpq(members={'war3map.w3i': info_fixture(title=b'TRIGSTR_1'),
                              'scripts\\war3map.j': AI_SCRIPT,
                              'war3map.wts': b'STRING 1\r\n{\r\nActual Map\r\n}'})
        data = analyze_map(p)
        self.assertEqual(data['lobby_name'], 'Actual Map')
        self.assertEqual(data['map_sha256'], hashlib.sha256(p.read_bytes()).hexdigest())
        self.assertEqual(data['script_sha256'], hashlib.sha256(AI_SCRIPT).hexdigest())
        self.assertEqual(data['ai_evidence']['level'], 'candidate')
        p.chmod(0o444)
        self.assertEqual(analyze_map(p), data)

    def test_empty_optional_wts_is_accepted(self):
        p = self.mpq(members={'war3map.w3i': info_fixture(), 'war3map.j': PLAIN_SCRIPT,
                              'war3map.wts': b''})
        self.assertEqual(analyze_map(p)['script_sha256'], hashlib.sha256(PLAIN_SCRIPT).hexdigest())

    def test_approved_preset_private_config_and_manual_reset(self):
        p = self.mpq()
        self.approve(p)
        before = (self.source.read_bytes(), self.operator.read_bytes(), p.read_bytes())
        self.cfg['gameplay']['map_cycle'] = ['test-map']
        effective = prepare_native(self.cfg, 'SyntheticUser')
        text = effective.read_text()
        for key, val in (('bot_lobby_ai_target', '6'), ('bot_lobby_ai_supported', '1'),
                         ('bot_lobby_ai_skill', '1'), ('bot_lobby_min_humans', '1'),
                         ('bot_vote_min_players', '1'), ('bot_autostartplayers', '6'),
                         ('autohost_startplayers', '6'), ('bot_maxplayers', '12'),
                         ('autohost_gamename', 'Exact Test Map'), ('bot_defaultmap', 'preset.cfg')):
            self.assertIn(key + ' = ' + val + '\n', text)
        generated = self.root / 'state/native/mapcfg/preset.cfg'
        self.assertEqual(generated.stat().st_mode & 0o777, 0o600)
        self.assertIn('map_localpath = test.w3x\n', generated.read_text())
        self.assertEqual(generated.read_text().count('0 255 0 0'), 12)
        self.assertEqual(before, (self.source.read_bytes(), self.operator.read_bytes(), p.read_bytes()))
        self.assertNotIn('#', text)
        self.cfg['gameplay']['map_cycle'] = ['manual']
        text = prepare_native(self.cfg, 'SyntheticUser').read_text()
        for key, value in AI_OFF.items():
            self.assertIn(key + ' = ' + value + '\n', text)
        self.assertIn('bot_defaultmap = selected.cfg\n', text)
        self.assertIn('bot_vote_min_players = 2\n', text)
        self.assertTrue((self.root / 'state/mapcfg/selected.cfg').is_symlink())

    def test_unknown_map_cannot_enable_ai_even_with_name_or_unused_resource(self):
        self.mpq(members={'war3map.w3i': info_fixture(), 'war3map.j': PLAIN_SCRIPT,
                          'unused.ai': AI_SCRIPT})
        entry = self.doc['presets'][1]
        entry['lobby_name'] = 'A Map With AI'
        self.save()
        game, native, _ = self.resolve()
        self.assertEqual(native, AI_OFF)
        entry['vote']['min_players'] = 1
        self.save()
        with self.assertRaisesRegex(ValueError, 'SINGLE_HUMAN'):
            self.resolve()
        entry['ai'] = {'enabled': True}
        self.save()
        with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
            self.resolve()

    def test_preset_autostart_flag_is_independent_of_votes_and_manual_fallback(self):
        self.approve(self.mpq())
        entry = self.doc['presets'][1]
        self.cfg['gameplay']['map_cycle'] = ['test-map']
        for enabled, vote_enabled in ((False, True), (False, False), (True, False)):
            entry['start']['autostart_enabled'] = enabled
            entry['vote']['enabled'] = vote_enabled
            self.save()
            text = prepare_native(self.cfg, 'SyntheticUser').read_text()
            with self.subTest(autostart=enabled, vote=vote_enabled):
                for key, value in (('bot_autostart_enabled', str(int(enabled))),
                                   ('bot_autostartplayers', '6'), ('autohost_startplayers', '6'),
                                   ('bot_vote_enabled', str(int(vote_enabled))),
                                   ('bot_vote_min_players', '1'), ('autohost_maxgames', '1')):
                    self.assertIn(key + ' = ' + value + '\n', text)
        del entry['start']['autostart_enabled']
        self.save()
        self.cfg['gameplay']['autostart_enabled'] = False
        # An older preset without the flag defaults true; raw false is manual-only.
        self.assertIn('bot_autostart_enabled = 1\n', prepare_native(self.cfg, 'SyntheticUser').read_text())
        self.cfg['gameplay']['map_cycle'] = ['manual']
        text = prepare_native(self.cfg, 'SyntheticUser').read_text()
        self.assertIn('bot_autostart_enabled = 0\n', text)
        self.assertIn('bot_autostartplayers = 10\n', text)
        self.assertIn('bot_vote_enabled = 1\n', text)

    def test_disabled_preset_keeps_threshold_above_maximum_but_not_invalid_vote(self):
        self.mpq(script=PLAIN_SCRIPT)
        entry = self.doc['presets'][1]
        entry['max_players'] = 2
        entry['start'] = {'autostart_enabled': False, 'autostart_players': 10}
        self.save()
        self.cfg['gameplay']['map_cycle'] = ['test-map']
        text = prepare_native(self.cfg, 'SyntheticUser').read_text()
        for line in ('bot_autostart_enabled = 0', 'bot_autostartplayers = 10',
                     'autohost_startplayers = 10', 'bot_maxplayers = 2', 'bot_vote_min_players = 2'):
            self.assertIn(line + '\n', text)
        entry['start']['autostart_enabled'] = True
        self.save()
        with self.assertRaisesRegex(ValueError, 'PLAYER_LIMIT'):
            self.resolve()
        entry['start']['autostart_enabled'] = False
        entry['vote']['min_players'] = 3
        self.save()
        with self.assertRaisesRegex(ValueError, 'PLAYER_LIMIT'):
            self.resolve()
        entry['vote']['min_players'] = 2
        for bad_threshold in (0, 1, 13):
            entry['start']['autostart_players'] = bad_threshold
            self.save()
            with self.subTest(threshold=bad_threshold), self.assertRaises(ValueError):
                self.resolve()

    def test_map_hash_script_hash_feature_and_approval_mismatches_rejected(self):
        p = self.mpq()
        self.approve(p)
        valid = copy.deepcopy(self.doc)
        for key, value in (('map_sha256', '0' * 64), ('script_sha256', '0' * 64),
                           ('features', ['unknown']), ('level', 'candidate')):
            self.doc = copy.deepcopy(valid)
            self.doc['capabilities'][0][key] = value
            self.save()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolve()
        self.doc = valid
        self.doc['capabilities'].append(copy.deepcopy(valid['capabilities'][0]))
        self.save()
        with self.assertRaises(ValueError):
            self.resolve()

    def test_approval_cannot_assert_absent_features(self):
        p = self.mpq(script=PLAIN_SCRIPT)
        self.approve(p)
        with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
            self.resolve()

    def test_approved_ai_target_range_and_default(self):
        self.approve(self.mpq())
        ai = self.doc['presets'][1]['ai']
        for target in range(2, 13):
            ai['target'] = target
            self.save()
            with self.subTest(target=target):
                game, native, _ = self.resolve()
                self.assertEqual(native['bot_lobby_ai_target'], str(target))
                self.assertEqual(native['bot_lobby_ai_supported'], '1')
                self.assertEqual(game['max_players'], 12)
        del ai['target']
        self.save()
        self.assertEqual(self.resolve()[1]['bot_lobby_ai_target'], '6')

    def test_ai_target_cannot_exceed_map_or_configured_capacity(self):
        self.approve(self.mpq(info=info_fixture(statuses=[1] * 4)))
        entry = self.doc['presets'][1]
        entry['max_players'] = 4
        entry['start']['autostart_players'] = 4
        for target in (5, 6, 12):
            entry['ai']['target'] = target
            self.save()
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'AI_TARGET'):
                self.resolve()
        entry['ai']['target'] = 4
        self.save()
        self.assertEqual(self.resolve()[1]['bot_lobby_ai_target'], '4')
        entry['max_players'] = 3
        entry['start']['autostart_players'] = 3
        self.save()
        with self.assertRaisesRegex(ValueError, 'AI_TARGET'):
            self.resolve()
        entry['ai']['target'] = 3
        self.save()
        self.assertEqual(self.resolve()[1]['bot_lobby_ai_target'], '3')

    def test_capacity_thresholds_disabled_unknown_and_slot_roles(self):
        p = self.mpq(info=info_fixture(statuses=[1] * 6 + [2, 3, 4]))
        self.approve(p)
        entry = self.doc['presets'][1]
        with self.assertRaisesRegex(ValueError, 'CAPACITY'):
            self.resolve()
        entry['max_players'] = 6
        self.save()
        game, native, text = self.resolve()
        self.assertEqual(game['max_players'], 6)
        self.assertIn('map_slot7 = 0 255 2 1 1 6 1 1 100\n', text)
        self.assertNotIn('map_slot8', text)
        entry['start']['autostart_players'] = 7
        self.save()
        with self.assertRaisesRegex(ValueError, 'PLAYER_LIMIT'):
            self.resolve()
        entry['start']['autostart_players'] = 6
        entry['enabled'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'UNAVAILABLE'):
            self.resolve()
        with self.assertRaises(ValueError):
            resolve_preset(self.source, gameplay_config({'map_preset': 'unknown'}))

    def test_non_ai_capping_only_closes_human_slots(self):
        self.mpq(info=info_fixture(statuses=[1] * 8 + [2]))
        entry = self.doc['presets'][1]
        entry['max_players'] = 6
        entry['start']['autostart_players'] = 5  # One native-capacity slot belongs to the original computer.
        self.save()
        _, native, text = self.resolve()
        self.assertEqual(native, AI_OFF)
        self.assertIn('map_slot6 = 0 255 1 0 0 5 1 1 100\n', text)
        self.assertIn('map_slot7 = 0 255 1 0 1 6 1 1 100\n', text)
        self.assertIn('map_slot9 = 0 255 2 1 1 8 1 1 100\n', text)

    def test_default_maximum_and_autostart_follow_small_map_not_global_limit(self):
        self.mpq(script=PLAIN_SCRIPT, info=info_fixture(statuses=[1] * 4))
        entry = self.doc['presets'][1]
        del entry['max_players']
        del entry['start']
        self.save()
        self.cfg['gameplay'] = gameplay_config({'map_preset': 'test-map', 'max_players': 8, 'autostart_players': 7})
        text = prepare_native(self.cfg, 'SyntheticUser').read_text()
        self.assertIn('bot_maxplayers = 4\n', text)
        self.assertIn('bot_autostartplayers = 4\n', text)
        self.assertIn('autohost_startplayers = 4\n', text)
        entry['start'] = {'autostart_players': 5}
        self.save()
        with self.assertRaisesRegex(ValueError, 'PLAYER_LIMIT'):
            self.resolve()
        entry['start']['autostart_enabled'] = False
        self.save()
        self.assertEqual(self.resolve()[0]['autostart_players'], 5)
        entry['max_players'] = 5
        self.save()
        with self.assertRaisesRegex(ValueError, 'MAP_CAPACITY'):
            self.resolve()

    def test_fixed_computers_consume_non_ai_capacity_and_preserve_original_roles(self):
        self.mpq(script=PLAIN_SCRIPT, info=info_fixture(statuses=[1] * 4 + [2, 2, 3, 4]))
        entry = self.doc['presets'][1]
        del entry['max_players']
        del entry['start']
        self.save()
        game, native, text = self.resolve()
        self.assertEqual((game['max_players'], game['autostart_players']), (6, 4))
        self.assertEqual(native, AI_OFF)
        computer_rows = [line for line in text.splitlines() if '0 255 2 1' in line]
        self.assertEqual(len(computer_rows), 2)
        self.assertNotIn('map_slot7', text)  # Neutral/rescuable roles are not playable capacity.
        entry['max_players'] = 4
        self.save()
        game, native, capped = self.resolve()
        self.assertEqual((game['max_players'], game['autostart_players']), (4, 2))
        self.assertIn('map_slot3 = 0 255 1 0 0 2 1 1 100\n', capped)
        self.assertEqual([line for line in capped.splitlines() if '0 255 2 1' in line], computer_rows)
        self.assertEqual(native, AI_OFF)
        entry['start'] = {'autostart_players': 3}
        self.save()
        with self.assertRaisesRegex(ValueError, 'HUMAN_CAPACITY'):
            self.resolve()
        entry['start']['autostart_enabled'] = False
        entry['vote']['min_players'] = 3
        self.save()
        with self.assertRaisesRegex(ValueError, 'HUMAN_CAPACITY'):
            self.resolve()

    def test_approved_ai_omitted_limits_clamp_to_small_map(self):
        self.approve(self.mpq(info=info_fixture(statuses=[1] * 4)))
        entry = self.doc['presets'][1]
        entry['ai']['target'] = 4
        del entry['max_players']
        del entry['start']
        self.save()
        game, native, _ = self.resolve()
        self.assertEqual((game['max_players'], game['autostart_players']), (4, 4))
        self.assertEqual(native['bot_lobby_ai_supported'], '1')
        self.assertEqual(native['bot_lobby_ai_target'], '4')

    def test_invalid_missing_truncated_oversize_and_new_format(self):
        p = self.root / 'maps/test.w3x'
        p.write_bytes(b'not an MPQ' * 30)
        with self.assertRaises(ValueError):
            analyze_map(p)
        p.unlink()
        self.mpq(members={'war3map.w3i': info_fixture()})
        with self.assertRaises(ValueError):
            analyze_map(p)
        p.unlink()
        self.mpq(info=uint(31))
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED'):
            analyze_map(p)
        p.unlink()
        self.mpq(script=b' ' * (4 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            analyze_map(p)
        p.unlink()
        self.mpq()
        data = p.read_bytes()
        p.write_bytes(data[:len(data) // 2])
        with self.assertRaises(ValueError):
            analyze_map(p)
        with p.open('wb') as stream:
            stream.truncate(128 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'SIZE'):
            analyze_map(p)

    def test_ambiguous_scripts_and_map_symlink_rejected(self):
        p = self.mpq(members={'war3map.w3i': info_fixture(), 'war3map.j': AI_SCRIPT,
                              'scripts\\war3map.j': PLAIN_SCRIPT})
        with self.assertRaises(ValueError):
            analyze_map(p)
        p.unlink()
        p.symlink_to(self.source)
        with self.assertRaises(ValueError):
            analyze_map(p)

    @unittest.skipUnless(os.environ.get('WAR3_TEST_MAP'), 'optional supplied real map')
    def test_supplied_footman_matches_curated_example(self):
        p = Path(os.environ['WAR3_TEST_MAP'])
        data = analyze_map(p)
        _, records = load_presets(ROOT / 'config/presets.example.json')
        record = records['footman-v11-static']
        self.assertEqual(data['map_sha256'], record['map_sha256'])
        self.assertEqual(data['script_sha256'], record['script_sha256'])
        self.assertEqual(data['ai_evidence']['features'], record['features'])
        self.assertEqual(data['format'], 25)
        self.assertEqual(data['human_capacity'], 12)

    @unittest.skipUnless(os.environ.get('WAR3_TEST_MAP'), 'optional supplied real map')
    def test_supplied_footman_example_compiles_with_twelve_humans(self):
        supplied = Path(os.environ['WAR3_TEST_MAP'])
        filename = 'Footman-Frenzy-Ultimate-AI-v1.1.w3x'
        shutil.copyfile(supplied, self.root / 'maps' / filename)
        self.doc = json.loads((ROOT / 'config/presets.example.json').read_text())
        self.save()
        self.cfg['gameplay']['map_cycle'] = ['footman-ultimate-ai']
        result = prepare_native(self.cfg, 'SyntheticUser').read_text()
        for line in ('autohost_gamename = Footman Ultimate AI', 'bot_maxplayers = 12',
                     'bot_autostart_enabled = 1',
                     'autohost_startplayers = 4', 'bot_vote_min_players = 2',
                     'bot_lobby_ai_target = 4', 'bot_lobby_ai_supported = 1'):
            self.assertIn(line + '\n', result)
        self.assertNotIn('#', result)
        generated = (self.root / 'state/native/mapcfg/preset.cfg').read_text()
        self.assertIn('map_localpath = ' + filename + '\n', generated)
        self.assertIn('map_slot12 = 0 255 0 0 3 11 4 1 100\n', generated)


if __name__ == '__main__':
    unittest.main()
