# Phase 4 acceptance evidence checklist

Updated 2026-11-02 for the local Phase 4 candidate with English MOTD and
separate periodic lobby reminders.
This checklist is not an acceptance gate or a release identity.
**No MAP CYCLE, MULTI-MATCH, ICCUP, AMP or GAMEPLAY GREEN is asserted.**
Earlier [candidate evidence](acceptance-evidence.md) and
[release records](release-candidate-validation.md) cover their own revisions.

Recorded offline results on this integration source:

* The packaged final candidate ran 288 Phase 4 tests (two optional skips) and
  329 Phase 3 tests (four optional skips), with no failures or expected failures.
  Separate relay and legacy runtime suites previously ran 155 and 28 tests.
* The actual production GHost binary, `RuntimeBackend` and `ICCupSupervisor`
  completed the ordinary-config test on the final candidate in 90.373 seconds:
  English command MOTD on join, separate periodic auto-start and voting notices
  without entering a command, vote-start A, auto-start
  B, both maps loaded, two concurrent action streams, capacity wait, A ends,
  repeated A opens at its next cycle position while B still exchanges actions.
  No spawn guard, backend or native timer was overridden. The test remaps only
  the binary/StormLib fixture locations and disables the realm; all TCP 6113
  traffic is local inside a network-disabled container.
* The separate instrumented controller harness completed the brief's three
  concurrent matches and fourth-lobby scenario in 43.454 seconds. Starts use
  votes/autostart and normal countdowns; this harness still supplies its own
  backend/spawn permission and ages only the lobby-creation timer.
* The ordinary packaged-backend scenario also held two concurrent matches for
  900 seconds on the earlier candidate, before the reminder timer change:
  848 action echoes, maximum echo latency 0.15 seconds, native descriptors
  stable at 21, RSS growth about 20 KiB. This does not certify the later timer
  patch or real engine gameplay.
* The merged native baseline ran 187 tests under ASan/UBSan (three skips),
  without sanitizer findings. The subsequent reminder patch rebuilt all GHost
  objects and ran its 12 focused tests in both release and ASan/UBSan builds,
  without skips or findings. The focused cases include fresh join/leave counts,
  3.5-second deferral, repeated reminders, disabled voting, countdown/game
  cancellation and uninterrupted actions in an older match.
* Local runtime candidate:
  `sha256:4c0d298a232d11eb43a88ee4714a22bea81efce61182d35967905c0c9f0845b5`.
  Native input:
  `sha256:47bb46f71bf7fe3394b657a51ed726d0b80de6f4f6bf0834c248ff299724bb80`.
  Build inputs were source-bound and excluded proprietary assets and profiles.
  All 21 historical image layers passed the prohibited-asset path audit; this
  audit does not claim an account-secret content scan.
  This candidate has no acceptance gate or release identity and has not been
  published or deployed.
* A bounded independent static review of startup, readiness, operator handoff
  and early cycle validation found no concrete P1/P2. It did not execute tests.

Paths below are repository-relative. Test paths without a directory prefix are
under `tests/phase4/`.

