# Persistent original Star worker, version 1

The production controller owns account cohort policy, relay proof, restart
backoff and readiness. This worker owns one original Launcher, win32 Wine
prefix and Xvfb. `RUNNING` means an observed live Launcher process, and never
means authenticated, connected, publish-ready or lobby-visible. The worker
performs no authentication retry and generates no authentication/Guard traffic.

## Entry point and configuration

```sh
python3 -m src.runtime.star --config /data/state/star-worker.json
```

The controller atomically creates this exact JSON schema, version 1:

```json
{
  "version": 1,
  "vendor": "/vendor/star",
  "profile": "/vendor/profile",
  "redist": "/vendor/redist/vc_redist.x86.exe",
  "game_metadata": "/vendor/game",
  "prefix": "/data/prefix",
  "session": "/data/session",
  "status": "/data/state/star-status.json",
  "manifest": "/opt/war3/config/star-vendor-files.json",
  "quit_helper": "/opt/war3/bin/quit-launcher.exe",
  "relay_port": 16112,
  "startup_timeout": 180,
  "generation": 1
}
```

Every key is required. Unknown or duplicate keys are rejected; booleans are
not accepted as integers. Paths must be absolute, must not contain `..`, and
must have no symlink components. Paths are configurable for container CI,
without test/authentication switches. `generation` is a positive integer;
`startup_timeout` is 1–3600 seconds and `relay_port` is 1–65535.

The config must be a single-link regular file owned by the runtime UID with
mode 0600. The existing state directory must be owned, mode 0700. Prefix and
session directories are created mode 0700 or must already satisfy those
checks. Writable roots cannot overlap vendor, profile, game metadata, each
other, or the status path. Source profile directory is owned mode 0700;
`launcher.ini` and `iccup.ini` are single-link, owned, mode 0600 files.
Existing `profile_username` and `require_loopback_profile` are used only for
validation. Username, saved-login cache, profile digests and parser exceptions
are never emitted. The worker does not read a canonical password file.

Mount vendor/profile/redist/game metadata read-only. Both metadata files
`war3.exe` and `game.dll` must be regular files on read-only mounts, enforced
with `statvfs`; neither is copied or executed. The pinned manifest contains
exactly 31 public filenames, lengths and SHA-256 values. Launcher is pinned to
`9ce99d541aaef4cb1b2a60a0837580678f81ed0494795efc720fe84c99a6253e`.
Every allowlisted input is hash-checked before copying; copied bytes are checked
again before Wine starts. Unknown files, links, game PEs, profiles or logs in
vendor input are rejected. Optional public `input-manifest.json` must exactly
match the manifest inventory and is not copied.

The writable `/data/session/launcher` is reconstructed under the session lock.
Only the 31 vendor files and the worker's protected profile copies are staged.
Copied `iccup.ini` forces Common `AutoUpdate=0`, `AutoUpdateMap=0`,
`RunAtStart=0`, and Warcraft III `AutoLoginOn=1`,
`MainExe=Z:/vendor/game/war3.exe` (using the configured metadata path).
The source profile is unchanged. Vendor files and profile copies are mode 0600.

## Wine and process lifecycle

Use non-root Linux amd64, read-only container root, `--cap-drop ALL`,
`no-new-privileges`, writable private data and a writable `/tmp` tmpfs.
Wine/Xvfb/system tools come from the explicitly pinned Wine base image. The
actual base image ID is recorded in the `war3.production.wine-id` image label
and build receipt.
The image supplies `wine`, `wineboot`, `wineserver`, `Xvfb`, `xdpyinfo` and
Python 3; no native BNCS library is required by this worker. Build
`quit-launcher.exe` from `docker/wine-research/quit-launcher.c`; no opaque helper
or vendor executable is included in Git or the image.

The child environment is constructed explicitly, rather than inheriting account
variables or Qt overrides:

```text
WINEARCH=win32
WINEPREFIX=/data/prefix
HOME=/data/session/home
TMPDIR=/data/session/tmp
DISPLAY=:99
WINEDEBUG=-all
WINEDLLOVERRIDES=winemenubuilder.exe=d;mscoree,mshtml=d;vcruntime140,msvcp140,concrt140=n,b
LIBGL_ALWAYS_SOFTWARE=1
QT_OPENGL=software
QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu
```

Paths follow configuration. Launcher cwd is `/data/session/launcher`.
Xvfb uses `-nolisten tcp`, and receives no GPU/device privileges. There is no
Qt sandbox bypass or additional renderer crash flag. stdout/stderr/stdin of
all vendor and Wine commands are `/dev/null`. Core dumps are disabled. Before
profile provisioning, registry writes disable Wine crash dialogs, replace
AeDebug's debugger with `cmd /c exit 1`, disable automatic debugging and disable
Windows Error Reporting/UI. No screenshots, backtraces, process memory, raw
vendor diagnostics or authentication payloads are captured.

Bootstrap is bounded by `startup_timeout`, including staging, X readiness,
wineboot, registry, native runtime provisioning and Launcher observation.
Individual command timeouts are also enforced. Once Launcher is observed,
the worker runs indefinitely until a stop request or a detected failure.

