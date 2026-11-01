"""Linux secret-file I/O with descriptor-based checks and redacted failures.

No credential is accepted from argv or environment. This module does not write
GHost configuration or implement authentication. Python strings cannot promise
secure memory erasure; keep loaded credentials scoped to the future adapter.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat

MAX_SECRET_BYTES = 4096


class SecretError(ValueError):
    """Messages contain fixed error codes, never input contents or paths."""


@dataclass(frozen=True, slots=True, repr=False)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


@contextmanager
def _parent(path: str | Path):
    # Do not resolve(): that would hide symlinks. Open each component relative
    # to its already-open parent so path substitution cannot redirect the read.
    value = os.fspath(path)
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        raise SecretError("SECRET_ABSOLUTE_PATH_REQUIRED")
    parts = value.split("/")[1:]
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise SecretError("SECRET_INVALID_PATH")
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    except OSError:
        raise SecretError("SECRET_FILE_ACCESS_DENIED") from None
    finally:
        if fd is not None:
            os.close(fd)


def _check_file(info) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SecretError("SECRET_REGULAR_SINGLE_LINK_REQUIRED")
    if info.st_uid != os.geteuid():
        raise SecretError("SECRET_OWNER_MISMATCH")
    if stat.S_IMODE(info.st_mode) not in (0o400, 0o600):
        raise SecretError("SECRET_MODE_MUST_BE_0400_OR_0600")
    if not 0 < info.st_size <= MAX_SECRET_BYTES:
        raise SecretError("SECRET_SIZE_INVALID")


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise SecretError("SECRET_SCHEMA_INVALID")
        out[key] = value
    return out


def _validate(value) -> Credentials:
    if (not isinstance(value, dict) or set(value) != {"version", "username", "password"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise SecretError("SECRET_SCHEMA_INVALID")
    for key, limit in (("username", 128), ("password", 1024)):
        item = value[key]
        if (not isinstance(item, str) or not 0 < len(item) <= limit
                or any(ord(c) < 32 or ord(c) == 127 for c in item)
                or any(0xD800 <= ord(c) <= 0xDFFF for c in item)):
            raise SecretError("SECRET_SCHEMA_INVALID")
    return Credentials(value["username"], value["password"])


def load_credentials(path: str | Path) -> Credentials:
    with _parent(path) as (directory, name):
        fd = None
        try:
            # NONBLOCK avoids hanging on a substituted FIFO before fstat.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
            before = os.fstat(fd)
            _check_file(before)
            data = bytearray()
            while len(data) <= MAX_SECRET_BYTES:
                block = os.read(fd, MAX_SECRET_BYTES + 1 - len(data))
                if not block:
                    break
                data.extend(block)
            after = os.fstat(fd)
            _check_file(after)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SecretError("SECRET_CHANGED_DURING_READ")
            if len(data) != before.st_size or len(data) > MAX_SECRET_BYTES:
                raise SecretError("SECRET_SIZE_INVALID")
            try:
                value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
            except (ValueError, UnicodeError, RecursionError):
                raise SecretError("SECRET_SCHEMA_INVALID") from None
            return _validate(value)
        except OSError:
            raise SecretError("SECRET_FILE_ACCESS_DENIED") from None
        finally:
            if fd is not None:
                os.close(fd)


def provision_credentials(path: str | Path, username: str, password: str) -> None:
    credentials = _validate({"version": 1, "username": username, "password": password})
    data = (json.dumps({"version": 1, "username": credentials.username, "password": credentials.password}, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_SECRET_BYTES:
        raise SecretError("SECRET_SIZE_INVALID")
    with _parent(path) as (directory, name):
        parent_info = os.fstat(directory)
        if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
            raise SecretError("SECRET_PARENT_MUST_BE_OWNED_0700")
        fd = None
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            # Explicit mode also works under an unusually restrictive umask.
            os.fchmod(fd, 0o600)
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise OSError("write failed")
                offset += written
            os.fsync(fd)
            _check_file(os.fstat(fd))
        except OSError:
            if fd is not None:
                os.unlink(name, dir_fd=directory)
            raise SecretError("SECRET_CREATE_FAILED_OR_EXISTS") from None
        finally:
            if fd is not None:
                os.close(fd)
