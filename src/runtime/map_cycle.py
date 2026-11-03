"""Pure map-cycle validation and migration; no runtime or filesystem effects."""
import json
import re

_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('MAP_CYCLE_ID_REJECTED')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('MAP_CYCLE_DUPLICATE_KEY')
        result[key] = value
    return result


def parse_cycle(data):
    """Decode a bounded JSON document, rejecting duplicate keys and non-JSON numbers."""
    if not isinstance(data, (str, bytes)):
        raise ValueError('MAP_CYCLE_JSON_REJECTED')
    try:
        if len(data.encode('utf-8') if isinstance(data, str) else data) > 65536:
            raise ValueError('MAP_CYCLE_JSON_REJECTED')
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('MAP_CYCLE_JSON_REJECTED')))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError('MAP_CYCLE_JSON_REJECTED') from None


def validate_cycle(cycle, presets):
    """Return an independent canonical cycle against an already validated preset mapping."""
    entries = cycle
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError('MAP_CYCLE_COUNT_REJECTED')
    if not isinstance(presets, dict):
        raise ValueError('MAP_CYCLE_PRESETS_REJECTED')
    result = []
    for entry in entries:
        key = _id(entry)
        # Manual is built in only when no explicit entry disables it.
        if key != 'manual' or key in presets:
            preset = presets.get(key)
            if not isinstance(preset, dict) or preset.get('enabled') is not True:
                raise ValueError('MAP_CYCLE_PRESET_UNAVAILABLE')
        result.append(key)
    return result


def migrate_cycle(gameplay, presets):
    """Use an explicit cycle if present; otherwise migrate the legacy selection."""
    if not isinstance(gameplay, dict):
        raise ValueError('MAP_CYCLE_GAMEPLAY_REJECTED')
    if 'map_cycle' in gameplay:
        cycle = gameplay['map_cycle']
    else:
        cycle = [gameplay.get('map_preset', 'manual')]
    return validate_cycle(cycle, presets)


def validate_resolved_cycle(cycle, source, gameplay):
    """Validate availability and resolve every occurrence with the production resolver.

    This reads the preset file and maps, but performs no writes. An absent preset
    file is allowed for manual mode; a present invalid one must never be ignored.
    """
    from pathlib import Path
    from .files import no_symlinks
    from .presets import load_presets, resolve_preset

    if not isinstance(gameplay, dict):
        raise ValueError('MAP_CYCLE_GAMEPLAY_REJECTED')
    source = no_symlinks(source)
    preset_file = no_symlinks(Path(source).parent / 'presets.json')
    try:
        preset_file.stat()
    except FileNotFoundError:
        presets = {}
    else:
        presets, _ = load_presets(preset_file)
    entries = validate_cycle(cycle, presets)
    maximum = gameplay.get('max_concurrent_matches', 3)
    if type(maximum) is not int or not 1 <= maximum <= 5:
        raise ValueError('MAP_CYCLE_MATCH_LIMIT_REJECTED')
    base = {key: value for key, value in gameplay.items()
            if key not in ('map_cycle', 'max_concurrent_matches')}
    for entry in entries:
        resolve_preset(source, {**base, 'map_preset': entry})
    return entries


def validate_full_cycle(cycle, source, gameplay):
    """Resolve every preset and additionally check the selected manual map."""
    from .manual_map import validate_manual_map

    entries = validate_resolved_cycle(cycle, source, gameplay)
    base = {key: value for key, value in gameplay.items()
            if key not in ('map_cycle', 'max_concurrent_matches')}
    for entry in entries:
        if entry == 'manual':
            validate_manual_map(source, base)
    return entries
