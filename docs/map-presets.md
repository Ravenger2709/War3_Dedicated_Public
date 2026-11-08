# Map presets and lobby rotation

Use two settings:

- **Preset definitions JSON** describes each map's lobby and gameplay settings.
- **Lobby rotation (preset IDs JSON)** lists their IDs in the order to host.

There is no separate **Map preset** selector in the current AMP template.

## A readable preset

```json
{
  "version": 1,
  "presets": [
    {
      "id": "my-map",
      "enabled": true,
      "map_filename": "My-Map.w3x",
      "lobby_name": "My Map",
      "start": {"autostart_enabled": true, "autostart_players": 4, "countdown_seconds": 5},
      "vote": {"enabled": true, "min_players": 2, "required_percent": 60},
      "ai": {"enabled": false, "target": 6, "skill": 1}
    }
  ]
}
```

The operator document contains **version** and **presets**. It does not need
`capabilities`, `capability`, `review`, `runtime_tested`, `map_sha256` or
`script_sha256`. Those were technical static-analysis records, not gameplay
settings. Existing controller-backed records are removed when presets are
applied; the runtime analyzes the actual selected map instead. Unusual legacy
starter-only approvals retain their original compatibility format.

Upload maps to `data/maps` and edit `data/config/presets.json` in AMP File Manager.
The AMP editor edits the same preset definitions. Short `start`, `vote` and `ai`
objects stay on one line when saved. Conflicting edits from the editor and File
Manager are retained and reported instead of silently replacing one another.

## Rotation

For example:

```json
["my-map", "another-map", "my-map"]
```

The first lobby uses `my-map`. When its match starts, the next lobby uses
`another-map`, while the first match can continue. The sequence repeats. Duplicate
IDs are allowed. A one-item list repeatedly hosts one map. Empty or disabled
presets and missing maps are rejected; the host does not silently select a
replacement map.

**Max concurrent matches** limits loading/running matches, from 1 to 5, default
3. When full, the host waits for a match to finish before opening another lobby.
An empty lobby stays on its current map until a match starts.

Save settings and restart the **game instance** to apply them. The panel does
not need to be rewritten. Updating an old template once may require reloading
that instance's AMP management process so it discovers the replacement field.
Later rotation edits use the existing field.

Standalone Docker uses the same runtime and presets. With `scripts/war3.py`, put
`"map_cycle": ["my-map", "another-map"]` into `data/config/amp-settings.json`
(the filename is shared with AMP; AMP itself is not required), then restart with
the CLI. For a direct runtime configuration, set the array under
`gameplay.map_cycle` in `data/config/supervisor.json`. Legacy `map_preset` values
are accepted only as a migration input for a one-item cycle.

## Preset fields

| Field | Meaning |
|---|---|
| `id` | Stable rotation ID, up to 32 lowercase letters, digits, underscores or hyphens. |
| `enabled` | Whether this preset can be hosted. |
| `map_filename` | Exact `.w3x`/`.w3m` filename from `data/maps`, without directories. |
| `lobby_name` | Optional lobby title, up to 24 UTF-8 bytes, without counters or `#`. Defaults to the map's title. |
| `max_players` | Optional population cap. Omit to use the map's playable capacity. |
| `start.autostart_enabled` | `true`/`false`; enables automatic countdown. |
| `start.autostart_players` | Required human players for auto-start. |
| `start.countdown_seconds` | Countdown duration. |
| `vote.enabled` | Whether players can use `!vote` to start early. |
| `vote.min_players` | Minimum humans present before start voting is allowed. |
| `vote.required_percent` | Percentage of human yes votes required. |
| `vote.timeout` / `vote.cooldown` | Optional vote duration and cooldown in seconds. |
| `ai.enabled` | Explicitly enable computer slots for a map with detected AI support. |
| `ai.target` | Initial total computer population, including predefined computers; 2–12, bounded by map capacity. |
| `ai.skill` | `0` Easy, `1` Normal, `2` Insane. |

