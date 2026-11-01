"""Synthetic operator assets and stub Docker only; run in the accepted offline image."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
from amp import bootstrap as boot
from amp import production

APPROVED = "sha256:" + "b" * 64
CLASSIC = "sha256:" + "d" * 64
PULL = "ghcr.io/example/war3@" + APPROVED


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


class OperatorBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.star, self.game = (self.root / n for n in ("repo", "star", "game"))
        for directory in (self.repo / "config", self.star, self.game):
            directory.mkdir(parents=True, mode=0o700)
        self.destination = self.root / "war3-production"
        rows = []
        for name in sorted(boot.VENDOR_PATHS):
            source = self.star / name
            source.parent.mkdir(parents=True, exist_ok=True)
            data = ("SYNTHETIC_NOT_PE " + name).encode()
            source.write_bytes(data)
            rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.manifest = self.repo / "config/star-vendor-files.json"
        write_json(self.manifest, {"version": 1, "files": rows})
        self.redist = self.star / "redist/vc_redist.x86.exe"
        self.redist.parent.mkdir(mode=0o700)
        self.redist.write_bytes(b"SYNTHETIC_NOT_PE VC2015 fixture")
        self.map = self.root / "operator-chosen.w3x"
        self.map.write_bytes(b"SYNTHETIC_NOT_MAP selected fixture")
        for name in ("war3.exe", "game.dll", "War3Patch.mpq"):
            (self.game / name).write_bytes(("SYNTHETIC_NOT_PE " + name).encode())
        # These files must never be opened, copied, chmod'ed or interpreted.
        for directory in (self.star, self.game):
            for name in ("launcher.ini", "iccup.ini", "password.txt", "War3.mpq", "Frozen Throne.exe"):
                (directory / name).write_bytes(b"unrelated original input")
        supervisor = json.loads((REPO / "config/supervisor-iccup.example.json").read_text())
        write_json(self.repo / "config/supervisor-iccup.example.json", supervisor)
        sources = {}
        for name in ("src/supervisor/config.py", "src/runtime/config.py", "config/supervisor-iccup.example.json"):
            path = self.repo / name
            if name.startswith("src/"):
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes((REPO / name).read_bytes())
            sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.accepted = {"schema_version": 1, "image_id": APPROVED, "approved_by_main": True,
                         "source_sha256": {"config/star-vendor-files.json": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                                           "src/runtime/star.py": "a" * 64, **sources},
                         "checks": {key: "PASS" for key in production.CHECKS}}
        self.gate = self.root / "main-approved.json"
        write_json(self.gate, self.accepted)
        self.digest = production.gate(self.accepted, APPROVED)
        self.meta = {"Id": APPROVED, "Os": "linux", "Architecture": "amd64",
                     "Config": {"User": "10001:10001", "Entrypoint": ["/opt/war3/entrypoint.sh"],
                                "Env": ["ONE=1"], "Healthcheck": {"Retries": 3},
                                "Labels": {"war3.production.source-digest": self.digest}},
                     "RootFS": {"Type": "layers", "Layers": ["sha256:" + "e" * 64]},
                     "RepoDigests": [PULL]}
        self.identity = self.root / "trusted-release.json"
        self.fixture_redist_hash = hashlib.sha256(self.redist.read_bytes()).hexdigest()
        self.addCleanup(patch.stopall)
        patch.object(boot, "ROOT", self.repo).start()
        # Success fixtures are deliberately not real redistributables. A separate test
        # proves the production constant is the exact runtime pin.
        patch.object(boot, "REDIST_SHA256", self.fixture_redist_hash).start()
        self.docker = patch.object(production, "docker_json", side_effect=self.inspect).start()
        self.analysis = {"format": 25, "flags": 32, "lobby_name": "Chosen Map",
                         "slots": [[0, 255, 0, 0, i // 2, i, 32, 1, 100] for i in range(4)],
                         "human_capacity": 4, "map_sha256": hashlib.sha256(self.map.read_bytes()).hexdigest(),
                         "script_sha256": "c" * 64, "ai_evidence": {"level": "none", "features": []}}
        self.stub = self.root / "docker_stub.py"
        self.stub.write_text('''import json, sys, time
from pathlib import Path
root = Path(__file__).parent
with (root / "docker_calls.jsonl").open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "rm":
    raise SystemExit(0)
mode = (root / "analyzer_mode").read_text()
if mode == "timeout":
    time.sleep(60)
if mode == "closed-timeout":
    import os
    os.close(1)
    time.sleep(60)
if mode == "oversize":
    sys.stdout.write("x" * 65537)
elif mode == "malformed":
    sys.stdout.write("not JSON")
elif mode == "duplicate":
    sys.stdout.write('{"map_sha256":"a","map_sha256":"b"}')
elif mode == "failure":
    print("DO_NOT_ECHO_ANALYZER_DIAGNOSTIC", file=sys.stderr)
    raise SystemExit(1)
elif mode == "real":
    from src.runtime.presets import analyze_map
    mount = sys.argv[sys.argv.index("--mount") + 1]
    selected = mount.split("src=", 1)[1].split(",dst=", 1)[0]
    print(json.dumps(analyze_map(Path(selected))))
else:
    sys.stdout.buffer.write((root / "analysis.json").read_bytes())
''')
        self.mode = self.root / "analyzer_mode"
        self.mode.write_text("success")
        patch.object(production, "DOCKER", [sys.executable, str(self.stub)]).start()
        analyze = boot.analyze_selected_map
        def run_analysis(*args):
            write_json(self.root / "analysis.json", self.analysis)
            return analyze(*args)
        self.analyzer = patch.object(boot, "analyze_selected_map", side_effect=run_analysis).start()

    def inspect(self, args):
        self.assertEqual(args[:2], ["image", "inspect"])
        self.assertEqual(len(args), 3)
        self.assertIn(args[2], (APPROVED, CLASSIC, PULL))
        return [copy.deepcopy(self.meta)]

    def export(self):
        return boot.export_release_identity(APPROVED, self.gate, self.identity, PULL)

    def setup(self, **extra):
        return boot.setup(self.destination, self.star, self.game, self.map,
                          **{"gate_path": self.gate, **extra})

    def assert_clean(self):
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".war3-bootstrap-*")), [])

    def pin_curated(self):
        example = json.loads((REPO / "config/presets.example.json").read_text())
        self.analysis["slots"] = [[0, 255, 0, 0, i // 3, i, 32, 1, 100] for i in range(12)]
        self.analysis["human_capacity"] = 12
        self.analysis["ai_evidence"] = {"level": "candidate", "features": ["ai_start", "computer_control"]}
        proof = example["capabilities"][0]
        proof.update(map_sha256=self.analysis["map_sha256"], script_sha256=self.analysis["script_sha256"])
        self.pin_example(example)
        return example

    def pin_example(self, example):
        path = self.repo / "config/presets.example.json"
        write_json(path, example)
        self.accepted["source_sha256"]["config/presets.example.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_json(self.gate, self.accepted)
        self.digest = production.gate(self.accepted, APPROVED)
        self.meta["Config"]["Labels"]["war3.production.source-digest"] = self.digest

    def generated(self):
        return production.read_json(self.destination / "data/config/presets.json")

    def analyzer_calls(self):
        return [json.loads(line) for line in (self.root / "docker_calls.jsonl").read_text().splitlines()]

    def test_success_copies_exact_narrow_sources_and_preserves_originals(self):
        originals = {p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
                     for root in (self.star, self.game) for p in root.rglob("*") if p.is_file()}
        originals[self.map] = (self.map.read_bytes(), stat.S_IMODE(self.map.stat().st_mode), self.map.stat().st_mtime_ns)
        allowed = {self.star / n for n in boot.VENDOR_PATHS} | {self.redist, self.map} | {
            self.game / n for n in ("war3.exe", "game.dll", "War3Patch.mpq")}
        opened = set()
        original_open = boot.open_source

        def check_open(path):
            path = Path(path)
            if path.is_relative_to(self.star) or path.is_relative_to(self.game) or path == self.map:
                self.assertIn(path, allowed)
                opened.add(path)
            return original_open(path)

        with patch.object(boot, "open_source", side_effect=check_open):
            self.setup()
        self.assertEqual(opened, allowed)
        for path, before in originals.items():
            self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns), before)
        app = self.destination
        actual_star = {p.relative_to(app / "vendor/star").as_posix()
                       for p in (app / "vendor/star").rglob("*") if p.is_file()}
        self.assertEqual(actual_star, boot.VENDOR_PATHS)
        self.assertEqual({p.name for p in (app / "vendor/game").iterdir()}, {"war3.exe", "game.dll"})
        self.assertEqual({p.name for p in (app / "data/warcraft").iterdir()}, {"War3Patch.mpq"})
        self.assertEqual((app / "data/maps/selected.w3x").read_bytes(), self.map.read_bytes())
        self.assertEqual(list((app / "vendor/profile").iterdir()), [])
        entry = self.generated()["presets"][0]
        self.assertEqual(entry["id"], "selected-map")
        self.assertEqual(entry["map_filename"], "selected.w3x")
        self.assertEqual(entry["lobby_name"], "Chosen Map")
        self.assertEqual(entry["max_players"], 4)
        self.assertFalse(entry["ai"]["enabled"])
        self.assertFalse(entry["start"]["autostart_enabled"])
        self.assertEqual(entry["start"]["autostart_players"], 4)
        spec = production.deployment(app)
        self.assertEqual(spec["host_instance_dir"], str(app))
        self.assertEqual(spec["image_id"], APPROVED)
        self.assertEqual(production.read_json(app / "acceptance.json"), self.accepted)
        settings = production.read_json(app / "data/config/amp-settings.json")
        self.assertEqual(settings["host_name"], "War3HostBot")
        self.assertEqual(settings["map_preset"], entry["id"])
        self.assertEqual(boot.validate_startup_config(app / "data/config/supervisor.json")["gameplay"]["map_preset"], entry["id"])
        manifest = json.loads((app / production.PRESET_MANIFEST).read_text())
        selector = next(f for f in manifest if f.get("ParamFieldName") == "map_preset")
        self.assertEqual(set(selector["EnumValues"]), {entry["id"]})
        self.assertEqual((app / "data/config/ghost.cfg").read_text(), boot.NATIVE_CONFIG)
        self.assertIn("map_localpath = selected.w3x", (app / "data/config/maps/selected.cfg").read_text())
        for path in (app, *app.rglob("*")):
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertEqual(path.stat().st_gid, os.getgid())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertEqual(self.docker.call_count, 1)
        calls = self.analyzer_calls()
        self.assertEqual(len(calls), 2)
        run = calls[0]
        for flag, value in (("--network", "none"), ("--memory", "256m"), ("--memory-swap", "256m"),
                            ("--pids-limit", "32"), ("--cpus", "1"), ("--entrypoint", "python3"),
                            ("--user", f"{os.getuid()}:{os.getgid()}"), ("--log-driver", "none"),
                            ("--workdir", "/")):
            self.assertEqual(run[run.index(flag) + 1], value)
        self.assertIn("--pull=never", run)
        self.assertIn("--read-only", run)
        self.assertIn("--no-healthcheck", run)
        self.assertIn("no-new-privileges:true", run)
        self.assertNotIn("--publish", run)
        self.assertEqual(run.count("--mount"), 1)
        self.assertRegex(run[run.index("--mount") + 1], r"/app/data/maps/selected\.w3x,dst=/selected\.w3x,readonly$")
        self.assertEqual(run[-4:], [APPROVED, "-m", "src.runtime.presets", "/selected.w3x"])
        self.assertEqual(calls[1], ["rm", "--force", run[run.index("--name") + 1]])

    def test_classic_id_requires_exact_complete_identity_and_keeps_original_approval(self):
        identity = self.export()
        self.meta["Id"] = CLASSIC
        self.setup(gate_path=None, release_identity=self.identity, image=CLASSIC)
        localized = production.read_json(self.destination / "acceptance.json")
        self.assertEqual(localized, {**self.accepted, "image_id": CLASSIC})
        receipt = production.read_json(self.destination / "release-correlation.json")
        self.assertEqual(receipt["original_gate"], self.accepted)
        self.assertEqual(receipt["local_image_id"], CLASSIC)
        self.assertEqual(receipt["source_digest"], self.digest)
        self.assertEqual(len(receipt["original_gate"]["checks"]), 12)
        self.assertEqual(production.read_json(self.identity), identity)
        with self.assertRaises(ValueError):
            production.gate(self.accepted, CLASSIC)
        self.assertEqual(production.gate(localized, CLASSIC), self.digest)
        self.assertEqual(self.analyzer_calls()[0][-4:], [CLASSIC, "-m", "src.runtime.presets", "/selected.w3x"])

    def test_exact_gate_cannot_translate_even_equivalent_classic_id(self):
        self.meta["Id"] = CLASSIC
        with patch.object(boot, "source_digest") as digest, self.assertRaises(ValueError):
            self.setup(image=CLASSIC)
        digest.assert_not_called()
        self.assert_clean()

    def test_complete_config_layers_platform_and_label_each_fail_before_asset_reads(self):
        self.export()
        original = copy.deepcopy(self.meta)
        mutations = [lambda m: m["Config"].update(User="0"),
                     lambda m: m["Config"]["Env"].append("NEW=1"),
                     lambda m: m["Config"]["Healthcheck"].update(Retries=True),
                     lambda m: m["Config"].update(NewField="untrusted"),
                     lambda m: m["RootFS"]["Layers"].append("sha256:" + "f" * 64),
                     lambda m: m.update(Architecture="arm64"),
                     lambda m: m.update(Os="windows"), lambda m: m.update(Variant="v8"),
                     lambda m: m["Config"]["Labels"].update({"war3.production.source-digest": "0" * 64})]
        for mutate in mutations:
            self.meta = copy.deepcopy(original)
            mutate(self.meta)
            with self.subTest(mutate=mutate), patch.object(boot, "source_digest") as digest:
                with self.assertRaises(ValueError):
                    self.setup(gate_path=None, release_identity=self.identity)
                digest.assert_not_called()
                self.assert_clean()

    def test_unknown_incomplete_or_unapproved_release_identity_never_approves_itself(self):
        good = self.export()
        mutations = [lambda x: x.update(schema_version=2), lambda x: x.update(extra=True),
                     lambda x: x["gate"].update(approved_by_main=False),
                     lambda x: x["gate"]["checks"].update(unit="PENDING"),
                     lambda x: x["gate"]["checks"].pop("offline_soak"),
                     lambda x: x.update(source_digest="0" * 64),
                     lambda x: x["gate"]["source_sha256"].update({"new.py": "1" * 64}),
                     lambda x: x.update(pull_ref="ghcr.io/example/war3:latest"),
                     lambda x: x.update(gate={"result": "BUILT"})]
        for mutate in mutations:
            value = copy.deepcopy(good)
            mutate(value)
            write_json(self.identity, value)
            with self.subTest(mutate=mutate), patch.object(boot, "source_digest") as digest:
                with self.assertRaises(ValueError):
                    self.setup(gate_path=None, release_identity=self.identity)
                digest.assert_not_called()
                self.assert_clean()

    def test_export_requires_exact_gate_id_source_label_and_correlated_pull_ref(self):
        self.meta["Id"] = CLASSIC
        with self.assertRaises(ValueError):
            self.export()
        self.meta["Id"] = APPROVED
        self.meta["Config"]["Labels"]["war3.production.source-digest"] = "0" * 64
        with self.assertRaises(ValueError):
            self.export()
        self.meta["Config"]["Labels"]["war3.production.source-digest"] = self.digest
        self.meta["RepoDigests"] = []
        with self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.identity.exists())
        value = boot.export_release_identity(APPROVED, self.gate, self.identity)
        self.assertNotIn("pull_ref", value)
        before = self.identity.read_bytes()
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(self.identity.read_bytes(), before)

    def test_bad_vendor_manifest_vc_or_missing_source_fails_before_prepare(self):
        original = self.manifest.read_bytes()
        cases = ("hash", "traversal", "duplicate", "missing-row", "size", "unapproved-manifest", "vc", "missing-game")
        for case in cases:
            self.manifest.write_bytes(original)
            value = json.loads(original)
            if case == "hash":
                value["files"][0]["sha256"] = "0" * 64
            elif case == "traversal":
                value["files"][0]["path"] = "../launcher.ini"
            elif case == "duplicate":
                value["files"][0] = value["files"][1]
            elif case == "missing-row":
                value["files"].pop()
            elif case == "size":
                value["files"][0]["bytes"] = True
            elif case == "vc":
                self.redist.write_bytes(b"incorrect redistributable")
            elif case == "missing-game":
                (self.game / "game.dll").unlink()
            write_json(self.manifest, value)
            gate = copy.deepcopy(self.accepted)
            if case != "unapproved-manifest":
                gate["source_sha256"]["config/star-vendor-files.json"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
            else:
                gate["source_sha256"]["config/star-vendor-files.json"] = "0" * 64
            write_json(self.gate, gate)
            self.meta["Config"]["Labels"]["war3.production.source-digest"] = production.gate(gate, APPROVED)
            with self.subTest(case=case), patch.object(production, "prepare") as prepare:
                with self.assertRaises((ValueError, OSError)):
                    self.setup()
                prepare.assert_not_called()
                self.assert_clean()
            self.redist.write_bytes(b"SYNTHETIC_NOT_PE VC2015 fixture")

    def test_existing_destinations_even_empty_or_symlinked_are_untouched(self):
        for kind in ("empty", "nonempty", "symlink", "file"):
            with self.subTest(kind=kind):
                if kind in ("empty", "nonempty"):
                    self.destination.mkdir(mode=0o700)
                    if kind == "nonempty":
                        (self.destination / "keep").write_text("keep")
                elif kind == "symlink":
                    self.destination.symlink_to(self.game, target_is_directory=True)
                else:
                    self.destination.write_text("keep")
                self.docker.reset_mock()
                with self.assertRaises(ValueError):
                    self.setup()
                self.docker.assert_not_called()
                if kind == "nonempty":
                    self.assertEqual((self.destination / "keep").read_text(), "keep")
                if self.destination.is_dir() and not self.destination.is_symlink():
                    if kind == "nonempty":
                        (self.destination / "keep").unlink()
                    self.destination.rmdir()
                else:
                    self.destination.unlink()

    def test_symlink_ancestors_asset_links_hardlinks_and_path_overlap_rejected(self):
        linked = self.root / "linked"
        linked.symlink_to(self.star, target_is_directory=True)
        with self.assertRaises(ValueError):
            boot.setup(self.destination, linked, self.game, self.map, gate_path=self.gate)
        resources = self.star / "resources"
        resources.rename(self.star / "real-resources")
        resources.symlink_to(self.star / "real-resources", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.setup()
        self.assert_clean()

    def test_source_parent_symlink_race_cannot_escape_provided_tree(self):
        resources = self.star / "resources"
        outside = self.root / "unprovided"
        outside.mkdir(mode=0o700)
        name = "qtwebengine_resources.pak"
        (outside / name).write_bytes(b"must never read outside the provided Star tree")
        real_open = os.open
        raced = False

        def race_open(path, flags, *args, **kwargs):
            nonlocal raced
            if path == "resources" and not raced:
                raced = True
                resources.rename(self.star / "moved-resources")
                resources.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(os, "open", side_effect=race_open), self.assertRaises(OSError):
            boot.source_digest(resources / name)
        self.assertTrue(raced)
        self.assert_clean()

    def test_unapproved_config_sources_rejected_before_asset_copies(self):
        for name in ("src/supervisor/config.py", "src/runtime/config.py"):
            path = self.repo / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.subTest(name=name), patch.object(boot, "private_copy") as copy_file:
                with self.assertRaisesRegex(ValueError, "approved release"):
                    self.setup()
                copy_file.assert_not_called()
                self.assert_clean()
            path.write_bytes(original)

    def test_display_name_example_change_is_merged_without_weakening_image_gate(self):
        path = self.repo / "config/supervisor-iccup.example.json"
        example = production.read_json(path)
        example["gameplay"]["host_name"] = "Earlier Default"
        write_json(path, example)
        self.setup()
        config = boot.validate_startup_config(self.destination / "data/config/supervisor.json")
        self.assertEqual(config["gameplay"]["host_name"], "War3HostBot")
        self.assertEqual(production.read_json(self.destination / "acceptance.json"), self.accepted)

    def test_wrong_final_runtime_paths_fail_before_asset_writes(self):
        original = production.prepare
        for field in ("command", "cwd", "prefix", "mode"):
            def bad_config(*args):
                original(*args)
                path = args[0] / "data/config/supervisor.json"
                config = production.read_json(path)
                if field == "command":
                    config["hostbot"]["command"] = ["/opt/war3/bin/ghost++", "/data/config/ghost.cfg"]
                elif field == "cwd":
                    config["hostbot"]["cwd"] = "/tmp"
                elif field == "prefix":
                    config["iccup"]["prefix"] = "/unmounted/prefix"
                else:
                    config["mode"] = "offline"
                production.private_json(path, config)
            with self.subTest(field=field), patch.object(production, "prepare", side_effect=bad_config), patch.object(boot, "private_copy") as copy_file:
                with self.assertRaisesRegex(ValueError, "mounts/startup"):
                    self.setup()
                copy_file.assert_not_called()
                self.assert_clean()

    def test_generated_configs_compile_without_starting_native_wine_or_network(self):
        from src.runtime.native import prepare_native

        self.setup()
        app = self.destination
        config = boot.validate_startup_config(app / "data/config/supervisor.json")
        self.assertEqual(config["gameplay"]["host_name"], "War3HostBot")
        # Redirect only writable/static-compiler paths into fixtures; retain the
        # generated container paths for the actual map and patch mounts.
        config["hostbot"]["cwd"] = str(app / "data")
        config["runtime_dir"] = str(app / "data/state/supervisor")
        config["iccup"]["native_config"] = str(app / "data/config/ghost.cfg")
        config["iccup"]["bridge_dir"] = str(app / "data/state/bridge")
        with patch.object(subprocess, "Popen", side_effect=AssertionError("no child execution")), patch("socket.socket", side_effect=AssertionError("no network")), patch("src.runtime.presets.analyze_map", return_value=self.analysis):
            effective = prepare_native(config, "SyntheticFixture", publish=False)
        native = dict(line.split(" = ", 1) for line in effective.read_text().splitlines())
        self.assertEqual(native["bot_war3path"], "/data/warcraft/")
        self.assertEqual(native["bot_mappath"], str(app / "data/maps") + "/")
        self.assertEqual(native["bot_defaultmap"], "preset.cfg")
        self.assertEqual(native["udp_broadcasttarget"], "127.0.0.1")
        self.assertEqual(native["lan_war3version"], "26")
        self.assertEqual(native["bnet_server"], "")
        self.assertEqual(native["bot_lobby_ai_supported"], "0")
        self.assertEqual(native["bot_autostart_enabled"], "0")
        self.assertEqual(native["autohost_gamename"], "Chosen Map")
        self.assertIn("map_localpath = selected.w3x", (Path(native["bot_mapcfgpath"]) / "preset.cfg").read_text())

    def test_path_defenses(self):
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        for destination in (linked / "new", self.root / "x/../new", self.root / "bad,name", self.star / "new"):
            with self.subTest(destination=destination), self.assertRaises((ValueError, OSError)):
                boot.setup(destination, self.star, self.game, self.map, gate_path=self.gate)
        self.assert_clean()
        for path in (self.star / "Qt5Core.dll", self.star / "resources/qtwebengine_resources.pak", self.game / "game.dll"):
            content = path.read_bytes()
            path.unlink()
            path.symlink_to(self.map)
            with self.assertRaises(ValueError):
                self.setup()
            self.assert_clean()
            path.unlink()
            path.write_bytes(content)
        target = self.game / "game.dll"
        alias = self.root / "alias.dll"
        os.link(target, alias)
        with self.assertRaises(ValueError):
            self.setup()
        self.assert_clean()

    def test_copy_failure_or_changed_input_removes_only_private_stage(self):
        copy_file = boot.private_copy
        count = 0

        def fail_copy(*args):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("synthetic disk error")
            return copy_file(*args)

        with patch.object(boot, "private_copy", side_effect=fail_copy), self.assertRaises(OSError):
            self.setup()
        self.assert_clean()
        def changed_copy(source, target, expected):
            if source == self.map:
                self.map.write_bytes(b"changed source fixture")
            return copy_file(source, target, expected)
        with patch.object(boot, "private_copy", side_effect=changed_copy), self.assertRaises(ValueError):
            self.setup()
        self.assert_clean()

    def test_prepare_failure_and_publish_race_leave_no_partial_destination(self):
        with patch.object(production, "merge_settings", side_effect=ValueError("invalid")), self.assertRaises(ValueError):
            self.setup()
        self.assert_clean()
        rename = boot.rename_new
        def raced(source, destination):
            destination.mkdir(mode=0o700)
            return rename(source, destination)
        with patch.object(boot, "rename_new", side_effect=raced), self.assertRaises(OSError):
            self.setup()
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(list(self.root.glob(".war3-bootstrap-*")), [])

    def test_optional_presets_and_account_prepare_extension_are_preserved(self):
        presets = self.pin_curated()
        prepare = production.prepare
        def extended(*args):
            prepare(*args)
            app = args[0]
            production.private_json(app / "control/account-input.json", {})
            (app / "control/account_setup.py").write_text("# account agent helper\n")
        with patch.object(production, "prepare", side_effect=extended):
            self.setup()
        expected = next(p for p in presets["presets"] if p["id"] == "footman-ultimate-ai")
        expected = {**expected, "map_filename": "selected.w3x"}
        self.assertEqual(self.generated(), {"version": 1, "presets": [expected],
                                           "capabilities": presets["capabilities"]})
        self.assertEqual(production.read_json(self.destination / "control/account-input.json"), {})
        self.assertTrue((self.destination / "control/account_setup.py").is_file())

    def test_curated_preset_preserves_proof_and_compiles_ai_with_the_selected_basename(self):
        from src.runtime.native import prepare_native

        example = self.pin_curated()
        self.setup()
        document = self.generated()
        self.assertEqual(len(document["presets"]), 1)
        self.assertEqual(document["capabilities"], example["capabilities"])
        self.assertFalse(document["capabilities"][0]["runtime_tested"])
        self.assertTrue(document["presets"][0]["ai"]["enabled"])
        self.assertEqual(production.read_json(self.destination / "acceptance.json"), self.accepted)
        config = boot.validate_startup_config(self.destination / "data/config/supervisor.json")
        self.assertEqual(config["gameplay"]["map_preset"], "footman-ultimate-ai")
        config["hostbot"]["cwd"] = str(self.destination / "data")
        config["runtime_dir"] = str(self.destination / "data/state/supervisor")
        config["iccup"]["native_config"] = str(self.destination / "data/config/ghost.cfg")
        config["iccup"]["bridge_dir"] = str(self.destination / "data/state/bridge")
        with patch("src.runtime.presets.analyze_map", return_value=self.analysis), patch("socket.socket", side_effect=AssertionError("no network")), patch.object(subprocess, "Popen", side_effect=AssertionError("no engine")):
            effective = prepare_native(config, "SyntheticFixture", publish=False)
        native = dict(line.split(" = ", 1) for line in effective.read_text().splitlines())
        self.assertEqual(native["bot_lobby_ai_target"], "4")
        self.assertEqual(native["bot_lobby_ai_supported"], "1")
        self.assertEqual(native["bot_vote_min_players"], "2")
        self.assertEqual(native["bot_autostartplayers"], "4")
        self.assertEqual(native["autohost_gamename"], "Footman Ultimate AI")
        self.assertIn("map_localpath = selected.w3x", (Path(native["bot_mapcfgpath"]) / "preset.cfg").read_text())

    def test_candidate_or_any_unbound_curated_evidence_stays_ai_off(self):
        original = self.pin_curated()
        for mismatch in ("map", "script", "features", "example-pin", "missing-pin", "other-id"):
            example = copy.deepcopy(original)
            self.analysis["ai_evidence"]["features"] = ["ai_start", "computer_control"]
            if mismatch == "map":
                example["capabilities"][0]["map_sha256"] = "0" * 64
            elif mismatch == "script":
                example["capabilities"][0]["script_sha256"] = "0" * 64
            elif mismatch == "features":
                self.analysis["ai_evidence"]["features"] = ["ai_start"]
            elif mismatch == "other-id":
                example["presets"][2]["id"] = "another-ai-map"
            self.pin_example(example)
            if mismatch == "example-pin":
                path = self.repo / "config/presets.example.json"
                path.write_bytes(path.read_bytes() + b"\n")
            elif mismatch == "missing-pin":
                del self.accepted["source_sha256"]["config/presets.example.json"]
                write_json(self.gate, self.accepted)
                self.meta["Config"]["Labels"]["war3.production.source-digest"] = production.gate(self.accepted, APPROVED)
            self.destination = self.root / ("unknown-" + mismatch)
            with self.subTest(mismatch=mismatch):
                self.setup()
                records = self.generated()["capabilities"]
                if mismatch == "features":
                    self.assertEqual(records, [])
                    entry = self.generated()["presets"][0]
                    self.assertFalse(entry["ai"]["enabled"])
                    continue
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["review"], "automatic static evidence; no runtime validation")
                self.assertFalse(records[0]["runtime_tested"])
                entry = self.generated()["presets"][0]
                self.assertEqual(entry["id"], "selected-map")
                self.assertEqual(entry["lobby_name"], self.analysis["lobby_name"])
                self.assertFalse(entry["ai"]["enabled"])
                self.assertFalse(entry["start"]["autostart_enabled"])

    def test_real_mpq_analysis_sets_title_capacity_and_conservative_thresholds(self):
        from test_map_presets import MPQTests, PLAIN_SCRIPT, info_fixture

        fixture = MPQTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.mpq(members={
                "war3map.w3i": info_fixture(title=b"TRIGSTR_7", statuses=[1, 1, 1, 2, 2]),
                "war3map.wts": b"STRING 7\n{\n|cffff0000Real Map Title|r\n}\n",
                "war3map.j": PLAIN_SCRIPT})
        self.map.write_bytes(source.read_bytes())
        self.mode.write_text("real")
        self.setup()
        entry = self.generated()["presets"][0]
        self.assertEqual(entry["lobby_name"], "Real Map Title")
        self.assertEqual(entry["max_players"], 5)  # Preserve the map's two predefined computers.
        self.assertEqual(entry["start"]["autostart_players"], 3)
        self.assertEqual(entry["vote"]["min_players"], 2)
        self.assertFalse(entry["start"]["autostart_enabled"])
        self.assertFalse(entry["ai"]["enabled"])

    def test_bad_analyzer_metadata_or_wrong_map_hash_never_publishes(self):
        original = copy.deepcopy(self.analysis)
        changes = ({"map_sha256": "0" * 64}, {"script_sha256": "bad"}, {"human_capacity": True},
                   {"human_capacity": 1}, {"slots": [[0] * 9] * 4}, {"lobby_name": "unsafe\nname"},
                   {"ai_evidence": {"features": ["unknown"]}}, {"slots": []})
        for change in changes:
            self.analysis = {**original, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.setup()
            self.assert_clean()

    def test_analyzer_timeout_bad_json_oversize_and_failure_clean_up_own_container(self):
        for mode in ("timeout", "closed-timeout", "oversize", "malformed", "duplicate", "failure"):
            self.mode.write_text(mode)
            with self.subTest(mode=mode), patch.object(boot, "ANALYSIS_TIMEOUT", 0.2), self.assertRaises(ValueError) as error:
                self.setup()
            self.assertNotIn("DO_NOT_ECHO_ANALYZER_DIAGNOSTIC", str(error.exception))
            self.assert_clean()
            calls = self.analyzer_calls()
            self.assertEqual(calls[-1], ["rm", "--force", calls[-2][calls[-2].index("--name") + 1]])
            self.assertRegex(calls[-1][-1], r"^war3-map-analysis-[0-9a-f]{32}$")

    def test_cli_noninteractive_and_interactive_prompts_need_only_paths(self):
        args = ["--noninteractive", "--gate", str(self.gate), "--instance-dir", str(self.destination),
                "--star-dir", str(self.star), "--game-dir", str(self.game), "--map", str(self.map)]
        with patch("builtins.input", side_effect=AssertionError("CI cannot prompt")), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(boot.cli(args), 0)
        other = self.root / "interactive"
        with patch("builtins.input", side_effect=[str(other), str(self.star), str(self.game), str(self.map), ""]), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(boot.cli(["--gate", str(self.gate)]), 0)
        stderr = io.StringIO()
        with patch("builtins.input", side_effect=AssertionError("CI cannot prompt")), patch("sys.stderr", new=stderr), self.assertRaises(SystemExit) as error:
            boot.cli(["--noninteractive", "--gate", str(self.gate)])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("missing required path: --instance-dir", stderr.getvalue())

    def test_fresh_setup_cli_process_prepares_selected_preset_without_amp_path(self):
        """No earlier account import or test-runner sys.path can hide packaging errors."""
        write_json(self.root / "inspect.json", self.meta)
        write_json(self.root / "analysis.json", self.analysis)
        code = '''import json, runpy, sys
from pathlib import Path
from amp import bootstrap, production
root = Path(sys.argv[1])
bootstrap.ROOT = root / "repo"
bootstrap.REDIST_SHA256 = sys.argv[2]
production.DOCKER = [sys.executable, str(root / "docker_stub.py")]
production.docker_json = lambda args: [json.loads((root / "inspect.json").read_text())]
script = sys.argv[3]
sys.argv = [script, *sys.argv[4:]]
runpy.run_path(script, run_name="__main__")
'''
        args = [sys.executable, "-c", code, str(self.root), self.fixture_redist_hash,
                str(REPO / "scripts/setup-production-instance.py"), "--noninteractive", "--gate", str(self.gate),
                "--instance-dir", str(self.destination), "--star-dir", str(self.star),
                "--game-dir", str(self.game), "--map", str(self.map)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=15,
                                env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.generated()["presets"][0]["id"], "selected-map")
        self.assertEqual(production.read_json(self.destination / "data/config/amp-settings.json")["map_preset"], "selected-map")

    def test_root_or_unsafe_parent_and_missing_local_image_fail_before_prepare(self):
        with patch.object(os, "getuid", return_value=0), self.assertRaises(ValueError):
            self.setup()
        original_mode = stat.S_IMODE(self.root.stat().st_mode)
        self.root.chmod(0o777)
        try:
            with self.assertRaises(ValueError):
                self.setup()
        finally:
            self.root.chmod(original_mode)
        with patch.object(production, "docker_json", side_effect=subprocess.CalledProcessError(1, "docker")), patch.object(production, "prepare") as prepare:
            with self.assertRaises(subprocess.CalledProcessError):
                self.setup()
            prepare.assert_not_called()
        self.assert_clean()


class PublicContractTests(unittest.TestCase):
    def test_manifest_allowlist_and_production_vc_pin_match_runtime(self):
        from src.runtime.star import VENDOR_NAMES, REDIST_SHA256
        self.assertEqual(boot.VENDOR_PATHS, VENDOR_NAMES)
        self.assertEqual(len(boot.VENDOR_PATHS), 31)
        self.assertEqual(boot.REDIST_SHA256, REDIST_SHA256)
        self.assertEqual(boot.REDIST_SHA256, "fdd1e1f0dcae2d0aa0720895eff33b927d13076e64464bb7c7e5843b7667cd14")

    def test_entrypoints_help_and_no_credentials_arguments(self):
        for script in ("setup-production-instance.py", "export-release-identity.py"):
            result = subprocess.run([sys.executable, str(REPO / "scripts" / script), "--help"],
                                    check=True, capture_output=True, text=True, timeout=10)
            self.assertNotIn("--password", result.stdout)
            self.assertNotIn("--profile", result.stdout)


if __name__ == "__main__":
    unittest.main()