| Requirement | Available source evidence | Remaining evidence for release work |
| --- | --- | --- |
| AMP Map Cycle replaces Map preset; typed JSON, duplicates, 1–32 entries; match limit default 3, range 1–5 | `amp/war3-productionconfig.json` has the textarea and an active 1–5 limit. `test_cycle_config.py`, `test_map_cycle.py` cover parsing, bounds and aliases. | Operator source tests preserve full normalized cycles and validate every multi-cycle entry in the accepted image before merge. Verify live AMP behavior and the exact new release image; source tests do not certify either. |
| Validate every preset, map, capacity and AI approval before launch | `test_manual_map.py`, `test_preload_catalog.py`, `test_cycle_compile.py`; sealed map/manifest tests cover input binding and rejection of invalid later entries. | Ordinary startup now checks the full cycle before acquiring launch ownership, repeats sealed validation at launch, and native verifies default/catalog SHA256. Operator maps still need an exact-image check. Synthetic map acceptance is not engine compatibility. |
| Idempotent legacy migration without losing definitions or settings | `test_cycle_config.py` checks migration and unchanged preset definitions; `test_cycle_migration_crash.py` checks cursor migration crash points. [AMP migration](contracts/phase4-amp-migration.md) records operator precautions. | Verify backed-up existing AMP settings, File Manager edits and rollback compatibility with the SQLite cursor. |
| Round robin advances once on actual native start; close/rehost does not advance | `test_cycle_state.py`, `test_cycle_external.py`, `test_cycle_journal_integration.py`, `test_fenced_close.py`; native controller harnesses correlate durable starts and close-v2 records. | Retain run receipts for the merged revision and repeat through normal configuration, including duplicate IDs and restart/replay. A staged ACK or stdout phase alone is not a start. |
| One native host, isolated per-map settings, retained player sockets and reusable listener | `tests/phase3/native-next-preset-test.py`, `native-preload-ipc-test.py` and the controller harnesses exercise real native code with synthetic maps/W3GS peers. | Verify real maps, AI replacement/no-AI transitions, independent votes, countdowns, timers and replays. The ordinary backend test reuses actual loopback TCP `6113`; instrumented harnesses use `16113`. Neither establishes host/WAN admission or engine desync behavior. |
| Multiple loading/running matches, capacity wait and automatic continuation | `tests/phase3/native-multimatch-test.py` includes five-match admission. Controller cycle/countdown harnesses cover A/B active, full capacity, A ending and C opening while B relays actions. The ordinary-backend two-match soak recorded resources above. | The exact three-match A/B/C → full → A ends → D opens scenario passed with B/C action echoes in the instrumented harness. Verify resource behavior and slow-storage latency on the intended host. |
| Real backend owns startup, sealed descriptors and catalog dispatch | `test_production_catalog_glue.py`, `test_production_cycle_prerequisites.py`; [startup boundary](catalog-startup-integration.md). | Ordinary backend rotation/countdown passed as recorded above, without a launch override or substitute backend. Unknown history still blocks launch; exact-image, soak and realm coverage remain pending. |
| Cursor survives controlled restart; unknown crashes fail closed | `native-controlled-restart-test.py`, `test_generation_handoff.py`, `test_exit_reconciliation.py`; [controlled restart scope](cycle-recovery-writer-scope.md). | Controlled orderly stop is distinct from unknown-crash recovery. Missing start/close evidence leaves claims, selections and reservations unresolved; `reconcile_after_native_exit` is unavailable. No automatic clearing or restored running matches may be inferred from PID absence or an empty journal. |
| One Star/account/relay; one public lobby; STOPADV/chat return/ACK and discovery | Offline relay lifecycle coverage is described in [Star lifecycle](star-lobby-lifecycle.md); native IPC tests cover publication-path faults without stopping running actions. | Verify real iCCup authentication once, sequential different-map publication, STOPADV/chat return, normal list **and** exact lookup, no duplicates and new-client admission while old games continue. ACK alone is insufficient. |
| `!status`, HTTP and AMP monitoring; capacity is normal waiting | `test_cycle_monitor.py`, `test_native_match_telemetry.py`; [monitoring contract](cycle-monitoring-contract.md). Fresh owned snapshots expire; `CAPACITY_FULL` is not an error/restart trigger. | HTTP readiness now requires current bound-session proof and fresh counts; a lobby also requires matching publication, while running games need no advertisement. Verify live `!status` parity and AMP presentation. Per-match HTTP data is not verified AMP numeric Analytics ingestion. `finished_matches` counts process-local retired objects, including interruptions, not completed gameplay; counters are not durable historical totals. |
| Real clients load maps and play parallel matches without desync | Available operator setup has **one real Warcraft III client only**; historical single-client findings are recorded in the Star lifecycle document. | At least two simultaneous real-client matches, action relay and entry into a subsequent lobby remain unverified. Synthetic peers and one real client cannot supply this acceptance evidence. |
| CI, sanitizers, release image, AMP update and rollback | `.github/workflows/source-checks.yml` includes Phase 4 unit tests as well as focused Phase 3 checks. [Public candidate source checks](https://github.com/Ravenger2709/War3_Dedicated_Public/actions/runs/41462713601) passed, including privacy, syntax and unit tests. Local unit/native/sanitizer evidence is recorded above. | Final-image original-Star cold start/soak, actual iCCup publication, AMP Start/Stop/Restart, backup, settings/cursor persistence, update and rollback remain pending. No verified current Star input bundle was available for the offline final-image cohort. |

The native controller harness uses actual patched GHost++ code and W3GS
traffic, but synthetic maps/peers, a test driver, no Star and no realm. Its
direct-start variant invokes the native start transition through instrumentation;
`native-controller-countdown-e2e-test.py` instead uses votes/autostart and normal
countdown, while still accelerating the autohost creation timer and inheriting
the custom backend/spawn override. Neither is a normal production-backend,
actual Warcraft III engine or live iCCup acceptance run.

Release work must close the remaining matrix items against one final image
and record its exact source and run evidence. Keep unknown
crash recovery blocked for operator reconciliation; do not reset pending state
to manufacture progress. Local offline soak and sanitizer evidence does not
substitute for actual engine/iCCup, live AMP or deployment/rollback acceptance.
No Phase 4 router changes, public launches, image publication or deployment
were performed in these checks.
