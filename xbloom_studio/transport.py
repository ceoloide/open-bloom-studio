"""Guarded transports above a byte-link connector, and the optional Bleak link.

:class:`BleakTransport` is the read-only transport: its only write is the fixed
session handshake. :class:`ControlTransport` additionally accepts encoded
control frames, each checked against the control command gate. Both validate
and segment frames above the :class:`~xbloom_studio.connector.Connector`, so
any connector runs behind the same checks, hooks and byte transcript.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol

from .connector import (
    DEFAULT_WRITE_SIZE,
    Connector,
    check_information_field,
    check_write_size,
    normalize_identity,
)
from .protocol import Packet, decode

SERVICE = "0000e0ff-3c17-d293-8e48-14fe2e4da212"
COMMAND = "0000ffe1-0000-1000-8000-00805f9b34fb"
STATUS = "0000ffe2-0000-1000-8000-00805f9b34fb"
HANDSHAKE = bytes.fromhex("580101a41f1400000001b900000001000000bdd1")
STANDARD_INFO = {
    "manufacturer": "2a29",
    "model": "2a24",
    "serial": "2a25",
    "firmware": "2a26",
    "hardware": "2a27",
    "software": "2a28",
}
HANDSHAKE_COMMAND = 8100

# Function 2 parameter, mode and preset commands; all others use function 1.
FUNCTION_2_COMMANDS = frozenset({11506, 11507, 11508, 11509, 11510, 11511, 11512})
CONTROL_COMMANDS = FUNCTION_2_COMMANDS | frozenset(
    {
        3500, 3502, 3505, 4506, 4507, 4508, 4510, 4512, 4513,
        8001, 8002, 8003, 8004, 8005, 8006, 8007, 8010, 8012, 8013, 8014,
        8016, 8017, 8018, 8019, 8020, 8021, 8022, 8102, 8103, 8104, 8500,
        40518, 40519, 40524, 40525, 40526,
    }
)  # fmt: skip
# Stop commands may be sent after a hook failure: a failing journal must not
# prevent a bounded attempt to stop the machine.
STOP_COMMANDS = frozenset({3505, 4507, 40519})
STOP_HOOK_TIMEOUT = 0.05

EventSink = Callable[[str, dict[str, Any]], None]


def validate_handshake(frame: bytes) -> Packet:
    """Return the decoded handshake; any other frame is rejected."""
    packet = decode(frame)
    if frame != HANDSHAKE or (
        packet.device,
        packet.function,
        packet.command,
        packet.marker,
    ) != (1, 1, HANDSHAKE_COMMAND, 1):
        raise ValueError("Only the fixed session handshake may open a session")
    return packet


def validate_control_frame(frame: bytes) -> Packet:
    """Return the decoded control frame, or reject it before any write."""
    packet = decode(frame)
    if (packet.device, packet.marker) != (1, 1) or (
        packet.command not in CONTROL_COMMANDS
    ):
        raise ValueError("Frame outside the control API allowlist")
    if packet.function != (2 if packet.command in FUNCTION_2_COMMANDS else 1):
        raise ValueError("Unexpected control function")
    return packet


class Transport(Protocol):
    """Session-level transport used by the read-only client."""

    async def connect(self, on_disconnect: Callable[[], None]) -> None: ...

    async def disconnect(self) -> None: ...

    async def subscribe(self, callback: Callable[[bytes], None]) -> None: ...

    async def unsubscribe(self) -> None: ...

    async def open_session(self) -> None: ...

    async def read_information(self, name: str) -> str | None: ...


class FrameTransport(Transport, Protocol):
    """Transport that also accepts encoded control frames."""

    async def _write_frame(self, frame: bytes) -> None: ...


class CommandHooks(Protocol):
    """Observer around each outgoing frame, for example a durable journal.

    ``before_write`` runs after the frame passed its gate and before the
    command guard and the first byte; raising prevents the write, except for
    stop commands, which wait at most ``STOP_HOOK_TIMEOUT`` and then proceed.
    Exactly one of ``write_completed`` and ``write_failed`` follows a write
    attempt. Neither may block.
    """

    async def before_write(self, packet: Packet, frame: bytes) -> None: ...

    def write_completed(self, packet: Packet) -> None: ...

    def write_failed(self, packet: Packet, error: BaseException) -> None: ...


def _bleak() -> Any:
    try:
        return importlib.import_module("bleak")
    except ImportError as exc:
        raise RuntimeError(
            "Install open-bloom-studio[ble] for Bluetooth support"
        ) from exc


async def discover(timeout: float = 10.0) -> list[dict[str, Any]]:
    """Discover Studio advertisements without connecting or pairing."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    found = await _bleak().BleakScanner.discover(timeout=timeout, return_adv=True)
    return [
        {
            "address": device.address,
            "name": adv.local_name or device.name,
            "rssi": adv.rssi,
            "service_uuids": adv.service_uuids,
        }
        for device, adv in found.values()
        if (adv.local_name or device.name or "").upper().startswith("XBLOOM")
        or SERVICE in [uuid.lower() for uuid in adv.service_uuids]
    ]


