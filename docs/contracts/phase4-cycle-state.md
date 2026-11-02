# Phase 4 cycle state and native lifecycle

`CycleState` is a single-owner state machine; it neither reads nor
acknowledges the native journal nor controls a lobby. The opt-in production
supervisor boundary is described below.
Its existing `reserve`/`started` API and version 1 state remain supported.

For native lifecycle events, `reserve_external(session_generation, host_counter)`
durably records the current preset and the observed native lobby identity before
start. The generation must be 32 lowercase hex digits, the counter an integer
from 1 through 2^28-1. The first external reservation migrates state to SQLite version 3 without
discarding the cursor, previous legacy `last_started`, or other state.
A pending lobby cannot be replaced or silently discarded.

`consume_external(event)` accepts the exact version 1 `game_started` JSON
object defined by the native lifecycle contract. A new event must match both
fields of the persisted external reservation. Its native `match_id`, generation,
counter and preset are persisted with the advanced cursor in one SQLite transaction before
returning `True`. SQLite uses rollback journaling and `synchronous=EXTRA`. Exact
replays of any recorded native `(session_generation, match_id)` return `False`
without advancing, including after a restart or later starts; the same identity
with a different counter is rejected. Unknown, stale, malformed or unmatched
events raise an error and leave state unchanged. Reservation pairs cannot be
reused after consumption, even if native later reuses a host counter.

Native idempotency keys are retained in a SQLite table without a fixed lifetime
count limit or automatic pruning; storage usage grows with starts and eventually
depends on available disk space. The cursor and new key commit together. Disk
and SQLite write failures fail closed; an uncertain result requires reopening
and reconciling before retry. SQLite requires a local filesystem and storage
that correctly implements locking, atomic operations and fsync. Protect and back
up the SQLite database and its directory consistently; do not copy a live DB
without SQLite's backup facilities. No multi-owner coordination is provided.

Migration imports version 1 or 2 state, including pending and all prior external
keys, into a private temporary SQLite file in the same directory. It commits,
checks integrity and imported data, fsyncs the file, and publishes it as
`<state-path>.sqlite3` using Linux `renameat2(RENAME_NOREPLACE)`. It fsyncs the
directory before replacing the JSON with a migration marker and fsyncs the
directory again. On restart a published database and old JSON are checked for
an exact migration match before the marker is completed and any new event can
be committed. Before publication, the original JSON remains authoritative. A
missing database with the marker, a corrupt published database, or inconsistent
artifacts fail closed. A preexisting final path is never overwritten. Crash
leftovers are not automatically deleted; only a separate ownership-aware
recovery procedure may remove them. Linux support for no-replace rename and
reliable filesystem fsync is required. Older readers cannot consume version 3;
rollback requires the new reader or an explicit lossless migration. SQLite
errors do not authorize acknowledgment or deletion of journal events.

The journal consumer must validate file name, file type, bounded JSON, producer
and directory durability as required by the native lifecycle journal contract.
Only after this state transition has been persisted may it create the durable
ack and remove the event according to that contract. On mismatch, uncertainty or
missing reservation, retain the journal record for reconciliation; never ack or
delete it as processed. These APIs do not provide concurrency control: one
trusted owner must serialize state and journal operations.

There is an unresolved start-before-reservation race: native can commit
`game_started` before the supervisor observes lobby state and persists the
reservation. Moreover, host counters may change or be reused, so observing a
matching pair alone does not establish a proof of lobby ownership across the
whole interval. Production integration requires a native handshake/gate that
prevents start until the correct reservation is durable, or another demonstrably
safe binding and reconciliation protocol. An unmatched record must remain
unacknowledged. The present module does not close that race; end-to-end lifecycle
handling is not implemented or verified.

## Production supervisor integration boundary (offline, opt-in)

`ICCupSupervisor(..., cycle_session={'root': absolute_root, 'cycle': [...]})`
opts into a journal pump in the production controller's control thread. The
root must already be an exclusively owned managed `SessionLease` session.
The pump opens the lease and cursor itself, scans at most once per second,
commits validated start/close events through `CycleCoordinator` and ACKs only
after the durable state transition. It never interprets an observed phase as a
start. A close does not advance the cursor; a start advances once. Any missing
or uncertain exit leaves the reservation pending. Journal or state errors
latch `storage_error`; the controller keeps an already running native process
and refuses subsequent native launches.

This constructor argument is an internal integration seam, not a deployable
configuration option. In this mode the controller unconditionally reports
`ready=false` and refuses to spawn native, including when there is no pending
reservation: production wiring for native IPC and proof of the selected preset
are not integrated. The opt-in controller reader exclusively reads binary stdout
from its own Popen, passes the exact child instance and reader token through its
internal queue, and binds the startup record under the lease on the control
thread. Wrong child, stale generation, duplicate startup or invalid metadata
never publishes a binding. A binding error blocks the cycle but does not kill a
running native process. This path is exercised offline; it is not reachable by
normal production configuration while launches remain disabled. No current
controller path constructs or enables this mode automatically.
The default controller has unchanged behavior.

