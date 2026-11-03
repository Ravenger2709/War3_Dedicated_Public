# Catalog startup integration — source and offline evidence

This describes integration through `48b85e9`, including startup wiring
`647605a`, early validation `8bbfe86` and readiness `befe247`. Public
`gameplay.map_cycle` now reaches the ordinary `RuntimeBackend` and
`ICCupSupervisor` catalog path. An explicit cycle, including one entry, opts in;
legacy `map_preset`-only configuration retains its prior startup path. These
are source integration and offline results, not a new release, live deployment,
real-client acceptance or overall GREEN.

`prepare_catalog_startup` validates the full cycle and seals each distinct map
and the manifest, returning the owning `StartupBundle` and candidate native
config overrides. The helper itself does not write the effective config or
launch GHost; the backend now performs that integration. Automatic startup uses
`default_map_bytes=None`, derives the size from the first sealed cycle preset,
and supplies its prepared settings/mapcfg and SHA-256 as the native default.
Native checks `bot_preload_default_sha256` against the loaded default map,
alongside the manifest checks. The explicit helper interface can still take an
observed default size. The caller supplies existing private IPC and lifecycle
directories and closes the bundle on failure or after verified child exit.
Pass exactly `bundle.pass_fds()` to the native subprocess only; regenerate all
inputs on restart. The native consumer must
independently validate the manifest, descriptors, seals, digests, default map,
cgroup ancestry and conservative memory admission. Accounted bytes are not a
hard RSS guarantee.

The AMP and standalone Docker launcher therefore sets `--cgroupns host` on
Linux cgroup v2. Native admission requires the full cgroup hierarchy, including
finite ancestor limits; a private namespace exposes only a subtree and is
rejected. The cgroup filesystem stays read-only, the PID namespace stays
private, all capabilities remain dropped, and the configured memory/PID limits
still apply. This flag provides resource metadata, not host process execution.

`_cycle_permits_native_spawn` now permits the ordinary automatic catalog backend
only with prepared, matching cycle configuration and an exclusively owned
session without unresolved state; it is no longer an unconditional feature fuse.
The controller provisions the managed root and journal-bound IPC, writes the
launch claim before spawning, and binds the startup session to its owned child
and sealed startup manifest. It initializes an empty per-session catalog queue,
consumes catalog-bound close v2 and drives requests from a fresh observation read from the
owned child. The adapter separately checks the sealed max-games limit and
revalidates freshness before IPC or the reservation marker; a matching lobby
requires generation, request, index and counter. ACK alone is not a lobby proof.
A snapshot remains advisory; native serializes the final admission check
with lobby creation. Storage calls can still stall the event loop.

## Startup checks and remaining limits

`RuntimeBackend.prepare` validates and seals the entire cycle before reading or
locking the account profile and before creating native launch ownership. Invalid
later maps, IDs or AI approvals therefore fail before a launch claim on this
early path. The backend closes those temporary inputs, then validates and seals
again for the final launch snapshot. The early check does not freeze File
Manager edits: a map edit between that check and final preparation can still
fail closed after a claim has been written, leaving it for reconciliation.

* A durable inode-bound launch claim is recorded before `Popen` and retained
  after crashes, including failures before session binding. Reconciliation is
  not implemented: a claim does not prove that its writer exited, and an empty
  journal is not that proof. Legacy sessions without claims remain ambiguous.
  IPC is named for the journal (`ipc-<journal-name>`) and pinned by directory
  descriptor while owned; replacement blocks the cycle without killing native.
  A fresh private IPC directory is provisioned after the launch claim.
  Controlled stop/restart is supported under the trusted-GHost process-group
  contract. Crash recovery and unresolved queues remain blocked. Existing
  queues and nonempty IPC directories are deliberately refused. Catalog startup
  uses HOSTBOT_SESSION v2 to bind the pinned native IPC inode to the owned
  supervisor descriptor before attach. See [writer scope](cycle-recovery-writer-scope.md).
* Further test native admission under concurrent close/rehost, crash and load.
  A fresh lobby observation must match the owned reader, generation, request,
  index and counter; a staged ACK or an old stdout snapshot is not a lobby
  proof. Only a durable start advances the cursor; close v2 fences the pair.
