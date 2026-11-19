"""Frame envelope: encoding, checksum, decoding and stream reassembly.

A frame is ``0x58``, a device byte, a function byte, a little-endian 16-bit
command, a little-endian 32-bit total length, a marker byte, the payload and
a little-endian CRC-16 over everything before it. Outgoing frames use device
1 and marker 1; machine notifications use device 2.
"""

from __future__ import annotations

import json
import struct
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any

HEADER = 0x58
MINIMUM_FRAME = 12
OUTGOING_DEVICE = 1
OUTGOING_MARKER = 1
INCOMING_DEVICE = 2


@lru_cache(maxsize=1)
def _catalog_text() -> str:
    return files("xbloom_studio").joinpath("commands.json").read_text("utf-8")


def catalog() -> dict[str, Any]:
    """Return a fresh copy of the packaged, non-executable command catalog."""
    result: dict[str, Any] = json.loads(_catalog_text())
    return result


def crc16(data: bytes) -> int:
    """CRC-16 with the reflected 0x1021 polynomial, zero initial value."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc


@dataclass(frozen=True)
class Packet:
    device: int
    function: int
    command: int
    marker: int
    payload: bytes

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["payload"] = self.payload.hex()
        result["command_hex"] = f"0x{self.command:04x}"
        result["names"] = [
            entry["name"]
            for entry in catalog()["commands"]
            if entry["code"] == self.command
        ]
        return result


def encode(
    command: int,
    payload: bytes = b"",
    *,
    function: int = 1,
    device: int = OUTGOING_DEVICE,
    marker: int = OUTGOING_MARKER,
) -> bytes:
    """Build one complete frame; values outside the field widths are rejected."""
    for name, value, limit in (
        ("device", device, 0xFF),
        ("function", function, 0xFF),
        ("command", command, 0xFFFF),
        ("marker", marker, 0xFF),
    ):
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError(f"{name} must be an integer from 0 to {limit}")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    body = (
        bytes([HEADER, device, function])
        + struct.pack("<HI", command, MINIMUM_FRAME + len(payload))
        + bytes([marker])
        + payload
    )
    return body + struct.pack("<H", crc16(body))


def decode(data: bytes) -> Packet:
    if len(data) < MINIMUM_FRAME or data[0] != HEADER:
        raise ValueError("Expected an xBloom frame of at least 12 bytes")
    if int.from_bytes(data[5:9], "little") != len(data):
        raise ValueError("Frame length does not match its 32-bit length field")
    if crc16(data[:-2]) != int.from_bytes(data[-2:], "little"):
        raise ValueError("Frame checksum mismatch")
    return Packet(
        data[1],
        data[2],
        int.from_bytes(data[3:5], "little"),
        data[9],
        bytes(data[10:-2]),
    )


class FrameError(ValueError):
    """Corrupt stream input; ``packets`` are the frames validated before it."""

    def __init__(self, message: str, packets: list[Packet]) -> None:
        super().__init__(message)
        self.packets = packets


class FrameBuffer:
    """Reassemble split or joined frames; reject corrupt input explicitly.

    Deliberately does not resynchronize by guessing past malformed bytes.
    A transport must reset the buffer at disconnect and handle errors explicitly.
    Corrupt input raises :class:`FrameError`, which keeps the frames that were
    complete and valid before the corruption.
    """

    def __init__(self, max_frame_size: int = 65536) -> None:
        self.buffer = bytearray()
        self.max_frame_size = max_frame_size

    def feed(self, chunk: bytes) -> list[Packet]:
        self.buffer.extend(chunk)
        result = []
        try:
            while self.buffer:
                if self.buffer[0] != HEADER:
                    raise ValueError("Unexpected frame header")
                if len(self.buffer) < 9:
                    break
                size = int.from_bytes(self.buffer[5:9], "little")
                if not MINIMUM_FRAME <= size <= self.max_frame_size:
                    raise ValueError("Invalid or excessive frame length")
                if len(self.buffer) < size:
                    break
                result.append(decode(bytes(self.buffer[:size])))
                del self.buffer[:size]
        except ValueError as exc:
            self.buffer.clear()
            raise FrameError(str(exc), result) from exc
        return result
