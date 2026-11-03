# Account setup through AMP

This feature prepares Star's saved AutoLogin profile from an ordinary account
username and password. The operator enters both once, saves, and uses AMP
Start/Restart. No AI assistance or recurring console work is needed. Star remains
the application that authenticates; setup sends no authentication traffic and
does not run Star, Wine, Guard, the game engine, or the native hostbot.

The account username is independent of the lobby display name. Gameplay
`host_name` continues to default to `War3HostBot`. Installing these sources does
not change any existing account or deployment.

## Operator flow

1. Stop the application through its AMP owner before applying account changes.
2. Open **War3_Dedicated → Account**. Enter **Account username** and
   **Account password**, then save both fields. The password field is masked.
3. Use AMP **Start** (or **Restart** when changing settings for the next start).
   The launcher generates a private profile offline, validates the remaining
   launch inputs, and then starts the runtime normally.

Saving fields while the application runs only changes the input for its next
start. There is no live profile update. To retain an existing derived profile,
leave **both** fields empty. A missing input file on an older installation also
retains the existing-profile workflow. A username without a password, or a
password without a username, rejects Start; it never silently uses an old login.
Clearing both fields does not remove the existing profile.

For first setup, `prepare()` installs `control/account.py` alongside the existing
launcher and creates an empty private input template. No existing saved profile
is required. Supply the normal accepted image/gate and operator-owned vendor
inputs separately. Account generation uses `deployment.json.uid/gid`, which must
match the actual non-root AMP data owner (for example `1000:1000` in CI or
`1001:1001` on the target). Root and mismatched owners reject generation. An unconfigured legacy
profile can continue using its existing ownership contract.

## File Manager and optional terminal

AMP File Manager can edit the same application-relative file:
`control/account-input.json`. Its initial content is:

```json
{
  "version": 1,
  "username": "",
  "password": ""
}
```

Replace the two empty strings with the desired login using normal JSON escaping,
save, import settings from this account file in AMP, and use AMP Start/Restart.
Import keeps AMP's saved fields consistent with File Manager edits, since AMP
can rewrite mapped JSON from AppSettings on save/start. This file is deliberately **outside**
`data/config`. Do not move it into Data, supervisor settings, native config, a
map config, or a source checkout. File Manager displays the JSON password in
clear text to an authorized reader; use the masked Settings field when preferred.

The schema requires exactly these three fields, integer version `1`, and strings
of at most 64 UTF-8 bytes for each account field. Configured fields must both be
nonempty. Controls, NULs, invalid Unicode, duplicate JSON keys, unknown fields,
symlinks, hardlinks, oversized files, and unsafe permissions reject setup.

Keep the application, `control`, `vendor`, profile, and temporary profile
directories owned by their data owner with mode `0700`; the input and profile
files use `0600`. File Manager edits must preserve the private mode. The launcher
fails closed rather than silently repairing an exposed credential file.

An optional terminal prompt, from the application directory, stores the same
input without taking credentials in arguments or environment variables:

```sh
python3 control/account.py --instance-dir .
```

It requires a terminal, uses `getpass` twice for the password, and only saves the
private input. Apply it through AMP Start/Restart. File Manager needs no terminal.
When combining Settings, File Manager, and the terminal prompt, import the file
or set matching fields in Settings before starting through AMP. Do not rely on
a JSON edit to erase AMP's saved AppSettings.

## Storage and execution boundary

The masked UI is a display control, not an encryption boundary. AMP can persist
the username/password in its private `App.AppSettings`/instance configuration in
addition to `control/account-input.json`. Administrators, the AMP owner, host
root, and Docker-daemon administrators can access those values. Protect AMP's
configuration, backups, and File Manager permissions accordingly. Clearing the
input does not promise to erase older AMP backups, logs produced by AMP itself,
filesystem snapshots, or Python process memory.

The gameplay AutoMap template contains only gameplay keys. A second JSON
metaconfig maps the account fields to `control/account-input.json` and allows an
explicit settings import after File Manager edits. Neither account key belongs
in `amp-settings.json` or `supervisor.json`.
The runtime command binds specific Data and vendor directories; it never binds
`control`, the persistent canonical input, or its temporary canonical copy.
The saved derived profile remains sensitive and is the only account material
deliberately available to the actual runtime.

