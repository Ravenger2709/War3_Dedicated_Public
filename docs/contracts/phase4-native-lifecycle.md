# Native lifecycle journal v1

This is an opt-in Linux-only contract; when disabled, it preserves the Phase 3
state and publication formats. `bot_lifecycle_event_dir` is an absolute path to an
already existing directory owned by the GHost effective UID, mode `0700`, with
no symlink components. Empty or absent disables the journal. An invalid enabled
configuration stops the host. The directory must be on a local filesystem that
supports `fsync` on regular files and directories, hard links, and Linux
`renameat2(RENAME_NOREPLACE)`. Use one trusted writer/consumer ownership domain;
the directory must not be replaced while GHost is running.

After the final native countdown check, GHost commits the game's in-memory
loading transition, writes a unique mode `0600` temporary file, fsyncs it,
renames it without replacement to the event name, and fsyncs the directory.
Only then does it emit `[HOSTBOT_LIFECYCLE]` and proceed to stop lobby
publication and queue game-start packets. A journal write error closes the
affected lobby and latches creation of new games off until native restart;
previously started games continue to be serviced. If the same storage failure
also prevents writing the unpublish intent, the publisher may retain a stale
advertisement; an external operator must reconcile it. A rename or directory-fsync error can leave a visible
record; consumers must retain and reconcile it. Existing records are never
modified or deleted by GHost, regardless of ack state.

The start and close journal writes, file fsyncs, renames, and directory fsyncs
execute synchronously on GHost's game event loop. Slow or stalled storage can delay packet processing
for other running matches. The offline tests do not measure event-loop latency
under storage contention or establish any real-time guarantee. Concurrent-match
latency must be measured on the intended storage before operational acceptance.

The final filename is `start-<session_generation>-<match_id>.json`. Contents
are UTF-8 JSON followed by a newline, exactly these fields:

```json
{"version":1,"event":"game_started","session_generation":"0123456789abcdef0123456789abcdef","match_id":"fedcba9876543210fedcba9876543210","host_counter":1}
```

When the journal is enabled and initialized successfully, the periodic
`[HOSTBOT_STATE]` JSON additionally contains `"session_generation"`, with the
exact same 32-digit value. It is present even in `IDLE` before a lobby exists
and remains constant for the native process. It is absent when the journal is
disabled. The state is a snapshot, not a durable event stream: reservations must
bind the generation and lobby `host_counter` to the actual native process and
persist that binding before a start. A process restart produces a new generation.
The journal `match_id` is created only at start; it is not a pre-start cursor
reservation UUID. Correlation and idempotent persistence remain consumer duties.

Both IDs are 32 lowercase hex digits from independent 128-bit OS CSPRNG draws.
`session_generation` is generated for each native process; `match_id` for each
committed start. The composite `(session_generation, match_id)` is the
idempotency key. `host_counter` is the native 28-bit advertised counter and is
not unique across restarts or refreshes. This is a start commitment, not proof
that a client received a packet, loaded the map, or completed a game. A crash
can occur after the durable record but before any packet reaches clients.

## Consumer and acknowledgement

Catalog-managed empty lobbies additionally support [idle timeout rotation](../empty-lobby-rotation.md).
Its close record is version `3`, with the same catalog identity as a version-2
close plus the exact `reason: "empty_timeout"`. It is emitted only after the
native admission thread checks the lobby has no human or potential connections
and fences that identity. Only this close kind advances the cycle. The SQLite
cursor commits its idle fence and next position in one transaction; no start
event is synthesized. Normal version-1/2 close semantics remain unchanged.

Python should scan all `start-*.json` and `close-*.json` files at startup and after every wakeup;
stdout is only a hint, never a cursor or an authoritative event stream. A scan
can see the name between rename and producer directory-fsync. Before using a
newly discovered event, open and fsync the journal directory, then read the
bounded JSON and validate version, event, IDs, filename, and counter. Reject
unexpected file types/symlinks and unknown versions. The producer fsyncs file
contents before rename. The consumer should durably record its processing and
idempotency key before acknowledging; exactly-once effects across another
database or service require an application-level transaction or idempotent
operation. No global ordering between events or across process generations is
promised; enumerate every event, not the current phase.

