# Map analysis and operator presets

AMP File Manager already opens at the `war3-production` instance root, with
`data`, `control` and `vendor` directories. Edit `data/config/presets.json` there.
The host-relative path is `war3-production/data/config/presets.json`; the
container path is `/data/config/presets.json`.
Copy `config/presets.example.json` there once, edit it, and select its `id` with
`gameplay.map_preset`. The default is `manual`. Restart the runtime after editing.
The companion `config/presets.schema.json` helps editors; runtime validation also
checks UTF-8 byte lengths, duplicate keys/IDs, hashes, capacity and thresholds.
No account, deployment or AMP settings are changed by this Python layer.

`presets.json` is always beside the operator's native config; maps live in the
sibling `maps` directory (`/data/maps` with the production layout). Neither path
is configurable through a preset. Map names must be single `.w3x`/`.w3m` basenames.
Symlinks, traversal and config injection are rejected. The JSON can be normally
human-readable; it is non-secret and does not require private-file permissions.

## Fields and selection

The top-level fields are `version: 1`, `presets` (1–32 entries) and optional
`capabilities` (at most 32 approval records). Unknown fields and duplicate JSON
keys are errors, including within disabled templates.

In AMP's **Preset definitions JSON** editor, `capabilities` may be omitted:
applying the editor preserves the existing approval records in
`data/config/presets.json`. Keep the preset's `ai.capability` reference to the
matching record. The records and their map/script hash checks are still required
for AI, and a different map must be reviewed before receiving its own approval.
When editing the file directly, retain the `capabilities` records there.

Stop the application before using **Apply presets**. Applying updates the file
and selector; it does not change the running lobby. Restart AMP if selector
choices changed, select the desired ID, then start the application. If AMP
reports that a War3 container already exists although the instance is stopped,
inspect and stop the owned container before starting; do not remove an unknown
container by name.

Each normal preset has:

| Field | Meaning |
| --- | --- |
| `id` | Selection key: 1–32 lowercase letters/digits/underscore/hyphen; starts with a letter/digit. |
| `enabled` | Disabled entries cannot be selected. |
| `map_filename` | Exact basename of the uploaded map in `/data/maps`. Required. |
| `lobby_name` | Exact lobby title, 1–24 UTF-8 bytes. No leading/trailing space, controls, `#`, `=`, brackets, backslash or Warcraft color markup. |
| `max_players` | Optional native population cap, 2–12 and no greater than the map's playable capacity; defaults to the analyzed map capacity. |
| `start` | Optional `autostart_enabled` (boolean, default true), `autostart_players` (2–12 humans), `countdown_seconds` (0–60). |
| `vote` | Optional `enabled`, `required_percent` (1–100), `min_players` (1–12), `timeout` (5–600), `cooldown` (0–600). |
| `ai` | Optional `enabled`, `target` (2–12, default 6, bounded by map capacity and `max_players`), `skill` (0 easy, 1 normal, 2 insane), `capability` (approval-record ID). |

### AI difficulty per preset

Set `ai.skill` independently in each preset:

| Value | Warcraft III computer difficulty |
|---|---|
| `0` | Easy |
| `1` | Normal (default) |
| `2` | Insane |

For example, change the existing Footman preset's AI object to:

```json
"ai": {"enabled": true, "target": 6, "skill": 2, "capability": "footman-v11-static"}
```

In AMP use **War3_Dedicated → Map presets → Preset definitions JSON**, or edit
`data/config/presets.json` through File Manager. Keep the preset's other fields
and its matching approval record. Stop the bot before applying changes, then
Start/Restart. The selected difficulty also applies to computers restored after
a human leaves the lobby. Computer rows already defined by the map retain their
original settings. AI support and hash checks remain required at every level.

Native integration tests inspect the actual W3GS computer-slot skill byte for
all three levels and verify that ownership survives joins and slot operations.

