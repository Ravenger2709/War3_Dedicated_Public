"""Private AMP account input and stopped-only, offline Star profile transactions."""
from contextlib import contextmanager, ExitStack
import configparser
import ctypes
import fcntl
import getpass
from importlib import import_module
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

INPUT = Path("control/account-input.json")
BACKUP = Path("control/account-profile.previous")
FILES = {"launcher.ini", "iccup.ini"}


def _production_module():
    """Resolve the sibling lazily, preserving package/flat context and error types."""
    return import_module(".production", __package__) if __package__ else import_module("production")


class AccountError(ValueError):
    """Only fixed, non-sensitive messages may be supplied here."""

    @property
    def public_message(self):
        key = self.args[0] if self.args and type(self.args[0]) is str else ""
        return {
            "Enter both account username and password, or clear both to keep the saved profile.":
                "Account setup rejected: enter both username and password, or clear both to retain the saved profile.",
            "Account input rejected; check its schema, fields and private permissions.":
                "Account setup rejected: invalid account JSON or private permissions.",
            "Account profile is in use; stop its AMP owner before account setup.":
                "Account setup rejected: profile is in use; stop its AMP owner first.",
            "Account generation requires deployment UID/GID to match the current non-root AMP data owner.":
                "Account setup rejected: deployment owner must match the current non-root AMP owner.",
        }.get(key, "Account setup rejected; verify private account input and stopped state.")


class ProfileRecoveryError(AccountError):
    """An old profile must survive staging cleanup for operator recovery."""


@contextmanager
def private_stage(instance):
    work = Path(tempfile.mkdtemp(prefix="war3-account-", dir=instance / "control"))
    retain = False
    try:
        work.chmod(0o700)
        yield work
    except ProfileRecoveryError:
        retain = True
        raise
    finally:
        (work / "canonical.json").unlink(missing_ok=True)
        (work / "defaults.ini").unlink(missing_ok=True)
        if not retain:
            shutil.rmtree(work)


@contextmanager
def atomic_signals():
    # Record each completed rename before AMP Stop/SIGINT can initiate rollback.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def real_path(path):
    path = Path(path).absolute()
    if ".." in path.parts or any(ord(c) < 32 or c == "," for c in str(path)):
        raise AccountError("Account path rejected.")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise AccountError("Symlinked account path rejected.")
    return path


def directory(path):
    path = real_path(path)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise AccountError("Account directories must be owned and mode 0700.")
    return path


def check_file(info, limit):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= limit):
        raise AccountError("Account files must be owned, single-link, bounded and mode 0600.")


def private_read(path, limit=4096):
    real_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        check_file(before, limit)
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise AccountError("Account input changed during reading.")
        return data
    finally:
        os.close(fd)


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AccountError("Account input has duplicate fields.")
        value[key] = item
    return value


