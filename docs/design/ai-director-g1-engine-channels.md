# G1 classic engine channel catalogue

Bounded read-only review, 2026-11-08. Target: unchanged TFT **1.26a / 1.26.0.6401**.
Scope: JASS-facing alternatives to a reserved service slot; no operator participation in this review.
This document adds no implementation, client helper, network service, engine execution or deployment.
The stopped LAN runtime is not started; AMP, VPN and normal checksum/desync checks remain untouched.

## Classification and evidence boundary

| Label | Meaning in this catalogue |
|---|---|
| `AVAILABLE_AUTONOMOUS_CHECK` | Source inspection, bounded parser/model or static fixture review is possible without Warcraft. This is not an engine PASS. |
| `NOT_A_LIVE_CHANNEL` | Useful configuration/computation, but supplies no new CI-originated decision during an existing match. |
| `OUTSIDE_UNCHANGED126A` | Requires a different client/native provider or a newly installed client-side component. |
| `NEEDS_REAL_ENGINE_PENDING` | Plausible API/family, but stock-client delivery, observation or synchronization remains unverified. |
| `UNSAFE_FOR_GAMEPLAY` | The proposed carrier changes gameplay/identity/timing or crosses an unbounded execution boundary; not authorized as a fallback. |

`NEEDS_ENGINE_VERIFICATION` below is an explicit alias of `NEEDS_REAL_ENGINE_PENDING` for inbound gamecache.
Availability of a native declaration proves an API surface, not arbitrary host-to-engine injection semantics.
An author's own experiment is primary evidence of that experiment, not an independent 1.26a reverse-engineering result.
Synthetic peers, native parser acceptance and replay bytes cannot demonstrate JASS execution or peer agreement.
Warsmash, if used later, would be a different engine; its behavior must not be promoted to stock Warcraft proof.

## Families and bounded next checks