Omitting `lobby_name` uses the map's W3I title, resolved through WTS where needed,
with color markup removed and shortened to 24 UTF-8 bytes. An explicit
`lobby_name` is used verbatim. `Footman Ultimate AI` in the example is the chosen
short title. Python appends no number or suffix; native naming must honor this
same title. The runtime host display name defaults to `War3HostBot`.

`manual` is built in and works without a presets file. It preserves the operator's
`bot_defaultmap`/`selected.cfg` selection and forces AI off, overriding AI keys in
the operator config. The optional manual JSON entry contains only
`{"id":"manual","enabled":true}`.

Each selected preset owns automatic countdown through `start.autostart_enabled`
and `start.autostart_players`. The enable flag defaults to true even when absent
from an older preset. False disables only automatic countdown; `!vote` remains
independent and follows the preset's vote settings. Autostart thresholds remain
validated at 2–12 even when disabled, but need fit `max_players` only when
autostart is enabled. The vote minimum always must fit `max_players`. The native
config always emits `bot_autostart_enabled = 0` or `1` separately from the bounded
`bot_autostartplayers` and `autohost_startplayers`; it never uses threshold zero
to disable autostart. Raw `gameplay.autostart_enabled` also defaults to true and
is the compatibility setting for manual mode. Footman's example enables
autostart; the disabled generic AI template sets its autostart flag false.

Map capacity counts playable human **and predefined computer** rows, not neutral,
rescuable or observer roles. An omitted maximum never inherits a global limit.
For non-AI presets, native's start limit counts occupied humans and predefined
computers together: those computers retain their rows and consume capacity,
and only the remaining human slots stay open. Approved AI presets follow native's
separate human admission limit, bounded by available human rows; their total
occupancy still cannot exceed the map. Existing computers alone do not approve AI.
Omitted autostart thresholds are clamped to the reachable human limit (default
six for approved AI, otherwise the legacy gameplay threshold, normally ten).
An explicit enabled threshold must fit that human limit; an explicit disabled
threshold may remain above it within 2–12. The vote minimum must always fit it.

Preset resolution happens before effective configuration is written. A selected
preset generates private `/data/state/native/mapcfg/preset.cfg` and
`/data/state/native/ghost.cfg`; it never edits the read-only operator config,
map configs or MPQ. Non-AI presets close excess human slots only, preserving
existing computer slots, colours, teams and races. Neutral/rescuable slots are
not converted into humans or AI. Observers are disabled in generated map configs.
Native CMap validation still applies; this layer does not bypass its team, map
or slot checks.

## AI evidence and native contract

AI is off by default. Neither `AI` in a filename/title nor an unused `.ai` member
enables computers. Static analysis strips comments and string/rawcode literals
before identifying these features:

- `ai_start`: an actual JASS `call StartMeleeAI(...)` or `StartCampaignAI(...)`.
- `computer_control`: computer-controller checks plus order calls and trigger
  action registration in the actual map script.

`none` means neither feature nor computer-controller evidence was found.
`detected` means controller evidence only. `candidate` means script features were
found, including potentially unused functions. These levels never grant AI.
Arbitrary JASS reachability and runtime correctness cannot be proved statically.

AI compilation additionally requires an explicit `approved_preset` record with
the exact whole-MPQ SHA-256, exact extracted script SHA-256, a nonempty subset of
detected features, review text, and a `runtime_tested` boolean. These are curated
operator approvals, not self-authenticating facts. Editing the approval list is
a trust decision. Matching hashes are checked again on every preparation; an
unknown or changed map/script or an absent feature rejects selection. Copying a
hash into an approval without reviewing the script is not verification.
`runtime_tested: false` permits a reviewed static approval but explicitly records
that real-client testing remains pending. No approved capability is bundled in
Python source, and analysis never writes an approval automatically.

The only path to `vote.min_players: 1` is successful compilation of an AI preset.
Raw `gameplay.vote_min_players` remains bounded to 2–12; no raw minimum-human or
AI-support settings are accepted. AI autostart defaults to **six real humans**;
it never defaults to one. Other vote/start settings inherit gameplay unless the
preset overrides them. The vote minimum and enabled autostart threshold must fit
`max_players`.

