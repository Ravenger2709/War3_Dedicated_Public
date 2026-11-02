# Cycle monitoring contract

When cycle control is configured, `/status` includes `cycle`. Public explicit
cycles now reach the ordinary backend's managed startup path in source
integration through `48b85e9`; telemetry remains read-only. HTTP readers get
a copy of a snapshot produced by the supervisor's owner loop; they do not
access the leased cursor or journal directly.

* `map_cycle`, zero-based `index`, `current_preset` and `next_preset` come from
  the validated durable cursor. Here `current_preset` is the cursor's next
  selection, not necessarily a currently running map; `next_preset` is the
  following cycle entry. Duplicate IDs remain distinct positions.
* `max_matches` comes from the verified attached startup manifest. A fresh,
  owned session state provides `active_matches` as an aggregate snapshot,
  including at capacity. It is not a native admission decision and can change
  immediately after observation. Old, missing, mismatched, or unowned state is
  unavailable. Cached native telemetry expires using the metadata TTL even if
  the controller loop stalls.
* `current_lobby` is populated only for LOBBY/COUNTDOWN when generation,
  request ID, catalog index and actual counter match the pending selection in a
  fresh owned state. `IDLE` means `none`; an IN_GAME state may represent a
  loading or previous game and is `unavailable`. A matching state is still a
  snapshot, not a publication acknowledgement or proof of freshness at a
  later marker operation.
* `active_match_list`, `finished_matches` and `rotation_errors` are populated only
  by a fresh validated native `matches_v1` extension bound to the startup
  manifest. Their process-lifetime scope and limitations are specified in
  [native match telemetry](native-match-telemetry.md). Otherwise they are null
  and unavailable. The all-error `error_count` stays unavailable. `last_transition`
  is a process-local observed durable start/close transition and observation
  time; it is not durable history.
* `state: capacity_full` and `status_code: CAPACITY_FULL` mean the latest
  verified snapshot reports `active_matches == max_matches`. This is normal
  waiting, not an error, request rejection, admission guarantee or restart
  trigger. Storage errors take precedence. When the snapshot expires, the code
  and native-derived values become unavailable; an unresolved pending request
  remains `blocked_pending` rather than being reported as an idle wait.

## Readiness from current evidence

Readiness integration `befe247` removes the former unconditional `ready=false`
for cycle mode. Common checks still require a wanted, non-stopping service,
healthy fresh native state and a healthy authenticated realm: current Star and
relay observations, expected identity confirmation and realm connection health.
The cycle must additionally bind to the current owned native child, its bound
session reader, the same lease and the attached catalog. Expired, missing or
mismatched evidence, cycle failure or storage error denies readiness.

With those common checks satisfied, readiness requires fresh verified active
counts consistent with native `active_games`, within the configured 1–5 limit,
and one of these cases:

* LOBBY/COUNTDOWN has a fresh matching catalog lobby identity, native listener,
  and accepted publication for the current native PID and host counter. A
  staged catalog ACK, unrelated advertisement or active older match cannot
  substitute for that lobby's evidence.
* Actual native IN_GAME has a positive active count. It does not require a new
  lobby, listener or advertisement, including at `CAPACITY_FULL`.

An IDLE or zero-count IN_GAME observation cannot assert readiness. Publication
acceptance still does not prove visibility in ordinary iCCup list/exact lookup
or admission by a real client. Unknown-crash ambiguity remains blocked; a
snapshot does not resolve pending launch ownership or recover lost matches.

The reported ordinary-backend offline E2E passed in 91.158 seconds with public
config, TCP 6113, normal vote A/autostart B, capacity two and repeated A while B
continued actions. Its realm was disabled, so it does not demonstrate live
authenticated readiness. The capacity-three harness passed in 43.454 seconds
with its custom backend/spawn override; it is not ordinary startup proof.
Both use synthetic maps/W3GS peers. These are source/offline observations, not
a new release, live/real-client acceptance or overall GREEN; no tests were
rerun for this documentation update. See [startup evidence](catalog-startup-integration.md).

AMP receives a concise `INFO supervisor: Cycle ...` console line when the
verified summary changes. Its existing console stream can be searched by
Analytics; this change does not add AMP panel widgets, numeric metric ingestion,
player joins, or a finished/error counter. `/status` is the machine-readable
source. Durable historical outcomes and counters across restarts need a separate
versioned event journal and replay protocol.
