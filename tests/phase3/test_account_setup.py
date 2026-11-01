"""Synthetic, offline account setup regression; Docker is always a source-actor stub."""
from contextlib import ExitStack, redirect_stdout
import configparser
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "amp"))
import account
import production

IMAGE = "sha256:" + "1" * 64
SYNTHETIC = {"version": 1, "username": "SyntheticAccount", "password": "synthetic-unit-only"}


def acceptance():
    return {"schema_version": 1, "image_id": IMAGE, "approved_by_main": True,
            "source_sha256": {"src/bridge/star_profile.py": "a" * 64},
            "checks": {key: "PASS" for key in production.CHECKS}}


def synthetic_profile(path, username="PreviousSynthetic", marker="1"):
    """Format-only fixture; no hashing, authentication or vendor application."""
    path.mkdir(mode=0o700, exist_ok=True)
    saved = json.dumps(json.dumps({username: marker * 74}, separators=(",", ":")))
    text = ('[General]\nAutoLogin=true\nUsername=' + json.dumps(username)
            + '\nPassword=\nListLogins=' + saved + '\n'
            + account.loopback_defaults().split('\n', 1)[1])
    for name, value in (("launcher.ini", text), ("iccup.ini",
            "[Common]\nAutoUpdate=0\nAutoUpdateMap=0\nRunAtStart=0\n"
            "[Warcraft III]\nMainExe=Z:/work/game/war3.exe\nAutoLoginOn=1\n")):
        (path / name).write_text(value)
        (path / name).chmod(0o600)


class ImportContextTests(unittest.TestCase):
    def run_fresh_context(self, context):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            production.private_json(work / "gate.json", acceptance())
            production.private_json(work / "supervisor.json", {"mode": "offline", "http": {"host": "127.0.0.1", "port": 8080}})
            (work / "ghost.cfg").write_text("bot_hostport = 6113\n")
            (work / "map.cfg").write_text("map_localpath = Synthetic.w3x\n")
            instance = work / "instance"
            if context == "flat":
                production.prepare(instance, IMAGE, work / "gate.json", work / "supervisor.json",
                                   work / "ghost.cfg", work / "map.cfg")
                import_root = instance / "control"  # Actual installed files, not a mock package.
            else:
                import_root = ROOT  # Deliberately no repository/amp path.
            script = r'''
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
from unittest.mock import patch

repository, import_root, work = map(Path, sys.argv[1:4])
context = sys.argv[4]
sys.path.insert(0, str(import_root))
assert str(repository / "amp") not in sys.path
if context == "package":
    from amp import production, account
    assert production.__package__ == account.__package__ == "amp"
    assert "production" not in sys.modules and "account" not in sys.modules
else:
    import production
    import account
    assert not production.__package__ and not account.__package__
    assert Path(production.__file__).parent == import_root
assert production.account is account
assert account._production_module() is production

instance = work / "instance"
with patch.object(production, "docker_output", side_effect=AssertionError("Docker forbidden")), \
        patch.object(production, "docker_json", side_effect=AssertionError("Docker forbidden")):
    if context == "package":
        production.prepare(instance, "sha256:" + "1" * 64, work / "gate.json", work / "supervisor.json",
                           work / "ghost.cfg", work / "map.cfg")
    production.initialize_preset_editor(instance)
    assert production.read_json(instance / production.PRESET_EDITOR) == {"version": 1, "preset_definitions": ""}
    assert (instance / account.INPUT).stat().st_mode & 0o777 == 0o600
    spec = production.deployment(instance)
    command = account.generator_command(instance, spec, instance / "control/war3-account-synthetic")
    assert command[:len(production.DOCKER)] == production.DOCKER
    assert command[command.index("--user") + 1] == f"{os.geteuid()}:{os.getegid()}"
    assert command[command.index("--network") + 1] == "none"
    with patch.object(production, "image_preflight") as image_check:
        with account.profile_update(instance, spec):
            pass
        image_check.assert_called_once_with(instance, spec)
    error = account.AccountError("Enter both account username and password, or clear both to keep the saved profile.")
    with patch.object(production, "main", side_effect=error), redirect_stdout(io.StringIO()) as output:
        assert production.cli([]) == 2
    assert "enter both username and password" in output.getvalue()
    with patch.object(account.getpass, "getpass", side_effect=AssertionError("interactive input forbidden")), \
            redirect_stdout(io.StringIO()):
        assert account.cli(["--instance-dir", str(instance)]) == 2
print("IMPORT_CONTEXT_OK")
'''
            result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                                     str(ROOT), str(import_root), str(work), context],
                                    cwd=work, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "IMPORT_CONTEXT_OK")

    def test_fresh_root_only_package_prepare_and_account_imports(self):
        self.run_fresh_context("package")

    def test_fresh_installed_flat_control_imports(self):
        self.run_fresh_context("flat")


class ContractTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("WAR3_SYNTHETIC_LAUNCHER"), "requires own read-only synthetic Launcher fixture")
    def test_accepted_image_formatter_with_real_library_and_synthetic_readonly_launcher(self):
        launcher = Path(os.environ["WAR3_SYNTHETIC_LAUNCHER"])
        self.assertEqual(launcher.read_bytes(), b"SYNTHETIC_NOT_PE_ACCOUNT_TEST\n")
        self.assertFalse(os.access(launcher, os.W_OK))
        spec = importlib.util.spec_from_file_location("src.bridge.accepted_profile_fixture",
                                                     "/opt/war3/src/bridge/star_profile.py")
        formatter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(formatter)
        from src.bridge.star_relay import profile_username, require_loopback_profile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "profile"
            output.mkdir(mode=0o700)
            secret = root / "canonical.json"
            production.private_json(secret, SYNTHETIC)
            defaults = root / "defaults.ini"
            defaults.write_text(account.loopback_defaults())
            # Only the fixture's public Launcher identity check is stubbed.
            # Hashing uses the accepted image's actual libbncsutil and formatter.
            with patch.object(formatter, "LAUNCHER_SHA256", hashlib.sha256(launcher.read_bytes()).hexdigest()):
                formatter.provision(secret, output, defaults, launcher)
            account.verify_generated(output, SYNTHETIC["username"])
            require_loopback_profile(output / "launcher.ini", 16112)
            self.assertEqual(profile_username(output / "launcher.ini"), SYNTHETIC["username"])
            self.assertNotIn(SYNTHETIC["password"], (output / "launcher.ini").read_text())

    def test_generator_is_bounded_offline_and_uses_only_four_scoped_binds(self):
        instance = Path("/AMP/war3-production")
        spec = {"image_id": IMAGE, "host_instance_dir": "/host/war3-production",
                "uid": os.geteuid(), "gid": os.getegid()}
        argv = account.generator_command(instance, spec, instance / "control/war3-account-fixture")
        for key, expected in (("--network", "none"), ("--user", f'{spec["uid"]}:{spec["gid"]}'),
                ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges:true"),
                ("--entrypoint", "python3"), ("--log-driver", "none"),
                ("--pids-limit", "32"), ("--memory", "128m"), ("--cpus", "0.5")):
            self.assertEqual(argv[argv.index(key) + 1], expected)
        for flag in ("--read-only", "--no-healthcheck", "core=0", "--pull=never", "--restart=no"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index(IMAGE) + 1:argv.index(IMAGE) + 3], ["-m", "src.bridge.star_profile"])
        mounts = [argv[i + 1] for i, token in enumerate(argv) if token == "--mount"]
        self.assertEqual(len(mounts), 4)
        self.assertEqual(sum(m.endswith(",readonly") for m in mounts), 3)
        for mount in mounts:
            self.assertIn("src=/host/war3-production/", mount)
            self.assertNotIn("src=/AMP/", mount)
            self.assertNotIn("account-input.json", mount)
            self.assertNotIn("docker.sock", mount)
        self.assertTrue(any("canonical.json,dst=/account/input.json,readonly" in m for m in mounts))
        self.assertTrue(any(m.endswith("dst=/account/profile") for m in mounts))
        runtime = production.command(instance, {**spec, "container_name": "war3-phase3-staging", "uid": 1001, "gid": 1001})
        runtime_mounts = [runtime[i + 1] for i, token in enumerate(runtime) if token == "--mount"]
        self.assertFalse(any("/control" in m or "canonical" in m or "account-input" in m for m in runtime_mounts))
        self.assertFalse(any(secret in " ".join(argv + runtime) for secret in SYNTHETIC.values() if isinstance(secret, str)))

    def test_generator_uses_each_deployment_owner_without_changing_process_privileges(self):
        instance = Path("/AMP/war3-production")
        for uid, gid in ((1000, 1000), (1001, 1001), (42042, 43043)):
            with self.subTest(uid=uid, gid=gid), \
                    patch.object(account.os, "geteuid", return_value=uid), \
                    patch.object(account.os, "getegid", return_value=gid):
                spec = {"image_id": IMAGE, "uid": uid, "gid": gid}
                argv = account.generator_command(instance, spec, instance / "control/war3-account-fixture")
                self.assertEqual(argv[argv.index("--user") + 1], f"{uid}:{gid}")

    def test_generator_rejects_root_mismatched_owners_and_invalid_identity_types(self):
        for actual in ((1000, 1000), (1001, 1001), (0, 0)):
            with patch.object(account.os, "geteuid", return_value=actual[0]), \
                    patch.object(account.os, "getegid", return_value=actual[1]):
                cases = ((0, 0), (1000, 1001), (1001, 1000), (True, True), ("1000", 1000), (-1, 1000))
                for uid, gid in cases:
                    with self.subTest(actual=actual, uid=uid, gid=gid), self.assertRaises(account.AccountError):
                        account.generator_owner({"uid": uid, "gid": gid})

    def test_loopback_defaults_cover_both_gateway_lists_and_selected_index(self):
        cfg = configparser.RawConfigParser()
        cfg.optionxform = str
        cfg.read_string(account.loopback_defaults())
        self.assertEqual(cfg.sections(), ["General"])
        general = cfg["General"]
        self.assertEqual(general["Connections"], "")
        self.assertEqual(general["CurrConnections"], "0")
        for key in ("SiteConnections", "UserConnections"):
            qt = json.loads(general[key])
            self.assertEqual(set(json.loads(qt[len('@ByteArray('):-1]).values()), {"127.0.0.1:16112"})

    def test_partial_inputs_types_controls_size_and_unknown_fields_fail_closed(self):
        cases = [{**SYNTHETIC, "username": ""}, {**SYNTHETIC, "password": ""},
                 {**SYNTHETIC, "version": True}, {**SYNTHETIC, "extra": True},
                 {**SYNTHETIC, "password": "x" * 65}, {**SYNTHETIC, "username": "й" * 33},
                 {**SYNTHETIC, "password": "x\n"}, {**SYNTHETIC, "password": "x\x00"},
                 {**SYNTHETIC, "username": "\ud800"}, {**SYNTHETIC, "password": None}]
        for value in cases:
            with self.assertRaises(account.AccountError):
                account.validate(value)
        self.assertEqual(account.validate({"version": 1, "username": "", "password": ""})["username"], "")

    def test_map_preset_is_a_string_and_never_an_auth_or_native_setting(self):
        self.assertEqual(production.settings({k: v for k, v in production.DEFAULTS.items() if k != "map_preset"})["map_preset"], "manual")
        self.assertEqual(production.settings({**production.DEFAULTS, "map_preset": "dota-safe_1"})["map_preset"], "dota-safe_1")
        for value in (1, None, True, "", "../secret", "x/y", "x\ny", "x" * 65):
            with self.assertRaises(ValueError):
                production.settings({**production.DEFAULTS, "map_preset": value})
        for key in ("username", "password"):
            with self.assertRaises(ValueError):
                production.settings({**production.DEFAULTS, key: "synthetic"})

    def test_internal_autostart_backcompat_and_disabled_thresholds(self):
        old = {k: v for k, v in production.DEFAULTS.items() if k not in {"autostart_enabled", "map_preset"}}
        self.assertTrue(production.settings(old)["autostart_enabled"])
        cfg = {**production.DEFAULTS, "max_players": 3, "vote_min_players": 3, "autostart_players": 10,
               "autostart_enabled": False}
        self.assertFalse(production.settings(cfg)["autostart_enabled"])
        with self.assertRaises(ValueError):
            production.settings({**cfg, "autostart_enabled": True})
        for change in ({"vote_min_players": 4}, {"autostart_players": 13}):
            with self.assertRaises(ValueError):
                production.settings({**cfg, **change})
        self.assertEqual(production.settings({**production.DEFAULTS, "autostart_players": 2, "vote_min_players": 3})["vote_min_players"], 3)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "instance"
        self.root.mkdir(mode=0o700)
        for name in ("control", "vendor", "vendor/star", "vendor/profile", "data", "data/config", "data/maps"):
            (self.root / name).mkdir(mode=0o700)
        self.profile = self.root / "vendor/profile"
        synthetic_profile(self.profile)
        self.before = {p.name: p.read_bytes() for p in self.profile.iterdir()}
        self.spec = {"schema_version": 1, "image_id": IMAGE, "container_name": "war3-phase3-staging",
                     "uid": os.geteuid(), "gid": os.getegid()}
        production.private_json(self.root / "deployment.json", self.spec)
        production.private_json(self.root / "acceptance.json", acceptance())
        production.private_json(self.root / account.INPUT, SYNTHETIC)
        production.private_json(self.root / "data/config/amp-settings.json", production.DEFAULTS)
        production.private_json(self.root / "data/config/supervisor.json", {"mode": "iccup", "http": {"host": "127.0.0.1", "port": 8080}})
        (self.root / "vendor/star/Launcher.exe").write_text("SYNTHETIC_NOT_PE\n")
        self.metadata = {"Id": IMAGE, "Architecture": "amd64", "Os": "linux",
                         "Config": {"Labels": {"war3.production.source-digest": production.gate(acceptance(), IMAGE)}}}
        self.calls = []
        self.events = []
        self.failure = None
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(production, "docker_json", side_effect=lambda args: [self.metadata]))
        self.stack.enter_context(patch.object(production, "docker_output", side_effect=self.ps))
        self.stack.enter_context(patch.object(account.subprocess, "run", side_effect=self.docker))
        # No Docker socket, ownership patch, chown or privilege change is used.
        # The source actor writes files as the actual non-root test data owner.

    def ps(self, args):
        self.assertEqual(args[:2], ["ps", "-aq"])
        self.events.append("stopped")
        return b""

    def docker(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        if "run" in argv:
            self.assertEqual(argv[argv.index("--user") + 1], f'{self.spec["uid"]}:{self.spec["gid"]}')
            self.events.append("generate")
            mounts = [argv[i + 1] for i, token in enumerate(argv) if token == "--mount"]
            canonical = Path(next(m.split(",dst=", 1)[0].split("src=", 1)[1] for m in mounts if "dst=/account/input.json," in m))
            self.canonical = canonical
            self.assertEqual(account.private_read(canonical), (canonical).read_bytes())
            self.assertEqual(json.loads(canonical.read_text()), SYNTHETIC)
            self.assertTrue(canonical.is_relative_to(self.root / "control"))
            self.assertNotEqual(canonical, self.root / account.INPUT)
            self.assertEqual(stat.S_IMODE(canonical.parent.stat().st_mode), 0o700)
            if self.failure == "generator":
                raise subprocess.CalledProcessError(78, argv, output=b"synthetic-parser-private", stderr=b"synthetic-parser-private")
            if self.failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 45)
            if self.failure == "interrupt":
                raise KeyboardInterrupt()
            if self.failure == "terminate":
                os.kill(os.getpid(), signal.SIGTERM)
            synthetic_profile(canonical.parent / "profile", SYNTHETIC["username"], "2")
            if self.failure == "output-mode":
                (canonical.parent / "profile/launcher.ini").chmod(0o644)
            if self.failure == "output-gateway":
                launcher = canonical.parent / "profile/launcher.ini"
                launcher.write_text(launcher.read_text().replace("127.0.0.1:16112", "192.0.2.1:6112"))
        else:
            self.assertEqual(argv[len(production.DOCKER):len(production.DOCKER) + 2], ["rm", "--force"])
        return subprocess.CompletedProcess(argv, 0)

    def update(self):
        with account.profile_update(self.root, self.spec):
            self.events.append("preflight")

    def unchanged(self):
        self.assertEqual({p.name: p.read_bytes() for p in self.profile.iterdir()}, self.before)
        self.assertEqual(list((self.root / "control").glob("war3-account-*")), [])

    def test_success_retains_live_canonical_outside_data_and_installs_private_profile(self):
        canonical_before = (self.root / account.INPUT).read_bytes()
        self.update()
        self.assertEqual((self.root / account.INPUT).read_bytes(), canonical_before)
        self.assertFalse(self.canonical.exists())
        self.assertEqual(self.events, ["stopped", "generate", "stopped", "preflight"])
        account.verify_generated(self.profile, SYNTHETIC["username"])
        for path in (self.profile, self.root / account.BACKUP):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual({p.name for p in path.iterdir()}, account.FILES)
            self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in path.iterdir()))
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / account.BACKUP).iterdir()}, self.before)
        for name in ("amp-settings.json", "supervisor.json"):
            data = (self.root / "data/config" / name).read_text()
            self.assertNotIn('"username"', data)
            self.assertNotIn('"password"', data)
            self.assertNotIn(SYNTHETIC["password"], data)

    def test_first_setup_accepts_empty_or_absent_profile(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                shutil.rmtree(self.profile)
                if not missing:
                    self.profile.mkdir(mode=0o700)
                self.update()
                account.verify_generated(self.profile, SYNTHETIC["username"])

    def test_repeated_setup_keeps_at_most_one_previous_profile(self):
        for _ in range(3):
            self.update()
        self.assertEqual([p.name for p in (self.root / "control").iterdir() if p.is_dir()], ["account-profile.previous"])

    def test_empty_or_missing_unconfigured_fields_preserve_existing_profile(self):
        for missing in (False, True):
            if missing:
                (self.root / account.INPUT).unlink()
            else:
                production.private_json(self.root / account.INPUT, {"version": 1, "username": "", "password": ""})
            self.update()
            self.unchanged()
        self.assertFalse(self.calls)

    def test_changed_username_without_password_rejects_instead_of_falling_back(self):
        production.private_json(self.root / account.INPUT, {**SYNTHETIC, "username": "NewSynthetic", "password": ""})
        with self.assertRaises(account.AccountError):
            self.update()
        self.assertFalse(self.calls)
        self.unchanged()

    def test_duplicate_and_malformed_json_are_redacted_and_fail_before_docker(self):
        for data in ('{"version":1,"username":"x","username":"y","password":"z"}', '{synthetic-private'):
            (self.root / account.INPUT).write_text(data)
            with self.assertRaises(account.AccountError) as error:
                self.update()
            self.assertNotIn("synthetic-private", str(error.exception))
        self.assertFalse(self.calls)
        self.unchanged()

    def test_failed_generation_timeout_and_interrupt_clean_canonical_and_keep_old_profile(self):
        for failure, error in (("generator", subprocess.CalledProcessError), ("timeout", subprocess.TimeoutExpired),
                               ("interrupt", KeyboardInterrupt), ("output-mode", account.AccountError),
                               ("output-gateway", account.AccountError)):
            with self.subTest(failure=failure):
                self.failure = failure
                with self.assertRaises(error):
                    self.update()
                self.assertFalse(self.canonical.exists())
                self.assertIn("rm", self.calls[-1])
                self.unchanged()

    def test_preflight_failure_rolls_back_atomic_exchange(self):
        with self.assertRaises(ValueError):
            with account.profile_update(self.root, self.spec):
                account.verify_generated(self.profile, SYNTHETIC["username"])
                raise ValueError("synthetic-preflight-failure")
        self.unchanged()

    def test_amp_sigterm_cleans_canonical_and_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.failure = "terminate"
        with self.assertRaises(SystemExit) as error:
            self.update()
        self.assertEqual(error.exception.code, 143)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertFalse(self.canonical.exists())
        self.assertIn("rm", self.calls[-1])
        self.unchanged()

    def test_sigterm_at_exchange_is_deferred_until_rollback_state_is_recorded(self):
        original = account.exchange
        count = 0
        def exchange(left, right):
            nonlocal count
            original(left, right)
            count += 1
            if count == 1:
                os.kill(os.getpid(), signal.SIGTERM)
        with patch.object(account, "exchange", side_effect=exchange):
            with self.assertRaises(SystemExit):
                self.update()
        self.assertEqual(count, 2)
        self.unchanged()

    def test_rollback_filesystem_failure_preserves_old_profile_in_private_backup(self):
        original = account.exchange
        count = 0
        def exchange(left, right):
            nonlocal count
            count += 1
            if count > 1:
                raise OSError("synthetic-rollback-failure")
            original(left, right)
        with patch.object(account, "exchange", side_effect=exchange):
            with self.assertRaises(account.AccountError):
                with account.profile_update(self.root, self.spec):
                    raise ValueError("synthetic-preflight-failure")
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / account.BACKUP).iterdir()}, self.before)
        self.assertEqual(list((self.root / "control").glob("war3-account-*")), [])

    def test_rollback_and_backup_filesystem_failures_keep_old_staging_without_canonical(self):
        original_exchange = account.exchange
        original_replace = os.replace
        count = 0
        def exchange(left, right):
            nonlocal count
            count += 1
            if count > 1:
                raise OSError("synthetic-rollback-failure")
            original_exchange(left, right)
        def replace(source, target):
            if Path(target) == self.root / account.BACKUP:
                raise OSError("synthetic-backup-failure")
            return original_replace(source, target)
        with patch.object(account, "exchange", side_effect=exchange), \
                patch.object(account.os, "replace", side_effect=replace):
            with self.assertRaises(account.ProfileRecoveryError):
                with account.profile_update(self.root, self.spec):
                    raise ValueError("synthetic-preflight-failure")
        retained = list((self.root / "control").glob("war3-account-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual({p.name for p in retained[0].iterdir()}, {"profile"})
        self.assertEqual(stat.S_IMODE(retained[0].stat().st_mode), 0o700)
        self.assertEqual({p.name: p.read_bytes() for p in (retained[0] / "profile").iterdir()}, self.before)
        self.assertFalse(self.canonical.exists())
        count = len(self.calls)
        with self.assertRaises(account.AccountError):
            self.update()
        self.assertEqual(len(self.calls), count)

    def test_first_profile_is_removed_if_preflight_fails(self):
        shutil.rmtree(self.profile)
        with self.assertRaises(ValueError):
            with account.profile_update(self.root, self.spec):
                raise ValueError("synthetic-preflight-failure")
        self.assertFalse(self.profile.exists())

    def test_exchange_failure_keeps_original_profile(self):
        with patch.object(account, "exchange", side_effect=OSError("synthetic-filesystem")):
            with self.assertRaises(OSError):
                self.update()
        self.unchanged()

    def test_backup_move_failure_restores_original_profile(self):
        original = os.replace
        def replace(source, target):
            if Path(target) == self.root / account.BACKUP:
                raise OSError("synthetic-backup-failure")
            return original(source, target)
        with patch.object(account.os, "replace", side_effect=replace):
            with self.assertRaises(OSError):
                self.update()
        self.unchanged()

    def test_failure_after_backup_move_rolls_back_and_removes_new_backup(self):
        original = account.sync_directory
        def sync(path):
            if Path(path) == self.root / "control":
                raise OSError("synthetic-directory-sync-failure")
            return original(path)
        with patch.object(account, "sync_directory", side_effect=sync):
            with self.assertRaises(OSError):
                self.update()
        self.unchanged()
        self.assertFalse((self.root / account.BACKUP).exists())

    def test_both_old_and_new_profile_inodes_remain_locked_during_preflight(self):
        previous = os.open(self.profile / "launcher.ini", os.O_RDONLY)
        try:
            with account.profile_update(self.root, self.spec):
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(previous, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (self.profile / "launcher.ini").open("rb") as active:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(previous)

    def test_profile_and_setup_locks_reject_without_generating(self):
        for path in (self.profile / "launcher.ini", self.root / "control/account-setup.lock"):
            with self.subTest(path=path.name):
                path.touch(mode=0o600, exist_ok=True)
                with path.open("rb") as active:
                    fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaises(account.AccountError):
                        self.update()
        self.assertFalse(self.calls)
        self.unchanged()

    def test_active_or_foreign_named_container_rejects_before_reading_input(self):
        with patch.object(production, "docker_output", return_value=b"foreign-or-active\n"), \
                patch.object(account, "read_input") as read:
            with self.assertRaises(ValueError):
                self.update()
            read.assert_not_called()
        self.assertFalse(self.calls)
        self.unchanged()

    def test_container_appearing_during_generation_rejects_publication(self):
        with patch.object(production, "docker_output", side_effect=[b"", b"active\n"]):
            with self.assertRaises(ValueError):
                self.update()
        self.assertTrue(self.calls)
        self.unchanged()

    def test_bad_gate_and_image_identity_reject_before_input_or_generation(self):
        mutations = (("Id", "sha256:" + "2" * 64), ("Architecture", "arm64"), ("Os", "windows"),
                     ("Config", {"Labels": {"war3.production.source-digest": "b" * 64}}))
        for key, value in mutations:
            old = self.metadata[key]
            self.metadata[key] = value
            with self.assertRaises(ValueError):
                self.update()
            self.metadata[key] = old
        production.private_json(self.root / "acceptance.json", {**acceptance(), "approved_by_main": False})
        with self.assertRaises(ValueError):
            self.update()
        self.assertFalse(self.calls)
        self.unchanged()

    def test_supplied_spec_cannot_bypass_deployment_identity(self):
        with self.assertRaises(account.AccountError):
            with account.profile_update(self.root, {**self.spec, "uid": self.spec["uid"] + 1}):
                pass
        self.assertFalse(self.calls)
        self.unchanged()

    def test_wrong_generator_owner_policy_rejects_before_generation(self):
        original = dict(self.spec)
        for key in ("uid", "gid"):
            self.spec = {**original, key: original[key] + 1}
            production.private_json(self.root / "deployment.json", self.spec)
            with self.assertRaises(account.AccountError):
                self.update()
        self.assertFalse(self.calls)
        self.unchanged()

    def test_input_permissions_hardlinks_and_symlinks_are_rejected(self):
        path = self.root / account.INPUT
        path.chmod(0o644)
        with self.assertRaises(account.AccountError):
            self.update()
        path.chmod(0o600)
        extra = self.root / "control/extra"
        os.link(path, extra)
        with self.assertRaises(account.AccountError):
            self.update()
        extra.unlink()
        path.rename(extra)
        path.symlink_to(extra)
        with self.assertRaises(account.AccountError):
            self.update()
        self.assertFalse(self.calls)
        self.unchanged()

    def test_input_fifo_oversize_directory_and_control_permissions_reject(self):
        path = self.root / account.INPUT
        path.write_bytes(b"x" * 4097)
        with self.assertRaises(account.AccountError):
            self.update()
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaises(account.AccountError):
            self.update()
        path.unlink()
        path.mkdir(mode=0o700)
        with self.assertRaises(account.AccountError):
            self.update()
        path.rmdir()
        production.private_json(path, SYNTHETIC)
        (self.root / "control").chmod(0o755)
        with self.assertRaises(account.AccountError):
            self.update()
        (self.root / "control").chmod(0o700)
        self.assertFalse(self.calls)
        self.unchanged()

    def test_symlinked_control_parent_or_profile_is_rejected(self):
        for target in (self.root / "control", self.profile):
            other = target.with_name(target.name + "-real")
            target.rename(other)
            target.symlink_to(other, target_is_directory=True)
            try:
                with self.assertRaises(account.AccountError):
                    self.update()
            finally:
                target.unlink()
                other.rename(target)
        self.assertFalse(self.calls)
        self.unchanged()

    def test_malicious_old_profile_or_backup_cannot_be_followed(self):
        path = self.profile / "iccup.ini"
        path.chmod(0o644)
        with self.assertRaises(account.AccountError):
            self.update()
        path.chmod(0o600)
        (self.root / account.BACKUP).symlink_to(self.profile, target_is_directory=True)
        with self.assertRaises(account.AccountError):
            self.update()
        self.assertFalse(self.calls)
        self.unchanged()

    def test_main_runs_setup_before_full_preflight_and_execs_after_commit(self):
        self.assertFalse((self.root / production.PRESETS).exists())
        def full_preflight(instance, spec):
            self.events.append("full-preflight")
            account.verify_generated(self.profile, SYNTHETIC["username"])
        def execute(*args):
            self.events.append("exec")
            self.assertTrue((self.root / account.BACKUP).is_dir())
            self.assertFalse(self.canonical.exists())
            with account.locked(self.profile / "launcher.ini"):
                pass
        with patch.object(production, "preflight", side_effect=full_preflight), \
                patch.object(production.os, "execvpe", side_effect=execute):
            self.assertIsNone(production.main(["run", "--instance-dir", str(self.root)]))
        self.assertEqual(self.events, ["stopped", "generate", "stopped", "full-preflight", "exec"])
        config = production.read_json(self.root / "data/config/supervisor.json")
        self.assertEqual(config["gameplay"]["host_name"], "War3HostBot")
        self.assertEqual(config["gameplay"]["map_preset"], "manual")
        self.assertNotIn("username", config["gameplay"])

    def test_run_rejects_missing_manual_disabled_or_unknown_selection_before_merge_or_spawn(self):
        document = {"version": 1, "presets": [
            {"id": "custom", "enabled": True, "map_filename": "Custom.w3x"},
            {"id": "disabled-map", "enabled": False, "map_filename": "Disabled.w3x"}]}
        production.private_json(self.root / production.PRESETS, document)
        before = (self.root / "data/config/supervisor.json").read_bytes()
        for selected in ("manual", "disabled-map", "unknown-map"):
            with self.subTest(selected=selected):
                production.private_json(self.root / "data/config/amp-settings.json",
                                        {**production.DEFAULTS, "map_preset": selected})
                with patch.object(production, "preflight") as preflight, \
                        patch.object(production, "merge_settings") as merge, \
                        patch.object(production.os, "execvpe") as execute, \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(production.cli(["run", "--instance-dir", str(self.root)]), 2)
                self.assertIn("Select an enabled preset", output.getvalue())
                self.assertNotIn(selected, output.getvalue())
                preflight.assert_not_called()
                merge.assert_not_called()
                execute.assert_not_called()
                self.assertEqual((self.root / "data/config/supervisor.json").read_bytes(), before)
                self.unchanged()

    def test_run_merges_validated_selection_snapshot_when_input_changes_during_preflight(self):
        production.private_json(self.root / production.PRESETS,
            {"version": 1, "presets": [{"id": "custom", "enabled": True, "map_filename": "Custom.w3x"}]})
        production.private_json(self.root / "data/config/amp-settings.json",
                                {**production.DEFAULTS, "map_preset": "custom"})
        def late_input_change(instance, spec):
            production.private_json(instance / "data/config/amp-settings.json", production.DEFAULTS)
        with patch.object(production, "preflight", side_effect=late_input_change), \
                patch.object(production.os, "execvpe") as execute:
            self.assertIsNone(production.main(["run", "--instance-dir", str(self.root)]))
        execute.assert_called_once()
        self.assertEqual(production.read_json(self.root / "data/config/supervisor.json")["gameplay"]["map_preset"], "custom")
        self.assertEqual(production.read_json(self.root / "data/config/amp-settings.json")["map_preset"], "manual")

    def test_main_merge_failure_rolls_profile_back_and_never_starts_runtime(self):
        production.private_json(self.root / "data/config/amp-settings.json", {**production.DEFAULTS, "password": "synthetic"})
        with patch.object(production, "preflight"), patch.object(production.os, "execvpe") as execute:
            self.assertEqual(production.cli(["run", "--instance-dir", str(self.root)]), 2)
            execute.assert_not_called()
        self.unchanged()

    def test_cli_redacts_generator_diagnostics_and_canonical_values(self):
        self.failure = "generator"
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(production.cli(["run", "--instance-dir", str(self.root)]), 2)
        for secret in (SYNTHETIC["username"], SYNTHETIC["password"], "synthetic-parser-private"):
            self.assertNotIn(secret, output.getvalue())
        self.unchanged()

    def test_pair_error_has_fixed_operator_message_without_echoing_input(self):
        production.private_json(self.root / account.INPUT, {**SYNTHETIC, "password": ""})
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(production.cli(["run", "--instance-dir", str(self.root)]), 2)
        self.assertIn("enter both username and password", output.getvalue())
        self.assertNotIn(SYNTHETIC["username"], output.getvalue())
        self.unchanged()

    def test_interactive_cli_uses_getpass_and_only_stores_control_input(self):
        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stderr, "isatty", return_value=True), \
                patch("builtins.input", return_value=SYNTHETIC["username"]), \
                patch.object(account.getpass, "getpass", side_effect=[SYNTHETIC["password"]] * 2), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(account.cli(["--instance-dir", str(self.root)]), 0)
        self.assertEqual(account.read_input(self.root), SYNTHETIC)
        self.assertFalse(self.calls)
        self.unchanged()


class PresetEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "application"
        self.root.mkdir(mode=0o700)
        for name in ("control", "data", "data/config", "vendor", "vendor/profile"):
            (self.root / name).mkdir(mode=0o700)
        self.manual = {"version": 1, "presets": [{"id": "manual", "enabled": True}]}
        self.desired = {"version": 1, "presets": [*self.manual["presets"],
            {"id": "synthetic-map", "enabled": True, "map_filename": "Synthetic.w3x", "lobby_name": 'Synthetic "Map"'},
            {"id": "disabled-map", "enabled": False, "map_filename": "Disabled.w3x"}]}
        production.private_json(self.root / production.PRESETS, self.manual)
        production.initialize_preset_editor(self.root)
        self.manifest = self.root.parent / "configmanifest.json"
        self.manifest.write_bytes((ROOT / "amp/war3-productionconfig.json").read_bytes())
        self.manifest.chmod(0o600)

    def edit(self, value):
        production.private_json(self.root / production.PRESET_EDITOR,
                                {"version": 1, "preset_definitions": json.dumps(value, indent=2) if value is not None else ""})

    def update(self):
        with production.preset_update(self.root) as check, production.preset_manifest_update(self.root, check):
            pass

    def external(self):
        return {"version": 1, "presets": [*self.manual["presets"],
            {"id": "file-manager-new", "enabled": True, "map_filename": "Other.w3x"}]}

    def test_applied_file_formats_short_settings_compactly(self):
        document = {"version": 1, "presets": [{"id": "example", "enabled": True,
                    "map_filename": "Map.w3x", "start": {"autostart_enabled": True,
                    "autostart_players": 4}, "vote": {"enabled": True, "min_players": 2}}]}
        self.edit(document)
        self.update()
        text = (self.root / production.PRESETS).read_text()
        self.assertEqual(json.loads(text), document)
        self.assertIn('"start": {"autostart_enabled": true, "autostart_players": 4}', text)
        self.assertIn('"vote": {"enabled": true, "min_players": 2}', text)

    def test_existing_container_has_actionable_error(self):
        with patch.object(production, "docker_output", return_value=b"existing-container"):
            with self.assertRaises(production.SettingsError) as error:
                production.stopped({"container_name": "war3-phase3-staging"})
        self.assertIn("container already exists", error.exception.public_message)

    def test_editor_can_omit_ai_approvals_without_removing_them(self):
        approved = {"version": 1, "presets": self.manual["presets"],
                    "capabilities": [{"id": "reviewed", "map_sha256": "a" * 64}]}
        production.private_json(self.root / production.PRESETS, approved)
        production.initialize_preset_editor(self.root)
        self.edit(self.desired)
        self.update()
        installed = production.read_json(self.root / production.PRESETS)
        self.assertEqual(installed["presets"], self.desired["presets"])
        self.assertEqual(installed["capabilities"], approved["capabilities"])
        baseline = production.read_json(self.root / production.PRESET_BASELINE)
        self.assertEqual(baseline["editor"], self.desired)
        self.assertEqual(baseline["file"], installed)

    def test_omitted_approvals_do_not_overwrite_file_manager_edits(self):
        approved = {"version": 1, "presets": self.manual["presets"],
                    "capabilities": [{"id": "reviewed"}]}
        production.private_json(self.root / production.PRESETS, approved)
        production.initialize_preset_editor(self.root)
        self.edit(self.desired)
        external = {**approved, "capabilities": [{"id": "external"}]}
        production.private_json(self.root / production.PRESETS, external)
        with self.assertRaises(production.SettingsError):
            self.update()
        self.assertEqual(production.read_json(self.root / production.PRESETS), external)

    def test_file_manager_save_during_failed_preflight_is_never_rolled_back(self):
        self.edit(self.desired)
        before = (self.root / production.PRESET_BASELINE).read_bytes()
        external = self.external()
        with self.assertRaises(production.SettingsError) as error:
            with production.preset_update(self.root):
                production.private_json(self.root / production.PRESETS, external)
                raise ValueError("preflight failed")
        self.assertEqual(error.exception.args, ("PRESET_EXTERNAL_CONFLICT",))
        self.assertEqual(production.read_json(self.root / production.PRESETS), external)
        recovery = self.root / production.PRESET_RECOVERY
        self.assertEqual(production.read_json(recovery / "previous.json"), self.manual)
        self.assertEqual(production.read_json(recovery / "external.json"), external)
        self.assertEqual((self.root / production.PRESET_BASELINE).read_bytes(), before)
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in recovery.iterdir()))
        with self.assertRaises(production.SettingsError):
            self.update()
        self.assertEqual(production.read_json(self.root / production.PRESETS), external)

    def test_file_manager_save_after_snapshot_before_write_is_preserved(self):
        self.edit(self.desired)
        external = self.external()
        restore = production._preset_restore
        injected = False
        def save_before_write(path, data, mode):
            nonlocal injected
            restore(path, data, mode)
            if path.name == "displaced.json" and not injected:
                injected = True
                production.private_json(self.root / production.PRESETS, external)
        with patch.object(production, "_preset_restore", side_effect=save_before_write):
            with self.assertRaises(production.SettingsError):
                self.update()
        self.assertTrue(injected)
        self.assertEqual(production.read_json(self.root / production.PRESETS), external)
        self.assertEqual(production.read_json(self.root / production.PRESET_RECOVERY / "external.json"), external)

    def test_file_manager_save_in_check_exchange_gap_is_retained_as_displaced_bytes(self):
        self.edit(self.desired)
        external = self.external()
        exchange = account.exchange
        injected = False
        def exchange_after_external_save(left, right):
            nonlocal injected
            if right == self.root / production.PRESETS and not injected:
                injected = True
                production.private_json(right, external)
            exchange(left, right)
        with patch.object(account, "exchange", side_effect=exchange_after_external_save):
            with self.assertRaises(production.SettingsError):
                self.update()
        recovery = self.root / production.PRESET_RECOVERY
        self.assertEqual(production.read_json(recovery / "displaced.json"), external)
        self.assertEqual(production.read_json(recovery / "previous.json"), self.manual)

    def test_file_manager_save_in_rollback_exchange_gap_is_retained(self):
        self.edit(self.desired)
        external = self.external()
        exchange = account.exchange
        count = 0
        def exchange_after_external_save(left, right):
            nonlocal count
            count += 1
            if count == 2:
                production.private_json(right, external)
            exchange(left, right)
        with patch.object(account, "exchange", side_effect=exchange_after_external_save):
            with self.assertRaises(production.SettingsError):
                with production.preset_update(self.root):
                    raise ValueError("preflight failed")
        self.assertEqual(count, 2)
        self.assertEqual(production.read_json(self.root / production.PRESET_RECOVERY / "displaced.json"), external)

    def test_displaced_file_manager_open_inode_survives_cleanup_and_late_write(self):
        self.edit(self.desired)
        exchange = account.exchange
        writer = None
        external = self.external()
        def race(left, right):
            nonlocal writer
            production.private_json(right, external)
            writer = os.open(right, os.O_WRONLY | os.O_APPEND)
            exchange(left, right)
        try:
            with patch.object(account, "exchange", side_effect=race):
                with self.assertRaises(production.SettingsError):
                    self.update()
            os.write(writer, b"\n ")
            os.fsync(writer)
            retained = self.root / production.PRESET_RECOVERY / "displaced.json"
            self.assertEqual(retained.stat().st_ino, os.fstat(writer).st_ino)
            self.assertTrue(retained.read_bytes().endswith(b"\n "))
            self.assertEqual(production.read_json(retained), external)
        finally:
            if writer is not None:
                os.close(writer)

    def test_manifest_generation_checks_accepted_preset_revision_and_reverts_on_external_save(self):
        self.edit(self.desired)
        original_manifest = self.manifest.read_bytes()
        save = production.private_json
        external = self.external()
        def external_save_before_manifest(path, value):
            if path == self.manifest:
                save(self.root / production.PRESETS, external)
            save(path, value)
        with patch.object(production, "private_json", side_effect=external_save_before_manifest):
            with self.assertRaises(production.SettingsError):
                self.update()
        self.assertEqual(production.read_json(self.root / production.PRESETS), external)
        self.assertEqual(self.manifest.read_bytes(), original_manifest)

    def test_custom_only_document_lists_only_created_enabled_ids_without_switching_selection(self):
        custom = {"version": 1, "presets": [{"id": "custom", "enabled": True, "map_filename": "Custom.w3x"}]}
        production.private_json(self.root / "data/config/amp-settings.json", production.DEFAULTS)
        settings_before = (self.root / "data/config/amp-settings.json").read_bytes()
        self.edit(custom)
        self.update()
        self.assertEqual(production.read_json(self.root / production.PRESETS), custom)
        self.assertEqual(set(production.preset_options(self.root)), {"custom"})
        fields = json.loads(self.manifest.read_bytes())
        selector = next(f for f in fields if f["ParamFieldName"] == "map_preset")
        self.assertEqual(set(selector["EnumValues"]), {"custom"})
        self.assertEqual(selector["DefaultValue"], "manual")
        self.assertEqual((self.root / "data/config/amp-settings.json").read_bytes(), settings_before)

    @unittest.skipUnless(os.environ.get("WAR3_PRESET_RUNTIME_PATH"), "requires read-only integrated runtime source fixture")
    def test_custom_only_document_matches_integrated_runtime_loader(self):
        source = Path(os.environ["WAR3_PRESET_RUNTIME_PATH"])
        spec = importlib.util.spec_from_file_location("preset_runtime_parity", source / "__init__.py",
                                                     submodule_search_locations=[str(source)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        runtime = importlib.import_module("preset_runtime_parity.presets")
        custom = {"version": 1, "presets": [{"id": "custom", "enabled": True, "map_filename": "Custom.w3x"}]}
        production.private_json(self.root / production.PRESETS, custom)
        presets, _ = runtime.load_presets(self.root / production.PRESETS)
        self.assertEqual(set(production.preset_options(self.root)), set(presets))

    def test_multiline_editor_body_becomes_json_object_and_custom_dropdown_choice(self):
        self.edit(self.desired)
        self.update()
        saved = production.read_json(self.root / production.PRESETS)
        self.assertEqual(saved, self.desired)
        self.assertIsInstance(saved, dict)
        self.assertEqual((self.root / production.PRESETS).stat().st_mode & 0o777, 0o600)
        fields = json.loads(self.manifest.read_bytes())
        selected = next(f for f in fields if f["ParamFieldName"] == "map_preset")
        self.assertEqual(set(selected["EnumValues"]), {"manual", "synthetic-map"})
        self.assertEqual(selected["EnumValues"]["synthetic-map"], 'Synthetic "Map" (synthetic-map)')
        original = json.loads((ROOT / "amp/war3-productionconfig.json").read_bytes())
        self.assertEqual([f for f in fields if f["ParamFieldName"] != "map_preset"],
                         [f for f in original if f["ParamFieldName"] != "map_preset"])
        for name in (production.PRESET_EDITOR, production.PRESET_BASELINE):
            self.assertTrue(str(name).startswith("control/"))
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_option_labels_are_bounded_printable_and_keep_stable_id_values(self):
        for name in ("Synthetic lobby", "unsafe\nlabel", "<markup>", "x" * 25, " padded ", 12, "\ud800"):
            document = {"version": 1, "presets": [*self.manual["presets"],
                        {"id": "synthetic-map", "enabled": True, "map_filename": "Synthetic.w3x", "lobby_name": name}]}
            production.private_json(self.root / production.PRESETS, document)
            options = production.preset_options(self.root)
            self.assertEqual(set(options), {"manual", "synthetic-map"})
            self.assertEqual(options["synthetic-map"], "Synthetic lobby (synthetic-map)" if name == "Synthetic lobby" else "synthetic-map")

    def test_blank_editor_preserves_file_manager_and_adopts_its_baseline(self):
        production.private_json(self.root / production.PRESETS, self.desired)
        before = (self.root / production.PRESETS).read_bytes()
        self.update()
        self.assertEqual((self.root / production.PRESETS).read_bytes(), before)
        state = production.read_json(self.root / production.PRESET_BASELINE)
        self.assertIsNone(state["editor"])
        self.assertEqual(state["file"], self.desired)

    def test_unchanged_stale_editor_keeps_file_manager_changes_and_options_follow_actual_file(self):
        self.edit(self.desired)
        self.update()
        external = {"version": 1, "presets": [*self.manual["presets"],
                    {"id": "file-manager-map", "enabled": True, "map_filename": "Other.w3x"}]}
        production.private_json(self.root / production.PRESETS, external)
        before = (self.root / production.PRESETS).read_bytes()
        self.update()
        self.assertEqual((self.root / production.PRESETS).read_bytes(), before)
        self.assertEqual(set(production.preset_options(self.root)), {"manual", "file-manager-map"})
        fields = json.loads(self.manifest.read_bytes())
        self.assertEqual(set(next(f for f in fields if f["ParamFieldName"] == "map_preset")["EnumValues"]),
                         {"manual", "file-manager-map"})

    def test_both_sources_changed_rejects_without_overwriting_file_or_baseline(self):
        self.edit(self.desired)
        self.update()
        production.private_json(self.root / production.PRESETS, self.manual)
        changed = {**self.desired, "capabilities": []}
        self.edit(changed)
        before = (self.root / production.PRESET_BASELINE).read_bytes()
        with self.assertRaises(ValueError):
            self.update()
        self.assertEqual(production.read_json(self.root / production.PRESETS), self.manual)
        self.assertEqual((self.root / production.PRESET_BASELINE).read_bytes(), before)
        # Explicitly clear/apply to adopt File Manager, then make a new UI edit.
        self.edit(None)
        self.update()
        self.edit(changed)
        self.update()
        self.assertEqual(production.read_json(self.root / production.PRESETS), changed)

    def test_matching_both_sources_is_safe_to_adopt(self):
        production.private_json(self.root / production.PRESETS, self.desired)
        self.edit(self.desired)
        self.update()
        self.assertEqual(production.read_json(self.root / production.PRESET_BASELINE)["editor"], self.desired)

    def test_invalid_json_duplicate_fields_ids_nonfinite_and_schema_fail_before_replacement(self):
        invalid = ('{invalid', '{"version":1,"version":1,"presets":[]}',
                   json.dumps({"version": True, "presets": self.manual["presets"]}),
                   json.dumps({"version": 1, "presets": self.manual["presets"] * 2}),
                   '{"version":1,"presets":[{"id":"manual","enabled":true}],"capabilities":NaN}',
                   json.dumps({"version": 1, "username": "synthetic", "password": "synthetic"}),
                   json.dumps({"version": 1, "presets": [{"id": "manual", "enabled": False}]}),
                   json.dumps({"version": 1, "presets": [{"id": "../injection", "enabled": True}]}),
                   json.dumps({"version": 1, "presets": [{"id": "disabled-only", "enabled": False, "map_filename": "Disabled.w3x"}]}))
        before = (self.root / production.PRESETS).read_bytes()
        baseline = (self.root / production.PRESET_BASELINE).read_bytes()
        for text in invalid:
            production.private_json(self.root / production.PRESET_EDITOR, {"version": 1, "preset_definitions": text})
            with self.assertRaises(ValueError):
                self.update()
            self.assertEqual((self.root / production.PRESETS).read_bytes(), before)
            self.assertEqual((self.root / production.PRESET_BASELINE).read_bytes(), baseline)
        production.private_json(self.root / production.PRESETS,
                                {"version": 1, "presets": [{"id": "disabled-only", "enabled": False, "map_filename": "Disabled.w3x"}]})
        with self.assertRaises(production.SettingsError) as error:
            production.preset_options(self.root)
        self.assertEqual(error.exception.args, ("PRESET_NONE_ENABLED",))

    def test_preflight_failure_restores_exact_preset_bytes_and_manifest(self):
        before = b'{ "version": 1, "presets": [{"id":"manual", "enabled":true}] }\n'
        (self.root / production.PRESETS).write_bytes(before)
        manifest = self.manifest.read_bytes()
        state = (self.root / production.PRESET_BASELINE).read_bytes()
        self.edit(self.desired)
        with self.assertRaises(ValueError):
            with production.preset_update(self.root), production.preset_manifest_update(self.root):
                raise ValueError("synthetic-preflight-failure")
        self.assertEqual((self.root / production.PRESETS).read_bytes(), before)
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertEqual((self.root / production.PRESET_BASELINE).read_bytes(), state)

    def test_discovery_is_readonly_and_ignores_stale_pending_editor(self):
        self.edit(self.desired)
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(set(production.preset_options(self.root)), {"manual"})
        with patch.object(production, "docker_output", side_effect=AssertionError("Docker forbidden")), \
                patch.object(account, "read_input", side_effect=AssertionError("account input forbidden")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(production.cli(["list-presets", "--instance-dir", str(self.root)]), 0)
        self.assertEqual(set(json.loads(output.getvalue())), {"manual"})
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        (self.root / production.PRESETS).unlink()
        self.assertEqual(set(production.preset_options(self.root)), {"manual"})

    def test_fixed_operator_errors_never_stringify_arbitrary_exception_payloads(self):
        errors = ((production.SettingsError("PRESET_JSON_INVALID"), "invalid preset JSON"),
                  (production.SettingsError("PRESET_NONE_ENABLED"), "enable at least one preset"),
                  (production.SettingsError("PRESET_SELECTION_REQUIRED"), "Select an enabled preset"),
                  (production.SettingsError("PRESET_ID_INVALID"), "invalid preset ID"),
                  (production.SettingsError("PRESET_EDIT_CONFLICT"), "both changed"),
                  (production.SettingsError(SYNTHETIC["password"]), "Settings rejected"),
                  (account.AccountError(SYNTHETIC["password"]), "Account setup rejected"),
                  (ValueError(SYNTHETIC["password"]), "Staging operation rejected"))
        for error, expected in errors:
            with patch.object(production, "main", side_effect=error), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(production.cli([]), 2)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn(SYNTHETIC["password"], output.getvalue())

    def test_editor_and_manifest_symlinks_and_shared_write_permissions_reject(self):
        self.edit(self.desired)
        target = self.root / production.PRESET_EDITOR
        other = self.root / "control/elsewhere.json"
        target.rename(other)
        target.symlink_to(other)
        with self.assertRaises(ValueError):
            self.update()
        target.unlink()
        other.rename(target)
        target.chmod(0o644)
        with self.assertRaises(ValueError):
            self.update()
        target.chmod(0o600)
        target = self.root / production.PRESETS
        target.chmod(0o666)
        with self.assertRaises(ValueError):
            self.update()
        target.chmod(0o600)
        self.manifest.rename(self.manifest.with_name("elsewhere.json"))
        self.manifest.symlink_to(self.manifest.with_name("elsewhere.json"))
        with self.assertRaises(ValueError):
            self.update()
        self.assertEqual(production.read_json(self.root / production.PRESETS), self.manual)

    def test_missing_initial_file_is_created_and_absence_restored_on_failure(self):
        (self.root / production.PRESETS).unlink()
        production.initialize_preset_editor(self.root)
        self.edit(self.desired)
        with self.assertRaises(ValueError):
            with production.preset_update(self.root):
                raise ValueError("synthetic-preflight-failure")
        self.assertFalse((self.root / production.PRESETS).exists())
        self.update()
        self.assertEqual(production.read_json(self.root / production.PRESETS), self.desired)

    def test_no_live_manifest_updates_only_staged_control_manifest(self):
        self.manifest.rename(self.root / production.PRESET_MANIFEST)
        self.edit(self.desired)
        self.update()
        self.assertFalse(self.manifest.exists())
        fields = json.loads((self.root / production.PRESET_MANIFEST).read_bytes())
        self.assertIn("synthetic-map", next(f for f in fields if f["ParamFieldName"] == "map_preset")["EnumValues"])

    def test_named_template_manifest_is_refreshed_without_creating_alias(self):
        alias = self.manifest
        named = self.root.parent / "war3-productionconfig.json"
        alias.rename(named)
        unrelated = self.root.parent / "other-manifest.json"
        unrelated.write_text("unrelated data, not a manifest")
        self.edit(self.desired)
        self.update()
        fields = json.loads(named.read_bytes())
        self.assertIn("synthetic-map", next(f for f in fields if f["ParamFieldName"] == "map_preset")["EnumValues"])
        self.assertTrue({"autostart_players", "autostart_enabled"}.isdisjoint(f["ParamFieldName"] for f in fields))
        self.assertFalse(alias.exists())
        self.assertEqual(unrelated.read_text(), "unrelated data, not a manifest")

    def test_named_manifest_and_existing_alias_both_refresh_preserving_other_fields(self):
        named = self.root.parent / "war3-productionconfig.json"
        fields = json.loads(self.manifest.read_bytes())
        fields[0]["DisplayName"] = "Named host label"
        production.private_json(named, fields)
        self.edit(self.desired)
        self.update()
        for path in (named, self.manifest):
            actual = json.loads(path.read_bytes())
            self.assertEqual(set(next(f for f in actual if f["ParamFieldName"] == "map_preset")["EnumValues"]),
                             {"manual", "synthetic-map"})
        self.assertEqual(json.loads(named.read_bytes())[0]["DisplayName"], "Named host label")
        self.assertEqual(json.loads(self.manifest.read_bytes())[0]["DisplayName"], "Host name")

    def test_failed_preflight_restores_both_known_manifest_files(self):
        named = self.root.parent / "war3-productionconfig.json"
        named.write_bytes(self.manifest.read_bytes())
        before = {p: p.read_bytes() for p in (named, self.manifest)}
        self.edit(self.desired)
        with self.assertRaises(ValueError):
            with production.preset_update(self.root) as check, production.preset_manifest_update(self.root, check):
                raise ValueError("synthetic-preflight-failure")
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(production.read_json(self.root / production.PRESETS), self.manual)

    def test_unsafe_named_manifest_does_not_fall_back_to_or_modify_alias(self):
        named = self.root.parent / "war3-productionconfig.json"
        named.symlink_to(self.manifest)
        before = self.manifest.read_bytes()
        self.edit(self.desired)
        with self.assertRaises(ValueError):
            self.update()
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(production.read_json(self.root / production.PRESETS), self.manual)

    def test_explicit_apply_checks_gate_and_stopped_state_without_profile_generation_or_start(self):
        custom_only = {"version": 1, "presets": [self.desired["presets"][1]]}
        self.edit(custom_only)
        production.private_json(self.root / "data/config/amp-settings.json", production.DEFAULTS)
        spec = {"schema_version": 1, "image_id": IMAGE, "container_name": "war3-phase3-staging",
                "uid": os.geteuid(), "gid": os.getegid()}
        production.private_json(self.root / "deployment.json", spec)
        production.private_json(self.root / "acceptance.json", acceptance())
        meta = {"Id": IMAGE, "Architecture": "amd64", "Os": "linux",
                "Config": {"Labels": {"war3.production.source-digest": production.gate(acceptance(), IMAGE)}}}
        with patch.object(production, "docker_json", return_value=[meta]), \
                patch.object(production, "docker_output", return_value=b"") as docker, \
                patch.object(account, "read_input", side_effect=AssertionError("canonical account forbidden")), \
                patch.object(account, "generate", side_effect=AssertionError("hashing forbidden")), \
                patch.object(production, "validate_preset_selection", side_effect=AssertionError("start-only selection check forbidden")), \
                patch.object(production.os, "execvpe", side_effect=AssertionError("runtime forbidden")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(production.cli(["apply-presets", "--instance-dir", str(self.root)]), 0)
        self.assertIn("Restart the AMP instance", output.getvalue())
        self.assertNotIn("refresh AMP Generic", output.getvalue())
        self.assertTrue(all(call.args[0][:2] == ["ps", "-aq"] for call in docker.call_args_list))
        self.assertEqual(production.read_json(self.root / production.PRESETS), custom_only)
        self.assertEqual(production.read_json(self.root / "data/config/amp-settings.json")["map_preset"], "manual")
        before = (self.root / production.PRESET_BASELINE).read_bytes()
        with patch.object(production, "docker_json", return_value=[meta]), \
                patch.object(production, "docker_output", return_value=b"active\n"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(production.cli(["apply-presets", "--instance-dir", str(self.root)]), 2)
        self.assertEqual((self.root / production.PRESET_BASELINE).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
