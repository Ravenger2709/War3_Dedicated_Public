#!/usr/bin/env python3
"""Run bridge verification only in project-scoped, isolated local containers.

Only Python source in SOURCE_FILES is staged. Never mount the project root:
the primary checkout can contain a proprietary installation or private files.
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

DEFAULT_IMAGE = "sha256:07d6959e196f24e49589770d0c14a55d7b5c2e1259c506945ef9324b5dc53853"
LOCAL_DAEMON = "unix:///var/run/docker.sock"
SOURCE_FILES = (
    "src/bridge/__init__.py", "src/bridge/__main__.py", "src/bridge/bncs.py",
    "src/bridge/probe.py", "src/bridge/secrets.py", "src/connector/__init__.py",
    "src/supervisor/__init__.py", "src/supervisor/__main__.py", "src/supervisor/child_exec.py",
    "src/supervisor/config.py", "src/supervisor/events.py", "src/supervisor/service.py",
    "tests/bridge/test_bncs.py", "tests/bridge/test_cli.py", "tests/bridge/test_probe.py",
    "tests/bridge/test_secrets.py", "tests/bridge/test_container_runner.py",
    "tests/unit/test_supervisor.py", "scripts/bridge-native-smoke.py", "scripts/bridge-test-container.py",
)
UNIT_CODE = """import unittest
suite = unittest.TestSuite()
for directory in ('tests/bridge', 'tests/unit'):
    suite.addTests(unittest.TestLoader().discover(directory))
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(not result.wasSuccessful())
"""


def prepare_sources(source: Path, destination: Path) -> dict:
    """Copy exact reviewed source paths only; reject symlinks and non-Python."""
    hashes = {}
    for relative in SOURCE_FILES:
        path = source / relative
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != source and source in parent.parents):
            raise ValueError("BRIDGE_SOURCE_SYMLINK_REJECTED")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
            raise ValueError("BRIDGE_SOURCE_TYPE_OR_SIZE_REJECTED")
        data = path.read_bytes()
        if len(data) > 128 * 1024 or b"\0" in data:
            raise ValueError("BRIDGE_SOURCE_TYPE_OR_SIZE_REJECTED")
        try:
            ast.parse(data.decode("utf-8"), filename=relative)
        except (ValueError, SyntaxError, UnicodeError):
            raise ValueError("BRIDGE_SOURCE_NOT_PYTHON") from None
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        target.chmod(0o600)
        hashes[relative] = hashlib.sha256(data).hexdigest()
    return hashes


def docker_environment() -> dict:
    # The caller may have a remote Docker context selected. Force only the
    # local Unix daemon and never inherit remote/TLS routing settings.
    environment = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_TLS"):
        environment.pop(key, None)
    return environment


def command(docker: str, bundle: Path, name: str, image: str, mode: str) -> list[str]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("BRIDGE_LOCAL_IMAGE_ID_REQUIRED")
    if mode not in ("unit", "native", "reconnect"):
        raise ValueError("BRIDGE_TEST_MODE_INVALID")
    if os.geteuid() == 0:
        raise ValueError("BRIDGE_TEST_REQUIRES_NONROOT_OWNER")
    args = [docker, "--host", LOCAL_DAEMON, "run", "--rm", "--pull=never", "--name", name,
            "--label", "org.war3-dedicated.phase2=bridge-test", "--network=none", "--read-only",
            "--user", f"{os.geteuid()}:{os.getegid()}", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--memory=256m", "--cpus=1", "--pids-limit=64",
            "--ulimit", "core=0:0", "--init", "--no-healthcheck",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--mount", f"type=bind,src={bundle},dst=/repo,readonly", "--workdir", "/repo",
            "--env", "PYTHONPATH=/repo", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint", "python3", image]
    if mode == "unit":
        args.extend(["-c", UNIT_CODE])
    else:
        args.append("/repo/scripts/bridge-native-smoke.py")
        if mode == "reconnect":
            args.append("--check-reconnect")
    return args


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("unit", "native", "reconnect", "all"), nargs="?", default="unit")
    parser.add_argument("--image-id", default=DEFAULT_IMAGE, help="existing local sha256 image ID; never pulled")
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("BRIDGE_LOCAL_DOCKER_REQUIRED")
    environment = docker_environment()
    modes = ("unit", "reconnect") if args.mode == "all" else (args.mode,)
    with tempfile.TemporaryDirectory(prefix="war3-bridge-source-") as temporary:
        bundle = Path(temporary)
        hashes = prepare_sources(source, bundle)
        print(json.dumps({"event": "BRIDGE_SOURCE_BUNDLE", "files": len(hashes),
                          "manifest_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
                          "local_daemon": LOCAL_DAEMON}), flush=True)
        for mode in modes:
            name = f"war3-bridge-{mode}-{uuid.uuid4().hex[:12]}"
            invocation = command(docker, bundle, name, args.image_id, mode)
            print(json.dumps({"event": "BRIDGE_CONTAINER_START", "mode": mode, "container": name,
                              "image_id": args.image_id}), flush=True)
            try:
                result = subprocess.run(invocation, env=environment, timeout=125 if mode == "reconnect" else 30)
                if result.returncode:
                    return result.returncode
            finally:
                # --rm normally removed it already; on timeout/interruption,
                # remove only this UUID-named project test container.
                subprocess.run([docker, "--host", LOCAL_DAEMON, "rm", "--force", name],
                               env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
