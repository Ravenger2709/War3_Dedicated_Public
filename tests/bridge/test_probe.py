from contextlib import contextmanager
import json
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from src.bridge.probe import ProbeConfig, probe
from test_bncs import packet


@contextmanager
def mock_server(handler, *, connections=1, host="127.0.0.1"):
    errors = []
    received = bytearray()
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as server:
        server.bind((host, 0))
        server.listen()
        server.settimeout(3)

        def run():
            try:
                for attempt in range(connections):
                    with server.accept()[0] as client:
                        client.settimeout(2)
                        handler(client, attempt)
                        try:
                            received.extend(client.recv(128))
                        except (ConnectionResetError, socket.timeout):
                            pass
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            yield server.getsockname()[1], received
        finally:
            worker.join(timeout=4)
            if worker.is_alive():
                raise AssertionError("mock server did not stop")
            if errors:
                raise errors[0]


class ProbeTests(unittest.TestCase):
    def test_external_dns_mapped_and_noncanonical_hosts_rejected_before_socket(self):
        with patch("socket.socket", side_effect=AssertionError("no socket")):
            for host in ("localhost", "127.0.0.2", "0.0.0.0", "198.51.100.21", "example.com",
                         "::ffff:127.0.0.1", "::", "::1%lo", "127.1", "2130706433"):
                with self.assertRaises(ValueError):
                    ProbeConfig(host=host)

    def test_limits_are_bounded(self):
        for changes in ({"timeout": float("nan")}, {"timeout": float("inf")}, {"attempts": 6},
                        {"attempts": True}, {"port": 0}, {"timeout": 11}, {"max_frames": 33},
                        {"backoff_initial": 4, "backoff_max": 1}):
            with self.assertRaises(ValueError):
                ProbeConfig(**changes)

    def test_fragmented_ipv4_and_ipv6_receive_without_sending(self):
        def handler(client, _):
            for byte in packet(0x25, b"test"):
                client.sendall(bytes([byte]))
        for host in ("127.0.0.1", "::1"):
            with self.subTest(host=host), mock_server(handler, host=host) as (port, received):
                with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")):
                    result = probe(ProbeConfig(host=host, port=port, max_frames=1))
            self.assertEqual(result["outcome"], "PROBE_FRAMES_OBSERVED")
            self.assertEqual(received, b"")
            self.assertFalse(result["ready"])
            self.assertFalse(result["iccup_authenticated"])

    def test_guard_and_auth_are_not_answered_and_never_retried(self):
        for sid in (0x5e, 0x51, 0x53, 0x54):
            with mock_server(lambda client, _: client.sendall(packet(sid, b"MUST-NOT-LOG"))) as (port, received):
                result = probe(ProbeConfig(port=port, attempts=5))
            self.assertIn("UNSUPPORTED", result["outcome"])
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(received, b"")
            self.assertNotIn("MUST-NOT-LOG", json.dumps(result))

    def test_disconnect_then_retry_with_capped_exponential_backoff(self):
        def handler(client, attempt):
            if attempt == 3:
                client.sendall(packet(0))
            client.shutdown(socket.SHUT_WR)
        with mock_server(handler, connections=4) as (port, received):
            result = probe(ProbeConfig(port=port, attempts=4, backoff_initial=.01, backoff_max=.02, max_frames=1))
        self.assertEqual([e["delay_seconds"] for e in result["events"] if e["event"] == "PROBE_BACKOFF"], [.01, .02, .02])
        self.assertEqual(result["outcome"], "PROBE_FRAMES_OBSERVED")
        self.assertEqual(received, b"")

    def test_empty_disconnect_has_finite_retry_budget(self):
        with mock_server(lambda client, _: client.shutdown(socket.SHUT_WR), connections=2) as (port, _):
            result = probe(ProbeConfig(port=port, attempts=2, backoff_initial=.01))
        self.assertEqual(len([e for e in result["events"] if e["event"] == "PROBE_PEER_CLOSED"]), 2)
        self.assertEqual(result["frames_observed"], 0)

    def test_truncation_is_reported_and_not_retried(self):
        def handler(client, _):
            client.sendall(packet(0x25, b"test")[:-1])
            client.shutdown(socket.SHUT_WR)
        with mock_server(handler) as (port, _):
            result = probe(ProbeConfig(port=port, attempts=5))
        self.assertEqual(result["outcome"], "BNCS_TRUNCATED_FRAME")
        self.assertEqual(len(result["events"]), 1)

    def test_slow_drip_does_not_extend_attempt_deadline(self):
        def handler(client, _):
            for byte in packet(0x25, b"test"):
                try:
                    client.sendall(bytes([byte]))
                    time.sleep(.03)
                except (BrokenPipeError, ConnectionResetError):
                    break
        with mock_server(handler) as (port, _):
            started = time.monotonic()
            result = probe(ProbeConfig(port=port, timeout=.08))
            elapsed = time.monotonic() - started
        self.assertEqual(result["outcome"], "PROBE_TIMEOUT")
        self.assertLess(elapsed, .5)

    def test_cancellation_interrupts_backoff_and_closes_connection(self):
        stop = threading.Event()
        timer = threading.Timer(.05, stop.set)
        with mock_server(lambda client, _: client.shutdown(socket.SHUT_WR)) as (port, _):
            timer.start()
            started = time.monotonic()
            result = probe(ProbeConfig(port=port, attempts=5, backoff_initial=2), stop)
            timer.join()
        self.assertEqual(result["outcome"], "PROBE_CANCELLED")
        self.assertLess(time.monotonic() - started, .6)

    def test_cli_sigterm_interrupts_read_and_reports_closed_not_ready(self):
        accepted = threading.Event()

        def handler(client, _):
            accepted.set()
            self.assertEqual(client.recv(1), b"")

        with mock_server(handler) as (port, received):
            child = subprocess.Popen([sys.executable, "-m", "src.bridge", "probe", "--port", str(port),
                                      "--timeout", "10", "--attempts", "5"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(accepted.wait(2))
                started = time.monotonic()
                child.send_signal(signal.SIGTERM)
                output, errors = child.communicate(timeout=2)
                self.assertLess(time.monotonic() - started, .6)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)
        result = json.loads(output)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(errors, b"")
        self.assertEqual(result["outcome"], "PROBE_CANCELLED")
        self.assertFalse(result["ready"])
        self.assertEqual(received, b"")


if __name__ == "__main__":
    unittest.main()
