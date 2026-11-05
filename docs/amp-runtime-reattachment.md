# Recovering AMP control of a surviving runtime

An ADS or AMP management restart can terminate the foreground Docker client
while the separate Star/GHost container continues running. The old launcher
then fails its stopped-container preflight. AMP reports Error even though the
bot remains authenticated and its lobby or matches are still alive.

The AMP foreground launcher now checks for a healthy survivor first. It verifies
the accepted image ID/source, deployment UID/GID, AMP ownership label, exact
read-only/writable bind mounts, security settings and TCP 6113-only publication.
Standalone-owned, stopped, paused, restarting or foreign containers are refused.
A bounded request to the live loopback `/health/ready` endpoint runs inside the
exact container. A stale `status.json` cannot establish readiness.

After a second identity check the launcher attaches by immutable container ID.
It does not stop or recreate the container, spawn another Star/native process,
clear the cursor or rewrite profiles/preset edits. The foreground Docker client
keeps its ordinary STDIO and signal proxy, so AMP Stop still sends SIGTERM.
A private `control/.amp-runtime-owner.lock` is held across exec; an abrupt
launcher exit releases it automatically. A second cooperating launcher cannot
attach concurrently. Healthy lobby/countdown/game states emit the matching
existing AMP ready message once when control is recovered.

An existing unhealthy or unverified container is retained for investigation;
this path does not authorize removing its launch claim or reservation. Missing
containers use the usual gated cold-start flow. The stopped-only host reboot
recovery remains a separate operation. Saved configuration edits apply on a
subsequent orderly Stop/Start, not during adoption of a running session.

Existing flat-control installations must update `production-launch.py`,
`production.py` and `runtime_attachment.py` together. Fresh setup copies the
module automatically. The runtime image, Wine setup, network topology and
Generic template stay unchanged. Standalone lifecycle and its owner labels
remain separate from the AMP foreground launcher.

`tests/phase3/test_runtime_attachment.py` covers adoption, private owner locking,
security/identity failures, live readiness, unchanged pending editor content and
ready-message compatibility. `tests/phase3/amp_attachment_lifecycle.py` is an
explicit Docker test using a caller-pinned local image and a synthetic heartbeat
payload. It kills a foreground Docker client, verifies the container survives,
reattaches twice to the same ID, checks the lease, and stops through SIGTERM.
It uses no host ports, network, accounts, game engine, Star or native game code;
it proves control lifecycle rather than gameplay or a global ADS restart.
