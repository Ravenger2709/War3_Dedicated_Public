"""Source-only diagnostic path tests; Docker and probes never execute."""
from contextlib import redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('production_local_check', ROOT / 'scripts/production-local-check.py')
local_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_check)
IMAGE = 'sha256:' + 'a' * 64


class LocalCheckAssetPathsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / 'public-checkout'
        for name in ('config', 'docker', 'tests/phase3'):
            (self.checkout / name).mkdir(parents=True)
        for name in ('config/supervisor-iccup.example.json', 'docker/ghost.cfg'):
            shutil.copyfile(ROOT / name, self.checkout / name)
        (self.checkout / 'tests/phase3/production_probe.py').write_text('# synthetic probe; never executed\n')
        self.star = self.root / 'explicit star'
        self.star.mkdir()
        (self.star / 'vendor-marker').write_bytes(b'original vendor input')
        self.game = self.root / 'explicit game'
        (self.game / 'maps/download').mkdir(parents=True)
        for name in ('war3.exe', 'game.dll', 'War3Patch.mpq', 'not-selected.exe'):
            (self.game / name).write_bytes(('original ' + name).encode())
        self.default_map = self.game / 'maps/download/iCCup DotA 501.w3x'
        self.default_map.write_bytes(b'synthetic default map')
        self.redist = self.root / 'explicit-redist.exe'
        self.redist.write_bytes(b'synthetic redist')
        self.run_dir = self.root / 'diagnostic-run'
        self.base = ['--image', IMAGE, '--run-dir', str(self.run_dir)]
        self.assets = ['--star-dir', str(self.star), '--game-dir', str(self.game), '--redist', str(self.redist)]

    def invoke(self, arguments):
        """Model only Docker command/inspect data, never start a container."""
        self.commands = []
        self.mounts = {}
        self.metadata = None
        def run(command, **kwargs):
            self.commands.append(command)
            self.assertEqual(command[:3], ['docker', '--host', 'unix:///var/run/docker.sock'])
            if command[3] == 'create':
                for index, value in enumerate(command):
                    if value == '--mount':
                        fields = command[index + 1].split(',')
                        data = dict(field.split('=', 1) for field in fields if '=' in field)
                        self.mounts[data['dst']] = (Path(data['src']), 'readonly' in fields)
                self.metadata = {
                    'Image': IMAGE, 'Config': {'User': '1000:1000'},
                    'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True,
                                   'CapDrop': ['ALL'], 'Privileged': False,
                                   'Init': True, 'SecurityOpt': ['no-new-privileges:true'],
                                   'Memory': 2 * 1024**3, 'PidsLimit': 256},
                    'Mounts': [{'Type': 'bind', 'Destination': target, 'RW': not readonly}
                               for target, (_, readonly) in self.mounts.items()],
                    'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': False,
                              'StartedAt': 'synthetic', 'FinishedAt': 'synthetic'}}
            return SimpleNamespace(returncode=0)
        def inspect(command, **kwargs):
            self.assertEqual(command[3], 'inspect')
            return json.dumps([self.metadata]).encode()
        with (patch.object(local_check, 'ROOT', self.checkout),
              patch.object(local_check.os, 'geteuid', return_value=1000),
              patch.object(local_check.os, 'getegid', return_value=1000),
              patch.object(local_check.signal, 'signal'),
              patch.object(local_check.subprocess, 'run', side_effect=run),
              patch.object(local_check.subprocess, 'check_output', side_effect=inspect),
              patch.object(local_check.shutil, 'copytree', side_effect=AssertionError('broad copy'))):
            return local_check.main(arguments)

    def test_each_asset_argument_is_required_before_docker_or_output(self):
        for option in ('--star-dir', '--game-dir', '--redist'):
            arguments = list(self.assets)
            index = arguments.index(option)
            del arguments[index:index + 2]
            with self.subTest(option=option), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.invoke(self.base + arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(self.commands, [])
                self.assertFalse(self.run_dir.exists())

    def test_explicit_assets_default_map_and_original_metadata_only(self):
        self.assertEqual(self.invoke(self.base + self.assets), 0)
        self.assertEqual(self.mounts['/vendor/star'], (self.star, True))
        self.assertEqual(self.mounts['/vendor/redist/vc_redist.x86.exe'], (self.redist, True))
        self.assertEqual(self.mounts['/vendor/game'], (self.run_dir / 'game', True))
        self.assertEqual(self.mounts['/data/warcraft/War3Patch.mpq'], (self.game / 'War3Patch.mpq', True))
        self.assertEqual(self.mounts['/data/maps/selected.w3x'], (self.default_map, True))
        self.assertEqual(sorted(path.name for path in (self.run_dir / 'game').iterdir()), ['game.dll', 'war3.exe'])
        for name in ('war3.exe', 'game.dll'):
            self.assertEqual((self.run_dir / 'game' / name).read_bytes(), (self.game / name).read_bytes())
        self.assertNotIn(self.game, [source for source, _ in self.mounts.values()])
        self.assertFalse((self.run_dir / 'star').exists())
        self.assertEqual(self.mounts['/vendor/profile'], (self.run_dir / 'profile', True))
        self.assertEqual([command[3] for command in self.commands], ['create', 'start', 'rm'])
        self.assertEqual(self.commands[0][self.commands[0].index('--network') + 1], 'none')
        self.assertEqual(json.loads((self.run_dir / 'isolation.json').read_text())['result'], 'PASS')

    def test_named_map_override_still_uses_its_exact_filename(self):
        selected = self.root / 'named.w3x'
        selected.write_bytes(b'synthetic explicit map')
        presets = self.root / 'presets.json'
        presets.write_text(json.dumps({'version': 1, 'presets': [
            {'id': 'named', 'enabled': True, 'map_filename': selected.name}]}))
        self.default_map.unlink()
        self.assertEqual(self.invoke(self.base + self.assets + [
            '--map', str(selected), '--presets', str(presets), '--map-preset', 'named']), 0)
        self.assertEqual(self.mounts['/data/maps/named.w3x'], (selected, True))
        self.assertNotIn('/data/maps/selected.w3x', self.mounts)
        self.assertEqual((self.run_dir / 'config/presets.json').read_bytes(), presets.read_bytes())

    def test_asset_arguments_use_existing_real_path_validation(self):
        for option in ('--star-dir', '--game-dir', '--redist'):
            original = Path(self.assets[self.assets.index(option) + 1])
            link = self.root / ('link-' + option[2:])
            link.symlink_to(original, target_is_directory=original.is_dir())
            for bad in (str(link), str(self.root / '..' / original.name),
                        str(self.root / 'comma,input'), str(self.root / 'control\ninput')):
                arguments = list(self.assets)
                arguments[arguments.index(option) + 1] = bad
                with self.subTest(option=option, bad=bad), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        self.invoke(self.base + arguments)
                    self.assertEqual(raised.exception.code, 2)
                    self.assertFalse(self.run_dir.exists())
                    self.assertEqual(self.commands, [])

    def test_metadata_symlink_is_rejected_before_docker(self):
        metadata = self.game / 'game.dll'
        original = self.game / 'original.dll'
        metadata.rename(original)
        metadata.symlink_to(original)
        with self.assertRaisesRegex(ValueError, 'INPUT_SYMLINK_REJECTED'):
            self.invoke(self.base + self.assets)
        self.assertEqual(self.commands, [])

    def test_no_research_relative_asset_paths_remain(self):
        source = (ROOT / 'scripts/production-local-check.py').read_text()
        self.assertNotIn("ROOT/'artifacts", source)
        self.assertNotIn("ROOT/'Warcraft 3 Frozen Throne'", source)
        # The successful fixture checkout contains only public templates/probe.
        self.assertFalse((self.checkout / 'artifacts').exists())


if __name__ == '__main__':
    unittest.main()
