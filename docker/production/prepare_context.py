#!/usr/bin/env python3
"""Export only production text sources; missing integrated runtime is an error."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "docker/production/Dockerfile", "docker/production/entrypoint.sh",
    "docker/production/runtime-contract.json", "docker/healthcheck.py",
    "docker/wine-research/quit-launcher.c",
    "config/star-vendor-files.json",
    "config/supervisor-iccup.example.json",
    "config/presets.example.json", "config/presets.schema.json",
    "src/runtime/curated_map_presets.json",
    "docker/Dockerfile", "scripts/build-ghost.sh", "scripts/ghost-check-source.py",
    "third_party/ghost.lock.json", "patches/ghostplusplus-modern-linux.patch",
)
NATIVE_FILES = ("docker/Dockerfile", "scripts/build-ghost.sh", "scripts/ghost-check-source.py",
                "third_party/ghost.lock.json", "patches/ghostplusplus-modern-linux.patch")
TREES = ("src/supervisor", "src/connector", "src/bridge", "src/runtime", "src/hostbot")


def prepare(root, destination):
    root, destination = Path(root).resolve(), Path(destination)
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("context must be a new or empty real directory")
    paths = [root / name for name in FILES]
    for name in TREES:
        tree = root / name
        if not tree.is_dir() or tree.is_symlink():
            raise ValueError("required production source tree missing or symlinked: " + name)
        sources = []
        for path in tree.rglob("*"):
            if path.is_symlink():
                raise ValueError("symlink in production source tree")
            suffixes = {".cpp", ".h"} if name == "src/hostbot" else {".py"}
            if path.is_file() and path.suffix in suffixes and not any(
                p.startswith(".") or p == "__pycache__" for p in path.relative_to(tree).parts
            ):
                sources.append(path)
        if not sources:
            raise ValueError("production source tree is empty: " + name)
        paths.extend(sources)
    contents = {}
    for path in sorted(set(paths)):
        name = path.relative_to(root).as_posix()
        if (not path.is_file() or path.is_symlink() or any(
                (root / parent).is_symlink() for parent in path.relative_to(root).parents)):
            raise ValueError("source file missing or symlinked")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("oversized source")
        data = path.read_bytes()
        if b"\0" in data:
            raise ValueError("binary source forbidden")
        data.decode("utf-8")
        contents[name] = data
    # Validate everything before writing anything. No private config or opaque input.
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if name.endswith(".sh") else 0o644)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    (destination / "production-context-manifest.json").write_text(json.dumps(list(hashes), indent=2) + "\n")
    (destination / "production-source-sha256.json").write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")
    return hashes


def source_digest(hashes):
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        hashes = prepare(ROOT, args.destination)
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(2, f"production context rejected: {type(error).__name__}\n")
    print(json.dumps({"files": len(hashes), "source_digest": source_digest(hashes)}))
