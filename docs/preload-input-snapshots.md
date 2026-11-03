# Sealed startup map inputs: preparation stage only

`src.runtime.preload_inputs.pin_preload_inputs` prepares sealed Linux memfds for
an eventual native startup catalog. This is not wired into startup, does not
load `CMap`, prepare map settings, select a lobby, implement IPC or authenticate
any request. The source patch exposes a sealed-FD `CMap` constructor, but no
production startup catalog calls it yet.

The caller must first validate each map and bind its expected lowercase SHA-256
to the resolved preset. Supply a cycle of `MapInput(preset_id, absolute_path,
validated_sha256)`, the size of the default map, and a native maximum-game
count. The function opens all path components with `O_NOFOLLOW`, requires a
regular file of 1–128 MiB, and uses one opened inode per unique preset ID.
Repeated IDs must have exactly the same path and validated digest; conflicts
fail closed. At most 32 cycle entries are accepted. Results contain distinct
`PinnedMap` records and a tuple mapping each cycle occurrence to an index.

Before creating any memfd, the admission estimate must fit 512 MiB: 128 MiB
of explicit load-buffer reserve, two copies of the sum of unique input sizes
(sealed source plus future cache), two default-map copies, and the largest
possible map multiplied by `max_games + 3` for running, lobby and old/new
pending copies. This is a conservative **payload estimate**, not enforcement of
process RSS, allocator overhead, kernel cache, or StormLib internals. The
future native catalog must independently validate and enforce its own budget.

Source bytes are copied once, streamed through SHA-256, and compared with the
validated digest; size, inode, mtime and ctime are checked before and after.
The returned anonymous FDs have kernel seals against write, growth, shrink and
further seal changes. Modifying or replacing the original path afterwards does
not change the sealed bytes. This does not validate a map's MPQ contents or bind
its gameplay settings: the caller must do that before use, and native loading
must use only the verified, sealed FD, not reopen the original pathname.

The catalog owns its FDs and is a context manager. `pass_fds()` checks their
identity and seals before returning them, and its result can be passed to
`subprocess.Popen(pass_fds=...)`. Keep the catalog open until the child has
inherited or duplicated the FDs; then close it. Map FDs are marked close-on-exec
unless explicitly passed. Losing the owning process loses these anonymous
snapshots; there is no persistent request/acknowledgement or restart recovery.

## Native sealed-FD building block

`CMap(host, config, config_label, sealed_fd)` duplicates the supplied FD,
requires all four seals, reads with `pread` independently of the caller's file
offset, and opens StormLib against that same inode. It rejects negative,
unsealed, oversized or unreadable FDs and requires a nonempty `map_localpath`
in the config; it never falls back to reopening that pathname. The same MPQ
member bounds and valid-map checks apply as for ordinary native map loads.
The caller retains ownership of the input FD. The constructor reads synchronously
and must only be used before matches start; it does not itself compare SHA-256
or bind gameplay settings to the input. The eventual startup adapter must pass
the exact descriptors verified by `PinnedCatalog`, validate the full manifest
and settings, apply the native payload admission calculation before allocation,
and fail closed if any entry fails. The current integration does none of that.

The offline sealed-map test also starts two synthetic matches using two sealed
MPQs and confirms that match A keeps receiving W3GS actions after B starts. The
harness deliberately invokes the sealed-FD constructor through test-only stdin
instrumentation; it is not a startup catalog and does not prove selection avoids
rereading a preloaded object. No production stdin protocol is provided.

A second constructor, `CMap(host, config, label, fd, expected_sha256)`, also
checks the sealed bytes against a lowercase 64-character SHA-256 before opening
the MPQ. It streams the existing map data through the bundled portable SHA-256
implementation, without requiring Linux AF_ALG. Malformed digests, read failures
and mismatches invalidate the map. This is a
native building block, not startup wiring. A production catalog must use this
verified constructor rather than the earlier four-argument test-oriented form.

## ICCup supervisor FD handoff contract

