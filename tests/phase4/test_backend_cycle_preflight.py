"""Invalid cycle input is rejected before acquiring account or launch state."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src.runtime.config import gameplay_config, iccup_config
from src.runtime.controller import ICCupSupervisor
from src.runtime import preload_catalog

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('cycle_preflight_maps',ROOT/'tests/phase4/test_preload_catalog.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


@unittest.skipUnless(Path(fixture.fixture.STORM).is_file(),'offline StormLib required')
class BackendPreflightTests(unittest.TestCase):
    def setUp(self):
        self.maps=fixture.PreparedCatalogTests('runTest');self.maps.setUp()
        self.addCleanup(self.maps.doCleanups)
        self.root=self.maps.root
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.config={'mode':'iccup','_native_catalog_cycle':True,
                     'runtime_dir':str(self.state/'runtime'),
                     'hostbot':{'cwd':str(self.root),'command':['/opt/war3/bin/ghost++',str(self.state/'native/ghost.cfg')],
                                'startup_timeout':5,'stop_timeout':1,'lobby_timeout':5},
                     'http':{'host':'127.0.0.1','port':0},
                     'recovery':{'max_restarts':2,'backoff_initial':1,'backoff_max':2,'stable_seconds':30},
                     'iccup':{**iccup_config({}),'native_config':str(self.maps.builder.source),
                              'bridge_dir':str(self.state/'bridge'),'prefix':str(self.root/'prefix'),
                              'session':str(self.root/'session'),'profile':str(self.root/'absent-profile')},
                     'gameplay':gameplay_config({**self.maps.game,'map_cycle':['one','two','one']})}

    def test_missing_later_map_never_claims_launch_or_reads_profile(self):
        self.maps.b.unlink()
        with (patch('src.runtime.controller.private_bytes',side_effect=AssertionError('account read')) as account,
              patch('src.runtime.controller.subprocess.Popen',side_effect=AssertionError('child launch')) as spawn):
            with self.assertRaises(FileNotFoundError):ICCupSupervisor(self.config)
        account.assert_not_called();spawn.assert_not_called()
        self.assertFalse((self.state/'cycle').exists())
        self.assertFalse(list(self.state.glob('native-launch-*')))

    def test_disabled_autohost_is_rejected_before_any_launch_or_profile_access(self):
        self.config['gameplay']['autohost_enabled']=False
        with (patch('src.runtime.controller.private_bytes',side_effect=AssertionError('account read')) as account,
              patch('src.runtime.controller.subprocess.Popen',side_effect=AssertionError('child launch')) as spawn):
            with self.assertRaisesRegex(ValueError,'MAP_CYCLE_AUTOHOST_REQUIRED'):ICCupSupervisor(self.config)
        account.assert_not_called();spawn.assert_not_called()
        self.assertFalse((self.state/'cycle').exists())

    def test_missing_preset_leaves_input_and_cursor_unmodified(self):
        self.config['gameplay']['map_cycle']=['one','unknown']
        before=self.maps.preset_path.read_bytes()
        with (patch('src.runtime.controller.private_bytes',side_effect=AssertionError('account read')),
              patch('src.runtime.controller.subprocess.Popen',side_effect=AssertionError('child launch'))):
            with self.assertRaisesRegex(ValueError,'UNAVAILABLE'):ICCupSupervisor(self.config)
        self.assertEqual(self.maps.preset_path.read_bytes(),before)
        self.assertFalse((self.state/'cycle').exists())

    def test_invalid_later_ai_closes_sealed_inputs_before_account_access(self):
        self.maps.doc['presets'][1]['ai']={'enabled':True,'target':3}
        self.maps.preset_path.write_text(json.dumps(self.maps.doc))
        actual=preload_catalog.prepare_preload_cycle
        def prepare(*args,**kwargs):return actual(*args,**kwargs,library=fixture.fixture.STORM)
        descriptors=set(os.listdir('/proc/self/fd'))
        with (patch.object(preload_catalog,'prepare_preload_cycle',side_effect=prepare),
              patch('src.runtime.controller.private_bytes',side_effect=AssertionError('account read')) as account,
              patch.object(ICCupSupervisor,'_spawn',side_effect=AssertionError('native launch')) as spawn):
            with self.assertRaisesRegex(ValueError,'AI_'):ICCupSupervisor(self.config)
        self.assertEqual(set(os.listdir('/proc/self/fd')),descriptors)
        account.assert_not_called();spawn.assert_not_called()
        self.assertFalse((self.state/'cycle').exists())


if __name__=='__main__':unittest.main()
