# Acceptance gate evidence — candidate under review

This document is an evidence checklist, **not** an acceptance gate. Candidate:
`sha256:f386a33cd646331d6f10d91cf475db661de1dcf303417b2f94923b4e9124ae09`.
Current source context has 39 files and digest
`31553cd96ca2fb4c70a6483f6f6096f77be593966505734e055cbbd12d169`, matching
the candidate's `war3.production.source-digest` label on 2026-11-01. The
map-discovery changes affect controller code outside the image context and have
separate tests. See [detailed test record](release-candidate-validation.md).

| Gate item | Evidence and remaining qualification |
| --- | --- |
| `clean_build` | **Incomplete:** independently rebuilt native binaries match byte for byte; helper matches after excluding PE timestamp and checksum. A no-cache Wine rebuild stalled on WineHQ, so a clean rebuild of every base is not established. The candidate uses a pinned, previously tested Wine base. |
| `unit` | Candidate ran 241 phase-3 tests, with two optional skips. The map-discovery controller subsequently passed eight focused tests, 74 account tests (two skips) and 25 standalone tests. |
| `integration` | 31 map/preset tests included the original Footman map. Offline cohort exercised the actual Star, relay and native supervisor together. No real-client match on this candidate. |
| `native_sanitizers` | Same native source and StormLib revision, separate ASan/UBSan build: 73 gameplay and 7 native publication tests passed. |
| `star_relay_mock` | 152 relay and protocol tests passed without external network. |
| `offline_start_stop` | Real Star cold-start, persistent-prefix restart and orderly stop passed in the candidate with synthetic profile and operator-provided game metadata, no external network. |
| `component_recovery` | Offline supervisor cohort recovered from Star, relay and native SIGKILL; realm recovery preserved the native PID. |
| `persistence` | Offline full supervisor restart and operator stop/start preserved the SQLite inode. |
| `no_zombies` | Offline soak reported zero zombies during runtime and none after stop; cohort reported no leftover processes. |
| `no_engine_execution` | Offline soak and cohort observed no game-engine execution; this does not establish behaviour outside the tested runs. |
| `secret_layer_audit` | 21 layers passed path-based asset/profile checks; a separate scan of 15,453 files against values from the operator-provided account file found no matches. Scans are bounded to the checks described. |
| `offline_soak` | The exact candidate completed a requested 900-second offline soak, 1013.478 seconds total, with no OOM. |

Actual iCCup authentication and a real-client match were not performed on this
candidate. Neither this checklist nor a build receipt asserts that those
online checks were passed. Original temporary test logs and proprietary inputs
were removed after summarizing results. A human or evidence-based release
process must decide how to resolve the incomplete `clean_build` item before
issuing any gate or identity.