class BleakConnector:
    """Local Bluetooth Low Energy link using the optional ``bleak`` package.

    Accepts an address or a platform device object from an existing scanner.
    Pairing, MTU overrides and adapter resets are deliberately absent. Writes
    are writes without response of at most 20 bytes.
    """

    write_size = DEFAULT_WRITE_SIZE

    def __init__(self, device: object, timeout: float = 15.0) -> None:
        self.device = device
        self.timeout = timeout
        self._identity = normalize_identity(device)
        self._client: Any = None

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def is_connected(self) -> bool:
        return self._client is not None and bool(self._client.is_connected)

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        bleak = _bleak()
        device = self.device
        if isinstance(device, str):
            device = await bleak.BleakScanner.find_device_by_address(
                device, timeout=self.timeout
            )
            if device is None:
                raise ConnectionError(
                    "xBloom not advertising; release other Bluetooth clients"
                )
        self._client = bleak.BleakClient(
            device,
            timeout=self.timeout,
            disconnected_callback=lambda _: on_disconnect(),
        )
        await self._client.connect()
        for uuid in (COMMAND, STATUS):
            if self._client.services.get_characteristic(uuid) is None:
                raise ConnectionError(f"Required Studio characteristic missing: {uuid}")

    def _require_client(self) -> Any:
        if self._client is None:
            raise ConnectionError("Not connected")
        return self._client

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        await self._require_client().start_notify(
            STATUS, lambda _, data: callback(bytes(data))
        )

    async def stop_notify(self) -> None:
        if self.is_connected:
            await self._client.stop_notify(STATUS)

    async def write(self, data: bytes) -> None:
        if not 1 <= len(data) <= self.write_size:
            raise ValueError("Write exceeds the connector write size")
        # The machine rejects writes with response on this characteristic.
        await self._require_client().write_gatt_char(COMMAND, data, response=False)

    async def read_information(self, name: str) -> str | None:
        uuid = f"0000{STANDARD_INFO[check_information_field(name)]}"
        uuid += "-0000-1000-8000-00805f9b34fb"
        client = self._require_client()
        if client.services.get_characteristic(uuid) is None:
            return None
        value = bytes(await client.read_gatt_char(uuid))
        return value.decode("utf-8", errors="replace").strip("\x00") or None

    async def disconnect(self) -> None:
        try:
            if self._client is not None:
                await self._client.disconnect()
        finally:
            self._client = None


class BleakTransport:
    """Read-only session transport; its only write is the fixed handshake.

    Pass an address or platform device to use the local Bleak connector, or
    ``connector=`` for any other link. A session may make the machine announce
    a pairing even though no operating-system bonding takes place.
    """

    command_guard_after_journal = True

    def __init__(
        self,
        device: object = None,
        timeout: float = 15.0,
        *,
        connector: Connector | None = None,
        hooks: CommandHooks | None = None,
    ) -> None:
        if connector is None:
            if device is None:
                raise ValueError("Provide a device address or a connector")
            connector = BleakConnector(device, timeout)
        elif not isinstance(connector, Connector):
            raise TypeError("connector must implement the Connector protocol")
        self._write_size = check_write_size(connector.write_size)
        self.connector = connector
        self.device = device if device is not None else connector.identity
        self.timeout = timeout
        self.hooks = hooks
        self.event_sink: EventSink | None = None
        # Runs after hooks.before_write and immediately before the first byte.
        self.command_guard: Callable[[], None] | None = None
        self._subscribed = False

    @property
    def identity(self) -> str:
        return self.connector.identity

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        self._subscribed = False
        await self.connector.connect(on_disconnect)

    async def subscribe(self, callback: Callable[[bytes], None]) -> None:
        if not self._subscribed:
            await self.connector.start_notify(callback)
            self._subscribed = True

    async def unsubscribe(self) -> None:
        if self._subscribed and self.connector.is_connected:
            await self.connector.stop_notify()
        self._subscribed = False

    async def open_session(self) -> None:
        if not self._subscribed:
            raise RuntimeError("Subscribe before opening session")
        await self._send(HANDSHAKE, validate_handshake(HANDSHAKE))

    async def read_information(self, name: str) -> str | None:
        return await self.connector.read_information(check_information_field(name))

    async def disconnect(self) -> None:
        try:
            await self.connector.disconnect()
        finally:
            self._subscribed = False

    async def _send(self, frame: bytes, packet: Packet) -> None:
        hooks = self.hooks
        if hooks is not None:
            if packet.command in STOP_COMMANDS:
                with suppress(Exception):
                    await asyncio.wait_for(
                        hooks.before_write(packet, frame), STOP_HOOK_TIMEOUT
                    )
            else:
                await hooks.before_write(packet, frame)
        try:
            if self.command_guard is not None:
                self.command_guard()
            if not self._subscribed or not self.connector.is_connected:
                raise ConnectionError("No subscribed control connection")
            for offset in range(0, len(frame), self._write_size):
                await self._write_bytes(frame[offset : offset + self._write_size])
        except BaseException as exc:
            if hooks is not None:
                hooks.write_failed(packet, exc)
            raise
        if hooks is not None:
            hooks.write_completed(packet)

    async def _write_bytes(self, data: bytes) -> None:
        if self.event_sink is not None:
            self.event_sink("ble.outgoing_chunk", {"hex": bytes(data).hex()})
        await self.connector.write(data)


class ControlTransport(BleakTransport):
    """Opt-in transport for the interaction API; read-only transport stays unchanged.

    Only encoded control frames that pass the command gate are written; ACKs
    are handled by the interaction, not this transport.
    """

    async def _write_frame(self, frame: bytes) -> None:
        await self._send(frame, validate_control_frame(frame))
