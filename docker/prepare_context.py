#!/usr/bin/env python3
"""Export a small explicit source allowlist, never the research installation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "docker/Dockerfile", "docker/healthcheck.py", "docker/entrypoint.sh", "docker/ghost.cfg",
    "scripts/build-ghost.sh", "scripts/ghost-run.sh", "scripts/ghost-check-source.py",
    "third_party/ghost.lock.json", "patches/ghostplusplus-modern-linux.patch",
    "config/supervisor.example.json",
)
TREES = ("src/supervisor", "src/connector", "src/hostbot")
SUFFIXES = {".py", ".sh", ".patch", ".json", ".md", ".cfg", ".txt", ".cpp", ".h"}


def prepare(root, destination):
    root, destination = Path(root).resolve(), Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("context destination must be empty")
    paths = [root / name for name in FILES]
    for name in TREES:
        tree = root / name
        if tree.is_symlink():
            raise ValueError("symlinked source tree is forbidden")
        if not tree.is_dir():
            raise ValueError("required source tree missing")
        for path in tree.rglob("*"):
            if path.is_symlink():
                raise ValueError("symlink in source tree is forbidden")
            if path.is_file() and path.suffix in SUFFIXES and not any(
                part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts
            ):
                paths.append(path)
    for path in paths:
        parents = path.relative_to(root).parents
        if (not path.is_file() or path.is_symlink()
                or any((root / parent).is_symlink() for parent in parents)):
            raise ValueError("required source file missing or symlinked")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("unexpectedly large source file")
        # Text-only input prevents renamed EXE/DLL/MPQ files entering a layer.
        content = path.read_bytes()
        if b"\x00" in content:
            raise ValueError("binary source input forbidden")
        content.decode("utf-8")
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(set(paths)):
        target = destination / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o755 if path.suffix == ".sh" else 0o644)
    manifest = sorted(str(path.relative_to(root)) for path in set(paths))
    (destination / "context-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    hashes = {name: hashlib.sha256((destination / name).read_bytes()).hexdigest() for name in manifest}
    (destination / "context-sha256.json").write_text(json.dumps(hashes, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        files = prepare(ROOT, args.destination)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(2, f"context rejected: {type(error).__name__}\n")
    print(f"Prepared {len(files)} allowlisted source files")
