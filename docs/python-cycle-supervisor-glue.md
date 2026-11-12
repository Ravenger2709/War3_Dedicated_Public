# Python cycle supervisor integration — source and offline scope

Public `gameplay.map_cycle` now selects the managed catalog path through the
ordinary `RuntimeBackend` and `ICCupSupervisor` in integration `48b85e9`.
`load_config` accepts the typed cycle and records explicit-cycle provenance;
an explicit one-entry cycle also opts in. Legacy `map_preset`-only input keeps
its prior startup path. This describes source integration and offline evidence;
it does not announce a new release, live operation, real-client acceptance or
overall GREEN.

The ordinary backend provisions the managed session and journal-bound IPC and
prepares sealed startup inputs. After the exclusive owned child emits its
authenticated session record, the supervisor checks that the
session journal and cycle match, reads the child's still-open sealed manifest,
checks its digest, descriptors, cycle and configured max, and creates one
exclusive generation-specific durable catalog queue. The catalog-aware driver
and lifecycle coordinator share the session's cursor and journal. A duplicate
or uncertain queue blocks; no automatic recreation after a crash is supported.
The supervisor continues processing durable close-v2/start events and will not
handoff a generation while a selection or reservation remains pending. Its
internal pump can dispatch and publish a marker only from a fresh matching
owned-child observation and a capacity check. Native launch admission now
checks the prepared automatic backend, cycle agreement, lease, journal and
absence of unresolved launch/selection state rather than rejecting every cycle.
Uncertain rotation errors block further cycle decisions without stopping existing
matches. A matching immutable `rejected` ACK before any lobby counter/reservation
is different: it proves that this selection was not admitted. The driver records
an immutable rejection intent, advances the cursor once, and tries the next
position after one second. Native request/ACK files and match history remain
unchanged. A crash between cursor commit and queue retirement resumes the same
intent without advancing twice. Missing, changed, foreign or `uncertain` ACKs and
existing reservations never authorize this path.

If every position is rejected, admission pauses for 30 seconds, then retries the
cycle with exponential delay capped at 300 seconds. Observing a real matching
lobby clears that failure streak. The pause affects new lobby selections only;
running matches and lifecycle consumption continue. Each rejection produces an
English warning naming the skipped preset, next preset and delay, so an empty
lobby is no longer silently pinned to a permanently negative request ID. A
rejection is not recorded as a played match or an empty-lobby timeout.

Early full-cycle validation (`8bbfe86`) runs before account-profile access and
launch ownership. Final preparation revalidates and seals the inputs, uses the
first sealed cycle preset as native default, and supplies its size, SHA-256 and
prepared settings. File Manager map edits between the early precheck and final
snapshot can still fail closed after claim creation; the claim may remain.
Interrupted catalog queues and unknown-crash ambiguity remain blocked for
reconciliation. Neither an empty scan nor PID absence authorizes a reset.

An owned, fresh native observation must prove the exact generation, request ID,
catalog index and real
host counter. Native checks the staged identity and available capacity on its
single event loop immediately before lobby creation; a staged ACK, name, old
stdout state or caller boolean is not a lobby proof.

The reported ordinary-backend E2E passed in **91.158 seconds**: production native,
actual backend/supervisor, public config, TCP 6113, normal vote A/autostart B,
distinct counters, capacity two and repeated A while B's actions continue.
Only fixture binary/library locations are remapped; realm startup is disabled.
The reported capacity-three harness passed in **43.454 seconds**, with B/C
actions continuing when A ends and the fourth lobby opens. That harness retains
a custom backend/spawn override and is not ordinary production startup proof.
Both use synthetic maps/peers. See [startup evidence](catalog-startup-integration.md)
for test paths and limits; no tests were rerun for this documentation update.

Readiness (`befe247`) now combines common healthy realm/native checks and the
current owned session binding with fresh cycle evidence: either a matching
published LOBBY/COUNTDOWN with listener, or actual IN_GAME with positive,
consistent active counts. Full capacity alone does not cause a restart. The
realm-disabled E2E does not demonstrate live authenticated readiness. See the
[monitoring contract](cycle-monitoring-contract.md). Unknown-crash recovery,
live iCCup/real clients and latency under load remain unverified.
