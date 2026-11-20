"""Explicit platform-neutral API registry shared by Python, CLI, RPC and MCP.

:meth:`Application.call` is the only dispatch path: every adapter names a
method from a fixed allowlist, and arguments bind against that method's
signature before anything runs. Nothing else on the application is
reachable by name, and no method accepts a file path chosen by a caller
except the local-only ``history_backup`` destination.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .application import MachineService
from .coffee_guidance import recipe_examples
from .connector import Connector
from .providers import HubProviders, RecipeImports
from .recipe_validation import check_recipe, recipe_constraints
from .recipes import RecipeService, compile_recipe
from .tea_guidance import recipe_tea_references
from .tea_validation import validate_tea_recipe
from .templates import recipe_templates

if TYPE_CHECKING:
    from .storage import Store


def bundled_examples(
    coffee: dict[str, Any], water_ml: float | None = None, limit: int = 5
) -> dict[str, Any]:
    """Bundled examples only: callers never choose a file to read."""
    return recipe_examples(coffee, water_ml, limit)


def bundled_tea_references(family: str | None = None) -> dict[str, Any]:
    """Bundled references only: callers never choose a file to read."""
    return recipe_tea_references(family)


PURE_METHODS: dict[str, tuple[Callable[..., Any], bool]] = {
    "recipe_validate": (check_recipe, True),
    "recipe_constraints": (recipe_constraints, False),
    "recipe_templates": (recipe_templates, True),
    "recipe_examples": (bundled_examples, True),
    "recipe_tea_references": (bundled_tea_references, True),
    "recipe_tea_validate": (validate_tea_recipe, True),
    "recipe_preview": (compile_recipe, True),
}


def bind_parameters(
    fn: Callable[..., Any], params: dict[str, Any] | None
) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("Parameters must be an object")
    arguments = params or {}
    inspect.signature(fn).bind(**arguments)
    return arguments


class Application:
    """Recipes, history and one machine owner behind one method allowlist."""

    def __init__(
        self,
        store: Store,
        device: object = None,
        machine: MachineService | None = None,
        providers: HubProviders | None = None,
        *,
        connector: Connector | None = None,
        capture_idle_telemetry: bool = False,
        interaction_options: dict[str, Any] | None = None,
    ) -> None:
        if machine is not None and (
            device is not None or connector is not None or interaction_options
        ):
            raise ValueError("Configure the machine service or pass one, not both")
        self.store = store
        self.machine = machine or MachineService(
            store,
            device,
            interaction_options=interaction_options,
            connector=connector,
            capture_idle_telemetry=capture_idle_telemetry,
        )
        self.recipes = RecipeService(store)
        self.providers = providers or HubProviders()
        self.imports = RecipeImports(store, self.providers)
        machine_api = self.machine
        # (callable, run blocking I/O off-loop). No arbitrary method dispatch.
        self.methods: dict[str, tuple[Callable[..., Any], bool]] = {
            **PURE_METHODS,
            "service_info": (self.service_info, False),
            "recipe_create": (store.create_recipe, True),
            "recipe_get": (store.get_recipe, True),
            "recipe_list": (store.recipes, True),
            "recipe_update": (store.update_recipe, True),
            "recipe_page": (store.recipe_page, True),
            "recipe_archive": (store.delete_recipe, True),
            "recipe_alias": (store.alias, True),
            "recipe_compile": (self.recipes.compile, True),
            "recipe_compare": (self.recipes.compare, True),
            "recipe_import_export": (self.imports.import_export, True),
            "recipe_match": (self.recipes.match, True),
            "recipe_import": (self.imports.import_recipe, True),
            "recipe_import_get": (store.get_import, True),
            "recipe_refresh": (self.imports.refresh_recipe, True),
            "provider_search": (self.providers.search, True),
            "provider_capabilities": (self.providers.capabilities, True),
            "provider_write": (self.providers.online_write, True),
            "operation_lookup": (store.operation_lookup, True),
            "history_list": (store.history, True),
            "history_get": (store.operation, True),
            "history_page": (store.history_page, True),
            "history_event_page": (store.event_page, True),
            "history_events": (store.events, True),
            "history_feedback": (store.feedback, True),
            "history_save_recipe": (self.recipes.from_brew, True),
            "history_backup": (store.backup, True),
            "machine_capabilities": (machine_api.capabilities, False),
            "machine_command": (machine_api.command, False),
            "recipe_upload": (machine_api.upload, False),
            "recipe_start_uploaded": (machine_api.start_uploaded, False),
            "recipe_cancel_uploaded": (machine_api.cancel_uploaded, False),
            "machine_state": (machine_api.state, False),
            "machine_prepare": (machine_api.prepare, True),
            "machine_connect": (machine_api.connect, False),
            "machine_disconnect": (machine_api.disconnect, False),
            "operation_start": (machine_api.start, False),
            "operation_stop": (machine_api.stop, False),
            "operation_pause_resume": (machine_api.pause_resume, False),
            "operation_acknowledge": (machine_api.acknowledge, False),
        }

    def service_info(self) -> dict[str, Any]:
        """The served machine identity and database; clients verify both."""
        return {"device": self.machine.device, "database": str(self.store.path)}

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not isinstance(method, str) or method not in self.methods:
            raise ValueError("Unknown application method")
        fn, blocking = self.methods[method]
        arguments = bind_parameters(fn, params)
        if blocking:
            return await asyncio.to_thread(fn, **arguments)
        result = fn(**arguments)
        return await result if inspect.isawaitable(result) else result

    async def close(self) -> None:
        await self.machine.close()