An ack is the corresponding event filename with `.json` replaced by `.ack`,
containing exactly the same bytes as the event. For a trusted single consumer, perform:

1. Persist the consumer's result and idempotency key.
2. `os.link(event, ack, follow_symlinks=False)`. If `ack` exists, verify that it
   is the same record; an existing matching ack means success, never overwrite.
3. `os.fsync(dirfd)` to make the ack durable.
4. Unlink the `.json` name if present, then `os.fsync(dirfd)` again.

On a crash between these operations both names may exist; a validated matching
`.ack` suppresses replay and cleanup can be retried. A crash before durable ack
can cause redelivery. The consumer owns ack and retention: once its durable
state no longer needs the record, it may unlink `.ack` and fsync the directory.
A crash before rename can also leave `.start-*.tmp` files; they are not events.
Only remove stale temporary files when no producer can still be writing them.
The producer performs no automatic pruning and does not enforce a size limit;
monitor free space and retain records until the consumer has acknowledged them.
A full, read-only, inaccessible or unsupported journal fails closed at start.

The design cannot atomically commit an in-memory game and filesystem state
across a process crash, guarantee storage hardware honours flushes, recover
already lost files, or prevent duplicate external effects without the consumer's
own durable idempotency. Power-loss testing, real clients, load, and end-to-end
supervisor integration remain outside these offline tests.

## Optional start-reservation gate v1

Set `bot_start_reservation_gate = 1` (or `true`) at native process startup to
require a reservation. The default `0`/`false` preserves the previous behavior.
Other values are invalid. Gate activation requires a valid, enabled
`bot_lifecycle_event_dir`; invalid enabled configuration stops initialization.
The gate setting is fixed for the process lifetime. The state generation is
available from `[HOSTBOT_STATE]` even before the first lobby.

For the current session generation and **current** native lobby host counter,
the supervisor must first durably commit its own reservation (including its
preset ID) in its own storage. Only after that transaction and its required
file/directory syncs succeed may it publish the marker in the journal directory:

```text
reservation-<session_generation>-<host_counter>.ready
```

The marker is a regular file, owned by the native effective UID, mode `0600`,
link count 1, with exactly this ASCII content, including the final newline:

```text
version=1
session_generation=0123456789abcdef0123456789abcdef
host_counter=42
```

`host_counter` is the nonzero canonical decimal advertised counter without
leading zeros. The supervisor creates a private uniquely named temporary file in
the same directory, writes the complete payload, `fsync`s the file, atomically
renames it to the `.ready` name without replacement, then `fsync`s the directory.
It must never publish before its reservation commit or modify an existing
marker. A hard-link-no-replace plus removal of the temporary link is another
valid publication method; while the file has two links native rejects it. The
supervisor leaves markers immutable for the lifetime of the native process;
only after that process exits may it remove obsolete markers and fsync the
directory. Markers are not lifecycle events and are not ACKed by the journal
consumer. The marker carries no preset: the gate trusts the supervisor to have
committed the correct `(generation, counter, preset_id)` before publication.

At the final `EventGameStarted` transition, after countdown readiness is
rechecked and before loading, publication stop, journal event, or start packets,
native opens the exact marker without following symlinks, checks its owner,
mode, type, single link, size, exact payload and current directory/file identity,
and fsyncs the marker and directory before accepting it. This extra directory
sync handles observing an atomic rename just before the supervisor's directory
fsync. Absent, stale, malformed, inaccessible or unsyncable markers deny the
start and cancel only that lobby's countdown; already running games continue.
The owner or autostart mechanism can retry after publication. No gate permission
is cached. Rehost changes `host_counter`, so the previous marker cannot permit
the new lobby. A native restart generates a different session generation, so
markers from the earlier process cannot permit its lobbies. Gate-enabled starts
reject full native counters outside `1..0x0fffffff` rather than reusing a 28-bit
identity after wrap; restart before reaching that bound.