| Native key | Manual/non-AI | Approved AI preset |
| --- | --- | --- |
| `bot_lobby_ai_supported` | `0` | `1` |
| `bot_lobby_ai_target` | `0` | preset `ai.target`, 2–12, default `6` |
| `bot_lobby_ai_skill` | `1` | `0`–`2`, default `1` |
| `bot_lobby_min_humans` | `2` | `1` |
| `bot_vote_min_players` | validated gameplay/preset value, at least `2` | `1` only if explicitly selected in preset |

The native implementation owns population changes: computers are
`max(0, target - real_humans)`; an empty lobby has `target` computers, each joining
human replaces one and a lobby departure restores one until the target is reached.
Footman defaults to target four (one computer per clan in the empty lobby),
auto-start at four real humans, and voting from two real humans. These values
belong to the preset's `ai.target`, `start.autostart_players`, and
`vote.min_players`; they can be changed independently for each preset.
Other approved presets may choose 2–12 within their map's
human-slot capacity and configured human limit.
Human capacity may remain twelve. Native must require the support flag, respect
map team/capacity constraints and protect reserved, observer and unrelated
computer slots. Counters used by publication remain internal. This Python change
does not implement or claim verification of that native behavior.

## Runtime AI telemetry

Native `[HOSTBOT_STATE]` version 1 may additionally report the following fields.
The runtime preserves them in `/status` under `game`, alongside `players`, which
continues to count real humans separately.

| `/status` field | Type and range | Meaning |
| --- | --- | --- |
| `game.computer_players` | integer 0–12 | Actual computer participants observed by native. |
| `game.ai_target` | integer 0–12 | Native's reported AI population target; zero when inactive or absent. |
| `game.ai_supported` | boolean | Native's reported AI support flag. |

An older state line that omits these fields produces `0`, `0`, and `false`.
Booleans/floats/strings are rejected as counts, and non-booleans are rejected as
the support flag. Rejected or stale-generation lines do not replace the last
valid observation. Counts are preserved as reported, not calculated from the
target: unrelated map computers and native slot protections remain native's
responsibility.

For a target-six lobby, expected observations are humans/computers `0/6`, `1/5`
after a join, and `0/6` after that human leaves the lobby. The Python tests feed
synthetic native observations through the production parser and actual `/status`
HTTP endpoint to verify forwarding and rejection. These tests do not establish
that a running native lobby performs the replacements; native/client integration
must verify that separately.

## Supplied Footman candidate

The example includes a static approval of the supplied
`Footman-Frenzy-Ultimate-AI-v1.1.w3x`, uploaded separately by the operator:

- Whole map SHA-256:
  `71f4600ddb293b55bfe02ac301d7efec5f868a4eef4031bfa8dfbd0ce99ab58e`.
- `scripts\war3map.j` SHA-256:
  `6781df1bb34e0f177f53c4b50580ae98fcfaa8445381d8a3b30b70fa80bb3800`.
- Classic W3I v25, 12 human slots, four teams of three; metadata title is
  `Footman Frenzy Lvl 10000 Ultimate AI`.
- Controller predicates gate the `OK` action's per-player `StartMeleeAI` calls;
  it is registered with `TriggerAddAction(PX,function OK)`. Eight referenced
  `war3mapImported\The*Clan.ai` resources were readable and contain `main`.
  Computer-specific triggers also issue upgrade/unit orders.
- No `Blz*`/`Dz*` API calls were found in the extracted map script. This limited
  namespace check does not certify every native's version compatibility.

This is a family-specific static capability approval, not proof of Warcraft
1.26 execution or gameplay quality. Real-client testing is still required for
one-human start, all AI difficulties, join/leave replacement, slot protection,
team behavior and autostart. The generic `ai-template` remains disabled and has
AI off until separate evidence is supplied.

## Analyzer bounds and verification

Run read-only analysis in the runtime image (without starting its entrypoint):

