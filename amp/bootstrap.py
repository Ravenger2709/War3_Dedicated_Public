"""Offline, new-destination-only operator setup; never starts a runtime or logs in."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import time
import uuid

from . import production

ROOT = Path(__file__).resolve().parents[1]
REDIST_SHA256 = "fdd1e1f0dcae2d0aa0720895eff33b927d13076e64464bb7c7e5843b7667cd14"
VENDOR_PATHS = frozenset((
    "Launcher.exe", "QtWebEngineProcess.exe", "iccwc3.icc", "libeay32.dll",
    "ssleay32.dll", "RepAnalyser.dll", "Storm.dll", "w3lh.dll",
    "chaticons.zip", "flags.zip", "mapicons.zip", "Qt5Core.dll", "Qt5Gui.dll",
    "Qt5Multimedia.dll", "Qt5Network.dll", "Qt5Positioning.dll", "Qt5Qml.dll",
    "Qt5Quick.dll", "Qt5Svg.dll", "Qt5WebChannel.dll", "Qt5WebEngineCore.dll",
    "Qt5WebEngineWidgets.dll", "Qt5Widgets.dll", "Qt5Xml.dll",
    "imageformats/qgif.dll", "imageformats/qsvg.dll", "platforms/qwindows.dll",
    "resources/icudtl.dat", "resources/qtwebengine_resources.pak",
    "resources/qtwebengine_resources_100p.pak", "resources/qtwebengine_resources_200p.pak"))
SHA = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
PULL_REF = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
ANALYSIS_TIMEOUT = 30
ANALYSIS_LIMIT = 65536

NATIVE_CONFIG = """# Credentials are supplied separately through AMP Account setup.
bot_tft = 1
bot_war3path = /data/warcraft/
bot_mappath = /data/maps/
bot_defaultmap = selected.cfg
bot_spoofchecks = 0
bot_requirespoofchecks = 0
bot_checkmultipleipusage = 0
bot_localadminmessages = 0
bot_allowdownloads = 1
bot_pingduringdownloads = 1
bot_lobbytimelimit = 10
bot_latency = 100
bot_synclimit = 50
lan_war3version = 26
udp_broadcasttarget = 127.0.0.1
udp_dontroute = 0
replay_war3version = 26
replay_buildnumber = 6401
admingame_create = 0
bnet_server =
"""
MAP_CONFIG = """map_path = Maps\\Download\\selected.w3x
map_localpath = selected.w3x
map_speed = 3
map_visibility = 4
map_observers = 1
map_flags = 3
map_loadInGame = 0
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def safe_path(path):
    path = Path(path)
    if ".." in path.parts or any(ord(c) < 32 or ord(c) == 127 or c == "," for c in str(path)):
        raise ValueError("unsafe path")
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlinked path rejected")
    return path


