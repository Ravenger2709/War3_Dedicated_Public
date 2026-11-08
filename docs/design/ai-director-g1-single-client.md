# Single-client G1 LAN smoke

This is an isolated CI/CD development instance. No production AMP, Star, iCCup account, VPN configuration, LLM service or Warcraft engine runs on the server. A real **TFT 1.26a / build 6401** client runs on the operator's LAN computer.

Current artifact 0.1.3 is the explicit **reserved-sender positive control**: use
native mode 3, lobby `G1 Sender Control`, one visible `FakePlayer`, and eleven
real-human places. The twelve-human real-PID candidate has not passed.

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

## Operator: first entry (capture only)

1. Open Warcraft III TFT 1.26a and choose **Local Area Network** (not Battle.net/iCCup).
2. Choose the supplied test lobby: **G1 Bridge 1.26a** for the capture candidate, or **G1 Sender Control** for the explicit reserved-sender control. The supplied LAN address is diagnostic information; the game is selected in the LAN list.
3. Remain in the first/red human row (colour 0). Download the map to 100%, then wait for automatic start (latency checks may take about 30 seconds).
4. In the match, confirm the map displays the G1 instructions and the neutral footman is visible.
5. Type exactly **`g1:hello`** in ordinary game chat, then remain connected for about 15 seconds and report loading/visibility/return-to-menu/disconnect results.

**The footman standing still is expected in capture mode.** The host must record a genuine action `0x60`, both receiver fields and the exact known hello action. Synthetic 17/23 fixture values must never be promoted as if they came from the real client.

## Second entry (after the real capture is reviewed)

Leave the first match when instructed. The maintainer pins the observed receiver pair for that exact map and switches only this test container to mode 2 for the real-PID candidate, or mode 3 for the current reserved-sender control. Join the new LAN lobby, let it start and type `g1:hello` again. Now check move/hold application, increasing sequence/digest and actual host ACKs; then test a brief pause/resume and a fresh match. A single client does not establish absence of multiplayer desync. Two-client comparison/replay and all other G1 acceptance gates remain pending.

Preserve only the bounded test-session evidence/replay in private storage. Do not store local infrastructure addresses or user names in the public acceptance example.
