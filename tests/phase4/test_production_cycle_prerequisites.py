"""Configuration provenance selects catalog startup without bypassing validation."""
import json
from pathlib import Path
import tempfile
import unittest

from src.runtime.config import gameplay_config, iccup_config
from src.runtime.controller import RuntimeBackend
from src.supervisor.config import load_config


class ProductionCyclePrerequisiteTests(unittest.TestCase):
    def test_valid_multi_cycle_reaches_supervisor_configuration(self):
        """A valid cycle should reach startup validation rather than a global fuse."""
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            path.write_text(json.dumps({'mode':'iccup','hostbot':{'command':['ghost']},
                                       'iccup':{},'gameplay':{'map_cycle':['one','two']}}))
            config=load_config(path)
            self.assertEqual(config['gameplay']['map_cycle'],['one','two'])

    def test_explicit_single_cycle_opts_in_but_legacy_preset_retains_single_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            for game,expected in (({'map_cycle':['one'],'max_concurrent_matches':3},True),
                                  ({'map_cycle':['one','one'],'max_concurrent_matches':3},True),
                                  ({'map_preset':'one'},False)):
                with self.subTest(game=game):
                    path.write_text(json.dumps({'mode':'iccup','hostbot':{'command':['ghost']},
                                               'iccup':{},'gameplay':game}))
                    config=load_config(path)
                    self.assertEqual(config['_native_catalog_cycle'],expected)
                    backend=RuntimeBackend(config)
                    self.assertEqual(backend._catalog_required,expected)
                    if not expected:self.assertIsNone(backend.native_preload(1))

    def test_real_runtime_backend_without_catalog_fails_closed(self):
        """The normal constructor cannot silently fall back to legacy hosting."""
        config={'iccup':iccup_config({}),
                'gameplay':gameplay_config({'map_cycle':['one','two'],'max_concurrent_matches':2})}
        backend=RuntimeBackend(config)
        # With no prepared session, a cycle launch must fail closed rather than
        # produce the legacy no-catalog sentinel (None).
        with self.assertRaisesRegex(ValueError, 'NATIVE_CATALOG_STARTUP_REQUIRED'):
            backend.native_preload(1)
        with self.assertRaisesRegex(ValueError, 'NATIVE_CATALOG_STARTUP_REQUIRED'):
            backend.command('native', 1)


if __name__=='__main__':unittest.main(verbosity=2)
