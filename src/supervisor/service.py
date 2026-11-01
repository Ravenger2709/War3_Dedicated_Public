"""Single-child controller. All mutations happen on one control loop."""
import fcntl
import json
import os
from pathlib import Path
import queue
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from src.connector import require_supported_connector
from .events import classify, sanitize


STATES = {"STOPPED", "STARTING", "CONNECTING", "AUTHENTICATING", "CONNECTED", "PUBLISHING", "READY", "LOBBY", "COUNTDOWN", "IN_GAME", "RECONNECTING", "ERROR", "STOPPING"}


class Supervisor:
    def __init__(self, config, output=None):
        self.config = config
        self.output = output or sys.stdout
        self.runtime = Path(config["runtime_dir"])
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lockfile = (self.runtime / "supervisor.lock").open("a")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lockfile.close()
            raise RuntimeError("another supervisor owns this runtime directory") from None
        self.events = queue.Queue(maxsize=2048)
        self.guard = threading.RLock()
        self.process = None
        self.generation = 0
        self.state = "STOPPED"
        self.reason = "initialized"
        self.entered = time.monotonic()
        self.started = None
        self.restarts = 0
        self.want_running = True
        self.running = True
        self.retry_at = 0.0
        self.last_exit_code = None
        self.last_log_time = None
        self.history = []
        self.thread = None
        self.reader = None
        self.stopping_child = False
        self._save()

    def snapshot(self):
        with self.guard:
            alive = self.process is not None and self.process.poll() is None
            return {"state": self.state, "reason": self.reason, "mode": self.config["mode"],
                    "pid": self.process.pid if alive else None,
                    "child_alive": alive, "ready": alive and self.state in {"READY", "LOBBY", "IN_GAME"},
                    "iccup_authenticated": False, "restarts": self.restarts,
                    "last_exit_code": self.last_exit_code, "last_log_time": self.last_log_time,
                    "history": list(self.history[-20:])}

    def _save(self):
        dest = self.runtime / "status.json"
        temp = dest.with_suffix(".tmp")
        temp.write_text(json.dumps(self.snapshot(), ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(dest)

    def log(self, kind, **fields):
        item = {"time": datetime.now(timezone.utc).isoformat(), "event": kind, **fields}
        data = json.dumps(item, ensure_ascii=False)
        with self.guard:
            print(data, file=self.output, flush=True)
            journal = self.runtime / "supervisor.jsonl"
            if journal.exists() and journal.stat().st_size > 10 * 1024 * 1024:
                for index in (2, 1):
                    old = journal.with_suffix(f".jsonl.{index}")
                    if old.exists():
                        old.replace(journal.with_suffix(f".jsonl.{index + 1}"))
                journal.replace(journal.with_suffix(".jsonl.1"))
            with journal.open("a", encoding="utf-8") as f:
                f.write(data + "\n")

    def transition(self, state, reason):
        assert state in STATES
        with self.guard:
            self.state, self.reason, self.entered = state, reason, time.monotonic()
            self.history.append({"state": state, "reason": reason})
            del self.history[:-20]
            self.log("state", state=state, reason=reason)
            self._save()

    def _reader(self, process, generation):
        try:
            while True:
                line = process.stdout.readline(65537)
                if not line:
                    break
                if generation == self.generation:
                    self.last_log_time = datetime.now(timezone.utc).isoformat()
                    self.log("hostbot", message=sanitize(line))
                # Backpressure bounds memory without silently losing lifecycle events.
                while self.running and generation == self.generation and not self.stopping_child:
                    try:
                        self.events.put(("line", generation, line), timeout=0.1)
                        break
                    except queue.Full:
                        pass
        finally:
            process.stdout.close()

    def _launch(self):
        require_supported_connector(self.config["mode"])
        self.transition("CONNECTING", "starting_native_hostbot")
        cfg = self.config["hostbot"]
        wrapper = str(Path(__file__).with_name("child_exec.py").resolve())
        cmd = [sys.executable, wrapper, str(os.getpid()), *cfg["command"]]
        with self.guard:
            self.stopping_child = False
            self.process = subprocess.Popen(cmd, cwd=cfg["cwd"], stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace",
                                            start_new_session=True, bufsize=1)
            self.started = time.monotonic()
            self.generation += 1
        self.reader = threading.Thread(target=self._reader, args=(self.process, self.generation), daemon=True)
        self.reader.start()
        self.log("child_started", pid=self.process.pid)

    def _terminate(self):
        proc = self.process
        if proc is None:
            return
        self.stopping_child = True
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=self.config["hostbot"]["stop_timeout"])
            except subprocess.TimeoutExpired:
                self.log("forced_stop", pid=proc.pid)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            except ProcessLookupError:
                proc.wait()
        self.last_exit_code = proc.returncode
        # The reader logs directly, including final diagnostics after SIGTERM.
        # Never discard these merely because the lifecycle loop is stopping.
        if self.reader is not None:
            self.reader.join(timeout=1)
        self.process = None
        self.generation += 1

    def _failure(self, reason):
        self._terminate()
        limit = self.config["recovery"]["max_restarts"]
        if self.restarts >= limit:
            self.want_running = False
            self.transition("ERROR", reason + ":restart_budget_exhausted")
            return
        delay = min(self.config["recovery"]["backoff_initial"] * 2 ** self.restarts,
                    self.config["recovery"]["backoff_max"])
        self.restarts += 1
        delay = min(delay * random.uniform(0.8, 1.2), self.config["recovery"]["backoff_max"])
        self.retry_at = time.monotonic() + delay
        self.transition("RECONNECTING", reason)
        self.log("retry_scheduled", attempt=self.restarts, delay_seconds=round(delay, 3))

    def submit(self, command):
        if command not in {"status", "start", "stop", "restart", "quit"}:
            return {"error": "commands: status, start, stop, restart, quit"}
        if command == "status":
            return self.snapshot()
        try:
            self.events.put_nowait(("command", command))
        except queue.Full:
            return {"error": "controller busy; retry command"}
        return {"queued": command}

    def _command(self, command):
        if command in {"stop", "restart", "quit"}:
            self.want_running = False
            self._terminate()
            self.transition("STOPPED", "operator_" + command)
        if command in {"start", "restart"} and self.process is None:
            self.restarts = 0
            self.retry_at = 0
            self.want_running = True
        if command == "quit":
            self.running = False

    def _line(self, generation, line):
        if generation != self.generation:
            return
        event = classify(line)
        if event == "fatal":
            self._failure("hostbot_initialization_or_listen_failure")
        elif event == "initialized" and self.state == "CONNECTING":
            self.transition("READY", "native_offline_initialization_complete")
        elif event == "lobby":
            self.transition("LOBBY", "hostbot_listening")
        elif event == "in_game":
            self.transition("IN_GAME", "hostbot_loading_started")
        elif event == "game_deleted" and self.state in {"LOBBY", "IN_GAME"}:
            self.transition("READY", "hostbot_game_deleted")

    def run(self):
        try:
            while self.running:
                now = time.monotonic()
                if self.want_running and self.process is None and now >= self.retry_at:
                    try:
                        self._launch()
                    except Exception as error:
                        # Avoid leaking argv, secrets, exception contents or configuration.
                        self.log("launch_error", error_type=type(error).__name__)
                        if self.config["mode"] == "iccup":
                            self.want_running = False
                            self.transition("ERROR", "ICCup_AUTH_BLOCKED")
                        else:
                            self._failure("hostbot_launch_failed")
                try:
                    event = self.events.get(timeout=0.05)
                    if event[0] == "command":
                        self._command(event[1])
                    else:
                        self._line(event[1], event[2])
                except queue.Empty:
                    pass
                if self.process is not None:
                    now = time.monotonic()
                    if self.process.poll() is not None:
                        self._failure("hostbot_exited")
                    elif self.state == "CONNECTING" and now - self.started > self.config["hostbot"]["startup_timeout"]:
                        self._failure("startup_timeout")
                    elif self.state == "LOBBY" and now - self.entered > self.config["hostbot"]["lobby_timeout"]:
                        self._failure("lobby_timeout")
                    elif self.state in {"READY", "LOBBY", "IN_GAME"} and now - self.started > self.config["recovery"]["stable_seconds"]:
                        self.restarts = 0
        finally:
            self._terminate()
            self.transition("STOPPED", "supervisor_shutdown")
            self.running = False
            self.lockfile.close()

    def start(self):
        self.thread = threading.Thread(target=self.run, name="hostbot-controller", daemon=True)
        self.thread.start()
        return self.thread
