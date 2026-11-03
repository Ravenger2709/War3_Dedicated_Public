# Native cycle chat status

`!status` retains the existing per-game map, AI and player-count information.
It adds the querying game's catalog preset, the process's current cycle lobby
preset (`none` when there is no lobby), the next preset after that lobby or the
latest active match, and the actual native running/max match counts. No player
names, request IDs, generations or other process metadata are added to chat.

The current preset and counts come from native objects. The next position
cannot be inferred from a deduplicated catalog ID: `one, two, one` has two
occurrences of `one` with different successors. The supervisor may supply the
exact occurrence using optional `native-feedback.cfg.cycle`, alongside the
unchanged numeric realm feedback file. The private sidecar has exactly these
fields (one `key = value` per line):

- `version = 1`
- `observed_monotonic_ms`: the original owned native observation time, never the time of a subsequent feedback refresh
- `ttl_ms`: 1..10000, from the configured metadata TTL
- `session_generation`: 32 lowercase hex characters
- `manifest_sha256`: 64 lowercase hex characters
- `request_id`: 32 lowercase hex characters
- `host_counter`: 1..2^28-1
- `catalog_index`: 0..31
- `cycle_position`: 0..31, checked against the native manifest cycle

Native compares the generation, digest, request, counter and catalog index to
its actual current lobby, or its latest still-active game when no lobby exists.
Only then does it resolve the successor of the supplied occurrence through its
own validated cycle. The supervisor emits a lobby occurrence only for its
matching durable reservation; an active occurrence needs the completed request
and corresponding committed last start. A staged ACK is insufficient.

Missing, malformed, stale, future, unsafe or mismatched metadata produces
`Next preset: unavailable`. An explicit unavailable record contains only
`version = 1` and `available = 0`. The sidecar is bounded to 4096 bytes, must be
an owned regular mode-0600 file with one link in a private mode-0700 directory,
and rejects symlinks, duplicate/unknown fields and directory replacement during
read. It is display-only: validation failures and write failures neither grant
nor revoke gameplay permissions. If a write cannot invalidate an older valid
file, its original observation TTL still bounds its use.
