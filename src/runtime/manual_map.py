"""Opt-in, read-only and fail-closed validation of a manual map selection."""
import os
from pathlib import Path
import re
import stat

from .config import gameplay_config
from .files import no_symlinks
from .presets import analyze_map, map_basename

_LIMIT = 65536
_MAP_FIELDS = {'map_path', 'map_localpath', 'map_speed', 'map_visibility',
               'map_observers', 'map_flags', 'map_loadInGame', 'map_loadingame'}


def _read_config(path):
    """Read a bounded regular file without following links, including ancestors."""
    path = no_symlinks(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _LIMIT:
                raise ValueError('MANUAL_CONFIG_REJECTED')
            data = b''
            while len(data) <= _LIMIT:
                chunk = os.read(file_fd, min(8192, _LIMIT + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            after = os.fstat(file_fd)
            fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if len(data) != before.st_size or fingerprint(before) != fingerprint(after):
                raise ValueError('MANUAL_CONFIG_CHANGED')
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)
    try:
        lines = data.decode('utf-8').splitlines()
    except UnicodeError:
        raise ValueError('MANUAL_CONFIG_REJECTED') from None
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r'[A-Za-z0-9_]+', key) or key in values:
            raise ValueError('MANUAL_CONFIG_REJECTED')
        values[key] = value
    return values


def load_manual_selection(source):
    """Snapshot the operator configs and resolve a restricted map selection."""
    source = no_symlinks(source)
    operator = _read_config(source)
    name = operator.get('bot_defaultmap', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. ()+-]{0,119}\.cfg', name) or '..' in name:
        raise ValueError('MANUAL_MAP_CONFIG_NAME_REJECTED')
    mapcfg = _read_config(source.parent / 'maps' / name)
    if set(mapcfg) - _MAP_FIELDS:
        raise ValueError('MANUAL_MAP_CONFIG_FIELDS_REJECTED')
    legacy = mapcfg.pop('map_loadInGame', None)
    native = mapcfg.get('map_loadingame')
    if legacy is not None:
        if native is not None and legacy != native:
            raise ValueError('MANUAL_MAP_LOADINGAME_CONFLICT')
        mapcfg['map_loadingame'] = legacy
    filename = map_basename(mapcfg.get('map_localpath'))
    return no_symlinks(source.parent.parent / 'maps' / filename), mapcfg


def validate_manual_capacity(game, analysis):
    slots = analysis['slots']
    capacity = len(slots)
    computers = sum(slot[3] == 1 for slot in slots)
    human_limit = min(analysis['human_capacity'], game['max_players'] - computers)
    if game['max_players'] > capacity:
        raise ValueError('MANUAL_MAP_CAPACITY_REJECTED')
    if (game['vote_min_players'] > human_limit or
            game['autostart_enabled'] and game['autostart_players'] > human_limit):
        raise ValueError('MANUAL_HUMAN_CAPACITY_REJECTED')
    return analysis


def validate_manual_map(source, gameplay):
    """Validate a restricted operator selection and its map-derived capacity."""
    game = gameplay_config({**gameplay, 'map_preset': 'manual'})
    path, _ = load_manual_selection(source)
    return validate_manual_capacity(game, analyze_map(path))
