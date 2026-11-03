"""Preload preparation validates immutable map bytes before producing settings."""
import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path
import unittest
from unittest.mock import patch

from src.runtime import preload_catalog as subject
from src.runtime.mpq import extract_sealed_map

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('preload_map_fixture', ROOT / 'tests/phase3/test_map_presets.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


@unittest.skipUnless(Path(fixture.STORM).is_file(), 'offline StormLib fixture required')
class PreparedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.builder = fixture.MPQTests('runTest'); self.builder.setUp()
        self.addCleanup(self.builder.doCleanups)
        self.root = self.builder.root
        self.a = self.builder.mpq(members={'war3map.w3i': fixture.info_fixture(statuses=[1]*4, title=b'First'),
                                           'war3map.j': fixture.PLAIN_SCRIPT}, name='a.w3x')
        self.b = self.builder.mpq(members={'war3map.w3i': fixture.info_fixture(statuses=[1]*4, title=b'Second'),
                                           'war3map.j': fixture.PLAIN_SCRIPT,
                                           'war3map.w3u': b'different'}, name='b.w3x')
        self.doc = {'version': 1, 'presets': [
            {'id': 'one', 'enabled': True, 'map_filename': 'a.w3x',
             'lobby_name': 'Named A', 'max_players': 4, 'start': {'autostart_players': 4}},
            {'id': 'two', 'enabled': True, 'map_filename': 'b.w3x',
             'lobby_name': 'Named B', 'max_players': 3,
             'start': {'autostart_players': 3}, 'vote': {'enabled': False}},
            {'id': 'manual', 'enabled': True}], 'capabilities': []}
        self.preset_path = self.root / 'config/presets.json'
        self.preset_path.write_text(json.dumps(self.doc))
        self.game = {'max_players': 4, 'autostart_players': 4, 'vote_min_players': 2,
                     'max_concurrent_matches': 2}

    def prepare(self, cycle=('one', 'two', 'one')):
        return subject.prepare_preload_cycle(list(cycle), self.builder.source, self.game,
                                              default_map_bytes=self.a.stat().st_size,
                                              library=fixture.STORM)

    def test_two_sealed_maps_unique_ids_and_settings_are_bound(self):
        with self.prepare() as result:
            self.assertEqual(result.cycle_indices, (0, 1, 0))
            self.assertEqual(result.max_games, 2)
            self.assertEqual([entry.preset_id for entry in result.entries], ['one', 'two'])
            self.assertEqual([entry.gameplay['game_name'] for entry in result.entries], ['Named A', 'Named B'])
            self.assertEqual([entry.gameplay['max_players'] for entry in result.entries], [4, 3])
            self.assertFalse(result.entries[1].gameplay['vote_enabled'])
            self.assertEqual(result.entries[0].map_sha256, hashlib.sha256(self.a.read_bytes()).hexdigest())
            self.assertEqual(result.entries[1].map_sha256, hashlib.sha256(self.b.read_bytes()).hexdigest())
            self.assertEqual(result.preset_sha256, hashlib.sha256(self.preset_path.read_bytes()).hexdigest())
            with self.assertRaises(TypeError): result.entries[0].gameplay['game_name'] = 'changed'
            self.assertEqual(len(result.pinned.pass_fds()), 2)
        with self.assertRaisesRegex(ValueError, 'CLOSED'): result.pinned.pass_fds()

    def test_source_replaced_after_pinning_does_not_change_validation(self):
        expected = hashlib.sha256(self.a.read_bytes()).hexdigest()
        original = subject._analyze_pinned
        def changed_after_pin(entry, library):
            if entry.preset_id == 'one': self.a.write_bytes(b'not a map any more')
            return original(entry, library)
        with patch.object(subject, '_analyze_pinned', side_effect=changed_after_pin):
            with self.prepare(('one',)) as result:
                self.assertEqual(result.entries[0].map_sha256, expected)
                self.assertEqual(result.entries[0].gameplay['game_name'], 'Named A')
                self.assertNotEqual(hashlib.sha256(self.a.read_bytes()).hexdigest(), expected)

    def test_settings_are_taken_from_one_preset_document_snapshot(self):
        expected = hashlib.sha256(self.preset_path.read_bytes()).hexdigest()
        original = subject._hash_source
        def replace_settings(path):
            changed = json.loads(self.preset_path.read_text())
            changed['presets'][0]['lobby_name'] = 'Changed later'
            self.preset_path.write_text(json.dumps(changed))
            return original(path)
        with patch.object(subject, '_hash_source', side_effect=replace_settings):
            with self.prepare(('one',)) as result:
                self.assertEqual(result.entries[0].gameplay['game_name'], 'Named A')
                self.assertEqual(result.preset_sha256, expected)
                self.assertNotEqual(result.preset_sha256, hashlib.sha256(self.preset_path.read_bytes()).hexdigest())

    def test_changed_map_between_hash_and_pin_is_rejected(self):
        original = subject.pin_preload_inputs
        def change_before_pin(*args, **kwargs):
            self.a.write_bytes(b'changed before pinning')
            return original(*args, **kwargs)
        with patch.object(subject, 'pin_preload_inputs', side_effect=change_before_pin):
            with self.assertRaisesRegex(ValueError, 'DIGEST_MISMATCH'):
                self.prepare(('one',))

    def test_invalid_id_is_rejected_before_opening_map_inputs(self):
        with patch.object(subject, '_hash_source', side_effect=AssertionError('unexpected read')):
            with self.assertRaisesRegex(ValueError, 'UNAVAILABLE'):
                self.prepare(('unknown',))

    def test_optional_two_local_maps_validate_from_sealed_bytes(self):
        paths = [Path(p) for p in os.environ.get('WAR3_KNOWN_MAPS', '').split(':') if p]
        if len(paths) < 2:
            self.skipTest('two local maps not supplied')
        shutil.copyfile(paths[0], self.a); shutil.copyfile(paths[1], self.b)
        # Afterlife has four predefined computers; retain room for four humans.
        self.doc['presets'][0]['max_players'] = 8
        self.preset_path.write_text(json.dumps(self.doc))
        with self.prepare() as result:
            self.assertEqual(result.cycle_indices, (0, 1, 0))
            self.assertEqual([entry.map_sha256 for entry in result.entries],
                             [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths[:2]])

    def test_prevalidation_does_not_accept_unsealed_input(self):
        fd = os.open(self.a, os.O_RDONLY)
        try:
            with self.assertRaisesRegex(ValueError, 'MAP_EXTRACTION_REJECTED'):
                extract_sealed_map(fd, library=fixture.STORM)
        finally:
            os.close(fd)

    def test_invalid_manifest_manual_and_match_count_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'MATCH_LIMIT'):
            subject.prepare_preload_cycle(['one'], self.builder.source,
                                           {**self.game, 'max_concurrent_matches': True},
                                           default_map_bytes=100, library=fixture.STORM)
        self.preset_path.write_text('{"version":1,"version":1,"presets":[]}')
        with self.assertRaises(ValueError): self.prepare(('one',))

    def setup_manual(self, loading_key='map_loadInGame'):
        self.builder.operator.chmod(0o644)
        self.builder.operator.write_text(
            'map_path = Maps\\Custom\\a.w3x\nmap_localpath = a.w3x\n'
            'map_speed = 2\nmap_visibility = 1\nmap_observers = 3\n'
            'map_flags = 7\n' + loading_key + ' = 1\n')

    def test_manual_migration_without_presets_keeps_settings_and_pins_once(self):
        self.setup_manual()
        self.preset_path.unlink()
        with self.prepare(('manual', 'manual')) as result:
            self.assertIsNone(result.preset_sha256)
            self.assertEqual(result.cycle_indices, (0, 0))
            self.assertEqual(len(result.entries), 1)
            entry = result.entries[0]
            self.assertEqual(entry.preset_id, 'manual')
            self.assertEqual(entry.map_sha256, hashlib.sha256(self.a.read_bytes()).hexdigest())
            for line in ('map_path = Maps\\Custom\\a.w3x', 'map_speed = 2',
                         'map_visibility = 1', 'map_observers = 3', 'map_flags = 7',
                         'map_loadingame = 1'):
                self.assertIn(line + '\n', entry.mapcfg)
            self.assertNotIn('map_loadInGame', entry.mapcfg)
            self.assertEqual(entry.ai['bot_lobby_ai_supported'], '0')
            from src.runtime.preload_manifest import encode_startup_manifest, parse_startup_manifest
            decoded = parse_startup_manifest(encode_startup_manifest(result))
            self.assertEqual(decoded['cycle_indices'], (0, 0))
            self.assertEqual(decoded['entries'][0]['mapcfg'], entry.mapcfg)

    def test_manual_snapshot_and_canonical_loadingame(self):
        self.setup_manual('map_loadingame')
        original = subject._hash_source
        def alter_after_config_snapshot(path):
            self.builder.operator.write_text('map_localpath = b.w3x\nmap_path = Maps\\Other\\b.w3x\n')
            return original(path)
        with patch.object(subject, '_hash_source', side_effect=alter_after_config_snapshot):
            with self.prepare(('manual',)) as result:
                self.assertIn('map_loadingame = 1\n', result.entries[0].mapcfg)
                self.assertEqual(result.entries[0].map_sha256, hashlib.sha256(self.a.read_bytes()).hexdigest())

    def test_manual_bad_alias_path_and_unsupported_settings_fail_closed(self):
        self.setup_manual()
        original = self.builder.operator.read_text()
        for suffix in ('map_loadingame = 0\n', 'map_slot1 = 0 1 2 3 4 5 6 7 8\n',
                       'map_loadingame = broken\n'):
            self.builder.operator.write_text(original + suffix)
            with self.subTest(suffix=suffix), self.assertRaises(ValueError): self.prepare(('manual',))
        for path in ('../a.w3x', 'Maps\\..\\a.w3x', '', 'x' * 60 + '.w3x'):
            self.builder.operator.write_text('map_localpath = a.w3x\nmap_path = ' + path + '\n')
            with self.subTest(path=path), self.assertRaises(ValueError): self.prepare(('manual',))

    def test_later_disabled_missing_or_unproven_ai_blocks_whole_cycle(self):
        original = json.loads(json.dumps(self.doc))
        for scenario in ('disabled', 'missing', 'ai'):
            self.doc = json.loads(json.dumps(original))
            if scenario == 'disabled':
                self.doc['presets'][1]['enabled'] = False
            elif scenario == 'ai':
                self.doc['presets'][1]['ai'] = {'enabled': True, 'target': 3}
            self.preset_path.write_text(json.dumps(self.doc))
            cycle = ('one', 'absent' if scenario == 'missing' else 'two')
            before = set(os.listdir('/proc/self/fd'))
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                self.prepare(cycle)
            self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_failed_validation_closes_all_sealed_descriptors(self):
        self.doc['presets'][0]['ai'] = {'enabled': True, 'target': 4}
        self.preset_path.write_text(json.dumps(self.doc))
        before = set(os.listdir('/proc/self/fd'))
        with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'): self.prepare(('one',))
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

if __name__ == '__main__': unittest.main()