```sh
python3 -m src.runtime.presets /data/maps/YourMap.w3x
```

Only classic W3I v18/v25 and JASS are supported. Newer formats, missing metadata
or scripts, unresolved titles, conflicting root/`scripts\war3map.j` scripts,
invalid player records and truncated data fail closed. W3I optimizers' unused
high force-mask bits are ignored just as native CMap uses colours 0–11.

Limits: JSON 64 KiB; MPQ 128 MiB; W3I 4 MiB; each possible JASS member 4 MiB;
WTS 2 MiB; metadata strings 8 KiB. StormLib runs in a subprocess with 10-second
wall timeout, 5-second CPU limit, 256-MiB address-space limit, no core dumps or
file output. It reads fixed member names and never runs map code. Both public C
and the existing image's older C++ StormLib exports are supported. Library
absence or extraction failure rejects selection. A changing map during analysis
is rejected; operators must also keep map bytes unchanged while native runs.

`tests/phase3/test_map_presets.py` builds actual tiny MPQs with the existing
StormLib and verifies rejection and compilation. `WAR3_STORM_LIBRARY` optionally
selects a library for tests; `WAR3_TEST_MAP` enables the supplied-map hash check.
Tests were run in image `b814ad9c2992…` with UID 1000, read-only root/source/map
mounts, no network, dropped capabilities and temporary storage only. No engine,
real account, AMP instance, deployment or registry is used.

An additional read-only check compiled the integrated repository's Footman preset
using its Python source at `e3c00b3` and the existing image's StormLib. Both hashes
above and both AI features matched. The effective config contained the exact
`Footman Ultimate AI` title, twelve-human limit, six-human autostart, vote minimum
one and AI target/support/skill/minimum-humans `6/1/1/1`. The generated map config
preserved all twelve slots and their four teams. Operator files were unchanged.
This validates static analysis and configuration compilation only;
`runtime_tested` remains false and no Warcraft engine or gameplay was executed.
# Automatic discovery on start

When AMP or the standalone CLI starts the instance, the controller examines
new `.w3x` and `.w3m` files directly under `data/maps` (including files uploaded
through AMP File Manager). It uses the accepted image's static map analyzer in
a temporary network-isolated container; map scripts and Warcraft are not run.
Valid maps get a basic entry in `data/config/presets.json`, with the title and
slot count read from the map, AI disabled and auto-start disabled. Select and
edit the entry to configure the map. AI requires the existing explicit,
hash-bound capability approval; detection alone does not grant it.

An existing entry for the same filename is never replaced, even if the map file
changes. A second filename with identical contents is also skipped when its
existing map file is available. Invalid or changing maps are skipped, and a
short count is logged. A maximum of 32 presets and 32 new candidates per scan
is enforced; remove invalid files if they prevent later files from being scanned.
No subdirectories are scanned. A repeated start does not create another preset.
The current selection does not change automatically. The selector manifest is
refreshed at start; AMP may need its configuration view reloaded before new
choices appear. The runtime mounts config read-only, while the stopped host-side
controller performs this update before starting the container.

The preset update checks the File Manager revision and uses the existing
transaction and recovery mechanism. If a concurrent edit is detected, startup
fails closed and retains recovery data in `control/preset-conflict` or a
`control/war3-preset-*` directory. Resolve that conflict before retrying. Do not
edit the preset file while the instance is starting.

Existing installations have a copied controller and must be updated explicitly
from the same trusted checkout while the instance is stopped, as the instance
owner. `INSTANCE` is the instance's application directory (the directory
containing `control` and `data`):

```sh
INSTANCE=/path/to/instance
install -m 600 amp/production.py amp/map_discovery.py "$INSTANCE/control/"
```

Then start the instance normally. The `production-launch.py` and `account.py`
already installed are retained; the accepted image needs the existing
`src.runtime.presets` analyzer. For standalone installations, use the updated
checkout's `scripts/war3.py`. Updating these controller files does not change the
runtime image or its acceptance receipt.