def open_source(path):
    """Open every ancestor with NOFOLLOW, including against a raced parent link."""
    path = safe_path(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
            os.close(fd)
            fd = following
        result = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=fd)
    finally:
        os.close(fd)
    stream = os.fdopen(result, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        stream.close()
        raise ValueError("regular single-link source required")
    return stream


def source_digest(path, target=None):
    """Stream bounded inputs; never load, run or alter proprietary source files."""
    with open_source(path) as stream:
        before = os.fstat(stream.fileno())
        if not 0 < before.st_size <= 4 * 1024**3:
            raise ValueError("source size rejected")
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > before.st_size:
                raise ValueError("source changed while reading")
            digest.update(chunk)
            if target is not None:
                target.write(chunk)
        after = os.fstat(stream.fileno())
        if (size != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("source changed while reading")
    return {"bytes": size, "sha256": digest.hexdigest()}


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def read_object(path):
    with open_source(path) as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("oversized JSON")
    value = json.loads(raw, object_pairs_hook=no_duplicates)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    canonical(value)
    return value, hashlib.sha256(raw).hexdigest()


def new_destination(path):
    if os.getuid() == 0 or os.geteuid() != os.getuid() or os.getgid() <= 0:
        raise ValueError("run as the non-root data owner")
    path = safe_path(path)
    if path.exists():
        raise ValueError("destination must be new")
    parent = path.parent.stat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o022):
        raise ValueError("destination parent must be owned and not group/world writable")
    # Ancestors writable by others could replace the owned parent during setup.
    for ancestor in path.parent.parents:
        info = ancestor.stat()
        if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise ValueError("unsafe destination ancestor")
    return path


def rename_new(source, destination):
    """Linux atomic no-replace publish: even an empty raced destination survives."""
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        raise OSError(ctypes.get_errno(), "new-destination publish rejected")


def image_identity(meta):
    if not isinstance(meta, dict) or not isinstance(meta.get("Id"), str) or not IMAGE_ID.fullmatch(meta["Id"]):
        raise ValueError("local Docker image ID rejected")
    platform = {"os": meta.get("Os"), "architecture": meta.get("Architecture"),
                "variant": meta.get("Variant", "")}
    config, rootfs = meta.get("Config"), meta.get("RootFS")
    if (platform != {"os": "linux", "architecture": "amd64", "variant": ""}
            or not isinstance(config, dict) or not isinstance(config.get("Labels"), dict)
            or not isinstance(rootfs, dict) or set(rootfs) != {"Type", "Layers"}
            or rootfs["Type"] != "layers" or not isinstance(rootfs["Layers"], list)
            or not rootfs["Layers"] or any(not isinstance(layer, str) or not IMAGE_ID.fullmatch(layer)
                                           for layer in rootfs["Layers"])):
        raise ValueError("image Config/RootFS/platform rejected")
    return {"image_config": config, "rootfs": rootfs, "platform": platform}


def inspect_image(reference):
    if (not isinstance(reference, str) or not reference or reference.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in reference)):
        raise ValueError("local image reference required")
    values = production.docker_json(["image", "inspect", reference])
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("one local image required")
    image_identity(values[0])
    return values[0]


def validate_identity(value):
    required = {"schema_version", "gate", "source_digest", "image_config", "rootfs", "platform"}
    if (not isinstance(value, dict) or set(value) not in (required, required | {"pull_ref"})
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["gate"], dict)):
        raise ValueError("unknown release identity")
    accepted = value["gate"]
    digest = production.gate(accepted, accepted.get("image_id", ""))
    if value["source_digest"] != digest:
        raise ValueError("release source digest mismatch")
    platform = value["platform"]
    if not isinstance(platform, dict) or set(platform) != {"os", "architecture", "variant"}:
        raise ValueError("release platform rejected")
    identity = image_identity({"Id": accepted["image_id"], "Config": value["image_config"],
                               "RootFS": value["rootfs"], "Os": platform["os"],
                               "Architecture": platform["architecture"], "Variant": platform["variant"]})
    if identity["image_config"]["Labels"].get("war3.production.source-digest") != digest:
        raise ValueError("release image source label mismatch")
    if "pull_ref" in value and (not isinstance(value["pull_ref"], str) or not PULL_REF.fullmatch(value["pull_ref"])):
        raise ValueError("immutable pull reference required")
    return digest


def export_release_identity(image_id, gate_path, output, pull_ref=None):
    """Maintainer-only export from an already approved gate and its EXACT local ID."""
    output = new_destination(output)
    accepted, _ = read_object(gate_path)
    digest = production.gate(accepted, image_id)
    meta = inspect_image(image_id)
    if meta["Id"] != image_id:
        raise ValueError("export requires the exact main-approved local image ID")
    value = {"schema_version": 1, "gate": accepted, "source_digest": digest, **image_identity(meta)}
    if pull_ref is not None:
        if pull_ref not in (meta.get("RepoDigests") or []):
            raise ValueError("pull reference not correlated with accepted local image")
        value["pull_ref"] = pull_ref
    validate_identity(value)
    with tempfile.TemporaryDirectory(prefix=".war3-release-", dir=output.parent) as directory:
        stage = Path(directory) / "identity.json"
        production.private_json(stage, value)
        rename_new(stage, output)
    return value


