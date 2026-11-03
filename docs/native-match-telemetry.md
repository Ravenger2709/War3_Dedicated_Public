# Native match telemetry v1

For catalog mode with a valid lifecycle generation, the native event-loop
`[HOSTBOT_STATE]` may include `matches_v1`. The enclosing `session_generation`
and each nonzero `host_counter` together identify a match for that process;
these are the same counters used by its start journal. No player names,
addresses, game names or map paths are in this object. It contains:

* `version: 1`, `active_count`, and `matches` (at most five). Each item has
  `host_counter`, `match_id` (or `null` when no durable start was committed),
  `catalog_index` (or `null` when unavailable), and `phase` (`loading` or
  `playing`). Native copies `match_id` into the game only after the start
  journal rename and directory fsync succeed. The index addresses the
  immutable startup manifest. Older v1 emitters may omit `match_id`.
* `retired_count`: how many objects were removed from `m_Games` since this
  native process started. This includes interrupted/loading matches and is not
  a win, game outcome, or proof of a normal map completion. It is not durable.
* `rotation_error_count`: process-lifetime count of new rejected staging ACKs,
  newly latched IPC faults, failed lobby construction/bind, catalog-admission
  identity failures, and newly latched lifecycle start/close journal failures in
  catalog mode. Capacity-full is a normal wait and is never counted. Other
  failure categories, failures before catalog startup and supervisor errors
  are not included. Replayed ACKs do not increment it. These counters reset
  with the native process; they are not historical totals across generations.

If native cannot construct a consistent bounded list, the entire extension is
omitted. Python rejects malformed/unknown shapes, duplicate counters, count
mismatches and invalid types. It accepts the extension only from the same fresh
owned native generation and rejects decreasing process counters or uptime.
Status resolves catalog index to preset ID and sealed map SHA-256 using the
manifest bound to that child. An unknown index makes the extension unavailable;
null index remains null and is never guessed from names. Stale data expires.
`finished_matches` exposes `retired_count` under the meaning above, and
`rotation_errors` exposes the scoped native rotation counter; the all-error
`error_count` remains unavailable. Availability is
`verified_process_snapshot`, not a durable total. Older native binaries without
the extension continue to report these fields as unavailable.

The checked-in AMP Generic template has player join/leave regexes and existing
AMP player/session Analytics APIs. Repository documentation records an AMP
2.8 installation exposing `AnalyticsPlugin/GetAnalyticsSummary`,
`AnalyticsPlugin/GetUserDetails`, and `Core/GetUserList`; this offline work does
not inspect a running AMP instance. There is no verified stock channel here
for arbitrary per-match numeric metrics or new graphs. `/status` supplies the
structured data and existing `INFO supervisor: Cycle ...` console lines display
aggregate counts; no AMP widget, numeric Analytics ingestion, fabricated user
or new endpoint is claimed. Production cycle launch remains guarded.

The native patch modifies `ghost.cpp`, `ghost.h`, `game_base.cpp` and
`game_base.h`; it is based on integration `f4293de`. A parallel native IPC
inode patch touching these files requires a source-level merge and regenerated
patch/digest followed by the native suite, rather than textual cherry-picking.
