"""Prepare a validated cycle from sealed map bytes, before native startup.

This is not a native startup manifest or request/ACK transport. The returned
catalog owns FDs; no map/path is reopened during its later inspection.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .config import gameplay_config
from .manual_map import load_manual_selection, validate_manual_capacity
from .map_cycle import validate_cycle
from .mpq import LIBRARY, _check_sealed_fd, extract_sealed_map
from .preload_inputs import MapInput, _fingerprint, _open_regular, pin_preload_inputs
from .presets import (AI_OFF, _resolve_named_snapshot, map_info, parse_presets,
                      script_evidence, short_title)


@dataclass(frozen=True)
class PreparedPreset:
    preset_id: str
    fd_index: int
    map_sha256: str
    gameplay: Mapping[str, object]
    ai: Mapping[str, str]
    mapcfg: str


class PreparedPreload:
    """Owns sealed descriptors and settings from a single preset document."""
    def __init__(self, pinned, entries, max_games, default_map_bytes, preset_sha256):
        self.pinned = pinned
        self.entries = tuple(entries)
        self.cycle_indices = pinned.cycle_indices
        self.max_games = max_games
        self.default_map_bytes = default_map_bytes
        self.preset_sha256 = preset_sha256

    def close(self):
        self.pinned.close()

    def __enter__(self):
        self.pinned.pass_fds()
        return self

    def __exit__(self, *_):
        self.close()


def _read_config_snapshot(path):
    fd, before = _open_regular(path)
    try:
        if before.st_size > 65536:
            raise ValueError('PRESET_FILE_REJECTED')
        data = bytearray()
        while len(data) < before.st_size:
            chunk = os.read(fd, min(65536, before.st_size - len(data)))
            if not chunk:
                raise ValueError('PRESET_FILE_CHANGED')
            data.extend(chunk)
        if os.read(fd, 1) or _fingerprint(before) != _fingerprint(os.fstat(fd)):
            raise ValueError('PRESET_FILE_CHANGED')
        return bytes(data)
    finally:
        os.close(fd)


def _hash_source(path):
    fd, before = _open_regular(path)
    try:
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError('PRELOAD_MAP_CHANGED')
            digest.update(chunk); remaining -= len(chunk)
        if os.read(fd, 1) or _fingerprint(before) != _fingerprint(os.fstat(fd)):
            raise ValueError('PRELOAD_MAP_CHANGED')
        return digest.hexdigest()
    finally:
        os.close(fd)


def _analyze_pinned(entry, library):
    def checked_identity():
        info = _check_sealed_fd(entry.fd)
        if (info.st_dev, info.st_ino, info.st_size) != (entry._device, entry._inode, entry.size):
            raise ValueError('PRELOAD_CATALOG_CHANGED')
    checked_identity()
    digest = hashlib.sha256()
    offset = 0
    while offset < entry.size:
        chunk = os.pread(entry.fd, min(1024 * 1024, entry.size - offset), offset)
        if not chunk:
            raise ValueError('PRELOAD_CATALOG_CHANGED')
        digest.update(chunk); offset += len(chunk)
    if digest.hexdigest() != entry.sha256:
        raise ValueError('PRELOAD_DIGEST_MISMATCH')
    members = extract_sealed_map(entry.fd, library=library)
    checked_identity()
    info = map_info(members['war3map.w3i'])
    script = members.get('war3map.j', members.get('scripts\\war3map.j'))
    try:
        evidence = script_evidence(script)
        title = short_title(info.pop('title'), members.get('war3map.wts', b''))
    except UnicodeError:
        raise ValueError('MAP_TEXT_REJECTED') from None
    return {**info, 'lobby_name': title, 'map_sha256': entry.sha256,
            'script_sha256': hashlib.sha256(script).hexdigest(), 'ai_evidence': evidence}


def prepare_preload_cycle(cycle, source, gameplay, *, default_map_bytes, library=LIBRARY):
    """Validate distinct presets over sealed bytes and return immutable settings.

    The caller must close the result. `default_map_bytes` must later be checked against the actual
    native default map; the estimate is not an RSS limit.
    """
    if not isinstance(gameplay, dict):
        raise ValueError('MAP_CYCLE_GAMEPLAY_REJECTED')
    source = Path(source)
    if not source.is_absolute() or '..' in source.parts:
        raise ValueError('PRELOAD_SOURCE_REJECTED')
    max_games = gameplay.get('max_concurrent_matches', 3)
    if type(max_games) is not int or not 1 <= max_games <= 5:
        raise ValueError('MAP_CYCLE_MATCH_LIMIT_REJECTED')
    try:
        raw = _read_config_snapshot(source.parent / 'presets.json')
    except FileNotFoundError:
        raw = None
        presets, capabilities = {}, []
    else:
        presets, capabilities = parse_presets(raw)
    cycle = validate_cycle(cycle, presets)
    manual_path, manual_cfg = load_manual_selection(source) if 'manual' in cycle else (None, None)
    base = {key: value for key, value in gameplay.items()
            if key not in ('map_cycle', 'max_concurrent_matches')}
    sources = {}
    for key in cycle:
        if key not in sources:
            path = manual_path if key == 'manual' else source.parent.parent / 'maps' / presets[key]['map_filename']
            sources[key] = MapInput(key, path, _hash_source(path))
    # Automatic cycle startup uses its first sealed preset as the default map.
    auto_default = default_map_bytes is None
    if auto_default:
        first_fd, first_info = _open_regular(sources[cycle[0]].path)
        try: default_map_bytes = first_info.st_size
        finally: os.close(first_fd)
    # pin_preload_inputs performs its own bounded copy and digest comparison.
    pinned = pin_preload_inputs([sources[key] for key in cycle],
                                default_map_bytes=default_map_bytes, max_games=max_games)
    try:
        pinned.pass_fds()
        if auto_default and pinned.maps[pinned.cycle_indices[0]].size != default_map_bytes:
            raise ValueError('PRELOAD_DEFAULT_MAP_CHANGED')
        result = []
        for index, entry in enumerate(pinned.maps):
            game = gameplay_config({**base, 'map_preset': entry.preset_id})
            analysis = _analyze_pinned(entry, library)
            if entry.preset_id == 'manual':
                validate_manual_capacity(game, analysis)
                resolved, ai = game, AI_OFF
                cfg = ''.join(key + ' = ' + value + '\n' for key, value in sorted(manual_cfg.items()))
                # Validate the exact canonical bytes before admitting this entry.
                from .preload_manifest import _mapcfg
                _mapcfg(cfg)
            else:
                resolved, ai, cfg, _, _ = _resolve_named_snapshot(
                    source, game, presets, capabilities, sealed_analysis=analysis)
            result.append(PreparedPreset(entry.preset_id, index, entry.sha256,
                                         MappingProxyType(dict(resolved)),
                                         MappingProxyType(dict(ai)), cfg))
        return PreparedPreload(pinned, result, max_games, default_map_bytes, hashlib.sha256(raw).hexdigest() if raw is not None else None)
    except BaseException:
        pinned.close()
        raise