def local_acceptance(image, gate_path=None, release_identity=None):
    if (gate_path is None) == (release_identity is None):
        raise ValueError("choose exactly one trusted gate or release identity")
    if release_identity is not None:
        trusted, trust_hash = read_object(release_identity)
        digest = validate_identity(trusted)
        original = trusted["gate"]
        reference = image or trusted.get("pull_ref", original["image_id"])
    else:
        original, trust_hash = read_object(gate_path)
        digest = production.gate(original, original.get("image_id", ""))
        reference = image or original["image_id"]
    meta = inspect_image(reference)
    identity = image_identity(meta)
    if identity["image_config"]["Labels"].get("war3.production.source-digest") != digest:
        raise ValueError("local image source label mismatch")
    if release_identity is not None:
        expected = {key: trusted[key] for key in identity}
        if canonical(identity) != canonical(expected):
            raise ValueError("local image differs from trusted release Config/RootFS/platform")
    else:
        production.gate(original, meta["Id"])  # Exact-ID path never translates IDs.
    localized = {**original, "image_id": meta["Id"]}
    production.gate(localized, meta["Id"])
    receipt = {"schema_version": 1, "method": "release-identity" if release_identity else "exact-gate",
               "trusted_input_sha256": trust_hash, "original_gate": original,
               "local_image_id": meta["Id"], "source_digest": digest,
               "image_identity_sha256": hashlib.sha256(canonical(identity)).hexdigest()}
    return localized, receipt


def vendor_manifest(accepted):
    manifest, digest = read_object(ROOT / "config/star-vendor-files.json")
    if accepted["source_sha256"].get("config/star-vendor-files.json") != digest:
        raise ValueError("vendor manifest differs from approved release source")
    if (set(manifest) != {"version", "files"} or type(manifest["version"]) is not int
            or manifest["version"] != 1 or not isinstance(manifest["files"], list)
            or len(manifest["files"]) != 31):
        raise ValueError("vendor manifest rejected")
    rows = {}
    for row in manifest["files"]:
        if (not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}
                or not isinstance(row["path"], str) or row["path"] not in VENDOR_PATHS
                or row["path"] in rows or type(row["bytes"]) is not int
                or not 0 < row["bytes"] <= 128 * 1024**2 or not isinstance(row["sha256"], str)
                or not SHA.fullmatch(row["sha256"])):
            raise ValueError("vendor manifest row rejected")
        rows[row["path"]] = {k: row[k] for k in ("bytes", "sha256")}
    if set(rows) != VENDOR_PATHS:
        raise ValueError("complete 31-file vendor manifest required")
    return rows


def supervisor_source(accepted):
    # A newer checkout can add settings which an older accepted image rejects.
    # Bind its static validators to the actual accepted runtime. The example's
    # gameplay defaults may differ (for example the approved display-name change);
    # production.prepare merges those, and the final config is parsed below.
    for name in ("src/supervisor/config.py", "src/runtime/config.py"):
        if accepted["source_sha256"].get(name) != source_digest(ROOT / name)["sha256"]:
            raise ValueError("runtime configuration validators differ from the approved release; use its matching checkout")
    return read_object(ROOT / "config/supervisor-iccup.example.json")[0]


def validate_startup_config(path):
    """Parse the final merged config without starting a process, socket or account."""
    from src.supervisor.config import load_config

    config = load_config(path)
    expected_paths = {"vendor": "/vendor/star", "profile": "/vendor/profile",
                      "redist": "/vendor/redist/vc_redist.x86.exe", "game_metadata": "/vendor/game",
                      "prefix": "/data/prefix", "session": "/data/session",
                      "manifest": "/opt/war3/config/star-vendor-files.json",
                      "quit_helper": "/opt/war3/bin/quit-launcher.exe",
                      "native_config": "/data/config/ghost.cfg", "bridge_dir": "/data/state/bridge"}
    if (config["mode"] != "iccup" or config["hostbot"]["cwd"] != "/data"
            or config["hostbot"]["command"] != ["/opt/war3/bin/ghost++", "/data/state/native/ghost.cfg"]
            or config["runtime_dir"] != "/data/state/supervisor"
            or config["http"] != {"host": "127.0.0.1", "port": 8080}
            or any(config.get("iccup", {}).get(key) != value for key, value in expected_paths.items())):
        raise ValueError("supervisor configuration does not match production mounts/startup paths")
    return config


def locate_redist(star):
    # Bounded known paths only: never scan the proprietary tree or user home.
    for name in ("vc_redist.x86.exe", "redist/vc_redist.x86.exe", "Redist/vc_redist.x86.exe"):
        candidate = star / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    raise ValueError("supply --redist; VC2015 x86 installer not at a known Star path")


