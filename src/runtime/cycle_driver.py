"""Orchestrate catalog selections and the fenced cycle journal.

The opt-in supervisor adapter verifies native child ownership, observation
freshness and capacity independently before using this pump. A native final
admission check serializes lobby capacity and staged identity; the snapshot is
still only advisory. Production native launch remains blocked until recovery
and broader validation are proven. ACK means staging only; missing or
conflicting state fails closed.
A catalog-bound close v2 durably records its exact request before retirement.
"""
from dataclasses import dataclass
import re
import time

from .catalog_requests import CatalogRequests, CatalogRequestError
from .cycle_coordinator import CycleCoordinator
from .cycle_state import CycleState


_HEX = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)


@dataclass(frozen=True)
class LobbyObservation:
    """Validated candidate matching the native catalog lobby identity."""
    generation: str
    request_id: str
    catalog_index: int
    host_counter: int

    def __post_init__(self):
        if (type(self.generation) is not str or not _HEX.fullmatch(self.generation)
                or type(self.request_id) is not str or not _HEX.fullmatch(self.request_id)
                or type(self.catalog_index) is not int or not 0 <= self.catalog_index < 32
                or type(self.host_counter) is not int or not 1 <= self.host_counter < 2**28):
            raise ValueError('invalid lobby observation')


@dataclass(frozen=True)
class DriverResult:
    state: str
    request_id: str | None = None
    marker: str | None = None
    retry_seconds: int = 0
    preset_id: str | None = None


class CycleDriver:
    """Single-owner offline pump. Exceptions require the caller to reconcile.

    ``capacity_available`` controls only admission of a *new* selection. A full
    server is normal, and in-flight work is still recovered. Set ``select`` only
    when the caller wishes to admit a new selection; retries never allocate IDs.
    """
    def __init__(self, catalog, cursor, journal, *, clock=time.monotonic):
        if not isinstance(catalog, CatalogRequests) or not isinstance(cursor, CycleState):
            raise TypeError('catalog and cursor required')
        if cursor.cycle != catalog.cycle:
            raise ValueError('catalog and cursor cycle mismatch')
        self.catalog, self.cursor, self.journal = catalog, cursor, journal
        self.coordinator = CycleCoordinator(cursor, journal, catalog_requests=catalog)
        self.clock = clock
        self.retry_at = 0.0
        self.rejected_positions = set()
        self.rejected_rounds = 0

    def _skip_rejected(self, record):
        if self.catalog.rejection_cleanup_pending(record):
            return DriverResult('awaiting_ack_cleanup', record['request_id'])
        self.catalog.retire_rejected(self.cursor, record['request_id'])
        self.rejected_positions.add(record['position'])
        delay = 1
        if len(self.rejected_positions) == len(self.catalog.cycle):
            self.rejected_rounds = min(self.rejected_rounds + 1, 5)
            delay = min(30 * 2 ** (self.rejected_rounds - 1), 300)
            self.rejected_positions.clear()
        self.retry_at = self.clock() + delay
        return DriverResult('rejected_skipped', record['request_id'], retry_seconds=delay,
                            preset_id=self.catalog.cycle[record['position']])

    def _owned(self):
        self.cursor._require_lease()
        if self.cursor.lease is not None:
            self.cursor.lease.require_native(self.catalog.generation)

    def _matches(self, observation, record):
        return (record is not None and observation.generation == self.catalog.generation
                and observation.request_id == record['request_id']
                and observation.catalog_index == record['index']
                and (record['counter'] is None or record['counter'] == observation.host_counter))

    def _retire(self):
        record = self.catalog.data['pending']
        if record is None or record['counter'] is None:
            return None
        state = self.catalog._cursor(self.cursor)
        if state['version'] != 3:
            return None
        generation, counter = self.catalog.generation, record['counter']
        if self.cursor._find_closed(generation, counter):
            self.catalog.retire_closed(self.cursor, record['request_id'])
            return 'idle_rotated' if self.cursor._find_idle(generation,counter) else 'closed'
        if self.cursor._find_external('counter', generation, counter):
            self.catalog.complete(self.cursor, record['request_id'])
            return 'started'
        return None

    def step(self, observation=None, *, select=False, capacity_available=False, revalidate=None):
        """Process a journal first, then retry/select or bind a matching lobby.

        Wrong/stale observations return ``ignored`` before journal or other I/O.
        A consumed start/close retires the request and ends the step: rehosting
        needs a fresh, explicit selection on a subsequent call.
        """
        if revalidate is not None and not callable(revalidate):
            raise ValueError('revalidate must be callable')
        def permitted():
            return revalidate is None or revalidate() is True
        if type(select) is not bool or type(capacity_available) is not bool:
            raise ValueError('capacity and select must be booleans')
        if observation is not None and type(observation) is not LobbyObservation:
            raise ValueError('invalid observation type')
        record = self.catalog.data['pending']
        if observation is not None and not self._matches(observation, record):
            return DriverResult('ignored')
        if self.catalog.failed:
            raise CatalogRequestError('storage uncertain; reopen')
        self._owned()
        self.coordinator.consume()
        if (record is not None and self.catalog.data['pending'] is None
                and self.catalog.data['completed'] is not None
                and self.catalog.data['completed']['request_id'] == record['request_id']
                and self.catalog.data['completed'].get('outcome') == 'closed'):
            return DriverResult('idle_rotated' if self.cursor._find_idle(self.catalog.generation,record['counter']) else 'closed', record['request_id'])
        outcome = self._retire()
        if outcome:
            return DriverResult(outcome, record['request_id'])
        record = self.catalog.data['pending']
        # Resume a cursor-committed rejection before enqueue's position check.
        if record is not None and record['status'] == 'rejected':
            if observation is not None:
                return DriverResult('ignored')
            if not permitted():
                return DriverResult('observation_stale')
            return self._skip_rejected(record)
        if record is None:
            if not select:
                return DriverResult('idle')
            if not capacity_available:
                return DriverResult('capacity_full')
            if self.clock() < self.retry_at:
                return DriverResult('retry_wait')
            if not permitted():
                return DriverResult('observation_stale')
            self.catalog.enqueue(self.cursor)
            record = self.catalog.data['pending']
        request_id = record['request_id']
        if record['counter'] is not None:
            if observation is None:
                return DriverResult('awaiting_lifecycle', request_id)
            if not permitted():
                return DriverResult('observation_stale', request_id)
            marker = self.catalog.bind_reservation(self.cursor, request_id, observation.host_counter, self.journal)
            self.rejected_positions.clear()
            self.rejected_rounds = 0
            return DriverResult('reserved', request_id, marker)
        if not permitted():
            return DriverResult('observation_stale', request_id)
        _, status = self.catalog.dispatch(self.cursor)
        if status == 'rejected' and observation is None:
            return self._skip_rejected(self.catalog.data['pending'])
        if status in ('rejected', 'uncertain'):
            return DriverResult(status, request_id)
        if status != 'staged':
            return DriverResult('awaiting_ack', request_id)
        if observation is None:
            return DriverResult('awaiting_lobby', request_id)
        if not permitted():
            return DriverResult('observation_stale', request_id)
        marker = self.catalog.bind_reservation(self.cursor, request_id, observation.host_counter, self.journal)
        self.rejected_positions.clear()
        self.rejected_rounds = 0
        return DriverResult('reserved', request_id, marker)
