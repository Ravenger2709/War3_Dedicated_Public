#!/usr/bin/env python3
"""Build from audited local bases and text allowlist. Never accepts runtime assets."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
spec = importlib.util.spec_from_file_location("production_context", ROOT / "docker/production/prepare_context.py")
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


def environment():
    result = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        result.pop(key, None)
    return result


def call(args):
    return subprocess.check_output([*DOCKER, *args], env=environment(), timeout=60)


def inspect(ref, expected):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError("full expected image ID required")
    meta = json.loads(call(["image", "inspect", ref]))[0]
    if meta["Id"] != expected or meta["Architecture"] != "amd64" or meta["Os"] != "linux":
        raise ValueError("local base image pin/platform mismatch")
    return meta


def verify_native_sources(image_id, hashes):
    code = "from pathlib import Path; print(Path('/opt/war3/share/context-sha256.json').read_text())"
    native = json.loads(call(["run", "--rm", "--pull=never", "--network", "none", "--read-only",
                              "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt",
                              "no-new-privileges:true", "--memory", "128m", "--cpus", "1",
                              "--pids-limit", "32", "--ulimit", "core=0", "--entrypoint", "python3",
                              image_id, "-c", code]))
    expected = {name: sha for name, sha in hashes.items()
                if name in context.NATIVE_FILES or name.startswith("src/hostbot/")}
    if not expected or any(native.get(name) != sha for name, sha in expected.items()):
        raise ValueError("native base source hashes are stale relative to integrated checkout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-image", required=True)
    parser.add_argument("--native-id", required=True)
    parser.add_argument("--wine-image", default="war3-wine-research:local")
    parser.add_argument("--wine-id", required=True)
    parser.add_argument("--helper-image", default="war3-win32-build:phase2")
    parser.add_argument("--helper-id", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_.-]+", args.tag):
        parser.error("explicit versioned output tag required")
    bases = {"native": (args.native_image, args.native_id), "wine": (args.wine_image, args.wine_id),
             "helper": (args.helper_image, args.helper_id)}
    with tempfile.TemporaryDirectory(prefix="war3-production-context-") as temp:
        hashes = context.prepare(ROOT, temp)  # Missing src/runtime is fatal before Docker access.
        refs = {}
        for name, (ref, expected) in bases.items():
            inspect(ref, expected)
            refs[name] = "war3-production-input:" + name + "-" + expected[7:]
            call(["image", "tag", expected, refs[name]])
        verify_native_sources(args.native_id, hashes)
        command = [*DOCKER, "build", "--platform", "linux/amd64", "--pull=false", "--network", "none",
                   "--file", str(Path(temp) / "docker/production/Dockerfile"), "--tag", args.tag,
                   "--label", "org.opencontainers.image.source=https://github.com/Ravenger2709/War3_Dedicated_Public",
                   "--label", "war3.production.source-digest=" + context.source_digest(hashes)]
        for name, ref in refs.items():
            command.extend(["--build-arg", name.upper() + "_IMAGE=" + ref,
                            "--label", "war3.production." + name + "-id=" + bases[name][1]])
        if args.no_cache:
            command.append("--no-cache")
        subprocess.run([*command, temp], env=environment(), check=True)
        # Verify refs after build as well, so a moved local input tag cannot receive a receipt.
        for name, ref in refs.items():
            inspect(ref, bases[name][1])
        result = json.loads(call(["image", "inspect", args.tag]))[0]
        receipt = {"schema_version": 1, "result": "BUILT", "image_id": result["Id"],
                   "base_image_ids": {name: pin for name, (_, pin) in bases.items()},
                   "source_sha256": hashes, "source_digest": context.source_digest(hashes),
                   "runtime_tested": False, "native_source_verified": True,
                   "proprietary_or_secret_build_inputs": False}
        with args.receipt.open("x", encoding="utf-8") as output:
            json.dump(receipt, output, indent=2, sort_keys=True)
            output.write("\n")
    print(json.dumps({"result": "BUILT", "image_id": receipt["image_id"], "runtime_tested": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("production build rejected; inspect local build diagnostics")
