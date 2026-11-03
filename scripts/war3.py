#!/usr/bin/env python3
"""Standalone Docker operator commands; Python 3 stdlib, no AMP service required."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "amp"))
import account
import production
from amp import bootstrap

DEFAULT_INSTANCE = Path.home() / ".war3-dedicated" / "instance"
MARKER = "control/standalone.json"
OWNER_LABEL = "war3.standalone.owner"
PATH_LABEL = "war3.standalone.instance"


class StandaloneError(ValueError):
    """Fixed operator-facing messages only; never include Docker/account output."""


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments, including an accidental
        # --password VALUE. Keep every argument error free of supplied values.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid command arguments; use --help. Never pass credentials as arguments.\n")


def docker(args, *, timeout=30, stream=False):
    result = subprocess.run([*production.DOCKER, *args], env=production.environment(),
                            check=True, timeout=timeout, capture_output=not stream)
    return result.returncode if stream else result.stdout


def private_object(path):
    value = json.loads(account.private_read(path, 2 * 1024 * 1024), object_pairs_hook=account.unique)
    if not isinstance(value, dict):
        raise StandaloneError("Private instance metadata must be a JSON object.")
    return value


def instance_config(path):
    if (os.getuid() == 0 or os.geteuid() != os.getuid() or os.getgid() <= 0
            or os.getegid() != os.getgid()):
        raise StandaloneError("Run as the non-root data owner, using its normal UID and GID.")
    instance = account.directory(bootstrap.safe_path(path))
    account.directory(instance / "control")
    marker = private_object(instance / MARKER)
    if (set(marker) != {"version", "owner", "instance_dir"} or type(marker["version"]) is not int
            or marker["version"] != 1 or marker["instance_dir"] != str(instance)
            or not isinstance(marker["owner"], str) or not re.fullmatch(r"[0-9a-f]{32}", marker["owner"])):
        raise StandaloneError("This directory is not a matching standalone setup; do not adopt an AMP instance.")
    private_object(instance / "deployment.json")
    private_object(instance / "acceptance.json")
    spec = production.deployment(instance)  # Retains the exact approved image gate.
    if (spec["uid"] != os.getuid() or spec["gid"] != os.getgid()
            or spec.get("host_instance_dir") != str(instance)):
        raise StandaloneError("Instance owner and host bind path must match this user and directory.")
    for name in ("data", "vendor", *("data/" + n for n in (*production.RO, *production.RW)),
                 *("vendor/" + n for n in production.VENDOR)):
        account.directory(instance / name)
    return instance, spec, marker


@contextmanager
def operation(path, exclusive=True):
    instance, spec, marker = instance_config(path)
    if not exclusive:
        yield instance, spec, marker
        return
    with account.locked(instance / "control/standalone.lock", create=True):
        yield instance, spec, marker


def path_label(instance):
    return hashlib.sha256(os.fsencode(instance)).hexdigest()


def run_command(instance, spec, marker, *, foreground=False, restart_policy="no"):
    if restart_policy not in ("no", "unless-stopped") or foreground and restart_policy != "no":
        raise StandaloneError("Foreground mode requires restart policy 'no'.")
    command = production.command(instance, spec)
    # Keep the shared security, mounts, port, image and shutdown contract intact.
    command.remove("--rm")  # Retain stopped container logs until the next start.
    command[command.index("--restart=no")] = "--restart=" + restart_policy
    extra = ["--label", OWNER_LABEL + "=" + marker["owner"],
             "--label", PATH_LABEL + "=" + path_label(instance)]
    if not foreground:
        extra.append("--detach")
    command[-1:-1] = extra
    return command


def owned_container(instance, spec, marker, reference=None):
    if reference is None:
        ids = docker(["ps", "-aq", "--filter", "name=^/" + spec["container_name"] + "$"]).decode().split()
        if not ids:
            return None
        if len(ids) != 1:
            raise StandaloneError("Container lookup is ambiguous; no container changed.")
        reference = ids[0]
    if not re.fullmatch(r"[0-9a-f]{12,64}", reference):
        raise StandaloneError("Container identity rejected.")
    values = json.loads(docker(["container", "inspect", reference]))
    if not isinstance(values, list) or len(values) != 1:
        raise StandaloneError("Container inspection rejected.")
    meta = values[0]
    config, host = meta.get("Config", {}), meta.get("HostConfig", {})
    labels = config.get("Labels") or {}
    expected = []
    for name in (*production.RO, *production.RW):
        expected.append((str(instance / "data" / name), "/data/" + name, name in production.RW))
    expected += [(str(instance / "vendor" / n), "/vendor/" + n, False) for n in production.VENDOR]
    mounts = [(m.get("Source"), m.get("Destination"), m.get("RW")) for m in meta.get("Mounts", [])
              if m.get("Type") == "bind"]
    policy = host.get("RestartPolicy", {}).get("Name")
    if (not isinstance(meta.get("Id"), str) or not re.fullmatch(r"[0-9a-f]{64}", meta["Id"])
            or meta.get("Name") != "/" + spec["container_name"] or meta.get("Image") != spec["image_id"]
            or labels.get(OWNER_LABEL) != marker["owner"] or labels.get(PATH_LABEL) != path_label(instance)
            or config.get("User") != f'{spec["uid"]}:{spec["gid"]}'
            or host.get("ReadonlyRootfs") is not True or host.get("Privileged") is not False
            or host.get("NetworkMode") != "bridge" or host.get("AutoRemove") is not False
            or host.get("PortBindings") != {"6113/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6113"}]}
            or host.get("CapDrop") != ["ALL"] or not host.get("Init")
            or "no-new-privileges:true" not in host.get("SecurityOpt", [])
            or policy not in ("no", "unless-stopped") or sorted(mounts) != sorted(expected)):
        raise StandaloneError("Existing container does not match this standalone owner and runtime; no container changed.")
    return meta


def remove_stopped(instance, spec, marker, meta):
    current = owned_container(instance, spec, marker, meta["Id"])
    state = current["State"]
    if state.get("Running") or state.get("Restarting") or state.get("Paused"):
        raise StandaloneError("Container must be fully stopped before removal.")
    docker(["container", "rm", current["Id"]])  # No --force; never remove data volumes.


def stop_container(instance, spec, marker, *, remove=False):
    meta = owned_container(instance, spec, marker)
    if meta is None:
        return None
    if meta["State"].get("Paused"):
        raise StandaloneError("Container is paused; resolve its state before stopping.")
    if meta["State"].get("Running") or meta["State"].get("Restarting"):
        docker(["container", "stop", "--time", "120", meta["Id"]], timeout=135)
        meta = owned_container(instance, spec, marker, meta["Id"])
    if meta["State"].get("Running") or meta["State"].get("Restarting"):
        raise StandaloneError("Container has not stopped; data and container retained.")
    if meta["State"].get("ExitCode", 0) != 0:
        raise StandaloneError("Container stopped with a nonzero exit; inspect logs before starting again.")
    if remove:
        remove_stopped(instance, spec, marker, meta)
    return meta


@contextmanager
def selected_preset_update(instance, preset):
    """Explicitly reconcile legacy and cycle selections under the start transaction."""
    transaction = None
    def apply():
        nonlocal transaction
        if preset is None:
            return
        path = instance / "data/config/amp-settings.json"
        snapshot = production._preset_snapshot(path, private=True, limit=2 * 1024 * 1024)
        if snapshot[0] is None:
            raise ValueError("missing settings")
        raw = json.loads(snapshot[0], object_pairs_hook=account.unique)
        if not isinstance(raw, dict):
            raise ValueError("settings object required")
        # Check the existing cycle separately so an explicit choice can resolve a
        # conflict, but cannot discard a staged multi-entry or malformed cycle.
        current = dict(raw)
        if "map_cycle" in current:
            current.pop("map_preset", None)
        normalized = production.settings(current)
        if len(normalized["map_cycle"]) != 1:
            raise production.SettingsError("MAP_CYCLE_OVERRIDE_CONFLICT")
        candidate = dict(raw)
        candidate.pop("map_preset", None)
        candidate["map_cycle"] = [preset]
        candidate["max_concurrent_matches"] = normalized["max_concurrent_matches"]
        production.settings(candidate)
        transaction = production._PresetTransaction(instance, snapshot, path=path, limit=2 * 1024 * 1024,
                                                     history_name="preset-metadata-history")
        transaction.preserve_original = True
        transaction.install(candidate)
        return transaction
    try:
        yield apply
        if transaction is not None:
            transaction.check()
    except BaseException:
        if transaction is not None and not transaction.conflicted:
            try:
                transaction.rollback()
            except (OSError, ValueError):
                transaction.conflict()
        raise
    finally:
        if transaction is not None:
            transaction.close()


def prepare_start(instance, spec, *, map_preset=None):
    # A checkout upgrade must not compile new settings for an older accepted
    # runtime. Reuse the bootstrap's source-bound schema check on every start.
    bootstrap.supervisor_source(private_object(instance / "acceptance.json"))
    # Shared helper performs offline profile generation, stopped/image checks and
    # rollback. All its contexts/locks must close before the runtime is launched.
    with production.supervisor_update(instance) as config_writer, \
            selected_preset_update(instance, map_preset) as apply_selection:
        with account.profile_update(instance, spec):
            selection_transaction = apply_selection()
            with production.preset_update(instance, discovery_spec=spec, active_transaction=selection_transaction) as check:
                with production.preset_manifest_update(instance, check):
                    if not check.selected_removed:
                        validated = production.validate_preset_selection(instance)
                        production.preflight(instance, spec)
                        production.validate_cycle_candidate(instance, spec, validated)
                        kwargs = {"validated": validated, "writer": config_writer}
                        production.merge_settings(instance / "data/config/supervisor.json", instance / "data/config/amp-settings.json", **kwargs)
                        bootstrap.validate_startup_config(instance / "data/config/supervisor.json")
                        if selection_transaction is not None:
                            selection_transaction.check()
            if check.selected_removed:
                raise production.SettingsError("PRESET_ACTIVE_MAP_MISSING")
    production.reconfigure_cycle_stopped(instance, spec, validated)


def start_container(instance, spec, marker, *, foreground=False, restart_policy=None, map_preset=None):
    meta = owned_container(instance, spec, marker)
    policy = restart_policy if restart_policy is not None else (
        meta["HostConfig"]["RestartPolicy"]["Name"] if meta else "no")
    command = run_command(instance, spec, marker, foreground=foreground, restart_policy=policy)
    if meta is not None:
        if meta["State"].get("Running") or meta["State"].get("Restarting") or meta["State"].get("Paused"):
            raise StandaloneError("Container is already active; use status or restart.")
        remove_stopped(instance, spec, marker, meta)
    prepare_start(instance, spec, map_preset=map_preset)
    if foreground:
        return command
    identifier = docker(command[len(production.DOCKER):], timeout=60).decode().strip()
    started = owned_container(instance, spec, marker, identifier)
    if not started["State"].get("Running"):
        raise StandaloneError("Container exited during startup; inspect logs.")
    print("Started " + started["Id"][:12] + "; use status for health and logs for startup progress.")
    return None


def setup(args):
    for field, label in (("star_dir", "Original Star directory"), ("game_dir", "Original Warcraft directory"),
                         ("map", "Chosen map file")):
        if getattr(args, field) is None:
            if args.noninteractive:
                raise StandaloneError("Setup requires --star-dir, --game-dir and --map in noninteractive mode.")
            answer = input(label + ": ").strip()
            if not answer:
                raise StandaloneError("Asset path is required.")
            setattr(args, field, Path(answer).expanduser())
    parent_created = False
    instance = None
    try:
        if args.instance_dir == DEFAULT_INSTANCE and not args.instance_dir.parent.exists():
            bootstrap.new_destination(args.instance_dir.parent).mkdir(mode=0o700)
            parent_created = True
        instance = bootstrap.setup(args.instance_dir, args.star_dir, args.game_dir, args.map,
                                   redist=args.redist, image=args.image, gate_path=args.gate,
                                   release_identity=args.release_identity)
        production.private_json(instance / MARKER, {"version": 1, "owner": secrets.token_hex(16),
                                                   "instance_dir": str(instance)})
    except BaseException:
        if instance is not None:
            shutil.rmtree(instance)
        if parent_created:
            args.instance_dir.parent.rmdir()
        raise
    print("Private standalone instance prepared. Run 'account', then 'start'.")


def parser():
    result = CommandParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True)
    for action in ("setup", "account", "start", "stop", "restart", "status", "logs"):
        sub = commands.add_parser(action)
        sub.add_argument("--instance-dir", type=Path, default=DEFAULT_INSTANCE,
                         help="private data directory; default: ~/.war3-dedicated/instance")
        if action == "setup":
            trust = sub.add_mutually_exclusive_group(required=True)
            trust.add_argument("--gate", type=Path, help="main-approved exact-local-image gate")
            trust.add_argument("--release-identity", type=Path, help="trusted maintainer release identity")
            sub.add_argument("--image", "--image-id", dest="image", help="already-local image ID/reference")
            for field in ("star-dir", "game-dir", "map", "redist"):
                sub.add_argument("--" + field, type=Path)
            sub.add_argument("--noninteractive", action="store_true")
        if action in ("start", "restart"):
            sub.add_argument("--foreground", action="store_true", help="attach STDIO until shutdown")
            sub.add_argument("--preset", "--map-preset", dest="map_preset",
                             help="select one preset; refuses to replace an existing multi-entry cycle")
            sub.add_argument("--restart-policy", choices=("no", "unless-stopped"), default=None,
                             help="Docker restart policy; initial default no, existing policy preserved")
        if action == "stop":
            sub.add_argument("--remove", action="store_true", help="also remove the owned stopped container, retaining bind data")
        if action == "logs":
            sub.add_argument("--tail", type=int, default=100)
            sub.add_argument("--follow", action="store_true")
    return result


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.action in ("start", "restart") and args.foreground and args.restart_policy == "unless-stopped":
        cli.error("--foreground requires --restart-policy no")
    if args.action in ("start", "restart") and args.map_preset is not None and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", args.map_preset):
        cli.error("invalid preset ID")
    if args.action == "logs" and not 1 <= args.tail <= 10000:
        cli.error("--tail must be between 1 and 10000")
    command = None
    try:
        if args.action == "setup":
            setup(args)
            return 0
        with operation(args.instance_dir, exclusive=args.action not in ("status", "logs")) as (instance, spec, marker):
            if args.action == "account":
                meta = owned_container(instance, spec, marker)
                if meta and (meta["State"].get("Running") or meta["State"].get("Restarting") or meta["State"].get("Paused")):
                    raise StandaloneError("Stop the instance before changing its account input.")
                result = account.cli(["--instance-dir", str(instance)])
                if result == 0:
                    print("Use standalone 'start' or 'restart' to apply the account; no AMP service is involved.")
                return result
            if args.action == "status":
                meta = owned_container(instance, spec, marker)
                print(json.dumps({"state": meta["State"]["Status"] if meta else "stopped",
                                  "health": meta["State"].get("Health", {}).get("Status", "unavailable") if meta else "unavailable",
                                  "image_id": spec["image_id"], "container_id": meta["Id"][:12] if meta else None,
                                  "restart_policy": meta["HostConfig"]["RestartPolicy"]["Name"] if meta else "no"}))
            elif args.action == "logs":
                meta = owned_container(instance, spec, marker)
                if meta is None:
                    raise StandaloneError("No retained container logs; the next start creates a new container.")
                options = ["logs", "--tail", str(args.tail)]
                if args.follow:
                    options.append("--follow")
                return docker([*options, meta["Id"]], timeout=None if args.follow else 30, stream=True)
            elif args.action == "stop":
                stop_container(instance, spec, marker, remove=args.remove)
                print("Stopped; private bind data retained.")
            else:
                policy = args.restart_policy
                if args.action == "restart":
                    meta = owned_container(instance, spec, marker)
                    if policy is None and meta:
                        policy = meta["HostConfig"]["RestartPolicy"]["Name"]
                    # Validate foreground/policy before stopping a running service.
                    run_command(instance, spec, marker, foreground=args.foreground, restart_policy=policy or "no")
                    stop_container(instance, spec, marker)
                command = start_container(instance, spec, marker, foreground=args.foreground, restart_policy=policy, map_preset=args.map_preset)
        if command is not None:
            os.execvpe(command[0], command, production.environment())
    except StandaloneError as error:
        print(str(error), file=sys.stderr)
        return 2
    except production.SettingsError as error:
        print(error.public_message, file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, subprocess.SubprocessError) as error:
        # Only exact fixed bootstrap messages may select a more specific hint.
        # Never echo the exception, operator paths, or supplied values.
        if type(error) is ValueError and error.args in (
                ("destination parent must be owned and not group/world writable",),
                ("unsafe destination ancestor",)):
            print("Choose a private data path under your home with --instance-dir. "
                  "Its parent must be owned by you and not group/world writable.", file=sys.stderr)
        else:
            print("Command rejected. Check the trusted gate, private owner/files, stopped state and local Docker access.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
