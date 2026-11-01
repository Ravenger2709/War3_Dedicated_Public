"""Offline tests for safe discovery and File Manager conflicts."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from amp import map_discovery, production

IMAGE = 'sha256:' + '1' * 64
SPEC = {'image_id': IMAGE, 'uid': os.geteuid(), 'gid': os.getegid()}
OWNER = f'{os.geteuid()}:{os.getegid()}'


def analysis(path):
    return {'map_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'script_sha256': 'a' * 64,
            'lobby_name': 'Sample map', 'slots': [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(4)],
            'human_capacity': 4}


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
        self.assertEqual(entry['ai'], {'enabled': False})
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
        with patch.object(production.map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner: discover(i, d, spec, docker, env, owner, concurrent)):
            with self.assertRaises(production.SettingsError):
                with production.preset_update(self.root, SPEC):
                    pass
        self.assertEqual(json.loads(path.read_text())['presets'][0]['id'], 'user-map')
        self.assertTrue((self.root / production.PRESET_RECOVERY).is_dir())

    def test_start_transaction_refreshes_selector_and_keeps_user_settings(self):
        production.private_json(self.root / production.PRESET_MANIFEST,
                                [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset'}])
        discover = map_discovery.discover
        with patch.object(production.map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner: discover(i, d, spec, docker, env, owner, self.analyze)):
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
        with patch.object(production.map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner: discover(i, d, spec, docker, env, owner, self.analyze)):
            with self.assertRaisesRegex(RuntimeError, 'preflight'):
                with production.preset_update(self.root, SPEC):
                    raise RuntimeError('preflight')
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
