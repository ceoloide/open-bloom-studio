"""The xbloom command line: routing, pure commands, direct and daemon modes."""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.machine import Machine
from tests.test_facade import run_cli
from xbloom_studio import cli
from xbloom_studio.facade import Application
from xbloom_studio.rpc import LocalServer
from xbloom_studio.simulator import ScriptedConnector
from xbloom_studio.storage import Store

WATER = {"kind": "water", "volume_ml": 50, "temperature_c": 20}


def factory(device):
    """Connector factory used through ``--connector tests.test_cli:factory``."""
    return Machine(device or "SIM-FACTORY", auto_complete=True)


def not_a_connector(device):
    return object()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "bloom.sqlite3"
        self.socket = self.root / "run" / "bloom.sock"
        environment = {"XDG_RUNTIME_DIR": str(self.root / "runtime")}
        self.env = patch.dict(os.environ, environment)
        self.env.start()
        os.environ.pop(cli.DEVICE_ENVIRONMENT, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return str(path)

    def xbloom(self, *args, connector="simulator"):
        base = ["--db", str(self.db), "--socket", str(self.socket)]
        if connector is not None:
            base += ["--connector", connector]
        return run_cli(*base, *args)


class RouteTests(CliCase):
    def route(self, *args):
        return cli.route(cli.parser().parse_args(list(args)))

    def test_commands_route_to_application_methods(self):
        doc = self.file("doc.json", {"name": "D", "steps": [WATER]})
        coffee = self.file("coffee.json", {"origin": "Kenya"})
        cases = [
            (
                ("recipe", "tea-references", "--family", "green"),
                "recipe_tea_references",
                {"family": "green"},
            ),
            (
                ("recipe", "tea-validate", doc, "--expected-output-ml", "240"),
                "recipe_tea_validate",
                None,
            ),
            (("recipe", "preview", doc), "recipe_preview", None),
            (("recipe", "constraints"), "recipe_constraints", {}),
            (("recipe", "templates"), "recipe_templates", {}),
            (
                ("recipe", "examples", coffee, "--water-ml", "250"),
                "recipe_examples",
                {"coffee": {"origin": "Kenya"}, "water_ml": 250.0, "limit": 5},
            ),
            (("recipe", "create", doc, "--temporary"), "recipe_create", None),
            (
                ("recipe", "update", "r", doc, "--expected-revision", "2"),
                "recipe_update",
                None,
            ),
            (
                ("recipe", "get", "r", "--revision", "1"),
                "recipe_get",
                {"reference": "r", "revision": 1},
            ),
            (
                ("recipe", "compile", "r"),
                "recipe_compile",
                {"reference": "r", "revision": None},
            ),
            (
                ("recipe", "archive", "r", "--expected-revision", "1"),
                "recipe_archive",
                {"reference": "r", "expected_revision": 1},
            ),
            (
                ("recipe", "alias", "r", "B"),
                "recipe_alias",
                {"reference": "r", "alias": "B"},
            ),
            (
                ("recipe", "refresh", "r", "--target", "tea"),
                "recipe_refresh",
                {"reference": "r", "flow_ml_s": None, "target": "tea"},
            ),
            (
                ("recipe", "refresh", "r"),
                "recipe_refresh",
                {"reference": "r", "flow_ml_s": None},
            ),
            (
                ("recipe", "import", "https://example.com/r"),
                "recipe_import",
                {"url": "https://example.com/r", "flow_ml_s": None, "temporary": False},
            ),
            (
                ("recipe", "search-online", "kenya", "--page", "2"),
                "provider_search",
                {"query": "kenya", "page": 2},
            ),
            (
                ("recipe", "match", coffee, "--limit", "3"),
                "recipe_match",
                {"coffee": {"origin": "Kenya"}, "limit": 3},
            ),
            (
                ("recipe", "match", coffee),
                "recipe_match",
                {"coffee": {"origin": "Kenya"}},
            ),
            (
                ("recipe", "list"),
                "recipe_list",
                {"query": "", "include_temporary": False, "limit": 100},
            ),
            (
                ("recipe", "list", "--cursor", "c"),
                "recipe_page",
                {
                    "query": "",
                    "include_temporary": False,
                    "limit": 25,
                    "cursor": "c",
                    "detail": "compact",
                },
            ),
            (
                ("history", "list", "--status", "completed"),
                "history_page",
                {
                    "query": "",
                    "liked": False,
                    "limit": 25,
                    "cursor": None,
                    "detail": "compact",
                    "status": "completed",
                    "min_rating": None,
                },
            ),
            (
                ("history", "list", "--limit", "5"),
                "history_list",
                {"query": "", "liked": False, "limit": 5},
            ),
            (("history", "get", "o"), "history_get", {"ident": "o"}),
            (
                ("history", "events", "o", "--after", "4"),
                "history_events",
                {"operation_id": "o", "after": 4},
            ),
            (
                ("history", "feedback", "o", "Nice", "--rating", "5"),
                "history_feedback",
                {"ident": "o", "note": "Nice", "rating": 5},
            ),
            (
                ("history", "save-recipe", "o", "Again"),
                "history_save_recipe",
                {"operation_id": "o", "name": "Again"},
            ),
            (("machine", "state"), "machine_state", {}),
            (
                ("machine", "connect", "--allow-untested"),
                "machine_connect",
                {"allow_untested": True},
            ),
            (
                ("machine", "command", "back_home", "--allow-control"),
                "machine_command",
                {
                    "command": "back_home",
                    "params": {},
                    "allow_control": True,
                    "allow_untested": False,
                },
            ),
            (
                ("machine", "start-uploaded", "t", "--key", "k", "--allow-control"),
                "recipe_start_uploaded",
                {"token": "t", "idempotency_key": "k", "allow_control": True},
            ),
            (
                ("machine", "cancel-uploaded", "t"),
                "recipe_cancel_uploaded",
                {"token": "t", "allow_control": False},
            ),
            (
                ("operation", "lookup", "k"),
                "operation_lookup",
                {"idempotency_key": "k"},
            ),
            (("operation", "watch", "o"), "history_get", {"ident": "o"}),
            (
                ("operation", "resume", "o"),
                "operation_pause_resume",
                {"operation_id": "o", "resume": True},
            ),
            (
                ("operation", "acknowledge", "o", "t"),
                "operation_acknowledge",
                {"operation_id": "o", "token": "t"},
            ),
            (("operation", "stop", "o"), "operation_stop", {"operation_id": "o"}),
        ]
        for args, method, params in cases:
            with self.subTest(args=args):
                routed, values = self.route(*args)
                self.assertEqual(routed, method)
                if params is not None:
                    self.assertEqual(values, params)

    def test_operation_starts_carry_key_and_explicit_opt_ins(self):
        method, params = self.route(
            "water", "--volume-ml", "50", "--key", "k", "--allow-control"
        )
        self.assertEqual(method, "operation_start")
        self.assertEqual(
            params,
            {
                "request": {
                    "kind": "water",
                    "volume_ml": 50.0,
                    "temperature_c": 20,
                    "flow_ml_s": 3,
                    "pattern": "center",
                },
                "idempotency_key": "k",
                "allow_control": True,
                "allow_untested": False,
            },
        )
        _, params = self.route(
            "grind", "--size", "20", "--rpm", "80", "--duration-s", "1", "--key", "g"
        )
        self.assertEqual(params["request"]["kind"], "grind")
        self.assertFalse(params["allow_control"])
        _, params = self.route("history", "repeat", "o", "--key", "r")
        self.assertEqual(params["request"], {"kind": "repeat", "operation_id": "o"})
        _, params = self.route("brew", "B", "--revision", "2", "--key", "b")
        self.assertEqual(
            params["request"], {"kind": "recipe", "reference": "B", "revision": 2}
        )
        doc = self.file("doc.json", {"name": "D", "steps": [WATER]})
        _, params = self.route("brew", "--file", doc, "--key", "b")
        self.assertEqual(params["request"]["document"]["name"], "D")
        for args in (
            ("brew", "--key", "b"),
            ("brew", "B", "--file", doc, "--key", "b"),
        ):
            with self.assertRaises(ValueError):
                self.route(*args)
        method, params = self.route("machine", "upload", "B", "--allow-control")
        self.assertEqual(method, "recipe_upload")
        self.assertEqual(params["request"]["reference"], "B")

    def test_unreadable_input_files_are_argument_errors(self):
        with self.assertRaisesRegex(ValueError, "Unable to read"):
            self.route("recipe", "validate", str(self.root / "missing.json"))
        code, _, errors = run_cli(
            "--socket", str(self.socket), "call", "x", "--params", str(self.root)
        )
        self.assertEqual(code, 2)
        self.assertEqual(errors[0]["error"]["code"], "invalid_argument")


class PureCommandTests(CliCase):
    def test_offline_commands_need_no_database_or_machine(self):
        code, lines, _ = self.xbloom("recipe", "templates", connector=None)
        self.assertEqual(code, 0)
        self.assertEqual(len(lines[0]["data"]), 3)
        good = self.file("good.json", {"name": "G", "steps": [WATER]})
        bad = self.file("bad.json", {"name": "", "steps": []})
        self.assertEqual(self.xbloom("recipe", "validate", good, connector=None)[0], 0)
        code, lines, _ = self.xbloom("recipe", "validate", bad, connector=None)
        self.assertEqual(code, 2)
        self.assertFalse(lines[0]["data"]["valid"])
        code, lines, _ = self.xbloom(
            "recipe",
            "grind-profile",
            "Pulse",
            "--size",
            "20",
            "--rpm",
            "80",
            "--duration-s",
            "2",
            connector=None,
        )
        self.assertEqual(code, 0)
        params = self.file("params.json", {})
        code, lines, _ = self.xbloom(
            "call", "recipe_constraints", "--params", params, connector=None
        )
        self.assertEqual((code, lines[0]["data"]["version"]), (0, 1))
        self.assertFalse(self.db.exists())

    def test_generic_call_only_reads_bundled_guidance(self):
        for method, arguments in (
            ("recipe_tea_references", {}),
            ("recipe_examples", {"coffee": {}}),
        ):
            with self.subTest(method=method):
                corpus = self.file("corpus.json", {})
                params = self.file("params.json", {**arguments, "path": corpus})
                code, _, errors = self.xbloom(
                    "call", method, "--params", params, connector=None
                )
                self.assertNotEqual(code, 0)
                self.assertIn(
                    "unexpected keyword argument", errors[0]["error"]["message"]
                )
        self.assertFalse(self.db.exists())

    def test_option_conflicts_are_refused(self):
        cases = [
            ("--ssh-config", "c", "recipe", "templates"),
            ("--ssh=-oProxyCommand=x", "recipe", "templates"),
            ("--ssh", "host", "--direct", "recipe", "templates"),
            ("--ssh", "host", "--connector", "simulator", "machine", "state"),
            ("mcp",),
            ("--db", "x", "relay"),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, _, errors = run_cli(*args)
                self.assertEqual(code, 2)
                self.assertFalse(errors[0]["ok"])

    def test_relay_runs_without_global_options(self):
        with patch("xbloom_studio.ssh_rpc.main", return_value=0) as relay:
            self.assertEqual(cli.main(["relay", "--socket", "/x"]), 0)
        relay.assert_called_once_with(["--socket", "/x"])

    def test_module_entry_runs_the_cli(self):
        argv = ["xbloom_studio", "--socket", str(self.socket), "recipe", "constraints"]
        with patch("sys.argv", argv), patch("sys.stdout"):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("xbloom_studio", run_name="__main__")
        self.assertEqual(caught.exception.code, 0)

    def test_interrupt_exits_130(self):
        with patch.object(cli, "run", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["recipe", "templates"]), 130)


class DirectTests(CliCase):
    def test_direct_water_runs_to_observed_completion(self):
        code, lines, errors = self.xbloom(
            "--direct", "water", "--volume-ml", "50", "--key", "w", "--allow-control"
        )
        self.assertEqual((code, errors), (0, []))
        self.assertEqual(lines[0]["data"]["status"], "accepted")
        self.assertEqual(lines[-1]["data"]["status"], "completed")
        code, lines, _ = self.xbloom("operation", "lookup", "w")
        self.assertEqual(lines[0]["data"]["operation"]["status"], "completed")
        code, lines, _ = self.xbloom(
            "--direct", "water", "--volume-ml", "50", "--key", "w", "--allow-control"
        )
        self.assertEqual((code, lines[-1]["data"]["status"]), (0, "completed"))
        store = Store(self.db)
        try:
            self.assertEqual(len(store.history()), 1)
        finally:
            store.close()

    def test_direct_generic_start_preserves_the_supplied_digest(self):
        params = self.file(
            "start.json",
            {
                "request": {
                    "kind": "recipe",
                    "document": {"name": "Recipe", "steps": [WATER]},
                },
                "idempotency_key": "digest-mismatch",
                "allow_control": True,
                "expected_recipe_sha256": "0" * 64,
            },
        )
        code, _, errors = self.xbloom(
            "--direct", "call", "operation_start", "--params", params
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(errors[0]["error"]["code"], "conflict")
        store = Store(self.db)
        try:
            self.assertEqual(store.history(), [])
        finally:
            store.close()

    def test_direct_null_digest_still_pins_the_previewed_recipe(self):
        store = Store(self.db)
        try:
            saved = store.create_recipe({"name": "Recipe", "steps": [WATER]})
        finally:
            store.close()
        ident = saved["id"]
        params = self.file(
            "start.json",
            {
                "request": {"kind": "recipe", "reference": ident},
                "idempotency_key": "digest-pin",
                "allow_control": True,
                "expected_recipe_sha256": None,
            },
        )
        call = Application.call

        async def update_after_preview(app, method, params=None):
            result = await call(app, method, params)
            if method == "machine_prepare":
                app.store.update_recipe(
                    ident, {"name": "Recipe", "steps": [{**WATER, "volume_ml": 70}]}, 1
                )
            return result

        with patch.object(Application, "call", update_after_preview):
            code, _, errors = self.xbloom(
                "--direct", "call", "operation_start", "--params", params
            )
        self.assertNotEqual(code, 0)
        self.assertEqual(errors[0]["error"]["code"], "conflict")
        store = Store(self.db)
        try:
            self.assertEqual(store.history(), [])
        finally:
            store.close()

    def test_direct_mode_refuses_daemon_only_work(self):
        gate = self.file(
            "gate.json",
            {"name": "Gate", "steps": [{"kind": "user", "instruction": "Cup"}, WATER]},
        )
        cases = [
            ("brew", "--file", gate, "--key", "g", "--allow-control"),
            ("machine", "connect"),
            ("machine", "command", "back_home", "--allow-control"),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, _, errors = self.xbloom(*args)
                self.assertEqual(code, 2)
                self.assertIn("daemon", errors[0]["error"]["message"])
        code, _, errors = self.xbloom(
            "water", "--volume-ml", "50", "--key", "x", connector=None
        )
        self.assertEqual(errors[0]["error"]["code"], "permission_required")

    def test_failed_direct_operation_exits_nonzero(self):
        machine = Machine("SIM-SILENT", auto_complete=True)
        machine.studio.silent.add(4506)
        loaded = (machine, dict(cli.SIMULATOR_OPTIONS, ack_timeout=0.2))
        with patch.object(cli, "load_connector", return_value=loaded):
            code, lines, _ = self.xbloom(
                "--direct",
                "water",
                "--volume-ml",
                "60",
                "--key",
                "s",
                "--allow-control",
            )
        self.assertEqual(code, 2)
        self.assertNotEqual(lines[-1]["data"]["status"], "completed")

    def test_history_export_streams_every_event(self):
        self.xbloom(
            "--direct", "water", "--volume-ml", "50", "--key", "e", "--allow-control"
        )
        store = Store(self.db)
        try:
            ident = store.operation_by_key("e")["id"]
            count = len(store.events(ident))
        finally:
            store.close()
        code, lines, _ = self.xbloom("history", "export", ident)
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), count)
        self.assertEqual(len({line["id"] for line in lines}), count)


class ConnectorSpecTests(unittest.TestCase):
    def test_simulator_and_factories(self):
        connector, options = cli.load_connector("simulator", None)
        self.assertIsInstance(connector, ScriptedConnector)
        self.assertEqual(connector.identity, "SIMULATED-STUDIO")
        self.assertEqual(options["transition_delay"], 0)
        connector, options = cli.load_connector("tests.test_cli:factory", "X-1")
        self.assertEqual((connector.identity, options), ("X-1", {}))
        for spec in ("nothing", "module:", ":name", "tests.test_cli:not-a-name"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                cli.load_connector(spec, None)
        with self.assertRaises(TypeError):
            cli.load_connector("tests.test_cli:not_a_connector", None)


class DaemonClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "bloom.sqlite3"
        self.store = Store(self.db)
        self.machine = Machine("00:00:5E:00:53:07", auto_complete=False)
        self.app = Application(
            self.store,
            connector=self.machine,
            interaction_options={
                "settle": 0,
                "subscription_settle": 0,
                "transition_delay": 0,
            },
        )
        self.socket = self.root / "run" / "bloom.sock"
        self.server = await LocalServer(self.app, self.socket).start()

    async def asyncTearDown(self):
        await self.server.close()
        self.store.close()
        self.tmp.cleanup()

    async def xbloom(self, *args):
        return await asyncio.to_thread(run_cli, "--socket", str(self.socket), *args)

    async def test_client_checks_the_served_machine_and_database(self):
        code, _, errors = await self.xbloom(
            "--device", "00:00:5E:00:53:08", "machine", "state"
        )
        self.assertEqual(code, 2)
        self.assertIn("different device", errors[0]["error"]["message"])
        code, _, _ = await self.xbloom(
            "--device", "00-00-5e-00-53-07", "machine", "state"
        )
        self.assertEqual(code, 0)
        code, _, errors = await self.xbloom(
            "--db", str(self.root / "x.db"), "machine", "state"
        )
        self.assertIn("different database", errors[0]["error"]["message"])
        code, _, errors = await self.xbloom(
            "--connector", "simulator", "machine", "state"
        )
        self.assertIn("daemon owns the link", errors[0]["error"]["message"])
        code, _, errors = await self.xbloom("--direct", "machine", "state")
        self.assertIn("do not bypass", errors[0]["error"]["message"])

    async def test_waiting_start_follows_the_daemon_operation(self):
        task = asyncio.create_task(
            self.xbloom(
                "water",
                "--volume-ml",
                "50",
                "--key",
                "watch",
                "--allow-control",
                "--wait",
            )
        )

        async def poured():
            while 4506 not in self.machine.codes():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poured(), 5)
        self.machine.emit(40511)
        self.machine.state(1)
        code, lines, _ = await asyncio.wait_for(task, 10)
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1]["data"]["status"], "completed")
        code, lines, _ = await self.xbloom(
            "operation", "watch", lines[-1]["data"]["id"]
        )
        self.assertEqual((code, lines[-1]["data"]["status"]), (0, "completed"))

    async def test_daemon_pure_methods_run_on_the_daemon(self):
        good = self.root / "good.json"
        good.write_text(json.dumps({"name": "G", "steps": [WATER]}))
        code, lines, _ = await self.xbloom("recipe", "validate", str(good))
        self.assertEqual((code, lines[0]["data"]["valid"]), (0, True))
        bad = self.root / "bad.json"
        bad.write_text("{}")
        code, _, _ = await self.xbloom("recipe", "validate", str(bad))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