def validate(value):
    if (not isinstance(value, dict) or set(value) != {"version", "username", "password"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise AccountError("Account input requires version 1, username and password only.")
    for key in ("username", "password"):
        text = value[key]
        if (not isinstance(text, str) or any(ord(c) < 32 or ord(c) == 127
                or 0xD800 <= ord(c) <= 0xDFFF for c in text)
                or len(text.encode("utf-8")) > 64):
            raise AccountError("Account fields must contain at most 64 UTF-8 bytes without controls.")
    if bool(value["username"]) != bool(value["password"]):
        raise AccountError("Enter both account username and password, or clear both to keep the saved profile.")
    return value


def read_input(instance):
    path = real_path(instance / INPUT)
    if not path.exists():  # Older installations retain their derived-profile workflow.
        return {"version": 1, "username": "", "password": ""}
    try:
        return validate(json.loads(private_read(path).decode("utf-8"), object_pairs_hook=unique))
    except AccountError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise AccountError("Account input rejected; check its schema, fields and private permissions.") from None


@contextmanager
def locked(path, create=False):
    real_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                 | (os.O_CREAT if create else 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise AccountError("Account lock must be an owned single-link file with mode 0600.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AccountError("Account profile is in use; stop its AMP owner before account setup.") from None
        yield
    finally:
        os.close(fd)


def profile_files(profile, empty=False):
    directory(profile)
    names = {p.name for p in profile.iterdir()}
    if empty and not names:
        return
    if names != FILES:
        raise AccountError("Account profile must contain exactly launcher.ini and iccup.ini.")
    for name in FILES:
        path = real_path(profile / name)
        check_file(path.lstat(), 16384)


def loopback_defaults():
    # Qt gateway format: empty legacy list, selected index zero, one local gateway.
    gateway = json.dumps('@ByteArray(' + json.dumps({"Local relay": "127.0.0.1:16112"},
                         separators=(",", ":")) + ')')
    return ("[General]\nConnections=\nCurrConnections=0\nSiteConnections=" + gateway
            + "\nUserConnections=" + gateway + "\n")


def generator_owner(spec):
    owner = spec.get("uid"), spec.get("gid")
    if (any(type(value) is not int or value <= 0 for value in owner)
            or owner != (os.geteuid(), os.getegid())):
        raise AccountError("Account generation requires deployment UID/GID to match the current non-root AMP data owner.")
    return f"{owner[0]}:{owner[1]}"


def generator_command(instance, spec, work):
    production = _production_module()
    host = Path(spec.get("host_instance_dir", str(instance)))
    relative = work.relative_to(instance)
    args = [*production.DOCKER, "run", "--rm", "--pull=never", "--name", work.name,
            "--network", "none", "--read-only", "--user", generator_owner(spec),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--ulimit", "core=0",
            "--pids-limit", "32", "--memory", "128m", "--memory-swap", "128m", "--cpus", "0.5",
            "--no-healthcheck", "--restart=no", "--log-driver", "none", "--stop-timeout", "1",
            "--entrypoint", "python3", "--workdir", "/opt/war3",
            "--env", "PYTHONDONTWRITEBYTECODE=1"]
    binds = ((host / relative / "canonical.json", "/account/input.json", True),
             (host / "vendor/star/Launcher.exe", "/account/Launcher.exe", True),
             (host / relative / "defaults.ini", "/account/defaults.ini", True),
             (host / relative / "profile", "/account/profile", False))
    for source, target, readonly in binds:
        args += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
    return [*args, spec["image_id"], "-m", "src.bridge.star_profile", "--secret", "/account/input.json",
            "--output", "/account/profile", "--defaults", "/account/defaults.ini",
            "--launcher", "/account/Launcher.exe"]


def generate(instance, spec, work, value):
    production = _production_module()
    canonical = work / "canonical.json"
    try:
        production.private_json(canonical, value)
        defaults = work / "defaults.ini"
        with defaults.open("x", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(loopback_defaults())
        args = generator_command(instance, spec, work)
        # Never relay subprocess output/errors: they may contain parser input.
        try:
            subprocess.run(args, env=production.environment(), check=True, timeout=45,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            # A timed-out/interrupted Docker client can leave its container alive.
            # This unique name belongs only to this invocation, never the runtime.
            subprocess.run([*production.DOCKER, "rm", "--force", work.name],
                           env=production.environment(), check=False, timeout=10,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        canonical.unlink(missing_ok=True)


def verify_generated(profile, username):
    profile_files(profile)
    cfg = configparser.RawConfigParser()
    cfg.optionxform = str
    cfg.read_string(private_read(profile / "launcher.ini", 16384).decode("utf-8"))
    general = cfg["General"]
    defaults = configparser.RawConfigParser()
    defaults.optionxform = str
    defaults.read_string(loopback_defaults())
    if (cfg.sections() != ["General"] or general["AutoLogin"] != "true"
            or json.loads(general["Username"]) != username or general["Password"] != ""
            or any(general[k] != v for k, v in defaults["General"].items())):
        raise AccountError("Generated account profile rejected.")
    # Derived login bytes stay private; never return, hash or log them.
    logins = json.loads(json.loads(general["ListLogins"]))
    if (not isinstance(logins, dict) or set(logins) != {username}
            or not isinstance(logins[username], str) or len(logins[username]) != 74
            or any(c not in "0123456789abcdef" for c in logins[username])):
        raise AccountError("Generated account profile rejected.")
    game = configparser.RawConfigParser()
    game.read_string(private_read(profile / "iccup.ini", 16384).decode("utf-8"))
    if (game["Warcraft III"]["AutoLoginOn"] != "1"
            or any(game["Common"][k] != "0" for k in ("AutoUpdate", "AutoUpdateMap", "RunAtStart"))):
        raise AccountError("Generated account profile rejected.")
    # The existing runtime worker fixes MainExe to its mounted game metadata.


def exchange(left, right):
    """Linux atomic directory exchange; unsupported filesystems fail closed."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(left), -100, os.fsencode(right), 2):  # AT_FDCWD, RENAME_EXCHANGE
        raise OSError(ctypes.get_errno(), "account profile exchange rejected")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def profile_update(instance, spec):
    """Yield with the staged profile installed; roll back if launch preflight fails.

    No configured fields means use the existing profile without reading its login.
    Callers must leave this context before starting the actual runtime.
    """
    # AMP Stop during the offline helper must run finally/rollback cleanup too.
    def terminated(number, frame):
        raise SystemExit(128 + number)
    previous = signal.signal(signal.SIGTERM, terminated)
    try:
        with _profile_update(instance, spec):
            yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def _profile_update(instance, spec):
    production = _production_module()
    instance = directory(instance)
    if production.deployment(instance) != spec:
        raise AccountError("Account deployment identity changed.")
    production.image_preflight(instance, spec)
    directory(instance / "control")
    directory(instance / "vendor")
    profile = real_path(instance / "vendor/profile")
    backup = real_path(instance / BACKUP)
    with ExitStack() as locks:
        locks.enter_context(locked(instance / "control/account-setup.lock", create=True))
        if any((instance / "control").glob("war3-account-*")):
            raise AccountError("Inspect retained private account staging before retrying setup.")
        if profile.exists():
            directory(profile)
            if (profile / "launcher.ini").exists() or (profile / "launcher.ini").is_symlink():
                locks.enter_context(locked(profile / "launcher.ini"))
        value = read_input(instance)
        if not value["username"]:
            yield
            return
        generator_owner(spec)
        launcher = real_path(instance / "vendor/star/Launcher.exe")
        if not launcher.is_file() or launcher.stat().st_nlink != 1:
            raise AccountError("Account generation requires a regular single-link local Launcher.exe.")
        existed = profile.exists()
        if existed:
            profile_files(profile, empty=True)
        if backup.exists():
            profile_files(backup, empty=True)
        with private_stage(instance) as work:
            staged = work / "profile"
            staged.mkdir(mode=0o700)
            generate(instance, spec, work, value)
            verify_generated(staged, value["username"])
            for name in FILES:
                with (staged / name).open("rb") as stream:
                    os.fsync(stream.fileno())
            sync_directory(staged)
            locks.enter_context(locked(staged / "launcher.ini"))
            production.stopped(spec)  # Recheck after hashing, before touching the active profile.
            if backup.exists():
                shutil.rmtree(backup)
            old = staged
            installed = False
            try:
                with atomic_signals():
                    if existed:
                        exchange(staged, profile)
                    else:
                        os.replace(staged, profile)
                    installed = True
                sync_directory(profile.parent)
                yield
                if existed:
                    with atomic_signals():
                        os.replace(staged, backup)
                        old = backup
                sync_directory(instance / "control")
            except BaseException:
                if installed:
                    try:
                        with atomic_signals():
                            if existed:
                                exchange(old, profile)
                            else:
                                os.replace(profile, staged)
                    except BaseException:
                        # Even a filesystem failure during rollback must never
                        # cause finally cleanup to delete the old login profile.
                        if existed and old == staged:
                            try:
                                os.replace(staged, backup)
                            except BaseException:
                                raise ProfileRecoveryError("Account rollback failed; old profile retained in private staging.") from None
                        raise AccountError("Account rollback failed; keep AMP stopped and restore the private previous profile.") from None
                    # If final backup sync failed, leave only the old active profile.
                    if existed and old == backup:
                        shutil.rmtree(backup)
                    sync_directory(profile.parent)
                raise


def cli(argv=None):
    """Interactive input storage only; AMP Start/Restart performs profile setup."""
    import argparse
    production = _production_module()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        instance = directory(args.instance_dir)
        production.deployment(instance)
        directory(instance / "control")
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise AccountError("Use a terminal for account input, or edit the private JSON in AMP File Manager.")
        with locked(instance / "control/account-setup.lock", create=True):
            username = input("Account username (blank keeps saved profile): ")
            password = getpass.getpass("Account password (blank keeps saved profile): ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise AccountError("Account password confirmation did not match.")
            value = validate({"version": 1, "username": username, "password": password})
            # Refuse unsafe destinations even though replacement itself is atomic.
            if (instance / INPUT).exists():
                private_read(instance / INPUT)
            production.private_json(real_path(instance / INPUT), value)
        print("Private account input saved; apply through AMP Start/Restart.")
        return 0
    except (Exception, KeyboardInterrupt):
        print("Account input rejected; verify private files and use a terminal or AMP File Manager.")
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
