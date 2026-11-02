"""Offline infrastructure security and persistence regression. Run in local Docker."""
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "amp"))
import production
import production_backup


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


context = load("production_context", "docker/production/prepare_context.py")
release = load("production_release", "docker/production/release.py")
build = load("production_build", "scripts/build-production-image.py")
IMAGE = "sha256:" + "1" * 64


def accepted():
    return {"schema_version": 1, "image_id": IMAGE, "approved_by_main": True,
            "source_sha256": {"src/runtime/star.py": "a" * 64},
            "checks": {key: "PASS" for key in production.CHECKS}}


class ContextTests(unittest.TestCase):
    def tree(self, root):
        for name in context.FILES:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}\n" if name.endswith(".json") else "# safe text\n")
        for name in context.TREES:
            (root / name).mkdir(parents=True, exist_ok=True)
            filename = "helper.cpp" if name == "src/hostbot" else "__init__.py"
            (root / name / filename).write_text("// source\n" if name == "src/hostbot" else "# source\n")

    def test_required_new_runtime_and_manifest_are_never_silently_omitted(self):
        for missing in ("src/runtime/__init__.py", "config/star-vendor-files.json",
                        "config/presets.example.json", "config/presets.schema.json"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "repo"
                self.tree(root)
                (root / missing).unlink()
                with self.assertRaises(ValueError):
                    context.prepare(root, Path(tmp) / "context")

    def test_context_includes_nested_runtime_sources_and_public_vendor_hashes_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, output = Path(tmp) / "repo", Path(tmp) / "context"
            self.tree(root)
            for name in ("src/runtime/star.py", "src/runtime/workers/continuous.py"):
                target = root / name
                target.parent.mkdir(exist_ok=True)
                target.write_text("pass\n")
            for name in ("vendor/Launcher.exe", "private/launcher.ini", "config/supervisor.json", "src/runtime/cached.dll"):
                target = root / name
                target.parent.mkdir(exist_ok=True)
                target.write_text("not an image input")
            hashes = context.prepare(root, output)
            self.assertIn("src/runtime/workers/continuous.py", hashes)
            self.assertIn("config/star-vendor-files.json", hashes)
            self.assertIn("config/presets.example.json", hashes)
            self.assertIn("config/presets.schema.json", hashes)
            self.assertFalse(any("private" in name or name.endswith((".exe", ".dll")) for name in hashes))
            self.assertEqual(json.loads((output / "production-source-sha256.json").read_text()), hashes)

    def test_source_symlink_binary_and_parent_symlink_rejected_without_partial_copy(self):
        for kind in ("link", "binary", "parent_link"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root, output = Path(tmp) / "repo", Path(tmp) / "context"
                self.tree(root)
                target = root / "src/runtime/star.py"
                if kind == "link":
                    target.symlink_to(root / "docker/healthcheck.py")
                elif kind == "binary":
                    target.write_bytes(b"MZ\0PE")
                else:
                    (root / "config").rename(root / "elsewhere")
                    (root / "config").symlink_to(root / "elsewhere")
                with self.assertRaises(ValueError):
                    context.prepare(root, output)
                self.assertFalse(output.exists())


class SettingsTests(unittest.TestCase):
    def test_amp_string_types_normalize_to_validated_gameplay_defaults(self):
        raw = {k: (str(v).lower() if type(v) is bool else str(v)) for k, v in production.DEFAULTS.items()}
        self.assertEqual(production.settings(raw), {**production.DEFAULTS, "map_cycle": ["manual"], "max_concurrent_matches": 3})

    def test_amp_cycle_takes_precedence_after_ui_updates_legacy_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("control", "data", "data/config"):
                (root / name).mkdir(mode=0o700)
            path = root / "data/config/amp-settings.json"
            config = root / "data/config/supervisor.json"
            legacy = {**production.DEFAULTS, "map_preset": "manual", "game_name": "Keep This"}
            production.private_json(path, legacy)
            production.private_json(config, {"http": {"host": "127.0.0.1", "port": 8080}})
            production.private_json(root / production.PRESETS, {"version": 1, "presets": [
                {"id": "manual", "enabled": True},
                {"id": "new-map", "enabled": True, "map_filename": "new.w3x"}]})
            # AMP adds the textarea to the old JSON while preserving map_preset.
            ui_saved = {**legacy, "map_cycle": '["new-map"]', "max_concurrent_matches": "3"}
            production.private_json(path, ui_saved)
            original = path.read_bytes()
            for _ in range(2):
                validated = production.validate_preset_selection(root)
                self.assertEqual(validated["map_cycle"], ["new-map"])
                production.merge_settings(config, path, validated=validated)
                self.assertEqual(production.read_json(config)["gameplay"]["map_cycle"], ["new-map"])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(production.read_json(path), ui_saved)

    def test_invalid_port_thresholds_names_and_unknown_fields_rejected(self):
        cases = [{"game_port": 6112}, {"autostart_players": 13}, {"max_players": 4},
                 {"vote_min_players": 1}, {"vote_required_percent": 101}, {"vote_timeout": -1},
                 {"lobby_refresh_interval": 9}, {"lobby_refresh_mode": "rehost"},
                 {"game_name": "injected\npassword=secret"}, {"vote_enabled": 1}, {"extra": True}]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                production.settings({**production.DEFAULTS, **change})

    def test_atomic_merge_preserves_runtime_config_and_never_writes_native_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "control").mkdir(mode=0o700)
            config = {"mode": "offline", "http": {"host": "127.0.0.1", "port": 8080},
                      "runtime": {"generation": 7}, "hostbot": {"cwd": "/data"}}
            production.private_json(root / "supervisor.json", config)
            production.private_json(root / "settings.json", production.DEFAULTS)
            production.merge_settings(root / "supervisor.json", root / "settings.json")
            saved = json.loads((root / "supervisor.json").read_text())
            self.assertEqual(saved["runtime"], config["runtime"])
            self.assertEqual(saved["gameplay"]["autostart_players"], 10)
            self.assertIs(saved["gameplay"]["autostart_enabled"], True)
            self.assertEqual(saved["gameplay"]["max_players"], 12)
            self.assertEqual(saved["gameplay"]["map_cycle"], ["manual"])
            self.assertEqual(saved["gameplay"]["lobby_refresh_interval"], 10)
            self.assertNotIn("game_port", saved["gameplay"])
            self.assertEqual((root / "supervisor.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(root.glob(".war3-json-*")), [])
            config["http"]["host"] = "0.0.0.0"
            production.private_json(root / "supervisor.json", config)
            before = (root / "supervisor.json").read_bytes()
            with self.assertRaises(ValueError):
                production.merge_settings(root / "supervisor.json", root / "settings.json")
            self.assertEqual((root / "supervisor.json").read_bytes(), before)

    def test_failed_validation_does_not_replace_existing_config_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "original").write_text("keep")
            (root / "link").symlink_to(root / "original")
            with self.assertRaises(ValueError):
                production.private_json(root / "link", {"evil": True})
            self.assertEqual((root / "original").read_text(), "keep")

    def test_runtime_incompatible_amp_values_do_not_replace_supervisor_config(self):
        for change in ({"countdown_seconds": -1}, {"vote_timeout": 601}, {"vote_cooldown": -1},
                       {"autostart_players": 13, "autostart_enabled": False}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = {"mode": "iccup", "http": {"host": "127.0.0.1", "port": 8080}}
                production.private_json(root / "supervisor.json", config)
                before = (root / "supervisor.json").read_bytes()
                production.private_json(root / "settings.json", {**production.DEFAULTS, **change})
                with self.assertRaises(ValueError):
                    production.merge_settings(root / "supervisor.json", root / "settings.json")
                self.assertEqual((root / "supervisor.json").read_bytes(), before)

    @unittest.skipUnless((ROOT / "src/runtime/config.py").is_file(), "requires integrated runtime")
    def test_supported_amp_boundaries_are_accepted_by_integrated_runtime(self):
        runtime = load("integrated_runtime_config", "src/runtime/config.py")
        for change in ({}, {"countdown_seconds": 1, "vote_timeout": 5, "vote_cooldown": 1},
                       {"countdown_seconds": 60, "vote_timeout": 300, "vote_cooldown": 600},
                       {"lobby_refresh_interval": 10}, {"lobby_refresh_interval": 3600},
                       {"max_players": 3, "autostart_players": 3, "vote_min_players": 3}):
            with self.subTest(change=change):
                values = production.settings({**production.DEFAULTS, **change})
                # map_preset runtime validation is supplied by the map worker's change.
                runtime_values = dict(values)
                if "map_preset" not in runtime.GAMEPLAY_DEFAULTS:
                    runtime_values.pop("map_preset")
                if "autostart_enabled" not in runtime.GAMEPLAY_DEFAULTS:
                    runtime_values.pop("autostart_enabled")
                self.assertEqual(runtime.gameplay_config(runtime_values), runtime_values)


class GateAndLaunchTests(unittest.TestCase):
    def test_stale_native_binary_sources_are_rejected_before_composite_build(self):
        hashes = {"patches/ghostplusplus-modern-linux.patch": "a" * 64,
                  "src/hostbot/metadata.cpp": "b" * 64, "src/runtime/star.py": "c" * 64,
                  "docker/Dockerfile": "d" * 64}
        with patch.object(build, "call", return_value=json.dumps(hashes).encode()):
            build.verify_native_sources(IMAGE, hashes)
            with self.assertRaises(ValueError):
                build.verify_native_sources(IMAGE, {**hashes, "docker/Dockerfile": "e" * 64})
        with patch.object(build, "call", return_value=b'{}'):
            with self.assertRaises(ValueError):
                build.verify_native_sources(IMAGE, hashes)

    def test_prepare_copies_no_assets_and_requires_the_integrated_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production.private_json(root / "accepted.json", accepted())
            production.private_json(root / "supervisor.json", {"mode": "offline", "http": {"host": "127.0.0.1", "port": 8080}})
            (root / "ghost.cfg").write_text("bot_hostport = 6113\n")
            (root / "map.cfg").write_text("map_localpath = selected.w3x\n")
            production.prepare(root / "instance", IMAGE, root / "accepted.json", root / "supervisor.json",
                               root / "ghost.cfg", root / "map.cfg")
            self.assertEqual(production.deployment(root / "instance")["image_id"], IMAGE)
            for name in production.VENDOR:
                self.assertEqual(list((root / "instance/vendor" / name).iterdir()), [])
            cfg = production.read_json(root / "instance/data/config/supervisor.json")
            self.assertEqual(cfg["gameplay"]["max_players"], 12)
            account_input = root / "instance/control/account-input.json"
            self.assertEqual(production.read_json(account_input), {"version": 1, "username": "", "password": ""})
            self.assertEqual(account_input.stat().st_mode & 0o777, 0o600)
            self.assertTrue((root / "instance/control/account.py").is_file())
            self.assertEqual(production.read_json(root / "instance/control/preset-editor.json"),
                             {"version": 1, "preset_definitions": ""})
            with self.assertRaises(FileExistsError):
                production.prepare(root / "instance", IMAGE, root / "accepted.json", root / "supervisor.json",
                                   root / "ghost.cfg", root / "map.cfg")

    def test_build_receipt_cannot_be_used_as_acceptance_and_incomplete_gate_rejected(self):
        self.assertRegex(production.gate(accepted(), IMAGE), r"^[0-9a-f]{64}$")
        for mutate in (lambda g: g.update(result="BUILT"), lambda g: g.update(approved_by_main=False),
                       lambda g: g["checks"].update(offline_soak="PENDING"),
                       lambda g: g["checks"].pop("native_sanitizers"),
                       lambda g: g.update(image_id="sha256:" + "2" * 64)):
            value = accepted()
            mutate(value)
            with self.assertRaises(ValueError):
                production.gate(value, IMAGE)

    def test_only_game_port_published_and_all_vendor_inputs_readonly(self):
        argv = production.command(Path("/tmp/private-instance"), {"container_name": "war3-phase3-staging",
                                  "image_id": IMAGE, "uid": 10001, "gid": 10001})
        self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--publish"], ["0.0.0.0:6113:6113/tcp"])
        mounts = [argv[i + 1] for i, x in enumerate(argv) if x == "--mount"]
        self.assertEqual(len(mounts), len(production.RO) + len(production.RW) + len(production.VENDOR))
        for mount in mounts:
            if "dst=/vendor/" in mount or any("dst=/data/" + n + "," in mount for n in production.RO):
                self.assertTrue(mount.endswith(",readonly"))
        for token in ("--init", "--read-only", "core=0", "--restart=no", "no-new-privileges:true"):
            self.assertIn(token, argv)
        self.assertNotIn("docker.sock", " ".join(mounts))
        self.assertNotIn("--privileged", argv)
        # Native admission checks finite memory limits through the full ancestor
        # hierarchy. A private cgroup subtree hides that proof and fails closed.
        self.assertEqual(argv[argv.index("--cgroupns") + 1], "host")
        self.assertNotIn("--pid", argv)
        self.assertNotIn("--cgroup-parent", argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")

    def test_external_stop_budgets_cover_sequential_component_shutdown(self):
        argv = production.command(Path("/tmp/private-instance"), {"container_name": "war3-phase3-staging",
                                  "image_id": IMAGE, "uid": 10001, "gid": 10001})
        docker_timeout = int(argv[argv.index("--stop-timeout") + 1])
        kvp = dict(line.split("=", 1) for line in (ROOT / "amp/war3-production.kvp").read_text().splitlines())
        self.assertEqual(docker_timeout, 120)
        self.assertEqual(int(kvp["App.ExitTimeout"]), 150)
        self.assertGreater(docker_timeout, 20 + 5 + 5 + 8 + 35 + 12 + 6)
        self.assertGreater(int(kvp["App.ExitTimeout"]), docker_timeout)

    def test_amp_container_uses_host_namespace_for_sibling_bind_sources(self):
        spec = {"container_name": "war3-phase3-staging", "image_id": IMAGE,
                "uid": 1001, "gid": 1001,
                "host_instance_dir": "/srv/amp/instances/War3/war3-production"}
        argv = production.command(Path("/AMP/war3-production"), spec)
        mounts = [argv[i + 1] for i, token in enumerate(argv) if token == "--mount"]
        self.assertTrue(all("src=" + spec["host_instance_dir"] + "/" in m for m in mounts))
        self.assertTrue(all("src=/AMP/" not in m for m in mounts))
        self.assertTrue(any("dst=/vendor/profile,readonly" in m for m in mounts))
        self.assertNotIn("docker.sock", " ".join(mounts))
        self.assertIn("--restart=no", argv)

    def test_deployment_rejects_ambiguous_host_mount_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production.private_json(root / "acceptance.json", accepted())
            spec = {"schema_version": 1, "container_name": "war3-phase3-staging",
                    "image_id": IMAGE, "uid": 1001, "gid": 1001}
            for value in ("relative/path", "/host/../other", "/host,readonly", "/host\nother", 1):
                with self.subTest(value=value):
                    production.private_json(root / "deployment.json", {**spec, "host_instance_dir": value})
                    with self.assertRaises(ValueError):
                        production.deployment(root)

    def test_preflight_requires_both_private_single_link_profile_files(self):
        for invalid in (None, "missing", "permissions", "hardlink"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                spec = {"container_name": "war3-phase3-staging", "image_id": IMAGE,
                        "uid": os.getuid(), "gid": os.getgid()}
                production.private_json(root / "acceptance.json", accepted())
                for name in (*production.RO, *production.RW):
                    (root / "data" / name).mkdir(parents=True)
                for name in production.VENDOR:
                    (root / "vendor" / name).mkdir(parents=True, mode=0o700)
                for name in ("star/Launcher.exe", "redist/vc_redist.x86.exe", "game/war3.exe", "game/game.dll",
                             "profile/launcher.ini", "profile/iccup.ini"):
                    target = root / "vendor" / name
                    target.write_text("SYNTHETIC_NOT_PE\n")
                    target.chmod(0o600)
                profile = root / "vendor/profile/iccup.ini"
                if invalid == "missing":
                    profile.unlink()
                elif invalid == "permissions":
                    profile.chmod(0o644)
                elif invalid == "hardlink":
                    os.link(profile, profile.with_name("duplicate"))
                meta = {"Id": IMAGE, "Architecture": "amd64", "Os": "linux",
                        "Config": {"Labels": {"war3.production.source-digest": production.gate(accepted(), IMAGE)}}}
                with patch.object(production, "docker_json", return_value=[meta]), \
                        patch.object(production, "stopped"):
                    if invalid is None:
                        production.preflight(root, spec, check_port=False)
                    else:
                        with self.assertRaises(ValueError):
                            production.preflight(root, spec, check_port=False)

    def test_stopped_container_guard_never_touches_foreign_named_container(self):
        spec = {"container_name": "war3-phase3-staging"}
        with patch.object(production, "docker_output", return_value=b"abc123\n") as docker:
            with self.assertRaises(ValueError):
                production.stopped(spec)
            self.assertEqual(docker.call_count, 1)
            self.assertEqual(docker.call_args.args[0][:2], ["ps", "-aq"])

    def test_docker_environment_cannot_redirect_to_remote_daemon(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote:2375", "DOCKER_CONTEXT": "remote"}):
            self.assertNotIn("DOCKER_HOST", production.environment())
            self.assertNotIn("DOCKER_CONTEXT", production.environment())
            self.assertEqual(production.DOCKER[1:3], ["--host", "unix:///var/run/docker.sock"])


class BackupTests(unittest.TestCase):
    def archive(self, path, name, kind=tarfile.REGTYPE):
        with tarfile.open(path, "w") as tar:
            item = tarfile.TarInfo(name)
            item.type = kind
            if kind == tarfile.REGTYPE:
                item.size = 4
                tar.addfile(item, io.BytesIO(b"keep"))
            else:
                item.linkname = "/tmp/outside"
                tar.addfile(item)

    def test_private_roundtrip_keeps_persistent_data_but_excludes_auth_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, data = Path(tmp), Path(tmp) / "data"
            for name in production_backup.ALLOWED | {"prefix", "session"}:
                (data / name).mkdir(parents=True)
                (data / name / "marker").write_text("keep")
            (data / "state/mapcfg").mkdir()
            (data / "state/mapcfg/selected.cfg").symlink_to("/data/config/maps/selected.cfg")
            output = root / "backup.tar.gz"
            production_backup.backup(data, output)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            production_backup.restore(output, root / "restored")
            self.assertEqual((root / "restored/sqlite/marker").read_text(), "keep")
            self.assertEqual(list((root / "restored/prefix").iterdir()), [])
            self.assertEqual(list((root / "restored/session").iterdir()), [])
            self.assertEqual((root / "restored/sqlite/marker").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "restored").stat().st_mode & 0o777, 0o700)
            with self.assertRaises(ValueError):
                production_backup.restore(output, root / "restored")
            self.assertTrue(output.exists())

    def test_restore_rejects_traversal_secret_roots_links_and_special_files(self):
        for name, kind in [("../outside", tarfile.REGTYPE), ("/tmp/outside", tarfile.REGTYPE),
                           ("prefix/secret", tarfile.REGTYPE), ("vendor/profile/launcher.ini", tarfile.REGTYPE),
                           ("state/link", tarfile.SYMTYPE), ("state/hard", tarfile.LNKTYPE),
                           ("state/fifo", tarfile.FIFOTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.archive(root / "bad.tar", name, kind)
                with self.assertRaises(ValueError):
                    production_backup.restore(root / "bad.tar", root / "result")
                self.assertFalse((root / "result").exists())
                self.assertEqual(list(root.glob(".war3-production-restore-*")), [])


class ManifestAndReleaseTests(unittest.TestCase):
    def test_layer_audit_reads_classic_and_oci_history_including_deleted_secrets(self):
        def layer_bytes(name):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as tar:
                item = tarfile.TarInfo(name)
                item.size = 4
                tar.addfile(item, io.BytesIO(b"data"))
            return output.getvalue()

        def saved_bytes(layers):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as tar:
                for name, content in layers:
                    item = tarfile.TarInfo(name)
                    item.size = len(content)
                    tar.addfile(item, io.BytesIO(content))
            return output.getvalue()

        class Process:
            def __init__(self, data):
                self.stdout = io.BytesIO(data)
            def wait(self, timeout):
                return 0
            def poll(self):
                return 0

        def classic(layers):
            return [("manifest.json", json.dumps([{"Layers": [name for name, _ in layers]}]).encode()), *layers]

        def oci(contents, media_types=None):
            layers, descriptors = [], []
            for data in contents:
                digest = hashlib.sha256(data).hexdigest()
                layers.append(("blobs/sha256/" + digest, data))
                descriptor={"digest": "sha256:" + digest, "size": len(data)}
                if media_types:descriptor["mediaType"]=media_types[len(descriptors)]
                descriptors.append(descriptor)
            manifest = json.dumps({"schemaVersion": 2, "layers": descriptors}).encode()
            digest = hashlib.sha256(manifest).hexdigest()
            index = {"schemaVersion": 2, "manifests": [{"digest": "sha256:" + digest, "size": len(manifest)}]}
            return [("index.json", json.dumps(index).encode()),
                    ("blobs/sha256/" + digest, manifest), *layers]

        clean = layer_bytes("opt/war3/bin/ghost++")
        statement=json.dumps({'_type':'https://in-toto.io/Statement/v0.1',
                              'predicateType':'https://slsa.dev/provenance/v0.2','predicate':{},
                              'subject':[{'name':'synthetic-image','digest':{'sha256':'a'*64}}]}).encode()
        types=['application/vnd.oci.image.layer.v1.tar','application/vnd.in-toto+json']
        layouts = [classic([("base/layer.tar", clean)]), oci([clean]),oci([clean,statement],types)]
        for layout in layouts:
            with patch.object(release.subprocess, "Popen", return_value=Process(saved_bytes(layout))):
                self.assertEqual(release.audit_layers(IMAGE), 1)
        history = classic([("old/layer.tar", layer_bytes("app/Launcher.exe")),
                           ("new/layer.tar", layer_bytes("app/.wh.Launcher.exe"))])
        with patch.object(release.subprocess, "Popen", return_value=Process(saved_bytes(history))):
            with self.assertRaises(ValueError):
                release.audit_layers(IMAGE)
        missing = classic([("missing/layer.tar", clean)])
        missing[-1] = ("other/layer.tar", clean)
        for layout in (missing, oci([clean, b"\x28\xb5\x2f\xfdunsupported-zstd-layer"]),
                       oci([clean, b'{"not_a_filesystem_layer":true}']),
                       oci([clean,b'{"_type":"unrecognized-provenance"}'],types)):
            with patch.object(release.subprocess, "Popen", return_value=Process(saved_bytes(layout))):
                with self.assertRaises(ValueError):
                    release.audit_layers(IMAGE)

    def test_release_rejects_stale_checkout_before_touching_docker_or_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            accepted_path = Path(tmp) / "gate.json"
            production.private_json(accepted_path, accepted())
            with patch.object(release.context, "prepare", return_value={"changed.py": "c" * 64}), \
                    patch.object(release, "docker_json") as docker:
                with self.assertRaises(ValueError):
                    release.verify(IMAGE, accepted_path)
                docker.assert_not_called()

    def test_amp_settings_match_compiler_and_persist_to_existing_json(self):
        settings = json.loads((ROOT / "amp/war3-productionconfig.json").read_text())
        self.assertEqual({s["Category"] for s in settings}, {"War3_Dedicated:sports_esports"})
        self.assertFalse(any(s["ParamFieldName"] == "$GamePort" for s in settings))
        gameplay = [s for s in settings if s["ParamFieldName"] in production.DEFAULTS]
        auth = [s for s in settings if s.get("Subcategory") == "Account:account_circle:10"]
        self.assertEqual({s["Subcategory"] for s in settings if s["Category"] == "War3_Dedicated:sports_esports"}, {
            "Account:account_circle:10", "Map presets:map:20", "Lobby:groups:30",
            "Lobby / Messages:chat:35", "Voting:how_to_vote:40", "Refresh:refresh:50"})
        internal_settings = {"autostart_players", "autostart_enabled", "game_port", "max_players"}
        self.assertEqual(len(gameplay), 14)
        self.assertEqual({s["ParamFieldName"] for s in gameplay}, set(production.DEFAULTS) - internal_settings - {"map_preset"})
        self.assertTrue(internal_settings.isdisjoint(s["FieldName"] for s in settings))
        self.assertTrue(internal_settings.isdisjoint(s["ParamFieldName"] for s in settings))
        self.assertEqual({s["ParamFieldName"] for s in auth}, {"username", "password"})
        editor = next(s for s in settings if s["ParamFieldName"] == "preset_definitions")
        self.assertEqual(editor["InputType"], "Textarea")
        self.assertEqual(editor["Subcategory"], "Map presets:map:20")
        self.assertEqual(editor["Actions"], [{"Caption": "Apply presets", "Module": "Core",
            "Method": "UpdateApplication", "Argument": "", "IsClientSide": False}])
        selector = next(s for s in settings if s["ParamFieldName"] == "map_cycle")
        self.assertEqual(selector["InputType"], "Textarea")
        self.assertEqual(json.loads(selector["DefaultValue"]), ["manual"])
        self.assertFalse(selector.get("Actions"))
        limit = next(s for s in settings if s["ParamFieldName"] == "max_concurrent_matches")
        self.assertEqual((limit["minValue"], limit["maxValue"], limit["DefaultValue"]), ("1", "5", "3"))
        self.assertEqual(limit["DisplayName"], "Max concurrent matches")
        self.assertNotIn("inactive", limit["DisplayName"])
        self.assertNotIn("no effect", limit["Description"])
        self.assertFalse(any(action.get("Method") == "ReloadGenericConfig"
                             for setting in settings for action in setting.get("Actions", [])))
        refresh = next(s for s in gameplay if s["ParamFieldName"] == "lobby_refresh_interval")
        self.assertEqual(refresh["DisplayName"], "Lobby refresh interval (seconds)")
        self.assertEqual((refresh["DefaultValue"], refresh["minValue"], refresh["maxValue"]), ("10", "10", "3600"))
        self.assertEqual(refresh["Subcategory"], "Refresh:refresh:50")
        manual = next(s for s in gameplay if s["ParamFieldName"] == "game_name")
        self.assertEqual(manual["DisplayName"], "Manual lobby name")
        for setting in gameplay:
            if setting["ParamFieldName"] in {"game_name", "max_players", "autostart_players", "countdown_seconds"} or setting["ParamFieldName"].startswith("vote_"):
                self.assertIn("selected preset may override", setting["Description"])
        self.assertEqual(next(s for s in auth if s["ParamFieldName"] == "password")["InputType"], "password")
        self.assertTrue(all(s["DefaultValue"] == "" and not s["Required"] for s in auth))
        self.assertTrue(all(not s["IncludeInCommandLine"] for s in settings))
        for setting in settings:
            if setting["ParamFieldName"] in production.RANGES:
                # The manual editor can expose a narrower range than legacy
                # configs/CLI; every UI endpoint and default must still compile.
                field = setting["ParamFieldName"]
                minimum, maximum = int(setting["minValue"]), int(setting["maxValue"])
                lower, upper = production.RANGES[field]
                self.assertGreaterEqual(minimum, lower)
                self.assertLessEqual(maximum, upper)
                self.assertLessEqual(minimum, maximum)
                for value in (minimum, maximum, setting["DefaultValue"]):
                    production.settings({**production.DEFAULTS, field: value})
        meta = json.loads((ROOT / "amp/war3-productionmetaconfig.json").read_text())
        self.assertEqual(meta, [
            {"ConfigFile": "data/config/amp-settings.json", "ConfigType": "json", "AutoMap": True, "Importable": True},
            {"ConfigFile": "control/account-input.json", "ConfigType": "json", "AutoMap": True, "Importable": True},
            {"ConfigFile": "control/preset-editor.json", "ConfigType": "json", "AutoMap": True, "Importable": True}])
        updates = json.loads((ROOT / "amp/war3-productionupdates.json").read_text())
        self.assertEqual(updates, [{"UpdateStageName": "Apply local map presets", "UpdateSourcePlatform": "Linux",
            "UpdateSource": "Executable", "UpdateSourceData": "/usr/bin/python3",
            "UpdateSourceArgs": '"{{$FullBaseDir}}control/production.py" apply-presets --instance-dir "{{$FullBaseDir}}"',
            "SkipOnFailure": False}])
        kvp = dict(line.split("=", 1) for line in (ROOT / "amp/war3-production.kvp").read_text().splitlines())
        self.assertEqual(kvp["App.ExitMethod"], "SIGTERM")
        self.assertEqual(kvp["App.SupportsLiveSettingsChanges"], "False")
        self.assertEqual(kvp["Meta.ContainerPolicy"], "Required")
        self.assertEqual(kvp["Meta.SpecificDockerImage"], "war3-amp-control:phase3-candidate")
        ready = re.compile(kvp["Console.AppReadyRegex"])
        self.assertIsNone(ready.fullmatch('{"event":"state","state":"AUTHENTICATING"}'))
        self.assertIsNone(ready.fullmatch('{"event":"state","state":"LOBBY"}'))
        self.assertIsNotNone(ready.fullmatch('2026-10-30 20:00:01 INFO supervisor: Lobby is ready.'))
        self.assertIsNotNone(ready.fullmatch('INFO supervisor: Lobby is ready.'))
        ports = json.loads((ROOT / "amp/war3-productionports.json").read_text())
        self.assertEqual([(p["Protocol"], p["Port"]) for p in ports], [("TCP", 6113)])
        self.assertEqual(ports[0]["Ref"], "GamePort")
        self.assertEqual(kvp["App.PrimaryApplicationPortRef"], ports[0]["Ref"])

    def test_amp_console_filter_removes_duplicate_utc_without_breaking_lifecycle_matches(self):
        kvp = dict(line.split("=", 1) for line in (ROOT / "amp/war3-production.kvp").read_text().splitlines())
        console_filter = re.compile(kvp["Console.FilterMatchRegex"])
        self.assertEqual(kvp["Console.FilterMatchReplacement"], "$1")
        cases = [("AppReadyRegex", "INFO supervisor: Lobby is ready."),
                 ("UserJoinRegex", "INFO native: Player joined: ExamplePlayer"),
                 ("UserLeaveRegex", "INFO native: Player left: ExamplePlayer")]
        for key, message in cases:
            pattern = re.compile(kvp["Console." + key].replace("(?<username>", "(?P<username>"))
            original = "2026-10-30 23:59:59 " + message
            filtered = console_filter.fullmatch(original).group(1)
            self.assertEqual(filtered, message)
            self.assertIsNotNone(pattern.fullmatch(original))
            self.assertIsNotNone(pattern.fullmatch(filtered))
            self.assertIsNone(pattern.fullmatch("INFO native: Lobby message: " + message))
        self.assertIsNone(console_filter.fullmatch("[untrusted chat] 2026-10-30 23:59:59 INFO native: Player joined: Fake"))

    def test_layer_filter_catches_assets_secrets_and_allows_source_built_helper(self):
        for name in ("app/Launcher.exe", "profile/launcher.ini", "game/war3.exe", "game/game.dll",
                     "maps/private.w3x", "data/prefix/user.reg", "vendor/star/Qt5Core.dll"):
            self.assertTrue(release.prohibited(name), name)
        for name in ("opt/war3/bin/ghost++", "opt/war3/bin/quit-launcher.exe", "usr/lib/wine/kernel32.dll"):
            self.assertFalse(release.prohibited(name), name)

    def test_port_contract_keeps_public_game_and_private_bncs_separate(self):
        contract = json.loads((ROOT / "docker/production/runtime-contract.json").read_text())
        self.assertEqual(contract["ports"]["public"], ["0.0.0.0:6113:6113/tcp"])
        self.assertEqual(contract["ports"]["outbound_bncs"], "6112/tcp")
        self.assertEqual(contract["ports"]["relay"], "127.0.0.1:16112")
        self.assertIn("bot_hostport = 6113", (ROOT / "docker/ghost.cfg").read_text())
        self.assertIn("EXPOSE 6113/tcp", (ROOT / "docker/production/Dockerfile").read_text())


class AnalyticsConsoleTests(unittest.TestCase):
    def setUp(self):
        self.kvp = dict(line.split("=", 1) for line in (ROOT / "amp/war3-production.kvp").read_text().splitlines())
        # The template uses .NET named captures. All other constructs here are
        # the shared regex subset; actual AMP rendering/API checks belong to main.
        self.join = re.compile(self.kvp["Console.UserJoinRegex"].replace("(?<username>", "(?P<username>"))
        self.leave = re.compile(self.kvp["Console.UserLeaveRegex"].replace("(?<username>", "(?P<username>"))
        self.ready = re.compile(self.kvp["Console.AppReadyRegex"])
        self.stamp = "2026-10-30 20:00:01"

    def test_human_join_leave_capture_the_same_verbatim_identity(self):
        for name in ("ActualName", "Player One", "Игрок", "O'Neil", "N" * 64):
            with self.subTest(name=name):
                joined = self.join.fullmatch(self.stamp + " INFO native: Player joined: " + name)
                left = self.leave.fullmatch(self.stamp + " INFO native: Player left: " + name)
                self.assertIsNotNone(joined)
                self.assertIsNotNone(left)
                self.assertEqual(joined.groupdict(), {"username": name})
                self.assertEqual(left.groupdict(), {"username": name})
        for key in ("Console.UserJoinRegex", "Console.UserLeaveRegex"):
            self.assertTrue(self.kvp[key].startswith("^") and self.kvp[key].endswith("$"))
            self.assertIn("(?<username>", self.kvp[key])
        self.assertEqual(json.loads(self.kvp["Console.UserActions"]), {})
        self.assertEqual(self.kvp["Console.UserChatRegex"], "^$")

    def test_raw_chat_aggregate_ai_and_unsafe_name_lines_never_track_players(self):
        non_players = [
            '[HOSTBOT_STATE] {"humans":1,"computers":6}',
            '[HOSTBOT_REFRESH] {"event":"slot_toggle"}',
            '[GAME: Lobby] player [ActualName|192.0.2.1] joined the game',
            '[GAME: Lobby] deleting player [ActualName]: disconnected',
            '[GAME: Lobby] [Local]: Player joined: FakeName',
            '[LOCAL: Lobby] Player left: FakeName',
            self.stamp + ' INFO native: Lobby players: humans=1 computers=6',
            self.stamp + ' INFO native: AI player joined: Computer 1',
            self.stamp + ' INFO native: Chat: Player joined: FakeName',
            self.stamp + ' INFO native: Lobby message: Player joined: FakeName',
            self.stamp + ' INFO supervisor: Player joined: FakeName',
            self.stamp + ' WARN native: Player joined: FakeName',
            'Player joined: MissingTimestamp',
        ]
        for line in non_players:
            self.assertIsNone(self.join.fullmatch(line), line)
            self.assertIsNone(self.leave.fullmatch(line), line)
        unsafe = ("", " leading", "trailing ", "[AI]", "color|c12345678", "<redacted>",
                  'quoted"name', "slash\\name", "bad\ufffdname", "A\x00B", "A\x7fB", "N" * 65,
                  "A\rB", "A\nB", "A\tB", "\u00a0leading", "trailing\u00a0")
        for name in unsafe:
            for verb, pattern in (("joined", self.join), ("left", self.leave)):
                self.assertIsNone(pattern.fullmatch(self.stamp + " INFO native: Player " + verb + ": " + name))

    def test_ready_requires_supervisor_state_after_accepted_publication(self):
        for message in ("Lobby is ready.", "Countdown is running.", "Game is running."):
            self.assertIsNotNone(self.ready.fullmatch(self.stamp + " INFO supervisor: " + message))
        for line in (
            self.stamp + " INFO native: Lobby opened: Lobby",
            self.stamp + " INFO native: Lobby rehosted: Lobby",
            self.stamp + " INFO native: Lobby is ready.",
            self.stamp + " INFO native: Lobby message: Lobby is ready.",
            self.stamp + " INFO supervisor: Authenticating.",
            self.stamp + " INFO supervisor: Lobby is ready",
            self.stamp + " ERROR supervisor: Lobby is ready.",
            '{"event":"state","state":"LOBBY"}',
            '[HOSTBOT_STATE] {"phase":"LOBBY","humans":1,"computers":6}',
        ):
            self.assertIsNone(self.ready.fullmatch(line), line)


@unittest.skipUnless((ROOT / ".github/workflows/production-ghcr.yml").is_file(),
                     "legacy private publisher workflow is not distributed in this checkout")
class ReleaseWorkflowTests(unittest.TestCase):
    def step(self, title):
        workflow = (ROOT / ".github/workflows/production-ghcr.yml").read_text()
        tail = workflow.split("      - name: " + title + "\n", 1)[1].split("        run: |\n", 1)[1]
        lines = []
        for line in tail.splitlines():
            if line.startswith("          "):
                lines.append(line[10:])
            elif line:
                break
        return "\n".join(lines) + "\n"

    def run_step(self, title, lookup="absent"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "war3-release-42").mkdir()
            docker = root / "docker.py"
            # All Docker operations are a stub: no socket, registry or account is accessed.
            docker.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
record = {'args': args}
for item in args:
    if item.startswith('type=bind,') and 'dst=/fixtures/game,' in item:
        source = pathlib.Path(dict(p.split('=', 1) for p in item.split(',') if '=' in p)['src'])
        record['fixture'] = {p.name: p.read_text() for p in source.iterdir()}
        record['fixture_path'] = str(source)
with open(os.environ['STUB_LOG'], 'a') as out:
    out.write(json.dumps(record) + '\\n')
if 'login' in args:
    sys.stdin.read()
if 'manifest' in args:
    result = os.environ['STUB_LOOKUP']
    if result == 'exists':
        print('{}')
        raise SystemExit(0)
    message = 'no such manifest: ' + args[-1] if result == 'absent' else result
    print(message, file=sys.stderr)
    raise SystemExit(1)
if 'image' in args and 'inspect' in args:
    print('["ghcr.io/example/synthetic@sha256:dummy"]')
""")
            env = {**os.environ, "RUNNER_TEMP": str(root), "GITHUB_WORKSPACE": str(ROOT), "GITHUB_RUN_ID": "42",
                   "GITHUB_STEP_SUMMARY": str(root / "summary"), "IMAGE_ID": IMAGE,
                   "GHCR_TOKEN": "synthetic", "GHCR_ACTOR": "synthetic",
                   "REGISTRY_IMAGE": "ghcr.io/example/synthetic", "VERSION_INPUT": "v0.0.0-test",
                   "STUB_LOG": str(root / "calls.jsonl"), "STUB_LOOKUP": lookup, "STUB_DOCKER": str(docker)}
            # A function also works when the test container mounts /tmp noexec.
            script = 'docker() { python3 "$STUB_DOCKER" "$@"; }\n' + self.step(title)
            result = subprocess.run(["bash", "-c", script], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertTrue((root / "calls.jsonl").exists(), result.stderr)
            records = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            for record in records:
                if "fixture_path" in record:
                    self.assertFalse(Path(record["fixture_path"]).exists(), "unit fixture was not removed")
            return result, records

    def test_unit_containers_use_checkout_and_synthetic_readonly_metadata(self):
        result, records = self.run_step("Offline unit and infrastructure regression in local Docker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(records), 5)
        for record in records:
            args = record["args"]
            self.assertEqual(args[args.index("--network") + 1], "none")
            self.assertEqual(args[args.index("--workdir") + 1], "/project")
            env = [args[i + 1] for i, value in enumerate(args) if value == "--env"]
            self.assertIn("PYTHONPATH=/project", env)
            mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
            self.assertTrue(any(m.endswith("dst=/fixtures/game,readonly") for m in mounts))
            self.assertEqual(set(record["fixture"]), {"war3.exe", "game.dll"})
            self.assertTrue(all(text.startswith("SYNTHETIC_NOT_PE ") for text in record["fixture"].values()))

    def test_publish_requires_confirmed_tag_absence(self):
        for lookup in ("absent", "exists", "registry timeout", "unauthorized"):
            with self.subTest(lookup=lookup):
                result, records = self.run_step("Publish accepted local image with version and digest", lookup)
                pushes = [record for record in records if "push" in record["args"]]
                tags = [record for record in records if "tag" in record["args"]]
                if lookup == "absent":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(pushes), 1)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(pushes)
                    self.assertFalse(tags)


if __name__ == "__main__":
    unittest.main()
