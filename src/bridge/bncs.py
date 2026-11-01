"""Bounded BNCS framing. No authentication, hashing, Guard or Warden replies."""

from dataclasses import dataclass
import struct


class FrameError(ValueError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class Frame:
    packet_id: int
    payload: bytes

    def __repr__(self) -> str:
        return f"Frame(packet_id=0x{self.packet_id:02x}, length={len(self.payload) + 4})"

    def metadata(self) -> dict:
        return {"packet_id": f"0x{self.packet_id:02x}", "length": len(self.payload) + 4}


class FrameDecoder:
    def __init__(self, max_frame: int = 4096):
        if type(max_frame) is not int or not 4 <= max_frame <= 65535:
            raise ValueError("BNCS_FRAME_LIMIT_INVALID")
        self.max_frame = max_frame
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        # Caller chunks are bounded too. No attacker-controlled length allocation.
        if len(data) > self.max_frame:
            raise FrameError("BNCS_READ_LIMIT_EXCEEDED")
        self.buffer.extend(data)
        frames = []
        while self.buffer:
            if self.buffer[0] != 0xff:
                raise FrameError("BNCS_BAD_HEADER")
            if len(self.buffer) < 4:
                break
            length = struct.unpack_from("<H", self.buffer, 2)[0]
            if not 4 <= length <= self.max_frame:
                raise FrameError("BNCS_BAD_LENGTH")
            if len(self.buffer) < length:
                break
            frames.append(Frame(self.buffer[1], bytes(self.buffer[4:length])))
            del self.buffer[:length]
        return frames

    def finish(self) -> None:
        if self.buffer:
            raise FrameError("BNCS_TRUNCATED_FRAME")


def validate_server_frame(frame: Frame) -> None:
    """Only allow non-authentication diagnostic messages; never expose payload."""
    if frame.packet_id == 0x5e:
        raise FrameError("BNCS_WARDEN_UNSUPPORTED")
    if frame.packet_id in (0x51, 0x53, 0x54):
        raise FrameError("BNCS_AUTHENTICATION_UNSUPPORTED")
    if frame.packet_id == 0x00 and not frame.payload:
        return
    if frame.packet_id == 0x25 and len(frame.payload) == 4:
        return
    if frame.packet_id == 0x50:
        # S->C fixed part: logon type, server token, UDP value, FILETIME.
        # Then two NUL strings, optionally the 128-byte Warcraft signature.
        tail = frame.payload[20:]
        parts = tail.split(b"\0", 2)
        if (len(frame.payload) >= 22 and len(parts) == 3
                and len(parts[0]) <= 255 and len(parts[1]) <= 255
                and len(parts[2]) in (0, 128)):
            return
    elif frame.packet_id not in (0x00, 0x25):
        raise FrameError("BNCS_PACKET_UNSUPPORTED")
    raise FrameError("BNCS_PAYLOAD_INVALID")
