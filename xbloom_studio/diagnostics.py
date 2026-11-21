"""Canonical diagnostics catalog, safe excerpts and plain human stderr display.

Every diagnostic carries a stable bracketed code, a plain cause, an optional
truthful location, a concrete fix, a canonical per-code documentation link and
an agent repair guide link. No fix or repair link ever suggests automatically
retrying an uncertain physical machine action.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DOCS_LOCAL_PATH = "docs/diagnostics.md"
DOCS_BASE_URL = (
    "https://github.com/ceoloide/open-xbloom-studio-tools/blob/main/" + DOCS_LOCAL_PATH
)

MAX_JSON_LOCATE_BYTES = 256 * 1024
MAX_JSON_LOCATE_DEPTH = 100
MAX_EXCERPT_WIDTH = 120
MAX_SUMMARY_WIDTH = 220
MAX_SANITISED_TEXT = 512

# Sensitive keys and secret-shaped tokens are redacted from local JSON excerpts.
_SENSITIVE_LINE = re.compile(
    r"(?i)(?:"
    r"\"[^\"]*(?:token|secret|password|passwd|api[_-]?key|authorization|"
    r"cookie|private[_-]?key|credential|bearer|oauth|jwt|firebaseauth|"
    r"signature)[^\"]*\"\s*:"
    r"|-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"|\b(?:gh[opsur]_[A-Za-z0-9]{16,}|github_pat_\w{20,})"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bpypi-AgEI[A-Za-z0-9_-]{20,}"
    r"|\bAIza[0-9A-Za-z_-]{20,}"
    r")"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_JSON_TOKEN = re.compile(
    r"""
    (?P<ws>[ \t\r\n]+)
    |(?P<string>"(?:\\.|[^"\\])*")
    |(?P<punct>[{}\[\]:,])
    |(?P<atom>true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)
    """,
    re.VERBOSE,
)

LOCATION_KEYS = frozenset(
    {
        "kind",
        "command",
        "method",
        "tool",
        "parameter",
        "pointer",
        "path",
        "line",
        "column",
        "slot",
        "operation_id",
    }
)
REPAIR_GUIDE_KEYS = frozenset({"local", "url", "skill"})
CONTEXT_KEYS = frozenset({"summary", "excerpt"})
EXCERPT_LINE_KEYS = frozenset({"line", "text", "highlight", "column"})
DIAGNOSTIC_KEYS = frozenset(
    {"code", "cause", "location", "fix", "docs_url", "repair_guide", "context"}
)


class InputFileReadError(ValueError):
    """A local CLI JSON input file could not be read or decoded as UTF-8."""

    def __init__(self, message: str, *, display_path: str) -> None:
        super().__init__(message)
        self.display_path = display_path


class LocalJsonSyntaxError(json.JSONDecodeError):
    """A local CLI JSON input file contained invalid JSON syntax."""

    def __init__(self, msg: str, doc: str, pos: int, *, display_path: str) -> None:
        super().__init__(msg, doc, pos)
        self.display_path = display_path


@dataclass(frozen=True)
class DiagnosticContext:
    """Truthful invocation context attached by CLI, RPC or MCP adapters."""

    command: str | None = None
    method: str | None = None
    tool: str | None = None
    parameter: str | None = None
    pointer: str | None = None
    source_path: str | None = None
    source_text: str | None = None
    allow_excerpt: bool = False


@dataclass(frozen=True)
class DiagnosticSpec:
    """Static metadata for one canonical diagnostic code."""

    code: str
    error_code: str
    title: str
    default_cause: str
    default_fix: str
    skill: str | None = None

    @property
    def anchor(self) -> str:
        return self.code.lower()

    @property
    def docs_url(self) -> str:
        return f"{DOCS_BASE_URL}#{self.anchor}"

    def repair_guide(self) -> dict[str, str]:
        guide: dict[str, str] = {
            "local": f"{DOCS_LOCAL_PATH}#{self.anchor}-agent-repair",
            "url": f"{DOCS_BASE_URL}#{self.anchor}-agent-repair",
        }
        if self.skill is not None:
            guide["skill"] = self.skill
        return guide


SPECS: tuple[DiagnosticSpec, ...] = (
    DiagnosticSpec(
        code="XB_RECIPE_VALIDATION",
        error_code="validation_error",
        title="Recipe validation failed",
        default_cause="The recipe document failed authoring validation.",
        default_fix=(
            "Fix the indicated JSON Pointer fields using the allowed bounds from "
            "`recipe_constraints` (`xbloom-studio recipe constraints`), then "
            "validate and preview again."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#draft-and-validate",
    ),
    DiagnosticSpec(
        code="XB_COMMAND_VALIDATION",
        error_code="validation_error",
        title="Machine command parameters failed validation",
        default_cause="The machine command parameters do not match the command schema.",
        default_fix=(
            "Inspect `machine_capabilities` (`xbloom-studio machine capabilities`) "
            "for this command's input schema, correct the indicated JSON Pointer "
            "fields, and resubmit."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#settings-and-scale",
    ),
    DiagnosticSpec(
        code="XB_JSON_SYNTAX",
        error_code="invalid_argument",
        title="Invalid JSON syntax",
        default_cause="The JSON input could not be parsed.",
        default_fix=(
            "Fix the JSON syntax at the reported line and column, then re-run "
            "the command. After any uncertain machine action, inspect history "
            "with the existing idempotency key instead of starting again."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#draft-and-validate",
    ),
    DiagnosticSpec(
        code="XB_INPUT_READ_FAILED",
        error_code="invalid_argument",
        title="Unable to read local JSON input file",
        default_cause="The local JSON file could not be read or decoded as UTF-8.",
        default_fix=(
            "Verify that the path exists, points to a readable UTF-8 JSON file "
            "(not a directory), and try again."
        ),
    ),
    DiagnosticSpec(
        code="XB_SOCKET_PATH_TOO_LONG",
        error_code="invalid_argument",
        title="Daemon Unix socket path exceeds platform limit",
        default_cause="The configured Unix socket path is longer than the OS allows.",
        default_fix=(
            "Pass a shorter `--socket` path inside a private 0700 directory for "
            "both the daemon and clients."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_INVALID_ARGUMENT",
        error_code="invalid_argument",
        title="Invalid command option or parameter",
        default_cause="A command option, method name, or argument value was rejected.",
        default_fix=(
            "Check the command usage, parameter bounds and `machine_capabilities` "
            "(or `recipe_validate` for recipes), then correct the invalid argument."
        ),
    ),
    DiagnosticSpec(
        code="XB_RECIPE_NOT_FOUND",
        error_code="not_found",
        title="Recipe or revision not found",
        default_cause="No saved recipe or requested revision matched the reference.",
        default_fix=(
            "List saved recipes with `xbloom-studio recipe list "
            "--include-temporary` (or `recipe_list(include_temporary=true)`), "
            "then pass the exact recipe ID, alias, or unambiguous name and revision."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#store-and-find",
    ),
    DiagnosticSpec(
        code="XB_OPERATION_NOT_FOUND",
        error_code="not_found",
        title="Operation not found",
        default_cause="No recorded operation matched the requested operation ID.",
        default_fix=(
            "List recorded attempts with `xbloom-studio history list --page` "
            "(or `history_list`) or look up by key with `operation_lookup`, "
            "then pass the exact durable operation ID."
        ),
        skill="skills/xbloom-studio-history/SKILL.md#find-the-brew",
    ),
    DiagnosticSpec(
        code="XB_IMPORT_NOT_FOUND",
        error_code="not_found",
        title="Retained source import not found",
        default_cause="No retained recipe source import matched the requested ID.",
        default_fix=(
            "Verify the retained import ID returned by `recipe_import` or "
            "`recipe_import_export` before calling `recipe_import_get`."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#import",
    ),
    DiagnosticSpec(
        code="XB_NOT_FOUND",
        error_code="not_found",
        title="Requested resource not found",
        default_cause=(
            "The requested recipe, operation, or retained import was not found."
        ),
        default_fix=(
            "Search `recipe_list`, `history_list`, or retained imports, then pass "
            "the exact ID and revision."
        ),
    ),
    DiagnosticSpec(
        code="XB_CONFLICT",
        error_code="conflict",
        title="State, revision, or idempotency conflict",
        default_cause="The request conflicted with current saved or machine state.",
        default_fix=(
            "Resolve ambiguous names with a recipe ID, reload changed revisions "
            "and previews before updating or starting, and keep idempotency keys "
            "bound to one exact attempt."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#store-and-find",
    ),
    DiagnosticSpec(
        code="XB_SOCKET_EXISTS",
        error_code="conflict",
        title="Daemon socket or destination already exists",
        default_cause="A local Unix socket or target file already exists at that path.",
        default_fix=(
            "Check whether an active daemon owns the socket. Only for an "
            "abandoned same-user socket, run `xbloom-studio serve "
            "--recover-stale-socket`; never delete an active socket."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_PRESET_WRITE_REFUSED",
        error_code="conflict",
        title="Preset write refused by journal policy",
        default_cause=(
            "The preset write policy refused this request before sending any frame."
        ),
        default_fix=(
            "Nothing was sent. Inspect `machine_preset_writes` (`xbloom-studio "
            "machine preset-writes`) for the slot's write ID, outcomes and exact "
            "contents; repeat only an identical write when `retry_allowed` is "
            "true, or ask the user before passing `acknowledge_incomplete`."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#settings-and-scale",
    ),
    DiagnosticSpec(
        code="XB_PRESET_WRITE_INCOMPLETE",
        error_code="preset_write_incomplete",
        title="Preset write incomplete; stored slot unverified",
        default_cause="The preset write did not confirm completion on the machine.",
        default_fix=(
            "Inspect the reported outcome, backup and progress, then disconnect "
            "and check `machine_preset_writes`. Never roll back automatically; "
            "resubmit only an identical single-slot write when `retry_allowed` "
            "is true."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#settings-and-scale",
    ),
    DiagnosticSpec(
        code="XB_MACHINE_BUSY",
        error_code="busy",
        title="Machine is busy with owned work",
        default_cause=(
            "Another operation, prepared upload, or lifecycle transition owns the "
            "machine; requests are never queued."
        ),
        default_fix=(
            "Inspect `machine_state` (`xbloom-studio machine state`); wait for, "
            "stop, or cancel the matching owned work before starting another "
            "command."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#start-watch-stop",
    ),
    DiagnosticSpec(
        code="XB_OWNER_LEASE_BUSY",
        error_code="busy",
        title="Database or machine owner lease is held",
        default_cause="Another daemon or direct session already owns this lease.",
        default_fix=(
            "Use the existing daemon over its socket or stop it cleanly before "
            "starting another daemon or direct command."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_CONTROL_PERMISSION_REQUIRED",
        error_code="permission_required",
        title="Explicit control permission required",
        default_cause="This physical machine action requires explicit control opt-in.",
        default_fix=(
            "Confirm the user explicitly asked for this physical action, then "
            "pass `--allow-control` (`allow_control=true`)."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#permission-comes-first",
    ),
    DiagnosticSpec(
        code="XB_DIRECTORY_PERMISSION_REQUIRED",
        error_code="permission_required",
        title="Private 0700 directory permission required",
        default_cause="The lease or socket directory is not private to this user.",
        default_fix=(
            "Ensure the lease or socket directory is owned by the daemon user, "
            "writable, and set to mode 0700."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_EXPERIMENTAL_REQUIRED",
        error_code="experimental_required",
        title="Explicit experimental opt-in required",
        default_cause=(
            "This route or command is not hardware-validated and requires "
            "experimental opt-in."
        ),
        default_fix=(
            "Check `machine_prepare` or `machine_capabilities`. Only pass "
            "`--allow-untested` (`allow_untested=true`) when the user explicitly "
            "accepts experimental behavior."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#permission-comes-first",
    ),
    DiagnosticSpec(
        code="XB_DAEMON_UNAVAILABLE",
        error_code="connection_unavailable",
        title="Configured local daemon is unavailable",
        default_cause="The local daemon socket is missing or refusing connections.",
        default_fix=(
            "Start or verify `xbloom-studio serve` and its `--socket` path. Do "
            "not open a competing Bluetooth connection."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_TRANSPORT_UNCERTAIN",
        error_code="connection_unavailable",
        title="Transport failed; delivery may be uncertain",
        default_cause=(
            "The daemon, SSH relay, or transport did not complete the request."
        ),
        default_fix=(
            "Run read-only `operation_lookup` with the original idempotency key "
            "and check `machine_state` and history. Never automatically replay "
            "a start."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_SESSION_UNAVAILABLE",
        error_code="connection_unavailable",
        title="Machine session unavailable or needs recovery",
        default_cause=(
            "No ready machine session is open, or the session requires recovery."
        ),
        default_fix=(
            "Inspect `machine_state` and the physical machine; explicitly "
            "disconnect and reconnect only when safe. Never replay a start "
            "automatically."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_MACHINE_PROTOCOL_ERROR",
        error_code="machine_protocol_error",
        title="Machine reported a protocol error",
        default_cause=(
            "The machine returned a protocol error; physical effects may be uncertain."
        ),
        default_fix=(
            "Inspect `machine_state`, `history_get`, and the physical machine. "
            "Reuse an existing idempotency key only to inspect its attempt; "
            "never force a new start."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_PROVIDER_ERROR",
        error_code="provider_error",
        title="Public recipe provider rejected or failed request",
        default_cause=(
            "The public recipe provider rejected the request or returned "
            "unsupported data."
        ),
        default_fix=(
            "Check `provider_capabilities` and the public URL; inspect any "
            "retained source import with `recipe_import_get` instead of guessing "
            "a conversion."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#import",
    ),
    DiagnosticSpec(
        code="XB_PROVIDER_UNAVAILABLE",
        error_code="provider_unavailable",
        title="Public recipe provider unreachable",
        default_cause=(
            "The public recipe provider could not be reached over the network."
        ),
        default_fix=(
            "Check network and provider availability; local recipes and cached "
            "history remain usable offline."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#import",
    ),
    DiagnosticSpec(
        code="XB_RESPONSE_TOO_LARGE",
        error_code="response_too_large",
        title="RPC response exceeded size limit",
        default_cause="The serialized RPC response exceeded the transport frame limit.",
        default_fix=(
            "Use cursor pagination (`--page` / `--limit` / `detail=compact`). "
            "If an operation start was accepted, inspect `history_get` with the "
            "reported `operation_id` and never start again with a new key."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_MCP_UNKNOWN_TOOL",
        error_code="unknown_tool",
        title="Unknown MCP tool in configured groups",
        default_cause="The requested MCP tool is not registered in this server.",
        default_fix=(
            "List the server's advertised tools or configure `xbloom-studio-mcp` "
            "with the required `--group` (`recipes`, `history`, or `machine`)."
        ),
    ),
    DiagnosticSpec(
        code="XB_MCP_OUTPUT_CONTRACT",
        error_code="output_contract_error",
        title="Unexpected application output shape",
        default_cause=(
            "The application returned an unexpected result shape; effects may "
            "already have occurred."
        ),
        default_fix=(
            "Inspect `history_get` with any reported `operation_id`, "
            "`operation_lookup` with the original key, and `machine_state`; "
            "check server logs using the correlation ID. Never force a new start."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_INTERNAL_ERROR",
        error_code="internal_error",
        title="Unexpected internal error",
        default_cause="An unexpected internal error prevented a reliable response.",
        default_fix=(
            "Check service logs with the correlation ID and inspect "
            "`operation_lookup`, `machine_state`, and history before retrying "
            "any machine action."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
)

CATALOG: dict[str, DiagnosticSpec] = {spec.code: spec for spec in SPECS}

FALLBACK_BY_ERROR_CODE: dict[str, str] = {
    "validation_error": "XB_RECIPE_VALIDATION",
    "not_found": "XB_NOT_FOUND",
    "conflict": "XB_CONFLICT",
    "busy": "XB_MACHINE_BUSY",
    "experimental_required": "XB_EXPERIMENTAL_REQUIRED",
    "invalid_argument": "XB_INVALID_ARGUMENT",
    "connection_unavailable": "XB_TRANSPORT_UNCERTAIN",
    "machine_protocol_error": "XB_MACHINE_PROTOCOL_ERROR",
    "preset_write_incomplete": "XB_PRESET_WRITE_INCOMPLETE",
    "permission_required": "XB_CONTROL_PERMISSION_REQUIRED",
    "internal_error": "XB_INTERNAL_ERROR",
    "provider_error": "XB_PROVIDER_ERROR",
    "provider_unavailable": "XB_PROVIDER_UNAVAILABLE",
    "response_too_large": "XB_RESPONSE_TOO_LARGE",
    "unknown_tool": "XB_MCP_UNKNOWN_TOOL",
    "output_contract_error": "XB_MCP_OUTPUT_CONTRACT",
}


def _truncate(text: str, limit: int) -> str:
    cleaned = _CONTROL_CHARS.sub(" ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _truncate_line(text: str, limit: int) -> str:
    """Sanitize a source excerpt line while preserving leading indentation."""
    cleaned = _CONTROL_CHARS.sub(" ", text).rstrip("\r\n")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _sanitize_excerpt_line(
    raw_line: str, column: int | None = None
) -> tuple[str, int | None, bool]:
    """Return bounded, redacted display text and adjusted 1-based column."""
    expanded = _CONTROL_CHARS.sub(" ", raw_line.rstrip("\r\n").replace("\t", "  "))
    if _SENSITIVE_LINE.search(expanded):
        return "<redacted>", None, True
    if len(expanded) <= MAX_EXCERPT_WIDTH:
        safe_col = (
            column if column is not None and 1 <= column <= len(expanded) + 1 else None
        )
        return expanded, safe_col, False
    if column is None:
        return expanded[: MAX_EXCERPT_WIDTH - 3] + "...", None, False
    # Center a narrow window around the syntax column on long single-line JSON.
    zero_col = max(0, min(column - 1, len(expanded)))
    half = (MAX_EXCERPT_WIDTH - 6) // 2
    start = max(0, zero_col - half)
    end = min(len(expanded), start + MAX_EXCERPT_WIDTH - 6)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(expanded) else ""
    snippet = prefix + expanded[start:end] + suffix
    adjusted = len(prefix) + (zero_col - start) + 1
    return snippet, adjusted, False


def extract_json_excerpt(
    text: str,
    line: int,
    column: int | None = None,
    *,
    radius: int = 1,
) -> list[dict[str, Any]] | None:
    """Return a minimal bounded source excerpt (1-3 lines) for a local JSON file."""
    if not text or line < 1 or len(text.encode("utf-8")) > MAX_JSON_LOCATE_BYTES:
        return None
    lines = text.splitlines()
    if line > len(lines):
        return None
    target_raw = lines[line - 1]
    if column is None and len(target_raw) > 200:
        return None
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    excerpt: list[dict[str, Any]] = []
    for number in range(start, end + 1):
        is_target = number == line
        cleaned, col, _ = _sanitize_excerpt_line(
            lines[number - 1], column if is_target else None
        )
        entry: dict[str, Any] = {
            "line": number,
            "text": cleaned,
            "highlight": is_target,
        }
        if is_target and col is not None:
            entry["column"] = col
        excerpt.append(entry)
    return excerpt


def _pointer_segments(pointer: str) -> list[str] | None:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        return None
    return [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    ]


def locate_json_pointer_line(text: str, pointer: str) -> tuple[int, int] | None:
    """Return exact 1-based (line, column) for an existing JSON Pointer in ``text``."""
    segments = _pointer_segments(pointer)
    if (
        segments is None
        or not text
        or len(text.encode("utf-8")) > MAX_JSON_LOCATE_BYTES
    ):
        return None
    tokens: list[tuple[str, str, int]] = []
    pos = 0
    for match in _JSON_TOKEN.finditer(text):
        if match.start() != pos:
            return None
        pos = match.end()
        kind = match.lastgroup
        if kind and kind != "ws":
            tokens.append((kind, match.group(), match.start()))
    if pos != len(text) or not tokens:
        return None

    def offset_to_line_col(offset: int) -> tuple[int, int]:
        line = text.count("\n", 0, offset) + 1
        last_nl = text.rfind("\n", 0, offset)
        return line, offset - last_nl

    index = 0
    found: int | None = None

    def parse_value(path: list[str], depth: int) -> bool:
        nonlocal index, found
        if depth > MAX_JSON_LOCATE_DEPTH or index >= len(tokens):
            return False
        kind, raw, start = tokens[index]
        if path == segments:
            found = start
        if kind == "string" or kind == "atom":
            index += 1
            return True
        if kind == "punct" and raw == "{":
            index += 1
            if index < len(tokens) and tokens[index][1] == "}":
                index += 1
                return True
            while index < len(tokens):
                if tokens[index][0] != "string":
                    return False
                try:
                    key = json.loads(tokens[index][1])
                except json.JSONDecodeError:
                    return False
                index += 1
                if index >= len(tokens) or tokens[index][1] != ":":
                    return False
                index += 1
                if not parse_value([*path, key], depth + 1):
                    return False
                if index >= len(tokens):
                    return False
                sep = tokens[index][1]
                index += 1
                if sep == "}":
                    return True
                if sep != ",":
                    return False
            return False
        if kind == "punct" and raw == "[":
            index += 1
            if index < len(tokens) and tokens[index][1] == "]":
                index += 1
                return True
            item = 0
            while index < len(tokens):
                if not parse_value([*path, str(item)], depth + 1):
                    return False
                item += 1
                if index >= len(tokens):
                    return False
                sep = tokens[index][1]
                index += 1
                if sep == "]":
                    return True
                if sep != ",":
                    return False
            return False
        return False

    if not parse_value([], 0) or index != len(tokens) or found is None:
        return None
    return offset_to_line_col(found)


def _format_expected(expected: Any) -> str | None:
    if not isinstance(expected, Mapping):
        return None
    parts: list[str] = []
    if "enum" in expected and isinstance(expected["enum"], Sequence):
        values = ", ".join(str(v) for v in expected["enum"][:8])
        if len(expected["enum"]) > 8:
            values += ", ..."
        parts.append(f"one of [{values}]")
    elif "minimum" in expected and "maximum" in expected:
        raw_kind = expected.get("type", "number")
        kind = "integer" if raw_kind == "tea_temperature" else raw_kind
        bound = f"{kind} {expected['minimum']}..{expected['maximum']}"
        if expected.get("enum_alternatives"):
            alts = ", ".join(str(v) for v in expected["enum_alternatives"])
            bound += f" or {alts}"
        if expected.get("numeric_exclusions"):
            excl = ", ".join(str(v) for v in expected["numeric_exclusions"])
            bound += f" (excluding {excl})"
        parts.append(bound)
    elif "min_items" in expected and "max_items" in expected:
        parts.append(f"{expected['min_items']}..{expected['max_items']} items")
    elif "minItems" in expected and "maxItems" in expected:
        parts.append(f"{expected['minItems']}..{expected['maxItems']} items")
    elif "allowed_fields" in expected and isinstance(
        expected["allowed_fields"], Sequence
    ):
        fields = ", ".join(str(v) for v in expected["allowed_fields"][:8])
        parts.append(f"fields [{fields}]")
    elif "properties" in expected and isinstance(expected["properties"], Sequence):
        fields = ", ".join(str(v) for v in expected["properties"][:8])
        parts.append(f"properties [{fields}]")
    elif "total_ml" in expected:
        parts.append(f"total_ml={expected['total_ml']}")
    elif "steep_count" in expected:
        parts.append(f"steep_count={expected['steep_count']}")
    elif "type" in expected:
        parts.append(str(expected["type"]))
    return "; ".join(parts) if parts else None


def _summarize_validation_issue(
    issues: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    if not issues:
        return None, None
    first = issues[0]
    pointer = first.get("path")
    pointer_str = pointer if isinstance(pointer, str) else None
    bits: list[str] = []
    if "value" in first:
        val = first["value"]
        rendered = _truncate(repr(val) if isinstance(val, str) else str(val), 48)
        bits.append(f"got {rendered}")
    expected = _format_expected(first.get("expected") or first.get("allowed"))
    if expected:
        bits.append(f"expected {expected}")
    if len(issues) > 1:
        bits.append(f"1 of {len(issues)} errors")
    summary = _truncate("; ".join(bits), MAX_SUMMARY_WIDTH) if bits else None
    return pointer_str, summary


def build_diagnostic(
    code: str,
    *,
    cause: str | None = None,
    fix: str | None = None,
    location: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct one canonical diagnostic dictionary from a catalog code."""
    spec = CATALOG[code]
    result: dict[str, Any] = {
        "code": spec.code,
        "cause": _truncate(cause or spec.default_cause, MAX_SANITISED_TEXT),
        "fix": _truncate(fix or spec.default_fix, MAX_SANITISED_TEXT),
        "docs_url": spec.docs_url,
        "repair_guide": spec.repair_guide(),
    }
    clean_loc = _clean_location(location)
    if clean_loc:
        result["location"] = clean_loc
    clean_ctx = _clean_context(context)
    if clean_ctx:
        result["context"] = clean_ctx
    return result


