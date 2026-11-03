# Run without AMP

Use `scripts/war3.py` to set up and operate one War3 runtime with Docker and
Python 3. No AMP installation, licence, API, control image, host Wine, Compose
or AI assistant is needed. The existing runtime, release gate and offline
account helper provide the same validation as the managed deployment.

> The release image and identity mentioned below are being prepared and are not yet published.

## Requirements

Use a Linux amd64 Docker host, Python 3.10 or newer, and a normal non-root user
who owns the data and can access the local `/var/run/docker.sock`. Use the same
UID/GID for every command. The CLI does not install dependencies or change Docker
permissions. Only one instance can use the fixed container name and TCP 6113 on
this host; an existing AMP or unrelated container is never adopted or stopped.

Download the matching source archive, Docker image, release identity and
`SHA256SUMS` from the same [GitHub Release](https://github.com/Ravenger2709/War3_Dedicated_Public/releases). Verify the downloads, then import the image:

```bash
sha256sum --check SHA256SUMS
docker load --input war3-runtime-linux-amd64.tar.gz
export RUNTIME_PULL_REF=war3-dedicated:v0.4.1
```

Use the tagged source checkout accompanying the release, and the accompanying
`release-identity.json` from a trusted release. Setup checks the local image's
complete configuration, layer identity and source digest against that identity.
The release archive does not contain Star, Warcraft III, maps or credentials.

The CLI does not change repository/registry visibility. It never pulls or builds
an image, invents approval, deploys remotely, or discovers credentials.

Run commands below from the release checkout. The default private application
directory is `~/.war3-dedicated/instance`, separate from the checkout. Setup
creates the new `~/.war3-dedicated` parent with mode 0700; existing paths must pass
the same private-owner checks. The instance directory must be new at setup.
Every command uses this same home-based default, regardless of checkout location,
and also accepts `--instance-dir /absolute/private/app`; for a custom location
its owned, non-writable-by-others parent must already exist. Group-writable CI
checkout ancestors are not suitable data locations. Do not chmod the repository
or weaken these checks to use them. Keep the data directory at its recorded host
path: the ownership marker and Docker bind sources must match.

## Set up and enter the account

Supply your licensed Star directory, Warcraft III 1.26a directory, selected map
and VC2015 x86 installer. The runtime requires Star Launcher **4.3.75.75** and
the exact 31 files recorded in `config/star-vendor-files.json`; a different Star
version is rejected. Download Star from the [official iCCup page](https://iccup.com/files/view/iCCup_BETA_Star_Launcher.html)
and allow its official updater to update a separate installation if needed. The
VC2015 x86 installer hash is documented in the [AMP guide](operator-quickstart.md).
Never add the Star installation or its profile to Git or the Docker image:


```bash
python3 scripts/war3.py setup --noninteractive \
  --release-identity /absolute/trusted/release-identity.json \
  --image "$RUNTIME_PULL_REF" \
  --star-dir /absolute/licensed/Star \
  --game-dir /absolute/licensed/Warcraft-III-1.26a \
  --redist /absolute/licensed/vc_redist.x86.exe \
  --map /absolute/licensed/chosen.w3x

python3 scripts/war3.py account
```

Omit `--noninteractive` to be prompted for missing asset paths. `--redist` may be
omitted at the bootstrap's known Star installer locations. Alternatively use
`--gate /absolute/main-approved.json --image sha256:...` when the main-approved
gate names the exact local Docker image ID. Only the trusted release-identity
path permits index/classic ID correlation, after complete metadata equality.

Setup verifies the release/image, all 31 Star files and the pinned VC2015 hash.
It copies only those files, `war3.exe`/`game.dll` metadata, `War3Patch.mpq`, and the
selected map. Original assets stay unchanged. Directories are private mode 0700,
files 0600. The map is available as `data/maps/selected.w3x` inside the private
application tree. Setup performs no authentication or engine execution.

Setup analyzes that copied map with Python in the accepted image, offline and
read-only, then writes and selects one enabled preset. No initial filename or
preset-selector JSON edits are needed: enter the account, then run `start`.
Ordinary maps use their actual map title and player-slot capacity, with AI and
automatic countdown disabled and player thresholds bounded by human slots.
An exact release-approved Footman map reuses its curated gameplay/AI settings
and proof, adapted to `selected.w3x`. Static AI detection alone never enables AI.
The analyzer has a 30-second deadline, 64 KiB output limit, 256 MiB memory,
32-process and one-CPU limits; it mounts only the copied map, with no network or
account/profile access. Analysis errors leave no published application directory.

The `account` command delegates to the existing interactive helper: enter the
username and hidden password/confirmation in a terminal. There are no password,
username or profile command-line arguments. It stores private input; `start`
later generates the derived autologin profile offline in the accepted runtime
image, with networking disabled. Real Star authentication begins only when the
actual runtime starts. The shared helper may mention AMP in its confirmation;
apply changes with this CLI's `start` or `restart` commands.

## Start and maintain

```bash
python3 scripts/war3.py start
python3 scripts/war3.py status
python3 scripts/war3.py logs --tail 100
python3 scripts/war3.py logs --follow
python3 scripts/war3.py stop
python3 scripts/war3.py restart
```

Start runs in the background. Its initial restart policy is `no`; to let Docker
restart the standalone container after a crash or daemon/host restart, opt in:

```bash
python3 scripts/war3.py start --restart-policy unless-stopped
```

Use `restart --restart-policy unless-stopped` when already running. Stop leaves
the container stopped, including across daemon restarts with `unless-stopped`;
its logs remain accessible. The next CLI start/restart removes only that owned
stopped container, revalidates the gate/image/private mounts, applies account and
settings helpers, and creates a new container using the same persistent bind
data. Without an explicit override it preserves the retained container's policy.
Docker automatic restarts reuse the prepared files; they do not rerun account
generation or apply pending editor input. Use the CLI to apply such changes.

`start --foreground --restart-policy no` attaches STDIO and preserves the existing
foreground Docker signal path. Foreground mode cannot use automatic restart.
Stop uses the existing 120-second shutdown budget. A nonzero stop retains the
container for diagnosis and blocks an automatic CLI restart; inspect its logs.

`status` reports Docker process state and image health separately: running does
not imply authenticated readiness. Status/log following does not hold the
lifecycle lock, so another terminal can still stop the instance. Runtime health
comes from the accepted image; no public status/RCON port is added. Only TCP
6113 is published. Relay and status stay on container loopback, and the runtime
never receives the Docker socket. Logs and all account/profile files are private;
do not publish them or commit the private application directory.

## Configure maps and gameplay

Stop first. Edit `~/.war3-dedicated/instance/data/config/amp-settings.json` with a normal
text editor; the historical filename does not require AMP. It contains the
display name, manual gameplay defaults and `map_cycle` selection. Legacy
`map_preset` is normalized to a one-item cycle when no cycle is present. Setup selects
`selected-map`, or `footman-ultimate-ai` for the exact curated map. The selected
preset supplies its title, capacity and start/vote settings. Native validation
remains authoritative. If you later add a `manual` selection, it must be an
explicit enabled preset entry; it uses `data/config/maps/selected.cfg`.
Removed, disabled and unknown choices are rejected before runtime launch; the
validated selection is used for the configuration merge without rereading a
different selection midway through startup.

For later map/gameplay changes, edit
`~/.war3-dedicated/instance/data/config/presets.json`, put each additional licensed map
under `data/maps/`. On `start` or `restart`, the same discovery as AMP adds a
basic preset for each new supported map and preserves entries whose map files are
absent, after checking a stable maps directory. The built-in `manual` entry and
AI records are retained. If the selected preset's map is missing, start stops
with an error; the preset and selection remain unchanged.
Select available presets, then start again. Set `map_cycle` to an ordered JSON
array such as `["selected-map", "other-map", "selected-map"]` and set
`max_concurrent_matches` to 1–5 (default 3). The cycle contains 1–32 enabled
existing IDs; duplicates intentionally repeat a map. Preserve private ownership
and permissions. An explicit cycle takes priority over any retained legacy key.

With a matching cycle-aware accepted image, startup validates every multi-cycle
entry through that image's full-cycle validator before merging settings or
launching the runtime. This offline Python container mounts only config/maps
read-only, has no network or profile access, and uses bounded resources. Missing
maps, invalid capacities, unsupported AI settings or an older image without the
validator block the whole cycle. Single-entry startup retains its existing image
validation. The ordinary-config/catalog runtime integration is a separate
dependency; source-only tests in the existing `war3-dedicated:v0.4.1` image do
not validate a new release runtime or establish gameplay/iCCup GREEN.

`start --preset ID` and `restart --preset ID` deliberately select a single
preset only when the existing cycle has one entry. If it has multiple entries,
the CLI reports a conflict and preserves the selection; edit `map_cycle`
explicitly while stopped to replace it, or omit `--preset` to start the full
existing cycle. Unknown-crash recovery remains fail-closed: preserve unresolved
cycle state for reconciliation rather than clearing it to force a restart.

Every preset replacement, including additions,
AI records and rollbacks, retains the displaced inode under the historical name
`control/preset-deletion-history` so a concurrent File Manager writer can
finish without losing its edits. Selector updates similarly retain
their replaced files under `control/preset-metadata-history`. These histories
carry a hash of the retained bytes at exchange time. Before a later start or
preset apply, the controller checks the histories; if an old editor saved after
the exchange, it refuses to start and reports the `displaced.json` recovery path.
Close the editor, review and merge its changes into the current file, then
remove the reviewed history folder. This is detected at the next operation, not
immediately in an already running instance. Histories created by older versions
without a baseline must also be reviewed and removed before proceeding.
Histories
have **no automatic expiry** and grow when changes are made: periodically stop
the instance, close all File Manager/editor sessions, review each retained
`displaced.json`, and delete the reviewed `war3-preset-*` subdirectories through
File Manager. Never delete `control/preset-conflict` or an active top-level
`control/war3-preset-*` as routine cleanup: they can contain recovery data.
An interrupted exchange leaves a top-level `control/war3-preset-*` directory;
startup stops for operator recovery, preserving the old file there.
Configure automatic countdown per preset with `start.autostart_enabled` and
`start.autostart_players`. For detected `computer_control`, setup and discovery
create a hash-bound static record; an AI starter call alone is insufficient.
AI remains off until enabled by the operator. Static
evidence does not prove in-game behavior. Setup's initial preset already names
`selected.w3x`; additional presets must name the actual added map files.

After editing, run `start` or `restart`. Keep the account JSON and preset editor
input blank when retaining their already applied files; never use a stale editor
copy to overwrite later manual changes. No template refresh or AMP action is
required for standalone operation.

## Backup and updates

Reuse the existing stopped-state backup tools. Remove the owned stopped container
first because the shared backup safety check refuses any same-named container:

```bash
python3 scripts/war3.py stop --remove
python3 scripts/production-backup.py --instance-dir "$HOME/.war3-dedicated/instance" \
  --output /absolute/private/war3-stopped.tar.gz
python3 scripts/production-restore.py /absolute/private/war3-stopped.tar.gz \
  /absolute/new-private-data
```

The archive contains config/maps/state/sqlite/logs/replays. Vendor assets,
profile, prefix and session are excluded; retain licensed sources and account
provisioning separately. Restore creates a new data tree, not a complete runnable
instance. Prepare a fresh standalone instance with its trusted release, restore
the required data deliberately, then re-enter the account and validate settings.

For a new release, stop/remove first and back up. Use that release's matching
checkout, approved gate/identity and image to prepare a new standalone directory;
transfer validated configuration/data while keeping the old instance stopped.
There is no automatic image update, approval bypass or simultaneous second account
session. Starting and health-checking a new image remains part of its acceptance.
