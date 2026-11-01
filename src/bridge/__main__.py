"""CLI: status, passive local probe, interactive secret provisioning/validation."""

import argparse
import getpass
import json
import signal
import sys
from threading import Event
import warnings

from . import status
from .probe import ProbeConfig, probe
from .secrets import SecretError, load_credentials, provision_credentials


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for command in ("secret-create", "secret-check"):
        secret = sub.add_parser(command)
        secret.add_argument("--path", required=True, help="absolute path; no credential values in argv")
    diagnostic = sub.add_parser("probe", help="passive only; sends no selector, auth, ping or Guard replies")
    diagnostic.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "::1"))
    diagnostic.add_argument("--port", type=int, default=6112)
    diagnostic.add_argument("--timeout", type=float, default=1.0, help="total seconds per attempt, including reads")
    diagnostic.add_argument("--attempts", type=int, default=1)
    diagnostic.add_argument("--max-frames", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result, code = status(), 78
        elif args.command == "secret-create":
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise SecretError("SECRET_INTERACTIVE_TTY_REQUIRED")
            # getpass normally falls back to echoed input if termios fails.
            # Refuse that fallback before it can consume any credential.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                username = getpass.getpass("Dedicated account username (hidden): ")
                password = getpass.getpass("Password (hidden): ")
                confirmation = getpass.getpass("Repeat password (hidden): ")
            if password != confirmation:
                raise SecretError("SECRET_CONFIRMATION_MISMATCH")
            provision_credentials(args.path, username, password)
            result, code = {"event": "SECRET_CREATED", "authentication_attempted": False}, 0
        elif args.command == "secret-check":
            load_credentials(args.path)
            result, code = {"event": "SECRET_FILE_VALID", "authentication_attempted": False}, 0
        else:
            config = ProbeConfig(host=args.host, port=args.port, timeout=args.timeout,
                                 attempts=args.attempts, max_frames=args.max_frames)
            stop = Event()
            old = {}
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    old[sig] = signal.signal(sig, lambda *_: stop.set())
                result = probe(config, stop)
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)
            code = 0 if result["outcome"] in ("PROBE_FRAMES_OBSERVED", "PROBE_PEER_CLOSED") and result["frames_observed"] else 1
    except (SecretError, ValueError) as error:
        result, code = {"event": str(error)}, 2
    except getpass.GetPassWarning:
        result, code = {"event": "SECRET_SECURE_TTY_REQUIRED"}, 2
    except (EOFError, KeyboardInterrupt):
        result, code = {"event": "BRIDGE_CANCELLED"}, 130
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
