#!/usr/bin/env python3
"""Prepare private runtime; read only the supplied map and War3Patch.mpq."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/hostbot'))
from map_slots import capped_slot_config, read_info, slots_from_info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-dir', type=Path, required=True)
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--players', type=int, default=2)
    parser.add_argument('--max-players', type=int, help='close extra human slots using native map_slot entries')
    parser.add_argument('--countdown-seconds', type=int, default=5, help='0..60; local GHost++ patch')
    parser.add_argument('--no-autohost', action='store_true', help='set native autohost_maxgames = 0')
    parser.add_argument('--host-port', type=int, default=6113)
    parser.add_argument('--name', default='Offline LAN')
    args = parser.parse_args()
    game, mapfile, runtime = (p.resolve() for p in (args.game_dir, args.map, args.runtime))
    if not 1 <= args.players <= 12 or not 1 <= args.host_port <= 65535:
        parser.error('players must be 1..12; port must be 1..65535')
    if args.max_players is not None and not args.players <= args.max_players <= 12:
        parser.error('max-players must be between start players and 12')
    if not 0 <= args.countdown_seconds <= 60:
        parser.error('countdown-seconds must be 0..60')
    if not args.name or len(args.name.encode('utf-8')) > 24 or any(c in args.name for c in '\r\n=[]'):
        parser.error('game name must be 1..24 bytes without CR, LF, =, [ or ]')
    if runtime == game or game in runtime.parents:
        parser.error('runtime must be outside the original game directory')
    if not mapfile.is_file() or mapfile.suffix.lower() not in ('.w3x', '.w3m'):
        parser.error('map must be an existing .w3x or .w3m file')
    patch = game / 'War3Patch.mpq'
    if not patch.is_file():
        parser.error('War3Patch.mpq is required to calculate authentic map checksums')
    try:
        library = ROOT / 'build/ghost/lib/libStorm.so'
        map_slots = slots_from_info(read_info(mapfile, library))
        capacity = sum(s[2] == 0 and s[3] == 0 for s in map_slots)
        if args.players > capacity:
            parser.error(f'start threshold {args.players} exceeds {capacity} human slots in map')
        slots = capped_slot_config(mapfile, library, args.max_players) if args.max_players else ''
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if runtime.exists() and any(runtime.iterdir()):
        parser.error('runtime must be new or empty; existing files are not overwritten')
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    for directory in ('game', 'maps', 'mapcfgs', 'replays'):
        (runtime / directory).mkdir(mode=0o700)
    # Absolute symlinks preserve original bytes; upstream opens MPQ/maps read only.
    os.symlink(patch, runtime / 'game/War3Patch.mpq')
    mapname = 'selected' + mapfile.suffix.lower()
    os.symlink(mapfile, runtime / 'maps' / mapname)
    shutil.copyfile(ROOT / 'build/ghost/share/language.cfg', runtime / 'language.cfg')
    template = (ROOT / 'config/ghost-offline.cfg').read_text()
    template = template.replace('bot_hostport = 6113', f'bot_hostport = {args.host_port}')
    template = template.replace('autohost_startplayers = 2', f'autohost_startplayers = {args.players}')
    template = template.replace('autohost_gamename = Offline LAN', f'autohost_gamename = {args.name}')
    template = template.replace('bot_countdownseconds = 5', f'bot_countdownseconds = {args.countdown_seconds}')
    if args.no_autohost:
        template = template.replace('autohost_maxgames = 1', 'autohost_maxgames = 0')
    (runtime / 'ghost.cfg').write_text(template)
    (runtime / 'mapcfgs/selected.cfg').write_text(
        f'map_path = Maps\\Download\\{mapname}\nmap_localpath = {mapname}\n'
        'map_speed = 3\nmap_visibility = 4\nmap_observers = 1\n'
        'map_flags = 3\nmap_loadInGame = 0\n' + slots)
    metadata = {'map': str(mapfile), 'map_sha256': hashlib.sha256(mapfile.read_bytes()).hexdigest(),
                'patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest(),
                'runtime': str(runtime), 'players': args.players, 'max_players': args.max_players,
                'map_human_slots': capacity,
                'countdown_seconds': args.countdown_seconds, 'autohost': not args.no_autohost,
                'host_port': args.host_port,
                'scope': 'offline-loopback-only; synthetic clients are not real Warcraft clients'}
    (runtime / 'input-manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()
