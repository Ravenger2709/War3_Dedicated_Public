# G1 capacity: remaining experiment

Status: **two-client reserved-sender control passed; slot-free route unvalidated**.
Local CI/CD LAN only. No AMP, VPN, DLL, client upgrade, game engine on the
server or LLM integration is authorized by this experiment.

The mode-3 control has genuine sender-11 calibration, a fresh map probe
ACK and 317 matching command ACK pairs from two stock clients. It reserves one existing
human row. All twelve W3I rows still being human does not mean twelve people
can join this runtime. Earlier real-PID injection attempts did not pass; their
failure does not prove that the engine skips its own reflected actions.

## What the inspected sources support

The pinned [GHost implementation](https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/game_base.cpp)
announces a virtual host outside the slot table, then removes it before loading
and when the twelfth participant joins. This is not an established additional
gameplay sender. The existing slot table, including observer rows, is bounded
to twelve in the [map implementation](https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/map.cpp).
Neither path establishes network-PID routing to JASS neutral players.

The historical [W3HMC sender](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/ghostw3hmc.cpp)
uses a real fake-player PID and calibrated chat bytes. The classic
[action description](https://github.com/scopatz/w3g/blob/master/w3g_actions.txt)
does not explain the two DWORD receivers. Guessing an unslotted/neutral PID or
deriving those values from a JASS handle is not a supported repair. These source
limits are not a proof that every possible twelve-human transport is impossible.

## One bounded candidate: two genuine senders

This is a proposed experiment, **not implemented or accepted**. It requires two
independent real clients; synthetic peers cannot settle its engine behavior.

1. Use a fresh exact-hash fixture with unchanged human rows and no fake sender.
   Capture each selected sender's actual chat receiver pair from that release.
2. Send the same harmless probe through both genuine PIDs in fixed A-then-B
   order. The map records each raw callback's sender colour/epoch/sequence, then
   deduplicates the probe before accepting or exporting confirmation. Current
   sequence-zero handling must be adapted: it does not yet provide this dedup.
3. Require exactly one accepted probe and a later genuine ACK from **each**
   client within the simulation timeout. Keep actor orders disabled throughout.
4. Compare raw callbacks: A-only-B and B-only-A would be consistent with local
   action skipping; both seeing both would support deduplication without proving
   that explanation. Missing callbacks, disconnects or double acceptance fail.
5. Only after this probe passes, test actual orders, two-client digest agreement,
   replay playback and twelve-human admission separately. A successful probe
   does not pass those gates.

On timeout or failure, issue no actor commands or retries. Do not allocate a fake
sender, evict anyone or change roles in the running match. Return to the proven
reserved-sender profile only in a new isolated test match after participants
leave. Existing production templates and runtime defaults remain unaffected.

## Preceding one-client self-delivery experiment

The first slot-free candidate uses exactly one real client, colour 0, and no
fake gameplay sender. It tests self-delivery only; it cannot establish delivery
to another client's world. Global native mode 2 with `wire_probe` reuses the
existing internal probe state machine and merges the probe into the real
publisher's action record. The genuine captured colour-0 receiver pair is a
candidate, not a handle-derived identity or a successful fresh calibration.

Map 0.1.7 accepts sender colour 0 and retains all twelve human rows. Runtime
allocates no fake row. Before any actor command, a harmless sequence-zero probe
must cause a genuine subsequent map `probe` export within the existing 20-tick
window. Merely seeing the probe in native output, a replay or synthetic peer
frames does not satisfy this requirement. Timeout disables actor commands for
the session; there is no automatic retry or fallback to a reserved slot.

If self-delivery fails, record the missing probe ACK without declaring an
engine defect or treating sender-skipping as proven. The two-genuine-sender
experiment above remains a separate candidate requiring two clients. Twelve
synthetic admissions only check native capacity; twelve real-human execution
remains a separate gate.
