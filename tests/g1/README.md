# G1 offline checks

These run in the local CI/CD compiler container, never on production AMP.

```sh
# Repository bind /repo; default working directory /repo.
# No network, Docker socket, account/profile, Wine or Warcraft engine.
g++ -std=c++11 -Wall -Wextra -Werror -Wno-misleading-indentation \
  -fsanitize=address,undefined -fno-omit-frame-pointer -I. \
  tests/g1/bridge-test.cpp -o /tmp/g1-parser
/tmp/g1-parser
WAR3_STORM_LIBRARY=/inputs/libstorm.so bash tests/g1/build-native-tests.sh
```

For the catalog concurrency regression, also use `--cgroupns host` and
`-v /sys/fs/cgroup:/sys/fs/cgroup:ro`; the script checks this before building.
Use an executable private tmpfs for compiled binaries, a read-only container root,
non-root UID, `--network none`, memory/PID/CPU limits. Python fixture analysis additionally needs a read-only **unsanitized** MIT StormLib
built from the same pin (to avoid embedding ASan into Python); it contains no game
assets. The script requires this path so concurrency cases cannot silently skip.
The native build needs only
the pinned public GHost/StormLib checkouts and the toolchain dependencies.
`docker/g1/Dockerfile` builds the toolchain using the existing signed Ubuntu
snapshot. Dependency acquisition/build is separate from offline tests.

The C++ suite checks boundary parsing, truncation, ownership, exact callback
binding, duplicate ACKs, per-match isolation, expiry/backpressure, forbidden
client downlinks, and deterministic malformed-byte fuzzing. The Python suite
sends W3GS packets to actual CGame, observes native outgoing actions and reads
its replay container. Synthetic peers do not execute JASS, simulate Warcraft,
validate chat receiver mapping or detect real desync. Those remain client gates.

`build-native-tests.sh --g1-only` runs focused bridge and ordinary multi-match
checks without repeating the unchanged periodic-message suite. Mode-3 tests
also verify the explicit fake sender, eleven-human budget, owner/reserved
rejection without kicks, and wrong-map isolation. These synthetic checks do
not establish that a real client executes the control command.


The current automatic control uses `bot_g1_receiver_binding = wire_probe` in
mode 3 with the complete colour-11 **wire capture**, not JASS handles. The map
publishes protocol v2 automatically; native gates actor commands on a fresh
causal probe ACK. Capture-only mode 1 logs all human colours without granting
active publisher authority to non-red players. Negative settings and ACK order
cases are covered. A real event-handle experiment disproved low-word conversion;
its failure is recorded in the acceptance receipt rather than treated as a fix.

`native-isolation-test.py` runs two actual CGame sessions in one native process
with synthetic peers and identical receiver pairs. It verifies independent
probe epochs, separate command sequences and packet recipients, and survival
of the second session when the first publisher leaves. This tests routing and
lifetime only, not two independently executing Warcraft worlds. The C++ suite
also checks repeated simulation ticks, ignored lag observations and retirement
followed by a fresh session; these are not an engine pause/lease test.

## Replay structure audit

```sh
python3 tests/g1/test_replay_audit.py
python3 tests/g1/replay-audit.py /input/completed-test.w3g
```

Mount only the selected test replay read-only; use the same non-root container
with `--network none` and a 256 MiB memory limit. The stdlib auditor checks
native TFT 26/build 6401 headers, folded block CRCs, bounded zlib streams and
record/action boundaries. Input is limited to 64 MiB compressed and less than
128 MiB expanded, with bounded records and command summaries. It outputs fixed
error codes or protocol scalars, without player names, addresses or chat.
Unknown grammar refuses the semantic report. Header/record integrity does not
establish that a command came from the trusted host or was applied by an engine.

The report distinguishes harmless probes, actor commands, exported ACK/digest,
and pause/resume records. ACK-to-epoch attribution is an inference from replay
order: the original incoming network batches cannot be reconstructed. It does
not certify receiver binding, replay playback or multiplayer desync safety.
The four completed earlier LAN attempts passed structural checks and retained
their original zero-ACK result. The currently running successful match is not
included until native closes and writes its replay.
