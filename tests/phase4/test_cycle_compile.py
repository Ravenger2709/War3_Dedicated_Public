import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.cycle_compile import CompiledCycleEntry, compile_cycle
from src.runtime.presets import AI_OFF


class CompileCycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'config' / 'maps').mkdir(parents=True)
        (self.root / 'maps').mkdir()
        self.source = self.root / 'config' / 'ghost.cfg'
        self.source.write_text('bot_defaultmap = selected.cfg\n')
        (self.root / 'config' / 'maps' / 'selected.cfg').write_text(
            'map_localpath = selected.w3x\n')
        for name in ('selected.w3x', 'one.w3x', 'two.w3x'):
            (self.root / 'maps' / name).write_bytes(b'fixture')
        self.path = self.root / 'config' / 'presets.json'
        self.doc = {'version': 1, 'presets': [
            {'id': 'one', 'enabled': True, 'map_filename': 'one.w3x',
             'ai': {'enabled': True, 'target': 4, 'skill': 2},
             'vote': {'min_players': 1, 'required_percent': 75}},
            {'id': 'two', 'enabled': True, 'map_filename': 'two.w3x',
             'vote': {'enabled': False, 'min_players': 3}}],
            'capabilities': [{'id': 'approved', 'level': 'approved_preset',
                'map_sha256': 'a' * 64, 'script_sha256': 'b' * 64,
                'features': ['computer_control'], 'review': 'fixture',
                'runtime_tested': False}]}
        self.save()
        self.analysis = {'map_sha256': 'a' * 64, 'script_sha256': 'b' * 64,
                         'ai_evidence': {'features': ['computer_control']},
                         'slots': [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(4)],
                         'human_capacity': 4, 'lobby_name': 'Test'}
        self.game = {'max_players': 4, 'autostart_players': 3,
                     'vote_min_players': 2, 'map_cycle': ['one']}

    def save(self):
        self.path.write_text(json.dumps(self.doc))

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def test_individual_results_repeats_and_no_mutation(self):
        before, game_before = self.snapshot(), copy.deepcopy(self.game)
        with patch('src.runtime.presets.analyze_map', return_value=self.analysis) as analyze, \
             patch('src.runtime.manual_map.analyze_map', return_value=self.analysis) as manual:
            result = compile_cycle(['one', 'two', 'manual', 'one'], self.source, self.game)
        self.assertEqual([x.preset_id for x in result], ['one', 'two', 'manual', 'one'])
        self.assertTrue(all(isinstance(x, CompiledCycleEntry) for x in result))
        self.assertEqual(analyze.call_count, 3)
        self.assertEqual(manual.call_count, 1)
        self.assertEqual(result[0].ai['bot_lobby_ai_target'], '4')
        self.assertEqual(result[0].ai['bot_lobby_ai_skill'], '2')
        self.assertEqual(result[0].gameplay['vote_min_players'], 1)
        self.assertEqual(result[0].gameplay['vote_required_percent'], 75)
        self.assertEqual(dict(result[1].ai), AI_OFF)
        self.assertEqual(result[1].gameplay['vote_min_players'], 3)
        self.assertFalse(result[1].gameplay['vote_enabled'])
        self.assertEqual(result[1].gameplay['vote_required_percent'], 60)
        self.assertEqual(dict(result[2].ai), AI_OFF)
        self.assertEqual(result[2].gameplay['vote_min_players'], 2)
        self.assertTrue(result[2].gameplay['vote_enabled'])
        self.assertIsNone(result[2].mapcfg)
        self.assertIn('map_localpath = one.w3x', result[0].mapcfg)
        self.assertIn('map_localpath = two.w3x', result[1].mapcfg)
        self.assertEqual(result[0], result[3])
        self.assertIsNot(result[0].gameplay, result[3].gameplay)
        self.assertIsNot(result[0].ai, result[3].ai)
        with self.assertRaises(TypeError):
            result[0].gameplay['vote_min_players'] = 4
        self.assertEqual(self.game, game_before)
        self.assertEqual(self.snapshot(), before)

    def test_boundaries_and_every_occurrence(self):
        with patch('src.runtime.presets.analyze_map', return_value=self.analysis) as analyze:
            for n in (1, 32):
                self.assertEqual(len(compile_cycle(['two'] * n, self.source, self.game)), n)
            self.assertEqual(analyze.call_count, 33)
        for cycle in ([], ['two'] * 33, ['two', 'missing'], ['two', '../bad']):
            with self.subTest(cycle=cycle), self.assertRaises(ValueError):
                compile_cycle(cycle, self.source, self.game)

    def test_any_failure_aborts_without_writes(self):
        before = self.snapshot()
        with patch('src.runtime.presets.analyze_map', side_effect=[self.analysis, ValueError('bad map')]):
            with self.assertRaisesRegex(ValueError, 'bad map'):
                compile_cycle(['two', 'one'], self.source, self.game)
        with patch('src.runtime.presets.analyze_map', return_value=self.analysis), \
             patch('src.runtime.manual_map.analyze_map', side_effect=ValueError('bad manual')):
            with self.assertRaisesRegex(ValueError, 'bad manual'):
                compile_cycle(['two', 'manual'], self.source, self.game)
        self.assertEqual(self.snapshot(), before)
        self.doc['capabilities'] = []
        self.save()
        before = self.snapshot()
        with patch('src.runtime.presets.analyze_map', return_value=self.analysis):
            with self.assertRaisesRegex(ValueError, 'AI_EVIDENCE'):
                compile_cycle(['two', 'one'], self.source, self.game)
        self.assertEqual(self.snapshot(), before)

    def test_manual_strict_and_invalid_preset_file(self):
        (self.root / 'config' / 'maps' / 'selected.cfg').write_text('map_localpath = ../bad.w3x\n')
        with self.assertRaises(ValueError):
            compile_cycle(['manual'], self.source, self.game)
        self.path.write_text('{invalid')
        with self.assertRaisesRegex(ValueError, 'PRESET_FILE_REJECTED'):
            compile_cycle(['manual'], self.source, self.game)

    def test_process_capacity_is_validated_and_not_inherited_as_map_setting(self):
        for limit in (1, 3, 5):
            with patch('src.runtime.presets.analyze_map', return_value=self.analysis):
                entry = compile_cycle(['two'], self.source,
                                      {**self.game, 'max_concurrent_matches': limit})[0]
            self.assertNotIn('max_concurrent_matches', entry.gameplay)
        for limit in (0, 6, True, '3'):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'MATCH_LIMIT'):
                compile_cycle(['two'], self.source,
                              {**self.game, 'max_concurrent_matches': limit})


if __name__ == '__main__':
    unittest.main()
