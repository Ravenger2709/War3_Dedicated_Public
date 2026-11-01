#!/usr/bin/env python3
"""Native GHost BNCS prefix against a local mock, with no auth acceptance.

Run in the restricted network=none container documented in iccup-bridge.md.
Uses no Warcraft assets. Synthetic account data never reaches ACCOUNTLOGON:
the mock sends no AUTH_INFO challenge, AUTH_CHECK success or Guard traffic.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.bridge.bncs import FrameDecoder  # noqa: E402


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def wait_log(path, marker, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = path.read_text(errors="replace") if path.exists() else ""
        if marker in text:
            return text
        time.sleep(.05)
    raise RuntimeError("NATIVE_EXPECTED_DIAGNOSTIC_MISSING")


def exact(connection, size, timeout=5):
    deadline = time.monotonic() + timeout
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        require(remaining > 0, "NATIVE_READ_TIMEOUT")
        connection.settimeout(remaining)
        block = connection.recv(size - len(result))
        require(block, "NATIVE_UNEXPECTED_EOF")
        result.extend(block)
    return bytes(result)


def receive_frame(connection):
    header = exact(connection, 4)
    length = struct.unpack_from("<H", header, 2)[0]
    require(header[0] == 255 and 4 <= length <= 4096, "NATIVE_FRAME_INVALID")
    return FrameDecoder().feed(header + exact(connection, length - 4))[0]


def initial_sequence(connection):
    require(exact(connection, 1) == b"\x01", "NATIVE_SELECTOR_INVALID")
    frame = receive_frame(connection)
    require(frame.packet_id == 0x50, "NATIVE_AUTH_INFO_MISSING")
    require(len(frame.payload) >= 38, "NATIVE_AUTH_INFO_TOO_SHORT")
    require(frame.payload[4:8] == b"68XI", "NATIVE_PLATFORM_INVALID")
    require(frame.payload[8:12] == b"PX3W", "NATIVE_PRODUCT_INVALID")
    require(frame.payload[12:16] == bytes([26, 0, 0, 0]), "NATIVE_VERSION_INVALID")
    # A real fragmented ping exercises the native parser/serializer. It does
    # not assert anything about iCCup, authentication, ICCGuard or Warden.
    ping = b"\xff\x25\x08\x00mock"
    connection.sendall(ping[:3])
    time.sleep(.02)
    connection.sendall(ping[3:])
    reply = receive_frame(connection)
    require(reply.packet_id == 0x25 and reply.payload == b"mock", "NATIVE_PING_REPLY_INVALID")
    return {"selector": "0x01", "client_auth_info": frame.metadata(), "fragmented_ping_echo": True}


def run_case(binary, root, kind, reconnect):
    directory = root / kind
    directory.mkdir(mode=0o700)
    config = directory / "ghost.cfg"
    # All values here are synthetic and fixed; no real secret loader/config.
    config.write_text("\n".join([
        "bot_log =", "bot_language = /opt/war3/share/language.cfg", "bot_tft = 1",
        "bot_bindaddress = 127.0.0.1", "bot_hostport = 16113", "bot_reconnect = 0",
        "bot_war3path = missing-game/", "bot_mappath = missing-maps/",
        "bot_mapcfgpath = missing-mapcfg/", "bot_defaultmap = none.cfg",
        "autohost_maxgames = 0", "admingame_create = 0", "bot_savereplays = 0",
        "udp_broadcasttarget = 127.0.0.1", "db_type = sqlite3", "db_sqlite3_file = ghost.dbs",
        "bnet_server = 127.0.0.1", "bnet_serveralias = LocalMockOnly",
        "bnet_username = SyntheticOnly", "bnet_password = synthetic-not-a-real-account",
        "bnet_cdkeyroc = FFFFFFFFFFFFFFFFFFFFFFFFFF", "bnet_cdkeytft = FFFFFFFFFFFFFFFFFFFFFFFFFF",
        "bnet_bnlsserver =", "bnet_custom_war3version = 26", "bnet_custom_passwordhashtype =",
        "bnet_firstchannel = SyntheticOnly", "bnet_publiccommands = 0", "",
    ]))
    config.chmod(0o600)
    log = directory / "stdout.txt"
    started = time.monotonic()
    child = None
    result = {"scenario": kind}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server, log.open("wb") as output:
        server.bind(("127.0.0.1", 6112))
        server.listen(1)
        server.settimeout(10)
        try:
            child = subprocess.Popen([str(binary), str(config)], cwd=directory, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            with server.accept()[0] as connection:
                result.update(initial_sequence(connection))
                if kind == "bad-header":
                    connection.sendall(b"\xfe\x00\x04\x00")
                    marker = "bad header constant"
                elif kind == "bad-length":
                    connection.sendall(b"\xff\x00\x03\x00")
                    marker = "bad length"
                else:
                    connection.shutdown(socket.SHUT_RDWR)
                    marker = "disconnected from battle.net"
                wait_log(log, marker)
            wait_log(log, "waiting 90 seconds to reconnect")
            result["disconnect_observed"] = True
            if reconnect:
                print(json.dumps({"event": "NATIVE_MOCK_WAITING_FOR_90S_RECONNECT", "scope": "local_mock_only"}), flush=True)
                disconnected = time.monotonic()
                server.settimeout(98)
                with server.accept()[0] as connection:
                    result["reconnected_sequence"] = initial_sequence(connection)
                    result["reconnect_wait_seconds"] = round(time.monotonic() - disconnected, 3)
                require(87 <= result["reconnect_wait_seconds"] <= 98, "NATIVE_RECONNECT_INTERVAL_INVALID")
        finally:
            if child is not None:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
                    raise RuntimeError("NATIVE_STOP_TIMEOUT") from None
                require(child.returncode == 0, "NATIVE_EXIT_NOT_ZERO")
    text = log.read_text(errors="replace")
    require("logon successful" not in text, "NATIVE_UNEXPECTED_AUTHENTICATION")
    require("creating game [" not in text, "NATIVE_UNEXPECTED_LOBBY")
    require("synthetic-not-a-real-account" not in text, "NATIVE_SYNTHETIC_PASSWORD_LEAK")
    require("all threads finished, exiting nicely" in text, "NATIVE_CLEANUP_MARKER_MISSING")
    result.update({"exit_code": child.returncode, "authenticated": False, "lobby_created": False,
                   "native_log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                   "elapsed_seconds": round(time.monotonic() - started, 3)})
    print(json.dumps({"event": "NATIVE_MOCK_CASE_PASS", "scenario": kind}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ghost", default="/opt/war3/bin/ghost++")
    parser.add_argument("--check-reconnect", action="store_true", help="wait for native 90-second retry once")
    args = parser.parse_args()
    require(os.geteuid() != 0, "NATIVE_TEST_REQUIRES_NONROOT")
    require(Path("/.dockerenv").exists(), "NATIVE_TEST_REQUIRES_CONTAINER")
    status = Path("/proc/self/status").read_text()
    require("NoNewPrivs:\t1" in status and "CapEff:\t0000000000000000" in status, "NATIVE_TEST_REQUIRES_RESTRICTED_CONTAINER")
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    require(not any(line.split()[1] == "00000000" for line in routes), "NATIVE_TEST_NO_DEFAULT_ROUTE_REQUIRED")
    binary = Path(args.ghost)
    with binary.open("rb") as stream:
        require(stream.read(4) == b"\x7fELF", "NATIVE_TEST_REQUIRES_ELF")
    with tempfile.TemporaryDirectory(prefix="bridge-native-") as temp:
        cases = [run_case(binary, Path(temp), kind, args.check_reconnect and kind == "peer-close")
                 for kind in ("bad-header", "bad-length", "peer-close")]
    print(json.dumps({"result": "PASS", "scope": "native_ghost_local_bncs_mock_only",
                      "ghost_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                      "cases": cases, "iccup_authenticated": False, "real_account_used": False,
                      "guard_or_warden_emulated": False, "proprietary_files_used": False}, sort_keys=True))


if __name__ == "__main__":
    main()
