"""One dispatch path: Python, the xbloom CLI, the daemon and MCP agree."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.machine import Machine
from xbloom_studio.application import MachineService
from xbloom_studio.cli import main
from xbloom_studio.facade import Application
from xbloom_studio.mcp_server import create_server
from xbloom_studio.rpc import LocalServer, call
from xbloom_studio.ssh_rpc import REMOTE_METHODS
from xbloom_studio.storage import Store

FAST = {"settle": 0, "subscription_settle": 0, "transition_delay": 0}
# Complete requests: the MCP contract fills defaults before dispatch, and an
# idempotency key always names one exact request.
WATER = {
    "kind": "water",
    "volume_ml": 50,
    "temperature_c": 20,
    "flow_ml_s": 3,
    "pattern": "center",
}
DOCUMENT = {"name": "Parity", "steps": [WATER]}


def run_cli(*args):
    """Run ``xbloom`` in this process; return (exit code, parsed stdout lines)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line]
    errors = [json.loads(line) for line in err.getvalue().splitlines() if line]
    return code, lines, errors


class ParityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name)
        self.db = self.root / "bloom.sqlite3"
        self.store = Store(self.db)
        self.machine = Machine("SIM-PARITY", auto_complete=True)
        self.app = Application(
            self.store, connector=self.machine, interaction_options=FAST
        )
        self.socket = self.root / "run" / "bloom.sock"
        self.server = await LocalServer(self.app, self.socket).start()

    async def asyncTearDown(self):
        await self.server.close()
        self.store.close()
        self.tmp.cleanup()

    async def cli(self, method, params):
        path = self.root / (method + ".json")
        path.write_text(json.dumps(params))
        code, lines, errors = await asyncio.to_thread(
            run_cli, "--socket", str(self.socket), "call", method, "--params", str(path)
        )
        self.assertEqual((code, errors), (0, []))
        return lines[-1]["data"]

    async def mcp(self, tool, arguments):
        server = create_server(caller=lambda m, p: call(self.socket, m, p))
        result = await server.call_tool(tool, arguments)
        self.assertFalse(result.is_error, result.structured_content)
        value = result.structured_content
        return value["result"] if set(value) == {"result"} else value

    async def test_read_methods_agree_across_every_adapter(self):
        recipe = await self.app.call("recipe_create", {"document": DOCUMENT})
        plan = await self.app.call("machine_prepare", {"request": WATER})
        operation, _ = self.store.create_operation(
            "SIM-PARITY", WATER, {**plan, "resolved_request": None}, "parity-key"
        )
        cases = [
            ("service_info", {}, None, None),
            ("recipe_constraints", {}, "recipe_constraints", {}),
            ("recipe_templates", {}, "recipe_templates", {}),
            ("recipe_validate", {"document": DOCUMENT}, "recipe_validate", None),
            ("recipe_preview", {"document": DOCUMENT}, "recipe_preview", None),
            ("recipe_get", {"reference": recipe["id"]}, "recipe_get", None),
            ("recipe_compile", {"reference": "Parity"}, "recipe_compile", None),
            ("history_get", {"ident": operation["id"]}, "history_get", None),
            (
                "operation_lookup",
                {"idempotency_key": "parity-key"},
                "operation_lookup",
                None,
            ),
            ("machine_capabilities", {}, "machine_capabilities", None),
            ("machine_prepare", {"request": WATER}, "machine_prepare", None),
            ("recipe_tea_references", {"family": "green"}, None, None),
            ("recipe_examples", {"coffee": {"origin": "Kenya"}}, None, None),
        ]
        for method, params, tool, arguments in cases:
            with self.subTest(method=method):
                python = await self.app.call(method, params)
                daemon = await call(self.socket, method, params)
                cli = await self.cli(method, params)
                self.assertEqual(json.loads(json.dumps(python)), daemon)
                self.assertEqual(daemon, cli)
                if tool is not None:
                    tool_result = await self.mcp(
                        tool, params if arguments is None else arguments
                    )
                    self.assertEqual(tool_result, daemon)

    async def test_mcp_defaults_replay_the_same_attempt_across_adapters(self):
        short = {"kind": "water", "volume_ml": 50, "temperature_c": 20}
        started = await call(
            self.socket,
            "operation_start",
            {"request": short, "idempotency_key": "short", "allow_control": True},
        )
        await asyncio.wait_for(asyncio.shield(self.app.machine.task), 5)
        server = create_server(caller=lambda m, p: call(self.socket, m, p))
        result = await server.call_tool(
            "operation_start",
            {"request": short, "idempotency_key": "short", "allow_control": True},
        )
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(result.structured_content["id"], started["id"])
        # The stored request is the one first accepted; nothing was rewritten.
        self.assertEqual(result.structured_content["request"], short)
        self.assertEqual(self.machine.codes().count(4506), 1)

    async def test_saved_reference_replays_across_rpc_and_mcp(self):
        saved = self.store.create_recipe(DOCUMENT)
        request = {"kind": "recipe", "reference": saved["id"]}
        params = {
            "request": {**request, "revision": None},
            "idempotency_key": "saved",
            "allow_control": True,
        }
        first = await call(self.socket, "operation_start", params)
        await asyncio.wait_for(asyncio.shield(self.app.machine.task), 5)
        result = await self.mcp("operation_start", {**params, "request": request})
        self.assertEqual(result["id"], first["id"])
        self.assertEqual(result["request"], params["request"])
        self.assertEqual(self.machine.codes().count(4506), 1)

    async def test_machine_writes_route_through_the_same_owner(self):
        started = await call(
            self.socket,
            "operation_start",
            {"request": WATER, "idempotency_key": "rpc", "allow_control": True},
        )
        await asyncio.wait_for(asyncio.shield(self.app.machine.task), 5)
        replay = await self.mcp(
            "operation_start",
            {"request": WATER, "idempotency_key": "rpc", "allow_control": True},
        )
        self.assertEqual(replay["id"], started["id"])
        self.assertEqual(replay["status"], "completed")
        code, lines, _ = await asyncio.to_thread(
            run_cli, "--socket", str(self.socket), "operation", "lookup", "rpc"
        )
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]["data"]["operation"]["id"], started["id"])
        # The CLI parses numbers as floats; the same attempt is still found.
        code, lines, _ = await asyncio.to_thread(
            run_cli,
            "--socket",
            str(self.socket),
            "water",
            "--volume-ml",
            "50",
            "--key",
            "rpc",
            "--allow-control",
        )
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]["data"]["id"], started["id"])
        self.assertEqual(self.machine.codes().count(4506), 1)
        self.assertEqual(self.machine.calls.count("connect"), 1)


class AllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bloom.sqlite3")
        self.app = Application(self.store)

    async def asyncTearDown(self):
        await self.app.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_only_allowlisted_methods_with_exact_arguments_dispatch(self):
        for method in ("close", "__init__", "methods", "store", "call", 3, None):
            with self.subTest(method=method), self.assertRaises(ValueError):
                await self.app.call(method)
        with self.assertRaises(ValueError):
            await self.app.call("service_info", [])
        with self.assertRaises(TypeError):
            await self.app.call("service_info", {"extra": 1})
        with self.assertRaises(TypeError):
            await self.app.call("recipe_get", {})
        info = await self.app.call("service_info")
        self.assertEqual(info, {"device": None, "database": str(self.store.path)})

    async def test_callers_never_choose_a_file_to_read(self):
        for method in ("recipe_examples", "recipe_tea_references"):
            with self.subTest(method=method), self.assertRaises(TypeError):
                params = {"path": "/etc/passwd"}
                if method == "recipe_examples":
                    params["coffee"] = {}
                await self.app.call(method, params)

    def test_remote_policy_is_fixed_and_narrower_than_local_methods(self):
        local = set(self.app.methods)
        self.assertLessEqual(REMOTE_METHODS, local)
        self.assertEqual(local - REMOTE_METHODS, {"history_backup", "provider_write"})
        self.app.methods["added_locally"] = (lambda: None, False)
        self.assertNotIn("added_locally", REMOTE_METHODS)

    async def test_provider_publishing_is_refused(self):
        with self.assertRaisesRegex(ValueError, "separately verified"):
            await self.app.call("provider_write", {"anything": 1})

    def test_machine_service_or_configuration_not_both(self):
        machine = MachineService(self.store)
        with self.assertRaises(ValueError):
            Application(self.store, "00:00:5E:00:53:01", machine=machine)
        self.assertIs(Application(self.store, machine=machine).machine, machine)


if __name__ == "__main__":
    unittest.main()