For each configured Start/Restart, the launcher:

1. Validates deployment identity, the accepted image ID/source/platform gate,
   and absence of any container with the managed runtime name. A foreign or
   stopped same-named container also rejects the operation; setup never removes
   it or stops another owner.
2. Takes a private setup lock and the existing runtime-compatible exclusive
   `flock` on `vendor/profile/launcher.ini`. A held lock rejects the operation.
3. Creates a private staging directory under `control`. The offline container
   uses **the same `spec["image_id"]`** and overrides the entrypoint with
   `python3 -m src.bridge.star_profile`, already included in the accepted image
   with libbncsutil. It never builds an image or downloads anything.
4. Gives this one container only four bind mounts: a one-use canonical JSON copy
   read-only, `vendor/star/Launcher.exe` read-only, generated non-secret defaults
   read-only, and a fresh empty output profile directory writable. The whole
   control directory and the old profile are never mounted into it. Host bind
   paths honor `deployment.json.host_instance_dir` when AMP runs in a container.
5. Runs with network `none`, read-only root, the validated deployment UID/GID, all capabilities
   dropped, no new privileges, core limit zero, no healthcheck or Docker logs,
   32 PIDs, 128 MiB RAM/swap limit, and half a CPU. The client timeout is 45
   seconds; cleanup targets only that invocation's unique helper-container name.
   Its output and errors are discarded. No password, saved-login bytes, digest,
   or credential fingerprint is emitted into console output, results, arguments,
   source, or image layers. The temporary canonical copy is removed in `finally`.
   AMP SIGTERM during setup triggers the same cleanup and rollback before exit.
6. Validates output files, AutoLogin and loopback defaults, locks the new profile,
   rechecks the stopped state, and atomically exchanges profile directories with
   Linux `renameat2(RENAME_EXCHANGE)`. Unsupported filesystems reject the change.
   Full launch preflight/settings validation runs before the transaction commits.
   Generation, exchange, or preflight failure restores/retains the previous
   profile. First setup failure restores the prior empty or absent profile.

The generated `[General]` defaults use an empty legacy `Connections`, selected
`CurrConnections=0`, and matching Qt `SiteConnections`/`UserConnections` lists
containing only `127.0.0.1:16112`. This is the existing worker/relay format.
The existing runtime worker fixes `MainExe` to its mounted game metadata path.
Setup implements neither authentication nor native configuration changes.

Successful replacement keeps at most one private previous profile at
`control/account-profile.previous`, outside runtime mounts. A later successful
replacement removes the older backup. On a failed rotation, the active old
profile remains usable; a superseded backup may already have been removed.
The setup and profile locks are released before launching the runtime. A
successful profile commit does not establish that a later real authentication
will succeed. If the filesystem also rejects rollback, startup fails and the old
profile is preserved in the private previous-profile backup, or under a private
`control/war3-account-*/profile` recovery directory if moving that backup fails
too. Canonical temporary input is still removed. Keep AMP stopped and have its
data owner restore that old profile and remove the inspected staging directory
before retrying. Retained staging blocks further automatic updates, preventing
additional old-profile backups from accumulating. This transaction handles
ordinary exceptions and interruption;
host power loss or forced process termination still requires operator inspection
of private staging files before a retry.

## Integration contract

- Repository imports (`amp.production`/`amp.account`) resolve sibling modules
  relative to the package. Installed control copies use flat sibling imports.
  Repository setup requires only the repository root on `sys.path`; no
  `ROOT/amp` insertion is needed. Fresh-process tests exercise prepare, preset
  initialization, account command construction and matching error types in both
  contexts without Docker or interactive credential input.
- `production.image_preflight(instance, spec)` performs the existing accepted
  image identity/source/platform check and `stopped(spec)` without requiring a
  preexisting profile.
