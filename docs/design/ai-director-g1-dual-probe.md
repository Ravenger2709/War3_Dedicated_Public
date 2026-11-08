# G1 slot-free two-real-sender probe

This is a probe-only experiment, not an accepted actor transport. No fake row,
gameplay observer, LLM or engine process is added to CI/CD. It requires two real
TFT 1.26a clients in LAN, red/colour 0 and blue/colour 1. The earlier two-client
reserved-sender baseline is separate evidence; it does not validate this route.

## Capture before active probing

Use current map 0.1.8 and the exact artifact/native pins in the acceptance
receipt. In a capture-only native mode-1 match, ask each player to type
`g1:hello` once after loading. Record each genuine receiver pair by colour.
Capture does not send a generated action or enable orders. The map keeps the
existing registration/allocation order; this does not replace genuine capture.

## Native configuration

```ini
bot_g1_bridge_mode = 4
bot_g1_receiver_binding = wire_probe
bot_g1_receiver_a = <captured colour-0 field A>
bot_g1_receiver_b = <captured colour-0 field B>
bot_g1_secondary_receiver_a = <captured colour-1 field A>
bot_g1_secondary_receiver_b = <captured colour-1 field B>
bot_g1_map_sha256 = <exact 0.1.8 artifact SHA256>
autohost_startplayers = 2
autohost_gamename = G1 Dual Receiver Probe
```

`bot_g1_dual_delivery = broadcast` preserves the first experiment. The separate
`cross_only` candidate omits only a generated probe whose source PID equals the
recipient PID. This option is valid only in global mode 4. It never omits an
original player action, even from the same PID or with matching bytes.

Mode 4 is deliberately distinct from the historical global mode-2 self route
and mode-3 reserved-sender control. Invalid/missing pairs, manual binding or an
unmatched map pin cannot activate the dual probe. A new header's digest joins
the native build profile so an old executable cannot be mistaken for this code.

Only native-verified real colours 0/1 can claim the two source PIDs, using wire
version 3. Both must publish a simulation tick of at least eight. Native then
queues the unchanged original action followed by two separate PID records in
fixed colour-0, colour-1 order. Each carries the same epoch/sequence-zero/kind-zero
probe with that sender's captured receiver fields. No original action is padded
or rewritten. No actor-command generator exists in this dual-probe class.

## Callback evidence

The fixture records raw valid probe callbacks per sender and deduplicates before
accepting the harmless probe. Exactly one accepted probe per client is required.
Raw observation counters are diagnostics only and never grant actor authority.
At the next timer, each real peer exports its own advancing tick and deferred
`raw0`, `raw1`, `accepted_probe`, then `probe` epoch.

Native requires all three diagnostic fields from each peer after queuing, each
raw count at most one, total raw count at least one, accepted count exactly one,
and the peer's later tick within the twenty-tick lease. Both genuine peer ACKs
are needed. Whole-batch deadline checks precede ACK handling; stale/early/wrong
peer/epoch responses cannot complete the probe. Duplicate callbacks, departure
or timeout retire this probe session. Confirmation still leaves actor commands
disabled; it is never a full G1 or twelve-human acceptance result.

If A reports only sender B and B only sender A, that is evidence consistent with
self-action skipping. Both seeing both with one accepted probe each would instead
show that deduplication handles dual delivery. Missing callbacks or a connection
failure must be recorded without choosing either explanation as fact.

This first callback test needs approximately twenty seconds after both clients
load, not another ten-minute baseline. Keep raw logs/replays private and publish
only bounded protocol counters. AMP and VPN remain outside this experiment.

## Broadcast attempt: reproduced failure

Both colours' genuine `g1:hello` actions on the exact 0.1.8 fixture confirmed
821/821 and 822/822. Two subsequent broadcast-mode matches each completed loading
on both real clients, queued the two probes and lost both TCP connections within
approximately 10–110 ms. GHost stayed alive with zero restarts/OOM events and no
native desync event. Both completed replays passed structure/CRC checks and
contain 2.4 seconds, two probes, zero actor commands and zero probe ACKs.
The callback gate failed. This establishes reproducibility, not an engine
root cause or an independent world-state desync diagnosis.

## Cross-only candidate

This candidate tests whether echoing a generated action under the receiving
client's own PID is the relevant difference. It is not yet an accepted fix.
Two exact generated action objects are marked when queued. At ordinary and
overflow serialization, each recipient retains every original object and
receives only marked probes from other PIDs. CRC is recomputed for that actual
recipient's payload; the shared time interval is preserved. Empty overflow
packets may be skipped, while the final normal tick is always sent.

Marks are erased before the generated objects are deleted after flushing, so
later player actions cannot inherit a filter through pointer reuse. Other maps
and modes take the existing unfiltered path. Canonical replay still stores both
generated probes; its contents cannot independently prove each recipient's
filtered view or engine playback. The map and captured receiver pairs are
unchanged. Real ACKs from both clients remain mandatory, and actor commands
remain disabled even after success.
