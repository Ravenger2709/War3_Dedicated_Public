"""Tests of supervision, not game or iCCup compatibility claims."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from src.connector import UnsupportedAuthentication, require_supported_connector
from src.supervisor.config import load_config
from src.supervisor.events import classify, sanitize
from src.supervisor.service import Supervisor
from src.supervisor.__main__ import serve


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {"mode": "offline", "runtime_dir": str(self.root / "run"),
                    "hostbot": {"command": [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
                                "cwd": str(self.root), "startup_timeout": 2,
                                "stop_timeout": 0.2, "lobby_timeout": 2},
                    "http": {"host": "127.0.0.1", "port": 0},
                    "recovery": {"max_restarts": 1, "backoff_initial": 0.02,
                                 "backoff_max": 0.02, "stable_seconds": 60}}
        self.services = []

    def tearDown(self):
        for service in self.services:
            if service.thread:
                service.submit("quit")
                service.thread.join(3)
                self.assertFalse(service.thread.is_alive())
            elif not service.lockfile.closed:
                service.lockfile.close()
        self.tmp.cleanup()

    def make(self, cfg=None):
        s = Supervisor(cfg or self.cfg, output=io.StringIO())
        self.services.append(s)
        return s

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition timed out")

    def test_connector_fails_before_network(self):
        with self.assertRaises(UnsupportedAuthentication):
            require_supported_connector("iccup")
        cfg = copy.deepcopy(self.cfg)
        cfg["mode"] = "iccup"
        s = self.make(cfg)
        s.start()
        self.wait_for(lambda: s.state == "ERROR")
        self.assertIsNone(s.process)
        self.assertEqual(s.restarts, 0)
        self.assertFalse(s.snapshot()["iccup_authenticated"])

    def test_duplicate_owner_rejected(self):
        self.make()
        with self.assertRaises(RuntimeError):
            Supervisor(self.cfg, output=io.StringIO())

    def test_crash_budget_and_manual_restart(self):
        self.cfg["hostbot"]["command"] = [sys.executable, "-c", "raise SystemExit(7)"]
        s = self.make()
        s.start()
        self.wait_for(lambda: s.state == "ERROR")
        self.assertEqual(s.restarts, 1)
        self.assertEqual(s.last_exit_code, 7)
        self.assertIn("restart_budget_exhausted", s.reason)
        s.submit("start")
        self.wait_for(lambda: sum(x["state"] == "CONNECTING" for x in s.history) >= 3)

    def test_lifecycle_and_stop_reaps_child(self):
        self.cfg["hostbot"]["command"] = [sys.executable, "-u", "-c",
            "import time; print('[GAME: test] listening on port 6112'); time.sleep(60)"]
        s = self.make()
        s.start()
        self.wait_for(lambda: s.state == "LOBBY")
        proc = s.process
        self.assertTrue(s.snapshot()["ready"])
        s.events.put(("line", s.generation, "[GAME: test] started loading with 2 players\n"))
        self.wait_for(lambda: s.state == "IN_GAME")
        s.events.put(("line", s.generation, "[GHOST] deleting game [test]\n"))
        self.wait_for(lambda: s.state == "READY")
        s.submit("stop")
        self.wait_for(lambda: s.state == "STOPPED" and s.process is None)
        self.assertIsNotNone(proc.poll())
        status = json.loads((s.runtime / "status.json").read_text())
        self.assertFalse(status["child_alive"])

    def test_watchdog_circuit_breaker(self):
        self.cfg["hostbot"]["startup_timeout"] = 0.05
        self.cfg["recovery"]["max_restarts"] = 0
        s = self.make()
        s.start()
        self.wait_for(lambda: s.state == "ERROR")
        self.assertIn("startup_timeout", s.reason)
        self.assertIsNone(s.process)

    def test_unresponsive_child_is_killed_and_reaped(self):
        self.cfg["hostbot"]["command"] = [sys.executable, "-u", "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('[GAME: x] listening on port 6112'); time.sleep(60)"]
        s = self.make()
        s.start()
        self.wait_for(lambda: s.state == "LOBBY")
        proc = s.process
        s.submit("stop")
        self.wait_for(lambda: s.process is None)
        self.assertEqual(proc.poll(), -9)
        self.assertIn("forced_stop", (s.runtime / "supervisor.jsonl").read_text())

    def test_lobby_timeout(self):
        self.cfg["hostbot"]["lobby_timeout"] = 0.05
        self.cfg["recovery"]["max_restarts"] = 0
        self.cfg["hostbot"]["command"] = [sys.executable, "-u", "-c",
            "import time; print('[GAME: x] listening on port 6112'); time.sleep(60)"]
        s = self.make()
        s.start()
        self.wait_for(lambda: s.state == "ERROR")
        self.assertIn("lobby_timeout", s.reason)

    def test_initialized_idle_engine_does_not_restart(self):
        self.cfg["hostbot"]["startup_timeout"] = 1
        self.cfg["hostbot"]["command"] = [sys.executable, "-u", "-c",
            "import time; print('[GHOST] offline initialization complete'); time.sleep(60)"]
        s = self.make()
        s.start()
        self.wait_for(lambda: s.state == "READY")
        time.sleep(1.1)
        self.assertTrue(s.snapshot()["ready"])
        self.assertEqual(s.restarts, 0)

    def test_stale_output_and_redaction(self):
        s = self.make()
        s.generation = 2
        s._line(1, "[GAME: old] listening on port 6112")
        self.assertEqual(s.state, "STOPPED")
        s.lockfile.close()
        self.cfg["hostbot"]["command"] = [sys.executable, "-u", "-c",
            "import signal,time; "
            "signal.signal(signal.SIGTERM, lambda *_: (print('final diagnostic'), exit(0))); "
            "print('password = DONT_LOG_THIS'); print('[GAME: x] listening on port 6112'); time.sleep(60)"]
        s = self.make()
        s.start()
        self.wait_for(lambda: s.state == "LOBBY")
        s.submit("stop")
        self.wait_for(lambda: s.process is None)
        journal = (s.runtime / "supervisor.jsonl").read_text()
        self.assertNotIn("DONT_LOG_THIS", journal)
        self.assertIn("final diagnostic", journal)

    def test_http_readiness_and_no_remote_mutation(self):
        s = self.make()
        server = serve(s)
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(url + "/health/live") as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(HTTPError) as error:
                urlopen(url + "/health/ready")
            self.assertEqual(error.exception.code, 503)
            with self.assertRaises(HTTPError) as error:
                urlopen(url + "/restart", data=b"restart")
            self.assertEqual(error.exception.code, 501)
        finally:
            server.shutdown()
            server.server_close()

    def test_config_validation(self):
        path = self.root / "supervisor.json"
        path.write_text(json.dumps(self.cfg))
        self.assertEqual(load_config(path)["mode"], "offline")
        for change in ({"command": "sh -c anything"}, {"startup_timeout": float("nan")}):
            cfg = copy.deepcopy(self.cfg)
            cfg["hostbot"].update(change)
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_classification_requires_observed_listening(self):
        self.assertIsNone(classify("[GHOST] creating game [test]"))
        self.assertEqual(classify("[GAME: test] listening on port 6112"), "lobby")
        self.assertEqual(classify("[GAME: test] error listening on port 6112"), "fatal")
        self.assertEqual(classify("[GHOST] offline initialization complete"), "initialized")
        self.assertEqual(classify("[GHOST] offline initialization failed"), "fatal")
        self.assertIsNone(classify("[GAME: test] [Local]: started loading with 2 players"))
        self.assertIsNone(classify("[GAME: test] [Local]: [GHOST] deleting game [test]"))
        self.assertEqual(sanitize("AutoLoginCache=sensitive"), "[redacted sensitive diagnostic]")


if __name__ == "__main__":
    unittest.main()
