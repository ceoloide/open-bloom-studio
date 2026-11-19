"""Independent frame oracle for tests.

Builds frames from the documented field layout with ``struct`` and computes
the reflected CRC-16 through ``binascii.crc_hqx`` (the non-reflected
polynomial) by bit-reversing input and output, so expected bytes never come
from the library's own encoder or checksum.
"""

from __future__ import annotations

import binascii
import struct


def _reverse8(value: int) -> int:
    return int(f"{value:08b}"[::-1], 2)


def _reverse16(value: int) -> int:
    return int(f"{value:016b}"[::-1], 2)


def reflected_crc(data: bytes) -> int:
    return _reverse16(binascii.crc_hqx(bytes(_reverse8(b) for b in data), 0))


def frame(
    code: int,
    payload: bytes = b"",
    *,
    device: int = 1,
    function: int = 1,
    marker: int = 1,
) -> bytes:
    body = struct.pack(
        "<BBBHIB", 0x58, device, function, code, 12 + len(payload), marker
    )
    body += payload
    return body + struct.pack("<H", reflected_crc(body))


def reply(code: int, payload: bytes = b"", marker: int = 0xC1) -> bytes:
    """Incoming notification frame (device 2, function 7)."""
    return frame(code, payload, device=2, function=7, marker=marker)


def u32(value: int) -> bytes:
    return struct.pack("<I", value)


def f32(value: float) -> bytes:
    return struct.pack("<f", value)


ACK = reply(8100)
IDLE = reply(8023, u32(1))


def identity_block(
    *,
    serial: bytes = b"J15TESTUNIT00",
    model: bytes = b"TEST",
    firmware: bytes = b"V9.9Z.99",
    water: int = 1,
    source: int = 0,
    grind_raw: int = 42,
    brightness: int = 15,
    temperature_unit: int = 1,
    weight_unit: int = 1,
    size: int = 63,
) -> bytes:
    """Synthetic identity block laid out at the documented offsets."""
    info = bytearray(max(size, 42))
    info[0:13] = serial.ljust(13, b"\0")[:13]
    info[13:19] = model.ljust(6, b"\0")[:6]
    info[19:29] = firmware.ljust(10, b"\0")[:10]
    info[33] = water
    info[36] = source
    info[37] = grind_raw
    info[38] = brightness
    info[40] = temperature_unit
    info[41] = weight_unit
    return bytes(info[:size])