def _clean_location(location: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(location, Mapping):
        return None
    cleaned: dict[str, Any] = {}
    for key, value in location.items():
        if key not in LOCATION_KEYS or value is None:
            continue
        if key in ("line", "column"):
            if type(value) is int and value >= 1:
                cleaned[key] = value
        elif isinstance(value, str):
            text = _truncate(value, MAX_SANITISED_TEXT)
            if text or key == "pointer":
                cleaned[key] = text
    return cleaned or None


def _clean_context(context: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(context, Mapping):
        return None
    cleaned: dict[str, Any] = {}
    summary = context.get("summary")
    if isinstance(summary, str) and summary.strip():
        cleaned["summary"] = _truncate(summary, MAX_SUMMARY_WIDTH)
    excerpt = context.get("excerpt")
    if isinstance(excerpt, list) and excerpt:
        lines: list[dict[str, Any]] = []
        for item in excerpt[:5]:
            if (
                isinstance(item, Mapping)
                and type(item.get("line")) is int
                and item["line"] >= 1
                and isinstance(item.get("text"), str)
                and type(item.get("highlight")) is bool
            ):
                entry: dict[str, Any] = {
                    "line": item["line"],
                    "text": _truncate_line(item["text"], MAX_EXCERPT_WIDTH),
                    "highlight": item["highlight"],
                }
                col = item.get("column")
                if type(col) is int and col >= 1:
                    entry["column"] = col
                lines.append(entry)
        if lines:
            cleaned["excerpt"] = lines
    return cleaned or None


def sanitize_diagnostic(raw: Any) -> dict[str, Any] | None:
    """Validate and sanitize a diagnostic object received from a remote peer."""
    if not isinstance(raw, Mapping):
        return None
    code = raw.get("code")
    if not isinstance(code, str) or code not in CATALOG:
        return None
    spec = CATALOG[code]
    cause = raw.get("cause")
    fix = raw.get("fix")
    # Never trust arbitrary remote URLs or local paths: always derive canonical
    # docs and repair guide links from the verified catalog entry.
    result: dict[str, Any] = {
        "code": spec.code,
        "cause": _truncate(
            cause if isinstance(cause, str) and cause.strip() else spec.default_cause,
            MAX_SANITISED_TEXT,
        ),
        "fix": _truncate(
            fix if isinstance(fix, str) and fix.strip() else spec.default_fix,
            MAX_SANITISED_TEXT,
        ),
        "docs_url": spec.docs_url,
        "repair_guide": spec.repair_guide(),
    }
    clean_loc = _clean_location(
        raw.get("location") if isinstance(raw.get("location"), Mapping) else None
    )
    if clean_loc:
        # Remote peers must never inject local filesystem paths.
        clean_loc.pop("path", None)
        if clean_loc:
            result["location"] = clean_loc
    clean_ctx = _clean_context(
        raw.get("context") if isinstance(raw.get("context"), Mapping) else None
    )
    if clean_ctx:
        # Excerpts are strictly local-file CLI artifacts, never remote payloads.
        clean_ctx.pop("excerpt", None)
        if clean_ctx:
            result["context"] = clean_ctx
    return result


def validation_diagnostic(
    errors: Sequence[Mapping[str, Any]],
    *,
    command_validation: bool = False,
    tea: bool = False,
    context: DiagnosticContext | None = None,
) -> dict[str, Any]:
    """Build a canonical diagnostic for recipe or machine-command validation."""
    code = "XB_COMMAND_VALIDATION" if command_validation else "XB_RECIPE_VALIDATION"
    pointer, summary = _summarize_validation_issue(errors)
    cause = None
    fix = None
    if errors:
        first = errors[0]
        label = pointer or "/"
        msg = first.get("message")
        if isinstance(msg, str) and msg.strip():
            cause = f"{label}: {msg}"
        if tea and isinstance(first.get("repair_hint"), str):
            fix = first["repair_hint"]
    location: dict[str, Any] = {}
    if pointer is not None:
        location["kind"] = "json_pointer"
        location["pointer"] = pointer
    if context is not None:
        if context.command:
            location["command"] = context.command
        if context.method:
            location["method"] = context.method
        if context.tool:
            location["tool"] = context.tool
    ctx_payload: dict[str, Any] = {}
    if summary:
        ctx_payload["summary"] = summary
    diagnostic = build_diagnostic(
        code,
        cause=cause,
        fix=fix,
        location=location or None,
        context=ctx_payload or None,
    )
    if context is not None:
        _apply_local_pointer_excerpt(diagnostic, context, pointer)
    return diagnostic


def preset_write_diagnostic(
    *,
    refused: bool,
    cause: str,
    payload: Mapping[str, Any] | None = None,
    attempt: Mapping[str, Any] | None = None,
    context: DiagnosticContext | None = None,
) -> dict[str, Any]:
    """Build a diagnostic for incomplete or policy-refused preset writes."""
    code = "XB_PRESET_WRITE_REFUSED" if refused else "XB_PRESET_WRITE_INCOMPLETE"
    location: dict[str, Any] = {}
    summary_bits: list[str] = []
    source = attempt if isinstance(attempt, Mapping) else payload
    slot = _extract_preset_slot(source) or _extract_preset_slot(payload)
    if slot:
        location["kind"] = "slot"
        location["slot"] = slot
    if context is not None:
        if context.command:
            location["command"] = context.command
        if context.method:
            location["method"] = context.method
    if isinstance(source, Mapping):
        if isinstance(source.get("write_id"), str):
            summary_bits.append(f"write_id={source['write_id']}")
        if isinstance(source.get("state"), str):
            summary_bits.append(f"state={source['state']}")
        if isinstance(source.get("outcome"), str):
            summary_bits.append(f"outcome={source['outcome']}")
        if (
            type(source.get("attempts_used")) is int
            and type(source.get("max_attempts")) is int
        ):
            summary_bits.append(
                f"attempts={source['attempts_used']}/{source['max_attempts']}"
            )
        if type(source.get("retry_allowed")) is bool:
            summary_bits.append(f"retry_allowed={str(source['retry_allowed']).lower()}")
    fix = None
    if isinstance(source, Mapping) and isinstance(source.get("next_step"), str):
        fix = source["next_step"]
    return build_diagnostic(
        code,
        cause=cause,
        fix=fix,
        location=location or None,
        context={"summary": "; ".join(summary_bits)} if summary_bits else None,
    )


def _extract_preset_slot(data: Mapping[str, Any] | None) -> str | None:
    if not isinstance(data, Mapping):
        return None
    slot = data.get("slot")
    if isinstance(slot, str):
        return slot
    preset = data.get("preset")
    if isinstance(preset, Mapping):
        preset_slot = preset.get("slot")
        if isinstance(preset_slot, str):
            return preset_slot
    slots = data.get("requested_slots")
    if isinstance(slots, list) and slots and all(isinstance(s, str) for s in slots):
        return ",".join(slots)
    return None


def _apply_local_pointer_excerpt(
    diagnostic: dict[str, Any],
    context: DiagnosticContext,
    pointer: str | None,
) -> None:
    if not context.allow_excerpt or not context.source_path:
        return
    location = diagnostic.setdefault("location", {"kind": "json_pointer"})
    location.setdefault("path", context.source_path)
    if pointer is None or not context.source_text:
        return
    # Command validation prefixes pointers with "/params"; strip when locating
    # inside a CLI --params JSON file whose root is the params object itself.
    lookup_pointer = pointer
    if context.parameter == "params" and pointer.startswith("/params"):
        lookup_pointer = pointer.removeprefix("/params")
    located = locate_json_pointer_line(context.source_text, lookup_pointer)
    if located is None:
        return
    line, col = located
    location["line"] = line
    location["column"] = col
    excerpt = extract_json_excerpt(context.source_text, line, col)
    if excerpt:
        diagnostic.setdefault("context", {})["excerpt"] = excerpt


def attach_local_context(
    diagnostic: dict[str, Any], context: DiagnosticContext | None
) -> dict[str, Any]:
    """Enrich an existing diagnostic with truthful local CLI command/file context."""
    if context is None:
        return diagnostic
    enriched = dict(diagnostic)
    location = dict(enriched.get("location") or {})
    if context.command and "command" not in location:
        location["command"] = context.command
    if context.method and "method" not in location:
        location["method"] = context.method
    if context.parameter and "parameter" not in location and "pointer" not in location:
        location["parameter"] = context.parameter
    if location and "kind" not in location:
        location["kind"] = "command" if "command" in location else "method"
    if location:
        enriched["location"] = location
    pointer = location.get("pointer")
    if isinstance(pointer, str):
        _apply_local_pointer_excerpt(enriched, context, pointer)
    elif (
        context.allow_excerpt
        and context.source_path
        and enriched["code"]
        in (
            "XB_JSON_SYNTAX",
            "XB_INPUT_READ_FAILED",
        )
    ):
        enriched.setdefault("location", {"kind": "file"})["path"] = context.source_path
    return enriched


def diagnostic_from_detail(
    detail: Mapping[str, Any], context: DiagnosticContext | None = None
) -> dict[str, Any]:
    """Return a canonical diagnostic for any error envelope, including older peers."""
    existing = detail.get("diagnostic")
    if isinstance(existing, Mapping):
        sanitized = sanitize_diagnostic(existing)
        if sanitized is not None:
            # Remote envelopes were already stripped by sanitize_diagnostic in
            # error_detail; preserve already-validated local file path/excerpt.
            if isinstance(existing.get("location"), Mapping):
                raw_path = existing["location"].get("path")
                if isinstance(raw_path, str) and raw_path.strip():
                    sanitized.setdefault("location", {})["path"] = _truncate(
                        raw_path, MAX_SANITISED_TEXT
                    )
            if isinstance(existing.get("context"), Mapping):
                clean_ctx = _clean_context(existing["context"])
                if clean_ctx and "excerpt" in clean_ctx:
                    sanitized.setdefault("context", {})["excerpt"] = clean_ctx[
                        "excerpt"
                    ]
            return attach_local_context(sanitized, context)

    error_code = str(detail.get("code") or "internal_error")
    message = str(detail.get("message") or "")
    details = detail.get("details")
    diag_code = _infer_code(error_code, message, details)
    next_step = detail.get("next_step")
    location: dict[str, Any] = {}
    ctx_payload: dict[str, Any] = {}

    if isinstance(details, Mapping):
        validation = details.get("validation")
        if isinstance(validation, Mapping) and isinstance(
            validation.get("errors"), list
        ):
            return validation_diagnostic(
                validation["errors"],
                command_validation=diag_code == "XB_COMMAND_VALIDATION",
                context=context,
            )
        if isinstance(details.get("preset_write"), Mapping):
            return preset_write_diagnostic(
                refused=diag_code == "XB_PRESET_WRITE_REFUSED",
                cause=message or CATALOG[diag_code].default_cause,
                payload=details["preset_write"],
                context=context,
            )
        if isinstance(details.get("operation_id"), str):
            location["kind"] = "method"
            location["operation_id"] = details["operation_id"]
        if isinstance(details.get("errors"), list) and details["errors"]:
            first = details["errors"][0]
            if isinstance(first, Mapping) and isinstance(first.get("path"), str):
                location["kind"] = "json_pointer"
                location["pointer"] = first["path"]
                if isinstance(first.get("message"), str):
                    ctx_payload["summary"] = first["message"]

    diagnostic = build_diagnostic(
        diag_code,
        cause=message or None,
        fix=next_step if isinstance(next_step, str) and next_step.strip() else None,
        location=location or None,
        context=ctx_payload or None,
    )
    return attach_local_context(diagnostic, context)


def _infer_code(error_code: str, message: str, details: Any) -> str:
    if error_code == "not_found":
        if "Recipe" in message:
            return "XB_RECIPE_NOT_FOUND"
        if "Operation" in message:
            return "XB_OPERATION_NOT_FOUND"
        if "Import" in message:
            return "XB_IMPORT_NOT_FOUND"
        return "XB_NOT_FOUND"
    if error_code == "validation_error" and isinstance(details, Mapping):
        validation = details.get("validation")
        if isinstance(validation, Mapping) and isinstance(
            validation.get("errors"), list
        ):
            errors = validation["errors"]
            if errors and isinstance(errors[0], Mapping):
                path = errors[0].get("path")
                if isinstance(path, str) and path.startswith("/params"):
                    return "XB_COMMAND_VALIDATION"
        return "XB_RECIPE_VALIDATION"
    if error_code == "conflict" and isinstance(details, Mapping):
        if "preset_write" in details:
            return "XB_PRESET_WRITE_REFUSED"
    return FALLBACK_BY_ERROR_CODE.get(error_code, "XB_INTERNAL_ERROR")


def _format_location(location: Mapping[str, Any] | None) -> str | None:
    if not isinstance(location, Mapping) or not location:
        return None
    parts: list[str] = []
    path = location.get("path")
    line = location.get("line")
    column = location.get("column")
    pointer = location.get("pointer")
    command = location.get("command")
    method = location.get("method")
    tool = location.get("tool")
    parameter = location.get("parameter")
    slot = location.get("slot")
    operation_id = location.get("operation_id")

    if isinstance(path, str):
        coord = path
        if type(line) is int:
            coord += f":{line}"
            if type(column) is int:
                coord += f":{column}"
        parts.append(coord)
    elif type(line) is int:
        coord = f"line {line}"
        if type(column) is int:
            coord += f", column {column}"
        parts.append(coord)

    if isinstance(pointer, str):
        parts.append(f"({pointer or '/'})" if parts else (pointer or "/"))

    if not parts:
        if isinstance(command, str):
            parts.append(f"xbloom-studio {command}")
        elif isinstance(tool, str):
            parts.append(f"tool {tool}")
        elif isinstance(method, str):
            parts.append(f"method {method}")

    qualifiers: list[str] = []
    if isinstance(parameter, str):
        qualifiers.append(f"parameter: {parameter}")
    if isinstance(slot, str):
        qualifiers.append(f"slot: {slot}")
    if isinstance(operation_id, str):
        qualifiers.append(f"operation_id: {operation_id}")
    if qualifiers:
        joined = ", ".join(qualifiers)
        parts.append(f"({joined})" if parts else joined)

    return " ".join(parts) if parts else None


def _render_excerpt(excerpt: Sequence[Mapping[str, Any]]) -> list[str]:
    width = max(len(str(item["line"])) for item in excerpt)
    rendered: list[str] = []
    for item in excerpt:
        marker = ">" if item.get("highlight") else " "
        num = str(item["line"]).rjust(width)
        rendered.append(f"{marker} {num} | {item['text']}")
        col = item.get("column")
        if item.get("highlight") and type(col) is int and col >= 1:
            pad = " " * (col - 1)
            rendered.append(f"  {' ' * width} | {pad}^")
    return rendered


def render_human_diagnostic(
    detail: Mapping[str, Any], *, context: DiagnosticContext | None = None
) -> str:
    """Format a plain-text Solid-inspired diagnostic block for human stderr."""
    diagnostic = diagnostic_from_detail(detail, context=context)
    code = diagnostic["code"]
    cause = diagnostic["cause"]
    fix = diagnostic["fix"]
    docs_url = diagnostic["docs_url"]
    guide = diagnostic["repair_guide"]

    blocks: list[str] = []
    ctx = diagnostic.get("context")
    if isinstance(ctx, Mapping) and isinstance(ctx.get("excerpt"), list):
        blocks.append("\n".join(_render_excerpt(ctx["excerpt"])))

    blocks.append(f"[{code}] {cause}")

    meta: list[str] = []
    loc_text = _format_location(diagnostic.get("location"))
    if loc_text:
        meta.append(f"Location: {loc_text}")
    if isinstance(ctx, Mapping) and isinstance(ctx.get("summary"), str):
        meta.append(f"Context: {ctx['summary']}")
    correlation_id = detail.get("correlation_id")
    if isinstance(correlation_id, str) and correlation_id:
        meta.append(f"Correlation ID: {correlation_id}")
    if meta:
        blocks.append("\n".join(meta))

    guide_line = f"{guide['local']} - {guide['url']}"
    if "skill" in guide:
        guide_line += f" (skill: {guide['skill']})"

    blocks.append(
        "\n".join(
            [
                f"Fix: {fix}",
                f"[{code}] what it means and how to fix it: {docs_url}",
                f"[{code}] agent repair guide: {guide_line}",
            ]
        )
    )
    return "\n\n".join(blocks)
