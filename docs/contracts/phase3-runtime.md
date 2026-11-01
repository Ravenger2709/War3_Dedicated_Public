# Production supervisor contract

`python3 -m src.supervisor --config /data/config/supervisor.json` selects
`ICCupSupervisor` only for `mode=iccup` and the explicit `original-star-v1`
adapter. `--check` validates configuration without starting a component or
reading an account profile. The ordinary offline supervisor remains available;
offline configuration with `gameplay` uses the same native settings compiler.

The controller owns one native GHost child and one Star/relay cohort. GHost
uses `/opt/war3/bin/ghost++ /data/state/native/ghost.cfg`, working directory
`/data`. The compiler disables native BNCS authentication and maps all validated
gameplay settings to the keys in `phase3-native.md`. A live flock on the supplied
read-only `launcher.ini` prevents another local controller from using the same
profile inode. It does not coordinate copied profiles on other hosts: stop the
CI account session before staging starts.

Readiness requires current child PIDs and generations, original Star's fresh
RUNNING heartbeat, relay-confirmed identity/authentication and recent realm
traffic, native initialization and typed lifecycle heartbeat. A lobby also
requires a TCP6113 listener owned by the native PID and an accepted publication
for its current PID/host counter. IN_GAME needs no lobby advertisement. Process
existence alone never proves authentication. Public directory discovery, exact
lookup, ACK and a refresh's position effect are distinct observations.

HTTP `/health/live`, `/health/ready` and `/status` bind container loopback8080.
The only public socket is TCP6113. Relay listens on loopback16112; native does
not connect directly to the realm. Native state is read from strict structured
stdout, and `native-feedback.cfg` contains only bounded state/counters. The
refresh-request mailbox binds each native slot-toggle event to its current
publisher generation. Refresh applies only to empty, unreserved playable slots;
occupied human slots never toggle. Vote/countdown/game and other unsafe native
states suppress refresh. No kick, player move or rehost is involved.

A Star/relay failure revokes online state and restarts that cohort after prefix
cleanup, preserving native and any current match. A native failure restarts the
cohort and native. Restart budgets use bounded exponential backoff, then a
300-second circuit cooldown before one probe. Explicit server authentication
rejection and unsuccessful prefix cleanup require operator intervention;
`start` rechecks cleanup before allowing a new cohort. Local console commands
are status/start/stop/restart/quit. No password is passed on argv or stdin.

A current worker's engine safety flag is processed even after worker exit and
after its readiness heartbeat expires. It stops every component and atomically
writes `/data/state/engine-blocked.json`. That marker survives a container
restart and prevents spawning. An operator must investigate the inputs before
removing it while the instance is stopped. Automatic recovery never clears it.

Stop sends native SIGTERM first so it can withdraw the lobby, then stops relay
and original Star. Star gets at least 35 seconds for WM_CLOSE and prefix-scoped
Wine cleanup. Docker's grace is120 seconds; AMP allows150 seconds. Prefix
cleanup accepts wineserver `-k`1 only when bounded `-w` succeeds, covering an
already stopped server. This follows Wine11's
[command implementation](https://github.com/wine-mirror/wine/blob/wine-11.0/server/main.c)
and [prefix lock implementation](https://github.com/wine-mirror/wine/blob/wine-11.0/server/request.c).
Nonzero cleanup or a timeout blocks a new cohort. No global wineserver kill is
used. Persistent supervisor journals rotate at10MiB with three archives; vendor
logs have their separate bounded budget in the Star contract.

`scripts/test-production-runtime.py` executes controller and Star regressions
with synthetic subprocesses/data in Docker network=none. It records hashes of
the staged sources. `scripts/production-local-check.py --image sha256:…
--run-dir artifacts/phase3/new-run --seconds 900` executes the actual packaged
native and original Star, with a synthetic profile, RO source assets, no network
and no engine. It checks cold/warm startup, cleanup, native crash recovery,
persistent SQLite and absence of leftover vendor processes/zombies. Its receipt
is separate from online and real Warcraft-client acceptance. A build receipt or
unit success does not issue the production gate.
