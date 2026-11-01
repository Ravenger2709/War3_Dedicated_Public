import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("bridge_container_runner", ROOT / "scripts/bridge-test-container.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ContainerRunnerTests(unittest.TestCase):
    def test_source_bundle_is_exact_allowlist_without_private_or_proprietary_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            hashes = runner.prepare_sources(ROOT, bundle)
            self.assertEqual(set(hashes), set(runner.SOURCE_FILES))
            self.assertEqual({str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file()}, set(runner.SOURCE_FILES))
            self.assertFalse((bundle / "Warcraft 3 Frozen Throne").exists())
            self.assertFalse((bundle / ".git").exists())
            self.assertTrue(all(p.endswith(".py") for p in hashes))

    def test_symlink_file_and_parent_are_rejected_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "bundle"
            (source / "src").mkdir(parents=True)
            target = root / "target.py"
            target.write_text("x = 1\n")
            (source / "src/module.py").symlink_to(target)
            with patch.object(runner, "SOURCE_FILES", ("src/module.py",)):
                with self.assertRaisesRegex(ValueError, "SYMLINK"):
                    runner.prepare_sources(source, destination)
            (source / "src/module.py").unlink()
            (source / "src").rmdir()
            (source / "src").symlink_to(root, target_is_directory=True)
            with patch.object(runner, "SOURCE_FILES", ("src/target.py",)):
                with self.assertRaisesRegex(ValueError, "SYMLINK"):
                    runner.prepare_sources(source, destination)

    def test_binary_or_non_python_substitution_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for content in (b"MZ\0binary", b"not valid python source\n", b"x" * (128 * 1024 + 1)):
                (root / "module.py").write_bytes(content)
                with patch.object(runner, "SOURCE_FILES", ("module.py",)):
                    with self.assertRaises(ValueError):
                        runner.prepare_sources(root, root / "bundle")

    def test_command_pins_local_daemon_and_container_restrictions(self):
        args = runner.command("/usr/bin/docker", Path("/tmp/allowlist-source"), "war3-bridge-unit-test", runner.DEFAULT_IMAGE, "unit")
        self.assertEqual(args[:4], ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "run"])
        for option in ("--network=none", "--read-only", "--pull=never", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges:true", "--pids-limit=64", "--memory=256m", "--cpus=1"):
            self.assertIn(option, args)
        self.assertIn("core=0:0", args)
        self.assertIn(f"{os.geteuid()}:{os.getegid()}", args)
        self.assertEqual(args[args.index("--mount") + 1], "type=bind,src=/tmp/allowlist-source,dst=/repo,readonly")
        self.assertNotIn("--privileged", args)
        self.assertEqual(args.count("--mount"), 1)

    def test_remote_docker_environment_is_removed_and_registry_images_refused(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://remote", "DOCKER_CONTEXT": "remote",
                                     "DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": "/private/certs"}):
            environment = runner.docker_environment()
        self.assertFalse(set(environment) & {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"})
        for image in ("ubuntu:latest", "example.invalid/private:tag", "sha256:bad"):
            with self.assertRaises(ValueError):
                runner.command("docker", Path("/tmp/bundle"), "war3-bridge-unit-test", image, "unit")


if __name__ == "__main__":
    unittest.main()
