"""Operator multi-cycle handoff; Docker and map analysis stay offline/stubbed."""
import io
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from amp import production
from src.runtime.map_cycle import validate_full_cycle


class OperatorCycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('control', 'data/config', 'data/maps'):
            (self.root / name).mkdir(parents=True, mode=0o700)
        self.selector = self.root / 'data/config/amp-settings.json'
        self.config = self.root / 'data/config/supervisor.json'
        self.ghost = self.root / 'data/config/ghost.cfg'
        self.ghost.write_text('bot_defaultmap = selected.cfg\n')
        self.document = {'version': 1, 'presets': [
            {'id': 'one', 'enabled': True, 'map_filename': 'one.w3x'},
            {'id': 'two', 'enabled': True, 'map_filename': 'two.w3x'},
            {'id': 'off', 'enabled': False, 'map_filename': 'off.w3x'}]}
        production.private_json(self.root / production.PRESETS, self.document)
        for name in ('one.w3x', 'two.w3x'):
            (self.root / 'data/maps' / name).write_bytes(b'synthetic')
        self.raw = {**production.DEFAULTS, 'map_preset': 'stale',
                    'map_cycle': '["one", "two", "one"]', 'max_concurrent_matches': '5'}
        production.private_json(self.selector, self.raw)
        production.private_json(self.config, {'http': {'host': '127.0.0.1', 'port': 8080},
                                             'iccup': {'keep': 'unchanged'}})
        self.spec = {'uid': 1000, 'gid': 1000, 'image_id': 'sha256:' + 'a' * 64,
                     'host_instance_dir': '/host/private-instance'}

    def test_selection_and_merge_preserve_full_normalized_snapshot(self):
        selected = production.validate_preset_selection(self.root)
        self.assertEqual(selected['map_cycle'], ['one', 'two', 'one'])
        self.assertEqual(selected['max_concurrent_matches'], 5)
        production.private_json(self.selector, production.DEFAULTS)
        production.merge_settings(self.config, self.selector, validated=selected)
        result = production.read_json(self.config)
        self.assertEqual(result['gameplay']['map_cycle'], ['one', 'two', 'one'])
        self.assertEqual(result['gameplay']['max_concurrent_matches'], 5)
        self.assertNotIn('map_preset', result['gameplay'])
        self.assertEqual(result['iccup'], {'keep': 'unchanged'})

    def test_invalid_later_ids_or_limits_never_write_supervisor(self):
        original = self.config.read_bytes()
        for change in ({'map_cycle': ['one', 'off']}, {'map_cycle': ['one', 'unknown']},
                       {'max_concurrent_matches': 0}, {'max_concurrent_matches': 6}):
            with self.subTest(change=change):
                production.private_json(self.selector, {**self.raw, **change})
                with self.assertRaises(ValueError):
                    production.validate_preset_selection(self.root)
                self.assertEqual(self.config.read_bytes(), original)

    def test_disabled_autohost_cannot_be_silently_overridden_by_catalog_startup(self):
        original=self.config.read_bytes()
        production.private_json(self.selector,{**self.raw,'autohost_enabled':False})
        with self.assertRaisesRegex(production.SettingsError,'MAP_CYCLE_AUTOHOST_REQUIRED'):
            production.validate_preset_selection(self.root)
        self.assertEqual(self.config.read_bytes(),original)

    def test_candidate_command_is_offline_readonly_secret_free_and_snapshot_bound(self):
        selected = production.validate_preset_selection(self.root)
        production.private_json(self.selector, production.DEFAULTS)
        with patch.dict(os.environ, {'DOCKER_HOST': 'untrusted'}), \
             patch.object(production.subprocess, 'run') as run:
            production.validate_cycle_candidate(self.root, self.spec, selected)
        args = run.call_args_list[0].args[0]
        options = run.call_args_list[0].kwargs
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertEqual(args[args.index('--user') + 1], '1000:1000')
        for flag in ('--read-only', '--pull=never', '--no-healthcheck'):
            self.assertIn(flag, args)
        mounts = [args[i + 1] for i, item in enumerate(args) if item == '--mount']
        self.assertEqual(mounts, [
            'type=bind,src=/host/private-instance/data/config,dst=/data/config,readonly',
            'type=bind,src=/host/private-instance/data/maps,dst=/data/maps,readonly'])
        self.assertEqual(args[-3], self.spec['image_id'])
        payload = json.loads(options['input'])
        self.assertEqual(payload['map_cycle'], ['one', 'two', 'one'])
        self.assertNotIn('map_preset', payload)
        self.assertNotIn('DOCKER_HOST', options['env'])
        self.assertEqual(options['stdout'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertTrue(options['check'])
        self.assertEqual(run.call_args_list[1].args[0][-1], args[args.index('--name') + 1])

    def execute_candidate(self, game, analyze):
        """Run the actual inline program against the repository validator locally."""
        def invoke(args, **kwargs):
            if '-c' not in args:
                return subprocess.CompletedProcess(args, 0)
            def validate(cycle, source, settings):
                self.assertEqual(source, '/data/config/ghost.cfg')
                return validate_full_cycle(cycle, self.ghost, settings)
            try:
                with patch('sys.stdin', io.StringIO(kwargs['input'].decode())), \
                     patch('src.runtime.map_cycle.validate_full_cycle', side_effect=validate), \
                     patch('src.runtime.presets.analyze_map', side_effect=analyze):
                    exec(args[-1], {})
            except ValueError:
                raise subprocess.CalledProcessError(1, args) from None
            return subprocess.CompletedProcess(args, 0)
        with patch.object(production.subprocess, 'run', side_effect=invoke):
            production.validate_cycle_candidate(self.root, self.spec, game)

    def analysis(self, path):
        path.read_bytes()  # Missing maps must not become successful mocked analyses.
        return {'slots': [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(12)],
                'human_capacity': 12, 'lobby_name': 'Fixture', 'map_sha256': 'a' * 64,
                'script_sha256': 'b' * 64, 'ai_evidence': {'features': []}}

    def test_candidate_analyzes_all_occurrences_before_returning(self):
        seen = []
        def analyze(path):
            seen.append(path.name)
            return self.analysis(path)
        before = self.config.read_bytes()
        self.execute_candidate(production.settings(self.raw), analyze)
        self.assertEqual(seen, ['one.w3x', 'two.w3x', 'one.w3x'])
        self.assertEqual(self.config.read_bytes(), before)

    def test_bad_later_map_or_ai_approval_fails_whole_candidate(self):
        for failure in ('map', 'ai'):
            with self.subTest(failure=failure):
                if failure == 'map':
                    def analyze(path):
                        if path.name == 'two.w3x':
                            raise ValueError('invalid later map')
                        return self.analysis(path)
                else:
                    self.document['presets'][1]['ai'] = {'enabled': True, 'target': 4}
                    production.private_json(self.root / production.PRESETS, self.document)
                    analyze = self.analysis
                before = self.config.read_bytes()
                with self.assertRaisesRegex(production.SettingsError, 'MAP_CYCLE_PREFLIGHT_FAILED'):
                    self.execute_candidate(production.settings(self.raw), analyze)
                self.assertEqual(self.config.read_bytes(), before)

    def test_timeout_or_old_image_failure_is_closed_and_named_container_cleaned(self):
        for error in (subprocess.TimeoutExpired('docker', 120),
                      subprocess.CalledProcessError(1, 'docker'), OSError('unavailable')):
            with self.subTest(error=type(error).__name__), \
                 patch.object(production.subprocess, 'run', side_effect=[error, None]) as run:
                with self.assertRaises(production.SettingsError) as raised:
                    production.validate_cycle_candidate(self.root, self.spec, production.settings(self.raw))
                self.assertEqual(raised.exception.args, ('MAP_CYCLE_PREFLIGHT_FAILED',))
                self.assertNotIn('unavailable', raised.exception.public_message)
                self.assertEqual(run.call_args.args[0][3:5], ['rm', '--force'])

    def test_legacy_single_entry_keeps_existing_image_validation_path(self):
        with patch.object(production.subprocess, 'run') as run:
            production.validate_cycle_candidate(self.root, self.spec, production.DEFAULTS)
        run.assert_not_called()

    def test_explicit_preflight_validates_candidate_without_merge_or_launch(self):
        events = []
        def candidate(instance, spec, selected):
            events.append('candidate')
            self.assertEqual(selected['map_cycle'], ['one', 'two', 'one'])
        with patch.object(production, 'deployment', return_value=self.spec), \
             patch.object(production, 'preflight', side_effect=lambda *a: events.append('preflight')), \
             patch.object(production, 'validate_cycle_candidate', side_effect=candidate), \
             patch.object(production, 'merge_settings') as merge, \
             patch.object(production.os, 'execvpe') as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(production.main(['preflight', '--instance-dir', str(self.root)]), 0)
        self.assertEqual(events, ['preflight', 'candidate'])
        merge.assert_not_called()
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
