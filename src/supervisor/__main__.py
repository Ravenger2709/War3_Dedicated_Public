import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import signal
import sys
import threading

from .config import load_config
from .service import Supervisor


def serve(supervisor):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = supervisor.snapshot()
            if self.path == "/health/live":
                alive=getattr(supervisor,"live",supervisor.running)
                body, code = {"alive": alive}, 200 if alive else 503
            elif self.path == "/health/ready":
                body, code = status, 200 if status["ready"] else 503
            elif self.path == "/status":
                body, code = status, 200
            else:
                body, code = {"error": "not found"}, 404
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    http = supervisor.config["http"]
    server = ThreadingHTTPServer((http["host"], http["port"]), Handler)
    threading.Thread(target=server.serve_forever, name="status-http", daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description="Native Warcraft III hostbot supervisor")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check", action="store_true", help="validate config without starting processes")
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        if args.check:
            from src.connector import require_supported_connector
            require_supported_connector(cfg["mode"],cfg.get("iccup",{}).get("adapter"))
            print("configuration valid; runtime/client compatibility not checked")
            return 0
        if cfg["mode"]=="iccup":
            from src.runtime.controller import ICCupSupervisor
            supervisor=ICCupSupervisor(cfg)
        elif "gameplay" in cfg:
            from src.runtime.controller import OfflineProductionSupervisor
            supervisor=OfflineProductionSupervisor(cfg)
        else:
            supervisor=Supervisor(cfg)
        try:
            server = serve(supervisor)
        except Exception:
            if hasattr(supervisor,"backend"):supervisor.backend.close()
            supervisor.lockfile.close()
            raise
    except Exception as error:
        # Config values and account files must not be printed on error.
        from src.connector import UnsupportedAuthentication
        from src.runtime.presets import AIEvidenceError
        if isinstance(error, AIEvidenceError):
            print(json.dumps({"error": "PRESET_AI_EVIDENCE_REJECTED", "message":
                              "AI cannot be enabled: no unique evidence matches this map and script. "
                              "Disable AI or check the map and preset approvals."}), file=sys.stderr)
        else:
            code = "ICCup_AUTH_BLOCKED" if isinstance(error, UnsupportedAuthentication) else "configuration_or_startup_failed"
            print(json.dumps({"error": code, "type": type(error).__name__}), file=sys.stderr)
        return 2

    def shutdown(*_):
        # Signals must remain effective if the diagnostic queue is full.
        supervisor.want_running = False
        supervisor.running = False

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)

    def console():
        for line in sys.stdin:
            print(json.dumps(supervisor.submit(line.strip().lower())), flush=True)

    threading.Thread(target=console, name="local-console", daemon=True).start()
    supervisor.start().join()
    server.shutdown()
    server.server_close()
    return 3 if getattr(supervisor,"engine_observed",False) or getattr(supervisor,"permanent_errors",{}).get("supervisor") else 0


if __name__ == "__main__":
    sys.exit(main())