Durability is conditional on the filesystem and storage honouring `fsync`; the
native side cannot verify that the supervisor actually committed its state
before it published the marker. Both processes and the private directory must
be trusted, and correlation with a cursor remains a supervisor responsibility.
A crash after a durable marker but before start is safe: the same reservation
can be retried for that generation/counter. A crash after start still requires
journal replay and idempotent event processing. Power-loss and end-to-end
supervisor integration have not been tested.

The existing native event loop is single threaded. Reading metadata and the
synchronous marker/file/directory `fsync`s occur on that loop, followed by the
journal's synchronous writes on successful starts. Slow or stalled storage can
therefore delay updates for *all* running games. This gate has not undergone
traffic or load testing and is not a latency or multi-match GREEN claim.

## Fenced closure of an unstarted lobby

With the reservation gate enabled, native emits a durable close event when an
unstarted current lobby is destroyed (including graceful shutdown) or before
its host counter changes on any supported rehost path. Close is not emitted
for a started/loading game. The game loop first marks the old lobby identity
fenced in memory; `EventGameStarted` then refuses that identity. Native writes,
file-fsyncs, renames without replacement, and directory-fsyncs this file before
rehost changes the counter or a close notification is logged:

```text
close-<session_generation>-<host_counter>.json
```

```json
{"version":1,"event":"lobby_closed","session_generation":"0123456789abcdef0123456789abcdef","host_counter":42}
```

The generation and nonzero canonical decimal counter have the same meaning and
bounds as the gate marker. The key `(session_generation, host_counter)` is
unique within a native session. Gate-enabled host counter allocation never wraps.
For the same pair, a successful `game_started` and `lobby_closed` are mutually
exclusive: a start commits on the single event loop before a close is possible,
and a close installs the in-memory fence before it becomes visible. This is
stronger than relying on directory listing order. An uncertain start journal
failure disables new games and suppresses close; do not infer absence of start
from missing files. A crash or SIGKILL before a durable close can leave the
reservation pending indefinitely. If the close write fails the old identity
remains fenced, the lobby is closed, new games are latched off, and active games
continue; a visible file after an uncertain directory fsync must be reconciled.

A consumer fsyncs/scans the journal and validates/ACKs close files using the
same rules as start files. `CycleCoordinator.consume` commits the closed pair
and clears only a matching external pending reservation in one SQLite
transaction, without advancing the cursor. A close for an unknown or different
pair is recorded as a tombstone without clearing another reservation. Replays
are idempotent; a closed pair cannot be reserved again, and conflicting start
and close records fail closed. The SQLite `user_version` is raised from 0 to 1
in the same transaction as the first close tombstone; a missing close table
with version 1 is rejected instead of silently permitting reuse. Tombstones are
not automatically pruned and can grow without bound. The consumer must preserve
its durable cursor and tombstone state before ACK. No controller or live host
integration is provided by this change, and there is no separate external revoke request API;
the consumer must wait for an actual native closure/rehost event. Filesystem
sync assumptions, `SIGKILL` ambiguity, old ACK retention, process ownership,
and single-threaded `fsync` latency limitations continue to apply. Do not use
state snapshots, marker deletion or publication changes alone as close proof.

## Native session startup observation (version 1)

When lifecycle journaling is configured and initialization succeeds, native emits
one stdout line before the first offline-ready notification:

```
[HOSTBOT_SESSION] {"version":1,"pid":1234,"session_generation":"0123456789abcdef0123456789abcdef","journal_device":2049,"journal_inode":5678}
```