| Family / exact surface | Evidence and direction | Classification / remaining boundary |
|---|---|---|
| Existing gamecache telemetry: `StoreInteger`, `SyncStoredInteger`, `GetStoredInteger` | Classic declarations [L1]; G1 `war3g1::decode` parses cache/key/value at `0x6b`; `CStatsW3MMD::ProcessAction` is an independent historical consumer [S4]. Map → host observation already has implementation evidence. | `AVAILABLE_AUTONOMOUS_CHECK`: framing, key/epoch limits and genuine source ownership. This uplink alone does not implement a live downlink. |
| **Host-originated gamecache integer/real/boolean/string synchronization**: `0x6b/0x6c/0x6d`, `SyncStoredString` | [L1] declares the family; current G1 decoder recognizes the first three, not the string family. [S1] describes host arbitration of genuine cache sync traffic. Host-injected values are a separate native family from failed `0x60` chat. | **`NEEDS_ENGINE_VERIFICATION`**. Bounded source/parser checks are available, but stock inbound acceptance, echo/arbitration, string support and the map's subsequent read gate are unproved. No opcode guessed for strings. |
| Genuine cache synchronization coordinated by `TriggerSyncStart` / `TriggerSyncReady` | Both take no payload [L1]. Xarian's `PlayerBroadcastStoredInteger` and `PlayerForceStoredInteger` are primary code [S1]; YDWE `YDWESync.Join` combines the barrier with an actual stored-integer sync [S2]. | `AVAILABLE_AUTONOMOUS_CHECK` for control-flow/model review; `NEEDS_REAL_ENGINE_PENDING` for exact arbitration/read timing. A barrier can coordinate payload sync, but is not itself a new CI value source. |
| Preload file output: `PreloadGenClear/Start`, `Preload`, `PreloadGenEnd` | Present in [L1]. Existing FileIO-based save/load code requires client-side file loading configuration [S3]. This concerns the computer running Warcraft, not the container running GHost. | `AVAILABLE_AUTONOMOUS_CHECK` for generated-text escaping/bounds; `NEEDS_REAL_ENGINE_PENDING` for filesystem behavior. Output on a remote player's disk is not automatically available to CI. |
| Preload file input: `Preloader`, `ReloadGameCachesFromDisk`, `InitGameCache` | Classic APIs [L1]. `blizzard.j::MeleeStartingUnitsHuman` invokes a bundled `.pld`; [S5] describes executing preload scripts and asynchronous loaded data. Disk reload/campaign cache is not a documented network file-transfer endpoint. | `NEEDS_REAL_ENGINE_PENDING`. A CI file cannot become a client-local file merely by sharing a path string. No helper-free live downlink is established. |
| Installed client helper + constrained preload mailbox | A helper could write on the publisher's PC and send its bounded observations to CI; this is a proposed architecture, not supplied classic API behavior. Map → other peers would still need genuine synchronization [S1,S3]. | `OUTSIDE_UNCHANGED126A` operational footprint (even if the EXE stays 1.26a); engine loading/reload gate remains pending. No helper, file sharing, registry or remote access is installed here. |
| Executable/preload text treated as arbitrary JASS/RPC | [S5] describes a distinct preload execution context and native-based transfer techniques. It does not establish unrestricted access to map globals or safe arbitrary-code evaluation on 1.26a. | `UNSAFE_FOR_GAMEPLAY` as an arbitrary-code interface. A future design would require a fixed serializer/template, integer-only schema, bounded path and explicit client installation review; none is implemented. |
| Modern Blizzard sync: `BlzSendSyncData`, `BlzTriggerRegisterPlayerSyncEvent`, getters | [S5] annotates this API family at **1.31.0.11889**; the independent Wurst implementation explicitly targets 1.31+ [S6]. The inspected classic declarations [L1] lack it. | `OUTSIDE_UNCHANGED126A`. Importing newer declarations does not install their implementation. Do not conflate “Classic” 1.31 with the target 1.26a. |
| Platform/client extensions: `DzSyncData`, `DzTriggerRegisterSyncData`; UjAPI `SendSyncData` | Concrete provider-facing declarations exist in [S7,S8]. They are distinct from the classic cached API, regardless of prefix similarity. | `OUTSIDE_UNCHANGED126A`. No exact supported Dz provider release/patch matrix was established here; do not claim that every Dz/Uj implementation supports 1.26a or iCCup. |
| Deterministic precomputed policy inside JASS | Classic `GetRandomInt/Real`, timer and unit APIs [L1] can run a fixed map policy with shared state/seed and bounded work. The decision inputs would already be inside the map. | `AVAILABLE_AUTONOMOUS_CHECK` for deterministic logic/budgets; **`NOT_A_LIVE_CHANNEL`** for new host decisions. Could supply useful autonomous AI if live control is deferred. |
| Packet latency/interval or pause as bit carrier | `CBaseGame::SendAllActions` advances `m_GameTicks` by `m_Latency`; packets carry an interval. Classic `TimerGetElapsed` is a timer API [L1], not a declared wall-clock/packet-arrival API. | `UNSAFE_FOR_GAMEPLAY` as a proposed control carrier; `NEEDS_REAL_ENGINE_PENDING` for observability. Delaying delivery alone does not prove a deterministic JASS-readable bit; changing intervals/pause alters simulation timing. |
| Initial HCL over occupied-row handicaps | `CBaseGame::EventGameStarted` encodes `m_HCLCommandString` then calls `SendAllSlotInfo`; matching map code must decode and restore original handicaps [S9]. One supported character per occupied row; no extra row is inherently required. | `AVAILABLE_AUTONOMOUS_CHECK` for encoding/preservation; **`NOT_A_LIVE_CHANNEL`**. Good startup configuration family only with a cooperating decoder; enabling it on arbitrary maps can change gameplay handicaps. |
| Player names, handicap, colour/team/controller or slot metadata during play | `Set/GetPlayerName`, `GetPlayerController/SlotState`, `Get/SetPlayerHandicap[XP]` exist [L1]. Local host `SendAllSlotInfo` explicitly skips loading/loaded games. A JASS setter needs an existing execution/input route. | `NEEDS_REAL_ENGINE_PENDING` for any proposed midgame network update; `UNSAFE_FOR_GAMEPLAY` if it repurposes names, teams, controller or handicap without exact preservation/restoration. No accepted midgame shortcut is established. |
| Reuse an already required AI/computer row as synthetic sender | Could avoid an **additional** service row on an AI map; still consumes one of twelve rows, so it is not twelve-human capacity. `CreateFakePlayer` currently obtains an open row and writes a human-style computer=0 row [L2]. | `NEEDS_REAL_ENGINE_PENDING`, not a validated autonomous safe candidate. Classic computer flag + network PID + JASS controller/AI compatibility is unverified. Never “fix” routing by turning the real AI's computer flag off. |