* Validate publisher outage handling beyond offline tests. The native patch
  blocks new lobbies, removes its own stale intent when possible and retries
  an unpublish with bounded backoff; running games continue. If invalidation
  itself fails, remote withdrawal still depends on the relay heartbeat timeout.
  Existing advertisements cannot be synchronously withdrawn through a missing
  or untrusted path. Load and real realm behaviour remain unverified.
* Ordinary-backend close/rehost, crash/replay and traffic under load need further
  evidence. Successful sealed startup and normal rotation do not establish
  recovery from an interrupted launch or catalog queue. No missing event,
  absent PID or empty journal permits resetting pending state or representing
  lost native matches as still running.

Journal scans and durable IPC operations currently run synchronously in the
controller loop. Storage stalls and event-loop latency under load remain
unmeasured. Source configuration enables this path; a matching accepted release
image and operational acceptance remain separate work.

## Reported offline exercises

`tests/phase4/native-runtime-backend-e2e-test.py` passed in **91.158 seconds**
on the integration run reported for `48b85e9`. It uses production native GHost++,
the actual `RuntimeBackend` and `ICCupSupervisor`, public configuration and TCP
`6113`. Synthetic W3GS peers start A by normal votes and B by autostart, follow
normal countdowns, verify distinct counters/match IDs and capacity two, then
open repeated A after A ends while B continues exchanging actions. One native
PID is retained. There is no custom backend or spawn-guard override: only the
fixture binary path and StormLib location are remapped, and the realm is
disabled. Maps and peers remain synthetic; no Warcraft III engine, real client,
Star authentication or live iCCup session is exercised.

`tests/phase4/native-controller-capacity3-test.py` passed in **43.454 seconds**
on the reported integration run. A/B/C start with normal native vote/autostart
and countdown handlers, capacity three holds, A ends, and a fourth lobby opens
while B and C continue W3GS actions. This retains the custom backend and
spawn-guard override from the controller harness and accelerates lobby-creation
time. It is capacity evidence within that harness, not ordinary production
startup proof. These timings are reported results; this documentation task did
not rerun tests or build an image.

`tests/phase4/native-controller-cycle-e2e-test.py` runs the actual controller
loop against the native test driver linked from the merged patch. The harness
alone permits one native spawn, disables realm/Star and supplies a stdin control
pipe. Driver commands age the autohost timer and invoke `EventGameStarted`;
the native reservation check, durable journal and IPC are not bypassed.
Synthetic maps and W3GS peers exercise three distinct presets: start A, fence
and rehost B without advancing, start B, remain full, finish A and create C.
Both active connections exchange W3GS actions, including while the server is
full. The test verifies distinct native match IDs and durable start/close ACKs.

The companion `native-controller-countdown-e2e-test.py` uses the same owned
controller/native harness but forbids direct start and countdown manipulation:
synthetic W3GS players start A through `!vote` and B through autostart, observe
the normal countdown, exchange game actions, rehost B and exercise full
capacity before C. The harness still ages only the lobby-creation autohost timer.

These controller harnesses also run without a real realm or game engine. None
of these offline exercises proves SIGKILL recovery, unknown-crash reconciliation,
live discovery, engine compatibility or behaviour under operational load.


## Native capacity admission

A `staged` catalog ACK confirms durable receipt and an in-memory staged preset;
it is not a promise that a lobby exists or that capacity is available. On the
native event loop, catalog lobby creation checks the staged session generation,
manifest digest, request ID, catalog index, map metadata, preset settings and
fixed limit. It checks `m_CurrentGame` and both match limits again immediately
before constructing the lobby. A full server or occupied lobby is a normal wait:
the staged request remains in place, with no new ACK or request ID. A mismatch
in staged identity disables new games while already running games continue.

The offline controller race test delays a genuine owned-child state observation
until after a second game has started, then verifies that the third request is
staged only once, remains pending while native is full, and creates the matching
lobby after capacity returns. W3GS actions continue in both running games. The
test uses a test-driver start command and does not cover a normal countdown,
process-crash reconciliation or real realm traffic. The ordinary backend now
has a source launch path, but these harness-specific observations retain their
original scope.