The managed computer count is `max(0, target - human_players - fixed_computers)`,
bounded by available slots and reservations. A human joining replaces one owned
computer; a human leaving the lobby restores it. Computer rows defined by the
map remain fixed and count towards the total target. Computers do not count towards human
vote or auto-start requirements. Their difficulty follows the preset.

Use booleans `true` and `false` in JSON. AMP's yes/no presentation does not change
the stored boolean type. Unknown keys, duplicate IDs/JSON keys, impossible
thresholds, invalid names and path traversal are rejected before launch.

## AI checks

The runtime reads the selected map's actual script, including sealed map inputs
used for multi-match hosting. Enabling computers requires computer-controller
checks together with trigger/order syntax. A title containing “AI”, a bundled
unused `.ai` file, or a starter call alone is insufficient. Map data and native
catalog hashes still protect the actual files being hosted; operators do not
copy or maintain those hashes in presets.

Static analysis does not prove gameplay behavior. Some maps require an in-game
command such as `-ai`; follow that map's instructions and test it with a real
client. Maps without detectable support remain AI-off or reject an explicit AI
request. The runtime never labels a static result as a successful gameplay test.

Legacy inline approval records remain parseable and retain strict hash,
feature and ambiguity checks. The source's `src/runtime/curated_map_presets.json`
is an internal bootstrap fixture for exact known-map defaults; it is not the
operator's editable preset file.

## Adding and deleting maps

The shared AMP/standalone start helper scans `data/maps`. New maps receive a
base preset with AI and automatic countdown off. Existing presets keep their
operator settings and IDs. Existing maps are not repeatedly launched through an
analyzer merely to regenerate bookkeeping. The runtime validates selected maps
when preparing them.

Removing a map removes its presets on refresh. If a removed preset is in the
rotation, correct the rotation before starting; the host reports the missing ID
rather than falling back to another map. Internal `selected.w3x` bootstrap copies
and identical map content do not produce duplicate presets.

## Player messages

The default English MOTD explains `!help`, `!status`, `!vote` and `!kick <player>`.
An explicit `data/config/motd.txt` overrides it; up to eight lines, 200 UTF-8 bytes
per line. Empty custom text disables the MOTD.

**Lobby message interval (seconds)** controls recurring auto-start, vote and help
hints, default 20 seconds. Vote hints follow auto-start notices by about 3.5
seconds; help follows separately. Counts are recalculated when sent. Command
replies remain immediate; this interval does not change empty-slot refresh.
See [kick voting](kick-voting.md) and [message interval](lobby-message-interval.md).

## Modern W3I inspection and classic-client compatibility

The metadata reader supports W3I 18, 25 and 33. From updated source, with
the StormLib runtime available, inspect a map without reading or executing
its JASS/Lua script:

```sh
python3 -m src.runtime.presets --info-only /path/to/map.w3x
```

W3I 33 includes the saved game version, script language, additional camera
settings and enemy-priority fields for players. Inspection supports modern
player colours up to 23, while classic hosting remains limited to 12 slots.
Modern results include `classic_compatible: false`. They are rejected by both
normal and sealed-input named-preset compilation for the current Warcraft III 1.26a
runtime. Reading a new header does not supply newer client native functions or
convert the map to an older game version. The normal script extraction limits
and AI approval requirements remain unchanged.

Format reference: [WC3MapSpecification, W3I 0–33](https://github.com/ChiefOfGxBxL/WC3MapSpecification/blob/master/Info/0-33.md).
Sunken City v2.5.2a is a modern-map example: its own header records Warcraft
2.0.4.23745, and its script uses newer `Blz` UI/gameplay APIs. Its author's
[release page](https://www.hiveworkshop.com/threads/sunken-city-v2-5-2a.168086/)
recommends Warcraft III 2.00. Use a compatible legacy release for iCCup 1.26a.
