"""Opt-in, connection-owning controls built from tested command sequences.

No recipe store, parser or guessed next-step command. EncodedRecipe accepts
machine recipe content produced by an encoder, never a complete frame. Every
frame passes the control command gate before any transport sees it.
"""

from __future__ import annotations

import asyncio
import struct
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

from .client import ProtocolCommandError, ReadOnlyClient, SessionError
from .connector import Connector
from .events import decode_event, protocol_error
from .machine_controls import (
    MachineControls,
    _number,
    cup_limits_payload,
    validate_cup_limits,
)
from .protocol import Packet, encode
from .transport import (
    STOP_COMMANDS,
    STOP_HOOK_TIMEOUT,
    CommandHooks,
    ControlTransport,
    FrameTransport,
    Transport,
    validate_control_frame,
)

IDLE_STATES = (1, 0x0A, 0x41, 0x24)
NO_WATER_STATE = 0x0C
MOTION_STATES = (0x22, 0x23)
SCALE_STATES = (4, 5)
CONTEXT_STATES = (1, 0x0A, 0x41, 0x24, 4, 5)
FAULT_EVENTS = (40517, 40522, 8203, 8204)
# Keys of Interaction._seen: (command, None), and (8023, state) for states.
SeenKey = tuple[int, "int | None"]
FAULT_KEYS: tuple[SeenKey, ...] = tuple((code, None) for code in FAULT_EVENTS)
MOTION_KEYS: tuple[SeenKey, ...] = (
    (8023, MOTION_STATES[0]),
    (8023, MOTION_STATES[1]),
    (9003, None),
    (9005, None),
)
WATER_KINDS = ("water", "recipe", "tea")
MEASUREMENTS = (20501, 40523, 40521)
STOP_CODES = {
    "water": 4507,
    "grinder": 3505,
    "calibration": 3505,
    "recipe": 40519,
    "tea": 40519,
}
EXIT_CODES = {"water": 8013, "grinder": 8012, "calibration": 8012}
PAUSE_RESUME_CODES = {
    "water": (8019, 8021),
    "grinder": (8018, 8020),
    "recipe": (40518, 40524),
    "tea": (40518, 40524),
}
PATTERNS = {"center": 0, "ring": 1, "spiral": 2}
CALIBRATED_POSITION = struct.pack("<I", 85)


class BusyError(RuntimeError):
    pass


class UnsupportedStageError(NotImplementedError):
    pass


class _Omitted:
    pass


_OMITTED = _Omitted()


@dataclass(frozen=True, init=False)
class EncodedRecipe:
    """Opaque machine content plus the settings sent separately.

    No built-in recipes. Cup limits are explicit optional (min, max) values;
    omitting them preserves the machine configuration.
    """

    payload: bytes
    dose_g: float
    grinder_enabled: bool = False
    cup_limits: tuple[float, float] | None = None
    bypass_ml: float = 0
    bypass_temperature_c: float = 20

    def __init__(
        self,
        payload: bytes,
        dose_g: float,
        grinder_enabled: bool | _Omitted = _OMITTED,
        cup_limits: tuple[float, float] | None = None,
        bypass_ml: float = 0,
        bypass_temperature_c: float = 20,
        *,
        set_grinder_size: bool | _Omitted = _OMITTED,
    ) -> None:
        """``set_grinder_size`` selects the grind-size upload; the alias is kept.

        Neither option establishes whether opaque content runs a grinder stage.
        """
        for name, value in (
            ("grinder_enabled", grinder_enabled),
            ("set_grinder_size", set_grinder_size),
        ):
            if value is not _OMITTED and type(value) is not bool:
                raise ValueError(name + " must be boolean")
        if (
            isinstance(grinder_enabled, bool)
            and isinstance(set_grinder_size, bool)
            and grinder_enabled != set_grinder_size
        ):
            raise ValueError(
                "Conflicting grinder_enabled and set_grinder_size upload options"
            )
        selected = (
            set_grinder_size
            if isinstance(set_grinder_size, bool)
            else grinder_enabled
            if isinstance(grinder_enabled, bool)
            else False
        )
        for name, item in (
            ("payload", payload),
            ("dose_g", dose_g),
            ("grinder_enabled", selected),
            ("cup_limits", cup_limits),
            ("bypass_ml", bypass_ml),
            ("bypass_temperature_c", bypass_temperature_c),
        ):
            object.__setattr__(self, name, item)
        self._validate()

    @property
    def set_grinder_size(self) -> bool:
        return self.grinder_enabled

    def upload_options(self) -> dict[str, Any]:
        return {
            "set_grinder_size": self.set_grinder_size,
            "upload_command": 8001 if self.set_grinder_size else 8004,
            "actual_grinding": "not_established_by_upload_option",
        }

    def _validate(self) -> None:
        if not isinstance(self.payload, bytes) or not 1 <= len(self.payload) <= 512:
            raise ValueError(
                "Recipe content must be 1–512 bytes, without a BLE envelope"
            )
        _number("dose_g", self.dose_g, 1, 100)
        if self.dose_g != int(self.dose_g):
            raise ValueError("Validated dose command requires whole grams")
        validate_cup_limits(self.cup_limits)
        _number("bypass_ml", self.bypass_ml, 0, 1000)
        _number("bypass_temperature_c", self.bypass_temperature_c, 20, 99)

    def settings_payload(self) -> bytes:
        # Bypass volume is an unscaled float32 and its temperature is in tenths
        # of a degree. Disabled bypass sends two zero bit patterns, not 20 C.
        temperature = self.bypass_temperature_c * 10 if self.bypass_ml else 0
        return struct.pack("<ffI", self.bypass_ml, temperature, int(self.dose_g))