Snapshots include a `cycle` object in opt-in mode with `state` (`initializing`,
`blocked_pending`, `waiting_native_ipc` or `storage_error`) and
`recovery='unavailable'`; a healthy pump also reports the current preset and
whether a reservation remains pending. Values are operational observations,
not proof that the native preset has changed. Startup after an unconfirmed exit
requires actual start/close journal evidence or future recovery; an empty scan
never abandons the reservation. Errors are latched until supervisor restart and
operator inspection. Journal fsync and SQLite transactions occur synchronously
on the controller thread and are throttled to at most one scan per second;
latency under slow storage and load has not been tested. Recovery and
multi-match are not declared GREEN.

## Cursor and journals across native generations

The managed root owns one persistent SQLite cursor and retains a separate
private journal directory per native process. After a verified exit, handoff
first drains start/close records, validates remaining ACKs against the SQLite
history, and requires that no reservation remain pending. It then creates and
fsyncs a fresh journal directory and atomically publishes a private catalog of
all retained journal device/inode identities, followed by a root directory
fsync. The original journal and its ACKs stay in place; the cursor is never
copied, reset or inferred from a phase. Reopening the lease selects the last
catalogued journal and checks every retained directory identity. The catalog
is limited to 1024 journals; pruning and compaction are not implemented.

The exit capability must be the same `OwnedChild` that produced the durable
session binding, with `waitid(P_PIDFD)` and `Popen.wait()` confirmed by this
supervisor. A competing `poll()`/wait, missing binding, uncertain exit, orphan
journal directory, malformed catalog, I/O uncertainty or pending reservation
without a durable start/close blocks handoff. A crash after creating a journal
but before publishing the catalog leaves an unlisted directory and deliberately
blocks reopen for manual reconciliation. A crash after successful publication
reopens the catalogued journal. No restart procedure fabricates a wait capability
or tombstones a pending reservation. In particular, an empty journal and an
exit observed only after supervisor restart do not permit automatic handoff.

This is a controlled handoff for an exact child and cooperating native journal,
not a proof of general recovery after an unconfirmed crash. No map selection or
native launch is enabled by the handoff; those remain blocked pending the native
IPC contract. Catalog validation is synchronous and grows with retained journal
count; filesystem latency, event-loop impact and load are unmeasured.

With sealed preload enabled, the native generation owns both its pinned map
catalog and its session reader/pidfd. The controller passes only the catalog's
sealed FDs to that native child; relay and Star do not inherit them. A failed
stop retains the live child and its owned resources for retry. Once the child
has stopped, the catalog and session handles are closed; handoff still requires
the exact-child wait proof and a fully resolved reservation. Session binding
does not itself enable the future preset IPC or authorize recovery.

For a native child with `OwnedChild`, controller liveness checks use
`waitid(P_PIDFD, WEXITED|WNOWAIT|WNOHANG)` and do not call `Popen.poll()`.
A liveness observation reports only whether the child has exited; it does not
create the verified-wait capability. `OwnedChild.wait()` remains the reaping
operation and records the exact status only after the pidfd wait and Popen wait
agree. An unavailable or ambiguous pidfd observation latches a cycle error,
keeps the child tracked, and prevents a restart; it is not treated as an exit.
If another caller reaps the child, normal process cleanup may proceed but
handoff still refuses without the verified-wait capability. Other components
and the default runtime retain their existing polling behavior. This does not
change the blocked IPC or unconfirmed-exit recovery contracts.

## Catalog close v2 and mailbox integration

The optional native catalog mailbox consumes a durable immutable request via
`next.ready`, stages the selected preset on its event loop, and publishes a
correlated close v2 containing generation, counter, request ID, manifest digest
and catalog index. The native lobby fence is installed before durable close
publication. A legacy close v1 remains valid for older non-catalog lobbies and
for already authenticated, bound legacy reservations; it cannot correlate an
unbound catalog selection.

`CycleCoordinator(..., catalog_requests=queue)` routes close v2 to the matching
`CatalogRequests`. The cursor commits the v2 identity and fence in SQLite before
queue retirement; the journal ACK follows both commits. A crash between them is
replayed idempotently, without moving the cursor. Wrong session/digest/index,
conflicting close identity, and a v2 record without its queue remain unacked.
The queue preserves legacy version-1 `outcome=closed` records. A v2 catalog
close upgrades the queue to version 2 with an explicit closed status; older
readers reject that version rather than silently interpreting the new state.
The original v2 closed record from `5965659`, which has no `outcome` field,
also remains readable and replayable. Previously bound legacy closes and new
v2 completions remain readable. A stale
close cannot retire a different current selection. `CycleDriver` passes its
catalog to the coordinator and ends the step on close before selecting again.

This covers only the checked offline protocol. The default production pump
still has no bound catalog and refuses a v2 event instead of ACKing it; native
launch, runtime IPC activation and exit-based recovery remain fail-closed.
