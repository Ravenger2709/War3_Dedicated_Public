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
