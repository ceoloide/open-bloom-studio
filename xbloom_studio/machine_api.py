"""JSON-facing machine controls through the application connection owner.

The allowlist is deliberately narrower than :class:`~xbloom_studio.Interaction`:
operation launchers and calibration stay out. Recipe execution always uses
recorded operation scheduling.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .command_schema import command_schema, validate_command_params
from .control import BusyError, EncodedRecipe, Interaction
from .machine_controls import (
    EncodedTeaRecipe,
    MachineControls,
    PresetWriteError,
    SavedPreset,
)
from .storage import ConflictError

if TYPE_CHECKING:
    from .journal import Recorder
    from .storage import Store

COMMANDS = {
    "set_brightness": "Set display brightness, integer 1..15.",
    "set_weight_unit": "Set display unit: ml, g or oz.",
    "set_temperature_unit": "Set display unit: C or F.",
    "set_pour_pattern": "Set center/ring/spiral after entering manual brewer; "
    "no dispense.",
    "back_home": "Return home; may wake the display.",
    "wake": "Use the home route and report observed wake status.",
    "set_mode": "Switch pro/recipes.",
    "write_preset": "Experimental single-slot transaction; supplied backup is not "
    "readback.",
    "write_presets": "Write A/B/C once each; requires caller-supplied backups.",
    "set_water_source": "Select tank/tap source by the existing validated argument "
    "contract.",
    "read_pour_radius": "Query current raw value; also prepares/home the machine.",
    "read_vibration": "Query current raw value; also prepares/home the machine.",
    "initialize_pour_radius": "Initialize radius baseline, not a passive query.",
    "set_pour_radius": "Set raw radius.",
    "set_pour_radius_level": "Set app level 0..4 with app baseline clamping.",
    "initialize_vibration": "Initialize raw vibration baseline.",
    "set_vibration_level": "Set app vibration level 0..5.",
    "set_cup_limits": "Set recipe cup-reference limits, not heater temperatures.",
    "configure_recipe": "Set dose/bypass/cup metadata; does not upload content.",
    "set_preset_count": "Experimental count; acknowledgment is not established.",
    "set_preset_order": "Experimental order; acknowledgment is not established.",
    "quit_recipe_preparation": "Leave native recipe preparation; not a native "
    "stage advance.",
    "back_to_normal": "Empty 40526 housekeeping; not a grinder-position query.",
    "configure_grinder": "Enter/configure/exit manual grinder without starting "
    "rotation.",
    "configure_brewer": "Enter/configure/exit manual brewer without dispensing.",
    "enter_scale": "Open an owned standalone scale session.",
    "tare": "Tare the owned scale; not scale calibration.",
    "exit_scale": "Close the owned standalone scale session.",
}
BACKUP_COMMANDS = ("write_preset", "write_presets", "set_preset_order")


def _signature(name: str) -> str:
    """The legacy unannotated parameter list, without ``self`` and backup path."""
    signature = inspect.signature(getattr(Interaction, name))
    return str(
        signature.replace(
            parameters=[
                value.replace(annotation=inspect.Parameter.empty)
                for key, value in signature.parameters.items()
                if key not in ("self", "backup_path")
            ],
            return_annotation=inspect.Signature.empty,
        )
    )


def command_capabilities() -> dict[str, Any]:
    """Offline descriptions. Command validation remains in the low-level API."""
    return {
        "commands": {
            name: {
                "description": description,
                "requires_allow_control": True,
                "requires_allow_untested": name in MachineControls.UNTESTED_METHODS,
                "parameters": _signature(name),
                "conditional_experimental": ["bypass_ml>0", "cup_limits"]
                if name == "configure_recipe"
                else [],
                "input_schema": command_schema(name),
            }
            for name, description in COMMANDS.items()
        },
        "preset_backup": "Service-generated private file; caller supplies previous "
        "contents, never machine readback.",
        "prepared_recipe": "Single native coffee/tea step; session-local single-use "
        "token, no automatic expiry or retry.",
        "native_stage_advance": False,
        "calibration_exposed": False,
        "grinder_upload": {
            "field": "set_grinder_size",
            "legacy_alias": "grinder_enabled",
            "true_upload": 8001,
            "false_upload": 8004,
            "actual_grinding": "not_established_by_upload_option",
        },
    }


def _preset(value: Any) -> SavedPreset:
    if not isinstance(value, dict) or set(value) != {"slot", "payload_hex", "flags"}:
        raise ValueError("Preset needs exactly slot, payload_hex and flags")
    if not isinstance(value["payload_hex"], str):
        raise ValueError("Preset payload_hex must be a string")
    return SavedPreset(
        value["slot"], bytes.fromhex(value["payload_hex"]), value["flags"]
    )


def command_parameters(name: str, params: Any) -> dict[str, Any]:
    """Convert only named JSON representations, rejecting paths and extra fields."""
    if name not in COMMANDS:
        raise ValueError("Unknown machine command; inspect machine_capabilities")
    if not isinstance(params, dict):
        raise ValueError("Command params must be an object")
    validate_command_params(name, params)
    converted = deepcopy(params)
    if "backup_path" in converted:
        raise ValueError("The service chooses the preset backup path")
    if name == "write_preset":
        for key in ("preset", "backup"):
            converted[key] = _preset(converted[key])
    if name == "write_presets":
        for key in ("presets", "backup"):
            converted[key] = [_preset(value) for value in converted[key]]
    if name == "configure_recipe" and converted.get("cup_limits") is not None:
        converted["cup_limits"] = tuple(converted["cup_limits"])
    if name in BACKUP_COMMANDS:
        converted["backup_path"] = None  # Required argument; filled by the service.
    inspect.signature(getattr(Interaction, name)).bind(None, **converted)
    return converted


def same_request(first: Any, second: Any) -> bool:
    """Whether two JSON requests name the same attempt.

    Numbers compare by value, so ``50`` and ``50.0`` from different adapters
    are one request; booleans never equal numbers, and everything else must
    match exactly.
    """
    if isinstance(first, bool) or isinstance(second, bool):
        return type(first) is type(second) and first == second
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        return first == second
    if isinstance(first, dict) and isinstance(second, dict):
        return first.keys() == second.keys() and all(
            same_request(first[key], second[key]) for key in first
        )
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(
            same_request(a, b) for a, b in zip(first, second, strict=True)
        )
    return type(first) is type(second) and bool(first == second)


def encoded_step(step: dict[str, Any]) -> EncodedRecipe | EncodedTeaRecipe:
    """Rebuild the retained machine content of one native coffee or tea step."""
    limits = step.get("cup_limits")
    if step["kind"] == "tea":
        return EncodedTeaRecipe(
            bytes.fromhex(step["payload_hex"]),
            tuple(limits) if limits is not None else None,
        )
    return EncodedRecipe(
        bytes.fromhex(step["payload_hex"]),
        step["dose_g"],
        grinder_enabled=step.get("grinder_enabled", False),
        cup_limits=tuple(limits) if limits is not None else None,
        bypass_ml=step.get("bypass_ml", 0),
        bypass_temperature_c=step.get("bypass_temperature_c", 20),
    )


class MachineCommandAPI:
    """Mixed into MachineService; uses its guard, recorder and retained session."""

    store: Store
    device: str | None
    guard: asyncio.Lock
    closed: bool
    disconnecting: bool
    stopping: bool
    task: asyncio.Task[None] | None
    operation_id: str | None
    projection: dict[str, Any] | None
    worker_started: asyncio.Event | None
    prepared: dict[str, Any] | None
    _prepared_plan: dict[str, Any] | None

    if TYPE_CHECKING:

        @property
        def _session(self) -> Interaction: ...

        @property
        def _recorder(self) -> Recorder: ...

        async def _own(self) -> None: ...

        async def _connect(self, allow_untested: bool) -> None: ...

        def prepare(self, request: Any) -> dict[str, Any]: ...

        async def _execute(
            self,
            ident: str,
            plan: dict[str, Any],
            prepared_token: str | None = None,
        ) -> None: ...

    def capabilities(self) -> dict[str, Any]:
        return command_capabilities()

    def _reject_queued_write(self) -> None:
        if self.guard.locked():
            raise BusyError("Machine is handling another request; nothing queued")

    def _require_unoccupied(self) -> None:
        if self.closed or self.disconnecting:
            raise RuntimeError("Machine service is closed or disconnecting")
        if self.task is not None and not self.task.done():
            raise BusyError("Machine already has an operation; nothing queued")
        if self.prepared is not None:
            raise BusyError(
                "A recipe owns preparation; start, cancel or disconnect first"
            )

    async def command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        allow_control: bool = False,
        *,
        allow_untested: bool = False,
    ) -> dict[str, Any]:
        if allow_control is not True:
            raise ValueError("Machine commands require allow_control")
        if type(allow_untested) is not bool:
            raise ValueError("allow_untested must be a boolean")
        values = command_parameters(command, {} if params is None else params)
        if command in MachineControls.UNTESTED_METHODS and not allow_untested:
            raise ValueError("Command requires allow_untested")
        self._reject_queued_write()
        async with self.guard:
            self._require_unoccupied()
            await self._connect(allow_untested)
            recorder, session = self._recorder, self._session
            recorder.emit(
                "machine.command_requested",
                {"command": command, "params": params or {}},
            )
            await recorder.flush()
            if "backup_path" in values:
                folder = Path(self.store.path).parent / "preset-backups"
                folder.mkdir(mode=0o700, exist_ok=True)
                values["backup_path"] = folder / (uuid.uuid4().hex + ".json")
            try:
                with recorder.capturing():
                    result: dict[str, Any] = await getattr(session, command)(**values)
            except PresetWriteError as exc:
                result = {
                    "status": "failed",
                    "error": str(exc),
                    "type": type(exc).__name__,
                    "details": deepcopy(exc.result),
                    "recovery_required": session._uncertain,
                }
            except BaseException as exc:
                recorder.emit(
                    "machine.command_failed",
                    {"command": command, "type": type(exc).__name__, "error": str(exc)},
                )
                raise
            recorder.emit(
                "machine.command_finished", {"command": command, "result": result}
            )
            await recorder.flush()
            return result

    async def upload(
        self,
        request: Any,
        allow_control: bool = False,
        *,
        allow_untested: bool = False,
        expected_recipe_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Pin and upload one native step. Does not send an execute command."""
        if allow_control is not True:
            raise ValueError("Upload requires allow_control")
        if type(allow_untested) is not bool:
            raise ValueError("allow_untested must be a boolean")
        self._reject_queued_write()
        async with self.guard:
            self._require_unoccupied()
            plan = await asyncio.to_thread(self.prepare, request)
            if len(plan["execution"]) != 1 or plan["execution"][0]["kind"] not in (
                "native",
                "tea",
            ):
                raise ValueError(
                    "Upload needs exactly one native coffee or tea execution step"
                )
            if (
                expected_recipe_sha256 is not None
                and plan["recipe_sha256"] != expected_recipe_sha256
            ):
                raise ConflictError("Prepared recipe changed; preview it again")
            if plan["requires_untested"] and not allow_untested:
                raise ValueError("Recipe requires allow_untested")
            plan.pop("resolved_request", None)
            step = plan["execution"][0]
            encoded = encoded_step(step)
            await self._connect(allow_untested)
            recorder, session = self._recorder, self._session
            recorder.emit("recipe.upload_requested", {"request": request, "plan": plan})
            await recorder.flush()
            # The service guard covers upload and reservation; no caller slips in.
            try:
                with recorder.capturing():
                    if isinstance(encoded, EncodedRecipe):
                        result = await session.upload_recipe(encoded)
                    else:
                        result = await session.upload_tea(encoded)
                self.prepared = {
                    **result,
                    "recipe_sha256": plan["recipe_sha256"],
                    "readback_verified": False,
                    "request": deepcopy(request),
                }
                self._prepared_plan = deepcopy(plan)
                recorder.emit("recipe.uploaded", self.prepared)
                await recorder.flush()
                return deepcopy(self.prepared)
            except BaseException as exc:
                session._uncertain = True
                recorder.emit(
                    "recipe.upload_failed",
                    {
                        "type": type(exc).__name__,
                        "error": str(exc),
                        "recovery_required": True,
                    },
                )
                raise

    async def start_uploaded(
        self, token: str, idempotency_key: str, allow_control: bool = False
    ) -> dict[str, Any]:
        """Start the pinned upload once. Replayed keys retrieve its recorded attempt."""
        if allow_control is not True:
            raise ValueError("Starting requires allow_control")
        if not isinstance(token, str) or not token:
            raise ValueError("A preparation token is required")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("A nonempty idempotency key is required")
        request = {"kind": "uploaded", "token": token}
        self._reject_queued_write()
        async with self.guard:
            await self._own()
            row = await asyncio.to_thread(self.store.operation_by_key, idempotency_key)
            if row:
                self._same_attempt(row, request)
                return row
            if self.task is not None and not self.task.done():
                raise BusyError("Machine already has an operation; nothing queued")
            if self.prepared is None or self.prepared["token"] != token:
                raise ConflictError("No matching prepared recipe; upload explicitly")
            # The Interaction revalidates token, session and context before execute.
            live = self._session.get_state().get("prepared") or {}
            if live.get("token") != token:
                raise ConflictError(
                    "Machine preparation is no longer valid; disconnect or cancel"
                )
            await self._recorder.flush()
            plan = deepcopy(self._prepared_plan) or {}
            return await self._accept_and_launch(
                request, plan, idempotency_key, prepared_token=token
            )

    async def cancel_uploaded(
        self, token: str, allow_control: bool = False
    ) -> dict[str, Any]:
        if allow_control is not True:
            raise ValueError("Canceling requires allow_control")
        async with self.guard:
            if self.prepared is None or self.prepared["token"] != token:
                raise ConflictError("No matching prepared recipe")
            recorder = self._recorder
            with recorder.capturing():
                result = await self._session.cancel_prepared(token)
            self.prepared = None
            self._prepared_plan = None
            recorder.emit("recipe.upload_cancelled", result)
            await recorder.flush()
            return result

    def _same_attempt(self, row: dict[str, Any], request: Any) -> None:
        """A replayed key must name the identical request on this machine."""
        if not same_request(row["request"], request) or not self._owns_device(
            row["device"]
        ):
            raise ConflictError("Idempotency key belongs to another request")

    def _owns_device(self, recorded: object) -> bool:
        from .connector import normalize_identity

        try:
            return self.device is not None and normalize_identity(recorded) == (
                self.device
            )
        except ValueError:
            return False

    async def _accept_and_launch(
        self,
        request: Any,
        plan: dict[str, Any],
        idempotency_key: str,
        prepared_token: str | None = None,
    ) -> dict[str, Any]:
        """Finish the durable acceptance boundary even if its caller is cancelled.

        Once committed, a daemon operation outlives delivery of the response. A
        retry with the same key retrieves that attempt; it cannot start it
        twice. The caller holds the guard across this boundary and scheduling.
        """
        accepted = asyncio.ensure_future(
            asyncio.to_thread(
                self.store.create_operation,
                str(self.device),
                request,
                plan,
                idempotency_key,
            )
        )
        cancelled = False
        while True:
            try:
                op, created = await asyncio.shield(accepted)
                break
            except asyncio.CancelledError:
                if accepted.cancelled():
                    raise
                cancelled = True
        if created:
            if prepared_token is not None:
                self.prepared = None
                self._prepared_plan = None
            self.stopping = False
            self.operation_id = op["id"]
            self.projection = deepcopy(op)
            self._recorder.operation_id = op["id"]
            self.worker_started = asyncio.Event()
            self.task = asyncio.create_task(
                self._execute(op["id"], plan, prepared_token=prepared_token)
            )
        if cancelled:
            raise asyncio.CancelledError()
        return op
