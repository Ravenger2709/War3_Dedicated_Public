"""Read-only compilation of a complete map cycle, independent of startup.

Each occurrence is resolved from the same input gameplay. Compilation returns
only after every occurrence passes, including strict validation of manual maps.
The caller must keep input files stable during compilation and until use; this
module does not lock or snapshot them for native consumption.
"""
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .files import no_symlinks
from .manual_map import validate_manual_map
from .map_cycle import validate_cycle
from .presets import load_presets, resolve_preset


@dataclass(frozen=True)
class CompiledCycleEntry:
    preset_id: str
    gameplay: Mapping[str, object]
    ai: Mapping[str, str]
    mapcfg: str | None


def compile_cycle(cycle: list[str], source: str | Path,
                  gameplay: dict) -> tuple[CompiledCycleEntry, ...]:
    """Resolve a 1..32 entry cycle or raise without returning partial results.

    A manual entry has mapcfg=None, as in the production preset resolver; its
    operator-selected map is validated separately using the strict policy.
    This function reads inputs but never writes them or starts the cycle.
    """
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
    # This is a process-wide capacity, not a per-lobby gameplay setting.
    base = {key: value for key, value in gameplay.items()
            if key not in ('map_cycle', 'max_concurrent_matches')}
    result = []
    for preset_id in entries:
        game, ai, mapcfg = resolve_preset(source, {**base, 'map_preset': preset_id})
        if preset_id == 'manual':
            validate_manual_map(source, game)
        game = {key: value for key, value in game.items()
                if key not in ('map_cycle', 'max_concurrent_matches')}
        result.append(CompiledCycleEntry(preset_id, MappingProxyType(dict(game)),
                                         MappingProxyType(dict(ai)), mapcfg))
    return tuple(result)
