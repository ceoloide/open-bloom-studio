"""Platform-neutral machine application service: one device, owner and scheduler.

:class:`MachineService` owns one machine connection for one local database.
Every accepted start is persisted under its idempotency key before any frame
is written, then runs as a background task that outlives the request that
started it. Command intent is journaled through
:class:`~xbloom_studio.journal.JournalHooks` before the command guard and the
first byte. Outcomes keep their evidence apart: a rejected request writes
nothing, a failed or interrupted write leaves delivery ``unknown``, an
acknowledgment confirms receipt only, completion is reported only when the
machine is observed finishing, and an upload is never reported as read back.
An owner that restarts marks unfinished operations ``unknown`` and never
replays them.
"""

from __future__ import annotations

import asyncio
import hmac
import uuid
from collections.abc import Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from ._cleanup import wait_cleanup
from .connector import Connector, normalize_identity
from .control import BusyError, Interaction
from .journal import JournalHooks, Recorder
from .machine_api import MachineCommandAPI, encoded_step, same_request
from .machine_controls import EncodedTeaRecipe
from .ownership import AddressLease
from .recipes import HistoryService, RecipeService, compile_recipe
from .storage import ConflictError
from .transport import BleakConnector

if TYPE_CHECKING:
    from .control import Operation
    from .storage import Store

__all__ = ["HistoryService", "MachineService", "RecordingConnector"]

InteractionFactory = Callable[..., Interaction]


class RecordingConnector:
    """Journal one session's link lifecycle around any connector.

    Connecting waits until its request is durable. A lost link is recorded
    as a capture gap: nothing arriving after it was observed.
    """

    def __init__(self, connector: Connector, recorder: Recorder) -> None:
        self.connector, self.recorder = connector, recorder

    @property
    def identity(self) -> str:
        return self.connector.identity

    @property
    def write_size(self) -> int:
        return self.connector.write_size

    @property
    def is_connected(self) -> bool:
        return self.connector.is_connected

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        self.recorder.emit("session.connect_requested", {})
        await self.recorder.flush()

        def disconnected() -> None:
            self.recorder.emit("session.disconnected", {"capture_gap": True})
            on_disconnect()

        await self.connector.connect(disconnected)
        self.recorder.emit("session.connected", {})

    async def disconnect(self) -> None:
        try:
            await self.connector.disconnect()
        finally:
            self.recorder.emit("session.closed", {})

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        await self.connector.start_notify(callback)
        self.recorder.emit("session.subscribed", {})

    async def stop_notify(self) -> None:
        await self.connector.stop_notify()
        self.recorder.emit("session.unsubscribed", {})

    async def write(self, data: bytes) -> None:
        await self.connector.write(data)

    async def read_information(self, name: str) -> str | None:
        value = await self.connector.read_information(name)
        self.recorder.emit("gatt.info", {"name": name, "value": value})
        return value


