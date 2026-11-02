# Controlled cycle restart

The supervisor records an inode-bound launch claim before preparing sealed FDs
or invoking `Popen`, then exclusively creates and fsyncs one private IPC
folder for the journal generation. Existing claims and IPC directories are not
reused. Sealed descriptors are closed after the child has exited.

For an explicit operator stop/restart or orderly supervisor shutdown, the
supervisor checks that its still-running owned native child is both session and
process-group leader. It sends the normal termination signal, waits using the
owned pidfd, requires exit status zero and checks that the process group is
empty. It then consumes and audits the journal and advances only if there is
no unresolved selection or reservation. The old journal, IPC and queue remain
on disk. A new native launch obtains a fresh journal-bound IPC directory and
reopens the same cursor. Any failed, ambiguous or forced stop remains blocked.

This controlled path relies on the configured GHost executable being trusted:
it must not leave the process group or delegate its journal/IPC writer. The
process-group check does not prove those properties against arbitrary code. A
supervisor crash, unexpected native exit, missing session binding, pending
selection, unverified wait or inaccessible process-group status does not grant
a handoff. Automatic reconciliation of such an interrupted queue is not
implemented; the launch claim and prior data remain for inspection. Native IPC
inode attestation and actual default-map validation also remain prerequisites.

The offline native test uses the real merged GHost binary, synthetic maps and
no realm or Star. It checks controlled exit, verified process group, cursor
preservation across supervisor and native restart, distinct IPC directories
and descriptor closure. It does not establish live compatibility or recovery
from crashes, or prove a writer cannot leave its group. The production launch
feature guard remains closed.
