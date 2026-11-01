#!/usr/bin/env python3
"""Retain only public release receipts in a new version directory beside the gate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat


RECEIPTS = ("verification.json", "repo-digests.json", "ghcr-privacy.json")


def read_receipts(source):
    """Use explicit names and opened-file checks; never follow receipt symlinks."""
    receipts = {}
    directory = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in RECEIPTS:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError(f"receipt is not a regular file: {name}")
                data = stream.read()
            json.loads(data.decode("utf-8"))
            receipts[name] = data
    finally:
        os.close(directory)
    return receipts


def workflow_identity(env):
    # Never serialize the environment: it can contain registry credentials.
    identity = {name.lower(): env["GITHUB_" + name] for name in (
        "SERVER_URL", "REPOSITORY", "RUN_ID", "RUN_ATTEMPT", "SHA", "REF", "WORKFLOW",
    )}
    if not all(identity.values()):
        raise ValueError("workflow run identity is incomplete")
    identity["url"] = (
        f"{identity['server_url']}/{identity['repository']}/actions/runs/"
        f"{identity['run_id']}/attempts/{identity['run_attempt']}"
    )
    return identity


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def persist(source, acceptance, version, identity):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?", version):
        raise ValueError("invalid release version")
    if not acceptance.is_absolute() or any(c in str(acceptance) for c in "\r\n"):
        raise ValueError("acceptance path must be absolute and on one line")
    receipts = read_receipts(source)
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in receipts.items()}
    manifest = {
        "schema_version": 1,
        "version": version,
        "workflow_run": identity,
        "receipts": {name: {"sha256": checksums[name], "size_bytes": len(data)}
                     for name, data in receipts.items()},
    }
    receipts["release-evidence.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    receipts["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in receipts.items()
    ).encode("ascii")

    releases = acceptance.parent / "releases"
    releases.mkdir(mode=0o700, exist_ok=True)
    if releases.is_symlink():
        raise ValueError("releases directory must not be a symlink")
    destination = releases / version
    # Exclusive mkdir also refuses existing empty directories, files and symlinks.
    destination.mkdir(mode=0o700)
    try:
        for name, data in receipts.items():
            with (destination / name).open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        sync_directory(destination)
        sync_directory(releases)
        sync_directory(acceptance.parent)
    except BaseException:
        # A failed write must not leave a directory that looks like completed evidence.
        shutil.rmtree(destination)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        destination = persist(args.source, args.acceptance, args.version,
                              workflow_identity(os.environ))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Release evidence persistence failed: {error}\n")
    print(destination)


if __name__ == "__main__":
    main()