- `account.profile_update(instance, spec)` is a context manager used only on the
  AMP `run` path, before full `preflight` and `merge_settings`. It yields no
  credentials or credential identifiers. `plan` and standalone `preflight` do
  not generate profiles. A legacy instance needs the new helper installed
  alongside `production.py`; a missing account input means unconfigured.
- Gameplay `map_preset` is a string defaulting to `manual`. AMP accepts identifiers
  matching `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` and merges the value into
  `supervisor.json.gameplay`. Existing gameplay files missing this new key get
  `manual`. Preset definitions have a separate fixed location,
  `data/config/presets.json`; the editor copies validated JSON there without map analysis.
  The map/runtime change must add `map_preset` to `GAMEPLAY_DEFAULTS` and validate
  selections before this combined feature is deployed. The baseline runtime at
  `475c675` does not yet accept that new gameplay key.
- UI coverage checks 14 exposed gameplay fields, with 19 internal default settings, and
  separately checks the two account fields and one preset editor field.
- Application settings use `Category=War3_Dedicated:sports_esports`. The six sections
  are `Account:account_circle:10`, `Map presets:map:20`, `Lobby:groups:30`,
  `Lobby / Messages:chat:35`, `Voting:how_to_vote:40`, and `Refresh:refresh:50`; the category suffix selects
  the icon while the visible sidebar name remains `War3_Dedicated`.
- `game_name` is labelled **Manual lobby name**. Manual lobby/player/start/vote
  fields explain that a selected preset can override them; host display name
  and refresh settings remain global.
- `autostart_players` and `autostart_enabled` are internal compatibility settings
  omitted from the AMP manifest entirely. `Hidden=true` maps to read-only behavior
  in the inspected AMP build and does not reliably remove the controls.
  `prepare()` still writes both keys to `data/config/amp-settings.json`, and the
  settings parser and gameplay merge retain them.
  Old settings files default `autostart_enabled` to true. The threshold stays
  bounded to 2–12 and only has to fit `max_players` when auto-start is enabled;
  the vote minimum always has to fit the maximum. Normal presets own the visible
  configuration through `start.autostart_enabled` and `start.autostart_players`.
  `autohost_enabled` creates/rehosts lobbies and is separate from auto-countdown.
- The custom gameplay `game_port` and `max_players` controls are omitted from
  the manifest. Their internal defaults remain in the settings file and merge
  behavior. The instance port is shown in ADS **Edit Settings**; the child panel
  omits a duplicate **Network and Ports** category. The port descriptor keeps
  `Ref=GamePort` and TCP 6113, matching the runtime publish. Map capacity stays
  runtime-owned.
- The global AMP lobby refresh interval declares a 10-second default/minimum
  and a 3600-second maximum. It applies to every preset through the global
  gameplay fallback; presets gain no refresh-specific schema. Matching helper,
  runtime and native validation/defaults must be integrated together.
- **Lobby / Messages → Lobby message interval (seconds)** sets the shared cadence
  for periodic auto-start and `!vote` lobby reminders. The global integer setting
  `lobby_message_interval` defaults to 20 seconds and accepts 10–3600 seconds,
  applying to every preset on the next AMP Start/Restart. Existing
  `data/config/amp-settings.json` files missing the key receive the default during
  validation without rewriting that file. The gameplay merge always forwards the
  global value. Runtime maps it to `bot_lobby_message_interval` outside the
  per-preset preload manifest; preset definitions gain no interval fields.

