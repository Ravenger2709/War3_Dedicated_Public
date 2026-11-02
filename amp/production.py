"""Validated AMP settings and local-only staging container control (stdlib)."""
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import uuid

# Source checkout: amp.production/amp.account. Installed control: flat siblings.
account = import_module(".account", __package__) if __package__ else import_module("account")
map_discovery = import_module(".map_discovery", __package__) if __package__ else import_module("map_discovery")

DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
CHECKS = {"clean_build", "unit", "integration", "native_sanitizers", "star_relay_mock",
          "offline_start_stop", "component_recovery", "persistence", "no_zombies",
          "no_engine_execution", "secret_layer_audit", "offline_soak"}
DEFAULTS = {
    "host_name": "War3HostBot", "game_name": "Warcraft III Host", "game_port": 6113,
    "max_players": 12, "autostart_players": 10, "countdown_seconds": 5,
    "vote_enabled": True, "vote_required_percent": 60, "vote_min_players": 2,
    "vote_timeout": 60, "vote_cooldown": 30, "lobby_refresh_enabled": True,
    "lobby_refresh_interval": 10, "lobby_refresh_mode": "slot_toggle",
    "autohost_enabled": True, "reconnect_enabled": True, "map_preset": "manual",
    "autostart_enabled": True,
}
RANGES = {"game_port": (6113, 6113), "max_players": (2, 12), "autostart_players": (2, 12),
          "countdown_seconds": (0, 60), "vote_required_percent": (1, 100),
          "vote_min_players": (2, 12), "vote_timeout": (5, 600), "vote_cooldown": (0, 600),
          "lobby_refresh_interval": (10, 3600), "max_concurrent_matches": (1, 5)}
RO = ("config", "maps", "warcraft")
RW = ("state", "sqlite", "logs", "replays", "prefix", "session")
VENDOR = ("star", "profile", "redist", "game")
PRESETS = Path("data/config/presets.json")
PRESET_EDITOR = Path("control/preset-editor.json")
PRESET_BASELINE = Path("control/preset-editor-baseline.json")
PRESET_MANIFEST = Path("control/preset-configmanifest.json")
PRESET_RECOVERY = Path("control/preset-conflict")


class SettingsError(ValueError):
    @property
    def public_message(self):
        code = self.args[0] if self.args and type(self.args[0]) is str else ""
        if code == "PRESET_HISTORY_CONFLICT":
            path = self.args[1] if len(self.args) > 1 and type(self.args[1]) is str else "control"
            if not re.fullmatch(r"control(?:/preset-(?:deletion|metadata)-history(?:/war3-preset-[A-Za-z0-9_-]+/displaced\.json)?)?", path):
                path = "control"
            return ("A retained preset or selector may contain a later edit. Close editors, inspect and merge "
                    f"the retained file, then remove its reviewed history folder: {path}")
        return {
            "SUPERVISOR_EDIT_CONFLICT": "Supervisor config changed during startup. Start was cancelled; inspect control/preset-conflict and retained history before retrying.",
            "MAP_CYCLE_MIGRATION_AMBIGUOUS": "Legacy preset and map cycle differ. Reconcile the private settings explicitly before migration; no changes were made.",
            "MAP_CYCLE_OVERRIDE_CONFLICT": "--preset would replace an existing multi-entry Map Cycle. Edit map_cycle deliberately while stopped, or omit --preset to retain the full cycle.",
            "MAP_CYCLE_PREFLIGHT_FAILED": "Full Map Cycle validation failed in the accepted image. Check every map, preset, AI approval and image compatibility; runtime was not started.",
            "MAP_CYCLE_AUTOHOST_REQUIRED": "Enable Autohost to use Map Cycle. Manual hosting is not supported by the cycle runtime; settings were not applied.",
            "PRESET_ID_INVALID": "Settings rejected: invalid preset ID; choose a valid lowercase preset identifier.",
            "PRESET_JSON_INVALID": "Settings rejected: invalid preset JSON; check version, unique IDs, enabled flags and any explicit manual entry.",
            "PRESET_NONE_ENABLED": "Settings rejected: enable at least one preset in the JSON document.",
            "PRESET_SELECTION_REQUIRED": "Select an enabled preset from Preset definitions JSON in Map Cycle before Start.",
            "PRESET_ACTIVE_MAP_MISSING": "The selected map is missing. Restore it or select an available preset, then start again.",
            "PRESET_SELECTION_CHANGED": "The selected preset changed during map discovery. Retry start.",
            "PRESET_EDIT_CONFLICT": "Settings rejected: preset editor and File Manager both changed; clear the editor and apply to adopt the file first.",
            "PRESET_EXTERNAL_CONFLICT": "Preset file changed during apply. Keep stopped and inspect control/preset-conflict and any control/war3-preset-* recovery directory before retrying; external edits were preserved.",
            "RUNTIME_STILL_RUNNING": "A War3 container already exists. Stop the instance before applying presets or starting it; if AMP shows stopped, inspect the existing container before removing it.",
        }.get(code, "Settings rejected; verify preset input and private files.")


