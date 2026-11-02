import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.map_cycle import migrate_cycle, parse_cycle, validate_cycle, validate_resolved_cycle
from src.runtime.presets import load_presets


class MapCycleTests(unittest.TestCase):
    def setUp(self):
        self.presets = {'one': {'id': 'one', 'enabled': True},
                        'two': {'id': 'two', 'enabled': True},
                        'off': {'id': 'off', 'enabled': False}}

    def test_order_and_duplicates_are_preserved_and_copied(self):
        cycle = ['one', 'two', 'one', 'manual']
        validated = validate_cycle(cycle, self.presets)
        self.assertEqual(validated, cycle)
        validated.append('two')
        self.assertEqual(cycle, ['one', 'two', 'one', 'manual'])

    def test_count_boundaries_count_occurrences(self):
        for n in (1, 32):
            self.assertEqual(len(validate_cycle(['one'] * n, self.presets)), n)
        for n in (0, 33):
            with self.subTest(n=n), self.assertRaisesRegex(ValueError, 'COUNT'):
                validate_cycle(['one'] * n, self.presets)

    def test_migration_defaults_explicit_selection_and_idempotence(self):
        self.assertEqual(migrate_cycle({}, self.presets), ['manual'])
        self.assertEqual(migrate_cycle({'map_preset': 'one'}, self.presets), ['one'])
        gameplay = {'map_preset': 'one', 'map_cycle': ['two', 'two']}
        before = copy.deepcopy(gameplay)
        cycle = migrate_cycle(gameplay, self.presets)
        self.assertEqual(cycle, ['two', 'two'])
        self.assertEqual(migrate_cycle({'map_cycle': cycle}, self.presets), cycle)
        self.assertEqual(gameplay, before)

    def test_bad_or_disabled_references_never_fall_back(self):
        for key in ('missing', 'off', '', '../bad', 'UPPER', 1, None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                migrate_cycle({'map_preset': key}, self.presets)
        for entry in (None, [], ['missing'], {'version': 1, 'entries': ['one']}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                migrate_cycle({'map_preset': 'one', 'map_cycle': entry}, self.presets)
        for enabled in (False, 1, None):
            with self.subTest(enabled=enabled), self.assertRaisesRegex(ValueError, 'UNAVAILABLE'):
                validate_cycle(['manual'], {'manual': {'enabled': enabled}})
        self.assertEqual(validate_cycle(['manual'], {}), ['manual'])
        self.assertEqual(validate_cycle(['manual'], {'manual': {'enabled': True}}), ['manual'])

    def test_strict_json_array(self):
        for data in ('NaN', b'\xff', '{', ' ' * 65537):
            with self.subTest(data=repr(data)[:20]), self.assertRaises(ValueError):
                parse_cycle(data)
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_KEY'):
            parse_cycle('{"a":1,"a":2}')
        self.assertEqual(validate_cycle(parse_cycle('["one", "one"]'), self.presets), ['one', 'one'])
        for cycle in ({}, ('manual',), 'manual', [True]):
            with self.subTest(cycle=cycle), self.assertRaises(ValueError):
                validate_cycle(cycle, self.presets)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'config').mkdir()
        (root / 'maps').mkdir()
        self.source = root / 'config' / 'supervisor.json'
        self.source.write_text('{}')
        self.path = root / 'config' / 'presets.json'
        self.doc = {'version': 1, 'presets': [
            {'id': 'manual', 'enabled': True},
            {'id': 'one', 'enabled': True, 'map_filename': 'test.w3x',
             'ai': {'enabled': True, 'target': 4}}], 'capabilities': []}
        self.analysis = {'map_sha256': 'a' * 64, 'script_sha256': 'b' * 64,
                         'ai_evidence': {'features': ['computer_control']},
                         'slots': [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(12)],
                         'human_capacity': 12, 'lobby_name': 'Test'}

    def save(self):
        self.path.write_text(json.dumps(self.doc))

    def test_manual_missing_file_and_explicit_disabled_policy(self):
        self.assertEqual(validate_resolved_cycle(['manual'], self.source, {}), ['manual'])
        self.doc['presets'][0]['enabled'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'PRESET_MANUAL_REQUIRED'):
            load_presets(self.path)
        for cycle in (['manual'], ['one']):
            with self.subTest(cycle=cycle), self.assertRaisesRegex(ValueError, 'PRESET_MANUAL_REQUIRED'):
                validate_resolved_cycle(cycle, self.source, {})
        self.doc['presets'][0]['enabled'] = True
        self.save()
        self.assertEqual(validate_resolved_cycle(['manual'], self.source, {}), ['manual'])

    def test_real_resolver_checks_every_occurrence_and_ai(self):
        self.doc['capabilities'] = [{'id': 'approved', 'level': 'approved_preset',
            'map_sha256': 'a' * 64, 'script_sha256': 'b' * 64,
            'features': ['computer_control'], 'review': 'fixture', 'runtime_tested': False}]
        self.save()
        gameplay = {'map_preset': 'manual', 'map_cycle': ['one', 'one'], 'max_players': 12}
        before = copy.deepcopy(gameplay)
        with patch('src.runtime.presets.analyze_map', return_value=self.analysis) as analyze:
            self.assertEqual(validate_resolved_cycle(['one', 'manual', 'one'], self.source, gameplay), ['one', 'manual', 'one'])
            self.assertEqual(analyze.call_count, 2)
        self.assertEqual(gameplay, before)
        with patch('src.runtime.presets.analyze_map', return_value={**self.analysis, 'map_sha256': 'c' * 64}):
            with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
                validate_resolved_cycle(['one'], self.source, {})
        with patch('src.runtime.presets.analyze_map', side_effect=ValueError('MAP_SIZE_REJECTED')):
            with self.assertRaisesRegex(ValueError, 'MAP_SIZE_REJECTED'):
                validate_resolved_cycle(['one'], self.source, {})

    def test_missing_map_and_unavailable_preset(self):
        self.save()
        with self.assertRaises((ValueError, OSError)):
            validate_resolved_cycle(['one'], self.source, {})
        self.doc['presets'][1]['enabled'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'UNAVAILABLE'):
            validate_resolved_cycle(['one'], self.source, {})


if __name__ == '__main__':
    unittest.main()