## What the primary sources do and do not establish

[S1] reports experiments with genuine sync traffic, latency cycles and first-arrival arbitration;
it also explicitly limits certainty about barrier packet mechanics. These observations motivate research,
but do not establish “last cache write wins”, current 1.26a behavior, or host injection acceptance.
The earlier GetHost discussion likewise warns that fastest response need not identify the host [S10].
The requested Integer Sync Tool page was blocked with HTTP 403 during this review [S11]; its code was not assessed.
No claim here relies on treating an inaccessible attachment or a secondary summary as executed evidence.

Tinker's own-action echo constraint and W3HMC's choice of cache uplink plus fake-chat downlink
are implementation clues, **not proofs that inbound cache synchronization cannot work**.
The failed self/dual `0x60` experiments therefore do not close the cache-native family.
Use [the existing dual-probe record](ai-director-g1-dual-probe.md) for those actual results;
ordinary chat, unslotted virtual-host packets and packet-specific alternatives belong to the separate main audit.

The separate order-only model is also outside this implementation scope: genuine SDK markers,
reordering without byte mutation, identical broadcast and per-PID FIFO, with a conservative third-observer policy.
Whether the engine actually requires that extra observer role remains unverified.
This catalogue neither prototypes it nor asserts a cache winner/read behavior from its offline model.

## Convenience, safety and recommendation

For an **unchanged client with no operator setup**, the most relevant unresolved non-chat family is
classic inbound gamecache synchronization/arbitration: an existing classic payload API, no disk helper,
no required service row in its declaration. This is a **research priority, not a working transport**.
Its first autonomous step can be an evidence/model audit; its engine read/echo/arbitration gate stays pending.
Do not merge a speculative receiver into production or label native fake-peer tests as engine PASS.

For **startup-only configuration**, HCL or an exact-hash precomputed policy is simpler to reason about.
Preserve occupied rows, computer flags, player identity and original handicap values; a cooperating map is mandatory.
Precomputed policy is the safest useful fallback when ongoing external decisions are not essential,
but cannot be presented as completing the live bridge requirement.

For **live external control after explicit client setup**, a bounded local helper is a distinct future option.
A helper must run where the client files exist, not merely on CI. One publisher's local data must be
validated and genuinely synchronized before any shared actor effect; restore any scratch state used for transfer.
Do not use executable text supplied by players/providers as the data format or promise unrestricted JASS loading.
Local-files availability, permission, reload/caching, crash behavior and multiplayer determinism need exact-client tests.
Modern-native caching/error notes in [S5] are not evidence of those same behaviors on 1.26a.

A timing carrier is not recommended: simulated time and wall-clock arrival are different observations.
A parser can prove the interval bytes changed, not that map JASS decoded a bit without affecting the game.
No pause/latency changes, checksum relaxation, slot/controller changes or AI replacement are authorized by this review.

The bounded catalogue is complete. No operator request or runtime experiment follows automatically.
Only this document is added; no build is needed and no engine-proof claim is made.

## Evidence index (primary code / author observations)