class MachineService(MachineCommandAPI):
    """One device, one owner, one scheduler. No implicit reconnect after failure.

    Pass ``device`` for the local Bluetooth link, or ``connector`` for any
    other :class:`~xbloom_studio.Connector`; both must name the same machine.
    The connector is reused for every session this service opens.
    """

    def __init__(
        self,
        store: Store,
        device: object = None,
        interaction_factory: InteractionFactory = Interaction,
        interaction_options: dict[str, Any] | None = None,
        *,
        connector: Connector | None = None,
        capture_idle_telemetry: bool = False,
    ) -> None:
        if type(capture_idle_telemetry) is not bool:
            raise ValueError("capture_idle_telemetry must be a boolean")
        if connector is not None and not isinstance(connector, Connector):
            raise TypeError("connector must implement the Connector protocol")
        identity = None if device is None else normalize_identity(device)
        if connector is not None:
            linked = normalize_identity(connector.identity)
            if identity is not None and identity != linked:
                raise ValueError("Device address and connector identity differ")
            identity = linked
        self.store = store
        # The canonical identity names the machine in history and ownership;
        # the address as given is what the local Bluetooth link looks up.
        self.device = identity
        self.address = device
        self.connector = connector
        self.capture_idle_telemetry = capture_idle_telemetry
        self.recipe_service = RecipeService(store)
        self.history_service = HistoryService(store)
        self.interaction_factory = interaction_factory
        self.options = dict(interaction_options or {})
        self.lease = AddressLease("application:" + str(store.path))
        self.owned = False
        self.session: Interaction | None = None
        self.recorder: Recorder | None = None
        self.task: asyncio.Task[None] | None = None
        self.operation_id: str | None = None
        self.projection: dict[str, Any] | None = None
        self.gate: dict[str, Any] | None = None
        self.gate_event: asyncio.Event | None = None
        self.guard = asyncio.Lock()
        self.closed = False
        self.disconnecting = False
        self._disconnect_task: asyncio.Task[dict[str, Any]] | None = None
        self.stopping = False
        self.worker_started: asyncio.Event | None = None
        self.prepared: dict[str, Any] | None = None
        self._prepared_plan: dict[str, Any] | None = None

    @property
    def _session(self) -> Interaction:
        if self.session is None:
            raise RuntimeError("No machine session; connect first")
        return self.session

    @property
    def _recorder(self) -> Recorder:
        if self.recorder is None:
            raise RuntimeError("No journal; connect first")
        return self.recorder

    def state(self) -> dict[str, Any]:
        prepared = deepcopy(self.prepared)
        if prepared is not None and self.session is not None:
            low = self.session.get_state()
            if not low.get("prepared"):
                last = low.get("last_preparation") or {}
                prepared.update(
                    status="invalidated",
                    reason=last.get("reason", "no_longer_prepared"),
                    recovery="Disconnect and explicitly upload again; retained "
                    "machine content is not verified",
                )
        recorder = self.recorder
        return {
            "device": self.device,
            "machine": self.session.get_state() if self.session else None,
            "operation": deepcopy(self.projection),
            "prepared": prepared,
            "gate": deepcopy(self.gate),
            "recording": {
                "pending": recorder.pending,
                "lost": recorder.lost,
                "error": str(recorder.error) if recorder.error else None,
            }
            if recorder
            else None,
        }

    async def _own(self) -> None:
        """Hold the database's owner lease, then recover interrupted operations."""
        if not self.owned:
            self.lease.acquire()
            try:
                await asyncio.to_thread(self.store.recover)
            except BaseException:
                self.lease.release()
                raise
            self.owned = True

    def _link(self) -> Connector:
        if self.connector is not None:
            return self.connector
        return BleakConnector(self.address)

    async def _connect(self, allow_untested: bool) -> None:
        if self.closed or self.disconnecting:
            raise RuntimeError("Machine service is closed or disconnecting")
        if self.session:
            if not self.session.state.session_ready:
                raise RuntimeError(
                    "Connection lost; explicitly disconnect and open a fresh session"
                )
            if allow_untested and not self.session.allow_untested:
                raise ValueError("Open a fresh session with experimental opt-in")
            return
        if not self.device:
            raise ValueError("Configure a machine address for machine operations")
        await self._own()
        recorder = Recorder(
            self.store, self.device, capture_idle_telemetry=self.capture_idle_telemetry
        )
        session = self.interaction_factory(
            connector=RecordingConnector(self._link(), recorder),
            command_hooks=JournalHooks(recorder),
            allow_control=True,
            allow_untested=allow_untested,
            event_sink=recorder.emit,
            **self.options,
        )
        cast(Any, session.transport).event_sink = recorder.emit
        try:
            with recorder.capturing():
                await session.__aenter__()
            await recorder.flush()
        except BaseException:
            try:
                await session.disconnect()
            finally:
                await recorder.close()
            raise
        self.session, self.recorder = session, recorder

    async def connect(self, *, allow_untested: bool = False) -> dict[str, Any]:
        if type(allow_untested) is not bool:
            raise ValueError("allow_untested must be a boolean")
        if self.closed or self.disconnecting:
            raise BusyError(
                "Machine service is closed or disconnecting; nothing queued"
            )
        async with self.guard:
            await self._connect(allow_untested)
            return self.state()

    def prepare(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("Request must be an object")
        kind = request.get("kind")
        if kind == "repeat":
            if set(request) != {"kind", "operation_id"}:
                raise ValueError("Repeat needs operation_id only")
            return {
                **self.history_service.repeat_plan(request["operation_id"]),
                "resolved_request": deepcopy(request),
            }
        if kind == "recipe":
            if set(request) - {"kind", "reference", "revision", "document"}:
                raise ValueError("Unknown recipe request fields")
            if ("reference" in request) == ("document" in request):
                raise ValueError(
                    "Provide exactly one recipe reference or inline document"
                )
            if "document" in request:
                return compile_recipe(request["document"])
            plan = self.recipe_service.compile(
                request["reference"], request.get("revision")
            )
            return {
                **plan,
                "resolved_request": {
                    "kind": "recipe",
                    "reference": plan["recipe_id"],
                    "revision": plan["recipe_revision"],
                },
            }
        if kind in ("water", "grind"):
            step = {k: v for k, v in request.items() if k != "name"}
            return compile_recipe({"name": request.get("name", kind), "steps": [step]})
        raise ValueError("Request kind must be recipe, repeat, water or grind")

    async def start(
        self,
        request: Any,
        idempotency_key: str,
        allow_control: bool = False,
        *,
        allow_untested: bool = False,
        expected_recipe_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("A nonempty idempotency key is required")
        if allow_control is not True:
            raise ValueError("Starting requires allow_control=True")
        if type(allow_untested) is not bool:
            raise ValueError("allow_untested must be a boolean")
        self._reject_queued_write()
        async with self.guard:
            # Replay lookup precedes resolution of mutable recipe names/revisions.
            if self.task is not None and not self.task.done():
                projection = self.projection or {}
                if projection.get("idempotency_key") == idempotency_key and (
                    same_request(projection.get("request"), request)
                ):
                    return deepcopy(projection)
                raise BusyError("Machine already has an operation; nothing queued")
            await self._own()
            row = await asyncio.to_thread(self.store.operation_by_key, idempotency_key)
            if row:
                self._same_attempt(row, request)
                return row
            if self.prepared is not None:
                raise BusyError(
                    "A recipe owns preparation; start, cancel or disconnect first"
                )
            plan = await asyncio.to_thread(self.prepare, request)
            if (
                expected_recipe_sha256 is not None
                and plan["recipe_sha256"] != expected_recipe_sha256
            ):
                raise ConflictError(
                    "Prepared recipe changed; preview it again before starting"
                )
            if plan["requires_untested"] and not allow_untested:
                raise ValueError("Recipe requires allow_untested")
            plan.pop("resolved_request", None)  # Preview routing, not history.
            await self._connect(allow_untested)
            return await self._accept_and_launch(request, plan, idempotency_key)

    async def _run_step(
        self, step: dict[str, Any], prepared_token: str | None
    ) -> Operation:
        session = self._session
        kind = step["kind"]
        params = {k: v for k, v in step.items() if k != "kind"}
        if prepared_token is not None:
            return await session.start_prepared(
                prepared_token, timeout=params.get("timeout", 600)
            )
        if kind in ("native", "tea"):
            recipe = encoded_step(step)
            if isinstance(recipe, EncodedTeaRecipe):
                return await session.start_tea(recipe, timeout=params["timeout"])
            return await session.start_recipe(
                recipe, timeout=params.get("timeout", 600)
            )
        if kind == "water":
            return await session.start_water(**params)
        if kind == "grind":
            return await session.start_grinder(**params)
        raise ValueError(f"Unsupported execution step: {kind}")

    async def _execute(
        self, ident: str, plan: dict[str, Any], prepared_token: str | None = None
    ) -> None:
        assert self.worker_started is not None
        self.worker_started.set()
        session, recorder = self._session, self._recorder
        results: list[dict[str, Any]] = []
        status = "failed"
        result: dict[str, Any] | None = None

        async def recording_failed(owner: asyncio.Task[Any]) -> None:
            await recorder.fault.wait()
            owner.cancel()

        current = asyncio.current_task()
        assert current is not None
        watcher = asyncio.create_task(recording_failed(current))
        try:
            await self._transition(ident, "running")
            for index, step in enumerate(plan["execution"]):
                await recorder.flush()
                recorder.emit("step.started", {"index": index, "step": step})
                kind = step["kind"]
                value: dict[str, Any]
                if kind == "wait":
                    await asyncio.sleep(step["duration_s"])
                    value = {"status": "completed"}
                elif kind == "user":
                    async with self.guard:
                        self.gate = {
                            "operation_id": ident,
                            "token": uuid.uuid4().hex,
                            "index": index,
                            "instruction": step["instruction"],
                        }
                        self.gate_event = asyncio.Event()
                        await self._transition(
                            ident,
                            "waiting_for_user",
                            {"gate": self.gate, "steps": results},
                        )
                        recorder.emit("operation.user_gate", self.gate)
                    await self.gate_event.wait()
                    await self._transition(ident, "running")
                    value = {
                        "status": "completed",
                        "physical_preparation_acknowledged": True,
                    }
                elif kind == "tare":
                    await session.enter_scale()
                    try:
                        await session.tare()
                    finally:
                        await session.exit_scale()
                    value = {"status": "completed"}
                else:
                    low = await self._run_step(step, prepared_token)
                    prepared_token = None
                    while not low.done:
                        if recorder.error:
                            await session.stop()
                            raise RuntimeError("Recording failed; stopped operation")
                        await asyncio.sleep(0.02)
                    value = await low.wait()
                results.append(value)
                recorder.emit("step.finished", {"index": index, "result": value})
                if (
                    value["status"] != "completed"
                    or value.get("cleanup_status", "completed") != "completed"
                ):
                    status = (
                        value["status"] if value["status"] != "completed" else "failed"
                    )
                    break
            else:
                status = "completed"
        except asyncio.CancelledError:
            low_op = session.operation
            active = low_op is not None and not low_op.done
            stopped = await session.stop()
            result = {"stop_result": stopped}
            confirmed = bool(
                stopped
                and stopped.get("stop_acknowledged")
                and stopped.get("status") != "unknown"
            )
            status = (
                "stopped"
                if not session._uncertain and (not active or confirmed)
                else "unknown"
            )
        except Exception as exc:
            result = {"error": str(exc), "type": type(exc).__name__}
            try:
                stopped = await session.stop()
                result["stop_result"] = stopped
                if session._uncertain or (
                    stopped and stopped.get("status") == "unknown"
                ):
                    status = "unknown"
            except Exception:
                status = "unknown"
        finally:
            if prepared_token is not None:
                session._invalidate_preparation("prepared_start_not_sent")
                session._uncertain = True
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self.gate = None
            self.gate_event = None
            result = {**(result or {}), "steps": results, "state": session.get_state()}
            recorder.emit("operation.finished", {"status": status, "result": result})
            try:
                await recorder.flush()
            except Exception as exc:
                status = "unknown"
                result["recording_error"] = str(exc)
            await self._transition(ident, status, result)
            recorder.operation_id = None

    async def _transition(
        self, ident: str, status: str, result: dict[str, Any] | None = None
    ) -> None:
        self.projection = {
            **(self.projection or {}),
            "status": status,
            "result": deepcopy(result),
        }
        await asyncio.to_thread(self.store.finish, ident, status, result)

    def _active(self, ident: str) -> asyncio.Task[None]:
        if ident != self.operation_id or self.task is None or self.task.done():
            raise BusyError("No matching active operation")
        return self.task

    async def acknowledge(self, operation_id: str, token: str) -> dict[str, Any]:
        async with self.guard:
            self._active(operation_id)
            gate, event = self.gate, self.gate_event
            if (
                gate is None
                or event is None
                or not isinstance(token, str)
                or not hmac.compare_digest(gate["token"], token)
            ):
                raise ConflictError("Stale or incorrect preparation gate")
            self._recorder.emit("operation.user_acknowledged", {"token": token})
            self.gate = None
            event.set()
            return {"acknowledged": True}

    async def stop(self, operation_id: str) -> dict[str, Any]:
        async with self.guard:
            task = self._active(operation_id)
            self.gate = None
            if not self.stopping:
                self.stopping = True
                assert self.worker_started is not None
                await self.worker_started.wait()
                task.cancel()
        await asyncio.shield(task)
        return await asyncio.to_thread(self.store.operation, operation_id)

    async def pause_resume(
        self, operation_id: str, resume: bool = False
    ) -> dict[str, Any]:
        if type(resume) is not bool:
            raise ValueError("resume must be a boolean")
        self._active(operation_id)
        session = self._session
        op = session.operation
        if op is None or op.done or self.gate is not None:
            raise BusyError("Pause/resume only supported during an active device step")
        await (session.resume() if resume else session.pause())
        return self.state()

    async def disconnect(self) -> dict[str, Any]:
        async with self.guard:
            if self._disconnect_task is None or self._disconnect_task.done():
                self.disconnecting = True
                self._disconnect_task = asyncio.create_task(self._finish_disconnect())
            cleanup = self._disconnect_task
        return await wait_cleanup(cleanup)

    async def _finish_disconnect(self) -> dict[str, Any]:
        try:
            async with self.guard:
                self.prepared = None
                self._prepared_plan = None
                task = self.task
                if task is not None and not task.done() and not self.stopping:
                    self.stopping = True
                    self.gate = None
                    assert self.worker_started is not None
                    await self.worker_started.wait()
                    task.cancel()
            if task is not None:
                await asyncio.shield(task)
        finally:
            try:
                if self.session is not None:
                    try:
                        await self.session.disconnect()
                    finally:
                        try:
                            await self._recorder.close()
                        finally:
                            self.session = None
                            self.recorder = None
            finally:
                async with self.guard:
                    self.disconnecting = False
        return {"disconnected": True}

    async def close(self) -> None:
        self.closed = True
        # Include waiting for the guard: a cancellation before disconnect has
        # acquired it must not release the lease while a request owns the link.
        try:
            await wait_cleanup(asyncio.create_task(self.disconnect()))
        finally:
            self.lease.release()
            self.owned = False