def private_json(path, value):
    """Replace a private JSON file atomically, refusing symlink destinations."""
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("symlinked private config rejected")
    fd, temporary = tempfile.mkstemp(prefix=".war3-json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("missing, oversized or symlinked JSON")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _preset_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate preset JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("non-finite preset JSON value")
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise SettingsError("PRESET_JSON_INVALID") from None


def format_preset_json(value):
    """Keep short settings on one line while leaving each preset easy to edit."""
    def render(item, depth, force=False):
        inline = json.dumps(item, ensure_ascii=True, separators=(", ", ": "))
        if not force and len(inline) + depth * 2 <= 110:
            return inline
        if isinstance(item, dict) and item:
            parts = [json.dumps(k) + ": " + render(v, depth + 1, k in ("presets", "capabilities"))
                     for k, v in item.items()]
            return "{\n" + ",\n".join("  " * (depth + 1) + p for p in parts) + "\n" + "  " * depth + "}"
        if isinstance(item, list) and item:
            parts = [render(v, depth + 1, isinstance(v, dict)) for v in item]
            return "[\n" + ",\n".join("  " * (depth + 1) + p for p in parts) + "\n" + "  " * depth + "]"
        return inline
    return render(value, 0, True) + "\n"


def _preset_document(value):
    # Envelope/discovery validation only. Runtime owns map/AI/domain validation.
    if (not isinstance(value, dict) or set(value) - {"version", "presets", "capabilities"}
            or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("presets"), list) or not 1 <= len(value["presets"]) <= 32):
        raise SettingsError("PRESET_JSON_INVALID")
    ids = set()
    for entry in value["presets"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", entry["id"])
                or entry["id"] in ids or type(entry.get("enabled")) is not bool):
            raise SettingsError("PRESET_JSON_INVALID")
        ids.add(entry["id"])
        if entry["id"] == "manual" and (not entry["enabled"] or set(entry) != {"id", "enabled"}):
            raise SettingsError("PRESET_JSON_INVALID")
    if not any(entry["enabled"] for entry in value["presets"]):
        raise SettingsError("PRESET_NONE_ENABLED")
    if len((json.dumps(value, indent=2, sort_keys=True) + "\n").encode()) > 65536:
        raise SettingsError("PRESET_JSON_INVALID")
    return value