- **[L1]** Maps worktree read-only `.build/jass/common.j`, SHA256 `9cb04da1c75dd30025d96fec12b4302c2f6a1f1a8c864b66d6cf52c65ecde924`: `TriggerSyncStart/Ready` 1353–1354, gamecache 1797–1820, player APIs 895–907/1697–1700, timers 915, preload 2407–2417. Exact installation provenance remains unverified. Same cache's `blizzard.j::MeleeStartingUnitsHuman` uses bundled `Preloader`, around line8245.
- **[L2]** Current host source `vendor/ghostplusplus/ghost/game_base.cpp`: `CreateFakePlayer` (~5052), `GetEmptySlot(bool,bool,bool)` (~3993), `SendAllSlotInfo` (~1259), `SendAllActions` (~1319), `EventGameStarted` (~3475). Local symbols are authoritative for our implementation; line numbers can move.
- **[L3]** Current `src/hostbot/native_g1_bridge.h::war3g1::decode` recognizes cache `0x6b/6c/6d`; this decoder is not a stock-engine implementation. `tests/g1/replay-audit.py` provides structural parsing, not playback evidence.
- **[S1]** [Xarian's original experiments and `PlayerBroadcastStoredInteger` / `PlayerForceStoredInteger`](https://www.hiveworkshop.com/threads/a-really-really-ridiculously-easy-way-to-sync-data-new-gethost-command.114475/), 2009. Source-author observations, not a verified build-6401 contract.
- **[S2]** [YDWE `YDWESync.j`, `Join`, pinned a69c315](https://github.com/actboy168/YDWE/blob/a69c315ebf2886ec54d66200707ab6e38bd3484d/Development/Component/ui/ydwe/jass/Util/YDWESync.j): stores a value and invokes sync/barrier natives. This is library code, not the engine implementation.
- **[S3]** [Nestharus Codeless SaveLoad, pinned d27426d](https://github.com/nestharus/JASS/blob/d27426d8b32a2e0bcbeccc33a9d1ee44b39ec77f/jass/Systems/Codeless%20SaveLoad/post.txt), `SaveFile`/`LoadFile` dependencies and loading-enabled checks (~1649–1672). Establishes client setup requirements in that system, not every 1.26a FileIO implementation.
- **[S4]** [GHost++ `statsw3mmd.cpp::ProcessAction`, pinned 6ba90f4](https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/statsw3mmd.cpp): consumes map-emitted cache telemetry. Its scanner is not our trusted framing decoder.
- **[S5]** [Jassdoc `common.j`, pinned 509c14d](https://github.com/lep/jassdoc/blob/509c14dd0f23e6847516b8a813478974268e9f44/common.j): `TriggerSyncReady` (~15951), `PreloadGenEnd`/`Preloader` (~26035/26091), `BlzSendSyncData` family (~28395), patch annotations. Community-authored API documentation; modern observations are version-scoped, not stock-1.26a proof.
- **[S6]** [Wurst `SyncSimple.wurst`](https://github.com/wurstscript/WurstStdlib2/blob/master/wurst/file/SyncSimple.wurst), module header and `BlzSendSyncData` calls explicitly describe 1.31+. Unpinned implementation viewed for version scope only; not imported.
- **[S7]** [JapiNative declarations, pinned f984e54](https://github.com/yefq/JapiNative/blob/f984e5450ee8f713ea560163acfb373e4b9a11b8/JapiNative.j#L497): `DzTriggerRegisterSyncData`, `DzSyncData`, data/player getters. Declaration evidence, not provider compatibility certification.
- **[S8]** [UjAPI native declarations, pinned 44aa9ed](https://github.com/UnryzeC/UjAPI/blob/44aa9ed985c0ea65caa91132d12c22550072b89a/uJAPIFiles/UjAPI.j), synchronization section around4986. A separate native provider, not unchanged classic `common.j`.
- **[S9]** [GHost++ `game_base.cpp`, pinned 6ba90f4](https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/game_base.cpp), `EventGameStarted` HCL encoding/restoration contract; compare current [L2] before adopting anything.
- **[S10]** [Original GetHost code and author discussion](https://www.hiveworkshop.com/threads/about-host-detection-and-local-player-code.94709/), 2008: source-level proposal with explicit limitations, not a reliable bot-host selector.
- **[S11]** [Integer Sync Tool author thread](https://www.hiveworkshop.com/threads/system-integer-sync-tool.218151/), access blocked in this review; recorded as an unassessed lead, not supporting proof.
