# Empty lobby rotation

An empty public lobby advances to the next entry in `map_cycle` after ten
continuous minutes without human players. This is independent of vote-start,
auto-start and the empty-slot refresh interval. Running matches continue on the
same native process and keep their player connections.

In AMP, use **Empty lobby rotation timeout (minutes)** under **Map presets**.
The default is `10`; `0` disables idle rotation; the supported range is
`0`–`1440`. Save and apply on the next safe application Start/Restart. Restarting
an occupied application still disconnects its players, so wait until it is empty.
This setting does not require changes to the AMP panel itself.

Standalone installations use the same setting in
`data/config/amp-settings.json` (the shared operator settings file):

```json
{"empty_lobby_rotation_minutes": 10}
```

Merge this field into the existing settings; retain the other settings and map
definitions. Direct supervisor configurations use
`gameplay.empty_lobby_rotation_minutes`. The compiler converts minutes to native
`bot_empty_lobby_rotation_seconds`; do not maintain a second timeout manually.

A human joining, including someone still downloading the map, resets the timer.
After the last human leaves, a full new interval begins. Computers and the
virtual host do not count as humans. An unfinished connection handshake defers
rotation until it is resolved, without restarting the interval. A countdown or
loading/running game cannot be closed by this timer. With a one-entry cycle the
same map gets a new lobby after the timeout. With repeated IDs, each cycle
position remains distinct.

The native event loop checks actual human connections before fencing only the
empty current lobby. A new join cannot be accepted into that fenced identity.
Existing matches are not stopped, rehosted or migrated. The controller publishes
the next map through the usual Star session and directory-confirmation path.
For catalog-managed hosting, the older `bot_lobbytimelimit` is ignored because
it could close a lobby containing players.

Idle closure emits an authenticated `lobby_closed` version-3 record with
`reason: "empty_timeout"`, the catalog identity and host counter. The SQLite
cursor commits the close fence, idle fence and next cycle position together.
Replaying the record cannot skip another entry. Ordinary version-1/2 closes
retain their previous behavior and do not advance the cycle. Idle skips create
no `game_started` record and do not change the last-started match. Once used,
the cursor database has `user_version=3`; an older runtime must not open it.
Keep the pre-upgrade configuration/state backup for a stopped rollback.

The operator console reports:

```text
INFO supervisor: Empty lobby timeout reached. Advancing to the next map; active matches are preserved.
```

Native/controller tests cover repeat-safe durable advancement, crash replay,
human downloads, pending admission and a running match exchanging actions while
the next empty lobby rotates. These synthetic W3GS tests do not replace a real
Warcraft client compatibility test.
