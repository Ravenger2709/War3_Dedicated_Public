"""Standalone CLI fixtures and Docker stubs; no PE, Wine, account auth or daemon."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "amp"))
spec = importlib.util.spec_from_file_location("standalone_war3", ROOT / "scripts/war3.py")
war3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(war3)
IMAGE = "sha256:" + "a" * 64
CONTAINER = "b" * 64


class DockerFixture:
    def __init__(self, accepted):
        self.accepted = accepted
        self.meta = None
        self.calls = []
        self.stop_exit = 0
        self.preparing = False
        self.fail_run = False

    def __call__(self, command, **kwargs):
        assert command[:3] == war3.production.DOCKER
        args = command[3:]
        self.calls.append((args, kwargs))
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            assert key not in kwargs["env"]
        output = b""
        if args[:2] == ["image", "inspect"]:
            output = json.dumps([{"Id": IMAGE, "Architecture": "amd64", "Os": "linux",
                                  "Config": {"Labels": {"war3.production.source-digest":
                                             war3.production.gate(self.accepted, IMAGE)}}}]).encode()
        elif args[0] == "ps":
            output = (CONTAINER + "\n").encode() if self.meta else b""
        elif args[:2] == ["container", "inspect"]:
            assert self.meta and self.meta["Id"].startswith(args[2])
            output = json.dumps([self.meta]).encode()
        elif args[0] == "run":
            assert not self.preparing, "profile transaction must close before runtime launch"
            if self.fail_run:
                raise subprocess.CalledProcessError(125, command, stderr=b"PASSWORD_MUST_NOT_BE_PRINTED")
            assert self.meta is None
            mounts, labels = [], {}
            for i, arg in enumerate(args):
                if arg == "--mount":
                    tokens = args[i+1].split(",")
                    fields = dict(t.split("=", 1) for t in tokens if "=" in t)
                    mounts.append({"Type": "bind", "Source": fields["src"], "Destination": fields["dst"],
                                   "RW": "readonly" not in tokens})
                if arg == "--label":
                    key, value = args[i+1].split("=", 1)
                    labels[key] = value
            restart = next(a.split("=", 1)[1] for a in args if a.startswith("--restart="))
            self.meta = {"Id": CONTAINER, "Name": "/" + args[args.index("--name")+1], "Image": args[-1],
                         "Config": {"User": args[args.index("--user")+1], "Labels": labels,
                                    "Env": ["DO_NOT_PRINT_PRIVATE_INSPECT_ENV"]}, "Mounts": mounts,
                         "HostConfig": {"RestartPolicy": {"Name": restart}, "NetworkMode": "bridge",
                                        "ReadonlyRootfs": True, "Privileged": False, "AutoRemove": False,
                                        "CapDrop": ["ALL"], "Init": True,
                                        "SecurityOpt": ["no-new-privileges:true"],
                                        "PortBindings": {"6113/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6113"}]}},
                         "State": {"Status": "running", "Running": True, "Restarting": False,
                                   "Paused": False, "ExitCode": 0, "Health": {"Status": "starting"}}}
            output = (CONTAINER + "\n").encode()
        elif args[:2] == ["container", "stop"]:
            assert args == ["container", "stop", "--time", "120", CONTAINER]
            assert kwargs["timeout"] == 135
            self.meta["State"].update(Running=False, Restarting=False, Status="exited", ExitCode=self.stop_exit)
        elif args[:2] == ["container", "rm"]:
            assert args == ["container", "rm", CONTAINER]
            assert not self.meta["State"]["Running"]
            self.meta = None
        elif args[0] == "logs":
            assert args[-1] == CONTAINER
        else:
            raise AssertionError("unexpected Docker operation: " + repr(args))
        return subprocess.CompletedProcess(command, 0, output, b"")

    def mutations(self):
        return [a for a, _ in self.calls if a[0] == "run" or a[:2] in (["container", "stop"], ["container", "rm"])]


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.instance = self.root / "private-instance"
        self.accepted = {"schema_version": 1, "image_id": IMAGE, "approved_by_main": True,
                         "source_sha256": {"src/synthetic.py": "c" * 64,
                                           **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                              for name in ("src/supervisor/config.py", "src/runtime/config.py")}},
                         "checks": {key: "PASS" for key in war3.production.CHECKS}}
        self.gate = self.root / "accepted.json"
        war3.production.private_json(self.gate, self.accepted)
        supervisor = self.root / "supervisor.json"
        supervisor.write_bytes((ROOT / "config/supervisor-iccup.example.json").read_bytes())
        native, mapcfg = self.root / "ghost.cfg", self.root / "map.cfg"
        native.write_text(war3.bootstrap.NATIVE_CONFIG)
        mapcfg.write_text(war3.bootstrap.MAP_CONFIG)
        war3.production.prepare(self.instance, IMAGE, self.gate, supervisor, native, mapcfg)
        for name in ("star/Launcher.exe", "profile/launcher.ini", "profile/iccup.ini", "redist/vc_redist.x86.exe",
                     "game/war3.exe", "game/game.dll"):
            path = self.instance / "vendor" / name
            path.write_bytes(b"SYNTHETIC_NOT_PE_NOT_ACCOUNT")
            path.chmod(0o600)
        self.marker = {"version": 1, "owner": "d" * 32, "instance_dir": str(self.instance)}
        war3.production.private_json(self.instance / war3.MARKER, self.marker)
        self.docker = DockerFixture(self.accepted)
        self.addCleanup(patch.stopall)
        patch.object(subprocess, "run", side_effect=self.docker).start()

    def call(self, action, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = war3.main([action, "--instance-dir", str(self.instance), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_explicit_preset_reconciles_legacy_edit_on_bootstrap_settings(self):
        from contextlib import nullcontext
        path = self.instance / "data/config/amp-settings.json"
        settings = {**{k: v for k, v in war3.production.DEFAULTS.items() if k != "map_preset"},
                    "map_cycle": '["manual"]', "max_concurrent_matches": 3, "map_preset": "custom"}
        war3.production.private_json(path, settings)
        war3.production.private_json(self.instance / war3.production.PRESETS,
            {"version": 1, "presets": [{"id": "manual", "enabled": True},
                {"id": "custom", "enabled": True, "map_filename": "Custom.w3x"}]})
        spec = war3.production.deployment(self.instance)
        (self.instance / "data/maps/Custom.w3x").write_bytes(b"synthetic map")
        def discover(instance, document, *args, **kwargs):
            return document, 0
        with patch.object(war3.account, "profile_update", return_value=nullcontext()), \
             patch.object(war3.bootstrap, "supervisor_source"), \
             patch.object(war3.production, "preflight"), \
             patch.object(war3.bootstrap, "validate_startup_config"), \
             patch.object(war3.production.map_discovery, "discover", side_effect=discover):
            war3.prepare_start(self.instance, spec)
            self.assertEqual(war3.production.read_json(path), settings)
            self.assertEqual(war3.production.read_json(self.instance / "data/config/supervisor.json")["gameplay"]["map_cycle"], ["manual"])
            war3.prepare_start(self.instance, spec, map_preset="custom")
            saved = war3.production.read_json(path)
            self.assertNotIn("map_preset", saved)
            self.assertEqual(saved["map_cycle"], ["custom"])
            self.assertEqual(saved["max_concurrent_matches"], 3)
            self.assertEqual(war3.production.read_json(self.instance / "data/config/supervisor.json")["gameplay"]["map_cycle"], ["custom"])
            war3.prepare_start(self.instance, spec)

    def test_explicit_selection_rejects_multi_and_rolls_back_invalid_or_failed_start(self):
        from contextlib import nullcontext
        path = self.instance / "data/config/amp-settings.json"
        spec = war3.production.deployment(self.instance)
        base = {**{k: v for k, v in war3.production.DEFAULTS.items() if k != "map_preset"},
                "map_cycle": '["manual"]', "max_concurrent_matches": 3}
        config = self.instance / "data/config/supervisor.json"
        with patch.object(war3.account, "profile_update", return_value=nullcontext()), \
             patch.object(war3.bootstrap, "supervisor_source"), \
             patch.object(war3.production, "preflight", side_effect=ValueError("preflight failed")):
            for change, choice in (({}, "missing"), ({}, "manual"),
                                   ({"map_cycle": '["manual", "manual"]'}, "manual"),
                                   ({"map_cycle": '"[\"manual\"]"'}, "manual")):
                with self.subTest(change=change, choice=choice):
                    war3.production.private_json(path, {**base, **change})
                    before, config_before = path.read_bytes(), config.read_bytes()
                    with self.assertRaises(ValueError):
                        war3.prepare_start(self.instance, spec, map_preset=choice)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(config.read_bytes(), config_before)

    def test_multi_cycle_start_keeps_snapshot_and_checks_candidate_before_merge(self):
        from contextlib import nullcontext
        path = self.instance / 'data/config/amp-settings.json'
        value = {**war3.production.DEFAULTS, 'map_cycle': ['manual', 'manual'],
                 'max_concurrent_matches': 5}
        war3.production.private_json(path, value)
        spec = war3.production.deployment(self.instance)
        events = []
        def candidate(instance, deployment, validated):
            events.append('candidate')
            self.assertEqual(validated['map_cycle'], ['manual', 'manual'])
            self.assertEqual(deployment, spec)
            # The already validated selection, not this late edit, must be merged.
            war3.production.private_json(path, war3.production.DEFAULTS)
        def merge(*args, **kwargs):
            events.append('merge')
            return original_merge(*args, **kwargs)
        original_merge = war3.production.merge_settings
        with patch.object(war3.account, 'profile_update', return_value=nullcontext()), \
             patch.object(war3.bootstrap, 'supervisor_source'), \
             patch.object(war3.bootstrap, 'validate_startup_config'), \
             patch.object(war3.production, 'preflight', side_effect=lambda *a: events.append('preflight')), \
             patch.object(war3.production, 'validate_cycle_candidate', side_effect=candidate), \
             patch.object(war3.production, 'merge_settings', side_effect=merge):
            war3.prepare_start(self.instance, spec)
        self.assertEqual(events, ['preflight', 'candidate', 'merge'])
        game = war3.production.read_json(self.instance / 'data/config/supervisor.json')['gameplay']
        self.assertEqual(game['map_cycle'], ['manual', 'manual'])
        self.assertEqual(game['max_concurrent_matches'], 5)

    def test_multi_cycle_preset_override_has_deliberate_conflict_and_preserves_files(self):
        path = self.instance / 'data/config/amp-settings.json'
        war3.production.private_json(path, {**war3.production.DEFAULTS,
                                            'map_cycle': ['manual', 'manual']})
        before = path.read_bytes()
        with war3.selected_preset_update(self.instance, 'manual') as apply:
            with self.assertRaises(war3.production.SettingsError) as raised:
                apply()
        self.assertEqual(raised.exception.args, ('MAP_CYCLE_OVERRIDE_CONFLICT',))
        self.assertIn('omit --preset', raised.exception.public_message)
        self.assertEqual(path.read_bytes(), before)
        for option in ('--preset', '--map-preset'):
            code, output, error = self.call('start', option, 'manual')
            self.assertEqual(code, 2)
            self.assertIn('omit --preset', output + error)
            self.assertEqual(path.read_bytes(), before)
            self.assertIsNone(self.docker.meta)

    def test_multi_cycle_candidate_failure_prevents_merge_or_runtime_launch(self):
        path = self.instance / 'data/config/amp-settings.json'
        config = self.instance / 'data/config/supervisor.json'
        war3.production.private_json(path, {**war3.production.DEFAULTS,
                                            'map_cycle': ['manual', 'manual']})
        before, selector = config.read_bytes(), path.read_bytes()
        with patch.object(war3.production, 'validate_cycle_candidate',
                          side_effect=war3.production.SettingsError('MAP_CYCLE_PREFLIGHT_FAILED')), \
             patch.object(war3.production, 'merge_settings') as merge:
            self.assertEqual(self.call('start')[0], 2)
        merge.assert_not_called()
        self.assertIsNone(self.docker.meta)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(path.read_bytes(), selector)

    def test_explicit_selection_rolls_back_both_files_if_final_validation_fails(self):
        from contextlib import nullcontext
        settings = self.instance / "data/config/amp-settings.json"
        config = self.instance / "data/config/supervisor.json"
        before = (settings.read_bytes(), config.read_bytes())
        with patch.object(war3.account, "profile_update", return_value=nullcontext()), \
             patch.object(war3.bootstrap, "supervisor_source"), \
             patch.object(war3.production, "preflight"), \
             patch.object(war3.bootstrap, "validate_startup_config", side_effect=ValueError("bad runtime config")):
            with self.assertRaises(ValueError):
                war3.prepare_start(self.instance, war3.production.deployment(self.instance), map_preset="manual")
        self.assertEqual((settings.read_bytes(), config.read_bytes()), before)

    def test_default_start_preserves_supervisor_race_without_explicit_preset(self):
        from contextlib import nullcontext
        config = self.instance / "data/config/supervisor.json"
        external = {"mode": "iccup", "http": {"host": "127.0.0.1", "port": 8080}, "external": 17}
        exchange = war3.account.exchange
        def race(left, right):
            if right == config:
                war3.production.private_json(config, external)
            return exchange(left, right)
        with patch.object(war3.account, "profile_update", return_value=nullcontext()), \
             patch.object(war3.bootstrap, "supervisor_source"), \
             patch.object(war3.production, "preflight"), \
             patch.object(war3.account, "exchange", side_effect=race):
            with self.assertRaisesRegex(war3.production.SettingsError, "SUPERVISOR_EDIT_CONFLICT"):
                war3.prepare_start(self.instance, war3.production.deployment(self.instance))
        recovery = self.instance / war3.production.PRESET_RECOVERY
        self.assertEqual(war3.production.read_json(recovery / "displaced.json"), external)
        self.assertTrue((recovery / "attempted.json").exists())

    def test_selection_transaction_preserves_concurrent_settings_edit(self):
        path = self.instance / "data/config/amp-settings.json"
        external = {**war3.production.DEFAULTS, "game_name": "External Edit"}
        with self.assertRaises(war3.production.SettingsError):
            with war3.selected_preset_update(self.instance, "manual") as apply:
                apply()
                war3.production.private_json(path, external)
        self.assertEqual(war3.production.read_json(path), external)
        recovery = self.instance / war3.production.PRESET_RECOVERY
        self.assertTrue((recovery / "previous.json").is_file())
        self.assertTrue((recovery / "attempted.json").is_file())
        self.assertEqual(war3.production.read_json(recovery / "external.json"), external)

    def test_start_and_restart_accept_explicit_preset(self):
        for action in ("start", "restart"):
            args = war3.parser().parse_args([action, "--map-preset", "custom-map"])
            self.assertEqual(args.map_preset, "custom-map")

    def test_prepare_start_discovers_selects_removes_and_rediscovers_maps(self):
        instance = self.instance
        maps = instance / 'data/maps'
        map_file = maps / 'Example.w3x'
        map_file.write_bytes(b'map fixture' * 10)
        presets = instance / war3.production.PRESETS
        war3.production.private_json(presets, {'version': 1, 'presets': [{'id': 'manual', 'enabled': True}]})
        manifest = instance / war3.production.PRESET_MANIFEST
        war3.production.private_json(manifest, [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset'}])
        settings_path = instance / 'data/config/amp-settings.json'
        data = war3.production.read_json(settings_path)
        data['map_preset'] = 'manual'
        war3.production.private_json(settings_path, data)
        discover = war3.production.map_discovery.discover
        def analyze(path, *_):
            return {'map_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'script_sha256': 'a' * 64,
                    'lobby_name': 'Example', 'slots': [[0, 255, 0, 0, 0, i, 32, 1, 100] for i in range(4)],
                    'human_capacity': 4, 'ai_evidence': {'level': 'candidate', 'features': ['computer_control']}}
        from contextlib import nullcontext
        with patch.object(war3.account, 'profile_update', return_value=nullcontext()), \
             patch.object(war3.bootstrap, 'supervisor_source'), \
             patch.object(war3.production, 'preflight'), \
             patch.object(war3.bootstrap, 'validate_startup_config'), \
             patch.object(war3.production.map_discovery, 'discover', side_effect=lambda i, d, spec, docker, env, owner, **kw: discover(i, d, spec, docker, env, owner, analyze, **kw)):
            spec = war3.production.deployment(instance)
            war3.prepare_start(instance, spec)
            doc = war3.production.read_json(presets)
            chosen = next(p['id'] for p in doc['presets'] if p['id'] != 'manual')
            self.assertNotIn('capabilities', doc)
            data['map_preset'] = chosen
            war3.production.private_json(settings_path, data)
            war3.prepare_start(instance, spec)
            self.assertEqual(war3.production.read_json(instance / 'data/config/supervisor.json')['gameplay']['map_cycle'], [chosen])
            map_file.unlink()
            settings_before = settings_path.read_bytes()
            with self.assertRaisesRegex(war3.production.SettingsError, 'PRESET_ACTIVE_MAP_MISSING'):
                war3.prepare_start(instance, spec)
            self.assertEqual([p['id'] for p in war3.production.read_json(presets)['presets']], ['manual', chosen])
            self.assertEqual(settings_path.read_bytes(), settings_before)
            self.assertEqual(set(json.loads(manifest.read_text())[0]['EnumValues']), {'manual', chosen})
            data['map_preset'] = 'manual'
            war3.production.private_json(settings_path, data)
            war3.prepare_start(instance, spec)
            self.assertEqual([p['id'] for p in war3.production.read_json(presets)['presets']], ['manual', chosen])
            map_file.write_bytes(b'map fixture' * 10)
            war3.prepare_start(instance, spec)
            self.assertEqual(len(war3.production.read_json(presets)['presets']), 2)
            self.assertNotIn('capabilities', war3.production.read_json(presets))

    def test_prepare_start_removes_last_selected_and_refreshes_manual_selector(self):
        from contextlib import nullcontext
        instance = self.instance
        presets = instance / war3.production.PRESETS
        manifest = instance / war3.production.PRESET_MANIFEST
        settings_path = instance / 'data/config/amp-settings.json'
        war3.production.private_json(presets, {'version': 1, 'presets': [{'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}]})
        war3.production.private_json(manifest, [{'Category': 'War3_Dedicated:sports_esports', 'ParamFieldName': 'map_preset',
                                                  'InputType': 'enum', 'EnumValues': {'gone': 'Gone'}}])
        war3.production.private_json(settings_path, {**war3.production.DEFAULTS, 'map_preset': 'gone'})
        before = settings_path.read_bytes()
        with patch.object(war3.account, 'profile_update', return_value=nullcontext()), \
             patch.object(war3.bootstrap, 'supervisor_source'):
            with self.assertRaisesRegex(war3.production.SettingsError, 'PRESET_ACTIVE_MAP_MISSING'):
                war3.prepare_start(instance, war3.production.deployment(instance))
        self.assertEqual(war3.production.read_json(presets)['presets'], [{'id': 'gone', 'enabled': True, 'map_filename': 'gone.w3x'}])
        self.assertEqual(set(json.loads(manifest.read_text())[0]['EnumValues']), {'gone'})
        self.assertEqual(settings_path.read_bytes(), before)

    def started(self, *args):
        code, output, error = self.call("start", *args)
        self.assertEqual(code, 0, error)
        self.assertIn("Started", output)
        return self.docker.meta

    def test_detached_start_reuses_gate_profile_presets_and_mount_security(self):
        events = []
        @contextmanager
        def profile(instance, spec):
            self.docker.preparing = True
            events.append("prepare-profile")
            yield
            self.docker.preparing = False
            events.append("profile-closed")
        with patch.object(war3.account, "profile_update", side_effect=profile) as helper, patch.object(war3.production, "preset_update", wraps=war3.production.preset_update) as presets:
            meta = self.started()
        self.assertEqual(events, ["prepare-profile", "profile-closed"])
        helper.assert_called_once()
        presets.assert_called_once()
        run = next(a for a, _ in self.docker.calls if a[0] == "run")
        for flag in ("--detach", "--read-only", "--init", "--interactive", "--sig-proxy=true", "--restart=no", "--pull=never"):
            self.assertIn(flag, run)
        self.assertNotIn("--rm", run)
        self.assertNotIn("--force", run)
        self.assertEqual(run[-1], IMAGE)
        self.assertEqual([run[i+1] for i, v in enumerate(run) if v == "--publish"], ["0.0.0.0:6113:6113/tcp"])
        self.assertFalse(any("docker.sock" in m["Source"] or "docker.sock" in m["Destination"] for m in meta["Mounts"]))
        self.assertEqual(meta["Config"]["Labels"][war3.OWNER_LABEL], self.marker["owner"])

    def test_normal_profile_helper_path_runs_only_its_stopped_empty_input_branch(self):
        with patch.object(war3.account, "generate", side_effect=AssertionError("no real credentials or PE")):
            self.started()
        operations = [a[:2] for a, _ in self.docker.calls]
        self.assertIn(["image", "inspect"], operations)
        self.assertTrue((self.instance / "control/account-setup.lock").is_file())

    def test_unless_stopped_is_explicit_standalone_only_and_preserved_on_restart(self):
        self.started("--restart-policy", "unless-stopped")
        self.assertEqual(self.docker.meta["HostConfig"]["RestartPolicy"]["Name"], "unless-stopped")
        marker_before = (self.instance / war3.MARKER).read_bytes()
        persistent = self.instance / "data/sqlite/keep"
        persistent.write_text("persistent-data")
        inode = persistent.stat().st_ino
        code, _, error = self.call("restart")
        self.assertEqual(code, 0, error)
        self.assertEqual([a[0] if a[0] == "run" else a[1] for a in self.docker.mutations()], ["run", "stop", "rm", "run"])
        self.assertEqual(self.docker.meta["HostConfig"]["RestartPolicy"]["Name"], "unless-stopped")
        self.assertEqual(persistent.read_text(), "persistent-data")
        self.assertEqual(persistent.stat().st_ino, inode)
        self.assertEqual((self.instance / war3.MARKER).read_bytes(), marker_before)

    def test_stop_keeps_logs_remove_retains_data_and_absent_stop_is_safe(self):
        self.started()
        private = self.instance / "data/state/keep"
        private.write_text("keep")
        self.assertEqual(self.call("stop")[0], 0)
        self.assertFalse(self.docker.meta["State"]["Running"])
        self.assertEqual(self.call("logs", "--tail", "25")[0], 0)
        args, options = self.docker.calls[-1]
        self.assertEqual(args, ["logs", "--tail", "25", CONTAINER])
        self.assertFalse(options["capture_output"])
        self.assertEqual(self.call("stop", "--remove")[0], 0)
        self.assertIsNone(self.docker.meta)
        self.assertEqual(private.read_text(), "keep")
        self.assertEqual(self.call("stop")[0], 0)

    def test_foreground_exec_happens_after_all_setup_and_cli_locks_close(self):
        def execute(binary, argv, env):
            self.assertEqual(binary, "docker")
            self.assertNotIn("--detach", argv)
            self.assertIn("--restart=no", argv)
            self.assertFalse(self.docker.preparing)
            with war3.account.locked(self.instance / "control/standalone.lock"):
                pass
        with patch.object(os, "execvpe", side_effect=execute) as execute:
            self.assertEqual(self.call("start", "--foreground")[0], 0)
        execute.assert_called_once()
        self.assertFalse(any(a[0] == "run" for a, _ in self.docker.calls))

    def test_invalid_foreground_policy_is_rejected_before_stopping_active_container(self):
        self.started("--restart-policy", "unless-stopped")
        before = self.docker.mutations()[:]
        self.assertEqual(self.call("restart", "--foreground")[0], 2)
        self.assertEqual(self.docker.mutations(), before)

    def test_status_prints_only_safe_summary_and_distinguishes_running_from_health(self):
        code, output, error = self.call("status")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["state"], "stopped")
        self.started()
        code, output, error = self.call("status")
        self.assertEqual(code, 0, error)
        summary = json.loads(output)
        self.assertEqual(summary["state"], "running")
        self.assertEqual(summary["health"], "starting")
        self.assertNotIn("DO_NOT_PRINT_PRIVATE_INSPECT_ENV", output)

    def test_following_logs_never_holds_the_lifecycle_lock(self):
        self.started()
        real_docker = war3.docker
        def streaming(args, **kwargs):
            if args[0] == "logs":
                self.assertIn("--follow", args)
                self.assertIsNone(kwargs["timeout"])
                with war3.account.locked(self.instance / "control/standalone.lock"):
                    pass
            return real_docker(args, **kwargs)
        with patch.object(war3, "docker", side_effect=streaming):
            self.assertEqual(self.call("logs", "--follow")[0], 0)

    def test_foreign_amp_or_other_instance_containers_never_stopped_removed_or_started(self):
        self.started()
        original = copy.deepcopy(self.docker.meta)
        mutations = [lambda m: m["Config"]["Labels"].pop(war3.OWNER_LABEL),
                     lambda m: m["Config"]["Labels"].update({war3.OWNER_LABEL: "f" * 32}),
                     lambda m: m["Config"]["Labels"].update({war3.PATH_LABEL: "0" * 64}),
                     lambda m: m["Config"].update(User="0:0"),
                     lambda m: m["Mounts"][0].update(Source="/foreign/data"),
                     lambda m: m["HostConfig"]["PortBindings"].update({"9999/tcp": []}),
                     lambda m: m.update(Image="sha256:" + "e" * 64)]
        for mutate in mutations:
            self.docker.meta = copy.deepcopy(original)
            mutate(self.docker.meta)
            for action in ("start", "stop", "restart", "status", "logs", "account"):
                before = self.docker.mutations()[:]
                with self.subTest(action=action, mutate=mutate):
                    self.assertEqual(self.call(action)[0], 2)
                    self.assertEqual(self.docker.mutations(), before)

    def test_missing_marker_bad_gate_wrong_owner_host_path_and_links_fail_before_docker(self):
        for invalid in ("marker", "gate", "uid", "host", "path-link", "marker-link"):
            marker_path = self.instance / war3.MARKER
            marker_bytes = marker_path.read_bytes()
            deployment_path = self.instance / "deployment.json"
            deployment_bytes = deployment_path.read_bytes()
            accepted_bytes = (self.instance / "acceptance.json").read_bytes()
            path = self.instance
            if invalid == "marker":
                marker_path.unlink()
            elif invalid == "gate":
                war3.production.private_json(self.instance / "acceptance.json", {**self.accepted, "approved_by_main": False})
            elif invalid in ("uid", "host"):
                value = json.loads(deployment_bytes)
                value["uid" if invalid == "uid" else "host_instance_dir"] = os.getuid()+1 if invalid == "uid" else "/foreign"
                war3.production.private_json(deployment_path, value)
            elif invalid == "path-link":
                path = self.root / "linked"
                path.symlink_to(self.instance, target_is_directory=True)
            else:
                marker_path.unlink()
                marker_path.symlink_to(self.gate)
            before = len(self.docker.calls)
            with self.subTest(invalid=invalid), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(war3.main(["status", "--instance-dir", str(path)]), 2)
            self.assertEqual(len(self.docker.calls), before)
            if marker_path.is_symlink():
                marker_path.unlink()
            marker_path.write_bytes(marker_bytes); marker_path.chmod(0o600)
            deployment_path.write_bytes(deployment_bytes)
            (self.instance / "acceptance.json").write_bytes(accepted_bytes)

    def test_profile_preflight_failure_rolls_back_before_runtime_launch(self):
        events = []
        @contextmanager
        def profile(*args):
            events.append("enter")
            try:
                yield
            except ValueError:
                events.append("rollback")
                raise
        with patch.object(war3.account, "profile_update", side_effect=profile), patch.object(war3.production, "merge_settings", side_effect=ValueError("invalid")):
            self.assertEqual(self.call("start")[0], 2)
        self.assertEqual(events, ["enter", "rollback"])
        self.assertEqual(self.docker.mutations(), [])

    def test_mismatched_checkout_schema_fails_before_account_generation_or_launch(self):
        self.accepted["source_sha256"]["src/runtime/config.py"] = "0" * 64
        war3.production.private_json(self.instance / "acceptance.json", self.accepted)
        with patch.object(war3.account, "profile_update") as helper:
            self.assertEqual(self.call("start")[0], 2)
        helper.assert_not_called()
        self.assertEqual(self.docker.mutations(), [])

    def test_removed_manual_or_disabled_preset_is_rejected_before_merge_or_launch(self):
        proofs = []
        for name in ('custom.w3x', 'disabled.w3x'):
            path = self.instance / 'data/maps' / name
            path.write_bytes((name * 8).encode())
            proofs.append({'id': name.split('.')[0], 'level': 'approved_preset',
                           'map_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                           'script_sha256': 'a' * 64, 'features': ['ai_start'],
                           'review': 'synthetic fixture', 'runtime_tested': False})
        presets = {"version": 1, "presets": [
            {"id": "custom", "enabled": True, "map_filename": "custom.w3x"},
            {"id": "disabled", "enabled": False, "map_filename": "disabled.w3x"}], 'capabilities': proofs}
        war3.production.private_json(self.instance / "data/config/presets.json", presets)
        config_path = self.instance / "data/config/supervisor.json"
        original = config_path.read_bytes()
        for choice in ("manual", "disabled", "removed"):
            with self.subTest(choice=choice):
                war3.production.private_json(self.instance / "data/config/amp-settings.json",
                                             {**war3.production.DEFAULTS, "map_preset": choice})
                with patch.object(war3.production, "preflight") as preflight, patch.object(war3.production, "merge_settings") as merge:
                    code, _, _ = self.call("start")
                self.assertEqual(code, 2)
                preflight.assert_not_called()
                merge.assert_not_called()
                self.assertEqual(self.docker.mutations(), [])
                self.assertEqual(config_path.read_bytes(), original)
                self.assertEqual(list((self.instance / "control").glob("war3-preset-*")), [])

    def test_merge_uses_validated_selection_if_settings_file_changes_during_preflight(self):
        map_path = self.instance / 'data/maps/custom.w3x'
        map_path.write_bytes(b'fixture map' * 10)
        proof = {'id': 'fixture', 'level': 'approved_preset', 'map_sha256': hashlib.sha256(map_path.read_bytes()).hexdigest(),
                 'script_sha256': 'a' * 64, 'features': ['ai_start'], 'review': 'synthetic test', 'runtime_tested': False}
        war3.production.private_json(self.instance / "data/config/presets.json", {"version": 1, "presets": [
            {"id": "custom", "enabled": True, "map_filename": "custom.w3x"}], 'capabilities': [proof]})
        settings_path = self.instance / "data/config/amp-settings.json"
        war3.production.private_json(settings_path, {**war3.production.DEFAULTS, "map_preset": "custom"})
        real_preflight = war3.production.preflight
        def concurrent_edit(instance, spec):
            real_preflight(instance, spec)
            value = war3.production.read_json(settings_path)
            value["map_preset"] = "manual"  # Not an enabled choice in this JSON.
            war3.production.private_json(settings_path, value)
        with patch.object(war3.production, "preflight", side_effect=concurrent_edit), patch.object(war3.production, "merge_settings", wraps=war3.production.merge_settings) as merge:
            self.started()
        self.assertEqual(merge.call_args.kwargs["validated"]["map_preset"], "custom")
        merged = war3.production.read_json(self.instance / "data/config/supervisor.json")
        self.assertEqual(merged["gameplay"]["map_cycle"], ["custom"])
        self.assertEqual(war3.production.read_json(settings_path)["map_preset"], "manual")

    def test_account_delegates_only_to_interactive_helper_and_requires_stopped_state(self):
        with patch.object(war3.account, "cli", return_value=0) as helper:
            self.assertEqual(self.call("account")[0], 0)
            helper.assert_called_once_with(["--instance-dir", str(self.instance)])
            self.started()
            self.assertEqual(self.call("account")[0], 2)
            self.assertEqual(helper.call_count, 1)

    def test_setup_delegates_shared_bootstrap_and_marks_only_its_new_instance(self):
        destination = self.root / "new-standalone"
        def prepared(instance, *args, **kwargs):
            instance.mkdir(mode=0o700)
            (instance / "control").mkdir(mode=0o700)
            return instance
        args = ["setup", "--instance-dir", str(destination), "--noninteractive", "--gate", str(self.gate),
                "--image", IMAGE, "--star-dir", "/explicit/star", "--game-dir", "/explicit/game", "--map", "/explicit/map.w3x"]
        with patch.object(war3.bootstrap, "setup", side_effect=prepared) as helper, redirect_stdout(io.StringIO()):
            self.assertEqual(war3.main(args), 0)
        self.assertEqual(helper.call_args.kwargs["gate_path"], self.gate)
        self.assertEqual(helper.call_args.kwargs["image"], IMAGE)
        marker = war3.private_object(destination / war3.MARKER)
        self.assertEqual(marker["instance_dir"], str(destination))
        self.assertRegex(marker["owner"], r"^[0-9a-f]{32}$")
        self.assertEqual((destination / war3.MARKER).stat().st_mode & 0o777, 0o600)
        before = (self.instance / "deployment.json").read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(war3.main([*args[:2], str(self.instance), *args[3:]]), 2)
        self.assertEqual((self.instance / "deployment.json").read_bytes(), before)

    def test_default_data_root_is_under_home_for_every_command(self):
        expected = Path.home() / ".war3-dedicated" / "instance"
        self.assertEqual(war3.DEFAULT_INSTANCE, expected)
        self.assertNotEqual(war3.DEFAULT_INSTANCE.parent, war3.ROOT / "local")
        for action in ("setup", "account", "start", "stop", "restart", "status", "logs"):
            args = [action, "--gate", str(self.gate)] if action == "setup" else [action]
            self.assertEqual(war3.parser().parse_args(args).instance_dir, expected)

    def test_default_setup_creates_private_home_parent_without_changing_checkout(self):
        home = self.root / "owned-home"
        home.mkdir(mode=0o700)
        checkout = self.root / "group-writable-checkout"
        checkout.mkdir()
        checkout.chmod(0o2775)
        default = home / ".war3-dedicated" / "instance"
        def prepared(instance, *args, **kwargs):
            self.assertEqual(instance, default)
            self.assertEqual(instance.parent.stat().st_mode & 0o777, 0o700)
            war3.bootstrap.new_destination(instance).mkdir(mode=0o700)
            (instance / "control").mkdir(mode=0o700)
            return instance
        args = ["setup", "--noninteractive", "--gate", str(self.gate), "--star-dir", "/explicit/star",
                "--game-dir", "/explicit/game", "--map", "/explicit/map.w3x"]
        with patch.object(war3, "DEFAULT_INSTANCE", default), patch.object(war3, "ROOT", checkout), patch.object(war3.bootstrap, "setup", side_effect=prepared), redirect_stdout(io.StringIO()):
            self.assertEqual(war3.main(args), 0)
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(default.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(checkout.stat().st_mode & 0o7777, 0o2775)
        self.assertFalse((checkout / "local").exists())
        self.assertEqual(war3.private_object(default / war3.MARKER)["instance_dir"], str(default))

    def test_unsafe_parent_errors_give_a_fixed_private_home_hint_without_paths(self):
        unsafe = self.root / "PRIVATE_PATH_MUST_NOT_BE_ECHOED"
        unsafe.mkdir()
        unsafe.chmod(0o2775)
        private = unsafe / "private-parent"
        private.mkdir(mode=0o700)
        for destination in (unsafe / "instance", private / "instance"):
            args = ["setup", "--instance-dir", str(destination), "--noninteractive", "--gate", str(self.gate),
                    "--star-dir", "/explicit/star", "--game-dir", "/explicit/game", "--map", "/explicit/map.w3x"]
            error = io.StringIO()
            with self.subTest(destination=destination), redirect_stderr(error):
                self.assertEqual(war3.main(args), 2)
            self.assertIn("Choose a private data path under your home", error.getvalue())
            self.assertNotIn("PRIVATE_PATH_MUST_NOT_BE_ECHOED", error.getvalue())
            self.assertFalse(destination.exists())
        self.assertEqual(unsafe.stat().st_mode & 0o7777, 0o2775)
        self.assertEqual(self.docker.calls, [])

    def test_parent_error_hint_requires_an_exact_fixed_message(self):
        args = ["setup", "--instance-dir", str(self.root / "new"), "--noninteractive", "--gate", str(self.gate),
                "--star-dir", "/explicit/star", "--game-dir", "/explicit/game", "--map", "/explicit/map.w3x"]
        error = io.StringIO()
        with patch.object(war3.bootstrap, "setup", side_effect=ValueError("unsafe destination ancestor: SECRET_MUST_NOT_BE_ECHOED")), redirect_stderr(error):
            self.assertEqual(war3.main(args), 2)
        self.assertNotIn("Choose a private data path", error.getvalue())
        self.assertNotIn("SECRET_MUST_NOT_BE_ECHOED", error.getvalue())

    def test_raw_docker_failure_output_is_never_printed(self):
        self.docker.fail_run = True
        code, output, error = self.call("start")
        self.assertEqual(code, 2)
        self.assertNotIn("PASSWORD_MUST_NOT_BE_PRINTED", output + error)

    def test_setup_marker_failure_cleans_only_the_fresh_destination(self):
        destination = self.root / "new-with-failure"
        def prepared(instance, *args, **kwargs):
            instance.mkdir(mode=0o700)
            (instance / "control").mkdir(mode=0o700)
            return instance
        args = ["setup", "--instance-dir", str(destination), "--noninteractive", "--gate", str(self.gate),
                "--star-dir", "/explicit/star", "--game-dir", "/explicit/game", "--map", "/explicit/map.w3x"]
        with patch.object(war3.bootstrap, "setup", side_effect=prepared), patch.object(war3.production, "private_json", side_effect=OSError("synthetic disk failure")), redirect_stderr(io.StringIO()):
            self.assertEqual(war3.main(args), 2)
        self.assertFalse(destination.exists())
        self.assertTrue((self.instance / "deployment.json").is_file())

    def test_actual_account_cli_stores_synthetic_input_without_printing_it(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        output, error = Terminal(), Terminal()
        with patch.object(sys.stdin, "isatty", return_value=True), redirect_stdout(output), redirect_stderr(error), patch("builtins.input", return_value="SyntheticUser"), patch.object(war3.account.getpass, "getpass", side_effect=["SYNTHETIC_HIDDEN_PASSWORD", "SYNTHETIC_HIDDEN_PASSWORD"]), patch.object(war3.account, "generate", side_effect=AssertionError("account stores input only")):
            self.assertEqual(war3.main(["account", "--instance-dir", str(self.instance)]), 0)
        self.assertNotIn("SYNTHETIC_HIDDEN_PASSWORD", output.getvalue() + error.getvalue())
        saved = war3.private_object(self.instance / "control/account-input.json")
        self.assertEqual(saved["username"], "SyntheticUser")
        self.assertEqual(saved["password"], "SYNTHETIC_HIDDEN_PASSWORD")
        self.assertEqual(self.docker.mutations(), [])

    def test_nonzero_stop_retains_container_and_blocks_restart(self):
        self.started()
        self.docker.stop_exit = 137
        self.assertEqual(self.call("restart")[0], 2)
        self.assertIsNotNone(self.docker.meta)
        self.assertFalse(self.docker.meta["State"]["Running"])
        self.assertEqual([a[0] if a[0] == "run" else a[1] for a in self.docker.mutations()], ["run", "stop"])

    def test_root_and_credential_arguments_are_rejected(self):
        with patch.object(os, "getuid", return_value=0):
            self.assertEqual(self.call("start")[0], 2)
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            war3.main(["account", "--password", "NEVER_ACCEPT_SECRET_ARGUMENT"])
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn("NEVER_ACCEPT_SECRET_ARGUMENT", stderr.getvalue())
        self.assertEqual(self.docker.calls, [])


if __name__ == "__main__":
    unittest.main()
