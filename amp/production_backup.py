"""Private stopped-state backups; secret Wine/session/vendor trees are excluded."""
import argparse
import ctypes
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile

ALLOWED = {"config", "maps", "state", "sqlite", "logs", "replays"}
MAX_BYTES = 32 * 1024**3
MAX_MEMBERS = 100000


def backup(data, output):
    if Path(data).is_symlink():
        raise ValueError("backup data directory may not be symlinked")
    data, output = Path(data).resolve(), Path(output).absolute()
    if output.is_relative_to(data):
        raise ValueError("backup must be outside instance data")
    paths = []
    for name in sorted(ALLOWED):
        tree = data / name
        if not tree.is_dir() or tree.is_symlink():
            raise ValueError("backup data root missing or symlinked")
        for path in [tree, *sorted(tree.rglob("*"))]:
            if path.is_symlink():
                # Generated RO mapcfg aliases are rebuilt by entrypoint; never follow them.
                expected = "/data/config/maps/" + path.name
                if path.parent == data / "state/mapcfg" and path.suffix == ".cfg" and os.readlink(path) == expected:
                    continue
                raise ValueError("unexpected backup link")
            if not path.is_dir() and not path.is_file():
                raise ValueError("backup special file rejected")
            paths.append(path)
    if len(paths) > MAX_MEMBERS or sum(p.stat().st_size for p in paths if p.is_file()) > MAX_BYTES:
        raise ValueError("backup size limit exceeded")
    created = False
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "wb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for path in paths:
                archive.add(path, arcname=path.relative_to(data).as_posix(), recursive=False)
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise


def restore(archive, destination):
    archive, destination = Path(archive), Path(destination).absolute()
    if (destination.exists() or destination.is_symlink() or not destination.parent.is_dir()
            or destination.parent.is_symlink()):
        raise ValueError("restore requires a new destination with a real parent")
    stage = None
    try:
        with tarfile.open(archive, "r:*") as source:
            members, names, total = [], set(), 0
            for member in source:
                path = PurePosixPath(member.name)
                if (len(members) >= MAX_MEMBERS or path.is_absolute() or ".." in path.parts
                        or not path.parts or path.parts[0] not in ALLOWED
                        or member.name.rstrip("/") != str(path)
                        or not (member.isfile() or member.isdir()) or str(path) in names):
                    raise ValueError("unsafe backup member")
                total += member.size
                if total > MAX_BYTES or member.size < 0:
                    raise ValueError("backup size limit exceeded")
                names.add(str(path))
                members.append(member)
            stage = Path(tempfile.mkdtemp(prefix=".war3-production-restore-", dir=destination.parent))
            stage.chmod(0o700)
            for name in ALLOWED | {"warcraft", "prefix", "session"}:
                (stage / name).mkdir(mode=0o700)
            for member in members:
                target = stage / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with source.extractfile(member) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    target.chmod(0o600)
            for path in stage.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            # Linux atomic no-replace rename. A raced destination can never be overwritten.
            rename = ctypes.CDLL(None, use_errno=True).renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 1):
                raise OSError(ctypes.get_errno(), "restore rename rejected")
            stage = None
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def cli(action, argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    if action == "backup":
        parser.add_argument("--instance-dir", required=True, type=Path)
        parser.add_argument("--output", required=True, type=Path)
    else:
        parser.add_argument("archive", type=Path)
        parser.add_argument("new_data_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if action == "backup":
            from production import deployment, stopped
            spec = deployment(args.instance_dir)
            stopped(spec)
            backup(args.instance_dir / "data", args.output)
            print("Private stopped-state backup created; vendor/profile/prefix/session excluded.")
        else:
            restore(args.archive, args.new_data_dir)
            print("Private data restored into new directory. Supply local vendor/profile and rebuild prefix before start.")
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        parser.exit(2, "Backup/restore rejected; original data and existing destinations retained.\n")
    return 0
