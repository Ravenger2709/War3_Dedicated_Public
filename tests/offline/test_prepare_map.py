#!/usr/bin/env python3
"""Validate operator inputs against the real private map without sockets."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MAP = ROOT / 'Warcraft 3 Frozen Throne/maps/Ather/(4)HearthglenHills.w3x'


@unittest.skipUnless(MAP.is_file() and (ROOT / 'build/ghost/mpq-map-info').is_file(),
                     'requires private test map and compiled native MPQ helper')
class PrepareMapValidation(unittest.TestCase):
    def reject(self, args, expected):
        with tempfile.TemporaryDirectory(prefix='ghost-negative-', dir=ROOT / 'runtime') as folder:
            runtime = Path(folder) / 'must-not-exist'
            command = ['python3', str(ROOT / 'scripts/prepare-map.py'), '--game-dir', str(MAP.parents[2]),
                       '--map', str(MAP), '--runtime', str(runtime)] + args
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected, result.stderr)
            self.assertFalse(runtime.exists())

    def test_unreachable_threshold_without_cap(self):
        self.reject(['--players', '5'], 'exceeds 4 human slots')

    def test_cap_above_map_capacity(self):
        self.reject(['--max-players', '5'], 'exceeds 4 human slots')

    def test_name_cannot_inject_log_delimiter(self):
        self.reject(['--name', 'bad]name'], 'without CR')

    def test_countdown_out_of_range(self):
        self.reject(['--countdown-seconds', '61'], '0..60')


if __name__ == '__main__':
    (ROOT / 'runtime').mkdir(exist_ok=True)
    unittest.main()
