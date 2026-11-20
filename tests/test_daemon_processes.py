"""Real daemon processes: caller loss, crash, restart recovery and shutdown."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_ssh_rpc import quiet_asyncio
from xbloom_studio.recipes import compile_recipe
from xbloom_studio.rpc import RemoteError, call
from xbloom_studio.storage import Store

WAIT = {"kind": "recipe", "document": {"name": "Wait", "steps": []}}


class DaemonProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        quiet_asyncio(self)
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name)
        self.db = self.root / "bloom.sqlite3"
        self.socket = self.root / "run" / "bloom.sock"
        self.processes = []

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.tmp.cleanup()

    async def start(self, *extra):
        args = ["--db", str(self.db), "--socket", str(self.socket)]
        args += ["--connector", "simulator", "serve", *extra]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "xbloom_studio",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
        self.processes.append(process)
        return process

    async def ready(self, process):
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        return json.loads(line)["data"]

    async def test_accepted_work_outlives_its_caller(self):
        daemon = await self.start()
        ready = await self.ready(daemon)
        self.assertEqual(ready, {"serving": str(self.socket), "connected": False})
        request = {
            "kind": "recipe",
            "document": {
                "name": "Pause",
                "steps": [{"kind": "wait", "duration_s": 0.5}],
            },
        }
        params = {"request": request, "idempotency_key": "lost", "allow_control": True}
        # Deliver the request, then leave before any response arrives.
        _, writer = await asyncio.open_unix_connection(str(self.socket))
        message = {"method": "operation_start", "params": params}
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        async def lookup():
            while True:
                found = await call(
                    self.socket, "operation_lookup", {"idempotency_key": "lost"}
                )
                if found["operation"] and found["operation"]["status"] == "completed":
                    return found["operation"]
                await asyncio.sleep(0.05)

        operation = await asyncio.wait_for(lookup(), 15)
        replay = await call(self.socket, "operation_start", params)
        self.assertEqual(replay["id"], operation["id"])
        daemon.send_signal(signal.SIGTERM)
        self.assertEqual(await asyncio.wait_for(daemon.wait(), 15), 0)
        self.assertFalse(self.socket.exists())

    async def test_crash_restart_recovers_unknown_without_replay(self):
        first = await self.start()
        await self.ready(first)
        store = Store(self.db)
        try:
            plan = compile_recipe(
                {"name": "Crash", "steps": [{"kind": "wait", "duration_s": 0}]}
            )
            op, _ = store.create_operation("SIMULATED-STUDIO", WAIT, plan, "crash")
            store.finish(op["id"], "running")
        finally:
            store.close()
        first.kill()
        await asyncio.wait_for(first.wait(), 15)
        self.assertTrue(self.socket.is_socket())
        refused = await self.start()
        _, err = await asyncio.wait_for(refused.communicate(), 30)
        self.assertEqual(refused.returncode, 2)
        error = json.loads(err)["error"]
        self.assertEqual(error["code"], "conflict")
        self.assertIn("--recover-stale-socket", error["next_step"])
        second = await self.start("--recover-stale-socket")
        await self.ready(second)
        found = await call(
            self.socket, "operation_lookup", {"idempotency_key": "crash"}
        )
        self.assertEqual(found["operation"]["status"], "unknown")
        result = await call(self.socket, "history_get", {"ident": op["id"]})
        self.assertIn("do not retry", result["result"]["reason"])
        replay = await call(
            self.socket,
            "operation_start",
            {"request": WAIT, "idempotency_key": "crash", "allow_control": True},
        )
        self.assertEqual((replay["id"], replay["status"]), (op["id"], "unknown"))
        state = await call(self.socket, "machine_state")
        self.assertIsNone(state["machine"])  # Nothing connected or replayed.
        missing = await call(self.socket, "operation_lookup", {"idempotency_key": "x"})
        self.assertEqual(missing, {"operation": None})
        with self.assertRaises(RemoteError):
            await call(
                self.socket,
                "operation_start",
                {"request": WAIT, "idempotency_key": "crash"},
            )
        second.send_signal(signal.SIGINT)
        self.assertEqual(await asyncio.wait_for(second.wait(), 15), 0)


if __name__ == "__main__":
    unittest.main()
