"""Produce a sealed binary startup-manifest candidate; native consumer pending.

The parser is provided for strict local validation and protocol tests only. This
module does not start GHost or implement a selection/ACK channel.
"""
import fcntl
import hashlib
import os
import re
import struct

from .config import gameplay_config
from .preload_catalog import PreparedPreload
from .preload_inputs import PinnedCatalog, _new_memfd, _SEALS, _write_all
from .presets import lobby_name, map_basename, preset_id

_MAGIC = b'W3PRLD01'
_HEADER = struct.Struct('<8sHHBBQQ')
_ENTRY = struct.Struct('<IQ32sBBI')
_SETTINGS = struct.Struct('<15I')
_LIMIT = 256 * 1024
_FILE_LIMIT = 128 * 1024 * 1024
_PAYLOAD_LIMIT = 512 * 1024 * 1024
_DIGITS = re.compile(r'0|[1-9][0-9]*')
_AI_KEYS = {'bot_lobby_ai_supported', 'bot_lobby_ai_target', 'bot_lobby_ai_skill', 'bot_lobby_min_humans'}


def _number(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('PRELOAD_MANIFEST_SETTINGS_REJECTED')
    return value


def _string_number(value, low, high):
    if not isinstance(value, str) or not _DIGITS.fullmatch(value):
        raise ValueError('PRELOAD_MANIFEST_SETTINGS_REJECTED')
    return _number(int(value), low, high)


def _settings(values):
    if len(values) != 15:
        raise ValueError('PRELOAD_MANIFEST_SETTINGS_REJECTED')
    (maximum, auto, threshold, countdown, vote, percent, minimum, timeout,
     cooldown, refresh, interval, ai, target, skill, humans) = values
    for value in (auto, vote, refresh, ai): _number(value, 0, 1)
    _number(maximum, 2, 12); _number(threshold, 2, 12); _number(countdown, 0, 60)
    _number(percent, 1, 100); _number(minimum, 1, maximum); _number(timeout, 5, 600)
    _number(cooldown, 0, 600); _number(interval, 10, 3600)
    _number(skill, 0, 2); _number(humans, 1, maximum)
    if auto and threshold > maximum:
        raise ValueError('PRELOAD_MANIFEST_SETTINGS_REJECTED')
    if ai:
        _number(target, 2, maximum)
    elif target != 0 or humans < 2:
        raise ValueError('PRELOAD_MANIFEST_SETTINGS_REJECTED')
    if minimum == 1 and not (ai and target >= 2 and humans == 1):
        raise ValueError('PRELOAD_MANIFEST_SETTINGS_REJECTED')
    return tuple(values)


def _mapcfg(raw):
    if not isinstance(raw, str) or not raw or '\x00' in raw:
        raise ValueError('PRELOAD_MANIFEST_MAPCFG_REJECTED')
    encoded = raw.encode('utf-8')
    if len(encoded) > 8192:
        raise ValueError('PRELOAD_MANIFEST_MAPCFG_REJECTED')
    allowed = {'map_path', 'map_localpath', 'map_speed', 'map_visibility',
               'map_observers', 'map_flags', 'map_loadingame'}
    allowed.update('map_slot' + str(i) for i in range(1, 13))
    values = {}
    for line in raw.splitlines():
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or key not in allowed or key in values or not value:
            raise ValueError('PRELOAD_MANIFEST_MAPCFG_REJECTED')
        values[key] = value
    required = {'map_path', 'map_localpath'}
    if not required <= values.keys():
        raise ValueError('PRELOAD_MANIFEST_MAPCFG_REJECTED')
    map_basename(values['map_localpath'])
    path = values['map_path']
    components = path.split('\\')
    if (len(path.encode('utf-8')) > 53 or len(components) < 1 or
            any(not re.fullmatch(r'[A-Za-z0-9_ ()+.-]+', part) or '..' in part
                or part in ('.', '..') for part in components)):
        raise ValueError('PRELOAD_MANIFEST_MAPCFG_REJECTED')
    map_basename(components[-1])
    for key, low, high in (('map_speed', 1, 3), ('map_visibility', 1, 4),
                           ('map_observers', 1, 4), ('map_flags', 0, 31),
                           ('map_loadingame', 0, 1)):
        if key in values: _string_number(values[key], low, high)
    slots = [i for i in range(1, 13) if 'map_slot' + str(i) in values]
    if slots != list(range(1, len(slots) + 1)):
        raise ValueError('PRELOAD_MANIFEST_MAPCFG_REJECTED')
    for i in slots:
        parts = values['map_slot' + str(i)].split()
        if len(parts) != 9:
            raise ValueError('PRELOAD_MANIFEST_MAPCFG_REJECTED')
        for part in parts: _string_number(part, 0, 255)
    return encoded


def _entry_settings(entry):
    game, ai = dict(entry.gameplay), dict(entry.ai)
    if set(ai) != _AI_KEYS:
        raise ValueError('PRELOAD_MANIFEST_SETTINGS_REJECTED')
    # The named-preset resolver alone may allow vote_min_players == 1 with AI.
    minimum = game.get('vote_min_players')
    normal = gameplay_config({**game, 'vote_min_players': max(2, minimum) if type(minimum) is int else minimum})
    normal['vote_min_players'] = minimum
    lobby_name(normal['game_name'])
    def flag(key):
        if type(normal[key]) is not bool:
            raise ValueError('PRELOAD_MANIFEST_SETTINGS_REJECTED')
        return int(normal[key])
    values = (normal['max_players'], flag('autostart_enabled'), normal['autostart_players'],
              normal['countdown_seconds'], flag('vote_enabled'), normal['vote_required_percent'],
              normal['vote_min_players'], normal['vote_timeout'], normal['vote_cooldown'],
              flag('lobby_refresh_enabled'), normal['lobby_refresh_interval'],
              _string_number(ai['bot_lobby_ai_supported'], 0, 1),
              _string_number(ai['bot_lobby_ai_target'], 0, 12),
              _string_number(ai['bot_lobby_ai_skill'], 0, 2),
              _string_number(ai['bot_lobby_min_humans'], 1, 12))
    return _settings(values)


def _accounted(sizes, default_size, max_games):
    _number(default_size, 1, _FILE_LIMIT); _number(max_games, 1, 5)
    if not 1 <= len(sizes) <= 32:
        raise ValueError('PRELOAD_MANIFEST_BUDGET_REJECTED')
    for size in sizes: _number(size, 1, _FILE_LIMIT)
    return (128 * 1024 * 1024 + 2 * sum(sizes) + 2 * default_size +
            max([default_size, *sizes]) * (max_games + 3))


def encode_startup_manifest(prepared):
    """Return canonical candidate bytes; caller still owns `prepared`."""
    if not isinstance(prepared, PreparedPreload):
        raise ValueError('PRELOAD_MANIFEST_REJECTED')
    fds = prepared.pinned.pass_fds()
    maps, entries, cycle = prepared.pinned.maps, prepared.entries, prepared.cycle_indices
    if not 1 <= len(entries) == len(maps) == len(fds) <= 32 or not 1 <= len(cycle) <= 32:
        raise ValueError('PRELOAD_MANIFEST_REJECTED')
    accounted = _accounted([item.size for item in maps], prepared.default_map_bytes, prepared.max_games)
    if accounted > _PAYLOAD_LIMIT or accounted != prepared.pinned.accounted_bytes:
        raise ValueError('PRELOAD_MANIFEST_BUDGET_REJECTED')
    result = bytearray(_HEADER.pack(_MAGIC, len(entries), len(cycle), prepared.max_games, 0,
                                    prepared.default_map_bytes, accounted))
    seen = set()
    for index, (entry, item) in enumerate(zip(entries, maps)):
        key = preset_id(entry.preset_id)
        if (key in seen or entry.fd_index != index or item.preset_id != key or
                item.sha256 != entry.map_sha256 or not 3 <= item.fd <= 0x7fffffff):
            raise ValueError('PRELOAD_MANIFEST_REJECTED')
        seen.add(key)
        name = lobby_name(entry.gameplay['game_name']).encode('utf-8')
        ident = key.encode('ascii'); cfg = _mapcfg(entry.mapcfg); settings = _entry_settings(entry)
        result.extend(_ENTRY.pack(item.fd, item.size, bytes.fromhex(item.sha256), len(ident), len(name), len(cfg)))
        result.extend(_SETTINGS.pack(*settings)); result.extend(ident); result.extend(name); result.extend(cfg)
    if any(type(i) is not int or not 0 <= i < len(entries) for i in cycle):
        raise ValueError('PRELOAD_MANIFEST_INDEX_REJECTED')
    result.extend(bytes(cycle))
    if len(result) > _LIMIT:
        raise ValueError('PRELOAD_MANIFEST_SIZE_REJECTED')
    data = bytes(result)
    parse_startup_manifest(data)  # Independent bounded parse before sealing.
    return data


def parse_startup_manifest(data):
    """Strictly parse candidate bytes; this does not validate live descriptors."""
    if not isinstance(data, bytes) or not _HEADER.size <= len(data) <= _LIMIT:
        raise ValueError('PRELOAD_MANIFEST_REJECTED')
    cursor = 0
    def take(size):
        nonlocal cursor
        if size < 0 or size > len(data) - cursor:
            raise ValueError('PRELOAD_MANIFEST_REJECTED')
        value = data[cursor:cursor + size]; cursor += size
        return value
    magic, count, occurrences, games, reserved, default_size, claimed = _HEADER.unpack(take(_HEADER.size))
    if magic != _MAGIC or reserved or not 1 <= count <= 32 or not 1 <= occurrences <= 32:
        raise ValueError('PRELOAD_MANIFEST_REJECTED')
    entries, ids, fds = [], set(), set()
    try:
        for _ in range(count):
            fd, size, digest, idlen, namelen, cfglen = _ENTRY.unpack(take(_ENTRY.size))
            values = _settings(_SETTINGS.unpack(take(_SETTINGS.size)))
            if not 3 <= fd <= 0x7fffffff or fd in fds or not 1 <= idlen <= 32 or not 1 <= namelen <= 24 or not 1 <= cfglen <= 8192:
                raise ValueError('PRELOAD_MANIFEST_REJECTED')
            key = take(idlen).decode('ascii'); name = take(namelen).decode('utf-8'); cfg = take(cfglen).decode('utf-8')
            if preset_id(key) in ids or lobby_name(name) != name:
                raise ValueError('PRELOAD_MANIFEST_REJECTED')
            _mapcfg(cfg); ids.add(key); fds.add(fd)
            entries.append({'id': key, 'fd': fd, 'size': size, 'sha256': digest.hex(),
                            'name': name, 'settings': values, 'mapcfg': cfg})
    except (UnicodeError, struct.error):
        raise ValueError('PRELOAD_MANIFEST_REJECTED') from None
    indices = tuple(take(occurrences))
    if cursor != len(data) or any(index >= count for index in indices):
        raise ValueError('PRELOAD_MANIFEST_INDEX_REJECTED')
    actual = _accounted([entry['size'] for entry in entries], default_size, games)
    if actual != claimed or actual > _PAYLOAD_LIMIT:
        raise ValueError('PRELOAD_MANIFEST_BUDGET_REJECTED')
    return {'entries': entries, 'cycle_indices': indices, 'max_games': games,
            'default_map_bytes': default_size, 'accounted_bytes': actual}


class StartupBundle(PinnedCatalog):
    """Supervisor-compatible owner of map FDs plus a sealed manifest FD."""
    def __init__(self, pinned, manifest_fd, info, manifest_sha256):
        super().__init__(pinned.maps, pinned.cycle_indices, pinned.accounted_bytes)
        self.manifest_fd = manifest_fd
        self.manifest_sha256 = manifest_sha256
        self._manifest_identity = (info.st_dev, info.st_ino, info.st_size)

    def pass_fds(self):
        maps = super().pass_fds()
        try:
            info = os.fstat(self.manifest_fd)
            seals = fcntl.fcntl(self.manifest_fd, fcntl.F_GET_SEALS)
            if ((info.st_dev, info.st_ino, info.st_size) != self._manifest_identity or
                    seals & _SEALS != _SEALS or self.manifest_fd in maps):
                raise ValueError('PRELOAD_MANIFEST_CHANGED')
        except OSError:
            raise ValueError('PRELOAD_MANIFEST_CHANGED') from None
        return (*maps, self.manifest_fd)

    def close(self):
        opened = not self._closed
        super().close()
        if opened:
            try:
                info = os.fstat(self.manifest_fd)
                if (info.st_dev, info.st_ino, info.st_size) == self._manifest_identity:
                    os.close(self.manifest_fd)
            except OSError:
                pass


def seal_startup_manifest(prepared):
    """Consume prepared ownership only after a complete manifest has been sealed."""
    data = encode_startup_manifest(prepared)
    fd = _new_memfd()
    try:
        _write_all(fd, data)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, _SEALS)
        if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & _SEALS != _SEALS:
            raise ValueError('PRELOAD_MANIFEST_SEAL_REJECTED')
        info = os.fstat(fd)
        bundle = StartupBundle(prepared.pinned, fd, info, hashlib.sha256(data).hexdigest())
        bundle.pass_fds()
        prepared.pinned._closed = True  # Transfer, do not close the map descriptors.
        return bundle
    except BaseException:
        os.close(fd)
        raise
