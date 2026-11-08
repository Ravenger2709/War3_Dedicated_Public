#!/usr/bin/env python3
"""Prepare a new, private, credential-free G1 LAN capture instance.

Paths in the generated configuration are container paths. No Docker, AMP,
network or game-engine execution is performed by this helper.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

def prepare(destination, mapfile, interface, name='G1 Bridge 1.26a'):
    network = ipaddress.IPv4Interface(interface)
    if not network.ip.is_private or network.ip.is_loopback or network.network.prefixlen != 24:
        raise ValueError('a private LAN IPv4 /24 is required')
    if not name or len(name.encode()) > 24 or any(c in name for c in '\r\n=[]'):
        raise ValueError('invalid lobby name')
    if destination.exists():
        raise ValueError('destination must not exist')
    # Pin the current map release before publishing a runnable configuration.
    digest = hashlib.sha256(mapfile.read_bytes()).hexdigest()
    destination.mkdir(mode=0o700)
    for folder in ('mapcfgs', 'replays', 'logs'):
        (destination / folder).mkdir(mode=0o700)
    settings = (ROOT / 'config/ghost-offline.cfg').read_text()
    changes = {
        'bot_log': '/data/logs/ghost.log', 'bot_language': '/opt/g1/share/language.cfg',
        'bot_hostport': '6114', 'bot_bindaddress': str(network.ip),
        'bot_war3path': '/no-game/', 'bot_mappath': '/maps/',
        'bot_mapcfgpath': '/data/mapcfgs/', 'bot_defaultmap': 'selected.cfg',
        'bot_replaypath': '/data/replays/', 'bot_virtualhostname': 'G1Capture',
        'bot_countdownseconds': '5', 'bot_lobbytimelimit': '0',
        'udp_broadcasttarget': str(network.network.broadcast_address), 'udp_dontroute': '1',
        'autohost_startplayers': '1', 'autohost_gamename': name,
        'autohost_owner': 'G1Operator', 'db_sqlite3_file': '/data/ghost.dbs',
        'bot_motdfile': '/data/motd.txt', 'bot_vote_enabled': '0',
        'bot_lobby_refresh_enabled': '0', 'bot_autostartplayers': '2',
        'bot_g1_bridge_mode': '1', 'bot_g1_map_sha256': digest,
        'bot_logmethod': '2',
    }
    lines = []
    for line in settings.splitlines():
        key = line.split('=', 1)[0].strip() if '=' in line else None
        lines.append(f'{key} = {changes.pop(key)}' if key in changes else line)
    lines += [f'{key} = {value}' for key, value in changes.items()]
    (destination / 'ghost.cfg').write_text('\n'.join(lines) + '\n')
    (destination / 'mapcfgs/selected.cfg').write_text(
        'map_path = Maps\\Download\\g1.w3x\nmap_localpath = g1.w3x\n'
        'map_speed = 3\nmap_visibility = 4\nmap_observers = 1\nmap_flags = 3\nmap_loadInGame = 0\n')
    (destination / 'motd.txt').write_text(
        'G1 capture test, Warcraft III 1.26a. No iCCup/LLM.\n'
        'The map starts automatically after downloading and latency checks.\n'
        'In game, player 1 types g1:hello. The footman stands still in capture mode.\n')
    record = {'schema_version': 1, 'mode': 'capture', 'lobby': name, 'map_sha256': digest,
              'bind_address': str(network.ip), 'tcp_port': 6114,
              'lan_broadcast': str(network.network.broadcast_address), 'udp_discovery_port': 6112,
              'vpn_used': False, 'amp_used': False, 'star_used': False, 'credentials': False}
    (destination / 'session.json').write_text(json.dumps(record, indent=2) + '\n')
    return record

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--lan', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.destination, args.map, args.lan), indent=2))
if __name__ == '__main__':
    main()
