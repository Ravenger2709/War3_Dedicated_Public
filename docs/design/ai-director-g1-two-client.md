# G1 two-client baseline

This test uses the proven reserved-sender control, not the proposed slot-free
transport. It requires two concurrently executing TFT 1.26a clients. There are
still eleven available real-human places and one fake colour-11 sender. Native
binary and map 0.1.6 are unchanged. No AMP, VPN, iCCup account or LLM is involved.

## Preparation

Use the isolated LAN host and exact pins from `tests/g1/acceptance.json`.
Configure `autohost_startplayers = 2` and a distinct lobby name,
`G1 Two Client Check`. Do not promote one real client plus a synthetic peer as
two-client evidence. Capture native output only from the current container start
and record its identity, start time, binary/map hashes and native host counter.
A new real probe ACK remains mandatory before actor orders.

## Operator sequence

1. Open Local Area Network on both real clients, using different LAN nicknames.
   Join `G1 Two Client Check`: first player red/colour 0, second blue/colour 1.
2. Download fully on both; the host waits for two real players before auto-start.
   Both should see the neutral footman and the automatic safety scenario.
3. Keep both clients in the match for eleven minutes after loading. START,
   LEASE HOLD, delayed samples and RESUME run automatically; do not type chat
   commands or change slots. Report a disconnect/menu return immediately.
4. After initial server confirmation, the blue player may pause once for ten
   seconds, then resume. This tests an ordinary shared simulation pause; do not
   disable a network interface, manipulate a VPN or suspend another application.
5. At the end, red leaves first. Blue remains for eight seconds and reports
   whether the lease-expired message appears, then exits. A departure observation
   is recorded separately; it does not substitute for measured physical stop.

The eleven-minute interval leaves margin for a full ten-minute paired ACK span.
Do not declare a shorter run accepted merely because the last digests match.

## Bounded ACK audit

Feed the current-start timestamped native output to
`python3 tests/g1/peer-ack-audit.py --epoch N --pids A B`. Supply the two actual
real-player PIDs and native host counter from this match, not colour numbers.
The default minimum is 600 seconds between completed first and last ACK pairs.
The tool reads stdin only and never connects to Docker or either client.

It checks every shared sequence, requires a contiguous paired prefix from one,
and rejects duplicate peer ACKs, unexpected peers, mismatched digests, missing
interior ACKs and nonmonotonic timestamps. Up to two unpaired trailing sequences
are reported explicitly; their timing cannot lengthen the accepted span.
Input is limited to 8 MiB, 8 KiB per line and 10,000 ACK records. Only anchored
native records are accepted; chat text is not an ACK. Its JSON contains scalar
protocol data, with no nicknames, addresses or chat.

`CONSISTENT_TWO_PEER_ACKS` establishes log consistency only. Attach separate
operator confirmation of two real clients, current-start process evidence,
native checksum/desync observations and completed replay structure before
recording this fixture's real-client gate. A running-match audit is provisional
until coordinated departure and replay collection finish.

## Evidence and interpretation

Compare every paired `(host_counter, sequence)` digest from the two expected
PIDs. Missing interior ACKs, an unexpected peer, duplicate records, or any digest
mismatch prevent acceptance; a small unconfirmed tail at coordinated exit is
reported explicitly. Native also compares the clients' engine-reported checksums
in `EventPlayerKeepAlive`; inspect fixed native desync events and replay checksum
records when available, not ordinary player chat containing similar words. The
pinned native currently comments out the replay `AddCheckSum` call, so zero
checksum records cannot establish an independently recorded engine-checksum
comparison. Live comparison remains enabled; absence of its desync event is
reported with that limitation.

The completed replay supplies record/CRC integrity and the controlled lease
snapshot. Actual engine playback remains a different gate. Protocol logs do not
prove that a peer is a real engine: operator confirmation of both clients and
continuous native player state are required. Passing this baseline establishes
observed two-client consistency for this fixture, not twelve-human capacity or
the proposed dual-real-sender transport in
[the capacity experiment plan](ai-director-g1-capacity.md).

## Completed 0.1.6 baseline

Two operator-confirmed real clients completed the local test on the unchanged
map and native binary. All 317 actor commands received one ACK from each client:
317 contiguous pairs spanning 650.610 seconds, with no digest mismatches,
missing sequences, duplicates or unpaired tail. There were no native desync
events or container restarts. The completed replay passed container CRC and
record-boundary checks; it contains one pause, one resume and two departures.
Its controlled-stall telemetry also passed the delayed physical-hold checks.
No engine replay playback was performed.

After the red publisher left, new actor commands stopped and the operator
confirmed the blue client remained in the game. The operator did not observe
the lease-expiry message. Physical hold after publisher departure therefore
remains **unverified**, separately from the earlier measured controlled stall.
The replay has zero engine-checksum records for the pinned-writer reason above.
This result passes the reserved-sender two-client ACK consistency baseline,
not every G1 gate or the proposed twelve-human transport.