The object has exactly those five fields. `pid` is the PID in native's PID
namespace; device and inode are unsigned integers from `fstat` on the open
journal directory; generation is 32 lowercase hexadecimal characters. Native
retains that directory descriptor until destruction and checks that every later
journal/marker access still resolves to that same inode, failing closed on
replacement. The line is absent when journaling is disabled. It is only a
startup observation: stdout, PID and these numbers do not themselves prove
ownership or durable association, and must not authorize reservation removal.
In particular, the supervisor must authenticate the source as the stdout of its
exact owned child, check PID namespace translation, durably bind process start
identity and journal inode before marker publication, and retain a common lease.
None of those integrations is implemented by this line.

## Native catalog session startup observation (version 2)

With the sealed catalog and IPC configured, native emits version 2 instead of
version 1, adding `ipc_device` and `ipc_inode` to the five fields above. Both
are unsigned integers from `fstat` of the IPC directory FD retained by native;
the inode must be nonzero. Native fails startup if the descriptor or pathname
is no longer stable before emission. Journal-only mode retains version 1.

Before launching the child, the controller holds the session lease and opens
and retains the private IPC directory. The authenticated reader of that exact
owned child requires v2, checks the session IPC device/inode against the held
FD and revalidates the directory pathname under the leased root before the
binding is persisted and before catalog attach. Later IPC operations recheck
that directory. Version 1 cannot authorize a catalog attach. A mismatch or
missing identity blocks the cycle and does not authorize a request or recovery.
The observation is not proof that a process has exited or that its journal is
drained. Crash recovery remains fail-closed.

## Opt-in managed session API (offline only)

`SessionLease.create(absolute_new_root, enabled=True)` creates a fresh mode-0700 session
root with `journal/`, the fixed `cursor.json` path, and a durable private marker.
The explicit `enabled=True` flag is mandatory; omitted or non-boolean settings
fail closed.
It holds an exclusive nonblocking `flock` on that root for its lifetime. Reopen
with `SessionLease(root, enabled=True)` after supervisor restart. The same live lease object
must be supplied to `CycleState(..., lease=lease)` and to `scan(..., lease=lease)`;
`CycleCoordinator` forwards it for scan and marker publication. Managed paths
reject these calls without a lease. The lease checks root and journal inode,
marker, owning process/thread and the lock attached to its fd before operations.
One cursor object and one startup reader may be claimed per lease. Forked or
cross-thread use is rejected. A completed binding or an orphan binding temp file
prevents another native reader from using the same journal.

Use `OwnedChild` on the actual live `Popen` and create
`NativeSessionReader(owned_child, lease, trusted_executable_path)` with an
unbuffered binary `stdout=PIPE` (`bufsize=0`). The reader must be the **sole**
reader of that pipe; it reads and strictly validates `[HOSTBOT_SESSION]` itself.
It checks that the Popen stdout pipe is the child's `/proc/<pid>/fd/1`, the
expected executable inode is `/proc/<pid>/exe`, the PID namespace is shared,
the retained pidfd/parent/boot/starttime identity is unchanged, and the native
PID and journal device/inode match the leased directory. It persists
`journal/session-<generation>.json` with session, process and executable inode identity using a
private temporary file, file fsync, no-replace rename and directory fsync.
Excessive output, invalid JSON/duplicate keys, identity changes, a different
journal or any uncertain I/O fail closed. The reader retains bytes following
the startup line in `buffer`; callers must maintain sole ownership of stdout.

Under this opt-in API, `allow_lobby` and marker publication additionally require
the successfully persisted binding for that generation and revalidate the live
child. Reopening a lease after restart does not restore that live capability and
cannot publish another marker from the stored record. A reservation stays
pending if the capability is lost. These rules do not change the legacy mode.

