"""Opt-in cycle control under one managed journal and cursor lease.

The controller binds an owned child and sealed manifest, drains durable events,
and may drive catalog IPC from fresh native observations. Native process launch
remains blocked by the controller fuse; ambiguous exits cannot clear pending
reservations. A snapshot does not serialize native admission with publication.
"""
import hashlib
import os

from .cycle_coordinator import CycleCoordinator
from .cycle_state import CycleState
from .session_lease import SessionLease


class ProductionCycleControl:
    def __init__(self, root, cycle):
        self.lease = SessionLease(root, enabled=True)
        try:
            self.state = CycleState(self.lease.cursor, cycle, lease=self.lease)
            self.coordinator = CycleCoordinator(self.state, self.lease.journal)
            self._exited_child = None
            self.catalog = None
            self.driver = None
            self.max_games = None
            self._ipc_fd = None
            self._ipc_path = None
            self._launch_claimed = False
            self._ipc_provisioned = False
            self._controlled_stop_identity = None
        except BaseException:
            self.lease.close()
            raise

    def claim_native_launch(self):
        """Fence an attempt before Popen; uncertain attempts are never reused.

        The claim is intentionally retained even if spawning or session binding
        fails. It does not prove absence of a legacy writer or authorize recovery.
        A verified-exit handoff advances to another inode and another claim.
        """
        import json
        import secrets
        self.lease.check()
        if os.listdir(self.lease.journal_fd):
            raise ValueError('CYCLE_LAUNCH_JOURNAL_NOT_EMPTY')
        journal = os.fstat(self.lease.journal_fd)
        payload = (json.dumps({'version': 1, 'journal_device': journal.st_dev,
                               'journal_inode': journal.st_ino,
                               'nonce': secrets.token_hex(16)},
                              separators=(',', ':')) + '\n').encode('ascii')
        name = 'native-launch-' + self.lease.journal.name + '.json'
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self.lease.fd)
        except FileExistsError:
            raise ValueError('CYCLE_LAUNCH_UNRESOLVED') from None
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('short launch claim write')
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.lease.fd)
        self.lease.check()
        self._launch_claimed = True

    def provision_launch_ipc(self):
        """Create one private IPC directory after a durable in-process claim.

        Existing paths are never reused, even after a failed attempt. The
        supervisor must still establish writer exclusion before any handoff.
        """
        from .lifecycle import _open_directory, _check_directory
        if not self._launch_claimed:
            raise ValueError('CYCLE_LAUNCH_UNRESOLVED')
        self.lease.check()
        if self._ipc_provisioned:
            raise ValueError('CYCLE_IPC_ALREADY_EXISTS')
        self._ipc_provisioned = True  # A failed mkdir/fsync is also uncertain.
        name = 'ipc-' + self.lease.journal.name
        try:
            os.mkdir(name, 0o700, dir_fd=self.lease.fd)
        except FileExistsError:
            raise ValueError('CYCLE_IPC_ALREADY_EXISTS') from None
        os.fsync(self.lease.fd)
        path = self.lease.root / name
        fd = _open_directory(str(path))
        try:
            _check_directory(str(path), fd)
            if os.listdir(fd):raise ValueError('CYCLE_IPC_NOT_EMPTY')
            os.fsync(fd)
            self.lease.check()
            _check_directory(str(path), fd)
        finally:
            os.close(fd)
        return path

    def pin_launch_ipc(self, ipc_directory):
        """Pin the private launch directory before the native process exists."""
        from pathlib import Path
        from .lifecycle import _open_directory, _check_directory
        self.lease.check()
        ipc = Path(os.path.abspath(os.fspath(ipc_directory)))
        if (not self._launch_claimed or self._ipc_fd is not None or ipc.parent != self.lease.root
                or ipc.name != 'ipc-' + self.lease.journal.name):
            raise ValueError('CYCLE_IPC_PATH_REJECTED')
        fd = _open_directory(str(ipc))
        try:
            _check_directory(str(ipc), fd)
            if os.listdir(fd):raise ValueError('CYCLE_IPC_NOT_EMPTY')
            os.fsync(fd)
            self.lease.check()
            _check_directory(str(ipc), fd)
        except BaseException:
            os.close(fd)
            raise
        self._ipc_fd, self._ipc_path = fd, ipc

    def bind_native(self, child, raw_line):
        """Persist startup read by the controller's authenticated child reader."""
        from .native_session import NativeSessionReader
        if child.session_owner is None or child.session_reader is not None:
            raise ValueError('NATIVE_SESSION_OWNER_UNAVAILABLE')
        reader = NativeSessionReader(child.session_owner, self.lease, child.session_executable,
                                     ipc_binding=(self._ipc_fd, self._ipc_path) if self._ipc_fd is not None else None)
        try:
            binding = reader._persist_owned_line(raw_line)
        except BaseException:
            reader.close()
            raise
        child.session_reader = reader
        return binding

    def attach_catalog(self, child, bundle, ipc_directory, max_games):
        """Bind a fresh queue to the manifest actually passed to the owned child.

        This only enables journal consumption, not process startup or selection.
        A pre-existing queue or nonempty IPC directory requires reconciliation;
        creating a replacement would discard unknown work after a crash.
        """
        from pathlib import Path
        from .catalog_requests import CatalogRequests
        from .cycle_driver import CycleDriver
        from .preload_manifest import StartupBundle, _LIMIT, parse_startup_manifest

        generation = self.lease._native_generation
        self.lease.require_native(generation)
        if (self.catalog is not None or self.driver is not None
                or child.session_reader is None
                or child.session_reader is not self.lease._native_reader
                or child.session_owner is not child.session_reader.child
                or child.preload is not bundle or type(bundle) is not StartupBundle
                or self.state.state['pending'] is not None):
            raise ValueError('CYCLE_CATALOG_OWNER_REJECTED')
        bundle.pass_fds()
        info = os.fstat(bundle.manifest_fd)
        if not 0 < info.st_size <= _LIMIT:
            raise ValueError('CYCLE_MANIFEST_REJECTED')
        manifest = os.pread(bundle.manifest_fd, info.st_size + 1, 0)
        if len(manifest) != info.st_size or hashlib.sha256(manifest).hexdigest() != bundle.manifest_sha256:
            raise ValueError('CYCLE_MANIFEST_REJECTED')
        parsed = parse_startup_manifest(manifest)
        if ([parsed['entries'][i]['id'] for i in parsed['cycle_indices']] != self.state.cycle
                or type(max_games) is not int or parsed['max_games'] != max_games
                or [(e['fd'],e['size'],e['sha256']) for e in parsed['entries']] !=
                   [(m.fd,m.size,m.sha256) for m in bundle.maps]):
            raise ValueError('CYCLE_CATALOG_MISMATCH')
        ipc = Path(os.path.abspath(os.fspath(ipc_directory)))
        if ipc.parent != self.lease.root or ipc.name != 'ipc-' + self.lease.journal.name:
            raise ValueError('CYCLE_IPC_PATH_REJECTED')
        if self._ipc_fd is None or self._ipc_path != ipc:
            raise ValueError('CYCLE_IPC_UNAVAILABLE')
        record=child.session_reader._session_record
        pinned=os.fstat(self._ipc_fd)
        if (type(record) is not dict or record.get('version') != 2
                or (record.get('ipc_device'),record.get('ipc_inode')) != (pinned.st_dev,pinned.st_ino)):
            raise ValueError('CYCLE_IPC_IDENTITY_MISMATCH')
        self.check_ipc()
        if os.listdir(self._ipc_fd):
            raise ValueError('CYCLE_IPC_NOT_EMPTY')
        os.fsync(self._ipc_fd)
        path = self.lease.root / ('catalog-' + generation + '.json')
        catalog = CatalogRequests.initialize(path, ipc, manifest, generation)
        driver = CycleDriver(catalog, self.state, self.lease.journal)
        self.check_ipc()
        self.catalog, self.driver = catalog, driver
        self.max_games = parsed['max_games']
        self.coordinator = driver.coordinator
        return catalog

    def check_ipc(self):
        """Detect replacement while the leased supervisor owns the session."""
        from .lifecycle import _check_directory
        self.lease.check()
        if self._ipc_fd is None or self._ipc_path is None:
            raise ValueError('CYCLE_IPC_UNAVAILABLE')
        _check_directory(str(self._ipc_path), self._ipc_fd)

    def prepare_controlled_stop(self, child):
        """Record the owned, running session leader before an operator stop."""
        reader = self.lease._native_reader
        generation = self.lease._native_generation
        if reader is None or child is not reader.child:
            raise ValueError('CYCLE_EXIT_NOT_VERIFIED')
        self.lease.require_native(generation)
        child.verify_running()
        pid = child.identity.pid
        if os.getpgid(pid) != pid or os.getsid(pid) != pid:
            raise ValueError('CYCLE_PROCESS_GROUP_UNVERIFIED')
        child.verify_running()
        self._controlled_stop_identity = child.identity

    def _require_writer_exclusion(self):
        """Under trusted-GHost policy, require a clean empty process group.

        This is not containment against a process that escapes the group or
        delegates a descriptor. Unexpected exit and ambiguous checks fail closed.
        """
        reader = self.lease._native_reader
        child = reader.child if reader is not None else None
        if (child is None or self._controlled_stop_identity != child.identity or
                child._verified_exit != (child.identity, 0)):
            raise ValueError('CYCLE_WRITER_SCOPE_UNPROVEN')
        try:
            os.killpg(child.identity.pid, 0)
        except ProcessLookupError:
            return
        except OSError as error:
            raise ValueError('CYCLE_PROCESS_GROUP_UNVERIFIED') from error
        raise ValueError('CYCLE_PROCESS_GROUP_NOT_EMPTY')

    def handoff_after_verified_exit(self, child):
        """Handoff after a controlled clean exit and an empty process group.

        This relies on the trusted native not escaping or delegating its writer.
        """
        from .writer_observation import _load_binding
        from .journal_audit import audit
        self.lease.check()
        reader = self.lease._native_reader
        generation = self.lease._native_generation
        if (reader is None or child is not reader.child or child._verified_exit is None
                or child._verified_exit[0] != child.identity
                or _load_binding(self.lease, generation) != child.identity):
            raise ValueError('CYCLE_EXIT_NOT_VERIFIED')
        # A pidfd wait alone cannot exclude inherited or delegated writers.
        self._require_writer_exclusion()
        if self.driver is not None:
            self.check_ipc()
        self.coordinator.consume()
        if self.driver is not None:
            self.driver._retire()
        inventory = audit(self.lease,generation)
        for item in inventory:
            if self.state.state['version'] != 3:
                raise ValueError('CYCLE_HISTORY_MISMATCH')
            event = item.event
            if not item.acknowledged or item.pending:
                raise ValueError('CYCLE_JOURNAL_NOT_DRAINED')
            if event.event == 'game_started':
                if self.state._find_external('match_id',generation,event.match_id) != (event.host_counter,event.match_id):
                    raise ValueError('CYCLE_HISTORY_MISMATCH')
            elif not self.state._find_closed(generation,event.host_counter):
                raise ValueError('CYCLE_HISTORY_MISMATCH')
            elif event.version in (2,3) and self.state._find_catalog_close(generation,event.host_counter) != (
                    event.request_id,event.manifest_sha256,event.catalog_index):
                raise ValueError('CYCLE_HISTORY_MISMATCH')
            if event.event == 'lobby_closed' and self.state._find_idle(generation,event.host_counter) != (event.version == 3):
                raise ValueError('CYCLE_HISTORY_MISMATCH')
        if self.state.state['pending'] is not None:
            raise ValueError('CYCLE_PENDING_UNRESOLVED')
        if self.catalog is not None and self.catalog.data['pending'] is not None:
            raise ValueError('CYCLE_CATALOG_PENDING_UNRESOLVED')
        path = self.lease._advance_journal()
        self._launch_claimed = False
        self._ipc_provisioned = False
        self._controlled_stop_identity = None
        self.coordinator = CycleCoordinator(self.state,path)
        self.catalog = None
        self.driver = None
        self.max_games = None
        if self._ipc_fd is not None:
            os.close(self._ipc_fd)
        self._ipc_fd = self._ipc_path = None
        return path

    def note_native_exit(self, child):
        """Remember only an exit verified by this same OwnedChild.wait()."""
        reader = self.lease._native_reader
        if reader is None or child is not reader.child or child._verified_exit is None:
            raise ValueError('CYCLE_EXIT_NOT_VERIFIED')
        self._exited_child = child

    def tick(self):
        """Consume journal before reporting whether a reservation is pending."""
        if self.driver is not None:
            self.check_ipc()
        result = self.coordinator.consume()
        driver_state = None
        reader = self.lease._native_reader
        if (self.driver is not None and (self._exited_child is not None or
                reader is not None and reader.child.is_running())):
            # Retiring committed journal history needs no lobby observation.
            driver_state = self.driver._retire()
        pending = self.state.state['pending']
        selection = self.catalog.data['pending'] if self.catalog is not None else None
        if self._exited_child is not None and pending is None and selection is None:
            self.handoff_after_verified_exit(self._exited_child)
            self._exited_child = None
        blocked = pending is not None or selection is not None
        status = {'state': 'blocked_pending' if blocked else 'waiting_native_ipc',
                  'pending': blocked, 'preset_id': self.state.current,
                  'processed': result.processed, 'recovered_acks': result.recovered_acks,
                  'recovery': 'unavailable'}
        if driver_state is not None:
            status['driver'] = driver_state
        return status

    def close(self):
        if self._ipc_fd is not None:
            os.close(self._ipc_fd)
            self._ipc_fd = None
        self.lease.close()
