#!/usr/bin/env python3
"""Create a NEW private data tree. Never touches AMP configuration or Docker."""
import argparse
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def prepare(destination):
    destination = Path(destination)
    destination.mkdir(mode=0o700)  # Refuse existing trees, including symlinks.
    try:
        for directory in ("config/maps", "maps", "logs", "replays", "state", "warcraft"):
            (destination / directory).mkdir(parents=True, mode=0o700)
        config = json.loads((ROOT / "config/supervisor.example.json").read_text())
        config["hostbot"]["command"] = ["/opt/war3/bin/ghost++", "/data/config/ghost.cfg"]
        (destination / "config/supervisor.json").write_text(json.dumps(config, indent=2) + "\n")
        shutil.copyfile(ROOT / "docker/ghost.cfg", destination / "config/ghost.cfg")
        (destination / "config/maps/selected.cfg").write_text(
            "map_path = Maps\\Download\\selected.w3x\nmap_localpath = selected.w3x\n"
            "map_speed = 3\nmap_visibility = 4\nmap_observers = 1\n"
            "map_flags = 3\nmap_loadInGame = 0\n")
        for path in destination.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
    except Exception:
        shutil.rmtree(destination)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.exit(2, "Run as the non-root owner of the future instance data.\n")
    try:
        prepare(args.data_dir)
    except (OSError, ValueError):
        parser.exit(2, "Preparation rejected; destination must be new and its parent writable.\n")
    print(f"Private data prepared; runtime UID={os.getuid()}, GID={os.getgid()}. Supply map and War3Patch.mpq separately.")