The lease is cooperative, not a defense against arbitrary same-UID code,
renaming/removing its files, an old runtime ignoring the marker, another stdout
reader, or native forwarding its output/journal writes to a different process.
The supervisor must use one fresh journal directory per child, preserve it,
trust and pin the executable, and serialize its own operations. The managed
root may retain multiple journal directories with one shared cursor as described
in the cycle-state contract. A crash before
the binding is durable can leave an unbound child: supervisor restart must not
reuse that journal or infer that the writer is gone. This offline API is not
wired into the live controller. It does not implement validated restart absence,
full recovery scanning or conditional tombstones.

## Saved writer observation v2 (diagnostic only)

The newly written session binding is version 2. Its `native` object captures
`boot_id`, `pid`, `start_ticks`, `pid_namespace_device`, `pid_namespace_inode`,
`exe_device` and `exe_inode` from the owned running child; those values are
cross-checked with the process and executable records and rechecked before
publishing the binding. Version-1 binding files are not promoted into v2.

`inspect_recorded_writer(lease, generation)` fsyncs the held journal directory,
validates the private binding and journal inode under the lease, checks boot ID,
PID namespace and procfs visibility, then uses `pidfd_open` and `/proc` identity
to report `running`, `exited`, `absent_pid`, `pid_reused` or `different_boot`.
PID reuse is reported only when the visible process has a later starttime;
ambiguous identity, unavailable pidfd, permission failures, namespace mismatch,
invalid metadata and unexpected poll events fail closed. Equal start ticks are
never used as proof of reuse. These statuses are observations, not capabilities
for reservation removal. Reopening the lease does not authenticate an unbound
child or prove journal writer exclusivity.

`journal_audit.audit(lease, generation)` separately fsyncs and enumerates the
whole managed journal under the lease. It validates pending and ACK-only start
and close records, gate markers and the v2 session binding, and refuses unknown,
partial, foreign, conflicting or changing entries. It does not ACK records,
compare all records with SQLite history, freeze a live native writer, or clear
pending state. In particular, an empty inventory is not an absence proof.

The source tree was inspected for direct `fork`, `vfork`, `clone`, `exec`,
`posix_spawn`, `system` and `popen` calls in native GHost and bncsutil; no
relevant direct calls were found. That inspection does not prove that an
arbitrary executable accepted by the current reader, dynamically linked code,
or inherited file descriptors cannot delegate the journal writer. The current
launcher does not install an attested no-fork/no-delegation fence. Consequently
no implementation may use a recorded PID's absence to tombstone a reservation.
A future implementation needs an enforceable and durably attested writer fence,
plus complete validated journal/ACK/temp-file reconciliation and an atomic,
conditional no-event tombstone with late-event rejection. Until then
`reconcile_after_native_exit` remains fail-closed, including after reboot.

## Exit-based absence reconciliation: unavailable

`CycleCoordinator.reconcile_after_native_exit(child, session_generation)` is a
fail-closed placeholder. It always raises `ReconciliationUnavailable` and does
not poll or wait on a child, scan or ACK the journal, or modify the cursor. It
accepts no caller boolean as proof. A real `game_started` or fenced
`lobby_closed` can still be processed through `consume()` under its existing
exclusive-owner contract; a missing event cannot currently clear a reservation.

A separate, non-integrated `src.runtime.process_ownership` module now provides
`OwnedChild`: it opens a pidfd while an actual `Popen` child is alive, checks
kernel parentage and start identity, and uses `waitid(P_PIDFD)` plus the owned
`Popen.wait()` for an exact-child exit. A competing reaper or unavailable pidfd
fails closed. `inspect_saved_identity` reports diagnostic visibility, including
simulated PID reuse, but never authorizes recovery after restart.

`write_unverified_observation` can atomically write a mode-0600,
file-and-directory-fsynced `unverified-observation-*.json` with boot ID, the
**observer's** PID namespace device/inode, PID, `/proc` start ticks, UID and
observed journal device/inode. Its `reported_session_generation` is supplied
by the caller and `verified_binding` is always false: the native writer and
the association to this journal have not been authenticated. The short-lived
journal flock serializes this observation only with cooperating journal scans;
it is not a lease over the cursor or marker publisher. These observations are
not lifecycle events, are ignored by `scan()`, and must never authorize a
`child_exited_no_event` tombstone. The module is not connected to the live
controller and does not change the fail-closed reconciliation API.

