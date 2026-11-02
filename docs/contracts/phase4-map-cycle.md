# Phase 4: map cycle validation contract

The validation functions remain read-only data APIs. In integration through
`48b85e9`, public explicit `gameplay.map_cycle` configuration now reaches the
ordinary `RuntimeBackend`/`ICCupSupervisor` catalog startup path; the functions
themselves do not activate rotation or migrate a live instance. Operator startup
also invokes the accepted image's full-cycle check for multi-entry candidates.
This describes source integration and offline evidence, not a new accepted
release, live/real-client validation or overall GREEN.

The user-facing JSON value is an array, for example `["one", "two", "one"]`.
It contains 1–32 ordered preset IDs. Repeated IDs are intentional: each occurrence
consumes a position and survives validation and migration. IDs follow the
existing preset grammar `[a-z0-9][a-z0-9_-]{0,31}`. Other than `manual`, each ID
must reference an enabled preset. `manual` is built in if absent from the preset
file; if present, its entry must be enabled. `load_presets` rejects an explicit
disabled manual entry even when a cycle does not select it. Empty or oversized
arrays, invalid IDs, unavailable presets and malformed JSON fail closed.

Integration API in `src.runtime.map_cycle`:

- `parse_cycle(data)` decodes at most 64 KiB of JSON text or bytes, rejecting
  duplicate object keys and non-JSON numeric constants. It only parses; call
  validation on the result.
- `validate_cycle(cycle, presets)` validates shape and availability against an
  already validated ID-to-entry mapping such as returned by `load_presets`.
  It returns a new list; it does not check maps or AI.
- `migrate_cycle(gameplay, presets)` validates `map_cycle` if present; otherwise
  it returns a single-element array from legacy `map_preset`, defaulting to
  `manual` when absent. Invalid explicit values fail closed. An explicit cycle
  takes precedence over a retained legacy selection. Inputs are not mutated.
- `validate_resolved_cycle(cycle, source, gameplay)` reads the `presets.json`
  adjacent to the native configuration path `source`, validates the cycle,
  and invokes the existing `resolve_preset` for every occurrence in order,
  passing a copy of gameplay with that occurrence as `map_preset` and removing
  `map_cycle` and process-wide `max_concurrent_matches` from that per-preset
  input. The gameplay parser now accepts typed cycles; this removal keeps
  occurrence resolution independent of the process-wide cycle. Non-manual
  entries therefore undergo current map analysis, capacity, gameplay and AI
  approval checks. The existing resolver's manual behavior applies: it validates
  gameplay and turns AI off, without analyzing an operator-selected map. This
  function loads a present preset file even for a manual-only cycle so invalid
  or explicitly disabled manual entries cannot be bypassed; an absent file is
  permitted for manual. Errors from loading and resolution propagate. It returns
  the validated array, not the resolver's generated configuration. This is the
  legacy-compatible interface; it does not check the manual map.
- `validate_full_cycle(cycle, source, gameplay)` performs the above resolution
  and, for each `manual` occurrence, calls `validate_manual_map`. Pass the
  actual operator `ghost.cfg` as `source`: it must be in `data/config`, next
  to `presets.json`. `bot_defaultmap` must be a plain `.cfg` basename under
  `data/config/maps`; `map_localpath` must be a plain `.w3x`/`.w3m` basename
  under `data/maps`. Configuration files must be bounded regular UTF-8 files
  with unambiguous keys. The strict mapcfg accepts only `map_path`,
  `map_localpath`, `map_speed`, `map_visibility`, `map_observers`, `map_flags`,
  `map_loadInGame`, and `map_loadingame`; conflicting aliases, custom slot and
  other fields fail closed. It checks the
  actual map with `analyze_map`, validates gameplay, requires `max_players` to
  fit map slots, and requires vote and enabled autostart thresholds to fit human
  slots after predefined computers consume the population limit. The check
  never changes config, map or slot data. Manual AI remains off under the
  existing resolver. This stricter API deliberately rejects some legacy
  configurations (including custom mapcfg slots and symlinks).

Validation and migration do not write files. Resolution reads local preset and
map files and can be costly, including repeated analysis for repeated IDs. The
caller must handle errors, prevent concurrent changes or revalidate at use time,
choose storage and rollout, and retain/recover the original configuration
transactionally. Rotation, cursor persistence and advance timing remain outside
these validation functions. `gameplay_config` accepts a typed list and
`load_config` accepts public multi-entry cycles. Explicit cycles, including a
single entry, select the leased catalog path; legacy `map_preset`-only input
retains its prior path. The match limit defaults to 3 and is bounded to 1–5.

The ordinary backend first calls `prepare_preload_cycle` before account-profile
access or launch ownership (`8bbfe86`), then validates and seals again for the
final launch snapshot. This separate preload path resolves distinct presets
against a preset-document snapshot and sealed map bytes, while preserving every
cycle occurrence in the manifest. Automatic startup binds the native default
to the first sealed cycle preset's size, settings and SHA-256. An early successful
check does not freeze File Manager changes: a map edit before final preparation
can still fail closed after claim creation, leaving that claim for inspection.
Unknown-crash ambiguity and interrupted pending state remain blocked; these
validation functions do not implement recovery.

Config reads open each path component with `O_NOFOLLOW`. The existing map
analyzer checks paths for symlinks and checks for map changes during analysis,
but does not pin the complete path across its subprocess and later native use.
The caller must prevent concurrent replacement of path components and files, and
revalidate immediately before use. Map metadata capacity does not certify
native CMap acceptance, client compatibility, or actual gameplay. The reported
ordinary-backend offline E2E passed in 91.158 seconds with synthetic maps/W3GS
peers, public config and TCP 6113; its realm was disabled. The separate
capacity-three harness passed in 43.454 seconds but retains a custom backend
and spawn override. See [startup evidence](../catalog-startup-integration.md)
for the precise boundaries. Neither result establishes engine, live iCCup or
real-client acceptance. No tests were rerun for this documentation update.
