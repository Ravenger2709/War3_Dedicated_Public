# G1 slot-free channels: autonomous review

Status: **bounded review complete; no new engine-validated bridge**. Date: 2026-11-08.
The operator requested no further participation and a stop after the available
safe, practical options were reviewed. This pass executes no Warcraft engine,
changes no native runtime/map code and starts no public LAN service. AMP, VPN
and normal desync checks are unchanged.

This is coverage of the twelve families below under the current stock-1.26a,
local CI/CD and unchanged-client constraints, not a proof about every possible
undocumented engine technique. Where a real engine is necessary, the result is
explicitly unresolved. Synthetic peers are not an engine substitute.

## Coverage and decisions

| Family | Autonomous evidence / earlier result | Decision for this pass |
|---|---|---|
| 1. Inject trigger-chat actions under real player PIDs | Earlier self/broadcast attempts lost connections; cross-only produced native desync. See [recorded trials](ai-director-g1-dual-probe.md). | Failed routes remain failed; no repeated trial or checksum relaxation. |
| 2. Classic gamecache synchronization as a downlink | Classic declarations and historical cache consumers exist. Uplink works; direct inbound `0x6b/0x6c/0x6d` is a distinct, untested engine family. | Retain as an unresolved candidate. Failure of `0x60` does not prove cache injection impossible. |
| 3. Lobby virtual host, observer, neutral PID, existing AI row | Native test proves the unslotted virtual host is removed before countdown end. Existing observer/AI rows still count within the map's twelve rows. Neutral/unslotted gameplay routing is not established. | No invented thirteenth participant or changed AI/controller flags. No twelve-human claim. |
| 4. Ordinary server/peer chat rather than game actions | Native `!help` response delivers `0x0f` text without host-injected actions. Tinker defines text and synchronized actions separately [P1]. | Packet delivery passes; whether a real client produces a usable JASS callback is unassessed. Text display alone is insufficient. |
| 5. Player names, handicap, colour/team or slot metadata during play | Player APIs exist; our `SendAllSlotInfo` skips loading/loaded games. No supported read/update path was established. | Do not repurpose player identity or gameplay settings as a mailbox. |
| 6. Ordering genuine cache markers plus a sync barrier | New bounded model chooses the order of two already-ready, unchanged action records, broadcasts identical bytes and preserves each PID's FIFO. Legacy author code motivates arbitration research [P2,P3]. | Model only. Cache winner, read/barrier timing, self-sync and a genuine relay ACK remain unverified. No runtime integration. |
| 7. Initial HCL, seed, map content or replay data | HCL/map policy can configure startup; replay is an output/history path. | Useful startup mechanisms, not a live reverse channel for new host decisions. |
| 8. Latency, pause or tick intervals as a carrier | Packet intervals are visible to protocol code; JASS-readable arrival/wall-clock bits are not established. Tinker warns of effects on time progression [P1]. | Not a practical safe carrier; leave time, pause and sync behavior unchanged. |
| 9. Preload/gamecache files on the client | Classic file APIs exist, but CI and the player's filesystem are different machines. | Requires client setup/read/reload verification; no installation, file serving, registry change or executable-text RPC. |
| 10. Client helper / genuine engine publisher | A local helper could supply values to a genuine client, which then synchronizes them. No such authorized, ready automation is available here. | Different client deployment footprint; no helper, GUI automation or engine installed in this pass. |
| 11. Modern Blizzard sync or Dz/Uj native providers | The inspected classic declarations lack the modern names; modern implementations explicitly target different native providers/versions. | Outside the unchanged 1.26a target. Declaring an API does not install it. |
| 12. Deterministic JASS policy or precomputed decisions | Shared script logic can execute decisions already present in the map. | Useful AI fallback, but does not complete a live host-to-map bridge. |

Details and primary sources for engine/cache/files/native-provider alternatives
are in [the engine-facing catalogue](ai-director-g1-engine-channels.md).

## New checks actually executed

- `test_native_channel_surfaces.py`: two actual-CGame wire checks, standard text
  routing and virtual-host departure. Peers are synthetic and send fixed-zero
  keepalives. No JASS callback or world-state agreement is inferred.
- `test_order_channel_model.py`: twenty-three model checks. Records remain exact,
  per-PID order survives, all recipient packets/CRC/intervals agree, and missing,
  coalesced, replayed or backlogged markers cause refusal without holding actions.
- A read-only container inspected nineteen selected declarations in the cached
  classic `common.j`. Its SHA256 is
  `9cb04da1c75dd30025d96fec12b4302c2f6a1f1a8c864b66d6cf52c65ecde924`.
  Exact installation provenance remains unverified; declarations are not execution.

These are **25 passing new tests**, not a new G1 GREEN. The research model is in
`tests/g1/order_channel_model.py`, outside production code. Its CLI prints
`MODEL_ONLY_ENGINE_NOT_RUN`. No native rebuild was necessary.

## Ordering candidate: useful limit

The model only reorders an exact adjacent head pair of genuine marker records
already available in the same ordinary aggregation tick. It creates no action,
changes no payload or clock, introduces no waiting queue and never searches
past ordinary user input. Thus busy/coalesced input can erase an opportunity;
this is an opportunistic model, not a reliable low-latency transport.

The demonstration frame is limited to 64 data bytes and 576 bit rounds. No
throughput is established. A conservative third genuine observer is model
policy, not a proven engine requirement. First-winner versus last-winner
arbitration, local sender behavior and a synchronized read/relay gate must be
resolved before applying any actor effect. Neither a passing CRC nor a Python
decoder supplies that proof.

## Stop condition

All twelve families have a recorded outcome for the checks available without
operator/engine participation. Direct inbound cache and genuine-marker ordering
remain research candidates; ordinary text also lacks callback proof. This pass
does not label them impossible or working. Unsafe identity/timing changes,
sync-check bypasses and client upgrades are excluded rather than tried.

The confirmed service-slot control remains the only engine-validated bridge in
this workspace (eleven real-human places). Production promotion is refused.
No further automatic trial or operator question is scheduled after this review.
The task-owned failed LAN runtime is retired; current source/build inputs and
private logs/replays/receipts are preserved.

## Primary references

- **[P1]** [Tinker protocol definitions](https://github.com/Strilanc/Tinker/blob/master/Warcraft3/Protocol/WC3%20Protocol.vb): `ServerPackets.Text`, `Tick`, `ClientPackets.GameAction`, `Tock`; source-author protocol documentation, not an independently recovered engine implementation.
- **[P2]** [Xarian's cache-sync experiments](https://www.hiveworkshop.com/threads/a-really-really-ridiculously-easy-way-to-sync-data-new-gethost-command.114475/): original code and explicit uncertainty about barrier internals; it does not establish a universal winner rule.
- **[P3]** [Original GetHost code/discussion](https://www.hiveworkshop.com/threads/about-host-detection-and-local-player-code.94709/): genuine cache synchronization and warnings about the selected responder.
- Native implementation pins are in `third_party/ghost.lock.json`; relevant symbols are `CBaseGame::SendAllSlotInfo`, `EventGameStarted`, `CreateVirtualHost`, `CreateFakePlayer`, and `CGameProtocol::SEND_W3GS_CHAT_FROM_HOST` / `SEND_W3GS_INCOMING_ACTION`.
