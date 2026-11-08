# G1: classic native action bridge, no LLM

Status: **experimental implementation; real-client gate pending**. Production AMP is untouched. This is a narrow spike for the authored AI Director G1 map, not a production API, provider worker, SDK, bot takeover or general replay decoder.

## What exists

- Map-to-host: classic `SyncStoredInteger` actions (`0x6b`), cache `AIG1.w3v`, mission `g1`, keys `tick`, `digest`, `ack`.
- Host-to-map: synthetic trigger-chat action (`0x60`, two little-endian uint32 receiver fields and NUL-terminated text) queued in the existing `CBaseGame::m_Actions` queue with the existing colour-0 human PID. It follows normal action aggregation and replay recording. No slot is reserved and no fake sender is allocated.
- Fixed payload: `~g1:<host_counter>:<sequence>:<source_tick>:<kind>`, kind 1 move to `(512,0)`, kind 2 hold. No arbitrary coordinates, unit handles, orders, JASS, strings or remote API input.
- Immutable per-match session. Whole action batches are boundary-decoded before side effects; client-origin reserved downlinks are rejected before forwarding, including loading/lag/retired state. Unknown or malformed actions are rejected only in the explicitly active pinned fixture. Ordinary maps and mode 0 retain their old path.
- The map alone chooses the map-owned neutral-passive actor, validates ownership/liveness/epoch/increasing sequence/five-second simulation lease, then orders it on every peer. Every peer emits ACK plus an integer digest covering sequence, applied count, kind and sampled actor position. The host logs PID, sequence and digest. An ACK means only that peer reported application, not proof of correct simulation.
- At most one command per two simulation seconds, one unacknowledged publisher command until ACK/expiry. No wall-clock retry. Publisher departure retires the session; the map falls back to hold. Automatic publisher failover is deliberately absent.

## Build and configure an isolated test

Use the toolchain image from `docker/g1/Dockerfile` and staged pinned open-source sources. Run `tests/g1/build-native-tests.sh` inside a non-root, resource-limited, read-only-root container with `--network none`, writable own repository build bind and executable private `/tmp` tmpfs. See `tests/g1/README.md`. No account/profile, Docker socket or game-engine mounts are needed for these checks.

The fixture is in the separate private maps repository: `maps/ai-director-g1/`. Build there with the standard map compiler, then hash the exact final `.w3x`. Only that SHA may be enabled:

```ini
# Native ghost.cfg, for the isolated G1 test only. 0/off is the default.
bot_g1_bridge_mode = 1
bot_g1_map_sha256 = <sha256 of exact G1 release>
```

Mode 1 only observes calibration, never injects commands or rejects unknown actions. Load the fixture with a stock 1.26a client in colour 0 and type `g1:hello`. Save the original action capture and the typed calibration line:

```text
[G1] calibration receiver_a=<uint32> receiver_b=<uint32> action_hex=<bounded known hello action> host_counter=<uint32>
```

**These fields must be observed from actual native traffic.** In manual binding, handle IDs are not a substitute for this traffic capture. Synthetic test values 17/23 are deliberately arbitrary. Capture twice with fresh matches and record whether mapping is stable; do not blindly reuse a previous map/release's pair. Active mode additionally requires the same pair in its in-game `g1:hello` handshake:

```ini
bot_g1_bridge_mode = 2
bot_g1_map_sha256 = <same exact release sha256>
bot_g1_receiver_a = <observed decimal uint32>
bot_g1_receiver_b = <observed decimal uint32>
```

An unmatched map SHA disables the experiment for that game. Invalid enabled settings fail native initialization. Configure active mode only after real calibration. There are no AMP settings and no production deployment in this G1 task.

For the isolated LAN single-client procedure, see [the LAN smoke instructions](ai-director-g1-single-client.md). The exact-hash fixture can auto-start with one human; ordinary maps retain their normal minimum/opponent checks.

## Acceptance record

One genuine 1.26a client is enough for a **single-client smoke**: map loads, handshake receiver pair is captured, footman alternates move/hold, sequences increase, ACK/digest arrive, a publisher disconnect/host stop results in hold after five simulated seconds. Preserve map/binary/config hashes and sanitized wire/replay evidence. A missed/expired command must not block later increasing sequences. Pause must suspend the simulation lease, not consume five seconds of wall time.

Full G1 additionally needs **two independent stock 1.26a clients** in the same match: compare digest for every sequence, run >=10 minutes including pause/resume, lag/recovery and replay playback on both clients, verify no desync, and prove all twelve human rows remain usable with the scripted neutral actor present. Synthetic W3GS peers do not count. Repeat against two concurrent CGame sessions and a fresh/rehosted match to rule out identity/receiver leakage. Invalid/stale/duplicate commands and publisher departure must not order a human-owned unit. Capture action offsets/receiver fields and record exact client build `1.26.0.6401`.

The local classic compilation inputs have recorded hashes but do not yet have independently established installation provenance. Do not claim the standard libraries are proven exact 1.26a solely because pjass accepts them.

