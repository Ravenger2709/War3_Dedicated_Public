#!/usr/bin/env bash
# Restore to a NEW private destination; never alters a running instance.
set -euo pipefail
if [[ $# -ne 2 ]]; then
    echo 'Usage: scripts/restore.sh backup.tar[.gz] NEW_DATA_DIR' >&2
    echo 'Optional: RESTORE_UID=10001 RESTORE_GID=10001 (requires chown permission).' >&2
    exit 2
fi
python3 - "$1" "$2" <<'PY'
import os
import ctypes
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile

archive, destination = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).absolute()
allowed = {"config", "maps", "logs", "replays", "state"}
staging = None
try:
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination must not exist")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise ValueError("destination parent must be an existing directory")
    uid = int(os.environ.get("RESTORE_UID", str(os.getuid())))
    gid = int(os.environ.get("RESTORE_GID", str(os.getgid())))
    if uid <= 0 or gid <= 0:
        raise ValueError("restore ownership must be non-root")
    # Validate the ENTIRE archive before extracting its first file.
    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] not in allowed
                    or not (member.isfile() or member.isdir())
                    or str(path) in names):
                raise ValueError("archive contains unsupported path, link or special file")
            names.add(str(path))
        staging = Path(tempfile.mkdtemp(prefix=".war3-restore-", dir=destination.parent))
        staging.chmod(0o700)
        for directory in allowed | {"warcraft"}:
            (staging / directory).mkdir(mode=0o700)
        for member in members:
            target = staging / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with source.extractfile(member) as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                target.chmod(0o600)
        for path in [*staging.rglob("*"), staging]:
            if path.is_dir():
                path.chmod(0o700)
            if path.stat().st_uid != uid or path.stat().st_gid != gid:
                os.chown(path, uid, gid)
        # Linux atomic no-replace rename closes the check/rename race.
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
            raise OSError(ctypes.get_errno(), "atomic restore rename rejected")
        staging = None
    print("Restore complete into a new private directory; Warcraft files must be mounted separately.")
except (OSError, ValueError, tarfile.TarError) as error:
    print(f"Restore rejected: {type(error).__name__}", file=sys.stderr)
    raise SystemExit(2)
finally:
    if staging is not None:
        shutil.rmtree(staging)
PY