The AMP JSON AutoMap and private AppSettings behavior follow the
[official Generic module documentation](https://github.com/CubeCoders/AMP/wiki/Configuring-the-%27Generic%27-AMP-module).
The account input uses the documented
[password field type](https://github.com/CubeCoders/AMPTemplates/blob/main/terrariaconfig.json).

## Preset editor and dropdown

**War3_Dedicated → Map presets** contains an enum selector and a multiline
**Preset definitions JSON** editor, using the stock `Textarea` input type seen
in the [official MongoDB template](https://github.com/CubeCoders/AMPTemplates/blob/main/mongodbconfig.json).
Initial choices are `manual` and `footman-ultimate-ai` (Footman Ultimate AI).
The editor contains the complete version 1 document accepted by the main map
feature: `version`, the `presets` list, and optional `capabilities`. Every entry
needs a unique lowercase `id` and boolean `enabled`. With a presets document
present, the selector lists exactly its enabled IDs; it adds no implicit
`manual` choice. If explicitly supplied, `manual` must be enabled and contain
only `id` and `enabled`. Custom-only documents list only their created IDs.
Documents with no enabled presets reject apply/Start instead of generating an
empty selector. Only a missing presets file gets the bootstrap `manual` fallback.
The helper checks bounded JSON, duplicate keys, the document
envelope and discovery fields. Runtime owns the remaining preset schema,
map evidence, AI approval, and gameplay validation; this helper runs no map code.

AMP maps the textarea into `control/preset-editor.json`, initially:

```json
{"version": 1, "preset_definitions": ""}
```

The wrapper deliberately stores text as a JSON string. On apply/Start the helper
parses that text and writes an actual JSON **object** into
`data/config/presets.json`, never a JSON string containing the file body. The
editor key never goes into gameplay settings or supervisor configuration.
No direct template expansion, remote option URL, command discovery metadata,
additional HTTP server, or account credentials are needed.

The editor placeholder shows a custom preset with
`"start":{"autostart_enabled":true,"autostart_players":6,"countdown_seconds":5}`.
This is a hint, not a saved default: the input stays blank until deliberately
filled or seeded from the actual installed public presets by main's integration.
Applying definitions leaves `amp-settings.json.map_preset` unchanged. After
restarting AMP, explicitly select one of the enabled IDs before Start, especially
when the previous selection (including the initial `manual`) is absent from the
new document. Apply remains allowed with that stale selection so definitions
can be installed before choosing a new ID. The `run` path then checks selection
against the enabled IDs after applying the preset transaction and before
settings merge or runtime launch. A missing, disabled, or removed selection,
including `manual` absent from an existing document, rejects Start with the
fixed message **Select an enabled preset**. Only a missing presets file allows
the legacy `manual` fallback. Start merges the same validated settings snapshot;
input edits made during preflight take effect on a later Start. The helper never
silently switches the selected map, and this check requires no AMP API.

`prepare()` installs an empty editor input, its private baseline, and
`control/preset-configmanifest.json` for source-only preparation. Bootstrap can
call `production.initialize_preset_editor(instance)` after copying the real
presets file and before UI edits, so the baseline reflects that installed file.
This initializer refuses to reset a nonempty editor. For a previously installed
instance, create the editor/baseline while the editor is blank before first use.

The private `control/preset-editor-baseline.json` tracks the last applied editor
document and its associated file document. It contains no account material.
Blank editor text adopts the current File Manager file without rewriting it.
Unchanged editor text also leaves later File Manager edits untouched. If both
sources change independently, apply/Start rejects instead of overwriting the
file. To resolve that conflict, clear the editor and apply once to adopt the
File Manager version, then edit/apply the desired document. If the new editor
document already matches the file, it can be adopted directly. Failed launch
preflight restores the prior preset bytes and selector manifest when the
canonical preset file still belongs to that transaction.

The helper rechecks inode/content/revision immediately before mutation and
rollback, and uses atomic file exchange so unexpected displaced saves are not
deleted. A File Manager save observed during a failed preflight is left in the
canonical file. A save racing between the final check and exchange may instead
be displaced; its inode is retained at `control/preset-conflict/displaced.json`,
including any writes completed through an already-open File Manager descriptor.
The private recovery directory also holds bounded previous/attempted/external
snapshots where available. If recovery copies cannot complete, the original
displaced inode stays under a private `control/war3-preset-*` directory.

Such a conflict rejects startup/apply, rolls back the selector where possible,
and blocks further automatic applies until the data owner inspects the recovery
copies, restores the desired canonical file, and removes the inspected recovery
directory. Keep the application stopped during resolution. The helper also
checks the accepted preset revision around selector regeneration, so a detected
concurrent change cannot be silently committed as an accepted new option list.
File Manager does not honor the helper's flock: these are optimistic checks and
preserved displaced data, **not** a strict compare-and-swap or a guarantee that
no concurrent writer can replace the file after a final check. That stronger
guarantee needs cooperating/versioned File Manager saves.

Applying presets does not require starting the bot:

```sh
python3 control/production.py apply-presets --instance-dir .
```

For the normal UI flow, the textarea's **Apply presets** action invokes stock
`Core/UpdateApplication` with no arguments. The main integration owns the
Executable update stage to `amp/war3-productionupdates.json` (owned by main):

```json
{
  "UpdateStageName": "Apply local map presets",
  "UpdateSourcePlatform": "Linux",
  "UpdateSource": "Executable",
  "UpdateSourceData": "/usr/bin/python3",
  "UpdateSourceArgs": "\"{{$FullBaseDir}}control/production.py\" apply-presets --instance-dir \"{{$FullBaseDir}}\"",
  "SkipOnFailure": false
}
```

The **Apply presets** action sets `Argument=""` and `IsClientSide=false`.
The flow is paste JSON → Apply presets while stopped → restart the AMP
instance using its ordinary controls → reconnect to the panel → select a preset
→ Start. Restarting AMP
reloads the static manifest and briefly disconnects the panel. A live
2.8.0.10 test confirmed that the managed control container was recreated, the
panel returned in about 33 seconds, and the new custom preset appeared.
`GenericModule/ReloadGenericConfig` and settings-cache refresh did not reload
changed enum definitions in that test.
Legacy installations with an empty updates manifest cannot apply presets.

This explicit helper validates deployment/image identity and the stopped state,
uses the same setup/profile locks, applies the editor, and regenerates enum
choices from enabled IDs in the actual presets file. It never reads canonical
account input, hashes a password, starts the runtime, or calls AMP. AMP Start
also regenerates choices after accepting editor changes. An unchanged stale
editor cannot cause the dropdown to ignore newer File Manager IDs.

When installed in AMP, the helper updates only the map selector's `EnumValues`
in `../war3-productionconfig.json`, the known template filename from
`Meta.ConfigManifest`. It also refreshes an existing `../configmanifest.json`
compatibility alias. Both names are fixed, and both files are validated before
writing; other fields are preserved and failed preflight restores both files.
Without either live file, the helper updates the prepared
`control/preset-configmanifest.json`; installation must put that manifest under
the configured name in the AMP datastore root. Symlinked or foreign manifests
reject the operation. The helper never reads `GenericModule.kvp` to obtain
arbitrary paths.

For a new custom preset: save its definitions, apply while stopped, restart the
AMP instance, reconnect, select the new ID, then Start.
If inline actions are unavailable, use AMP's ordinary
**Update** button to execute the local stage, restart the AMP control instance
with the runtime stopped, reopen configuration, select the new preset, then
Start. This fallback uses existing AMP controls and requires no plugin or
additional server. Official CubeCoders templates serve as working-pattern
references; no legacy-version rework is part of this change.
This helper has no AMP authentication dependency
and does not invoke those APIs. `ToolDiscovery` discovers binaries and is not
used as a JSON command source. `list-presets --instance-dir .` is a read-only
diagnostic that emits an ID-to-label JSON object from the actual presets file.

The source tests check the action contract, literal synthetic definitions, the wrapper
string and actual output object, and generated selector values. They do not
independently render the live UI or execute the restart API.

Routine errors expose whitelisted fixed messages for incomplete account pairs,
invalid preset JSON/IDs, and editor/File Manager conflicts. Unknown exception
payloads, subprocess output, input values and tracebacks remain suppressed.

## Operator console and stock player Analytics

Stock Generic player tracking accepts the named capture `username` on join and
leave events; `userid` is optional. The installed Generic module constructs a
user with a UTC join time and emits its normal user join/leave events. The
[official Generic console contract](https://github.com/CubeCoders/AMP/wiki/Configuring-the-%27Generic%27-AMP-module#console)
and the pinned
[ASKA template](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/aska.kvp)
use these hooks. The template feeds stock current-player/session tracking; it
adds no Analytics service, plugin, or fabricated player identities. Use the
instance's Analytics view to review human connections and session durations.

The installed AMP 2.8 instance provides existing methods
`AnalyticsPlugin/GetAnalyticsSummary`, `AnalyticsPlugin/GetUserDetails`, and
`Core/GetUserList`. Verify the installed instance's actual join/leave tracking
when upgrading. This template invokes none of these
methods and requires no additional service or plugin.

The coordinated runtime operator logger emits these exact UTC prose lines:

```text
2026-10-30 20:00:01 INFO native: Player joined: ActualName
2026-10-30 20:05:01 INFO native: Player left: ActualName
```

Only accepted native peer joins create tracked names. The logger deduplicates
exact safe names, emits leave only for known joins, and retires them once on
native exit, recovery or lobby-generation retirement. Aggregate human/computer
counts never create named Analytics users; AI slots, virtual hosts, chat and
raw native lines do not match the AMP event regexes. The original native IP
and deletion reason are absent from these console events.

The template and runtime console format must match. Native output must frame or escape
untrusted embedded CR/LF/NUL so an injected physical line cannot impersonate a
join marker. The native `CONSOLE_Print` implementation escapes these characters;
the logger regression covers escaped chat. Native Local chat
is rendered only as `Lobby message: ...`, never a named player/readiness event.
Anchored regex matching alone does not
authenticate an event's origin or independently prove that prerequisite.

Names remain verbatim: the logger requires 1–64 UTF-8 bytes, printable text,
no surrounding whitespace, none of `[]|<>"` or backslash/replacement characters,
and no change under existing sensitive-data sanitization. Names that fail are
omitted, never collapsed into a shared redacted identity. The AMP regex captures
a bounded superset; byte limits, privacy filtering and roster reconciliation
remain the logger's responsibility. `Console.UserActions` stays empty because
there is no supported native kick/ban command contract. Such administrative
buttons are not required for user/session events.

AMP startup readiness matches only the supervisor's accepted-state messages:

```text
2026-10-30 20:00:01 INFO supervisor: Lobby is ready.
2026-10-30 20:00:02 INFO supervisor: Countdown is running.
2026-10-30 20:00:03 INFO supervisor: Game is running.
```

Preliminary native lobby-open/rehost messages and raw `HOSTBOT_STATE` or
`HOSTBOT_REFRESH` diagnostics are not readiness proof. AMP Ready is a startup
milestone; continuous authenticated/current-native-PID health remains in the
runtime status path. These regexes require the coordinated operator-log runtime
format and must be integrated with that runtime change. The source tests use
synthetic console fixtures and do not establish live Analytics acceptance.

## Offline verification

Run `test_account_setup.py` and `infra_test.py` inside the accepted RC4 image
`sha256:b814ad9c299262a381eaa7bb18bafd0d73d6b314a0ae9394b18f3ef5f1a6b54f`,
with network disabled, a read-only root and checkout, and UID/GID `1000:1000`.
The source-actor tests stub Docker completely and exercise real private-file
permissions, file locks, atomic directory exchange, rollback, first setup,
legacy fallback, image gates, stopped-state guards, redaction, and mount scopes.
Transactions run as the actual non-root test owner without ownership policy
patches, chown, or privilege changes. Independent command/owner checks simulate
UID/GID 1000, 1001, and other deployment owners and reject root/mismatches.
These tests do not connect to AMP,
read real credentials/caches, deploy, or test a real login.

One optional compatibility test uses the accepted image's actual formatter and
libbncsutil with synthetic credentials and a read-only fixture containing exactly
`SYNTHETIC_NOT_PE_ACCOUNT_TEST` plus a newline. Mount this operator-created fixture
as `/fixtures/star/Launcher.exe`, and set
`WAR3_SYNTHETIC_LAUNCHER=/fixtures/star/Launcher.exe` in the **test** container.
Only the public Launcher identity check is substituted for this synthetic
fixture; the formatter/library and existing relay parser run normally. The test
creates a fresh empty private output directory and never mounts an old profile.
This checks formatting/relay compatibility, not acceptance of a real Launcher or
real account authentication.
