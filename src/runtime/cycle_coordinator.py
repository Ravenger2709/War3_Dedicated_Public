"""Connect cycle reservations to the native start journal and gate.

The supervisor must exclusively own the cursor and validate that observations
come from its current native child. This component does not create lobbies,
change maps, or abandon reservations merely because a lobby vanished.
Only a validated durable native fenced-close can clear an external reservation.
"""
from dataclasses import asdict

from .lifecycle import scan
from .reservation_marker import publish


class ReconciliationUnavailable(RuntimeError):
    """Exit-based reconciliation lacks a verified ownership capability."""


class CycleCoordinator:
    def __init__(self, state, journal, *, catalog_requests=None):
        self.state = state
        self.journal = journal
        self.catalog_requests = catalog_requests

    def consume(self):
        def persist(event):
            # An already committed event is an acceptable, durable replay.
            if event.event == 'lobby_closed':
                if event.version in (2, 3):
                    if self.catalog_requests is None:
                        raise ValueError('CATALOG_CLOSE_COORDINATOR_REQUIRED')
                    self.catalog_requests.consume_catalog_close(self.state, asdict(event))
                else:
                    self.state.consume_close(asdict(event))
            else:
                self.state.consume_external(asdict(event))
            return True
        return scan(self.journal, persist, lease=self.state.lease)

    def reconcile_after_native_exit(self, child, session_generation):
        """Fail closed until child ownership and journal exclusion are provable.

        The current runtime cannot bind its native generation durably to the
        exact child and journal, or hold exclusive ownership across all cursor,
        publisher and consumer operations. A Popen, PID, caller-supplied
        generation, exit code or boolean does not supply that proof. This
        method intentionally does not poll, wait, scan, ACK or mutate anything.
        A genuine journal event may still be processed by ``consume()`` under
        its existing ownership contract. See the recovery contract for the
        capabilities required before this method can reconcile absence.
        """
        raise ReconciliationUnavailable('NATIVE_EXIT_OWNERSHIP_PROOF_UNAVAILABLE')

    def allow_lobby(self, generation, host_counter):
        """Reserve durably before making the matching native gate visible."""
        if self.state.lease is not None:
            self.state.lease.require_native(generation)
        pending = self.state.state['pending']
        if pending is None:
            self.state.reserve_external(generation, host_counter)
        elif (pending.get('session_generation'), pending.get('host_counter')) != (generation, host_counter):
            raise ValueError('MAP_CYCLE_LOBBY_MISMATCH')
        return publish(self.journal, self.state, generation, host_counter)