VC2015 x86 installer SHA-256 is
`fdd1e1f0dcae2d0aa0720895eff33b927d13076e64464bb7c7e5843b7667cd14`.
Install only if `.star-vc2015.json` is absent; marker creation follows successful
installation and hashes of all three installed native runtime DLLs. Subsequent
starts validate marker ownership, installer identity, win32 architecture and
current DLL hashes. A corrupt marker/DLL fails closed instead of silently
reinstalling. Prefix contents survive worker and container restarts.

Nonblocking advisory locks `.star-worker.lock` in both prefix and session are
held for the entire worker lifecycle. Lock descriptors are CLOEXEC, so worker
SIGKILL releases them even while Wine descendants survive. `.star-owner.json`
is private atomic metadata binding UID and prefix device/inode to recorded
Xvfb/Launcher Linux PID, process group and start ticks. Direct children use Linux
parent-death SIGKILL with a parent-PID check, closing the spawn/record race.
Wine descendants are additionally cleaned through their own prefix server.

On SIGTERM/SIGINT, invoke `wine <quit_helper>` (12-second limit), wait for
Launcher (2 seconds), invoke `wineserver -k` and `-w` with that exact prefix
(12-second cleanup budget), stop the recorded Xvfb group and reap direct
children. Remaining direct groups are killed after identity checks. Allow
**35 seconds** in the parent before escalating to worker SIGKILL. Helper
failure is recorded and forces cleanup; it never launches a replacement.
Unexpected Launcher exit, Xvfb exit, unreadable process metadata or a game
engine observation exits nonzero. No session/authentication retry occurs here.

## Private file limits

