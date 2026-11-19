import random
import struct
import unittest

from tests.frames import frame, reflected_crc, reply
from xbloom_studio.protocol import FrameBuffer, Packet, catalog, crc16, decode, encode

# Session handshake, field by field: header, device 1, function 1, command
# 8100 (0x1fa4), total length 20, marker 1, payload, checksum.
HANDSHAKE = bytes.fromhex("580101a41f1400000001b900000001000000bdd1")
# Session acknowledgment: device 2, function 7, command 8100, length 12, marker c1.
ACK = bytes.fromhex("580207a41f0c000000c190b8")


class ChecksumTests(unittest.TestCase):
    def test_standard_check_value(self):
        # Reflected CRC-16, polynomial 0x1021, initial value zero.
        self.assertEqual(crc16(b"123456789"), 0x2189)

    def test_matches_independent_oracle(self):
        rng = random.Random(7)
        for size in range(0, 80):
            data = bytes(rng.randrange(256) for _ in range(size))
            self.assertEqual(crc16(data), reflected_crc(data))


class EncodeDecodeTests(unittest.TestCase):
    def test_literal_frames_decode(self):
        packet = decode(HANDSHAKE)
        self.assertEqual(
            packet, Packet(1, 1, 8100, 1, bytes.fromhex("b900000001000000"))
        )
        self.assertEqual(decode(ACK), Packet(2, 7, 8100, 0xC1, b""))

    def test_encode_matches_literal_and_oracle(self):
        self.assertEqual(encode(8100, bytes.fromhex("b900000001000000")), HANDSHAKE)
        self.assertEqual(encode(8100, function=7, device=2, marker=0xC1), ACK)
        self.assertEqual(
            encode(4510, struct.pack("<I", 935)), frame(4510, struct.pack("<I", 935))
        )
        self.assertEqual(
            encode(11511, b"\0" * 4, function=2), frame(11511, b"\0" * 4, function=2)
        )

    def test_encode_rejects_out_of_range_fields(self):
        for kwargs in (
            {"command": -1},
            {"command": 0x10000},
            {"command": 1, "function": 256},
            {"command": 1, "device": True},
            {"command": 1, "marker": 1.0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                encode(**kwargs)
        with self.assertRaises(TypeError):
            encode(1, "text")

    def test_corrupt_checksum_truncation_and_extra_bytes(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            decode(ACK[:-1] + bytes([ACK[-1] ^ 1]))
        for data in (b"", HANDSHAKE[:-1], HANDSHAKE + ACK, b"\x57" + ACK[1:]):
            with self.assertRaises(ValueError):
                decode(data)

    def test_to_dict_names_from_catalog(self):
        result = decode(reply(40511)).to_dict()
        self.assertEqual(result["command_hex"], "0x9e3f")
        self.assertEqual(result["payload"], "")
        self.assertEqual(result["names"], ["brewer_stopped"])
        self.assertEqual(
            decode(HANDSHAKE).to_dict()["names"],
            ["open_session", "session_acknowledged"],
        )
        self.assertEqual(decode(reply(9999)).to_dict()["names"], [])

    def test_catalog_is_a_fresh_copy(self):
        first = catalog()
        first["commands"].clear()
        self.assertTrue(catalog()["commands"])


class FrameBufferTests(unittest.TestCase):
    def test_all_split_boundaries_and_joined_frames(self):
        weight = reply(20501, struct.pack("<f", 99.5))
        stream = HANDSHAKE + ACK + weight
        for split in range(len(stream) + 1):
            buffer = FrameBuffer()
            result = buffer.feed(stream[:split]) + buffer.feed(stream[split:])
            self.assertEqual([p.command for p in result], [8100, 8100, 20501])
            self.assertFalse(buffer.buffer)

    def test_excessive_size_resets_buffer(self):
        buffer = FrameBuffer()
        with self.assertRaisesRegex(ValueError, "excessive"):
            buffer.feed(bytes.fromhex("580207a41fffffffff"))
        self.assertFalse(buffer.buffer)
        self.assertEqual(buffer.feed(ACK)[0].command, 8100)

    def test_bad_header_and_bad_checksum_reset_without_resync(self):
        buffer = FrameBuffer()
        with self.assertRaisesRegex(ValueError, "header"):
            buffer.feed(b"\x00" + ACK)
        self.assertFalse(buffer.buffer)
        with self.assertRaisesRegex(ValueError, "checksum"):
            buffer.feed(ACK[:-1] + b"\x00" + ACK)
        self.assertFalse(buffer.buffer)

    def test_bounded_frame_size(self):
        buffer = FrameBuffer(max_frame_size=12)
        self.assertEqual(len(buffer.feed(ACK)), 1)
        with self.assertRaises(ValueError):
            buffer.feed(HANDSHAKE)


if __name__ == "__main__":
    unittest.main()