`tests/g1/acceptance.json` distinguishes automatic checks from these pending gates. No G1 GREEN claim until they are complete. Changing receiver transport or relaxing the boundary parser requires new wire evidence and tests; unsupported G1 opcodes must not be guessed or substring-scanned.

## Wire references and implementation provenance

Original bridge/session/parser/test implementation, Apache-2.0, authored here. Wire layout facts were cross-checked against:

- [th-ghost `ghostw3hmc.cpp`, 63df93c](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/ghostw3hmc.cpp), a historical GPL implementation. No source or HTTP/LLM worker code is imported; in particular its byte-scanning decoder is not used.
- [w3gjs `ActionParser.ts`, 8ccf546](https://github.com/PBug90/w3gjs/blob/8ccf5463098f1dc88b6938f6fd7483d36352b117/src/parsers/ActionParser.ts), for action payload boundaries. Our parser is intentionally restricted, not a complete replay parser.
- The repository's pinned Apache GHost source, for CIncomingAction ownership, queue aggregation and replay storage.

The fixture uses its own terrain/JASS/assets, not a redistributed Warcraft installation or protected user map.

## Stock-client diagnostic experiment

The first real-client attempt with fixture 0.1.1 loaded and produced a genuine
hello action (both receiver fields 821). One reverse command reached the native
queue/replay, but no ACK arrived; the server observed a remote TCP close at
6.4 simulation seconds. The operator reported a stationary visible footman
and no intentional exit. This is **not a successful round trip**.

The next candidate coalesces generated downlink bytes into the publisher's
original bounded action record, rather than adding a second same-PID record.
Native serialization recalculates packet CRC and replay records the merged
bytes. Oversized batches remain unchanged and command retry waits for the
existing simulation expiry. Duplicate-PID rejection is an **unproven
hypothesis**, so this is an A/B transport experiment, not a confirmed repair.
Fixture 0.1.2 adds display-only startup/hello/command/rejection diagnostics;
actual guards, orders and ACK logic are unchanged. A fresh stock-client test
must distinguish callback nondelivery from explicit map rejection. No reserved
slot, synthetic client ACK or production deployment is used as a substitute.

## Explicit reserved-sender positive control

Mode 3 is a **separate control profile**, not the slot-preserving solution.
It is still off by default and requires an exact map SHA plus a real publisher
hello matching the configured receiver pair. On that map only, native creates
one ordinary fake-human sender in the initially open human colour-11 row.
No other row may be replaced to create it. Its action uses that participant's
PID; the real publisher remains colour 0. The matching 0.1.3 fixture explicitly
accepts triggering player 11 and visibly labels its effective capacity:
**eleven real humans, one reserved human slot**. Full twelve-human acceptance
is NOT PASSED. A full control lobby rejects overflow, including owner/reserved
joins, before selecting any occupant for replacement.

This is grounded in the historical source's explicit [fake-player sender
assignment](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/ghost.cpp),
not proof of this implementation's client compatibility. Both real-PID
experiments (separate and coalesced records) have produced zero real ACKs so
far. The control is intended to isolate sender identity; it must not silently
reduce production capacity or be promoted as the final G1 design. No DLL,
LLM/provider, Star/iCCup account, AMP or VPN is used in this LAN experiment.


## Receiver calibration and automatic wire probe (fixture 0.1.4)

A real 0.1.4 run disproved converting the JASS event handle's low word into
its wire receiver. The event handle and the known genuine chat receiver did
not match. Native correctly refused actor commands. `GetHandleId` metadata is
diagnostic only; it is never used to derive the command address.

Capture the full 0x60 receiver pair with the real client playing colour 11 in
a capture-only match of the exact map, with no fake player. Capture mode now
accepts diagnostics from every human colour and logs the colour alongside the
known hello action. Manual active authority remains restricted to colour 0.
The historical implementation likewise requires calibration while playing as
its configured sender; its `tid1`/`tid2` configuration names mean the low/high
bytes repeated into both DWORDs, not two separate receiver IDs.

Only explicit mode 3 accepts `bot_g1_receiver_binding = wire_probe`.
After rehosting with a fake sender at colour 11 and the real publisher at
colour 0, protocol version 2 binds the publisher automatically. Native uses
only the configured, actually captured receiver pair for a harmless
sequence-0/kind-0 probe under the fake sender PID. No actor order is enabled
until the matching publisher probe ACK arrives on a later simulation tick,
in a later action batch, within twenty ticks. Expiry is checked against the
whole incoming batch, including ACK-before-tick ordering. A timeout disables
commands for that game. This gate checks cross-match reuse; such reuse is not
assumed merely because map bytes are unchanged.

The map timer emits metadata, tick, then deferred probe or digest/command ACK.
World changes remain synchronous on all peers. Manual binding remains the
default and other maps remain untouched. Synthetic probe tests do not prove a
real callback, order application or desync safety. Full capacity remains eleven
real humans for this control profile. No actual command ACK has passed yet.