def _deque_256() -> deque[Any]:
    return deque(maxlen=256)


@dataclass
class Operation:
    """One operation. Waiting does not own a command lock or release Bluetooth."""

    kind: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "configuring"
    status: str = "running"
    start_observed: bool = False
    stop_acknowledged: bool | None = None
    stop_observed: bool = False
    cleanup_status: str = "pending"
    error: str | None = None
    protocol_error: dict[str, Any] | None = None
    pending_user_action: str | None = None
    last_machine_event: dict[str, Any] | None = None
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _events: deque[tuple[int, bytes]] = field(default_factory=_deque_256, repr=False)
    _armed: bool = field(default=False, repr=False)
    _stop_requested: bool = field(default=False, repr=False)
    _paused: bool = field(default=False, repr=False)
    _final: dict[str, Any] | None = field(default=None, repr=False)
    # Event revision at launch: faults or motion after it block the start.
    _since: int = field(default=0, repr=False)
    # Water source encoded in the start command, when it carries one.
    _source: int | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in (
                "id",
                "kind",
                "phase",
                "status",
                "start_observed",
                "stop_acknowledged",
                "stop_observed",
                "cleanup_status",
                "error",
                "protocol_error",
                "pending_user_action",
                "last_machine_event",
            )
        }

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    async def wait(self) -> dict[str, Any]:
        if self._task is None:
            raise RuntimeError("Operation has not started")
        # Cancelling a consumer does not silently cancel the machine operation,
        # and a worker cancelled during cleanup still leaves its final result.
        await asyncio.wait({self._task})
        if not self._task.cancelled():
            self._task.result()
        return {**self.snapshot(), "state": self._final}


Waiter = tuple[tuple[int, int, "bytes | None"], "asyncio.Future[Packet]"]


class _Progress:
    """How far one command got: rejected by its guard, or transmitted."""

    rejected = False
    transmitted_at: float | None = None
    expiry: asyncio.TimerHandle | None = None


def _confirmed(future: asyncio.Future[Packet]) -> bool:
    return future.done() and not future.cancelled() and future.exception() is None


