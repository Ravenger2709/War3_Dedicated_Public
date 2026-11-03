"""Prepare catalog inputs for a future owned native startup.

This preparatory interface does not launch a process or enable cycle mode.
The caller must retain the returned bundle until the native child has exited.
"""
from pathlib import Path

from .files import private_directory
from .preload_catalog import prepare_preload_cycle
from .preload_manifest import seal_startup_manifest
from .mpq import LIBRARY


def prepare_catalog_startup(cycle, source, gameplay, *, default_map_bytes,
                            ipc_directory, journal_directory, library=LIBRARY):
    """Validate the entire cycle and return (owned bundle, native overrides).

    `default_map_bytes` is the observed size of the default CMap that native
    will actually load; native must independently enforce it and memory limits.
    Directories must already exist and be private. Values are config overrides,
    not a complete config; publishing them is deliberately left to integration.
    """
    paths = []
    for raw in (ipc_directory, journal_directory):
        path = Path(raw)
        if not path.is_absolute() or '..' in path.parts or any(ord(c) < 32 for c in str(path)):
            raise ValueError('CATALOG_STARTUP_PATH_REJECTED')
        if not path.is_dir():
            raise ValueError('CATALOG_STARTUP_PATH_REJECTED')
        private_directory(path)
        paths.append(str(path))
    if paths[0] == paths[1]:
        raise ValueError('CATALOG_STARTUP_PATH_REJECTED')
    prepared = prepare_preload_cycle(cycle, source, gameplay,
                                     default_map_bytes=default_map_bytes, library=library)
    try:
        bundle = seal_startup_manifest(prepared)
    except BaseException:
        prepared.close()
        raise
    try:
        bundle.pass_fds()
        if default_map_bytes is None:
            default_index = prepared.cycle_indices[0]
            bundle.default_entry = prepared.entries[default_index]
            bundle.default_sha256 = bundle.maps[default_index].sha256
        overrides = {
            'bot_preload_manifest_fd': str(bundle.manifest_fd),
            'bot_preload_manifest_sha256': bundle.manifest_sha256,
            'bot_preload_ipc_dir': paths[0],
            'bot_lifecycle_event_dir': paths[1],
            'bot_start_reservation_gate': '1',
            'bot_maxgames': str(prepared.max_games),
            'autohost_maxgames': str(prepared.max_games),
        }
        if default_map_bytes is None:
            overrides['bot_preload_default_sha256'] = bundle.default_sha256
        return bundle, overrides
    except BaseException:
        bundle.close()
        raise
