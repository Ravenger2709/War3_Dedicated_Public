"""Launch admission rejects ambiguous session history before creating a child."""
import os
from pathlib import Path
import tempfile
import threading
import unittest

from src.runtime.config import gameplay_config, iccup_config
from src.runtime.controller import ICCupSupervisor, RuntimeBackend
from src.runtime.production_cycle import ProductionCycleControl
from src.runtime.session_lease import SessionLease


class BackendCycleAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'cycle'
        lease=SessionLease.create(self.root,enabled=True);lease.close()
        self.control=ProductionCycleControl(self.root,['one','two']);self.addCleanup(self.control.close)
        self.config={'iccup':iccup_config({}),
                     'gameplay':gameplay_config({'map_cycle':['one','two'],'max_concurrent_matches':2})}
        self.service=object.__new__(ICCupSupervisor)
        self.service.config=self.config
        self.service.backend=RuntimeBackend(self.config)
        self.service.cycle_session={'root':str(self.root),'cycle':['one','two']}
        self.service.cycle_control=self.control
        self.service.prepared=True;self.service.cycle_failed=False

    def test_fresh_managed_session_only_allows_prevalidated_backend_attempt(self):
        self.assertTrue(self.service._cycle_permits_native_spawn())
        self.control.claim_native_launch()
        self.assertFalse(self.service._cycle_permits_native_spawn())
        self.assertEqual(os.listdir(self.control.lease.journal_fd),[])

    def test_unknown_journal_and_pending_never_authorize_launch(self):
        self.control.state.reserve_external('a'*32,4)
        self.assertFalse(self.service._cycle_permits_native_spawn())
        (self.control.lease.journal/'unknown').write_bytes(b'not exit proof')
        self.assertFalse(self.service._cycle_permits_native_spawn())

    def test_mismatched_config_failed_state_or_unprepared_backend_blocks(self):
        self.service.cycle_failed=True
        self.assertFalse(self.service._cycle_permits_native_spawn())
        self.service.cycle_failed=False;self.service.prepared=False
        self.assertFalse(self.service._cycle_permits_native_spawn())
        self.service.prepared=True
        self.service.cycle_session['cycle']=['two','one']
        self.assertFalse(self.service._cycle_permits_native_spawn())

    def test_missing_previously_provisioned_root_is_not_recreated(self):
        self.control.close()
        self.service.cycle_control=None
        self.service.guard=threading.RLock()
        self.service.log=lambda *args,**kwargs:None
        self.service._cycle_initialize()
        self.assertFalse(self.service.cycle_failed)
        self.service.cycle_control.close();self.service.cycle_control=None
        self.root.rename(self.root.with_name('previous-root'))
        self.service._cycle_initialize()
        self.assertTrue(self.service.cycle_failed)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.service._cycle_permits_native_spawn())

    def test_replaced_leased_root_blocks_instead_of_reopening(self):
        self.root.rename(self.root.with_name('retained'))
        self.root.mkdir(mode=0o700)
        self.assertFalse(self.service._cycle_permits_native_spawn())
