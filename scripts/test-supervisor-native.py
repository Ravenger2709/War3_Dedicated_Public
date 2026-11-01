#!/usr/bin/env python3
"""Exercise the supervisor against the actual native GHost binary on loopback."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.supervisor.service import Supervisor
from src.supervisor.__main__ import serve


def wait_for(predicate, timeout=55):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise TimeoutError("native supervisor condition timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--host-port", type=int, default=16113)
    args = parser.parse_args()
    (ROOT / "runtime").mkdir(exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="supervisor-qa-", dir=ROOT / "runtime"))
    runtime.rmdir()  # prepare-map requires a new or empty destination.
    subprocess.run([sys.executable, str(ROOT / "scripts/prepare-map.py"),
                    "--game-dir", str(args.game_dir.resolve()), "--map", str(args.map.resolve()),
                    "--runtime", str(runtime), "--host-port", str(args.host_port),
                    "--name", "Supervisor QA"], check=True, stdout=subprocess.DEVNULL)
    config = {"mode": "offline", "runtime_dir": str(runtime / "supervisor"),
              "hostbot": {"command": [str(ROOT / "scripts/ghost-run.sh"), str(runtime / "ghost.cfg")],
                          "cwd": str(runtime), "startup_timeout": 50, "stop_timeout": 2,
                          "lobby_timeout": 300},
              "http": {"host": "127.0.0.1", "port": 0},
              "recovery": {"max_restarts": 2, "backoff_initial": 0.2,
                           "backoff_max": 1, "stable_seconds": 300}}
    (runtime / "supervisor.json").write_text(json.dumps(config, indent=2) + "\n")
    checks = []
    with (runtime / "supervisor-test.jsonl").open("w") as log:
        supervisor = Supervisor(config, output=log)
        server = serve(supervisor)
        try:
            supervisor.start()
            wait_for(lambda: supervisor.snapshot()["state"] == "LOBBY")
            first_pid = supervisor.snapshot()["pid"]
            checks.append("actual_native_lobby_observed")
            with urlopen(f"http://127.0.0.1:{server.server_port}/health/ready", timeout=2) as response:
                status = json.load(response)
            assert status["ready"] and not status["iccup_authenticated"]
            checks.append("http_readiness_and_no_iccup_claim")
            os.kill(first_pid, signal.SIGKILL)
            wait_for(lambda: supervisor.snapshot()["state"] == "LOBBY"
                     and supervisor.snapshot()["pid"] not in (None, first_pid))
            assert any(x["state"] == "RECONNECTING" for x in supervisor.history)
            checks.append("native_child_crash_backoff_rehost")
        finally:
            supervisor.submit("quit")
            if supervisor.thread:
                supervisor.thread.join(8)
                assert not supervisor.thread.is_alive()
            server.shutdown()
            server.server_close()
        checks.append("native_child_reaped_on_shutdown")
        # Same on-disk SQLite/runtime reused after complete supervisor restart.
        recovered = Supervisor(config, output=log)
        try:
            recovered.start()
            wait_for(lambda: recovered.state == "LOBBY")
            checks.append("persistent_runtime_restart_rehost")
        finally:
            recovered.submit("quit")
            recovered.thread.join(8)
            assert not recovered.thread.is_alive()
    result = {"result": "PASS", "scope": "native GHost + supervisor, loopback, no players or iCCup",
              "checks": checks, "runtime": str(runtime)}
    (runtime / "supervisor-test-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
