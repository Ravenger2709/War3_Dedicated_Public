# Phase 4 AMP settings migration

Before installing or refreshing the cycle-aware AMP manifest for an existing
instance, stop the instance, close settings editors and back up its private
configuration. With the matching reviewed `control/production.py` in place,
run as the instance data owner:

```sh
python3 /path/to/instance/control/production.py migrate-cycle --instance-dir /path/to/instance
```

The command verifies the accepted image and stopped state under the existing
setup locks. It normalizes the legacy `map_preset` into a typed `map_cycle`
array, preserves other settings, removes the legacy key from the migrated file,
validates enabled IDs, and retains the displaced file for conflict detection.
It is idempotent when the cycle and legacy selection agree. Do this **before** the new manifest can
write its default; the command does not update a manifest or start a runtime.

If both fields already exist and their first selections differ, the command
refuses to guess and leaves the file unchanged. An AMP-added `["manual"]`
default is indistinguishable from an intentional edit to the same value; inspect
the existing settings and operator intent, then explicitly reconcile the cycle
before retrying. Normal startup gives an existing `map_cycle` priority, so
skipping the pre-migration can switch a legacy selection to `manual`.

Concurrent File Manager edits during a supervisor merge stop startup. Inspect
`control/preset-conflict` (including `displaced.json` for an edit racing the
atomic exchange) and any retained `control/preset-metadata-history` entries;
merge the desired data before removing reviewed recovery files. A writer still
holding the old inode can finish after startup: retained history detects that
change before a subsequent preset/start transaction. The exchange is not a
compare-and-swap and this procedure does not prevent that late write.

Operator startup accepts 1–32 enabled existing IDs, preserving order and
duplicates. `max_concurrent_matches` defaults to 3 and accepts 1–5; the
cycle-aware native runtime counts both loading and running matches.
For multiple entries, Start and explicit `preflight` invoke the accepted image's
`src.runtime.map_cycle.validate_full_cycle` before merging the validated settings
snapshot. Every entry, including `manual`, must pass its map, capacity and
required AI approval checks. The validator has no network, a read-only root,
read-only config/maps binds, no account/profile mounts, and bounded CPU, memory,
processes and execution time. Failure or a missing validator in an older image
blocks launch; it never falls back to validating only the first map. Single-entry
deployments retain their existing image startup validation.

This operator change must be paired with the separate ordinary-config/catalog
runtime integration and a matching accepted image. On 2026-11-02, the account,
discovery, standalone and operator-cycle source suites ran 161 tests successfully
with two optional skips in existing `war3-dedicated:v0.4.1` (image
`sha256:57683e4e39ec00cb02a48ec872bc92436b26b5f48a0740ba8ad1c03359cae466`).
The container used UID/GID 1000, no network, read-only root and read-only checkout;
Docker actions and map analysis in the focused operator tests are stubbed. They do not
exercise the new validator inside a release image or establish runtime, AMP,
iCCup or gameplay GREEN. Unknown-crash recovery remains fail-closed; unresolved
claims, selections and reservations require reconciliation rather than reset.
