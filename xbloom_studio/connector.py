"""Byte-link connector contract beneath the library's guarded transports.

A connector moves raw bytes between this host and one physical machine. It
knows nothing about frames, sessions or commands: the library validates the
handshake, gates every control frame, waits for acknowledgments, serializes
writes, segments frames and tracks uncertainty above it, so a Bluetooth
backend, a simulator or another link all run behind the same guards.

Lifecycle, for each session the library owns:

1. ``connect(on_disconnect)`` opens the link. ``on_disconnect`` must be called
   at most once, on the event loop, when an open link is lost or closed.
2. ``start_notify(callback)`` delivers every incoming notification, in order,
   as ``bytes`` on the event loop. Notifications may split or join frames.
3. ``write(data)`` sends one chunk of at most ``write_size`` bytes as a write
   without response. The library never overlaps two writes on one link and
   never retries a write; an exception means delivery is unknown. A write
   should end promptly when cancelled: a write still pending at its write
   bound, or at a timed command's deadline such as a grinder pulse, is
   cancelled once, and any stop follows only after it returns. A write that
   ignores cancellation holds the link, and delays that stop, until it
   returns.
4. ``stop_notify()`` ends notifications; ``disconnect()`` closes the link and
   must be safe to call when already closed or after a failed connect.

``identity`` is the stable physical-machine identity used for exclusive local
ownership. It must be known before connecting and must not change between
sessions. Spellings that :func:`normalize_identity` maps to the same value
share one owner.

Failures should raise ``ConnectionError``, ``OSError`` or ``TimeoutError``;
the library reports them as session errors.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol, runtime_checkable

DEFAULT_WRITE_SIZE = 20
MAX_WRITE_SIZE = 512
INFORMATION_FIELDS = (
    "manufacturer",
    "model",
    "serial",
    "firmware",
    "hardware",
    "software",
)

_HARDWARE_ADDRESS = re.compile(r"[0-9A-F]{2}(?:[:-][0-9A-F]{2}){5}")


@runtime_checkable
class Connector(Protocol):
    """Raw byte link to one machine; see the module documentation."""

    @property
    def identity(self) -> str: ...

    @property
    def write_size(self) -> int: ...

    @property
    def is_connected(self) -> bool: ...

    async def connect(self, on_disconnect: Callable[[], None]) -> None: ...

    async def disconnect(self) -> None: ...

    async def start_notify(self, callback: Callable[[bytes], None]) -> None: ...

    async def stop_notify(self) -> None: ...

    async def write(self, data: bytes) -> None: ...

    async def read_information(self, name: str) -> str | None: ...


def normalize_identity(device: object) -> str:
    """Return the canonical owner key for an address, device object or identity.

    Objects with an ``address`` attribute use it. Hardware addresses compare
    case-insensitively with either separator; other identities (for example
    platform UUIDs) compare case-insensitively.
    """
    raw = str(getattr(device, "address", device)).strip().upper()
    if not raw or raw == "NONE":
        raise ValueError("A machine address or connector identity is required")
    if _HARDWARE_ADDRESS.fullmatch(raw):
        raw = raw.replace("-", ":")
    return raw


def check_write_size(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_WRITE_SIZE:
        raise ValueError(f"write_size must be an integer from 1 to {MAX_WRITE_SIZE}")
    return value


def check_information_field(name: str) -> str:
    if name not in INFORMATION_FIELDS:
        raise ValueError(f"Unknown standard information field: {name}")
    return name
