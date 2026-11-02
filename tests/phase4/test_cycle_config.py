"""Configuration parity and fail-closed migration without any native launch."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from amp import production
from src.runtime.config import gameplay_config
from src.runtime.map_cycle import validate_cycle
from src.supervisor.config import load_config


class CycleConfigurationTests(unittest.TestCase):
    def test_typed_legacy_migration_and_copy(self):
        self.assertEqual(gameplay_config({})['map_cycle'], ['manual'])
        self.assertEqual(gameplay_config({'map_preset': 'old'})['map_cycle'], ['old'])
        value = ['one', 'two', 'one']
        game = gameplay_config({'map_preset': 'stale', 'map_cycle': value, 'max_concurrent_matches': 5})
        value.append('manual')
        self.assertEqual(game['map_cycle'], ['one', 'two', 'one'])
        self.assertEqual(game['map_preset'], 'one')
        self.assertEqual(game['max_concurrent_matches'], 5)

    def test_invalid_cycles_and_limits(self):
        for cycle in ([], ['X'], ['a.b'], [''], ['x'] * 33, 'manual', [None], [True]):
            with self.subTest(cycle=cycle), self.assertRaises(ValueError): gameplay_config({'map_cycle': cycle})
        for limit in (0, 6, True, '2', 2.0):
            with self.subTest(limit=limit), self.assertRaises(ValueError): gameplay_config({'max_concurrent_matches': limit})

    def test_all_ids_disabled_missing_and_ai_are_checked(self):
        presets = {'one': {'enabled': True}, 'off': {'enabled': False},
                   'ai': {'enabled': True, 'ai': {'enabled': True}}}
        self.assertEqual(validate_cycle(['one', 'ai', 'one'], presets), ['one', 'ai', 'one'])
        for cycle in (['one', 'off'], ['one', 'missing'], ['one', 'ai', 'bad id']):
            with self.subTest(cycle=cycle), self.assertRaises(ValueError): validate_cycle(cycle, presets)
        with self.assertRaises(ValueError): validate_cycle(['manual'], {'manual': {'enabled': False}})

    def test_amp_runtime_parity_and_legacy_stale_alias(self):
        for extra in ({'map_preset': 'old'}, {'map_cycle': '["a", "b", "a"]',
                       'map_preset': 'stale', 'max_concurrent_matches': '5'},
                      {'countdown_seconds': 0, 'vote_cooldown': 0, 'vote_timeout': 600}):
            values = production.settings({**production.DEFAULTS, **extra})
            game = gameplay_config(values)
            self.assertEqual(game, values)

    def test_direct_iccup_multi_cycle_parses_for_startup_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'mode': 'iccup', 'hostbot': {'command': ['ghost']},
                'gameplay': {'map_cycle': ['a', 'b']}}))
            self.assertEqual(load_config(path)['gameplay']['map_cycle'], ['a','b'])

    def test_migration_preserves_presets_and_refuses_missing_later_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('control', 'data/config'):
                (root / name).mkdir(parents=True, mode=0o700)
            presets = root / 'data/config/presets.json'
            document = {'version': 1, 'presets': [{'id': 'manual', 'enabled': True},
                {'id': 'other', 'enabled': True, 'map_filename': 'other.w3x'},
                {'id': 'disabled', 'enabled': False, 'map_filename': 'gone.w3x'}]}
            production.private_json(presets, document)
            settings = root / 'data/config/amp-settings.json'
            production.private_json(settings, {**production.DEFAULTS, 'map_preset': 'other'})
            before = presets.read_bytes()
            self.assertTrue(production.migrate_cycle_settings(root))
            result = production.read_json(settings)
            self.assertEqual(result['map_cycle'], ['other'])
            self.assertNotIn('map_preset', result)
            self.assertEqual(result['max_concurrent_matches'], 3)
            self.assertEqual(presets.read_bytes(), before)
            self.assertFalse(production.migrate_cycle_settings(root))
            production.private_json(settings, {**result, 'map_cycle': ['other', 'missing']})
            with self.assertRaisesRegex(production.SettingsError, 'PRESET_SELECTION_REQUIRED'):
                production.migrate_cycle_settings(root)
            self.assertEqual(presets.read_bytes(), before)

if __name__ == '__main__': unittest.main()
