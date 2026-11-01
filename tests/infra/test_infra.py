"""Offline validation of context isolation, restore, and Generic manifests."""
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("context", ROOT / "docker/prepare_context.py")
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


class InfraTests(unittest.TestCase):
    def archive(self, path, members):
        with tarfile.open(path, "w") as output:
            for name, kind in members:
                member = tarfile.TarInfo(name)
                if kind == "link":
                    member.type, member.linkname = tarfile.SYMTYPE, "/tmp/outside"
                    output.addfile(member)
                else:
                    member.size = 4
                    output.addfile(member, io.BytesIO(b"test"))

    def restore(self, archive, destination):
        return subprocess.run(["bash", str(ROOT / "scripts/restore.sh"), str(archive), str(destination)], capture_output=True)

    def test_restore_new_tree_private_and_original_kept(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            self.archive(path / "backup.tar", [("config/ghost.cfg", "file"), ("state/keep", "file")])
            result = self.restore(path / "backup.tar", path / "restored")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((path / "restored/state/keep").read_bytes(), b"test")
            self.assertEqual((path / "restored/config/ghost.cfg").stat().st_mode & 0o777, 0o600)
            self.assertEqual((path / "restored").stat().st_mode & 0o777, 0o700)
            self.assertTrue((path / "restored/warcraft").is_dir())
            self.assertTrue((path / "backup.tar").exists())
            self.assertNotEqual(self.restore(path / "backup.tar", path / "restored").returncode, 0)

    def test_restore_rejects_traversal_links_and_proprietary_tree(self):
        for name, kind in [("../outside", "file"), ("/tmp/outside", "file"),
                           ("maps/link", "link"), ("warcraft/War3Patch.mpq", "file")]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                path = Path(temp)
                self.archive(path / "bad.tar", [(name, kind)])
                self.assertNotEqual(self.restore(path / "bad.tar", path / "restored").returncode, 0)
                self.assertFalse((path / "restored").exists())
                self.assertEqual(list(path.glob(".war3-restore-*")), [])

    def test_allowlist_excludes_research_game_and_private_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            for name in context.FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("safe")
            for name in context.TREES:
                (root / name).mkdir(parents=True, exist_ok=True)
                (root / name / "source.py").write_text("pass\n")
            for name in ("Warcraft 3 Frozen Throne/game.dll", "config/private.json", "vendor/evil.py"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("must never enter context")
            files = context.prepare(root, Path(temp) / "context")
            self.assertTrue(files)
            self.assertFalse(any("private" in p or "Warcraft" in p or "vendor" in p for p in files))

    def test_context_rejects_binary_or_symlink(self):
        for kind in ("binary", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "source"
                for name in context.FILES:
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("safe")
                for name in context.TREES:
                    (root / name).mkdir(parents=True, exist_ok=True)
                target = root / "src/supervisor/renamed.py"
                if kind == "binary":
                    target.write_bytes(b"MZ\x00binary")
                else:
                    target.symlink_to(root / context.FILES[0])
                with self.assertRaises(ValueError):
                    context.prepare(root, Path(temp) / "context")

    def test_amp_includes_and_settings_types(self):
        kvp = {}
        for line in (ROOT / "amp/war3-dedicated.kvp").read_text().splitlines():
            key, value = line.split("=", 1)
            self.assertNotIn(key, kvp)
            kvp[key] = value
            if value.startswith("@IncludeJson["):
                json.loads((ROOT / "amp" / value[13:-1]).read_text())
        self.assertEqual(kvp["App.ExecutableLinux"], "/usr/bin/docker")
        self.assertEqual(kvp["App.ExitMethod"], "SIGTERM")
        self.assertEqual(kvp["App.AdminMethod"], "STDIO")
        self.assertEqual(kvp["Meta.ContainerPolicy"], "NotSupported")
        self.assertNotIn("docker.sock", kvp["App.CommandLineArgs"])
        settings = json.loads((ROOT / "amp/war3-dedicatedconfig.json").read_text())
        fields = {setting["FieldName"] for setting in settings}
        self.assertEqual(len(fields), len(settings))
        for setting in settings:
            self.assertTrue({"DisplayName", "FieldName", "InputType", "ParamFieldName", "DefaultValue"} <= setting.keys())
            self.assertFalse(setting["IncludeInCommandLine"])
            if setting["InputType"] == "enum":
                self.assertIn(setting["DefaultValue"], setting["EnumValues"])
            if setting["InputType"] == "checkbox":
                self.assertIn(setting["DefaultValue"], setting["EnumValues"].values())
        for token in re.findall(r"\{\{([^$][^}]+)\}\}", kvp["App.CommandLineArgs"]):
            self.assertIn(token, fields)
        regex = re.compile(kvp["Console.AppReadyRegex"])
        self.assertIsNone(regex.fullmatch('{"event": "state", "state": "ERROR"}'))
        self.assertIsNotNone(regex.fullmatch('{"event": "state", "state": "LOBBY"}'))

    def test_amp_parameters_map_to_existing_defaults(self):
        settings = json.loads((ROOT / "amp/war3-dedicatedconfig.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run(["python3", str(ROOT / "amp/prepare-instance.py"), str(Path(temp) / "data")], check=True, capture_output=True)
            cfg = (Path(temp) / "data/config/ghost.cfg").read_text()
            self.assertIn("replay_buildnumber = 6401", cfg)
            game_keys = {line.split("=", 1)[0].strip() for line in cfg.splitlines() if "=" in line}
            mapcfg = (Path(temp) / "data/config/maps/selected.cfg").read_text()
            map_keys = {line.split("=", 1)[0].strip() for line in mapcfg.splitlines() if "=" in line}
            for setting in settings:
                if setting["FieldName"] not in {"Image", "DockerNetwork", "RunUID", "RunGID", "Mode"}:
                    self.assertIn(setting["ParamFieldName"], game_keys | map_keys)
            self.assertEqual(subprocess.run(["python3", str(ROOT / "amp/prepare-instance.py"), str(Path(temp) / "data")], capture_output=True).returncode, 2)

    def test_compose_contract(self):
        result = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.yaml"), "config", "--format", "json"], capture_output=True, check=True)
        service = json.loads(result.stdout)["services"]["hostbot"]
        mounts = {mount["target"]: mount for mount in service["volumes"]}
        for target in ("/data/config", "/data/maps", "/data/warcraft"):
            self.assertTrue(mounts[target]["read_only"])
        for target in ("/data/state", "/data/logs", "/data/replays"):
            self.assertFalse(mounts[target].get("read_only", False))
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertNotIn("container_name", service)


if __name__ == "__main__":
    unittest.main()
