import struct
import unittest

from src.bridge.bncs import Frame, FrameDecoder, FrameError, validate_server_frame


def packet(packet_id, payload=b""):
    return struct.pack("<BBH", 255, packet_id, len(payload) + 4) + payload


class FrameTests(unittest.TestCase):
    def test_each_possible_fragment_boundary_and_multiple_frames(self):
        message = packet(0) + packet(0x25, b"test")
        expected = [Frame(0, b""), Frame(0x25, b"test")]
        for split in range(len(message) + 1):
            decoder = FrameDecoder()
            self.assertEqual(decoder.feed(message[:split]) + decoder.feed(message[split:]), expected)
            decoder.finish()
        decoder = FrameDecoder()
        frames = []
        for byte in message:
            frames.extend(decoder.feed(bytes([byte])))
        self.assertEqual(frames, expected)

    def test_bad_header_lengths_and_frame_budget(self):
        for data in (b"\xfe", b"\xff\x00\x00\x00", b"\xff\x00\x03\x00", b"\xff\x00\xff\xff"):
            with self.assertRaises(FrameError):
                FrameDecoder(4096).feed(data)
        with self.assertRaises(FrameError):
            FrameDecoder(8).feed(b"x" * 9)

    def test_truncated_header_and_body(self):
        for length in range(1, 8):
            decoder = FrameDecoder()
            decoder.feed(packet(0x25, b"test")[:length])
            with self.assertRaisesRegex(FrameError, "TRUNCATED"):
                decoder.finish()

    def test_non_authentication_server_frames_only(self):
        for frame in (Frame(0, b""), Frame(0x25, b"test"),
                      Frame(0x50, bytes(20) + b"ver-IX86-0.mpq\0formula\0"),
                      Frame(0x50, bytes(20) + b"\0\0" + bytes(128))):
            validate_server_frame(frame)
        for frame in (Frame(0, b"x"), Frame(0x25, b""), Frame(0x50, bytes(20)),
                      Frame(0x50, bytes(20) + b"a\0b\0x"), Frame(0x50, bytes(20) + b"a" * 256 + b"\0b\0")):
            with self.assertRaisesRegex(FrameError, "PAYLOAD_INVALID"):
                validate_server_frame(frame)

    def test_authentication_guard_and_unknown_frames_fail_closed(self):
        for sid in (0x51, 0x53, 0x54, 0x5e, 0x99):
            with self.assertRaisesRegex(FrameError, "UNSUPPORTED"):
                validate_server_frame(Frame(sid, b"MUST-NOT-LOG"))

    def test_frame_metadata_and_repr_never_disclose_payload(self):
        frame = Frame(0x50, b"MUST-NOT-LOG")
        self.assertNotIn("MUST-NOT-LOG", repr(frame))
        self.assertNotIn("MUST-NOT-LOG", str(frame.metadata()))


if __name__ == "__main__":
    unittest.main()