def private_copy(source, target, expected):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as outgoing:
        os.fchmod(outgoing.fileno(), 0o600)
        if source_digest(source, outgoing) != expected:
            raise ValueError("source changed after validation")


def analyze_selected_map(image_id, path):
    """Only the accepted Python analyzer runs; no runtime entrypoint or private binds."""
    name = "war3-map-analysis-" + uuid.uuid4().hex
    args = [*production.DOCKER, "run", "--rm", "--pull=never", "--name", name,
            "--label", "war3.purpose=offline-map-analysis", "--restart=no", "--network", "none",
            "--read-only", "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "32",
            "--memory", "256m", "--memory-swap", "256m", "--cpus", "1", "--ulimit", "core=0",
            "--log-driver", "none", "--no-healthcheck", "--stop-timeout", "1",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
            "--mount", f"type=bind,src={safe_path(path)},dst=/selected.w3x,readonly",
            "--workdir", "/", "--entrypoint", "python3", image_id,
            "-m", "src.runtime.presets", "/selected.w3x"]
    output = bytearray()
    try:
        with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=production.environment()) as process:
            try:
                deadline = time.monotonic() + ANALYSIS_TIMEOUT
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError("offline map analysis timed out")
                        for key, _ in selector.select(remaining):
                            chunk = os.read(key.fd, min(8192, ANALYSIS_LIMIT + 1 - len(output)))
                            if not chunk:
                                selector.unregister(key.fileobj)
                            else:
                                output.extend(chunk)
                                if len(output) > ANALYSIS_LIMIT:
                                    raise ValueError("offline map analysis output exceeded limit")
                try:
                    result = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise ValueError("offline map analysis timed out") from None
                if result:
                    raise ValueError("offline map analysis rejected the map")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        # Killing the Docker client alone does not stop its daemon-side process.
        # The unpredictable name belongs only to this setup attempt; never touch
        # the production singleton or another operator's analyzer.
        subprocess.run([*production.DOCKER, "rm", "--force", name], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=production.environment(), timeout=5, check=False)
    try:
        value = json.loads(output, object_pairs_hook=no_duplicates,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("offline map analysis returned invalid JSON") from None
    return value


def initial_preset(analysis, expected_sha, curated_path=None):
    """Keep reviewed Footman defaults; other maps start with AI disabled."""
    from src.runtime.presets import load_presets, lobby_name

    if (not isinstance(analysis, dict) or analysis.get("map_sha256") != expected_sha
            or not isinstance(analysis.get("script_sha256"), str)
            or not SHA.fullmatch(analysis["script_sha256"])):
        raise ValueError("offline map analysis hash mismatch or missing metadata")
    title = lobby_name(analysis.get("lobby_name"))
    slots, humans, evidence = (analysis.get(key) for key in ("slots", "human_capacity", "ai_evidence"))
    if (not isinstance(slots, list) or not 2 <= len(slots) <= 12
            or any(not isinstance(s, list) or len(s) != 9
                   or any(type(v) is not int or not 0 <= v <= 255 for v in s)
                   or (s[2], s[3]) not in ((0, 0), (2, 1)) or s[4] >= 12 or s[5] >= 12 for s in slots)
            or len({s[5] for s in slots}) != len(slots)
            or type(humans) is not int or humans != sum(s[2] == 0 and s[3] == 0 for s in slots)
            or not isinstance(evidence, dict) or not isinstance(evidence.get("features"), list)
            or any(not isinstance(f, str) or f not in ("ai_start", "computer_control") for f in evidence["features"])
            or len(set(evidence["features"])) != len(evidence["features"])):
        raise ValueError("offline map analysis capacity/evidence rejected")
    entry, record = None, None
    if curated_path is not None:
        presets, capabilities = load_presets(curated_path)
        candidate = presets.get("footman-ultimate-ai")
        if candidate and candidate["enabled"] and candidate.get("ai", {}).get("enabled"):
            proof = capabilities.get(candidate["ai"].get("capability"))
            if (proof and proof["map_sha256"] == expected_sha
                    and proof["script_sha256"] == analysis["script_sha256"]
                    and set(proof["features"]) == set(evidence["features"])):
                entry, record = dict(candidate), proof
                entry["map_filename"] = "selected.w3x"
    if entry is None:
        if humans < 2:
            raise ValueError("unapproved map needs at least two human slots for AI-off setup")
        entry = {"id": "selected-map", "enabled": True, "map_filename": "selected.w3x",
                 "lobby_name": title, "max_players": len(slots),
                 "start": {"autostart_enabled": False, "autostart_players": min(10, humans),
                           "countdown_seconds": 5},
                 "vote": {"enabled": True, "required_percent": 60, "min_players": 2,
                          "timeout": 60, "cooldown": 30}, "ai": {"enabled": False, "target": min(6, len(slots)), "skill": 1}}
        record = production.map_discovery.automatic_approval(analysis, [])
    maximum = entry.get("max_players", len(slots))
    ai_enabled = entry.get("ai", {}).get("enabled", False)
    human_budget = min(humans, maximum if ai_enabled else maximum - sum(s[3] == 1 for s in slots))
    start, vote = entry.get("start", {}), entry.get("vote", {})
    if (not 2 <= maximum <= len(slots) or human_budget < (1 if ai_enabled else 2)
            or vote.get("min_players", 2) > human_budget
            or start.get("autostart_enabled", True) and start.get("autostart_players", 6) > human_budget
            or ai_enabled and entry["ai"].get("target", 6) > maximum):
        raise ValueError("initial preset exceeds analyzed map capacity")
    return {"version": 1, "presets": [entry], "capabilities": [record] if record else []}


def setup(instance_dir, star_dir, game_dir, map_file, *, redist=None, image=None,
          gate_path=None, release_identity=None):
    destination = new_destination(instance_dir)
    accepted, correlation = local_acceptance(image, gate_path, release_identity)
    rows = vendor_manifest(accepted)
    supervisor = supervisor_source(accepted)
    star, game, selected_map = (safe_path(p) for p in (star_dir, game_dir, map_file))
    if not star.is_dir() or not game.is_dir() or selected_map.suffix.lower() not in (".w3x", ".w3m"):
        raise ValueError("Star/game directories and a Warcraft map required")
    if any(destination.is_relative_to(root) for root in (star, game)):
        raise ValueError("destination must be outside original proprietary directories")
    redist = safe_path(redist) if redist is not None else locate_redist(star)
    # Validate ALL sources and pins before prepare or any proprietary destination writes.
    copies = [(star / name, "vendor/star/" + name, row) for name, row in sorted(rows.items())]
    copies += [(redist, "vendor/redist/vc_redist.x86.exe", None),
               (game / "war3.exe", "vendor/game/war3.exe", None),
               (game / "game.dll", "vendor/game/game.dll", None),
               (game / "War3Patch.mpq", "data/warcraft/War3Patch.mpq", None),
               (selected_map, "data/maps/selected.w3x", None)]
    validated = []
    for source, target, expected in copies:
        try:
            actual = source_digest(source)
        except (OSError, ValueError) as error:
            raise ValueError("missing, unsafe or unreadable input for " + target) from error
        if expected is not None and actual != expected:
            raise ValueError("Star vendor size/hash mismatch for " + target)
        if target == "vendor/redist/vc_redist.x86.exe" and actual["sha256"] != REDIST_SHA256:
            raise ValueError("VC2015 x86 installer hash mismatch")
        validated.append((source, target, actual))
    presets_path = ROOT / "config/presets.example.json"
    presets = read_object(presets_path) if presets_path.exists() or presets_path.is_symlink() else None
    with tempfile.TemporaryDirectory(prefix=".war3-bootstrap-", dir=destination.parent) as directory:
        stage = Path(directory)
        production.private_json(stage / "acceptance.json", accepted)
        production.private_json(stage / "supervisor.json", supervisor)
        for name, content in (("ghost.cfg", NATIVE_CONFIG), ("selected.cfg", MAP_CONFIG)):
            path = stage / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o600)
        app = stage / "app"
        production.prepare(app, accepted["image_id"], stage / "acceptance.json", stage / "supervisor.json",
                           stage / "ghost.cfg", stage / "selected.cfg")
        validate_startup_config(app / "data/config/supervisor.json")
        for source, target, expected in validated:
            private_copy(source, app / target, expected)
        curated_path = None
        if presets is not None and accepted["source_sha256"].get("config/presets.example.json") == presets[1]:
            curated_path = stage / "approved-presets.json"
            production.private_json(curated_path, presets[0])
        analysis = analyze_selected_map(accepted["image_id"], app / "data/maps/selected.w3x")
        map_sha = validated[-1][2]["sha256"]
        generated = initial_preset(analysis, map_sha, curated_path)
        production.private_json(app / "data/config/presets.json", generated)
        # Use the same public schema and settings transaction as ordinary starts.
        from src.runtime.presets import load_presets
        load_presets(app / "data/config/presets.json")
        settings_path = app / "data/config/amp-settings.json"
        settings = production.read_json(settings_path)
        settings.pop("map_preset", None)
        settings["map_cycle"] = [generated["presets"][0]["id"]]
        settings["max_concurrent_matches"] = 3
        production.private_json(settings_path, settings)
        selected = production.validate_preset_selection(app)
        production.merge_settings(app / "data/config/supervisor.json", settings_path, validated=selected)
        validate_startup_config(app / "data/config/supervisor.json")
        with production.preset_manifest_update(app):
            pass
        deployment = production.read_json(app / "deployment.json")
        deployment["host_instance_dir"] = str(destination)
        production.private_json(app / "deployment.json", deployment)
        production.private_json(app / "release-correlation.json", correlation)
        # Final tree includes any account helper/input added by production.prepare.
        for path in (app, *app.rglob("*")):
            info = path.lstat()
            if (info.st_uid != os.getuid() or info.st_gid != os.getgid()
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise ValueError("unsafe prepared tree")
            path.chmod(0o700 if path.is_dir() else 0o600)
        new_destination(destination)
        rename_new(app, destination)
    return destination


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--noninteractive", action="store_true", help="require paths without prompting")
    trust = parser.add_mutually_exclusive_group(required=True)
    trust.add_argument("--release-identity", type=Path, help="trusted maintainer-exported release JSON")
    trust.add_argument("--gate", type=Path, help="main-approved gate for an EXACT local image ID")
    parser.add_argument("--image", "--image-id", dest="image", help="already-local image ID/reference; never pulled")
    for name in ("instance-dir", "star-dir", "game-dir", "redist", "map"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args(argv)
    try:
        for field, label in (("instance_dir", "NEW host application directory (war3-production)"),
                             ("star_dir", "Original Star directory"), ("game_dir", "Original Warcraft directory"),
                             ("map", "Chosen .w3x/.w3m map file")):
            if getattr(args, field) is None:
                if args.noninteractive:
                    raise ValueError("missing required path: --" + field.replace("_", "-"))
                answer = input(label + ": ").strip()
                if not answer:
                    raise ValueError("required path missing")
                setattr(args, field, Path(answer).expanduser())
        if args.redist is None and not args.noninteractive:
            answer = input("VC2015 x86 installer path (Enter to locate under Star): ").strip()
            if answer:
                args.redist = Path(answer).expanduser()
        setup(args.instance_dir, args.star_dir, args.game_dir, args.map, redist=args.redist,
              image=args.image, gate_path=args.gate, release_identity=args.release_identity)
    except ValueError as error:
        parser.exit(2, "Setup rejected: " + str(error) + ".\n")
    except subprocess.SubprocessError:
        parser.exit(2, "Setup rejected: local Docker image inspection failed; verify Docker access and image reference.\n")
    except (OSError, TypeError, KeyError, AttributeError, EOFError):
        parser.exit(2, "Setup rejected: unavailable/unsafe local path or malformed release input.\n")
    print("Private application prepared with the chosen map preset selected. "
          "Enter credentials in AMP Account setup or control/account-input.json, then Start through AMP.")
    return 0


def export_cli(argv=None):
    parser = argparse.ArgumentParser(description="Export public identity of an exact main-approved local image.")
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pull-ref", help="optional immutable reference present in this image's RepoDigests")
    args = parser.parse_args(argv)
    try:
        export_release_identity(args.image_id, args.gate, args.output, args.pull_ref)
    except ValueError as error:
        parser.exit(2, "Export rejected: " + str(error) + ".\n")
    except (OSError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        parser.exit(2, "Export rejected; exact main-approved gate/image and a new output path required.\n")
    print("Release identity exported; distribute through the trusted release channel.")
    return 0
