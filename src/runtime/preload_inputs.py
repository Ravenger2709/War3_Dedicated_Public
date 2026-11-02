"""Sealed, deduplicated map input snapshots for a future native startup catalog.

This module does not parse maps, produce native configuration, start GHost, or
implement IPC. Callers must first validate each map and supply its validated
SHA-256. Linux memfds are sealed before they are exposed to the caller.
"""
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat

from .presets import preset_id

_MAP_LIMIT = 128 * 1024 * 1024
_ACCOUNTED_LIMIT = 512 * 1024 * 1024
_LOAD_RESERVE = 128 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}')
_SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL


@dataclass(frozen=True)
class MapInput:
    preset_id: str
    path: Path
    validated_sha256: str


@dataclass(frozen=True)
class PinnedMap:
    preset_id: str
    fd: int
    size: int
    sha256: str
    _device: int
    _inode: int


class PinnedCatalog:
    """Owns sealed map FDs; keep it open until native has duplicated them."""
    def __init__(self, maps, cycle_indices, accounted_bytes):
        self.maps = tuple(maps)
        self.cycle_indices = tuple(cycle_indices)
        self.accounted_bytes = accounted_bytes
        self._closed = False

    def pass_fds(self):
        if self._closed:
            raise ValueError('PRELOAD_CATALOG_CLOSED')
        try:
            for entry in self.maps:
                info = os.fstat(entry.fd)
                seals = fcntl.fcntl(entry.fd, fcntl.F_GET_SEALS)
                if ((info.st_dev, info.st_ino, info.st_size) !=
                        (entry._device, entry._inode, entry.size) or seals & _SEALS != _SEALS):
                    raise ValueError('PRELOAD_CATALOG_CHANGED')
        except OSError:
            raise ValueError('PRELOAD_CATALOG_CHANGED') from None
        return tuple(entry.fd for entry in self.maps)

    def close(self):
        if not self._closed:
            self._closed = True
            for entry in self.maps:
                try:
                    info = os.fstat(entry.fd)
                    if (info.st_dev, info.st_ino) == (entry._device, entry._inode):
                        os.close(entry.fd)
                except OSError:
                    pass

    def __enter__(self):
        self.pass_fds()
        return self

    def __exit__(self, *_):
        self.close()


def _open_regular(path):
    """Open every path component without symlinks; caller closes returned FD."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.name in ('', '.', '..'):
        raise ValueError('PRELOAD_MAP_PATH_REJECTED')
    current = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=current)
            os.close(current)
            current = following
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=current)
    finally:
        os.close(current)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= _MAP_LIMIT:
            raise ValueError('PRELOAD_MAP_SIZE_REJECTED')
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _new_memfd():
    fd = os.memfd_create('war3-preload', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    if fd < 3:
        try:
            replacement = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
        finally:
            os.close(fd)
        return replacement
    return fd


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError('short memfd write')
        view = view[count:]


def pin_preload_inputs(inputs, *, default_map_bytes, max_games):
    """Verify validated digests, seal one copy per ID and return cycle indices.

    The accounting includes sealed inputs *and* future cache payloads, two
    defaults, worst-case game/lobby/pending copies and a load-buffer reserve.
    It is an admission estimate of payload bytes, not an RSS/cgroup limit.
    """
    if (not isinstance(inputs, (list, tuple)) or not 1 <= len(inputs) <= 32 or
            type(default_map_bytes) is not int or not 1 <= default_map_bytes <= _MAP_LIMIT or
            type(max_games) is not int or not 1 <= max_games <= 5):
        raise ValueError('PRELOAD_INPUT_REJECTED')
    unique, positions, indices = [], {}, []
    for item in inputs:
        if not isinstance(item, MapInput):
            raise ValueError('PRELOAD_INPUT_REJECTED')
        key = preset_id(item.preset_id)
        if not isinstance(item.validated_sha256, str) or not _HASH.fullmatch(item.validated_sha256):
            raise ValueError('PRELOAD_DIGEST_REJECTED')
        path = Path(item.path)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('PRELOAD_MAP_PATH_REJECTED')
        identity = (path, item.validated_sha256)
        if key in positions:
            index, original = positions[key]
            if identity != original:
                raise ValueError('PRELOAD_DUPLICATE_ID_CONFLICT')
        else:
            index = len(unique)
            positions[key] = (index, identity)
            unique.append((key, path, item.validated_sha256))
        indices.append(index)

    opened, snapshots = [], []
    try:
        for key, path, digest in unique:
            fd, info = _open_regular(path)
            opened.append((key, fd, info, digest))
        sizes = [info.st_size for _, _, info, _ in opened]
        largest = max([default_map_bytes, *sizes])
        # Unlike the C++ future-cache estimator, this also counts the sealed
        # memfd inputs retained while the native catalog is being constructed.
        accounted = (_LOAD_RESERVE + 2 * sum(sizes) + 2 * default_map_bytes +
                     largest * (max_games + 3))
        if accounted > _ACCOUNTED_LIMIT:
            raise ValueError('PRELOAD_PAYLOAD_BUDGET_REJECTED')
        for key, source, before, expected in opened:
            target = _new_memfd()
            try:
                digest = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    chunk = os.read(source, min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('PRELOAD_MAP_CHANGED')
                    digest.update(chunk)
                    _write_all(target, chunk)
                    remaining -= len(chunk)
                if os.read(source, 1) or _fingerprint(before) != _fingerprint(os.fstat(source)):
                    raise ValueError('PRELOAD_MAP_CHANGED')
                if digest.hexdigest() != expected:
                    raise ValueError('PRELOAD_DIGEST_MISMATCH')
                fcntl.fcntl(target, fcntl.F_ADD_SEALS, _SEALS)
                if fcntl.fcntl(target, fcntl.F_GET_SEALS) & _SEALS != _SEALS:
                    raise ValueError('PRELOAD_SEAL_REJECTED')
                os.lseek(target, 0, os.SEEK_SET)
                info = os.fstat(target)
                snapshots.append(PinnedMap(key, target, info.st_size, expected, info.st_dev, info.st_ino))
            except BaseException:
                os.close(target)
                raise
        return PinnedCatalog(snapshots, indices, accounted)
    except BaseException:
        for entry in snapshots:
            os.close(entry.fd)
        raise
    finally:
        for _, fd, _, _ in opened:
            os.close(fd)
