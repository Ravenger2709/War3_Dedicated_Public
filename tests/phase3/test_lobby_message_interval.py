"""AMP's single global reminder interval; temp-owned files and no Docker calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "amp"))
import production


class LobbyMessageIntervalTests(unittest.TestCase):
    def test_missing_legacy_key_defaults_without_mutating_input(self):
        legacy = {key: value for key, value in production.DEFAULTS.items()
                  if key != "lobby_message_interval"}
        original = dict(legacy)
        result = production.settings(legacy)
        self.assertEqual(result["lobby_message_interval"], 20)
        self.assertEqual(legacy, original)
        self.assertEqual(production.settings(result), result)

    def test_bounded_integer_or_amp_decimal_string_only(self):
        for value in (10, 20, 3600, "10", "20", "3600"):
            with self.subTest(value=value):
                result = production.settings({**production.DEFAULTS, "lobby_message_interval": value})
                self.assertIs(type(result["lobby_message_interval"]), int)
                self.assertEqual(result["lobby_message_interval"], int(value))
        for value in (True, False, None, 9, 3601, -1, 20.0, "9", "3601", "20.0",
                      "+20", " 20", "20 ", "1e2", "", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                production.settings({**production.DEFAULTS, "lobby_message_interval": value})

    def test_global_interval_survives_every_selected_preset_and_legacy_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("control", "data", "data/config"):
                (root / name).mkdir(mode=0o700)
            settings_path = root / "data/config/amp-settings.json"
            config_path = root / "data/config/supervisor.json"
            production.private_json(root / production.PRESETS, {"version": 1, "presets": [
                {"id": "one", "enabled": True, "map_filename": "one.w3x"},
                {"id": "two", "enabled": True, "map_filename": "two.w3x"}]})
            for cycle in (["one"], ["two"], ["one", "two", "one"]):
                for interval in (None, "45"):
                    raw = {**production.DEFAULTS, "map_cycle": cycle}
                    if interval is None:
                        raw.pop("lobby_message_interval")
                    else:
                        raw["lobby_message_interval"] = interval
                    production.private_json(settings_path, raw)
                    production.private_json(config_path, {"http": {"host": "127.0.0.1", "port": 8080},
                                                         "hostbot": {"preserved": True}})
                    before = settings_path.read_bytes()
                    validated = production.validate_preset_selection(root)
                    production.merge_settings(config_path, settings_path, validated=validated)
                    saved = production.read_json(config_path)
                    self.assertEqual(saved["gameplay"]["lobby_message_interval"], 20 if interval is None else 45)
                    self.assertEqual(saved["gameplay"]["map_cycle"], cycle)
                    self.assertEqual(saved["hostbot"], {"preserved": True})
                    self.assertNotIn("bot_lobby_message_interval", saved["gameplay"])
                    self.assertEqual(settings_path.read_bytes(), before)

    def test_invalid_interval_does_not_write_supervisor_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "control").mkdir(mode=0o700)
            config_path = root / "supervisor.json"
            settings_path = root / "settings.json"
            production.private_json(config_path, {"http": {"host": "127.0.0.1", "port": 8080}})
            before = (config_path.read_bytes(), config_path.stat().st_ino)
            for value in (True, 9, "3601"):
                production.private_json(settings_path, {**production.DEFAULTS, "lobby_message_interval": value})
                entries = set(root.rglob("*"))
                with self.assertRaises(ValueError):
                    production.merge_settings(config_path, settings_path)
                self.assertEqual((config_path.read_bytes(), config_path.stat().st_ino), before)
                self.assertEqual(set(root.rglob("*")), entries)

    def test_template_exposes_one_shared_global_field(self):
        fields = json.loads((ROOT / "amp/war3-productionconfig.json").read_text())
        matches = [field for field in fields if field["ParamFieldName"] == "lobby_message_interval"]
        self.assertEqual(len(matches), 1)
        field = matches[0]
        self.assertEqual(field["FieldName"], "lobby_message_interval")
        self.assertEqual(field["DisplayName"], "Lobby message interval (seconds)")
        self.assertEqual(field["Subcategory"], "Lobby / Messages:chat:35")
        self.assertEqual((field["DefaultValue"], field["minValue"], field["maxValue"]), ("20", "10", "3600"))
        self.assertEqual(field["InputType"], "number")
        self.assertIn("auto-start", field["Description"])
        self.assertIn("!vote", field["Description"])
        self.assertIn("next AMP Start/Restart", field["Description"])
        self.assertFalse(field["IncludeInCommandLine"])
        self.assertEqual([item["ParamFieldName"] for item in fields
                          if item["ParamFieldName"].endswith("message_interval")], ["lobby_message_interval"])


if __name__ == "__main__":
    unittest.main()
