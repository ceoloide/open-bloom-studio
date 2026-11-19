"""Ordered off-loop journal writer and command hooks for durable intent.

:class:`Recorder` appends events to the store in order on one worker thread,
with a bounded queue. :class:`JournalHooks` implements
:class:`~xbloom_studio.transport.CommandHooks`: before each frame it records a
``command.intent`` event and waits until that event and everything queued
before it are committed, so the transport's command guard and the first byte
only follow a durable intent. A committed intent is an attempt, not a
delivery: ``command.write_completed`` only means the frame was handed to the
connector, and ``command.write_failed`` that it was refused or failed.
Delivery and its effect are observed separately, from the machine.

A journal failure prevents every later write except stop commands, which the
transport sends after a bounded wait so a stalled or failed disk can never
block a stop.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Protocol

from .protocol import Packet
from .serialization import now

PASSIVE_TELEMETRY_KINDS = frozenset(
    {"ble.incoming_chunk", "ble.packet", "ble.decode_error"}
)


class JournalStore(Protocol):
    def append(
        self,
        kind: str,
        data: Any,
        *,
        session_id: str | None = ...,
        sequence: int | None = ...,
        operation_id: str | None = ...,
        device: str | None = ...,
        at: str | None = ...,
        monotonic_ns: int | None = ...,
    ) -> int | None: ...


class Recorder:
    """Queue events in order for one writer thread; report any loss or failure.

    ``emit`` never blocks and never raises: a full queue or a failed append
    marks the recorder failed, and ``flush`` then raises. Passive telemetry
    is skipped while idle unless ``capture_idle_telemetry`` is set, an
    operation is active or a ``capturing()`` window is open.
    """

    def __init__(
        self,
        store: JournalStore,
        device: str | None,
        capacity: int = 8192,
        *,
        capture_idle_telemetry: bool = True,
    ) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Journal capacity must be a positive integer")
        if type(capture_idle_telemetry) is not bool:
            raise ValueError("capture_idle_telemetry must be a boolean")
        self.loop = asyncio.get_running_loop()
        self.fault = asyncio.Event()
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(capacity)
        self.pending = 0
        self.lost = 0
        self.reported_lost = 0
        self.store, self.device = store, device
        self.capture_idle_telemetry = capture_idle_telemetry
        self._capture_depth = 0
        self.session_id = uuid.uuid4().hex
        self.operation_id: str | None = None
        self.sequence = 0
        self.error: BaseException | None = None
        self.closed = False
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="bloom-journal"
        )
        self.last: Future[Any] | None = None

    def _fail(self, exc: BaseException) -> None:
        self.error = exc
        try:
            self.loop.call_soon_threadsafe(self.fault.set)
        except RuntimeError:
            pass  # The loop already closed; ``error`` still records the failure.

    def _idle(self, kind: str) -> bool:
        return (
            kind in PASSIVE_TELEMETRY_KINDS
            and not self.capture_idle_telemetry
            and self.operation_id is None
            and self._capture_depth == 0
        )

    def accepts(self, kind: str) -> bool:
        with self.lock:
            return not self._idle(kind)

    @contextmanager
    def capturing(self) -> Iterator[None]:
        with self.lock:
            self._capture_depth += 1
        try:
            yield
        finally:
            with self.lock:
                self._capture_depth -= 1

    def emit(self, kind: str, data: Any) -> None:
        with self.lock:
            if self.closed:
                self._fail(RuntimeError("Event arrived after journal closed"))
                return
            if self._idle(kind):
                return
            if not self.slots.acquire(blocking=False):
                self.lost += 1
                self._fail(RuntimeError("Capture queue full; recording incomplete"))
                return
            self.sequence += 1
            context: dict[str, Any] = {
                "session_id": self.session_id,
                "sequence": self.sequence,
                "operation_id": self.operation_id,
                "device": self.device,
                "at": now(),
                "monotonic_ns": time.monotonic_ns(),
            }
            self.pending += 1
            self.last = self.executor.submit(
                self.store.append, kind, deepcopy(data), **context
            )
            self.last.add_done_callback(self._finished)

    def _finished(self, future: Future[Any]) -> None:
        with self.lock:
            self.pending -= 1
            self.slots.release()
            try:
                future.result()
            except BaseException as exc:
                self._fail(exc)

    async def flush(self) -> None:
        """Wait until every queued event is committed; raise if any was not.

        Cancelling a flush never cancels queued appends.
        """
        lost = 0
        try:
            if self.last is not None:
                await asyncio.shield(asyncio.wrap_future(self.last))
            with self.lock:
                future = None
                if self.lost > self.reported_lost:
                    self.sequence += 1
                    lost = self.lost
                    future = self.executor.submit(
                        self.store.append,
                        "capture.gap",
                        {"lost_events": lost, "reason": "queue_overflow"},
                        session_id=self.session_id,
                        sequence=self.sequence,
                        operation_id=self.operation_id,
                        device=self.device,
                    )
                    self.last = future
            if future is not None:
                await asyncio.shield(asyncio.wrap_future(future))
                self.reported_lost = lost
        except Exception as exc:
            self._fail(exc)
        if self.error is not None:
            raise RuntimeError(f"Journal failed: {self.error}") from self.error

    async def close(self) -> None:
        try:
            await self.flush()
        finally:
            self.closed = True
            await asyncio.to_thread(self.executor.shutdown, wait=True)


class JournalHooks:
    """Durable intent before every frame, through the transport's CommandHooks.

    Pass to ``Interaction(command_hooks=...)`` or a transport's ``hooks``.
    The transport orders: intent committed, command guard, first byte. A guard
    that refuses after the commit (for example because the machine changed
    state during the flush) leaves the intent followed by ``write_failed``.
    """

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def before_write(self, packet: Packet, frame: bytes) -> None:
        self.recorder.emit(
            "command.intent", {"command": packet.command, "frame_hex": frame.hex()}
        )
        await self.recorder.flush()

    def write_completed(self, packet: Packet) -> None:
        self.recorder.emit("command.write_completed", {"command": packet.command})

    def write_failed(self, packet: Packet, error: BaseException) -> None:
        self.recorder.emit(
            "command.write_failed", {"command": packet.command, "error": repr(error)}
        )