`ICCupSupervisor` supports an optional backend method
`native_preload(generation) -> PinnedCatalog | None`. The built-in production
backend currently returns `None`: no startup catalog producer is wired in. A
future producer must create a fresh catalog for each native generation, close
any partially created resources if its method raises, and prepare a validated
manifest/config that `backend.command('native', generation)` references using
those exact descriptor numbers. It must not reuse a closed generation's FDs.

For native only, the supervisor validates the returned catalog and calls
`Popen(..., close_fds=True, pass_fds=catalog.pass_fds())` for `child_exec.py`.
The wrapper uses `execvp`; descriptors named in `pass_fds` remain open across
that exec. Relay and Star receive an empty `pass_fds`, so they do not inherit
the maps. The supervisor retains its catalog until the native process exits and
is reaped; command construction and `Popen` failures close it immediately. If
signalling or waiting fails while the child is still alive, the supervisor
retains both the child and catalog for a later stop attempt. A restart invokes
`native_preload` again and gets a fresh set of sealed snapshots.

This contract does not yet arrange the native startup manifest, a readiness
acknowledgement that maps were loaded, catalogue selection, IPC or recovery of
request identities. `OfflineProductionSupervisor` uses a separate launch path
and does not implement this optional handoff. Neither path currently enables
native preload in production.

## Prepared preset cycle

`src.runtime.preload_catalog.prepare_preload_cycle` is an optional preparation
API; it is **not** called by production startup. It reads one bounded
`presets.json` snapshot through no-symlink file descriptors, rejects invalid or
disabled IDs before opening map inputs, and resolves each distinct ID once.
It hashes and seals the corresponding map bytes, then runs the bounded MPQ
extractor in a subprocess against the sealed FD. Gameplay, AI approval,
map-config text and map SHA-256 are resolved from the same preset snapshot and
those sealed bytes. The result owns the pinned catalog, provides unique entries
and cycle indices, and closes all FDs on any error or context exit.

The preparation uses `gameplay.max_concurrent_matches` (default 3, valid 1–5)
in its payload admission estimate. Its supplied `default_map_bytes` remains a
caller assertion: a future native startup implementation must compare it and
the match limit with actual native values before allocating. The estimate does
not enforce RSS. Manual entries snapshot the operator `ghost.cfg` and selected
mapcfg, seal the selected map, and retain the allowed map settings; repeated
manual entries reuse one pinned input. An absent `presets.json` is permitted
for a manual-only cycle, while an invalid present file is rejected. The
prepared manual mapcfg requires a safe, nonempty native `map_path` and permits
only `map_localpath`, `map_path`, `map_speed`, `map_visibility`,
`map_observers`, `map_flags`, and loading-in-game. Legacy `map_loadInGame` is
canonicalized to `map_loadingame`; conflicting aliases, custom slots and other
fields fail closed. This is a restricted compatibility contract, not support
for every legacy mapcfg. No native catalogue installation, IPC, selection or
ACK is implemented by this API.

### Candidate sealed startup manifest

`prepare_preload_cycle` can be passed to `seal_startup_manifest` to produce a
`StartupBundle`: its `pass_fds()` contains the unique sealed map descriptors
followed by a sealed manifest descriptor. The manifest encodes a versioned
binary header, the declared match limit and default map size, unique preset
IDs, descriptor numbers, lengths, SHA-256 digests, names, validated settings,
map configuration, and cycle indices. It is bounded to 256 KiB. A successful
seal transfers ownership of the map descriptors to the bundle; on failure the
prepared catalog retains ownership. The supervisor must close the bundle after
the child exits and recreate it for each new child generation.

This format is an experimental producer and local parser only: there is no
native manifest consumer, production wiring, selection IPC or acknowledgement.
A future native consumer must independently check every byte, FD seal, size and
digest, match its actual configured concurrency and actual default-map size,
and reject the entire catalog on any mismatch. The encoded accounting is an
admission estimate, not actual consumption or an RSS limit; it is not evidence
that a process stays under 512 MiB. The manual preparation path is not wired
to production startup.


Native catalog IPC and v2 catalog-bound close are included in the source-level
merge. They are opt-in and remain disconnected from the production cycle pump;
`HOSTBOT_SESSION` is emitted only after catalog initialization succeeds. No
runtime launch, live integration or unconfirmed-exit recovery is enabled.