class Interaction(MachineControls, ReadOnlyClient):
    """Hold the link until context exit, including idle and user-input waits.

    Controls are opt-in independently of connecting. Pass an address, a
    platform device or ``connector=``. Reconnection is never automatic.
    State methods and stop remain responsive while operations run.
    """

    transport: FrameTransport

    def __init__(
        self,
        device: object = None,
        *,
        allow_control: bool = False,
        allow_untested: bool = False,
        transport: FrameTransport | None = None,
        connector: Connector | None = None,
        command_hooks: CommandHooks | None = None,
        ack_timeout: float = 5.0,
        write_timeout: float = 5.0,
        transition_delay: float = 3.0,
        start_timeout: float = 5.0,
        **kwargs: Any,
    ) -> None:
        if type(allow_control) is not bool or type(allow_untested) is not bool:
            raise ValueError("allow_control and allow_untested must be booleans")
        _number("ack_timeout", ack_timeout, 0.001, 60)
        _number("write_timeout", write_timeout, 0.001, 60)
        _number("transition_delay", transition_delay, 0, 30)
        _number("start_timeout", start_timeout, 0.001, 60)
        super().__init__(
            device,
            transport=transport,
            connector=connector,
            command_hooks=command_hooks,
            **kwargs,
        )
        self.allow_control = allow_control
        self.allow_untested = allow_untested
        self.ack_timeout = ack_timeout
        self.write_timeout = write_timeout
        self.transition_delay = transition_delay
        self.start_timeout = start_timeout
        self.operation: Operation | None = None
        self._commands = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._waiter: Waiter | None = None
        self._closing = False
        self._entered = False
        self._scale_mode = False
        self._uncertain = False
        self._event_revision = 0
        self._received_events: deque[tuple[int, Packet]] = deque(maxlen=256)
        # Latest revision per event (and per reported state), kept apart from
        # the bounded history so benign traffic can never evict a fault.
        self._seen: dict[SeenKey, int] = {}
        self._preset_write_used = False
        self._last_preset_write: dict[str, Any] | None = None
        self._selected_water_source: int | None = None
        self._prepared: dict[str, Any] | None = None
        self._last_preparation: dict[str, Any] | None = None

    @staticmethod
    def _default_transport(
        device: object, connector: Connector | None, hooks: CommandHooks | None
    ) -> Transport:
        return ControlTransport(device, connector=connector, hooks=hooks)

    async def __aenter__(self) -> Interaction:
        if self._entered or self._closing:
            raise BusyError("Interaction context is already in use")
        if not self.allow_control:
            raise SessionError("Controls require allow_control=True")
        self._entered = True
        try:
            await super().__aenter__()
        except BaseException:
            self._entered = False
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.disconnect()

    async def unsubscribe(self) -> None:
        # Check and unsubscribe under the lifecycle lock, so no operation or
        # preparation is admitted between the check and the call.
        async with self._lifecycle:
            if self._prepared is not None:
                raise BusyError("Cancel the prepared recipe before disabling telemetry")
            if self.operation is not None and not self.operation.done:
                raise BusyError("Stop the active operation before disabling telemetry")
            await super().unsubscribe()

    async def disconnect(self) -> None:
        self._closing = True
        self._invalidate_preparation("disconnect")

        async def finish() -> None:
            try:
                await self.stop()
                # Wait for an in-flight scale/mode command before disconnecting.
                async with self._lifecycle:
                    if self._scale_mode and self.state.session_ready:
                        await self._command(8014)
                        self._scale_mode = False
            finally:
                await ReadOnlyClient.disconnect(self)
                self._entered = False

        task = asyncio.create_task(finish())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _disconnected(self) -> None:
        self._invalidate_preparation("connection_lost")
        super()._disconnected()
        if self._waiter is not None:
            _, future = self._waiter
            if not future.done():
                future.set_exception(
                    SessionError("Connection lost; machine state is unknown")
                )

    def _packet_received(self, packet: Packet) -> None:
        error = protocol_error(packet)
        if error is not None:
            self._protocol_error(error)
            return
        if (packet.device, packet.function) != (2, 7):
            return
        if self._waiter is not None:
            (code, marker, expected_payload), future = self._waiter
            if (
                (packet.command, packet.marker) == (code, marker)
                and (expected_payload is None or packet.payload == expected_payload)
                and not future.done()
            ):
                future.set_result(packet)
        if packet.marker != 0xC1:
            return
        if self._prepared is not None:
            self._check_preparation(packet)
        self._event_revision += 1
        self._seen[(packet.command, None)] = self._event_revision
        if packet.command == 8023 and len(packet.payload) >= 4:
            state = int.from_bytes(packet.payload[:4], "little")
            self._seen[(8023, state)] = self._event_revision
        if packet.command not in MEASUREMENTS:
            self._received_events.append((self._event_revision, packet))
        op = self.operation
        if op is None or op._task is None or op._task.done() or not op._armed:
            return
        if packet.command in MEASUREMENTS:
            return  # measurements live in the state cache, not an event queue
        self._observe(op, packet)
        self._signal()

    def _protocol_error(self, error: dict[str, Any]) -> None:
        self._invalidate_preparation("protocol_error")
        if self.state.session_ready:
            self._uncertain = True
        pending = self._waiter[0][0] if self._waiter is not None else None
        exc = ProtocolCommandError(error, pending)
        if self._waiter is not None and not self._waiter[1].done():
            self._waiter[1].set_exception(exc)
        op = self.operation
        if op is not None and not op.done and op.protocol_error is None:
            op.protocol_error = exc.details
        self._signal()

    def _check_preparation(self, packet: Packet) -> None:
        assert self._prepared is not None
        code, payload = packet.command, packet.payload
        motion = code in (9003, 9005, 40502)
        # Sufficient water is a prerequisite for execution, not for storing an
        # unexecuted recipe. Keep its reservation so it can be started after
        # refill; start_prepared checks water again before sending.
        known_low_water = (
            code == 40522
            and len(payload) >= 4
            and int.from_bytes(payload[:4], "little") == 0
        )
        low_water_fault = code == 40522 and not (
            known_low_water and self._prepared["status"] in ("uploading", "prepared")
        )
        fault = code in (40517, 8203, 8204) or low_water_fault
        context = code in (8111, 9000, 9001, 9002, 9004, 9006, 9008)
        if code == 8023 and len(payload) >= 4:
            state = int.from_bytes(payload[:4], "little")
            motion = state in MOTION_STATES
            context = state in CONTEXT_STATES
        if motion or fault or context:
            self._invalidate_preparation(
                "machine_fault"
                if fault
                else "machine_motion"
                if motion
                else "machine_context_changed"
            )
            if motion or fault:
                self._uncertain = True

    def _observe(self, op: Operation, packet: Packet) -> None:
        code, payload = packet.command, packet.payload
        event = {
            "revision": self._event_revision,
            "at": datetime.now(timezone.utc).isoformat(),
            "command": code,
            "payload": payload.hex(),
        }
        op._events.append((code, payload))
        op.last_machine_event = event
        try:
            decoded = decode_event(packet)
        except ValueError:
            return  # Recorded by telemetry as a decode error, never lifecycle data.
        if decoded is not None:
            op.last_machine_event = {**event, **decoded}
            if (code == 9003 and op.kind in ("grinder", "recipe")) or (
                code == 9005 and op.kind in ("water", "recipe", "tea")
            ):
                op.start_observed = True
                op.phase = "grinding" if code == 9003 else "pouring"
                op._paused = False
            if (op.kind, code) in (("grinder", 9009), ("water", 9010)):
                op._paused = True
                op.phase = "paused"
            if op.kind in ("recipe", "tea") and code in (40520, 40527):
                op.phase = "bypass" if code == 40520 else "before_vibration"
        if code == 8023 and len(payload) >= 4:
            self._observe_state(op, int.from_bytes(payload[:4], "little"))
        # A pod identifier is not evidence of motion.
        if code == 40502 and op.kind in ("water", "recipe", "tea"):
            op.start_observed = True
        if (
            op.kind == "calibration"
            and code == 40526
            and payload == CALIBRATED_POSITION
        ):
            op.start_observed = True
        if op.kind == "tea" and code in (9012, 9011, 40515):
            op.start_observed = True
            op.phase = {
                9012: "soaking",
                9011: "tea_resume_observed",
                40515: "tea_pause_observed",
            }[code]
            if code in (9011, 40515):
                op._paused = code == 40515

    @staticmethod
    def _observe_state(op: Operation, state: int) -> None:
        if state in MOTION_STATES:
            if op.kind in ("recipe", "tea") or (op.kind, state) in (
                ("grinder", 0x22),
                ("calibration", 0x22),
                ("water", 0x23),
            ):
                op.start_observed = True
            op.phase = "grinding" if state == 0x22 else "pouring"
            op._paused = False
        elif (op.kind, state) in (("grinder", 2), ("water", 3)):
            op._paused = True
            op.phase = "paused"
        elif op.kind in ("recipe", "tea") and state not in (1, 0x24):
            op.phase = "unknown_machine_stage"

    def get_state(self) -> dict[str, Any]:
        """Immediate cached observations; this does not send a status command."""
        return {
            "machine": self.snapshot(),
            "operation": self.operation.snapshot() if self.operation else None,
            "event_revision": self._event_revision,
            "recovery_required": self._uncertain,
            "last_preset_write": self._last_preset_write,
            "last_acknowledged_water_source": self._selected_water_source,
            "prepared": dict(self._prepared) if self._prepared else None,
            "last_preparation": dict(self._last_preparation)
            if self._last_preparation
            else None,
            "query_source": "notifications_cache",
        }

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "read_only": False,
            "requires": ["allow_control"],
            "operations": [
                "start_water",
                "dispense_water",
                "start_grinder",
                "grind",
                "start_recipe",
                "upload_recipe",
                "upload_tea",
                "start_prepared",
                "cancel_prepared",
                "pause",
                "resume",
                "stop",
                "enter_scale",
                "tare",
                "exit_scale",
                "get_state",
                "wait_for_state",
            ]
            + list(MachineControls.METHODS),
            "recipe_input": "encoded_machine_content_with_explicit_settings",
            "remote_stage_advance": False,
            "active_state_query": False,
            "automatic_reconnect": False,
            "automatic_start_retry": False,
            "untested_requires": "allow_untested=True",
            "untested_operations": list(MachineControls.UNTESTED_METHODS),
            "untested_recipe_options": ["grinder_enabled", "bypass_ml>0", "cup_limits"],
            "active_setting_queries": ["read_pour_radius", "read_vibration"],
            "firmware_update": False,
            "address_lock_scope": "same OS user and host",
            "physical_recipe_controls": (
                "observed via notifications; unmapped stages stay unknown"
            ),
        }

    async def wait_for_state(
        self, after_revision: int, *, timeout: float = 5.0
    ) -> dict[str, Any]:
        _number("timeout", timeout, 0.001, 3600)
        await self.wait_for_update(after_revision, timeout)
        return self.get_state()

    def _require_session(self) -> None:
        if (
            not self.allow_control
            or not self._entered
            or self._closing
            or not self.state.session_ready
        ):
            raise SessionError("Enter an open control interaction first")
        if self._uncertain:
            raise SessionError(
                "Previous operation uncertain; inspect machine and open a new "
                "interaction"
            )

    def _require_idle(self, *, allow_no_water: bool = False) -> None:
        self._require_quiescent()
        observation = self.state.observations.get("machine_state_code")
        if (
            self._scale_mode
            and observation is not None
            and observation.value in SCALE_STATES
        ):
            return  # Owned standalone scale can be exited by _prepare.
        states = IDLE_STATES + ((NO_WATER_STATE,) if allow_no_water else ())
        if observation is None or observation.value not in states:
            raise BusyError(
                "Machine has not reported a recognized idle/completed state"
            )

    def _require_quiescent(self, *, allow_prepared: bool = False) -> None:
        self._require_session()
        if self._prepared is not None and not allow_prepared:
            raise BusyError(
                "A prepared recipe owns the interaction; start or cancel it first"
            )
        if self.operation is not None and not self.operation.done:
            raise BusyError("An operation already owns this interaction")
        activity = self.state.observations.get("machine_state_code")
        if activity and activity.value in MOTION_STATES:
            raise BusyError("Machine reports motion outside an owned operation")

    async def _wait_event(
        self,
        after_revision: int,
        predicate: Callable[[Packet], bool],
        timeout: float,
    ) -> Packet:
        deadline = time.monotonic() + timeout
        while True:
            for revision, packet in self._received_events:
                if revision > after_revision and predicate(packet):
                    return packet
            if not self.state.connected:
                raise SessionError("Disconnected while waiting for machine event")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Expected machine event was not observed")
            await self.wait_for_update(self.state.revision, min(0.1, remaining))

    async def _command(
        self,
        code: int,
        payload: bytes = b"",
        function: int = 1,
        *,
        active_operation: Operation | None = None,
        ack_payload: bytes | None = None,
        before_write: Callable[[], None] | None = None,
        on_transmit: Callable[[], None] | None = None,
        ack_budget: float | None = None,
    ) -> Packet:
        """Serialize frame+ACK; cancellation finishes the bounded frame first.

        ``before_write`` guards the first byte, after any journal hook; when it
        rejects, nothing is sent. The ACK is armed, and ``on_transmit`` runs,
        immediately before the frame can reach the machine: before its final
        chunk, or before a legacy transport's call. ``ack_budget`` bounds the
        ACK wait from that boundary.
        """
        frame = encode(code, payload, function=function)
        # The gate runs above every transport, injected ones included, and a
        # rejected frame is never handed to a transport.
        validate_control_frame(frame)
        async with self._commands:
            if active_operation is not None and (
                active_operation._stop_requested or active_operation.status != "running"
            ):
                raise BusyError("Operation is stopping or has ended")
            if not self.state.connected or not self.state.session_ready:
                raise SessionError("Control connection unavailable")
            future: asyncio.Future[Packet] = asyncio.get_running_loop().create_future()
            expected = payload if code == 11511 else ack_payload
            key = (code, 0xC2 if function == 2 else 0xC1, expected)
            progress = _Progress()
            loop = asyncio.get_running_loop()
            guard: Callable[[], None] | None = None
            if before_write is not None:
                admit = before_write

                def guard() -> None:
                    try:
                        admit()
                    except BaseException:
                        progress.rejected = True
                        raise

            def expire() -> None:
                # Cancels a write still suspended at the budget, if the
                # connector honors cancellation; the next write still waits
                # for it to return.
                if not write.done():
                    write.cancel()

            def transmit() -> None:
                # A matching notification before this point is stale.
                progress.transmitted_at = time.monotonic()
                self._waiter = (key, future)
                if ack_budget is not None:
                    progress.expiry = loop.call_later(ack_budget, expire)
                if on_transmit is not None:
                    on_transmit()

            # A stop's bounded journal wait must not consume its write bound.
            bound = self.write_timeout + (
                STOP_HOOK_TIMEOUT if code in STOP_COMMANDS else 0
            )
            write = asyncio.create_task(
                asyncio.wait_for(self._write(frame, guard, transmit), bound)
            )
            try:
                try:
                    await asyncio.wait({write})
                except asyncio.CancelledError:
                    await write
                    raise
                if write.cancelled():
                    # Only the budget cancels the write itself. An ACK proves
                    # delivery; otherwise the outcome is unknown.
                    if not _confirmed(future):
                        raise TimeoutError(
                            "Command budget expired during its write; "
                            "delivery unconfirmed"
                        )
                    return future.result()
                write.result()
                wait, budgeted = self.ack_timeout, False
                if ack_budget is not None and progress.transmitted_at is not None:
                    left = progress.transmitted_at + ack_budget - time.monotonic()
                    if left < wait:
                        wait, budgeted = max(0.0, left), True
                # wait_for can swallow cancellation racing with a completed ACK
                # on Python 3.10/3.11. wait preserves it and leaves cleanup below
                # responsible for the shared ACK future.
                done, _ = await asyncio.wait({future}, timeout=wait)
                if not done:
                    if budgeted:
                        raise TimeoutError(
                            "Acknowledgment not received within the command budget"
                        )
                    raise asyncio.TimeoutError()
                return future.result()
            except BaseException as exc:
                # A missing ACK leaves command effects uncertain; never retry.
                # A guard rejection sent nothing. A cancellation is uncertain
                # once the frame could have reached the machine unacknowledged.
                if isinstance(exc, Exception):
                    if not progress.rejected:
                        self._uncertain = True
                elif progress.transmitted_at is not None and not _confirmed(future):
                    self._uncertain = True
                raise
            finally:
                if progress.expiry is not None:
                    progress.expiry.cancel()
                self._waiter = None
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()  # consume an error even if the write failed

    async def _write(
        self,
        frame: bytes,
        guard: Callable[[], None] | None,
        transmit: Callable[[], None],
    ) -> None:
        transport = self.transport
        saved: dict[str, Any] = {}
        if guard is not None and getattr(
            transport, "command_guard_after_journal", False
        ):
            # The transport runs the guard after its hooks, before the first byte.
            saved["command_guard"] = getattr(transport, "command_guard", None)
            setattr(transport, "command_guard", guard)  # noqa: B010
            guard = None
        if getattr(transport, "transmit_hook_before_final_chunk", False):
            saved["on_transmit"] = getattr(transport, "on_transmit", None)
            setattr(transport, "on_transmit", transmit)  # noqa: B010
        try:
            if guard is not None:
                guard()
            if "on_transmit" not in saved:
                transmit()
            await transport._write_frame(frame)
        finally:
            for name, value in saved.items():
                setattr(transport, name, value)

    async def _prepare(self) -> None:
        if self._scale_mode:
            await self._command(8014)
            self._scale_mode = False
            await asyncio.sleep(self.transition_delay)
        await self._command(8022)
        await asyncio.sleep(self.transition_delay)
        await self._command(11511, b"\0" * 4, 2)
        await asyncio.sleep(self.transition_delay)

    def _water_source(self) -> int:
        info = self.state.observations.get("device_info_raw")
        sufficient = self.state.observations.get("water_sufficient")
        if sufficient is None or sufficient.value is not True:
            raise SessionError("Water sufficiency not confirmed; no automatic retry")
        raw = bytes.fromhex(info.value) if info else b""
        if len(raw) < 63 or raw[36] not in (0, 1):
            raise SessionError("Current water-source configuration unknown")
        if self._selected_water_source is not None:
            return self._selected_water_source
        return raw[36]

    async def start_water(
        self,
        *,
        volume_ml: float,
        temperature_c: float = 20,
        flow_ml_s: float = 3.0,
        pattern: str = "center",
        timeout: float | None = None,
    ) -> Operation:
        _number("volume_ml", volume_ml, 1, 1000)
        _number("temperature_c", temperature_c, 20, 99)
        _number("flow_ml_s", flow_ml_s, 0.1, 10)
        if pattern not in PATTERNS:
            raise ValueError("pattern must be center, ring or spiral")
        pattern_code = PATTERNS[pattern]
        limit = volume_ml / flow_ml_s + 45 if timeout is None else timeout
        async with self._lifecycle:
            self._require_idle()
            source = self._water_source()

            async def configure() -> None:
                await self._command(
                    8007, struct.pack("<If", pattern_code, temperature_c * 10)
                )
                await self._command(4510, struct.pack("<I", round(temperature_c * 10)))

            start = struct.pack(
                "<fffII",
                flow_ml_s * 10,
                volume_ml * 10,
                temperature_c * 10,
                source,
                pattern_code,
            )
            return self._launch("water", configure, 4506, start, limit, source=source)

    async def dispense_water(self, **kwargs: Any) -> dict[str, Any]:
        return await self._complete(self.start_water(**kwargs))

    async def _complete(self, starting: Awaitable[Operation]) -> dict[str, Any]:
        op = await starting
        try:
            return await op.wait()
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def start_grinder(
        self, *, size: int, rpm: int, duration_s: float
    ) -> Operation:
        _number("size", size, 1, 80)
        _number("rpm", rpm, 60, 120)
        _number("duration_s", duration_s, 0.1, 120)
        if size != int(size) or rpm != int(rpm):
            raise ValueError("size and rpm must be integers")
        async with self._lifecycle:
            self._require_idle()

            async def configure() -> None:
                await self._command(8006, struct.pack("<II", int(size), int(rpm)))

            start = struct.pack("<III", 1000, int(size), int(rpm))
            return self._launch("grinder", configure, 3500, start, duration_s)

    async def grind(self, **kwargs: Any) -> dict[str, Any]:
        return await self._complete(self.start_grinder(**kwargs))

    def _require_recipe_options(self, recipe: EncodedRecipe) -> None:
        if recipe.bypass_ml or recipe.cup_limits is not None or recipe.set_grinder_size:
            self._require_untested(
                "Grinder-setting upload, bypass or cup-limit behavior"
            )

    async def start_recipe(
        self, recipe: EncodedRecipe, *, timeout: float = 600
    ) -> Operation:
        if not isinstance(recipe, EncodedRecipe):
            raise TypeError(
                "Provide EncodedRecipe machine content, not a recipe name or URL"
            )
        self._require_recipe_options(recipe)
        async with self._lifecycle:
            self._require_idle()
            self._water_source()

            async def configure() -> None:
                await self._command(8102, recipe.settings_payload())
                if recipe.cup_limits is not None:
                    await self._command(8104, cup_limits_payload(recipe.cup_limits))
                await self._command(
                    8001 if recipe.set_grinder_size else 8004, recipe.payload
                )
                await asyncio.sleep(1 if self.transition_delay else 0)

            return self._launch("recipe", configure, 8002, b"", timeout)

    def _invalidate_preparation(self, reason: str) -> None:
        if self._prepared is not None:
            self._last_preparation = {
                **self._prepared,
                "status": "invalidated",
                "reason": reason,
            }
            self._prepared = None
            self._signal()

    def _preparation(self, token: object) -> dict[str, Any]:
        if (
            not isinstance(token, str)
            or self._prepared is None
            or token != self._prepared["token"]
            or self._prepared["status"] != "prepared"
        ):
            raise SessionError("Stale or invalid preparation token; no command sent")
        self._require_quiescent(allow_prepared=True)
        return self._prepared

    async def upload_recipe(self, recipe: EncodedRecipe) -> dict[str, Any]:
        """Upload coffee settings/content without executing or advancing a stage."""
        if not isinstance(recipe, EncodedRecipe):
            raise TypeError("Provide EncodedRecipe machine content")
        self._require_recipe_options(recipe)

        async def configure() -> None:
            await self._upload_command(8102, recipe.settings_payload())
            if recipe.cup_limits is not None:
                await self._upload_command(8104, cup_limits_payload(recipe.cup_limits))
            await self._upload_command(
                8001 if recipe.set_grinder_size else 8004, recipe.payload
            )

        return await self._upload("recipe", configure)

    async def upload_tea(self, recipe: Any) -> dict[str, Any]:
        """Upload dedicated tea content; never sends the tea execute command."""
        from .machine_controls import EncodedTeaRecipe

        if not isinstance(recipe, EncodedTeaRecipe):
            raise TypeError("Provide EncodedTeaRecipe machine content")
        self._require_untested("Dedicated tea recipe upload")

        async def configure() -> None:
            if recipe.cup_limits is not None:
                await self._upload_command(8104, cup_limits_payload(recipe.cup_limits))
            await self._upload_command(4513, recipe.payload)

        return await self._upload("tea", configure)

    async def _upload_command(self, code: int, payload: bytes) -> Packet:
        preparation = self._prepared

        def validate() -> None:
            self._require_session()
            if preparation is None or self._prepared is not preparation:
                raise SessionError("Machine context changed during recipe upload")

        validate()
        return await self._command(code, payload, before_write=validate)

    async def _upload(
        self, kind: str, configure: Callable[[], Awaitable[None]]
    ) -> dict[str, Any]:
        async with self._lifecycle:
            self._require_idle(allow_no_water=True)
            try:
                await self._prepare()
                self._require_session()
                self._prepared = {
                    "token": uuid.uuid4().hex,
                    "kind": kind,
                    "status": "uploading",
                    "retained_until": "start/cancel/disconnect",
                }
                await configure()
                self._require_session()
                prepared = self._prepared
                if prepared is None:
                    raise SessionError("Machine context changed during recipe upload")
                prepared["status"] = "prepared"
                self._signal()
                return dict(prepared)
            except BaseException:
                self._invalidate_preparation("upload_failed")
                self._uncertain = True
                raise

    async def start_prepared(self, token: str, *, timeout: float = 600) -> Operation:
        """Execute one owned upload without home/mode changes or another upload."""
        _number("timeout", timeout, 0.1, 3600)
        async with self._lifecycle:
            preparation = self._preparation(token)
            self._water_source()
            kind = preparation["kind"]
            preparation["status"] = "starting"

            def validate() -> None:
                self._require_session()
                if self._prepared is not preparation:
                    raise SessionError(
                        "Prepared machine context changed before execution"
                    )
                self._water_source()

            async def configure() -> None:
                validate()

            def claim() -> None:
                validate()
                self._last_preparation = {**preparation, "status": "consumed"}
                self._prepared = None

            return self._launch(
                kind,
                configure,
                8002 if kind == "recipe" else 4512,
                b"",
                timeout,
                prepare=False,
                before_start=claim,
            )

    async def cancel_prepared(self, token: str) -> dict[str, Any]:
        """Leave an owned preparation and return home; no experimental gate."""
        async with self._lifecycle:
            preparation = self._preparation(token)
            self._prepared = None
            last = {**preparation, "status": "cancelling"}
            self._last_preparation = last
            try:
                await self._command(8017, ack_payload=b"")
                await asyncio.sleep(self.transition_delay)
                await self._command(8022)
                await asyncio.sleep(self.transition_delay)
                last["status"] = "cancelled"
                return {**last, "cancelled": True}
            except BaseException:
                last.update(status="invalidated", reason="cancel_failed")
                self._uncertain = True
                raise
            finally:
                self._signal()

    def _launch(
        self,
        kind: str,
        configure: Callable[[], Awaitable[None]],
        start_code: int,
        payload: bytes,
        timeout: float,
        *,
        prepare: bool = True,
        before_start: Callable[[], None] | None = None,
        source: int | None = None,
    ) -> Operation:
        _number("timeout", timeout, 0.1, 3600)
        op = Operation(kind, _since=self._event_revision, _source=source)
        self.operation = op
        op._task = asyncio.create_task(
            self._run(
                op,
                configure,
                start_code,
                payload,
                timeout,
                prepare=prepare,
                before_start=before_start,
            )
        )
        self._signal()
        return op

    def _start_guard(
        self, op: Operation, claim: Callable[[], None] | None
    ) -> Callable[[], None]:
        """Recheck admission after configuration and hooks, before byte one."""

        def guard() -> None:
            self._require_session()
            if not self._owns():
                raise SessionError("Machine ownership lost; start not sent")
            if op._stop_requested:
                raise BusyError("Operation was stopped before its start was sent")
            if claim is None and op.kind in WATER_KINDS:
                # A selected source still overrides the reported one.
                source = self._water_source()
                if op._source is not None and source != op._source:
                    raise SessionError(
                        "Water source changed after admission; start not sent"
                    )
            if self._last(FAULT_KEYS) > op._since:
                self._uncertain = True
                raise SessionError("Machine reported a fault before start; not sent")
            if self._last(MOTION_KEYS) > op._since:
                self._uncertain = True
                raise BusyError("Machine reported motion before start; not sent")
            if claim is not None:
                claim()

        return guard

    def _completed(self, op: Operation) -> bool:
        events = list(op._events)
        if op.protocol_error is not None:
            raise ProtocolCommandError(
                op.protocol_error, op.protocol_error.get("pending_command")
            )
        if any(code in FAULT_EVENTS for code, _ in events):
            raise SessionError("Machine reported a fault; inspect last machine event")
        states = [
            int.from_bytes(data[:4], "little")
            for code, data in events
            if code == 8023 and len(data) >= 4
        ]
        if op.kind in ("recipe", "tea"):
            return 0x24 in states or (
                any(code == 40513 for code, _ in events)
                and bool(states)
                and states[-1] == 1
            )
        if op.kind == "calibration":
            return any(
                code == 40526 and data == CALIBRATED_POSITION for code, data in events
            )
        finished = 40511 if op.kind == "water" else 40507
        return any(code == finished for code, _ in events)

    async def _run(
        self,
        op: Operation,
        configure: Callable[[], Awaitable[None]],
        start_code: int,
        payload: bytes,
        timeout: float,
        *,
        prepare: bool = True,
        before_start: Callable[[], None] | None = None,
    ) -> None:
        success = False
        try:
            if prepare:
                await self._prepare()
            await configure()
            op.phase = "awaiting_machine"
            boundary: list[float] = []

            def transmitted() -> None:
                op._armed = True  # starts can take effect before their ACK
                boundary.append(time.monotonic())

            grinder = op.kind == "grinder"
            await self._command(
                start_code,
                payload,
                before_write=self._start_guard(op, before_start),
                on_transmit=transmitted,
                # A grinder pulse is budgeted from its start frame, so a slow
                # ACK cannot extend it; expiry before the ACK requests stop.
                ack_budget=timeout if grinder else None,
            )
            started_at = time.monotonic()
            deadline = (boundary[0] if grinder else started_at) + timeout
            while True:
                if not self.state.connected:
                    raise SessionError(
                        "Connection lost during operation; machine state unknown"
                    )
                success = self._completed(op)
                if success:
                    op.stop_observed = op.kind != "calibration"
                    break
                now = time.monotonic()
                if (
                    op.kind in ("water", "grinder")
                    and not op.start_observed
                    and now - started_at >= self.start_timeout
                ):
                    raise TimeoutError(
                        "Start acknowledged but activity not observed; requesting stop"
                    )
                if now >= deadline:
                    if op.kind == "grinder":
                        if not op.start_observed:
                            raise TimeoutError(
                                "Grinder activity not observed; requesting stop"
                            )
                        break  # planned bounded pulse; stop is sent below
                    raise TimeoutError("Operation deadline reached; requesting stop")
                # Never mistake ACK for start. Keep recipe prompts observable.
                await asyncio.sleep(0.05)
            op.status = "completed"
        except asyncio.CancelledError:
            op.status = "stopped"
        except Exception as exc:
            op.status = "failed"
            op.error = f"{type(exc).__name__}: {exc}"
        finally:
            await self._finish(op, success, before_start is not None)

    async def _finish(self, op: Operation, success: bool, claimed: bool) -> None:
        try:
            await self._cleanup_operation(op, success, claimed)
        finally:
            # Even an interrupted cleanup leaves a final result for waiters.
            if op.cleanup_status == "pending":
                op.cleanup_status = "skipped_uncertain"
                self._uncertain = True
            op.phase = op.status
            op._final = self.snapshot()
            self._signal()

    async def _cleanup_operation(
        self, op: Operation, success: bool, claimed: bool
    ) -> None:
        if claimed and self._prepared is not None:
            self._invalidate_preparation("execution_not_transmitted")
            self._uncertain = True
        # Also stop after natural manual completion to make cleanup explicit.
        if op._armed and (not success or op.kind not in ("recipe", "tea")):
            await self._send_stop(op)
        if op._armed and not op.stop_observed and self._moving(op):
            # An unconfirmed operation is never terminal while motion is known.
            op.status = "unknown"
            op.error = op.error or "Stop not observed; machine reports motion"
            self._uncertain = True
        if not self.state.connected:
            op.status = "unknown"
            self._uncertain = True
        # A lost ACK may leave mode/actuator effects uncertain even after stop.
        if self.state.session_ready and not self._uncertain:
            try:
                exit_code = EXIT_CODES.get(op.kind)
                if exit_code is not None:
                    await self._command(exit_code)
                    await asyncio.sleep(self.transition_delay)
                if op.kind not in ("recipe", "tea") or not success:
                    await self._command(8022)
                op.cleanup_status = "completed"
            except Exception as exc:
                op.error = f"Cleanup unconfirmed: {type(exc).__name__}: {exc}"
                self._uncertain = True
                op.cleanup_status = "failed"
        else:
            op.cleanup_status = "skipped_uncertain"

    def _last(self, keys: tuple[SeenKey, ...]) -> int:
        return max(self._seen.get(key, 0) for key in keys)

    def _rest_and_motion(self, kind: str) -> tuple[int, int]:
        stopped_code = 40507 if kind in ("grinder", "calibration") else 40511
        return self._last(((stopped_code, None), (8023, 1))), self._last(MOTION_KEYS)

    def _stopped_since(self, revision: int, kind: str, stopped: bool) -> bool:
        """The newest stop or motion evidence after ``revision`` decides;
        without any, ``stopped`` (prior completion) stands."""
        rest, motion = self._rest_and_motion(kind)
        return rest > motion if max(rest, motion) > revision else stopped

    def _moving(self, op: Operation) -> bool:
        rest, motion = self._rest_and_motion(op.kind)
        return motion > max(rest, op._since)

    async def _send_stop(self, op: Operation) -> None:
        boundary: list[int] = []
        try:
            await self._command(
                STOP_CODES[op.kind],
                on_transmit=lambda: boundary.append(self._event_revision),
            )
            op.stop_acknowledged = True
            await asyncio.sleep(self.transition_delay)
        except BaseException as exc:
            if not op.stop_acknowledged:
                op.stop_acknowledged = False
                op.status = "unknown"
                op.error = f"Stop unconfirmed: {type(exc).__name__}: {exc}"
                self._uncertain = True
            if not isinstance(exc, Exception):
                raise
        finally:
            # Completion observed before the stop (its own stop evidence) counts
            # from launch; otherwise only evidence after the stop's write
            # boundary counts. Either way newer motion voids it, even when the
            # stop itself was only acknowledged.
            since = op._since if op.stop_observed else boundary[0] if boundary else None
            if since is not None:
                op.stop_observed = self._stopped_since(since, op.kind, op.stop_observed)

    async def stop(self) -> dict[str, Any] | None:
        op = self.operation
        if op is None or op._task is None:
            return None
        if op.status == "running" and not op._task.done() and not op._stop_requested:
            op._stop_requested = True
            # Scheduled on the loop, not in this coroutine: the request survives
            # its caller's cancellation, and every later caller shares it. It
            # runs after a newly scheduled worker has entered its try/finally.
            asyncio.get_running_loop().call_soon(self._cancel_worker, op, op._task)
        return await op.wait()

    @staticmethod
    def _cancel_worker(op: Operation, task: asyncio.Task[None]) -> None:
        # Never interrupt cleanup: the worker leaves "running" before it.
        if not task.done() and op.status == "running":
            task.cancel()

    async def _pause_resume(self, resume: bool) -> dict[str, Any]:
        self._require_session()
        op = self.operation
        if op is None or op.status != "running" or not op._armed:
            raise BusyError("No active operation to pause or resume")
        if op.kind == "calibration":
            raise UnsupportedStageError(
                "Calibration supports stop; pausing its adjustment is not validated"
            )
        if resume != op._paused:
            raise BusyError(
                "Operation is not paused" if resume else "Operation already paused"
            )
        code = PAUSE_RESUME_CODES[op.kind][int(resume)]
        try:
            await self._command(code, active_operation=op)
        except (Exception, asyncio.CancelledError):
            # An uncertain pause/resume must not leave an unattended operation.
            await self.stop()
            raise
        op._paused = not resume
        # ACK-confirmed request, not evidence that the physical action occurred.
        op.phase = "resume_acknowledged" if resume else "pause_acknowledged"
        self._signal()
        return op.snapshot()

    async def pause(self) -> dict[str, Any]:
        return await self._pause_resume(False)

    async def resume(self) -> dict[str, Any]:
        return await self._pause_resume(True)

    async def advance(self, *, expected_stage: str, revision: int) -> None:
        self._require_session()
        if revision != self._event_revision:
            raise BusyError("Machine events changed; read state before advancing")
        raise UnsupportedStageError(
            f"Remote advancement for {expected_stage!r} is not validated; "
            "use the machine controls"
        )

    async def enter_scale(self) -> dict[str, Any]:
        async with self._lifecycle:
            self._require_idle()
            await self._prepare()
            await self._command(8003)
            self._scale_mode = True
        return self.get_state()

    async def tare(self) -> dict[str, Any]:
        async with self._lifecycle:
            self._require_session()
            if not self._scale_mode or (self.operation and not self.operation.done):
                raise BusyError("Tare requires standalone scale mode")
            await self._command(8500)
        return self.get_state()

    async def exit_scale(self) -> dict[str, Any]:
        async with self._lifecycle:
            self._require_session()
            if self.operation and not self.operation.done:
                raise BusyError("Operation in progress")
            if self._scale_mode:
                await self._command(8014)
                self._scale_mode = False
                await asyncio.sleep(self.transition_delay)
                await self._command(8022)
        return self.get_state()
