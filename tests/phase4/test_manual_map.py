import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.map_cycle import validate_full_cycle, validate_resolved_cycle


class ManualMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / 'config'
        (self.config / 'maps').mkdir(parents=True)
        (root / 'maps').mkdir()
        self.source = self.config / 'ghost.cfg'
        self.mapcfg = self.config / 'maps' / 'selected.cfg'
        self.mapfile = root / 'maps' / 'selected.w3x'
        self.source.write_text('bot_defaultmap = selected.cfg\n')
        self.mapcfg.write_text('map_path = Maps\\Download\\selected.w3x\nmap_localpath = selected.w3x\nmap_observers = 1\n')
        self.mapfile.write_bytes(b'x' * 64)
        self.game = {'max_players': 4, 'autostart_players': 3, 'vote_min_players': 2}
        self.analysis = {'slots': [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(4)],
                         'human_capacity': 4}

    def check(self, cycle=None, game=None):
        return validate_full_cycle(['manual'] if cycle is None else cycle,
                                   self.source, self.game if game is None else game)

    def test_valid_repeated_manual_and_no_writes(self):
        before = [p.read_bytes() for p in (self.source, self.mapcfg, self.mapfile)]
        with patch('src.runtime.manual_map.analyze_map', return_value=self.analysis) as analyze:
            self.assertEqual(self.check(['manual', 'manual']), ['manual', 'manual'])
            self.assertEqual(analyze.call_count, 2)
            self.assertEqual(analyze.call_args.args[0], self.mapfile)
        self.assertEqual(before, [p.read_bytes() for p in (self.source, self.mapcfg, self.mapfile)])

    def test_legacy_resolution_kept_separate_from_full_check(self):
        self.source.unlink()
        self.assertEqual(validate_resolved_cycle(['manual'], self.source, self.game), ['manual'])
        with self.assertRaises(OSError):
            self.check()

    def test_thresholds_and_computer_capacity(self):
        with patch('src.runtime.manual_map.analyze_map', return_value=self.analysis):
            with self.assertRaisesRegex(ValueError, 'MAP_CAPACITY'):
                self.check(game={'max_players': 5, 'autostart_players': 3, 'vote_min_players': 2})
            computers = [row[:] for row in self.analysis['slots']]
            computers[0][2:4] = [2, 1]
            with patch('src.runtime.manual_map.analyze_map', return_value={'slots': computers, 'human_capacity': 3}):
                with self.assertRaisesRegex(ValueError, 'HUMAN_CAPACITY'):
                    self.check(game={'max_players': 4, 'autostart_players': 4, 'vote_min_players': 2})
                self.assertEqual(self.check(game={'max_players': 4, 'autostart_players': 4,
                                                  'autostart_enabled': False, 'vote_min_players': 2}), ['manual'])
                with self.assertRaisesRegex(ValueError, 'HUMAN_CAPACITY'):
                    self.check(game={'max_players': 4, 'autostart_players': 2, 'vote_min_players': 4})
            with self.assertRaisesRegex(ValueError, 'GAMEPLAY'):
                self.check(game={'max_players': 1})

    def test_reject_unsafe_names_and_ambiguous_configs(self):
        for name in ('../selected.cfg', '/tmp/selected.cfg', 'sub/selected.cfg', 'sub\\selected.cfg', 'selected.cfg/..', ''):
            self.source.write_text('bot_defaultmap = ' + name + '\n')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.check()
        self.source.write_text('bot_defaultmap = selected.cfg\nbot_defaultmap = other.cfg\n')
        with self.assertRaisesRegex(ValueError, 'CONFIG_REJECTED'):
            self.check()
        self.source.write_text('bot_defaultmap = selected.cfg\n')
        for value in ('../selected.w3x', '/tmp/selected.w3x', 'dir\\selected.w3x', 'dir/selected.w3x'):
            self.mapcfg.write_text('map_localpath = ' + value + '\n')
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check()
        for extra in ('map_slot1 = 0 255 0 0 0 0 32 1 100', 'map_numplayers = 12', 'map_localpath = other.w3x'):
            self.mapcfg.write_text('map_localpath = selected.w3x\n' + extra + '\n')
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.check()

    def test_symlinks_and_missing_maps_rejected(self):
        for path in (self.source, self.mapcfg, self.mapfile):
            original = path.read_bytes()
            target = path.with_name('target-' + path.name)
            target.write_bytes(original)
            path.unlink()
            path.symlink_to(target)
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.check()
            path.unlink()
            path.write_bytes(original)
        self.mapfile.unlink()
        with self.assertRaises((ValueError, OSError)):
            self.check()

    def test_analyzer_errors_propagate_and_configs_are_bounded(self):
        with patch('src.runtime.manual_map.analyze_map', side_effect=ValueError('MAP_FORMAT_UNSUPPORTED')):
            with self.assertRaisesRegex(ValueError, 'MAP_FORMAT_UNSUPPORTED'):
                self.check()
        self.mapcfg.write_bytes(b'a' * 65537)
        with self.assertRaisesRegex(ValueError, 'CONFIG_REJECTED'):
            self.check()


if __name__ == '__main__':
    unittest.main()