The current runtime is insufficient to implement the requested absence proof:

- `Child.process` is a live `Popen` while the supervisor owns it, and the
  wrapper `child_exec.py` uses `execvp`, but native `session_generation` is
  only an observation from stdout. No durable record binds that generation,
  journal identity and reservation to this exact child lifetime. The new
  offline managed API now binds an owned stdout observation, but the live
  controller does not use it. The
  supervisor's integer child generation is a different identity.
- `_stop_component` removes the `Child` from the registry before waiting and
  does not hand a verified exit capability to `CycleCoordinator`. A PID, an
  exit code, a caller-provided generation, `poll()`, or a missing `/proc/PID`
  cannot establish this binding or rule out PID reuse. A parent-death signal
  is not evidence that the process has exited. After supervisor restart, its
  original `Popen`/pidfd and the ability to wait for that child are lost.
- The journal `flock` in `scan()` protects only that scan. `CycleState`, marker
  publication and all coordinator entry points do not share a process-wide
  lease over the cursor and journal for the whole reservation/exit/reconcile
  sequence. The existing account lock does not prove exclusive ownership of
  a particular cursor and journal. No external monitor currently supplies an
  equivalent fencing capability.

A future implementation must supply all of the following, not caller claims:

1. While the child is alive, bind the generation read from its owned stdout to
   a retained exact child handle (`Popen` plus pidfd or equivalent), process
   start identity and boot identity, the exact journal directory inode and
   durable cursor/reservation identity. Persist that binding before publishing
   the gate marker. Do not use the supervisor's integer generation as the
   native generation. The trusted execution model must establish that no other
   process can write events under that native generation.
2. Acquire and retain a cross-process exclusive lease covering *all* cursor
   mutations, marker publications and journal consumption, verify the lock and
   directory identities, and prevent replacement or another native writer for
   that generation. Every participating path must honor the same lease; the
   present per-scan lock is not enough. Lease loss or uncertain ownership must
   fail closed.
3. Verify termination and wait for **that exact owned child**, with a retained
   handle immune to PID reuse, before scanning. Do not reap it in a competing
   poller. Do not manufacture proof from a PID, caller boolean, elapsed time,
   SIGKILL request, or an already lost/reparented child. A restarted supervisor
   requires an independently retained, verifiable ownership/fence capability;
   without one the reservation stays pending for operator reconciliation.
4. With the process stopped and lease held, fsync and validate the stable
   journal directory, completely enumerate and process all valid start/close
   records and relevant ACKs, and check the durable SQLite start/close history.
   Preserve the lease and directory identity through the cursor commit. Treat
   malformed, missing, inaccessible, conflicting, unaccounted, partial or
   ambiguous files, including orphaned temporary files for that generation, as
   unresolved unless an explicitly verified recovery protocol proves them safe.
   If a genuine event resolves the pending reservation, do not infer absence.
5. Only if the same pending pair remains and the complete evidence proves that
   neither start nor fenced close can have been lost, atomically persist a
   distinct `child_exited_no_event` tombstone and clear that exact reservation
   without advancing the cursor. Record the verified process/generation and
   journal provenance with the tombstone; never forge a `lobby_closed` event.
   The transaction and directory/database durability must survive crash and
   retries, and must refuse conflicting or late events. Crash-injection and
   power-loss testing are required before this protocol is considered ready.

A synthetic `SIGKILL` test with a waited `Popen` is deliberately insufficient:
it tests OS behavior, not the missing provenance and exclusive lease. If the
required capability is unavailable or uncertainty remains, leave the pending
reservation untouched and require operator reconciliation. This restriction
also applies to the earlier conservative recovery sketch: it is not an
implemented or validated procedure.
