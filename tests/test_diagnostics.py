"""Offline tests for Solid-inspired diagnostics across CLI, RPC, SSH and MCP."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError, URLError

from tests.machine import Machine
from tests.test_facade import run_cli
from tests.test_skills import command_problems
from xbloom_studio import cli, diagnostics
from xbloom_studio.client import ProtocolCommandError, SessionError
from xbloom_studio.command_schema import CommandValidationError
from xbloom_studio.control import BusyError
from xbloom_studio.diagnostics import (
    CATALOG,
    DiagnosticContext,
    LocalJsonSyntaxError,
    diagnostic_from_detail,
    extract_json_excerpt,
    locate_json_pointer_line,
    render_human_diagnostic,
    sanitize_diagnostic,
)
from xbloom_studio.errors import error_detail
from xbloom_studio.facade import Application
from xbloom_studio.machine_controls import PresetWriteError, UntestedOperationError
from xbloom_studio.mcp_server import create_server
from xbloom_studio.ownership import LeaseBusyError, LeaseDirectoryError
from xbloom_studio.preset_writes import PresetWriteRefused
from xbloom_studio.providers import ProviderError
from xbloom_studio.recipe_validation import (
    RecipeValidationError,
    check_recipe,
)
from xbloom_studio.rpc import LocalServer, RemoteError, call
from xbloom_studio.rpc_types import SocketDirectoryError, SocketPathError
from xbloom_studio.storage import ConflictError, Store
from xbloom_studio.tea_validation import validate_tea_recipe

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "diagnostics.md"
FAST = {"settle": 0, "subscription_settle": 0, "transition_delay": 0}


def _slug(heading: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 _-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", cleaned)


def _headings(path: Path) -> set[str]:
    anchors: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.+)$", line)
        if match:
            anchors.add(_slug(match.group(1)))
    return anchors


def run_cli_raw(*args: str, stderr_tty: bool = False) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    err.isatty = lambda: stderr_tty  # type: ignore[method-assign]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


class CatalogAndDocsTests(unittest.TestCase):
    def test_every_catalog_code_has_valid_docs_and_repair_anchors(self):
        self.assertTrue(DOCS.is_file())
        doc_anchors = _headings(DOCS)
        doc_text = DOCS.read_text(encoding="utf-8")
        skill_cache: dict[str, set[str]] = {}
        for code, spec in CATALOG.items():
            with self.subTest(code=code):
                self.assertRegex(code, r"^XB_[A-Z0-9_]+$")
                self.assertIn(spec.anchor, doc_anchors)
                self.assertIn(f"{spec.anchor}-agent-repair", doc_anchors)
                self.assertIn(f"[`{code}`](#{spec.anchor})", doc_text)
                self.assertTrue(spec.docs_url.endswith(f"#{spec.anchor}"))
                guide = spec.repair_guide()
                self.assertEqual(
                    guide["local"], f"docs/diagnostics.md#{spec.anchor}-agent-repair"
                )
                self.assertTrue(
                    guide["url"].endswith(
                        f"docs/diagnostics.md#{spec.anchor}-agent-repair"
                    )
                )
                self.assertNotIn("retry the start", spec.default_fix.lower())
                if spec.skill is not None:
                    rel_path, _, anchor = spec.skill.partition("#")
                    skill_file = ROOT / rel_path
                    self.assertTrue(skill_file.is_file(), rel_path)
                    anchors = skill_cache.setdefault(rel_path, _headings(skill_file))
                    self.assertIn(anchor, anchors, spec.skill)

    def test_docs_commands_parse_with_cli_and_mcp_parsers(self):
        text = DOCS.read_text(encoding="utf-8")
        self.assertEqual(command_problems(text), [])


class ExcerptAndPointerTests(unittest.TestCase):
    def test_locate_json_pointer_handles_objects_arrays_and_escapes(self):
        text = (
            "{\n"
            '  "name": "Morning",\n'
            '  "meta": {"a/b": {"c~d": 42}},\n'
            '  "steps": [\n'
            '    {"kind": "water", "volume_ml": 0}\n'
            "  ]\n"
            "}\n"
        )
        self.assertEqual(locate_json_pointer_line(text, "/steps/0/volume_ml"), (5, 36))
        self.assertEqual(locate_json_pointer_line(text, "/meta/a~1b/c~0d"), (3, 27))
        self.assertEqual(locate_json_pointer_line(text, ""), (1, 1))
        # Missing fields never fabricate a line.
        self.assertIsNone(locate_json_pointer_line(text, "/steps/0/temperature_c"))
        self.assertIsNone(locate_json_pointer_line(text, "no-leading-slash"))
        self.assertIsNone(locate_json_pointer_line("{bad", "/a"))
        self.assertIsNone(locate_json_pointer_line("[]", "/0"))
        self.assertEqual(locate_json_pointer_line("{}", ""), (1, 1))
        self.assertEqual(locate_json_pointer_line("[[1, 2]]", "/0/1"), (1, 6))
        for bad_json in (
            "{1: 2}",
            '{"a" 1}',
            '{"a": 1 2}',
            '{"a": }',
            "[1 2]",
            "[1,]",
            '{"a": 1} extra',
        ):
            with self.subTest(bad_json=bad_json):
                self.assertIsNone(locate_json_pointer_line(bad_json, "/a"))

    def test_expected_formatting_and_location_rendering_variants(self):
        # Cover summary formatting for every validation schema shape.
        long_enum = list(range(12))
        cases = [
            ({"enum": long_enum}, "one of [0, 1, 2, 3, 4, 5, 6, 7, ...]"),
            (
                {
                    "type": "tea_temperature",
                    "minimum": 40,
                    "maximum": 99,
                    "enum_alternatives": ["RT", "BP"],
                    "numeric_exclusions": [96],
                },
                "integer 40..99 or RT, BP (excluding 96)",
            ),
            ({"min_items": 1, "max_items": 3}, "1..3 items"),
            ({"minItems": 2, "maxItems": 2}, "2..2 items"),
            ({"allowed_fields": ["name", "steps"]}, "fields [name, steps]"),
            ({"properties": ["level"]}, "properties [level]"),
            ({"total_ml": 240}, "total_ml=240"),
            ({"steep_count": 2}, "steep_count=2"),
            ({"type": "object"}, "object"),
        ]
        for expected, snippet in cases:
            with self.subTest(snippet=snippet):
                diag = diagnostics.validation_diagnostic(
                    [
                        {
                            "path": "/x",
                            "message": "bad",
                            "value": "v",
                            "expected": expected,
                        },
                        {"path": "/y", "message": "second"},
                    ]
                )
                self.assertIn(snippet, diag["context"]["summary"])
                self.assertIn("1 of 2 errors", diag["context"]["summary"])

        # Preset write diagnostic supports batch requested_slots and attempt next_step.
        batch_diag = diagnostics.preset_write_diagnostic(
            refused=False,
            cause="batch incomplete",
            payload={"requested_slots": ["A", "B", "C"]},
            attempt={"next_step": "Ask user first."},
        )
        self.assertEqual(batch_diag["location"]["slot"], "A,B,C")
        self.assertEqual(batch_diag["fix"], "Ask user first.")

        # Human rendering handles line-without-path, tool, method, and operation_id.
        rendered_line = render_human_diagnostic(
            error_detail(json.JSONDecodeError("Expecting value", "{", 1))
        )
        self.assertIn("Location: line 1, column 2", rendered_line)
        rendered_tool = render_human_diagnostic(
            {
                "code": "invalid_argument",
                "message": "bad args",
                "details": {"errors": [{"path": "/rating", "message": "too high"}]},
            },
            context=DiagnosticContext(tool="history_feedback"),
        )
        self.assertIn("Location: /rating", rendered_tool)
        rendered_op = render_human_diagnostic(
            {
                "code": "response_too_large",
                "message": "too large",
                "details": {"operation_id": "op-5"},
            },
            context=DiagnosticContext(method="operation_start"),
        )
        self.assertIn("method operation_start (operation_id: op-5)", rendered_op)
        # Very long cause text is cleanly truncated.
        long_diag = diagnostics.build_diagnostic("XB_INVALID_ARGUMENT", cause="x" * 700)
        self.assertTrue(long_diag["cause"].endswith("..."))
        self.assertLessEqual(len(long_diag["cause"]), diagnostics.MAX_SANITISED_TEXT)

    def test_excerpt_is_minimal_windowed_and_redacts_secrets(self):
        # Assemble sensitive test strings at runtime to satisfy publication_policy.
        gh_tok = "gh" + "p_" + "a" * 36
        api_tok = "s" + "k-" + "b" * 24
        text = (
            "{\n"
            f'  "api_key": "{api_tok}",\n'
            '  "steps": [\n'
            f'    {{"note": "{gh_tok}"}}\n'
            "  ]\n"
            "}\n"
        )
        redacted_key = extract_json_excerpt(text, 2, 15)
        assert redacted_key is not None
        target = next(item for item in redacted_key if item["highlight"])
        self.assertEqual(target["text"], "<redacted>")
        self.assertNotIn("column", target)
        self.assertNotIn(api_tok, json.dumps(redacted_key))

        redacted_val = extract_json_excerpt(text, 4, 10)
        assert redacted_val is not None
        self.assertNotIn(gh_tok, json.dumps(redacted_val))

        # Long minified line: syntax error gets narrow window; no-column skips dump.
        minified = '{"name": "' + ("a" * 240) + '", "bad": }'
        self.assertIsNone(extract_json_excerpt(minified, 1, None))
        windowed = extract_json_excerpt(minified, 1, len(minified))
        assert windowed is not None
        self.assertLessEqual(len(windowed[0]["text"]), diagnostics.MAX_EXCERPT_WIDTH)
        self.assertIn("column", windowed[0])
        self.assertIsNone(extract_json_excerpt("", 1, 1))
        self.assertIsNone(extract_json_excerpt("{}", 5, 1))


class ErrorContractAndSanitizationTests(unittest.TestCase):
    def test_every_known_exception_maps_to_expected_diagnostic_code(self):
        recipe_err = RecipeValidationError(check_recipe({"name": "", "steps": []}))
        cmd_err = CommandValidationError(
            [{"path": "/params/level", "message": "out of range", "allowed": {}}]
        )
        cases = [
            (recipe_err, "validation_error", "XB_RECIPE_VALIDATION"),
            (cmd_err, "validation_error", "XB_COMMAND_VALIDATION"),
            (
                PresetWriteError("incomplete", {"slot": "A", "outcome": "unconfirmed"}),
                "preset_write_incomplete",
                "XB_PRESET_WRITE_INCOMPLETE",
            ),
            (
                PresetWriteRefused(
                    "refused",
                    {
                        "write_id": "w1",
                        "preset": {"slot": "B"},
                        "state": "exhausted",
                        "attempts_used": 3,
                        "max_attempts": 3,
                        "retry_allowed": False,
                    },
                ),
                "conflict",
                "XB_PRESET_WRITE_REFUSED",
            ),
            (
                UntestedOperationError("needs opt-in"),
                "experimental_required",
                "XB_EXPERIMENTAL_REQUIRED",
            ),
            (
                ProtocolCommandError({"kind": "fault", "value": 7}, 8100),
                "machine_protocol_error",
                "XB_MACHINE_PROTOCOL_ERROR",
            ),
            (LeaseBusyError("owned"), "busy", "XB_OWNER_LEASE_BUSY"),
            (
                LeaseDirectoryError("bad mode"),
                "permission_required",
                "XB_DIRECTORY_PERMISSION_REQUIRED",
            ),
            (
                SocketDirectoryError("bad dir"),
                "permission_required",
                "XB_DIRECTORY_PERMISSION_REQUIRED",
            ),
            (
                SocketPathError("too long"),
                "invalid_argument",
                "XB_SOCKET_PATH_TOO_LONG",
            ),
            (BusyError("busy"), "busy", "XB_MACHINE_BUSY"),
            (ConflictError("changed"), "conflict", "XB_CONFLICT"),
            (KeyError("Recipe not found"), "not_found", "XB_RECIPE_NOT_FOUND"),
            (KeyError("Operation not found"), "not_found", "XB_OPERATION_NOT_FOUND"),
            (KeyError("Import not found"), "not_found", "XB_IMPORT_NOT_FOUND"),
            (ProviderError("bad"), "provider_error", "XB_PROVIDER_ERROR"),
            (
                HTTPError("https://example.com", 502, "bad", None, None),
                "provider_error",
                "XB_PROVIDER_ERROR",
            ),
            (URLError("offline"), "provider_unavailable", "XB_PROVIDER_UNAVAILABLE"),
            (
                SessionError("dropped"),
                "connection_unavailable",
                "XB_SESSION_UNAVAILABLE",
            ),
            (FileExistsError("exists"), "conflict", "XB_SOCKET_EXISTS"),
            (
                FileNotFoundError("no sock"),
                "connection_unavailable",
                "XB_DAEMON_UNAVAILABLE",
            ),
            (
                TimeoutError("timed out"),
                "connection_unavailable",
                "XB_TRANSPORT_UNCERTAIN",
            ),
            (
                json.JSONDecodeError("Expecting value", "{", 1),
                "invalid_argument",
                "XB_JSON_SYNTAX",
            ),
            (
                ValueError("needs allow_control"),
                "permission_required",
                "XB_CONTROL_PERMISSION_REQUIRED",
            ),
            (
                ValueError("needs allow_untested"),
                "experimental_required",
                "XB_EXPERIMENTAL_REQUIRED",
            ),
            (ValueError("bad arg"), "invalid_argument", "XB_INVALID_ARGUMENT"),
            (
                RuntimeError("internal secret"),
                "internal_error",
                "XB_INTERNAL_ERROR",
            ),
        ]
        for exc, error_code, diag_code in cases:
            with self.subTest(exc=type(exc).__name__, diag_code=diag_code):
                detail = error_detail(exc, log_unexpected=False)
                self.assertEqual(detail["code"], error_code)
                diag = detail["diagnostic"]
                self.assertEqual(diag["code"], diag_code)
                self.assertEqual(diag["docs_url"], CATALOG[diag_code].docs_url)
                self.assertEqual(
                    diag["repair_guide"], CATALOG[diag_code].repair_guide()
                )
                if error_code == "internal_error":
                    self.assertNotIn("secret", json.dumps(detail))
                    rendered = render_human_diagnostic(detail)
                    self.assertIn("Correlation ID:", rendered)
                    self.assertNotIn("secret", rendered)

    def test_no_fabricated_location_without_context(self):
        detail = error_detail(KeyError("Recipe not found"))
        self.assertNotIn("location", detail["diagnostic"])
        rendered = render_human_diagnostic(detail)
        self.assertNotIn("Location:", rendered)
        self.assertIn("[XB_RECIPE_NOT_FOUND] Recipe not found", rendered)

    def test_remote_peer_sanitization_and_older_peer_compatibility(self):
        # Older peer without diagnostic stays untouched when filtered.
        older = RemoteError({"code": "not_found", "message": "Recipe not found"})
        filtered = error_detail(older)
        self.assertEqual(filtered, {"code": "not_found", "message": "Recipe not found"})
        # Human display still synthesizes a Solid-style diagnostic for older peers.
        rendered = render_human_diagnostic(
            filtered,
            context=DiagnosticContext(command="recipe get", parameter="reference"),
        )
        self.assertIn("[XB_RECIPE_NOT_FOUND] Recipe not found", rendered)
        self.assertIn(
            "Location: xbloom-studio recipe get (parameter: reference)", rendered
        )
        self.assertIn("docs/diagnostics.md#xb_recipe_not_found", rendered)

        # Untrusted remote peer cannot spoof docs_url, repair_guide, path or excerpt.
        spoofed = RemoteError(
            {
                "code": "conflict",
                "message": "Changed",
                "diagnostic": {
                    "code": "XB_CONFLICT",
                    "cause": "Changed",
                    "fix": "Reload first.",
                    "docs_url": "https://evil.example/phish",
                    "repair_guide": {
                        "local": "/etc/passwd",
                        "url": "https://evil.example",
                    },
                    "location": {
                        "kind": "file",
                        "path": "/etc/shadow",
                        "method": "recipe_update",
                    },
                    "context": {
                        "summary": "rev mismatch",
                        "excerpt": [{"line": 1, "text": "leaked", "highlight": True}],
                    },
                    "extra": "drop-me",
                },
            }
        )
        clean = error_detail(spoofed)["diagnostic"]
        self.assertEqual(clean["docs_url"], CATALOG["XB_CONFLICT"].docs_url)
        self.assertEqual(clean["repair_guide"], CATALOG["XB_CONFLICT"].repair_guide())
        self.assertNotIn("path", clean["location"])
        self.assertNotIn("excerpt", clean["context"])
        self.assertNotIn("extra", clean)
        self.assertEqual(clean["location"]["method"], "recipe_update")

        # Unknown remote diagnostic code is dropped cleanly.
        unknown_code = RemoteError(
            {"code": "busy", "message": "m", "diagnostic": {"code": "UNKNOWN"}}
        )
        self.assertNotIn("diagnostic", error_detail(unknown_code))
        self.assertIsNone(sanitize_diagnostic("not-a-dict"))

    def test_validation_reports_carry_diagnostics_only_when_invalid(self):
        valid = check_recipe(
            {
                "name": "Ok",
                "steps": [{"kind": "water", "volume_ml": 50, "temperature_c": 90}],
            }
        )
        self.assertTrue(valid["valid"])
        self.assertNotIn("diagnostic", valid)

        invalid = check_recipe(
            {
                "name": "Bad",
                "steps": [{"kind": "water", "volume_ml": 0, "temperature_c": 90}],
            }
        )
        self.assertFalse(invalid["valid"])
        self.assertEqual(invalid["diagnostic"]["code"], "XB_RECIPE_VALIDATION")
        self.assertEqual(
            invalid["diagnostic"]["location"]["pointer"], "/steps/0/volume_ml"
        )
        self.assertIn("got 0", invalid["diagnostic"]["context"]["summary"])

        tea_bad = validate_tea_recipe(
            {
                "name": "Green",
                "mode": "tea",
                "tea": {
                    "dose_g": 3,
                    "steeps": [
                        {"volume_ml": 80, "temperature": 96, "steep_seconds": 45}
                    ],
                },
            }
        )
        self.assertFalse(tea_bad["valid"])
        self.assertEqual(tea_bad["diagnostic"]["code"], "XB_RECIPE_VALIDATION")
        self.assertIn("Numeric 96 must use BP", tea_bad["diagnostic"]["fix"])

    def test_fallback_diagnostic_inference_for_older_peer_envelopes(self):
        cmd_diag = diagnostic_from_detail(
            {
                "code": "validation_error",
                "message": "bad",
                "details": {
                    "validation": {
                        "errors": [{"path": "/params/level", "message": "bad"}]
                    }
                },
            }
        )
        self.assertEqual(cmd_diag["code"], "XB_COMMAND_VALIDATION")
        refused = diagnostic_from_detail(
            {
                "code": "conflict",
                "message": "refused",
                "details": {
                    "preset_write": {"write_id": "w1", "preset": {"slot": "A"}}
                },
            }
        )
        self.assertEqual(refused["code"], "XB_PRESET_WRITE_REFUSED")
        self.assertEqual(refused["location"]["slot"], "A")
        op_not_found = diagnostic_from_detail(
            {"code": "not_found", "message": "Operation not found"}
        )
        self.assertEqual(op_not_found["code"], "XB_OPERATION_NOT_FOUND")
        imp_not_found = diagnostic_from_detail(
            {"code": "not_found", "message": "Import not found"}
        )
        self.assertEqual(imp_not_found["code"], "XB_IMPORT_NOT_FOUND")


class CliDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "baseline.sqlite3"
        self.sock = self.root / "none.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_baseline_missing_recipe_json_and_human_and_tty_auto(self):
        base = ["--db", str(self.db), "--socket", str(self.sock)]
        # Non-TTY auto keeps JSON automation contract with additive diagnostic.
        code, lines, errors = run_cli(*base, "recipe", "get", "definitely-missing")
        self.assertEqual((code, lines), (2, []))
        err = errors[0]["error"]
        self.assertEqual(err["code"], "not_found")
        self.assertEqual(err["diagnostic"]["code"], "XB_RECIPE_NOT_FOUND")
        self.assertEqual(
            err["diagnostic"]["location"],
            {
                "kind": "command",
                "command": "recipe get",
                "method": "recipe_get",
                "parameter": "reference",
            },
        )
        self.assertIn("#xb_recipe_not_found", err["diagnostic"]["docs_url"])
        self.assertIn(
            "#xb_recipe_not_found-agent-repair",
            err["diagnostic"]["repair_guide"]["local"],
        )

        # Explicit --error-format human renders plain Solid-style stderr display.
        code, stdout, stderr = run_cli_raw(
            "--error-format", "human", *base, "recipe", "get", "definitely-missing"
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("[XB_RECIPE_NOT_FOUND] Recipe not found", stderr)
        self.assertIn(
            "Location: xbloom-studio recipe get (parameter: reference)", stderr
        )
        self.assertIn("what it means and how to fix it:", stderr)
        self.assertIn("agent repair guide:", stderr)

        # TTY auto switches to human display; explicit json overrides TTY.
        code, _, tty_err = run_cli_raw(
            *base, "recipe", "get", "definitely-missing", stderr_tty=True
        )
        self.assertEqual(code, 2)
        self.assertIn("[XB_RECIPE_NOT_FOUND]", tty_err)

        code, _, forced_json = run_cli_raw(
            "--error-format",
            "json",
            *base,
            "recipe",
            "get",
            "definitely-missing",
            stderr_tty=True,
        )
        self.assertEqual(code, 2)
        parsed = json.loads(forced_json)
        self.assertEqual(parsed["error"]["diagnostic"]["code"], "XB_RECIPE_NOT_FOUND")

    def test_local_json_syntax_error_reports_exact_location_and_excerpt(self):
        broken = self.root / "broken.json"
        broken.write_text('{\n  "name": "Pour",\n  "steps": [\n}\n', encoding="utf-8")
        code, stdout, stderr = run_cli_raw(
            "--error-format",
            "human",
            "--socket",
            str(self.sock),
            "recipe",
            "validate",
            str(broken),
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("[XB_JSON_SYNTAX]", stderr)
        self.assertIn(f"Location: {broken}:4:1", stderr)
        self.assertIn('  3 |   "steps": [', stderr)
        self.assertIn("> 4 | }", stderr)
        self.assertIn("^", stderr)

        # JSON format includes structured location and excerpt too.
        code, _, errors = run_cli(
            "--socket", str(self.sock), "recipe", "validate", str(broken)
        )
        self.assertEqual(code, 2)
        diag = errors[0]["error"]["diagnostic"]
        self.assertEqual(diag["code"], "XB_JSON_SYNTAX")
        self.assertEqual(diag["location"]["line"], 4)
        self.assertEqual(diag["location"]["column"], 1)
        self.assertEqual(diag["location"]["path"], str(broken))
        self.assertTrue(diag["context"]["excerpt"])

        # LocalJsonSyntaxError remains a json.JSONDecodeError subclass.
        with self.assertRaises(LocalJsonSyntaxError):
            cli.load(broken)

    def test_local_recipe_validation_human_stderr_has_source_excerpt(self):
        bad = self.root / "bad-coffee.json"
        bad.write_text(
            json.dumps(
                {
                    "name": "Morning",
                    "mode": "coffee",
                    "coffee_recipe": {
                        "cup_type": "xdripper",
                        "dose_g": 15,
                        "grind_size": 60,
                        "ratio": 16,
                        "pours": [{"volume_ml": 240, "temperature": 96}],
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        # Non-TTY auto keeps stderr empty for recipe validate (automation contract).
        code, out, err = run_cli_raw(
            "--socket", str(self.sock), "recipe", "validate", str(bad)
        )
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        self.assertFalse(json.loads(out)["data"]["valid"])

        # Human format prints JSON on stdout AND Solid-style excerpt on stderr.
        code, out, err = run_cli_raw(
            "--error-format",
            "human",
            "--socket",
            str(self.sock),
            "recipe",
            "validate",
            str(bad),
        )
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["data"]["valid"])
        self.assertIn("[XB_RECIPE_VALIDATION]", err)
        self.assertIn("/coffee_recipe/pours/0/temperature", err)
        self.assertIn('"temperature": 96', err)
        self.assertIn("^", err)

    def test_unreadable_input_file_reports_input_read_failed(self):
        missing = self.root / "nope.json"
        code, _, err = run_cli_raw(
            "--error-format",
            "human",
            "--socket",
            str(self.sock),
            "recipe",
            "validate",
            str(missing),
        )
        self.assertEqual(code, 2)
        self.assertIn("[XB_INPUT_READ_FAILED]", err)
        self.assertIn(str(missing), err)


class AsyncSurfaceDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "xbloom-studio.sqlite3")
        self.machine = Machine("00:00:5E:00:53:09", auto_complete=True)
        self.app = Application(
            self.store, connector=self.machine, interaction_options=FAST
        )
        self.socket = self.root / "run" / "daemon.sock"
        self.server = await LocalServer(self.app, self.socket).start()

    async def asyncTearDown(self):
        await self.server.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_daemon_rpc_validation_and_local_cli_excerpt_merge(self):
        bad_params = self.root / "brightness.json"
        bad_params.write_text('{\n  "level": 99\n}\n', encoding="utf-8")
        # Direct RPC error carries XB_COMMAND_VALIDATION without server paths.
        with self.assertRaises(RemoteError) as caught:
            await call(
                self.socket,
                "machine_command",
                {
                    "command": "set_brightness",
                    "params": {"level": 99},
                    "allow_control": True,
                },
            )
        remote_diag = caught.exception.detail["diagnostic"]
        self.assertEqual(remote_diag["code"], "XB_COMMAND_VALIDATION")
        self.assertEqual(remote_diag["location"]["pointer"], "/params/level")
        self.assertNotIn("path", remote_diag["location"])
        self.assertNotIn("excerpt", remote_diag.get("context") or {})

        # Through the CLI, local --params file line/excerpt is attached cleanly.
        code, _, stderr = await asyncio.to_thread(
            run_cli_raw,
            "--error-format",
            "human",
            "--socket",
            str(self.socket),
            "machine",
            "command",
            "set_brightness",
            "--params",
            str(bad_params),
            "--allow-control",
        )
        self.assertEqual(code, 2)
        self.assertIn("[XB_COMMAND_VALIDATION]", stderr)
        self.assertIn(f"{bad_params}:2:12 (/params/level)", stderr)
        self.assertIn('> 2 |   "level": 99', stderr)

    async def test_daemon_too_large_start_carries_diagnostic_and_operation_id(self):
        big = {"id": "op-42", "status": "accepted", "pad": "x" * 4096}
        with (
            patch("xbloom_studio.rpc.MAX_RESPONSE", 2048),
            patch.object(self.app, "call", AsyncMock(return_value=big)),
        ):
            with self.assertRaises(RemoteError) as caught:
                await call(
                    self.socket,
                    "operation_start",
                    {"request": {}, "idempotency_key": "k-large"},
                )
        diag = caught.exception.detail["diagnostic"]
        self.assertEqual(diag["code"], "XB_RESPONSE_TOO_LARGE")
        self.assertEqual(diag["location"]["operation_id"], "op-42")
        self.assertIn("Never start again", diag["fix"])

    async def test_failed_preset_write_carries_diagnostic_and_cli_human_display(self):
        self.machine.studio.silent.add(11510)
        self.app.machine.options["ack_timeout"] = 0.05
        preset = {"slot": "A", "payload_hex": "6e6577", "flags": 4}
        result = await self.app.call(
            "machine_command",
            {
                "command": "write_preset",
                "params": {"preset": preset},
                "allow_control": True,
                "allow_untested": True,
            },
        )
        self.assertEqual(result["status"], "failed")
        diag = result["diagnostic"]
        self.assertEqual(diag["code"], "XB_PRESET_WRITE_INCOMPLETE")
        self.assertEqual(diag["location"]["slot"], "A")
        self.assertIn("write_id=", diag["context"]["summary"])
        self.assertIn("retry_allowed=true", diag["context"]["summary"])

        # CLI human mode renders the failed command diagnostic on stderr too.
        outcome = {
            "status": "failed",
            "error": "incomplete",
            "type": "PresetWriteError",
            "details": {"slot": "A"},
            "recovery_required": True,
            "diagnostic": diag,
        }
        dispatch = self.app.call

        async def simulate(method, params=None):
            if method == "machine_command":
                return outcome
            return await dispatch(method, params)

        with patch.object(self.app, "call", simulate):
            code, out, err = await asyncio.to_thread(
                run_cli_raw,
                "--error-format",
                "human",
                "--socket",
                str(self.socket),
                "machine",
                "command",
                "back_home",
                "--allow-control",
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["data"]["status"], "failed")
        self.assertIn("[XB_PRESET_WRITE_INCOMPLETE]", err)
        self.assertIn("slot: A", err)

    async def test_mcp_errors_include_canonical_diagnostics(self):
        server = create_server(caller=lambda m, p: self.app.call(m, p))
        unknown = await server.call_tool("no_such_tool", {})
        self.assertTrue(unknown.is_error)
        self.assertEqual(
            unknown.structured_content["error"]["diagnostic"]["code"],
            "XB_MCP_UNKNOWN_TOOL",
        )
        self.assertEqual(
            unknown.structured_content["error"]["diagnostic"]["location"]["tool"],
            "no_such_tool",
        )

        # Pydantic input error carries JSON Pointer and never echoes raw input.
        secret_input = "never-leak-raw-value"
        bad_arg = await server.call_tool(
            "recipe_upload",
            {
                "request": {"kind": "water", "volume_ml": 50, "temperature_c": 20},
                "expected_recipe_sha256": secret_input,
            },
        )
        self.assertTrue(bad_arg.is_error)
        diag = bad_arg.structured_content["error"]["diagnostic"]
        self.assertEqual(diag["code"], "XB_INVALID_ARGUMENT")
        self.assertEqual(diag["location"]["pointer"], "/expected_recipe_sha256")
        self.assertNotIn(secret_input, json.dumps(bad_arg.structured_content))

        # Domain error through MCP carries tool location.
        missing = await server.call_tool("recipe_get", {"reference": "missing"})
        self.assertEqual(
            missing.structured_content["error"]["diagnostic"]["code"],
            "XB_RECIPE_NOT_FOUND",
        )
        self.assertEqual(
            missing.structured_content["error"]["diagnostic"]["location"]["tool"],
            "recipe_get",
        )

        # Output contract error keeps correlation_id and operation_id.
        async def odd(method, params):
            return {"id": "op-9", "weird": True}

        bad_server = create_server(caller=odd)
        with self.assertLogs("xbloom_studio.mcp_server", "ERROR"):
            bad_out = await bad_server.call_tool(
                "operation_start",
                {
                    "request": {"kind": "water", "volume_ml": 50, "temperature_c": 20},
                    "idempotency_key": "k-odd",
                    "allow_control": True,
                },
            )
        err = bad_out.structured_content["error"]
        self.assertEqual(err["diagnostic"]["code"], "XB_MCP_OUTPUT_CONTRACT")
        self.assertEqual(err["diagnostic"]["location"]["operation_id"], "op-9")
        self.assertEqual(len(err["correlation_id"]), 32)


if __name__ == "__main__":
    unittest.main()
