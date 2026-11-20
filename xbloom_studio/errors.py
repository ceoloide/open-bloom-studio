"""Safe application and RPC error contracts, independent of the optional MCP SDK.

Known domain errors keep their message and recovery guidance; anything else
becomes an ``internal_error`` with a correlation ID and no internal detail.
No guidance ever suggests automatically repeating a physical action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any
from urllib.error import HTTPError, URLError

from .client import ProtocolCommandError, SessionError
from .command_schema import CommandValidationError
from .control import BusyError
from .machine_controls import PresetWriteError, UntestedOperationError
from .ownership import LeaseBusyError, LeaseDirectoryError
from .providers import ProviderError
from .recipe_validation import RecipeValidationError
from .rpc_types import RemoteError, SocketDirectoryError, SocketPathError
from .storage import ConflictError

CODES = frozenset(
    {
        "validation_error",
        "not_found",
        "conflict",
        "busy",
        "experimental_required",
        "invalid_argument",
        "connection_unavailable",
        "machine_protocol_error",
        "preset_write_incomplete",
        "permission_required",
        "internal_error",
        "provider_error",
        "provider_unavailable",
        "response_too_large",
    }
)
FIELDS = (
    "type",
    "code",
    "message",
    "details",
    "next_step",
    "retryable",
    "correlation_id",
)
NOT_FOUND = frozenset(
    {
        "Recipe not found",
        "Recipe revision not found",
        "Operation not found",
        "Import not found",
        "Source import not found",
    }
)


def _detail(
    name: str,
    code: str,
    message: str,
    next_step: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": name,
        "code": code,
        "message": message,
        "details": details or {},
        "next_step": next_step,
        "retryable": False,
    }


def _internal() -> dict[str, Any]:
    return {
        **_detail(
            "InternalError",
            "internal_error",
            "An internal error prevented a reliable response.",
            "Check service logs with the correlation ID and inspect history before "
            "retrying any machine action.",
        ),
        "correlation_id": uuid.uuid4().hex,
    }


def error_detail(exc: BaseException, *, log_unexpected: bool = True) -> dict[str, Any]:
    """Return known domain errors with recovery guidance; hide unexpected internals.

    Preset backup identity and progress are deliberately retained. No retry
    advice can turn an uncertain machine action into another physical attempt.
    """
    if isinstance(exc, RemoteError):
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("code") in CODES:
            # The serving side already sanitized this envelope; drop unknown keys.
            return {k: v for k, v in detail.items() if k in FIELDS}
        return _internal()
    name = type(exc).__name__
    if isinstance(exc, (RecipeValidationError, CommandValidationError)):
        return _detail(
            name,
            "validation_error",
            str(exc),
            "Correct the indicated JSON Pointer fields using their allowed values; "
            "preview again.",
            {"validation": exc.report},
        )
    if isinstance(exc, PresetWriteError):
        return _detail(
            name,
            "preset_write_incomplete",
            "Preset write did not finish; stored contents are unverified.",
            "Inspect backup and acknowledged progress; disconnect and inspect the "
            "machine before recovery. Do not retry or roll back automatically.",
            {"preset_write": exc.result},
        )
    if isinstance(exc, UntestedOperationError):
        return _detail(
            name,
            "experimental_required",
            str(exc),
            "Only set allow_untested=true if experimental behavior is explicitly "
            "intended.",
        )
    if isinstance(exc, ProtocolCommandError):
        return _detail(
            name,
            "machine_protocol_error",
            "Machine reported a protocol error; command effects may be uncertain.",
            "Inspect machine_state/history and the physical machine. Reuse an "
            "existing idempotency key to inspect its attempt; never force a new "
            "start.",
            {"protocol": exc.details},
        )
    if isinstance(exc, LeaseBusyError):
        return _detail(
            name,
            "busy",
            str(exc),
            "Use the existing owner or stop it cleanly before starting a new service.",
        )
    if isinstance(exc, LeaseDirectoryError):
        return _detail(
            name,
            "permission_required",
            str(exc),
            "Check ownership and mode 0700 for the lease directory under "
            "the account's home. Ensure the daemon user can write there.",
        )
    if isinstance(exc, SocketDirectoryError):
        return _detail(
            name,
            "permission_required",
            str(exc),
            "Select a private socket directory owned by the daemon user (mode 0700).",
        )
    if isinstance(exc, SocketPathError):
        return _detail(
            name,
            "invalid_argument",
            str(exc),
            "Use a shorter --socket path in a private directory for the daemon "
            "and clients.",
        )
    if isinstance(exc, BusyError):
        return _detail(
            name,
            "busy",
            str(exc),
            "Inspect machine_state; finish, stop or cancel the matching owned work "
            "before another command.",
        )
    if isinstance(exc, ConflictError):
        return _detail(
            name,
            "conflict",
            str(exc),
            "Use a recipe ID to resolve ambiguous names, reload changed revisions, "
            "and preserve keys for the same intended attempt.",
        )
    if isinstance(exc, KeyError) and exc.args and exc.args[0] in NOT_FOUND:
        return _detail(
            name,
            "not_found",
            exc.args[0],
            "Search recipe_list/history_list or retained imports, then use the "
            "exact ID and revision.",
        )
    if isinstance(exc, ProviderError):
        return _detail(
            name,
            "provider_error",
            str(exc),
            "Check provider_capabilities and the supported public recipe URL; "
            "retain blocked source data rather than guessing a conversion.",
        )
    if isinstance(exc, HTTPError):
        return _detail(
            name,
            "provider_error",
            "The public recipe provider rejected the request.",
            "Check the public URL and provider availability; no credentials or "
            "machine action were retried.",
            {"http_status": exc.code},
        )
    if isinstance(exc, URLError):
        return _detail(
            name,
            "provider_unavailable",
            "The public recipe provider could not be reached.",
            "Check network/provider availability; local recipes and cached history "
            "remain available.",
        )
    if isinstance(exc, SessionError):
        return _detail(
            name,
            "connection_unavailable",
            str(exc),
            "Inspect cached state and the machine; explicitly disconnect/reconnect "
            "only when appropriate. Do not replay a start automatically.",
        )
    if isinstance(exc, FileExistsError):
        return _detail(
            name,
            "conflict",
            "A local socket or destination already exists.",
            "Inspect its owner/destination. For an abandoned daemon socket use "
            "xbloom serve --recover-stale-socket; never remove an active socket.",
        )
    if isinstance(exc, (FileNotFoundError, ConnectionRefusedError)):
        return _detail(
            name,
            "connection_unavailable",
            "The configured local daemon is unavailable.",
            "Start or check xbloom serve and its configured socket. Do not open a "
            "competing Bluetooth connection.",
        )
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return _detail(
            name,
            "connection_unavailable",
            "The daemon or transport did not complete the request.",
            "Check operation_lookup with the original idempotency key, "
            "machine_state and history. An accepted operation may continue. Do not "
            "automatically replay a start: an absent key can still start if the "
            "original was never accepted.",
        )
    if isinstance(exc, json.JSONDecodeError):
        return _detail(
            name,
            "invalid_argument",
            "Invalid JSON request.",
            "Correct the JSON syntax and retry the same read or inspect the "
            "existing operation key.",
        )
    if type(exc) in (ValueError, TypeError):
        message = str(exc)
        code = (
            "permission_required"
            if "allow_control" in message
            else "experimental_required"
            if "allow_untested" in message
            else "invalid_argument"
        )
        return _detail(
            name,
            code,
            message,
            "Check tool parameter choices/bounds and machine_capabilities; use "
            "recipe_validate for full recipe diagnostics.",
        )
    detail = _internal()
    if log_unexpected:
        logging.getLogger(__name__).error(
            "Unexpected application error; correlation_id=%s",
            detail["correlation_id"],
            exc_info=exc,
        )
    return detail
