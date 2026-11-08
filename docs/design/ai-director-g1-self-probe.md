# G1 one-client slot-free self-delivery probe

This isolated experiment asks whether one stock TFT 1.26a client receives a
generated trigger-chat action reflected in its own real-player action record.
It does not allocate a fake player or a gameplay observer. All twelve map rows
remain available to humans. This is not yet twelve-human engine acceptance.

Use map 0.1.7 and the exact native/map pins recorded in
`tests/g1/acceptance.json`. There are no production or AMP setting changes.
The existing G1 LAN preparation helper still defaults to capture mode; promote
only this private experimental session with the following explicit settings:

```ini
bot_g1_bridge_mode = 2
bot_g1_receiver_binding = wire_probe
bot_g1_receiver_a = 821
bot_g1_receiver_b = 821
bot_g1_map_sha256 = <exact 0.1.7 artifact SHA256>
autohost_startplayers = 1
autohost_gamename = G1 Self Receiver Probe
```

The pair 821/821 was genuinely captured from colour 0 in an earlier release.
Preserved registration/allocation order makes it a candidate for this fixture,
not proof of its current validity. A JASS handle is never used as its substitute.
Map 0.1.7 permits sender colour 0 instead of the earlier fake colour 11.

## Operator step

Join `G1 Self Receiver Probe` in Local Area Network, red slot, with one real
1.26a client. After full download and auto-start, leave the game running for
approximately twenty seconds. The host sends an automatic harmless sequence-zero
probe. A valid later `probe` cache export enables the first actor command; only
then should the footman move and increasing `G1 applied sequence` messages appear.
No chat command is required for the automatic probe.

Native must distinguish `receiver probe queued merged=1` from actor commands.
The queued action, LAN advertisement, synthetic peer receive, or saved replay
alone cannot pass the callback gate. Only a real map response followed by actual
command ACKs can do so. If the probe expires after the existing twenty-tick
window, actor commands remain disabled for that session. No automatic retries,
role changes, fake-sender fallback or player eviction are permitted.

## Result scope

Offline native tests cover the merged record under the real PID, no allocated
fake row, the fresh-probe prerequisite, timeout without actor commands and
twelve synthetic admissions without dropping existing peers. They do not
execute Warcraft or prove callback delivery.

If self-delivery fails, record the observed failure without assuming which
engine behavior caused it. The next candidate in
[the capacity plan](ai-director-g1-capacity.md) uses two genuine senders and
therefore requires two real clients. No repeated ten-minute two-client baseline
is needed merely to check this initial single-client route.