Every heartbeat sweeps prefix and session without following directory symlinks
(including Wine's `dosdevices` links). Log files are owned, single-link regular
files forced to mode 0600. `*.log`, including `messages.log`, `debug.log` and
`Launcher.log`, plus `__log.txt` and `backtrace.txt`, use a **10 MiB live limit
and at most three 10 MiB archives**. Rotation copies a bounded tail into a
private archive and truncates the original inode so an open vendor log
descriptor stays attached. Contents are never decoded, parsed or output.
Writes between sweeps can transiently cross the limit; a non-append vendor
descriptor can retain a sparse offset after truncation, which is truncated on
the next sweep.

The combined budget for these logs and archives across both roots is **64 MiB**;
old archives are removed first and live logs are truncated as needed. Old or
excess-numbered archives are bounded too. Dump filenames `*.dmp`, `*.mdmp`,
`*.core`, `core` and `core.*` are truncated/unlinked using metadata only, never
read or copied. Game/vendor inputs, INI/profile files, native runtime DLLs and
other persistent prefix data are outside this log budget and remain intact.
Unsafe links/hardlinks in managed logs fail the worker closed with
`LOG_BUDGET_FAILED`. File size is controlled by periodic sweeps, not by reading
account diagnostics or memory dumps.

## Status and parent recovery interface

Status is an atomic, owned, mode 0600 JSON document, updated every 0.5 seconds
(polling granularity 0.2 seconds), including command waits and shutdown:

```json
{
  "version": 1,
  "worker_pid": 200,
  "phase": "RUNNING",
  "launcher_pid": 240,
  "launcher_alive": true,
  "game_engine_observed": false,
  "observed_monotonic_seconds": 12345.0,
  "generation": 1,
  "exit_code": null,
  "normal_close_helper_exit": null,
  "forced_stop": false,
  "reason": "PROCESS_RUNNING",
  "renderer_count": 1,
  "renderer_health": "PRESENT"
}
```

Required keys are unchanged from the controller contract. `phase` is only
`STARTING`, `RUNNING`, `ERROR`, `STOPPED`; `launcher_pid` is positive or null;
`normal_close_helper_exit` is an integer or null if not run/timed out.
`game_engine_observed` is sticky. `observed_monotonic_seconds` records actual
process observation, not authentication freshness. Generation must match the
controller's current cohort. Exit code is null until terminal, 0 on requested
stop including successful forced cleanup, 70 on runtime failure. Config/root
rejection returns 78 and emits only a fixed `STAR_WORKER_REJECTED` event;
a rejected duplicate does not overwrite another worker's status.

Optional `renderer_count` and `renderer_health` are separate process metadata.

`monitor_error_codes` contains bounded fixed codes only, latched until worker
exit so successful shutdown samples cannot erase the original failure. Process maps are read
as bytes because Linux mapping paths need not be UTF-8; the monitor compares
only ASCII engine module basenames and never emits mapping paths. A stat/maps
race gets one complete metadata retry. A process confirmed exited/zombie is no
longer a live mapping target; an unchanged unreadable live process still fails
closed. A non-UTF8 directory cannot hide a `game.dll` module.

Docker HEALTHCHECK/exec temporarily exposes a protected pre-exec
`runc:[2:INIT]` with kernel PPID0 (parent outside this PID namespace) and its own
process group. Only its maps `PermissionError`, after fresh stat in that same
attempt, is classified as container initialization rather than app failure.
The same process name with an in-container parent remains fail-closed, as do
all other unreadable processes. Application children cannot create a parent
outside their own PID namespace. This exception neither grants ptrace
capabilities nor reads protected mapping contents. A network-none reproducer
without Wine/profile observed27 failures before the fix and zero over14,213
snapshots/60,566 fork-execs after it; the protected runc cases were still
observed. `container_init_observations` records these bootstrap observations.
Health values are `NOT_OBSERVED`, `PRESENT`, `LOST` (sticky once an observed
renderer disappears). This is not a claim of functional UI, authentication or
a diagnosed crash. Prior static research records Qt 5.6.3 WebEngine faults
under Wine, including with Mesa software rendering and a sandbox switch;
native VC2015 alone did not establish a renderer fix. No new account-bearing
crash experiment is performed. Parent readiness must always depend on fresh
original-relay authentication proof independently of Launcher/renderer counts.

The monitor reads only same-UID `/proc/*/stat` and `/proc/*/maps`. It never reads
cmdline, environ, mem or dump data. `war3.exe`, Warcraft III executable names
(including Linux comm truncation), or mapped `game.dll`/`war3.exe` fail closed.
Process/module names themselves are not published in status.

After worker SIGKILL, the parent must reap the worker and call:

```python
from src.runtime.processes import stop_owned_prefix
result = stop_owned_prefix(prefix, timeout=12)
```

Exact signature: `stop_owned_prefix(prefix, timeout=12)`; prefix accepts `Path`
or absolute path string. It obtains the prefix lock, validates the ownership
record, performs prefix-scoped `wineserver -k/-w`, and stops the recorded Xvfb
group with PID/start-time reuse checks. No global Wine kill or arbitrary process
match is used. Return keys are exactly:

```json
{"version":1,"cleaned":true,"reason":"PREFIX_CLEANED","kill_exit":0,"wait_exit":0,"xvfb_stopped":true}
```

False results use `PREFIX_UNSAFE_OR_BUSY`, `PREFIX_CLEANUP_FAILED`, or
`PREFIX_CLEANUP_TIMEOUT`; exit fields may be null. **Do not start the next account
cohort unless `cleaned` is true.** A newly started worker also recovers a marked,
unlocked prefix before staging, and refuses an unmarked, nonempty prefix.
Parent controls all restart/backoff and prevents concurrent account sessions.

Idempotent cleanup also permits `kill_exit=1` when `wait_exit=0` and
`xvfb_stopped=true`: Wine11 returns1 from `-k` if no server remains to signal.
A bounded successful `-w` is still mandatory. Other nonzero codes, unsuccessful
wait or timeout remain failures. The actual offline packaged-component probe
exercises cleanup after a successful WM_CLOSE, then restarts the same prefix.

Reason codes are fixed public constants: `STARTUP`, `PROCESS_RUNNING`,
`SIGNAL_STOP`, `SIGNAL_STOP_FORCED`, `WORKER_FAILED`, `SHUTDOWN_FAILED`,
`STARTUP_TIMEOUT`, `STARTUP_PROCESS_CONFLICT`, `XVFB_EXITED`,
`XVFB_IDENTITY_FAILED`, `PREFIX_BOOT_FAILED`, `PREFIX_RECOVERY_FAILED`,
`PREFIX_NOT_OWNED`, `DEBUGGER_SUPPRESSION_FAILED`, `VC_MARKER_REJECTED`,
`VC_INSTALL_FAILED`, `VENDOR_MANIFEST_REJECTED`, `VENDOR_INPUT_REJECTED`,
`VENDOR_HASH_MISMATCH`, `REDIST_HASH_MISMATCH`, `INPUT_REJECTED`, `INPUT_CHANGED`,
`QUIT_HELPER_REJECTED`, `GAME_METADATA_NOT_READ_ONLY`, `PROFILE_REJECTED`,
`PROCESS_MONITOR_FAILED`, `LOG_BUDGET_FAILED`, `LAUNCHER_EXITED`,
`GAME_ENGINE_OBSERVED`. Filesystem/lock exceptions become `WORKER_FAILED`.

## Synthetic validation

Tests execute only in local Docker `--network none`, non-root, capabilities
dropped, read-only root. Bind the repository read-only at `/repo` and an
independent synthetic metadata directory (two ordinary non-PE text fixtures
named `war3.exe` and `game.dll`) read-only at `/fixtures/game`. No original
vendor files, real profile, canonical password, game engine or account is
mounted/executed. Run:

```sh
python3 -m unittest discover -s tests/phase3 -p test_star_worker.py -v
```

Tests inject subprocesses/clocks through internal Python APIs, with no
configuration bypass. They cover an unbounded 240-second virtual session,
heartbeat gaps, original-process crash, engine process/module rejection,
prefix retention and validated install-once marker, registry/profile ordering,
secret-free child environment, renderer separation, graceful/forced shutdown,
atomic private files, log rotation/budgets/symlinks, and a real Linux SIGKILL
lock-release/process-group cleanup regression using harmless Python sleepers.
These results do not establish live authentication, long-term Qt compatibility,
real gameplay, WAN readiness or AMP deployment.
