# Single-client G1 LAN smoke

This is an isolated CI/CD development instance. No production AMP, Star, iCCup account, VPN configuration, LLM service or Warcraft engine runs on the server. A real **TFT 1.26a / build 6401** client runs on the operator's LAN computer.

The proven pause/resume artifact 0.1.4 is the explicit **reserved-sender positive control**: use
native mode 3 with `bot_g1_receiver_binding = wire_probe`, lobby
`G1 Receiver Probe`, one visible `FakePlayer`, and eleven real-human places.
The twelve-human real-PID candidate has not passed. Configure only a genuine colour-11 wire capture for this exact map. A fresh
real map probe ACK must arrive before any actor order. JASS event handles
are not wire identifiers; that earlier mapping experiment failed.

## Prepare the host

Build the exact G1 source and private authored fixture first. Use an unused TCP port (the helper fixes 6114), a directly connected private LAN /24 address and its directed broadcast. Record the map/native/compiler-input hashes. Generate a new private session using:

```sh
python3 scripts/g1-lan-prepare.py --destination /private/session \
  --map /input/g1.w3x --lan LAN_IP/24
```

The generated config uses capture mode, no BNET server, no game executable, and no iCCup profile. It binds TCP only to that LAN IP. `udp_dontroute=1` and the /24 directed broadcast keep game announcements on the local subnet. Do not use VPN addresses or change routes, firewall, production ports or AMP configuration.

Use the same non-root UID/GID for preparation and runtime: the private session is
mode 0700. For example, pass `--user "$(id -u):$(id -g)"` to both containers;
using a different UID prevents access to `/data`.

Run the locally pinned toolchain container as a non-root user, read-only root, dropped capabilities, no-new-privileges, bounded memory/PIDs/CPU/stdout and `restart=no`. Use host networking for native LAN broadcasts. Bind these inputs only:

- current native build directory read-only at `/opt/g1`, `LD_LIBRARY_PATH=/opt/g1/lib`;
- new private session writable at `/data`, also working directory;
- exact current map read-only at **`/maps/g1.w3x`**, not its source directory;
- local classic `common.j` and `blizzard.j` read-only at `/data/mapcfgs/common.j` and `/data/mapcfgs/blizzard.j`.

Entrypoint: `/opt/g1/ghost++ /data/ghost.cfg`. There is no Warcraft installation/PE or profile mount. The already recorded standard-library hashes are used to calculate map checksums; exact 1.26a installation provenance remains unverified until established separately.

In an explicitly enabled, exact-hash G1 fixture only, native auto-start may use one human and a neutral actor. The ordinary-map minimum and computer-opponent checks are unchanged. The helper disables votes for this calibration session. Do not use a test-driver force-start to claim this path works.

## Verify transport before inviting the operator

Observe a native W3GS `GAMEINFO` advertisement from the LAN IP with version 26, exact lobby name and port 6114. From the same isolated test environment run:

```sh
python3 tests/g1/lan-smoke.py --server LAN_IP --broadcast LAN_BROADCAST \
  --exercise --expected-map-sha256 EXACT_SHA256
```

The synthetic probe downloads the actual map, compares SHA-256, waits for normal solo auto-start and receives gameplay action timeslots. It never sends `g1:hello`, fabricated callback pairs, digest or ACK. This is transport evidence, not engine execution. After it exits, confirm a new empty lobby is advertised before the real-client handoff.

## Operator: current automatic probe

1. Leave any previous G1 match. Open **Local Area Network** in TFT 1.26a.
2. Join **G1 Receiver Probe** and remain in the first/red human row (colour 0).
3. Download to 100% and wait for automatic start (latency checks may take about 30 seconds).
4. Do not type a command. Metadata and a harmless receiver probe are automatic.
5. Watch for `G1 receiver probe received`, `G1 probe confirmation sent`, then
   `G1 applied sequence=... digest=...`. Remain connected for about 20 seconds.

The maintainer must independently observe native **probe ACK** and increasing
**command ACK** sequences/digests from the real client. Queue/replay presence,
a local hello callback or a successful probe alone does not prove actor orders.
The footman receives alternating move/hold orders; after reaching its fixed
destination it can appear stationary even while healthy command ACKs continue.

If the captured sender receiver pair fails its fresh probe within twenty
simulation ticks, native disables actor commands for that game. Record the typed failure and map/native hashes; do not
retry arbitrary receiver IDs or treat synthetic ACKs as real execution.

## Manual capture fallback (historical profiles)

The preparation helper defaults to **mode 1 capture only**. A stock client in
colour 0 types `g1:hello` in ordinary game chat. It is a map diagnostic without
an exclamation mark. Capture the actual 0x60 receiver pair and action; mode 1
never injects commands. A stationary footman is expected in this profile.

Manual mode 2 (real publisher PID) and mode 3 (reserved sender) require the
observed pair and the exact map SHA; use `bot_g1_receiver_binding = manual`
(default). Those older real-client attempts produced no command ACK and are
not accepted fixes. Do not transplant calibration from a different release
without checking it. The current wire-probe profile uses a real colour-11 capture and verifies
that pair again through a harmless real-client probe after rehosting.

Single-client success does not establish absence of multiplayer desync.
Two-client comparison, pause/lag/replay, isolation and twelve-human acceptance
remain pending. Preserve bounded session evidence only in private storage;
never put local addresses, player names or raw replays in public acceptance.


## Obtain a genuine colour-11 sender capture

Use mode 1 with manual binding, the exact same map, and no fake sender. Close
rows 0 through 10 in the native map configuration, keeping only colour 11 open.
The real client joins `G1 Sender Calibration`, downloads and starts normally,
then types `g1:hello` once in ordinary match chat. Native logs the complete
receiver pair, known hello action and **colour=11**. No orders are injected.
Do not infer a wire ID from an event or trigger handle. Reopen all rows, enable
mode 3 with `wire_probe` and that observed pair, then rejoin as red/colour0.
The fresh probe ACK remains mandatory because cross-match stability has not
been established. This needs one real client in two sequential matches.

## Automatic safety scenario (candidate 0.1.5)

Join `G1 Safety Probe` in red and wait for automatic start. No chat is required.
After the fifth move the fixture displays START, skips tick publication for forty
simulation ticks, records LEASE HOLD, then displays RESUME and restores normal
commands. Keep the client connected for about a minute, then leave so native can
write the replay. A separately announced eight-second host-container freeze may
follow RESUME to exercise connection recovery; it does not replace the map lease
check or prove packet-loss recovery.

Use `Maps\Download\g1-safety-0.1.5.w3x` as the client map path, with the actual
file still bound at `/maps/g1.w3x` on the host. A release-specific client filename
preserves the previously downloaded `g1.w3x` needed by the 0.1.4 replay. Do not
retain obsolete release binaries in the CI map dist directory.

The native binary is unchanged by this map scenario. The prior genuine receiver
pair is only a startup candidate for the new exact-hash map: fresh real probe ACK
remains mandatory before actor orders. If it fails, capture the sender's genuine
pair for the new release rather than deriving or enumerating IDs. Structural and
reported-telemetry consistency checks do not establish engine replay playback.


Candidate 0.1.6 repeats the same automatic scenario with position history and
post-hold samples. Use the release-specific path
`Maps\Download\g1-safety-0.1.6.w3x`. Wait for `hold samples collected` and RESUME,
then leave after approximately 45 seconds. The already-passed host freeze need
not be repeated. The extra samples distinguish an actual stopped unit from a
completed immediate order whose current-order getter returns zero.
