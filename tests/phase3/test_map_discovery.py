"""Offline tests for safe discovery and File Manager conflicts."""
import hashlib
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from amp import bootstrap, map_discovery, production

IMAGE = 'sha256:' + '1' * 64
SPEC = {'image_id': IMAGE, 'uid': os.geteuid(), 'gid': os.getegid()}
OWNER = f'{os.geteuid()}:{os.getegid()}'


def analysis(path):
    return {'map_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'script_sha256': 'a' * 64,
            'lobby_name': 'Sample map', 'slots': [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(4)],
            'human_capacity': 4, 'ai_evidence': {'level': 'none', 'features': []}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('control', 'data', 'data/config', 'data/maps'):
            (self.root / name).mkdir(mode=0o700)
        production.initialize_preset_editor(self.root)
        self.map = self.root / 'data/maps/Sample.w3x'
        self.map.write_bytes(b'map data' * 10)
        self.doc = {'version': 1, 'presets': [{'id': 'manual', 'enabled': True}]}

    def analyze(self, path, *_):
        return analysis(path)

    def test_generates_once_preserving_existing_and_safe_defaults(self):
        (self.root / 'data/maps/copy.w3x').write_bytes(self.map.read_bytes())
        result, skipped = map_discovery.discover(self.root, self.doc, SPEC, [], {}, OWNER, self.analyze)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(result['presets']), 2)
        entry = result['presets'][1]
        self.assertEqual(entry['max_players'], 4)
        self.assertEqual(entry['lobby_name'], 'Sample map')
        self.assertEqual(entry['ai'], {'enabled': False, 'target': 4, 'skill': 1})
        self.assertFalse(entry['start']['autostart_enabled'])
        self.assertEqual(map_discovery.discover(self.root, result, SPEC, [], {}, OWNER, self.analyze)[0], result)
        self.assertEqual(self.doc['presets'], [{'id': 'manual', 'enabled': True}])

    def test_bad_and_changing_maps_are_skipped(self):
        (self.root / 'data/maps/invalid.w3x').write_bytes(b'bad')
        def changing(path, *_):
            value = analysis(path)
            path.write_bytes(b'changed' * 12)
            return value
        result, skipped = map_discovery.discover(self.root, self.doc, SPEC, [], {}, OWNER, changing)
        self.assertEqual(result, self.doc)
        self.assertEqual(skipped, 2)

    def test_bootstrap_initial_unapproved_map_has_complete_disabled_ai(self):
        info = {**analysis(self.map), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        doc = bootstrap.initial_preset(info, info['map_sha256'])
        self.assertEqual(len(doc['capabilities']), 1)
        self.assertFalse(doc['capabilities'][0]['runtime_tested'])
        self.assertEqual(doc['presets'][0]['ai'], {'enabled': False, 'target': 4, 'skill': 1})

    def test_bootstrap_curated_record_and_gameplay_are_preserved(self):
        curated = Path(__file__).resolve().parents[2] / 'config/presets.example.json'
        source = json.loads(curated.read_text())
        preset = next(x for x in source['presets'] if x['id'] == 'footman-ultimate-ai')
        record = next(x for x in source['capabilities'] if x['id'] == preset['ai']['capability'])
        info = {**analysis(self.map), 'map_sha256': record['map_sha256'],
                'script_sha256': record['script_sha256'],
                'ai_evidence': {'level': 'candidate', 'features': record['features']},
                'slots': [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(12)], 'human_capacity': 12}
        doc = bootstrap.initial_preset(info, info['map_sha256'], curated)
        self.assertEqual(doc['capabilities'], [record])
        self.assertEqual(doc['presets'][0], {**preset, 'map_filename': 'selected.w3x'})

    def test_discovery_creates_static_hash_bound_evidence_without_enabling_ai(self):
        def candidate(path, *_):
            return {**analysis(path), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        result, skipped = map_discovery.discover(self.root, self.doc, SPEC, [], {}, OWNER, candidate)
        self.assertEqual(skipped, 0)
        record = result['capabilities'][0]
        self.assertEqual((record['map_sha256'], record['script_sha256']), (analysis(self.map)['map_sha256'], 'a' * 64))
        self.assertEqual(record['review'], 'automatic static evidence; no runtime validation')
        self.assertIs(record['runtime_tested'], False)
        self.assertFalse(result['presets'][1]['ai']['enabled'])
        self.assertEqual(map_discovery.discover(self.root, result, SPEC, [], {}, OWNER, candidate)[0], result)
        self.map.write_bytes(b'new map bytes' * 10)
        changed = map_discovery.discover(self.root, result, SPEC, [], {}, OWNER, candidate)[0]
        self.assertEqual(changed['presets'], result['presets'])
        self.assertEqual(len(changed['capabilities']), 2)
        self.assertEqual(changed['capabilities'][0], record)
        self.assertNotEqual(hashlib.sha256(self.map.read_bytes()).hexdigest(), record['map_sha256'])

    def test_existing_approval_is_reused_and_invalid_evidence_skipped(self):
        def candidate(path, *_):
            return {**analysis(path), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        existing = map_discovery.automatic_approval(candidate(self.map), [])
        original = {**self.doc, 'capabilities': [existing]}
        result, skipped = map_discovery.discover(self.root, original, SPEC, [], {}, OWNER, candidate)
        self.assertEqual(skipped, 0)
        self.assertEqual(result['capabilities'], [existing])
        def malformed(path, *_):
            return {**analysis(path), 'ai_evidence': {'level': 'candidate', 'features': ['unknown']}}
        self.assertEqual(map_discovery.discover(self.root, self.doc, SPEC, [], {}, OWNER, malformed), (self.doc, 1))

    def test_record_limit_keeps_new_preset_ai_off(self):
        def candidate(path, *_):
            return {**analysis(path), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        existing = [{'id': str(i), 'map_sha256': 'f' * 64, 'script_sha256': 'e' * 64} for i in range(32)]
        original = {**self.doc, 'capabilities': existing}
        result, skipped = map_discovery.discover(self.root, original, SPEC, [], {}, OWNER, candidate)
        self.assertEqual(skipped, 0)
        self.assertEqual(result['capabilities'], existing)
        self.assertFalse(result['presets'][1]['ai']['enabled'])
        non_ai, skipped = map_discovery.discover(self.root, original, SPEC, [], {}, OWNER, self.analyze)
        self.assertEqual(skipped, 0)
        self.assertFalse(non_ai['presets'][1]['ai']['enabled'])
        self.assertEqual(non_ai['capabilities'], existing)

    def test_matching_map_hash_skips_analyzer_for_existing_preset(self):
        entry = {'id': 'mine', 'enabled': True, 'map_filename': self.map.name}
        info = {**analysis(self.map), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        proof = map_discovery.automatic_approval(info, [])
        doc = {'version': 1, 'presets': [entry], 'capabilities': [proof]}
        result, skipped = map_discovery.discover(self.root, doc, SPEC, [], {}, OWNER,
                                                lambda *_: self.fail('matching map should not spawn analyzer'))
        self.assertEqual(skipped, 0)
        self.assertEqual(result, doc)

    def test_ai_starter_alone_never_creates_automatic_approval(self):
        data = {**analysis(self.map), 'ai_evidence': {'level': 'candidate', 'features': ['ai_start']}}
        self.assertIsNone(map_discovery.automatic_approval(data, []))
        doc = bootstrap.initial_preset(data, data['map_sha256'])
        self.assertEqual(doc['capabilities'], [])
        self.assertFalse(doc['presets'][0]['ai']['enabled'])

    def test_existing_scan_budget_does_not_starve_new_maps(self):
        self.map.unlink()
        entries = []
        for i in range(31):
            name = f'existing-{i:02d}.w3x'
            (self.root / 'data/maps' / name).write_bytes((f'existing {i:02d} ' * 8).encode())
            entries.append({'id': f'existing-{i:02d}', 'enabled': True, 'map_filename': name})
        (self.root / 'data/maps/a-invalid.w3x').write_bytes(b'bad')
        new_path = self.root / 'data/maps/z-new.w3x'
        new_path.write_bytes(b'new map content' * 8)
        result, skipped = map_discovery.discover(self.root, {'version': 1, 'presets': entries}, SPEC, [], {}, OWNER, self.analyze)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(result['presets']), 32)
        self.assertEqual(result['presets'][-1]['map_filename'], new_path.name)

    def test_32_invalid_sizes_do_not_exhaust_new_map_budget(self):
        self.map.unlink()
        for i in range(32):
            (self.root / 'data/maps' / f'a-invalid-{i:02d}.w3x').write_bytes(b'bad')
        valid = self.root / 'data/maps/z-valid.w3x'
        valid.write_bytes(b'valid synthetic map' * 5)
        result, skipped = map_discovery.discover(self.root, self.doc, SPEC, [], {}, OWNER, self.analyze)
        self.assertEqual(skipped, 32)
        self.assertEqual(result['presets'][-1]['map_filename'], valid.name)

    def test_late_file_manager_saves_survive_addition_and_approval(self):
        path = self.root / production.PRESETS
        for existing in (False, True):
            with self.subTest(existing=existing):
                entry = {'id': 'existing', 'enabled': True, 'map_filename': self.map.name,
                         'ai': {'enabled': False, 'target': 4, 'skill': 1}}
                doc = {'version': 1, 'presets': [{'id': 'manual', 'enabled': True}] + ([entry] if existing else [])}
                production.private_json(path, doc)
                production.private_json(self.root / production.PRESET_BASELINE, {'version': 1, 'editor': None, 'file': doc})
                discover = map_discovery.discover
                def candidate(map_path, *_):
                    return {**analysis(map_path), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
                with path.open('r+b') as writer, patch.object(map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, candidate, **kw)):
                    with production.preset_update(self.root, SPEC):
                        pass
                    writer.seek(0)
                    writer.truncate()
                    writer.write(b'{"late_edit": true}')
                    writer.flush()
                retained = list((self.root / 'control/preset-deletion-history').glob('*/displaced.json'))
                self.assertTrue(any(item.read_bytes() == b'{"late_edit": true}' for item in retained))
                result = production.read_json(path)
                self.assertEqual(len(result['capabilities']), 1)
                self.assertEqual(len(result['presets']), 2)
                # Operator reviewed and removed this synthetic history before the next case.
                shutil.rmtree(self.root / 'control/preset-deletion-history')

    def test_late_file_manager_save_blocks_next_start_and_identifies_recovery(self):
        path = self.root / production.PRESETS
        production.private_json(path, self.doc)
        discover = map_discovery.discover
        with path.open('r+b') as writer, patch.object(map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, self.analyze, **kw)):
            with production.preset_update(self.root, SPEC):
                pass
            # The unmodified retained file is accepted at the next start.
            with production.preset_update(self.root, SPEC):
                pass
            writer.seek(0)
            writer.truncate()
            writer.write(b'{"later_file_manager_save": true}')
            writer.flush()
            before = path.read_bytes()
            with self.assertRaises(production.SettingsError) as raised:
                with production.preset_update(self.root, SPEC):
                    self.fail('must stop before preparing a runtime')
            self.assertEqual(raised.exception.args[0], 'PRESET_HISTORY_CONFLICT')
            recover = self.root / raised.exception.args[1]
            self.assertEqual(recover.read_bytes(), b'{"later_file_manager_save": true}')
            self.assertIn(raised.exception.args[1], raised.exception.public_message)
            self.assertEqual(path.read_bytes(), before)

    def test_late_metadata_save_and_missing_baseline_fail_closed(self):
        manifest = self.root / production.PRESET_MANIFEST
        production.private_json(self.root / production.PRESETS, self.doc)
        production.private_json(manifest, [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset'}])
        with manifest.open('r+b') as writer:
            with production.preset_manifest_update(self.root):
                pass
            writer.seek(0)
            writer.truncate()
            writer.write(b'late metadata update')
            writer.flush()
        with self.assertRaises(production.SettingsError) as raised:
            with production.preset_update(self.root, SPEC):
                pass
        self.assertIn('preset-metadata-history', raised.exception.public_message)
        history = self.root / 'control/preset-metadata-history'
        baseline = next(history.glob('*/retained-baseline.json'))
        baseline.unlink()
        with self.assertRaisesRegex(production.SettingsError, 'PRESET_HISTORY_CONFLICT'):
            with production.preset_update(self.root, SPEC):
                pass

    def test_late_save_to_rolled_back_version_is_retained(self):
        path = self.root / production.PRESETS
        production.private_json(path, self.doc)
        writer = None
        discover = map_discovery.discover
        try:
            with patch.object(map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, self.analyze, **kw)):
                with self.assertRaisesRegex(ValueError, 'fixture failure'):
                    with production.preset_update(self.root, SPEC):
                        writer = path.open('r+b')
                        raise ValueError('fixture failure')
            writer.seek(0)
            writer.truncate()
            writer.write(b'{"late_rollback_edit": true}')
            writer.flush()
            retained = list((self.root / 'control/preset-deletion-history').glob('*/displaced.json'))
            self.assertTrue(any(item.read_bytes() == b'{"late_rollback_edit": true}' for item in retained))
            self.assertEqual(production.read_json(path), self.doc)
        finally:
            if writer is not None:
                writer.close()

    def test_existing_preset_gets_only_missing_record_and_no_duplicates(self):
        entry = {'id': 'mine', 'enabled': True, 'map_filename': self.map.name,
                 'lobby_name': 'My lobby', 'ai': {'enabled': True, 'target': 3, 'skill': 2},
                 'vote': {'min_players': 2}}
        original = {'version': 1, 'presets': [entry], 'capabilities': []}
        def candidate(path, *_):
            return {**analysis(path), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        result, skipped = map_discovery.discover(self.root, original, SPEC, [], {}, OWNER, candidate)
        self.assertEqual(skipped, 0)
        self.assertEqual(result['presets'], [entry])
        self.assertEqual(original['capabilities'], [])
        self.assertEqual(len(result['capabilities']), 1)
        self.assertEqual(map_discovery.discover(self.root, result, SPEC, [], {}, OWNER, candidate)[0], result)

    def test_missing_or_changed_existing_file_never_gets_record(self):
        doc = {'version': 1, 'presets': [{'id': 'mine', 'enabled': True, 'map_filename': self.map.name}]}
        def changing(path, *_):
            value = {**analysis(path), 'ai_evidence': {'level': 'candidate', 'features': ['ai_start']}}
            path.write_bytes(b'replaced' * 20)
            return value
        self.assertEqual(map_discovery.discover(self.root, doc, SPEC, [], {}, OWNER, changing), (doc, 1))
        self.map.unlink()
        self.assertEqual(map_discovery.discover(self.root, doc, SPEC, [], {}, OWNER, changing),
                         (doc, 0))

    def test_missing_aliases_removed_manual_and_capabilities_preserved_case_conservative(self):
        entries = [{'id': 'manual', 'enabled': True},
                   {'id': 'same', 'enabled': True, 'map_filename': self.map.name},
                   {'id': 'case', 'enabled': True, 'map_filename': self.map.name.upper()},
                   {'id': 'gone1', 'enabled': True, 'map_filename': 'gone.w3x'},
                   {'id': 'gone2', 'enabled': False, 'map_filename': 'gone.w3x'}]
        doc = {'version': 1, 'presets': entries, 'capabilities': [{'id': 'untouched', 'map_sha256': 'f' * 64,
                 'script_sha256': 'a' * 64, 'features': ['ai_start']}]}
        result, _ = map_discovery.discover(self.root, doc, SPEC, [], {}, OWNER, self.analyze)
        self.assertEqual(result['presets'], entries)
        self.assertEqual(result['capabilities'], doc['capabilities'])

    def test_directory_io_error_or_replacement_never_commits_deletions(self):
        path = self.root / production.PRESETS
        doc = {'version': 1, 'presets': [{'id': 'manual', 'enabled': True},
                                         {'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}]}
        production.private_json(path, doc)
        original = path.read_bytes()
        with patch.object(map_discovery.os, 'listdir', side_effect=PermissionError('synthetic')):
            with self.assertRaises(PermissionError):
                with production.preset_update(self.root, SPEC):
                    pass
        self.assertEqual(path.read_bytes(), original)
        discover = map_discovery.discover
        def replace(i, d, spec, docker, env, owner, **kw):
            result = discover(i, d, spec, docker, env, owner, self.analyze, **kw)
            maps = self.root / 'data/maps'
            maps.rename(self.root / 'data/maps-old')
            maps.mkdir(mode=0o700)
            return result
        with patch.object(map_discovery, 'discover', side_effect=replace):
            with self.assertRaisesRegex(ValueError, 'map changed'):
                with production.preset_update(self.root, SPEC):
                    pass
        self.assertEqual(path.read_bytes(), original)

    def test_amp_run_discovers_selects_and_removes_with_active_guard(self):
        from contextlib import nullcontext
        class Executed(Exception):
            pass
        path = self.root / production.PRESETS
        settings_path = self.root / 'data/config/amp-settings.json'
        production.private_json(path, self.doc)
        manifest = self.root / production.PRESET_MANIFEST
        production.private_json(manifest, [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset'}])
        settings = dict(production.DEFAULTS)
        production.private_json(settings_path, settings)
        discover = map_discovery.discover
        def run():
            with self.assertRaises(Executed):
                production.main(['run', '--instance-dir', str(self.root)])
        with patch.object(production, 'deployment', return_value=SPEC), \
             patch.object(production.account, 'profile_update', return_value=nullcontext()), \
             patch.object(production, 'preflight'), patch.object(production, 'merge_settings'), \
             patch.object(production, 'command', return_value=[]), \
             patch.object(production.os, 'execvpe', side_effect=Executed), \
             patch.object(map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, self.analyze, **kw)):
            run()
            doc = production.read_json(path)
            chosen = next(p['id'] for p in doc['presets'] if p['id'] != 'manual')
            settings['map_preset'] = chosen
            production.private_json(settings_path, settings)
            run()
            self.map.unlink()
            settings_before = settings_path.read_bytes()
            with self.assertRaisesRegex(production.SettingsError, 'PRESET_ACTIVE_MAP_MISSING'):
                production.main(['run', '--instance-dir', str(self.root)])
            self.assertEqual([p['id'] for p in production.read_json(path)['presets']], ['manual', chosen])
            self.assertEqual(settings_path.read_bytes(), settings_before)
            options = json.loads(manifest.read_text())[0]['EnumValues']
            self.assertEqual(set(options), {'manual', chosen})
            settings['map_preset'] = 'manual'
            production.private_json(settings_path, settings)
            run()
            self.assertEqual([p['id'] for p in production.read_json(path)['presets']], ['manual', chosen])
            self.map.write_bytes(b'map data' * 10)
            run()
            self.assertEqual(len(production.read_json(path)['presets']), 2)

    def test_amp_last_selected_deletion_creates_manual_selector(self):
        from contextlib import nullcontext
        self.map.unlink()
        path = self.root / production.PRESETS
        manifest = self.root / production.PRESET_MANIFEST
        production.private_json(path, {'version': 1, 'presets': [{'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}]})
        production.private_json(manifest, [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset',
                                            'InputType': 'enum', 'EnumValues': {'gone': 'Gone'}}])
        settings = self.root / 'data/config/amp-settings.json'
        production.private_json(settings, {**production.DEFAULTS, 'map_preset': 'gone'})
        before = settings.read_bytes()
        with patch.object(production, 'deployment', return_value=SPEC), \
             patch.object(production.account, 'profile_update', return_value=nullcontext()), \
             patch.object(production.os, 'execvpe') as execute:
            with self.assertRaisesRegex(production.SettingsError, 'PRESET_ACTIVE_MAP_MISSING'):
                production.main(['run', '--instance-dir', str(self.root)])
        execute.assert_not_called()
        self.assertEqual(production.read_json(path)['presets'], [{'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}])
        self.assertEqual(set(json.loads(manifest.read_text())[0]['EnumValues']), {'gone'})
        self.assertEqual(settings.read_bytes(), before)

    def test_manifest_external_edit_during_rollback_is_preserved(self):
        manifest = self.root / production.PRESET_MANIFEST
        production.private_json(self.root / production.PRESETS, self.doc)
        production.private_json(manifest, [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset'}])
        external = [{'Category': 'external', 'ParamFieldName': 'external'}]
        with self.assertRaisesRegex(ValueError, 'fixture failure'):
            with production.preset_manifest_update(self.root):
                production.private_json(manifest, external)
                raise ValueError('fixture failure')
        self.assertEqual(json.loads(manifest.read_text()), external)
        self.assertTrue((self.root / production.PRESET_RECOVERY).is_dir())

    def test_manifest_edit_immediately_before_exchange_is_retained(self):
        manifest = self.root / production.PRESET_MANIFEST
        production.private_json(self.root / production.PRESETS, self.doc)
        production.private_json(manifest, [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset'}])
        external = [{'Category': 'external', 'ParamFieldName': 'external'}]
        exchange = production.account.exchange
        def race(first, second):
            if second == manifest:
                production.private_json(manifest, external)
            return exchange(first, second)
        with patch.object(production.account, 'exchange', side_effect=race):
            with self.assertRaises(production.SettingsError):
                with production.preset_manifest_update(self.root):
                    pass
        recovery = self.root / production.PRESET_RECOVERY / 'displaced.json'
        self.assertEqual(json.loads(recovery.read_text()), external)

    def test_selected_deletion_rolls_back_if_selector_conflicts(self):
        from contextlib import nullcontext
        self.map.unlink()
        path = self.root / production.PRESETS
        manifest = self.root / production.PRESET_MANIFEST
        doc = {'version': 1, 'presets': [{'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}]}
        production.private_json(path, doc)
        production.private_json(manifest, [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset'}])
        settings = self.root / 'data/config/amp-settings.json'
        production.private_json(settings, {**production.DEFAULTS, 'map_preset': 'gone'})
        before = settings.read_bytes()
        external = [{'Category': 'external', 'ParamFieldName': 'external'}]
        exchange = production.account.exchange
        def race(first, second):
            if second == manifest:
                production.private_json(manifest, external)
            return exchange(first, second)
        with patch.object(production, 'deployment', return_value=SPEC), \
             patch.object(production.account, 'profile_update', return_value=nullcontext()), \
             patch.object(production.account, 'exchange', side_effect=race):
            with self.assertRaisesRegex(production.SettingsError, 'PRESET_EXTERNAL_CONFLICT'):
                production.main(['run', '--instance-dir', str(self.root)])
        self.assertEqual(production.read_json(path), doc)
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(json.loads((self.root / production.PRESET_RECOVERY / 'displaced.json').read_text()), external)
    def test_map_replacement_before_install_or_during_preflight_rolls_back(self):
        path = self.root / production.PRESETS
        doc = {'version': 1, 'presets': [{'id': 'mine', 'enabled': True, 'map_filename': self.map.name}]}
        production.private_json(path, doc)
        original = path.read_bytes()
        discover = map_discovery.discover
        def candidate(map_path, *_):
            return {**analysis(map_path), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        def wrapped(i, d, spec, docker, env, owner, **kw):
            result = discover(i, d, spec, docker, env, owner, candidate, **kw)
            self.map.write_bytes(b'replaced after scan' * 8)
            return result
        with patch.object(production.map_discovery, 'discover', side_effect=wrapped):
            with self.assertRaisesRegex(ValueError, 'map changed'):
                with production.preset_update(self.root, SPEC):
                    pass
        self.assertEqual(path.read_bytes(), original)
        self.map.write_bytes(b'map data' * 10)
        with patch.object(production.map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, candidate, **kw)):
            with self.assertRaisesRegex(ValueError, 'map changed'):
                with production.preset_update(self.root, SPEC):
                    self.map.unlink()
        self.assertEqual(path.read_bytes(), original)

    def test_deleted_map_reappears_and_external_preset_save_is_not_lost(self):
        path = self.root / production.PRESETS
        doc = {'version': 1, 'presets': [{'id': 'manual', 'enabled': True},
                                         {'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}]}
        production.private_json(path, doc)
        original = path.read_bytes()
        discover = map_discovery.discover
        with patch.object(map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, self.analyze, **kw)):
            with self.assertRaisesRegex(ValueError, 'map changed'):
                with production.preset_update(self.root, SPEC):
                    (self.root / 'data/maps/gone.w3x').write_bytes(b'reappeared' * 10)
        self.assertEqual(path.read_bytes(), original)
        (self.root / 'data/maps/gone.w3x').unlink()
        external = {'version': 1, 'presets': [{'id': 'manual', 'enabled': True},
                    {'id': 'external', 'enabled': True, 'map_filename': 'external.w3x'}]}
        with patch.object(map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, self.analyze, **kw)):
            with self.assertRaises(production.SettingsError):
                with production.preset_update(self.root, SPEC):
                    production.private_json(path, external)
        self.assertEqual(production.read_json(path), external)

    def test_selection_change_during_deletion_rolls_back(self):
        path = self.root / production.PRESETS
        settings_path = self.root / 'data/config/amp-settings.json'
        doc = {'version': 1, 'presets': [{'id': 'manual', 'enabled': True},
                                         {'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}]}
        production.private_json(path, doc)
        original = path.read_bytes()
        settings = dict(production.DEFAULTS)
        production.private_json(settings_path, settings)
        discover = map_discovery.discover
        with patch.object(map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, self.analyze, **kw)):
            with self.assertRaisesRegex(production.SettingsError, 'PRESET_SELECTION_CHANGED'):
                with production.preset_update(self.root, SPEC):
                    settings['map_preset'] = 'gone'
                    production.private_json(settings_path, settings)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(production.read_json(settings_path)['map_preset'], 'gone')

    def test_missing_selection_preserves_original_preset_inode(self):
        self.map.unlink()
        path = self.root / production.PRESETS
        doc = {'version': 1, 'presets': [{'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}]}
        production.private_json(path, doc)
        production.private_json(self.root / 'data/config/amp-settings.json', {**production.DEFAULTS, 'map_preset': 'gone'})
        original = path.stat().st_ino
        with production.preset_update(self.root, SPEC) as check:
            self.assertTrue(check.selected_removed)
        self.assertEqual(path.stat().st_ino, original)
        self.assertEqual(production.read_json(path), doc)

    def test_missing_map_does_not_exchange_preset_file(self):
        self.map.unlink()
        path = self.root / production.PRESETS
        doc = {'version': 1, 'presets': [{'id': 'manual', 'enabled': True},
                                         {'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}]}
        production.private_json(path, doc)
        with patch.object(production.account, 'exchange', side_effect=AssertionError('unexpected exchange')):
            with production.preset_update(self.root, SPEC):
                pass
        self.assertEqual(production.read_json(path), doc)

    def test_bootstrap_selected_map_is_not_generated_as_a_new_preset(self):
        (self.root / 'data/maps/selected.w3x').write_bytes(b'other map' * 10)
        result, skipped = map_discovery.discover(self.root, self.doc, SPEC, [], {}, OWNER, self.analyze)
        self.assertEqual(skipped, 0)
        self.assertEqual([p.get('map_filename') for p in result['presets']], [None, 'Sample.w3x'])

    def test_analyzer_runs_isolated_with_only_the_map_bind_and_cleans_up(self):
        stub = self.root / 'docker_stub.py'
        trace = self.root / 'calls.jsonl'
        stub.write_text('''import json, sys
from pathlib import Path
args = sys.argv[1:]
with Path(sys.argv[0]).with_name("calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] == "run":
    print(json.dumps({"map_sha256": "a" * 64}))
''')
        result = map_discovery.analyze(self.map, Path('/host/instance/data/maps/Sample.w3x'), IMAGE, [sys.executable, str(stub)], dict(os.environ), OWNER)
        self.assertEqual(result['map_sha256'], 'a' * 64)
        run, cleanup = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(run[run.index('--network') + 1], 'none')
        self.assertIn('--read-only', run)
        self.assertEqual(sum(x == '--mount' for x in run), 1)
        self.assertEqual(run[run.index('--mount') + 1], 'type=bind,src=/host/instance/data/maps/Sample.w3x,dst=/selected.w3x,readonly')
        self.assertEqual(run[run.index('--user') + 1], OWNER)
        self.assertEqual(cleanup[:2], ['rm', '--force'])
        self.assertEqual(cleanup[2], run[run.index('--name') + 1])

    def test_discovery_uses_host_bind_path_for_a_different_control_path(self):
        spec = {**SPEC, 'host_instance_dir': '/host/amp/instance'}
        calls = []
        def capture(local, host, image, docker, env, owner):
            calls.append((local, host, image, owner))
            return analysis(local)
        result, skipped = map_discovery.discover(self.root, self.doc, spec, [], {}, OWNER, capture)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(result['presets']), 2)
        self.assertEqual(calls, [(self.map, Path('/host/amp/instance/data/maps/Sample.w3x'), IMAGE, OWNER)])

    def test_wrong_deployment_uid_rejects_before_analyzing(self):
        with patch.object(production.map_discovery, 'discover', side_effect=AssertionError('must not analyze')):
            with self.assertRaises(production.account.AccountError):
                with production.preset_update(self.root, {**SPEC, 'uid': os.geteuid() + 1}):
                    pass

    def test_conflicting_file_manager_save_is_preserved(self):
        path = self.root / production.PRESETS
        production.private_json(path, self.doc)
        def concurrent(path_map, *_):
            external = {'version': 1, 'presets': [{'id': 'user-map', 'enabled': True, 'map_filename': 'user.w3x'}]}
            production.private_json(path, external)
            return analysis(path_map)
        discover = map_discovery.discover
        with patch.object(production.map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, concurrent, **kw)):
            with self.assertRaises(production.SettingsError):
                with production.preset_update(self.root, SPEC):
                    pass
        self.assertEqual(json.loads(path.read_text())['presets'][0]['id'], 'user-map')
        self.assertTrue((self.root / production.PRESET_RECOVERY).is_dir())

    def test_start_transaction_refreshes_selector_and_keeps_user_settings(self):
        production.private_json(self.root / production.PRESET_MANIFEST,
                                [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset'}])
        discover = map_discovery.discover
        with patch.object(production.map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, self.analyze, **kw)):
            with production.preset_update(self.root, SPEC) as check:
                with production.preset_manifest_update(self.root, check):
                    check()
            saved = json.loads((self.root / production.PRESETS).read_text())
            self.assertEqual(len(saved['presets']), 2)
            options = json.loads((self.root / production.PRESET_MANIFEST).read_text())[0]['EnumValues']
            self.assertIn(saved['presets'][1]['id'], options)
            with production.preset_update(self.root, SPEC) as check:
                check()
            self.assertEqual(json.loads((self.root / production.PRESETS).read_text()), saved)

    def test_preflight_failure_rolls_back_generated_preset(self):
        path = self.root / production.PRESETS
        production.private_json(path, self.doc)
        original = path.read_bytes()
        discover = map_discovery.discover
        def candidate(path, *_):
            return {**analysis(path), 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        with patch.object(production.map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, candidate, **kw)):
            with self.assertRaisesRegex(RuntimeError, 'preflight'):
                with production.preset_update(self.root, SPEC):
                    raise RuntimeError('preflight')
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
