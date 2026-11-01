"""Passive, loopback-only diagnostics. Sends zero bytes and cannot set readiness."""

from dataclasses import dataclass
import math
import socket
from threading import Event
import time

from .bncs import FrameDecoder, FrameError, validate_server_frame


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    host: str = "127.0.0.1"
    port: int = 6112
    timeout: float = 1.0
    attempts: int = 1
    backoff_initial: float = 0.25
    backoff_max: float = 2.0
    max_frames: int = 4
    max_frame: int = 4096

    def __post_init__(self):
        # Numeric literals only: no DNS, alternate loopback range, mapped IPv6,
        # scoped addresses, arbitrary private ranges, or an allow-external flag.
        if self.host not in ("127.0.0.1", "::1"):
            raise ValueError("PROBE_LOOPBACK_LITERAL_REQUIRED")
        for value, low, high in ((self.port, 1, 65535), (self.attempts, 1, 5),
                                 (self.max_frames, 1, 32), (self.max_frame, 4, 65535)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("PROBE_LIMIT_INVALID")
        for value, low, high in ((self.timeout, 0.05, 10), (self.backoff_initial, 0.01, 5), (self.backoff_max, 0.01, 5)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("PROBE_LIMIT_INVALID")
        if self.backoff_initial > self.backoff_max:
            raise ValueError("PROBE_BACKOFF_INVALID")


def _observe(config: ProbeConfig, stop: Event) -> tuple[str, list[dict], bool]:
    family = socket.AF_INET6 if config.host == "::1" else socket.AF_INET
    decoder = FrameDecoder(config.max_frame)
    frames = []
    deadline = time.monotonic() + config.timeout
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(config.timeout)
            connection.connect((config.host, config.port))
            if connection.getpeername()[0] != config.host:
                return "PROBE_PEER_MISMATCH", [], False
            byte_budget = config.max_frame * config.max_frames
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "PROBE_TIMEOUT", frames, True
                connection.settimeout(min(remaining, 0.1))
                try:
                    data = connection.recv(min(config.max_frame, byte_budget))
                except socket.timeout:
                    continue
                if not data:
                    decoder.finish()
                    return "PROBE_PEER_CLOSED", frames, not frames
                byte_budget -= len(data)
                for frame in decoder.feed(data):
                    validate_server_frame(frame)
                    frames.append(frame.metadata())
                    if len(frames) >= config.max_frames:
                        return "PROBE_FRAMES_OBSERVED", frames, False
                if byte_budget <= 0:
                    return "PROBE_BYTE_LIMIT", frames, False
            return "PROBE_CANCELLED", frames, False
    except FrameError as error:
        return str(error), frames, False
    except (socket.timeout, TimeoutError):
        return "PROBE_TIMEOUT", frames, True
    except OSError:
        return "PROBE_CONNECTION_FAILED", frames, True


def probe(config: ProbeConfig, stop: Event | None = None) -> dict:
    stop = stop if stop is not None else Event()
    events = []
    outcome = "PROBE_CANCELLED"
    all_frames = []
    for attempt in range(1, config.attempts + 1):
        if stop.is_set():
            break
        outcome, frames, retry = _observe(config, stop)
        all_frames.extend(frames)
        events.append({"attempt": attempt, "event": outcome, "frames": frames})
        if not retry or attempt == config.attempts:
            break
        delay = min(config.backoff_max, config.backoff_initial * 2 ** (attempt - 1))
        events.append({"attempt": attempt, "event": "PROBE_BACKOFF", "delay_seconds": delay})
        if stop.wait(delay):
            outcome = "PROBE_CANCELLED"
            break
    return {
        "scope": "loopback_passive_diagnostic",
        "outcome": outcome,
        "events": events,
        "frames_observed": len(all_frames),
        "bytes_sent": 0,
        "ready": False,
        "iccup_authenticated": False,
    }
