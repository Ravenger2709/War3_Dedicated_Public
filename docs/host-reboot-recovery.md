# Host reboot recovery

AMP Start/Restart and `scripts/war3.py start/restart` now run a stopped-only
recovery check before starting the accepted runtime. This is control code; it
uses the existing runtime image and needs no Wine, game or native rebuild.
Existing installations must update both `control/production.py` and
`control/power_recovery.py` from the same source revision. Fresh setup copies
both files automatically. Do not copy this helper into a running runtime.

A power failure previously left a native launch claim and a pending lobby
reservation. These intentionally prevent an ambiguous second writer. Recovery
now verifies the saved native session binding, journal/IPC inodes, SQLite
integrity and kernel boot ID. A different kernel boot establishes that writers
from that boot cannot still be running. The control host and offline helper
must see the same current boot ID. Missing PIDs, container restart, AMP restart
and elapsed time are insufficient evidence.

On a verified reboot:

- Real committed native start/close records are replayed idempotently. A start
  must still match its original reservation, and catalog closes must match
  their recorded request identity.
- The interrupted lobby is retired as `host_reboot_interruption`, without
  inventing a normal native close or a game start.
- Rotation position, `last_started`, all start/close history, and replay fences
  remain intact. The existing rotation is not reset to the first map.
- Old journals, native bindings, launch claims, IPC directories and catalog
  requests remain untouched. A fresh journal inode is published for the next
  native generation.

A private `.boot-recovery.json` intent is fsynced before retirement. It records
before/after state and fingerprints of history and retained evidence. Recovery
resumes that exact transaction after interruption, including a second host
reboot during recovery. Completed records are retained as
`boot-recovery-<old-journal>.json`. These are operator metadata, not native
lifecycle events. A boot fence on an unused journal also covers a subsequent
power failure before its native session binding is written.

Recovery is conservative. Same-boot process failures, corrupt SQLite, missing
or conflicting boot evidence, unknown/partial journal records, changed history,
foreign events or an active session lease still block startup. Preserve the
private state and investigate; do not delete `pending`, launch claims, the
`.cycle.provisioned` marker, or the cycle directory to force startup. No software
recovery can repair storage that failed to persist data despite fsync.

AMP remains the application restart owner (`Docker restart=no`). Host boot must
start the AMP management instance and its application using the installed
foreground launcher. A standalone Docker `unless-stopped` policy alone bypasses
the external CLI pre-start check: if that direct restart is blocked by retained
cycle state, stop it and use `scripts/war3.py start` to run recovery. This change
does not install a system-wide service or alter other instances' autostart.

Offline tests exercise real SQLite and native-format journals with synthetic
boot IDs, including interruptions after intent, directory creation, state,
catalog and receipt writes. They do not power-cycle a production host or claim
that an interrupted Warcraft match can be resumed.
