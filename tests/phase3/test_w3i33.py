"""W3I33 inspection is independent of legacy-client hosting approval."""
from pathlib import Path
import struct
import unittest

import test_map_presets as legacy
from src.runtime.mpq import extract_map_info
from src.runtime.presets import map_info, analyze_map


def modern_info(*, colours=(0, 1, 2), language=0, graphics=1, game_data=1):
    u = lambda *values: struct.pack('<' + 'I' * len(values), *values)
    b = u(33, 7, 6060) + u(2, 0, 4, 23745)
    b += b'Modern Test\0Author\0Description\0Three players\0'
    b += struct.pack('<8f4I', *([0.0] * 8), *([0] * 4))
    b += u(152, 180, 32) + b'Z'
    b += u(0) + b'Loading.mdx\0Text\0Title\0Subtitle\0'
    b += u(0) + b'Prologue.mdx\0Text\0Title\0Subtitle\0'
    b += u(0) + struct.pack('<3f', 0.0, 100.0, 0.5) + bytes((255, 128, 0, 255))
    b += b'RAIN' + b'Dungeon\0' + b'Z' + bytes((255, 128, 0, 255))
    b += u(language, graphics, game_data) + struct.pack('<3f', 1650.0, 3000.0, 1250.0)
    b += u(len(colours))
    for colour in colours:
        b += u(colour, 1, 2, 1) + b'Player\0'
        b += struct.pack('<2f4I', 100.0, 200.0, 0, 0, 0, 0)
    return b + u(1, 11, sum(1 << c for c in colours)) + b'Co-op\0'


class W3I33Tests(unittest.TestCase):
    def test_exact_modern_header_and_extra_player_fields(self):
        info = map_info(modern_info())
        self.assertEqual(info['format'], 33)
        self.assertEqual(info['game_version'], [2, 0, 4, 23745])
        self.assertEqual(info['title'], b'Modern Test')
        self.assertEqual(info['human_capacity'], 3)
        self.assertEqual([s[5] for s in info['slots']], [0, 1, 2])
        self.assertEqual([s[4] for s in info['slots']], [0, 0, 0])
        self.assertEqual(info['script_language'], 'jass')
        self.assertIs(info['classic_compatible'], False)

    def test_lua_and_modern_player_colours_are_inspectable_not_approved(self):
        info = map_info(modern_info(colours=(0, 12, 23), language=1))
        self.assertEqual(info['script_language'], 'lua')
        self.assertEqual([s[5] for s in info['slots']], [0, 12, 23])
        self.assertIs(info['classic_compatible'], False)

    def test_invalid_settings_duplicates_colours_and_truncation_reject(self):
        variants = [modern_info(language=2), modern_info(graphics=0), modern_info(game_data=2),
                    modern_info(colours=(0, 0)), modern_info(colours=(24,))]
        raw = modern_info()
        variants += [raw[:n] for n in (0, 4, 12, 27, 100, 200, len(raw) - 1)]
        for raw in variants:
            with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                map_info(raw)

    def test_legacy_results_and_future_unknown_format_remain_strict(self):
        for version in (18, 25):
            info = map_info(legacy.info_fixture(version=version))
            self.assertEqual(info['format'], version)
            self.assertNotIn('game_version', info)
            self.assertNotIn('classic_compatible', info)
        raw = bytearray(modern_info());struct.pack_into('<I', raw, 0, 34)
        with self.assertRaisesRegex(ValueError, 'MAP_FORMAT_UNSUPPORTED'):
            map_info(raw)

    def test_sealed_modern_analysis_cannot_bypass_classic_host_gate(self):
        from src.runtime.presets import _resolve_named_snapshot
        with legacy.tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);(root / 'config').mkdir();(root / 'maps').mkdir()
            (root / 'maps/test.w3x').write_bytes(b'fixture')
            doc = legacy.document();presets = {p['id']: p for p in doc['presets']}
            game = legacy.gameplay_config({'map_preset': 'test-map'})
            analysis = map_info(modern_info())
            with self.assertRaisesRegex(ValueError, 'MAP_REQUIRES_NEWER_WARCRAFT'):
                _resolve_named_snapshot(root / 'config/ghost.cfg', game, presets, {}, sealed_analysis=analysis)


@unittest.skipUnless(Path(legacy.STORM).is_file(), 'Existing offline runtime StormLib required')
class W3I33ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.MPQTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_info_only_inspects_large_or_missing_scripts_without_relaxing_limits(self):
        for members in ({'war3map.w3i': modern_info()},
                        {'war3map.w3i': modern_info(), 'scripts\\war3map.j': b'x' * (4 * 1024 * 1024 + 1)}):
            path = self.fixture.mpq(members=members, name=str(len(members)) + '.w3x')
            info = map_info(extract_map_info(path, library=legacy.STORM))
            self.assertEqual(info['game_version'], [2, 0, 4, 23745])
            self.assertIs(info['classic_compatible'], False)
            with self.assertRaises(ValueError):
                analyze_map(path)

    def test_small_valid_jass_still_cannot_approve_a_modern_map(self):
        path = self.fixture.mpq(info=modern_info(), script=legacy.PLAIN_SCRIPT)
        with self.assertRaisesRegex(ValueError, 'MAP_REQUIRES_NEWER_WARCRAFT'):
            analyze_map(path)