def _preset_snapshot(path, private=False, limit=65536):
    path = account.real_path(path)
    if not path.exists():
        return None, None, None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        mode = stat.S_IMODE(before.st_mode)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or mode & 0o022 or (private and mode != 0o600)
                or not 0 < before.st_size <= limit):
            raise ValueError("unsafe preset input")
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("preset input changed during read")
        return data, mode, (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    finally:
        os.close(fd)


def _preset_file(path, private=False, limit=65536):
    return _preset_snapshot(path, private=private, limit=limit)[:2]


def _preset_inputs(instance):
    account.directory(instance)
    account.directory(instance / "control")
    account.directory(instance / "data/config")
    data, _ = _preset_file(instance / PRESET_EDITOR, private=True, limit=524288)
    if data is None:  # Legacy installations have no editor input.
        return ""
    value = _preset_json(data)
    if (not isinstance(value, dict) or set(value) != {"version", "preset_definitions"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["preset_definitions"], str)
            or len(value["preset_definitions"].encode()) > 65536):
        raise ValueError("preset editor schema rejected")
    return value["preset_definitions"].strip()


def preset_options(instance):
    """Read-only enabled IDs from the actual validated presets file, never stale UI text."""
    instance = Path(instance).absolute()
    _preset_inputs(instance)
    data, _ = _preset_file(instance / PRESETS)
    document = _preset_document(_preset_json(data)) if data else None
    def label(entry):
        name = entry.get("lobby_name")
        if (isinstance(name, str) and name == name.strip() and name
                and all(c.isprintable() and c not in "<>" for c in name)
                and len(name.encode("utf-8")) <= 24):
            return name + " (" + entry["id"] + ")"
        return entry["id"]
    if document is None:
        return {"manual": "Manual (operator map configuration)"}
    return {entry["id"]: ("Manual (operator map configuration)" if entry["id"] == "manual" else label(entry))
            for entry in document["presets"] if entry["enabled"]}


def validate_preset_selection(instance):
    """Read-only start validation; explicit preset apply leaves selection unchanged."""
    instance = Path(instance).absolute()
    validated = settings(read_json(instance / "data/config/amp-settings.json"))
    if not validated["autohost_enabled"]:
        raise SettingsError("MAP_CYCLE_AUTOHOST_REQUIRED")
    options = preset_options(instance)
    if any(preset not in options for preset in validated["map_cycle"]):
        raise SettingsError("PRESET_SELECTION_REQUIRED")
    return validated


@contextmanager
def preset_manifest_update(instance, check_revision=None):
    """Refresh the known template filename and any existing compatibility alias."""
    # The application root is below the Generic instance datastore directory.
    # Do not read GenericModule.kvp or accept an arbitrary manifest path.
    live = [instance.parent / name for name in ("war3-productionconfig.json", "configmanifest.json")]
    targets = [path for path in live if path.exists() or path.is_symlink()] or [instance / PRESET_MANIFEST]
    prepared, transactions = [], []
    if check_revision:
        check_revision()
    options = preset_options(instance)
    for target in targets:
        snapshot = _preset_snapshot(target, limit=2 * 1024 * 1024)
        if snapshot[0] is None:
            continue  # Older source-only skeleton without either live or staged manifest.
        manifest = _preset_json(snapshot[0])
        if not isinstance(manifest, list) or any(not isinstance(f, dict) for f in manifest):
            raise ValueError("preset selector manifest rejected")
        fields = [f for f in manifest if f.get("ParamFieldName") == "map_preset"]
        if not fields:
            cycle = [f for f in manifest if f.get("ParamFieldName") == "map_cycle"]
            if len(cycle) == 1 and cycle[0].get("Category") == "War3_Dedicated:sports_esports" and cycle[0].get("InputType") == "Textarea":
                continue
        if len(fields) != 1 or fields[0].get("Category") != "War3_Dedicated:sports_esports":
            raise ValueError("preset selector must belong to this War3_Dedicated manifest")
        if fields[0].get("InputType") == "enum" and fields[0].get("EnumValues") == options:
            continue
        fields[0]["InputType"] = "enum"
        fields[0]["EnumValues"] = options
        prepared.append((target, snapshot, manifest))
    try:
        for target, snapshot, manifest in prepared:
            if check_revision:
                check_revision()
            transaction = _PresetTransaction(instance, snapshot, path=target, limit=2 * 1024 * 1024,
                                             history_name="preset-metadata-history")
            transaction.preserve_original = True
            transactions.append(transaction)
            transaction.install(manifest)
            if check_revision:
                check_revision()
            transaction.check()
        yield
        if check_revision:
            check_revision()
        for transaction in transactions:
            transaction.check()
    except BaseException:
        for transaction in reversed(transactions):
            if not transaction.conflicted:
                try:
                    transaction.rollback()
                except (OSError, ValueError):
                    pass  # conflict() retains both revisions and blocks the next run.
        raise
    finally:
        for transaction in transactions:
            transaction.close()


def apply_presets(instance, spec):
    """Explicit stopped-only apply/selector refresh, without starting any bot process."""
    account.directory(instance)
    account.directory(instance / "control")
    account.generator_owner(spec)
    image_preflight(instance, spec)
    with ExitStack() as locks:
        locks.enter_context(account.locked(instance / "control/account-setup.lock", create=True))
        profile = instance / "vendor/profile/launcher.ini"
        if profile.exists() or profile.is_symlink():
            locks.enter_context(account.locked(profile))
        stopped(spec)
        with preset_update(instance) as check_revision, preset_manifest_update(instance, check_revision):
            pass


def migrate_cycle_settings(instance):
    """Prepare a stopped legacy instance before installing the cycle-aware AMP UI."""
    instance = Path(instance).absolute()
    check_retained_history(instance)
    if (instance / PRESET_RECOVERY).exists() or any((instance / "control").glob("war3-preset-*")):
        raise SettingsError("PRESET_EXTERNAL_CONFLICT")
    path = instance / "data/config/amp-settings.json"
    snapshot = _preset_snapshot(path, private=True, limit=2 * 1024 * 1024)
    if snapshot[0] is None:
        raise ValueError("missing AMP settings")
    raw = _preset_json(snapshot[0])
    if not isinstance(raw, dict):
        raise ValueError("settings object required")
    normalized = settings(raw)
    if "map_cycle" in raw:
        if "map_preset" in raw and raw["map_preset"] != normalized["map_cycle"][0]:
            raise SettingsError("MAP_CYCLE_MIGRATION_AMBIGUOUS")
    options = preset_options(instance)
    if any(preset not in options for preset in normalized["map_cycle"]):
        raise SettingsError("PRESET_SELECTION_REQUIRED")
    candidate = {**raw, "map_cycle": list(normalized["map_cycle"]),
                 "max_concurrent_matches": normalized["max_concurrent_matches"]}
    candidate.pop("map_preset", None)
    if candidate == raw:
        return False
    settings(candidate)
    transaction = _PresetTransaction(instance, snapshot, path=path, limit=2 * 1024 * 1024,
                                     history_name="preset-metadata-history")
    transaction.preserve_original = True
    try:
        transaction.install(candidate)
        transaction.check()
    except BaseException:
        if not transaction.conflicted:
            try:
                transaction.rollback()
            except (OSError, ValueError):
                transaction.conflict()
        raise
    finally:
        transaction.close()
    return True


def migrate_cycle_stopped(instance, spec):
    account.directory(instance)
    account.directory(instance / "control")
    account.generator_owner(spec)
    image_preflight(instance, spec)
    with ExitStack() as locks:
        locks.enter_context(account.locked(instance / "control/account-setup.lock", create=True))
        profile = instance / "vendor/profile/launcher.ini"
        if profile.exists() or profile.is_symlink():
            locks.enter_context(account.locked(profile))
        stopped(spec)
        return migrate_cycle_settings(instance)


def initialize_preset_editor(instance):
    """Bootstrap may call this after installing File Manager presets, before UI edits."""
    instance = Path(instance)
    if not (instance / PRESET_EDITOR).exists():
        private_json(instance / PRESET_EDITOR, {"version": 1, "preset_definitions": ""})
    if _preset_inputs(instance):
        raise ValueError("clear preset editor before adopting the File Manager baseline")
    data, _ = _preset_file(instance / PRESETS)
    current = _preset_document(_preset_json(data)) if data else None
    private_json(instance / PRESET_BASELINE, {"version": 1, "editor": None, "file": current})


def _preset_restore(path, data, mode):
    if data is None:
        path.unlink(missing_ok=True)
        return
    fd, temporary = tempfile.mkstemp(prefix=".war3-preset-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class _PresetTransaction:
    """Optimistic revision checks plus retention of any unexpected displaced save.

    File Manager does not share our flock. Exchange preserves the displaced inode;
    it is not a compare-and-swap and cannot prevent all concurrent replacement.
    """
    def __init__(self, instance, original, path=None, limit=65536, history_name="preset-deletion-history"):
        self.instance, self.path, self.original = instance, path or instance / PRESETS, original
        self.limit, self.history_name = limit, history_name
        self.expected = original
        self.work = Path(tempfile.mkdtemp(prefix="war3-preset-", dir=instance / "control"))
        self.displaced = self.work / "displaced.json"
        self.attempted = None
        self.installed = self.conflicted = self.keep = self.preserve_original = self.retain_rollback = False

    @staticmethod
    def moved_matches(actual, expected):
        # rename/link may change ctime; inode, data, mode and mtime must agree.
        return (actual[:2] == expected[:2] and actual[2] is not None and expected[2] is not None
                and actual[2][:4] == expected[2][:4])

    def conflict(self, external=None):
        if not self.conflicted:
            self.conflicted = self.keep = True
            recovery = self.instance / PRESET_RECOVERY
            try:
                recovery.mkdir(mode=0o700)
                copies = {"previous.json": self.original[0], "attempted.json": self.attempted,
                          "external.json": external[0] if external else None}
                for name, data in copies.items():
                    if data is not None:
                        _preset_restore(recovery / name, data, 0o600)
                if self.displaced.exists() or self.displaced.is_symlink():
                    # Preserve the inode too: a File Manager writer may still
                    # have it open and finish its save after our exchange.
                    os.replace(self.displaced, recovery / "displaced.json")
                    if not (recovery / "displaced.json").is_symlink():
                        (recovery / "displaced.json").chmod(0o600)
                self.keep = False
            except (OSError, ValueError):
                # Keep the displaced inode in private staging if copies cannot
                # be completed. Never let finally delete an external save.
                pass
        raise SettingsError("PRESET_EXTERNAL_CONFLICT") from None

    def check(self):
        try:
            current = _preset_snapshot(self.path, limit=self.limit)
        except (OSError, ValueError):
            self.conflict()
        if current != self.expected:
            self.conflict(current)

    def install(self, document):
        self.attempted = format_preset_json(document).encode()
        _preset_restore(self.displaced, self.attempted, 0o600)
        candidate = _preset_snapshot(self.displaced, limit=self.limit)
        self.check()  # Compare again immediately before mutating the file.
        with account.atomic_signals():
            if self.original[0] is None:
                try:
                    os.link(self.displaced, self.path)  # Atomic create, never replace a new FM save.
                except FileExistsError:
                    self.conflict(_preset_snapshot(self.path, limit=self.limit))
                self.displaced.unlink()
            else:
                account.exchange(self.displaced, self.path)
            self.installed = True
        current = _preset_snapshot(self.path, limit=self.limit)
        if self.original[0] is not None:
            displaced = _preset_snapshot(self.displaced, limit=self.limit)
            if not self.moved_matches(displaced, self.original):
                self.conflict(current)
        if not self.moved_matches(current, candidate):
            self.conflict(current)
        self.expected = current

    def rollback(self):
        self.check()  # Never restore over a known external File Manager save.
        if not self.installed:
            return
        with account.atomic_signals():
            if self.original[0] is None:
                os.replace(self.path, self.displaced)
            else:
                account.exchange(self.displaced, self.path)
        displaced = _preset_snapshot(self.displaced, limit=self.limit)
        if not self.moved_matches(displaced, self.expected):
            self.conflict(_preset_snapshot(self.path, limit=self.limit))
        self.installed = False
        self.retain_rollback = True

    def close(self):
        if self.preserve_original and (self.installed or self.retain_rollback) and not self.keep and self.displaced.exists():
            # A File Manager writer may still hold the exchanged old inode open.
            # Keep it reachable even if that writer finishes after our commit.
            self.keep = True
            baseline = self.original[0] if self.installed else self.expected[0]
            if baseline is None:
                raise ValueError("missing retained preset baseline")
            metadata = {"version": 1, "size": len(baseline), "sha256": hashlib.sha256(baseline).hexdigest()}
            _preset_restore(self.work / "retained-baseline.json", json.dumps(metadata, sort_keys=True).encode(), 0o600)
            history = self.instance / "control" / self.history_name
            history.mkdir(mode=0o700, exist_ok=True)
            account.directory(history)
            os.rename(self.work, history / self.work.name)
            return
        if not self.keep:
            shutil.rmtree(self.work)


def check_retained_history(instance):
    """Fail closed if a retained exchanged inode received a later write."""
    for name in ("preset-deletion-history", "preset-metadata-history"):
        history = instance / "control" / name
        relative = f"control/{name}"
        try:
            try:
                history.lstat()
            except FileNotFoundError:
                continue
            account.directory(history)
            with os.scandir(history) as entries:
                names = sorted(item.name for item in entries if item.name.startswith('war3-preset-'))
        except (OSError, ValueError, account.AccountError):
            raise SettingsError("PRESET_HISTORY_CONFLICT", relative) from None
        for child in names:
            entry = history / child
            recover = f"{relative}/{child}/displaced.json"
            try:
                account.directory(entry)
                baseline_data = _preset_snapshot(entry / 'retained-baseline.json', private=True, limit=8192)[0]
                if baseline_data is None:
                    raise ValueError('missing baseline')
                baseline = _preset_json(baseline_data)
                if (not isinstance(baseline, dict) or set(baseline) != {'version', 'size', 'sha256'}
                        or type(baseline['version']) is not int or baseline['version'] != 1
                        or type(baseline['size']) is not int or not 0 < baseline['size'] <= 2 * 1024 * 1024
                        or not isinstance(baseline['sha256'], str)
                        or not re.fullmatch(r'[0-9a-f]{64}', baseline['sha256'])):
                    raise ValueError('invalid baseline')
                data = _preset_snapshot(entry / 'displaced.json', limit=2 * 1024 * 1024)[0]
                if data is None or len(data) != baseline['size'] or hashlib.sha256(data).hexdigest() != baseline['sha256']:
                    raise ValueError('retained file changed')
            except (OSError, ValueError, account.AccountError):
                raise SettingsError("PRESET_HISTORY_CONFLICT", recover) from None


@contextmanager
def preset_update(instance, discovery_spec=None, active_transaction=None):
    """Called under profile_update's accepted-image/stopped checks and setup flock."""
    text = _preset_inputs(instance)
    check_retained_history(instance)
    if ((instance / PRESET_RECOVERY).exists() or (instance / PRESET_RECOVERY).is_symlink()
            or any(path != getattr(active_transaction, "work", None)
                   for path in (instance / "control").glob("war3-preset-*"))):
        raise SettingsError("PRESET_EXTERNAL_CONFLICT")
    if not (instance / PRESET_EDITOR).exists() and discovery_spec is None:
        yield None
        return
    snapshot = _preset_snapshot(instance / PRESETS)
    original = snapshot[0]
    current = _preset_document(_preset_json(original)) if original else None
    state_bytes, state_mode = _preset_file(instance / PRESET_BASELINE, private=True, limit=524288)
    state = _preset_json(state_bytes) if state_bytes else {"version": 1, "editor": None, "file": None}
    if (not isinstance(state, dict) or set(state) != {"version", "editor", "file"}
            or type(state["version"]) is not int or state["version"] != 1):
        raise ValueError("preset editor baseline rejected")
    for key in ("editor", "file"):
        if state[key] is not None:
            _preset_document(state[key])
    editor_document = _preset_document(_preset_json(text)) if text else None
    desired = editor_document
    # The AMP editor may omit the technical AI approvals. Preserve the records
    # from the current file; runtime still verifies each map and script hash.
    if (desired is not None and "capabilities" not in desired and current is not None
            and "capabilities" in current):
        desired = {**desired, "capabilities": current["capabilities"]}
    # An unchanged UI editor must never erase later File Manager edits.
    unchanged = editor_document is not None and editor_document == state["editor"]
    if not unchanged and desired is not None and current != state["file"] and desired != current:
        raise SettingsError("PRESET_EDIT_CONFLICT")
    changed = not unchanged and desired is not None and desired != current
    transaction = _PresetTransaction(instance, snapshot)
    map_revisions = {}
    selection_revision = None
    state_written = False
    def check():
        transaction.check()
        map_discovery.verify_revisions(map_revisions)
        if selection_revision is not None and _preset_snapshot(instance / "data/config/amp-settings.json") != selection_revision:
            raise SettingsError("PRESET_SELECTION_CHANGED")
    check.selected_removed = False
    try:
        if discovery_spec is not None:
            account.directory(instance / "data")
            account.directory(instance / "data/maps")
            base = desired if changed else current
            if base is None:
                base = {"version": 1, "presets": [{"id": "manual", "enabled": True}]}
            before_settings = _preset_snapshot(instance / "data/config/amp-settings.json")
            generated, skipped = map_discovery.discover(instance, base, discovery_spec, DOCKER, environment(),
                                                       account.generator_owner(discovery_spec), revisions=map_revisions)
            check()
            # Missing maps block selected presets without deleting operator data.
            selected = settings(_preset_json(before_settings[0]))['map_cycle'] if before_settings[0] else ['manual']
            maps_path = instance / 'data/maps'
            if maps_path not in map_revisions:
                map_revisions[maps_path] = ('directory', map_discovery.directory_snapshot(maps_path))
            names = set(map_revisions[maps_path][1][1])
            missing = {entry['id'] for entry in generated['presets']
                       if entry.get('id') != 'manual' and isinstance(entry.get('map_filename'), str)
                       and entry['map_filename'] not in names}
            check.selected_removed = any(preset in missing for preset in selected)
            if missing:
                selection_revision = before_settings
            _preset_document(generated)
            if generated != base:
                desired = generated
                changed = True
            if skipped:
                print(f"Map discovery: skipped {skipped} invalid or changing map(s).")
        if changed:
            # Every replacement can race a File Manager writer holding the old
            # inode, not only replacements caused by a deletion.
            transaction.preserve_original = True
            check()
            transaction.install(desired)
            check()
        yield check
        check()
        if not unchanged or changed:
            state_written = True
            private_json(instance / PRESET_BASELINE, {"version": 1, "editor": editor_document,
                                                   "file": desired if desired is not None else current})
            check()
    except BaseException:
        if not transaction.conflicted:
            try:
                transaction.rollback()
            except (OSError, ValueError):
                transaction.conflict()
        if state_written:
            _preset_restore(instance / PRESET_BASELINE, state_bytes, state_mode)
        raise
    finally:
        transaction.close()


def settings(raw):
    # Existing instances acquire the new optional selection without changing maps.
    if not isinstance(raw, dict):
        raise ValueError("settings object required")
    raw = dict(raw)
    explicit_cycle = "map_cycle" in raw
    cycle = raw.pop("map_cycle", None) if "map_cycle" in raw else [raw.get("map_preset", "manual")]
    limit = raw.pop("max_concurrent_matches", 3)
    raw = {"map_preset": "manual", "autostart_enabled": True, **raw}
    if set(raw) != set(DEFAULTS):
        raise ValueError("settings must contain exactly the documented fields")
    if isinstance(cycle, str):
        if len(cycle.encode("utf-8")) > 65536:
            raise ValueError("map cycle too large")
        try:
            cycle = json.loads(cycle, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON")))
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("map cycle JSON rejected") from None
    if (not isinstance(cycle, list) or not 1 <= len(cycle) <= 32
            or any(not isinstance(x, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", x) for x in cycle)):
        raise ValueError("map cycle rejected")
    # AMP retains unknown/old JSON keys: an explicit cycle is authoritative.
    # Preserve the stale key on disk; never let it block or override the UI.
    if explicit_cycle:
        raw["map_preset"] = cycle[0]
    if isinstance(limit, str) and re.fullmatch(r"[0-9]{1,5}", limit):
        limit = int(limit)
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("max concurrent matches rejected")
    result = {}
    for name, default in DEFAULTS.items():
        value = raw[name]
        if type(default) is bool:
            if value in ("True", "true", "1"):
                value = True
            elif value in ("False", "false", "0"):
                value = False
            if type(value) is not bool:
                raise ValueError("boolean setting rejected: " + name)
        elif name in RANGES:
            if isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value):
                value = int(value)
            low, high = RANGES[name]
            if type(value) is not int or not low <= value <= high:
                raise ValueError("integer setting rejected: " + name)
        elif name in ("host_name", "game_name"):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,23}", value):
                raise ValueError("name setting rejected: " + name)
        elif name == "map_preset":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
                raise SettingsError("PRESET_ID_INVALID")
        elif value != "slot_toggle":
            raise ValueError("refresh mode rejected")
        result[name] = value
    if (result["vote_min_players"] > result["max_players"]
            or (result["autostart_enabled"] and result["autostart_players"] > result["max_players"])):
        raise ValueError("vote minimum and enabled autostart threshold must not exceed maximum")
    result["map_cycle"] = list(cycle)
    result["max_concurrent_matches"] = limit
    result["map_preset"] = cycle[0]
    return result


class _SupervisorTransaction(_PresetTransaction):
    def conflict(self, external=None):
        try:
            super().conflict(external)
        except SettingsError:
            raise SettingsError("SUPERVISOR_EDIT_CONFLICT") from None


@contextmanager
def supervisor_update(instance):
    """Retain revisions and roll back a failed startup without erasing FM edits."""
    transaction = None
    def write(path, value, snapshot):
        nonlocal transaction
        if transaction is not None or snapshot[0] is None:
            raise ValueError("supervisor transaction rejected")
        transaction = _SupervisorTransaction(instance, snapshot, path=Path(path), limit=2 * 1024 * 1024,
                                             history_name="preset-metadata-history")
        transaction.preserve_original = True
        transaction.install(value)
    try:
        yield write
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


def merge_settings(config_path, settings_path, validated=None, writer=None):
    validated = settings(read_json(settings_path) if validated is None else validated)
    config_path = Path(config_path)
    snapshot = _preset_snapshot(config_path, private=True, limit=2 * 1024 * 1024)
    if snapshot[0] is None:
        raise ValueError("missing supervisor config")
    config = _preset_json(snapshot[0])
    if not isinstance(config, dict) or config.get("http") != {"host": "127.0.0.1", "port": 8080}:
        raise ValueError("production status must bind container loopback port 8080")
    config["gameplay"] = {k: v for k, v in validated.items() if k not in {"game_port", "map_preset"}}
    if writer is None:
        instance = config_path.parent.parent.parent if config_path.parent.name == "config" and config_path.parent.parent.name == "data" else config_path.parent
        with supervisor_update(instance) as guarded_write:
            guarded_write(config_path, config, snapshot)
    else:
        writer(config_path, config, snapshot)
    return validated


def gate(value, image_id):
    if (set(value) != {"schema_version", "image_id", "source_sha256", "checks", "approved_by_main"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["approved_by_main"] is not True or value["image_id"] != image_id
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)):
        raise ValueError("production image gate missing or mismatched")
    if not isinstance(value["checks"], dict) or set(value["checks"]) != CHECKS or any(
            state != "PASS" for state in value["checks"].values()):
        raise ValueError("integrated production checks incomplete")
    hashes = value["source_sha256"]
    if not isinstance(hashes, dict) or not hashes or any(
            not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
            for name, sha in hashes.items()):
        raise ValueError("production source hashes missing")
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def environment():
    result = os.environ.copy()
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        result.pop(name, None)
    return result


def docker_output(args):
    process = subprocess.run([*DOCKER, *args], env=environment(), check=True,
                             capture_output=True, timeout=30)
    return process.stdout


def docker_json(args):
    return json.loads(docker_output(args))


def deployment(instance):
    instance = Path(instance).absolute()
    if instance.is_symlink() or not instance.is_dir() or instance.stat().st_mode & 0o077:
        raise ValueError("instance must be a private real directory")
    value = read_json(instance / "deployment.json")
    required = {"schema_version", "image_id", "container_name", "uid", "gid"}
    if (set(value) not in (required, required | {"host_instance_dir"})
            or value["schema_version"] != 1 or value["container_name"] != "war3-phase3-staging"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"])
            or any(type(value[k]) is not int or value[k] <= 0 for k in ("uid", "gid"))):
        raise ValueError("staging deployment config rejected")
    host_path = value.get("host_instance_dir", str(instance))
    if (not isinstance(host_path, str) or not Path(host_path).is_absolute()
            or ".." in Path(host_path).parts or any(ord(c) < 32 or c == "," for c in host_path)):
        raise ValueError("absolute host instance directory required")
    gate(read_json(instance / "acceptance.json"), value["image_id"])
    return value


def command(instance, spec):
    """Only TCP 6113 is published; proprietary inputs are always separate RO binds."""
    instance = Path(instance).absolute()
    # AMP sees its instance under /AMP; the host daemon resolves bind sources
    # in the host namespace. This operator-owned path is recorded at prepare.
    host_instance = Path(spec.get("host_instance_dir", str(instance)))
    args = [*DOCKER, "run", "--rm", "--pull=never", "--name", spec["container_name"],
            "--label", "war3.managed=phase3-staging", "--init", "--interactive", "--sig-proxy=true",
            "--restart=no", "--read-only", "--user", f'{spec["uid"]}:{spec["gid"]}',
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "256",
            "--memory", "2g", "--memory-swap", "2g", "--cpus", "2", "--ulimit", "core=0",
            "--shm-size", "128m", "--stop-timeout", "120", "--network", "bridge",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--publish", "0.0.0.0:6113:6113/tcp",
            "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3"]
    for name in (*RO, *RW):
        source = host_instance / "data" / name
        args.extend(["--mount", f"type=bind,src={source},dst=/data/{name}" + (",readonly" if name in RO else "")])
    for name in VENDOR:
        args.extend(["--mount", f"type=bind,src={host_instance / 'vendor' / name},dst=/vendor/{name},readonly"])
    args.append(spec["image_id"])
    return args


def stopped(spec):
    # A same-named foreign container is always an error. Never stop/remove anything here.
    all_containers = docker_output(["ps", "-aq", "--filter", "name=^/" + spec["container_name"] + "$"])
    if all_containers:
        raise SettingsError("RUNTIME_STILL_RUNNING")


def image_preflight(instance, spec):
    """Check the accepted local image and stopped state without requiring a profile."""
    accepted = read_json(instance / "acceptance.json")
    digest = gate(accepted, spec["image_id"])
    meta = docker_json(["image", "inspect", spec["image_id"]])[0]
    labels = meta.get("Config", {}).get("Labels") or {}
    if (meta["Id"] != spec["image_id"] or meta["Architecture"] != "amd64" or meta["Os"] != "linux"
            or labels.get("war3.production.source-digest") != digest):
        raise ValueError("accepted local image identity/source/platform mismatch")
    stopped(spec)


def preflight(instance, spec, check_port=True):
    image_preflight(instance, spec)
    for source in [*(instance / "data" / n for n in (*RO, *RW)), *(instance / "vendor" / n for n in VENDOR)]:
        if not source.is_dir() or source.is_symlink() or "," in str(source):
            raise ValueError("mount source missing, symlinked or invalid")
    for name in RW:
        if (instance / "data" / name).stat().st_uid != spec["uid"]:
            raise ValueError("writable data owner must match effective runtime UID")
    required = ["star/Launcher.exe", "profile/launcher.ini", "profile/iccup.ini",
                "redist/vc_redist.x86.exe", "game/war3.exe", "game/game.dll"]
    for name in required:
        source = instance / "vendor" / name
        if not source.is_file() or source.is_symlink():
            raise ValueError("local operator vendor input missing")
    profile = instance / "vendor/profile"
    if profile.stat().st_uid != spec["uid"] or stat.S_IMODE(profile.stat().st_mode) != 0o700:
        raise ValueError("profile directory must be owned and mode 0700")
    for name in ("launcher.ini", "iccup.ini"):
        info = (profile / name).stat()
        if info.st_uid != spec["uid"] or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError("profile files must be owned single-link files with mode 0600")
    if check_port:
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", 6113))  # Local conflict check, never a WAN scan.


def validate_cycle_candidate(instance, spec, validated):
    """Check every multi-cycle entry offline before merging the selected snapshot.

    Single-entry deployments retain their existing image startup validation.
    No fallback to first-preset validation is allowed for multi-entry cycles,
    including when an older accepted image lacks the full-cycle validator.
    """
    game = settings(validated)
    if len(game["map_cycle"]) == 1:
        return
    game = {key: value for key, value in game.items() if key not in {"game_port", "map_preset"}}
    host = Path(spec.get("host_instance_dir", str(Path(instance).absolute())))
    if (not host.is_absolute() or ".." in host.parts
            or any(ord(c) < 32 or ord(c) == 127 or c == "," for c in str(host))):
        raise ValueError("invalid cycle validation bind path")
    name = "war3-cycle-validation-" + uuid.uuid4().hex
    args = [*DOCKER, "run", "--rm", "--pull=never", "--name", name,
            "--interactive", "--restart=no", "--network", "none", "--read-only",
            "--user", f'{spec["uid"]}:{spec["gid"]}', "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "32",
            "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
            "--ulimit", "core=0", "--log-driver", "none", "--no-healthcheck",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
            "--workdir", "/opt/war3", "--env", "PYTHONDONTWRITEBYTECODE=1"]
    for source in ("config", "maps"):
        args += ["--mount", f"type=bind,src={host / 'data' / source},dst=/data/{source},readonly"]
    script = ("import json,sys; from src.runtime.map_cycle import validate_full_cycle; "
              "game=json.load(sys.stdin); "
              "validate_full_cycle(game['map_cycle'], '/data/config/ghost.cfg', game)")
    args += ["--entrypoint", "python3", spec["image_id"], "-c", script]
    try:
        subprocess.run(args, input=json.dumps(game).encode(), env=environment(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise SettingsError("MAP_CYCLE_PREFLIGHT_FAILED") from None
    finally:
        # A timed-out Docker client does not imply its container has stopped.
        subprocess.run([*DOCKER, "rm", "--force", name], env=environment(),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, timeout=10)


def prepare(instance, image_id, acceptance, supervisor_config, native_config, map_config):
    accepted = read_json(acceptance)
    gate(accepted, image_id)
    base = read_json(supervisor_config)
    if base.get("http") != {"host": "127.0.0.1", "port": 8080}:
        raise ValueError("supply main's loopback-only integrated supervisor config")
    if os.getuid() == 0:
        raise ValueError("prepare as non-root data owner")
    instance = Path(instance)
    instance.mkdir(mode=0o700)  # Refuse existing instances, symlinks, other servers.
    try:
        for name in (*RO, *RW):
            (instance / "data" / name).mkdir(mode=0o700, parents=True)
        (instance / "data/config/maps").mkdir(mode=0o700)
        for name in VENDOR:
            (instance / "vendor" / name).mkdir(mode=0o700, parents=True)
        (instance / "control").mkdir(mode=0o700)
        for name in ("production.py", "production-launch.py", "account.py", "map_discovery.py"):
            shutil.copyfile(Path(__file__).parent / name, instance / "control" / name)
            (instance / "control" / name).chmod(0o600)
        shutil.copyfile(Path(__file__).parent / "war3-productionconfig.json", instance / PRESET_MANIFEST)
        (instance / PRESET_MANIFEST).chmod(0o600)
        private_json(instance / "acceptance.json", accepted)
        private_json(instance / "deployment.json", {"schema_version": 1, "image_id": image_id,
                     "container_name": "war3-phase3-staging", "uid": os.getuid(), "gid": os.getgid(),
                     "host_instance_dir": str(instance.absolute())})
        private_json(instance / "data/config/supervisor.json", base)
        private_json(instance / "data/config/amp-settings.json", DEFAULTS)
        private_json(instance / "control/account-input.json", {"version": 1, "username": "", "password": ""})
        initialize_preset_editor(instance)
        for source, target in ((native_config, "ghost.cfg"), (map_config, "maps/selected.cfg")):
            if Path(source).is_symlink() or not Path(source).is_file():
                raise ValueError("regular operator native/map configs required")
            shutil.copyfile(source, instance / "data/config" / target)
            (instance / "data/config" / target).chmod(0o600)
        for directory in (instance / "data", instance / "vendor"):
            directory.chmod(0o700)
        merge_settings(instance / "data/config/supervisor.json", instance / "data/config/amp-settings.json")
    except Exception:
        shutil.rmtree(instance)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "plan", "preflight", "run", "rollback", "list-presets", "apply-presets", "migrate-cycle"):
        sub = commands.add_parser(action)
        sub.add_argument("--instance-dir", required=True, type=Path)
        if action in ("prepare", "rollback"):
            sub.add_argument("--image-id", required=True)
            sub.add_argument("--gate", required=True, type=Path)
        if action == "prepare":
            for name in ("supervisor-config", "native-config", "map-config"):
                sub.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    instance = args.instance_dir.absolute()
    if args.action == "list-presets":
        print(json.dumps(preset_options(instance), sort_keys=True))
        return 0
    if args.action == "prepare":
        prepare(instance, args.image_id, args.gate, args.supervisor_config, args.native_config, args.map_config)
        print("Private staging skeleton prepared; supply local runtime assets separately. No Docker or AMP changes made.")
        return 0
    spec = deployment(instance)
    if args.action == "migrate-cycle":
        changed = migrate_cycle_stopped(instance, spec)
        print("Legacy cycle migrated." if changed else "Cycle already present; no settings changed.")
        return 0
    if args.action == "apply-presets":
        apply_presets(instance, spec)
        print("Preset JSON and selector applied; selection unchanged. Restart the AMP instance, reconnect to the panel, choose an enabled preset from your JSON, then Start. Bot start not performed.")
        return 0
    if args.action == "rollback":
        accepted = read_json(args.gate)
        gate(accepted, args.image_id)
        stopped(spec)
        previous = {"deployment": dict(spec), "gate": read_json(instance / "acceptance.json")}
        spec["image_id"] = args.image_id
        private_json(instance / "previous-deployment.json", previous)
        private_json(instance / "acceptance.json", accepted)
        private_json(instance / "deployment.json", spec)
        print("Stopped staging image selection changed; start and verify through AMP. Data was retained.")
        return 0
    if args.action == "plan":
        settings(read_json(instance / "data/config/amp-settings.json"))
        print(json.dumps({"command": command(instance, spec), "restart_owner": "AMP", "remote_operations": False}))
        return 0
    if args.action == "preflight":
        validated = validate_preset_selection(instance)
        preflight(instance, spec)
        validate_cycle_candidate(instance, spec, validated)
        print("Local staging preflight passed; AMP start not performed.")
        return 0
    # Offline setup precedes full preflight (including the first-ever profile).
    # A failed preflight/merge rolls the profile transaction back before returning.
    with supervisor_update(instance) as config_writer:
        with account.profile_update(instance, spec):
            with preset_update(instance, discovery_spec=spec) as check_revision:
                with preset_manifest_update(instance, check_revision):
                    if not check_revision.selected_removed:
                        validated = validate_preset_selection(instance)
                        preflight(instance, spec)
                        validate_cycle_candidate(instance, spec, validated)
                        merge_settings(instance / "data/config/supervisor.json", instance / "data/config/amp-settings.json",
                                       validated=validated, writer=config_writer)
            if check_revision.selected_removed:
                raise SettingsError("PRESET_ACTIVE_MAP_MISSING")
    # No shell, no detached container, no TTY. exec preserves stdin and AMP's SIGTERM chain.
    os.execvpe(DOCKER[0], command(instance, spec), environment())


def cli(argv=None):
    try:
        return main(argv)
    except (account.AccountError, SettingsError) as error:
        # Whitelisted fixed messages only; never stringify arbitrary exceptions.
        print(error.public_message)
        return 2
    except Exception:
        # Account/INI parsers and Docker errors can embed sensitive input.
        print("Staging operation rejected; verify account/preset input, gate, private files, stopped state, local Docker and TCP 6113.")
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
