"""MachineService: owned scheduling, durable intent and explicit uncertainty."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.frames import reply, u32
from tests.machine import Machine, until
from xbloom_studio.application import MachineService, RecordingConnector
from xbloom_studio.client import SessionError
from xbloom_studio.control import BusyError
from xbloom_studio.errors import error_detail
from xbloom_studio.journal import Recorder
from xbloom_studio.machine_api import same_attempt, same_request
from xbloom_studio.ownership import LeaseBusyError
from xbloom_studio.preset_writes import STARTED, PresetWriteRefused
from xbloom_studio.storage import ConflictError, Store

FAST = {
    "settle": 0,
    "subscription_settle": 0,
    "transition_delay": 0,
    "ack_timeout": 0.2,
    # These simulator writes include a durable SQLite flush.
    "write_timeout": 3,
}
WATER = {"kind": "water", "volume_ml": 50, "temperature_c": 20}
POUR = 4506
NATIVE = {
    "kind": "recipe",
    "document": {
        "name": "Native",
        "mode": "native",
        "native": {"payload_hex": "0102", "dose_g": 15},
    },
}


def recipe(*steps):
    return {"kind": "recipe", "document": {"name": "Steps", "steps": list(steps)}}


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    auto_complete = True

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "xbloom-studio.sqlite3")
        self.machine = Machine("00:00:5E:00:53:01", auto_complete=self.auto_complete)
        self.service = self.make(self.machine)

    def make(self, machine, store=None, **kwargs):
        return MachineService(
            store or self.store,
            interaction_options=dict(FAST),
            connector=machine,
            **kwargs,
        )

    async def asyncTearDown(self):
        await self.service.close()
        self.store.close()
        self.tmp.cleanup()

    async def finish(self):
        await asyncio.wait_for(asyncio.shield(self.service.task), 5)
        return self.store.operation(self.service.operation_id)

    def kinds(self, operation_id=None):
        return [event["kind"] for event in self.store.events(operation_id, limit=10000)]


class LifecycleTests(ServiceCase):
    async def test_inline_recipe_records_history_and_repeat_reuses_plan(self):
        first = await self.service.start(
            recipe({**WATER, "kind": "water"}), "one", True
        )
        self.assertEqual(first["status"], "accepted")
        done = await self.finish()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["device"], "00:00:5E:00:53:01")
        repeated = await self.service.start(
            {"kind": "repeat", "operation_id": first["id"]}, "two", True
        )
        self.assertEqual(repeated["plan"], done["plan"])
        self.assertEqual((await self.finish())["status"], "completed")
        self.assertEqual(self.machine.calls.count("connect"), 1)
        self.assertEqual(self.machine.codes().count(POUR), 2)

    async def test_lifecycle_is_journaled_in_order_around_commands(self):
        await self.service.start(WATER, "order", True)
        await self.finish()
        await self.service.disconnect()
        kinds = self.kinds()
        self.assertLess(
            kinds.index("session.connect_requested"), kinds.index("session.connected")
        )
        self.assertLess(kinds.index("session.connected"), kinds.index("command.intent"))
        self.assertIn("session.subscribed", kinds)
        self.assertEqual(kinds[-1], "session.closed")
        # Each committed intent precedes its first outgoing chunk.
        for index, kind in enumerate(kinds):
            if (
                kind == "ble.outgoing_chunk"
                and kinds[index - 1] != "ble.outgoing_chunk"
            ):
                self.assertEqual(kinds[index - 1], "command.intent")
        self.assertEqual(kinds.count("command.intent"), len(self.machine.sent))

    async def test_link_loss_is_recorded_as_a_capture_gap(self):
        await self.service.connect()
        self.machine.drop()
        await until(lambda: "session.disconnected" in self.kinds())
        gap = [e for e in self.store.events() if e["kind"] == "session.disconnected"]
        self.assertEqual(gap[0]["data"], {"capture_gap": True})
        with self.assertRaisesRegex(SessionError, "fresh session") as raised:
            await self.service.start(WATER, "after-loss", True)
        detail = error_detail(raised.exception)
        self.assertEqual(detail["code"], "connection_unavailable")
        self.assertIn("fresh session", detail["message"])
        self.assertIn("disconnect/reconnect", detail["next_step"])
        self.assertIsNone(self.store.operation_by_key("after-loss"))

    async def test_information_reads_are_recorded(self):
        link = RecordingConnector(self.machine, Recorder(self.store, "X"))
        await link.connect(lambda: None)
        self.assertEqual(await link.read_information("manufacturer"), "Simulator")
        await link.stop_notify()
        await link.disconnect()
        await link.recorder.close()
        self.assertEqual(link.write_size, self.machine.write_size)
        self.assertIn("gatt.info", self.kinds())
        self.assertIn("session.unsubscribed", self.kinds())

    async def test_gate_tokens_acknowledge_one_step_and_stop_prevents_pour(self):
        request = recipe(
            {"kind": "user", "instruction": "Place cup"},
            {"kind": "user", "instruction": "Place funnel"},
            WATER,
        )
        await self.service.start(request, "gate", True)
        await until(lambda: self.service.gate is not None)
        # Gate creation and its database transition share the service guard.
        async with self.service.guard:
            first = dict(self.service.gate)
            ident = self.service.operation_id
            self.assertEqual(self.store.operation(ident)["status"], "waiting_for_user")
        with self.assertRaises(ConflictError):
            await self.service.acknowledge(ident, "wrong")
        self.assertEqual(
            await self.service.acknowledge(ident, first["token"]),
            {"acknowledged": True},
        )
        await until(
            lambda: (
                self.service.gate is not None
                and self.service.gate["token"] != first["token"]
            )
        )
        with self.assertRaises(ConflictError):
            await self.service.acknowledge(ident, first["token"])
        with self.assertRaises(BusyError):
            await self.service.pause_resume(ident)
        stopped = await self.service.stop(ident)
        self.assertEqual(stopped["status"], "stopped")
        self.assertNotIn(POUR, self.machine.codes())
        with self.assertRaises(BusyError):
            await self.service.stop(ident)

    async def test_replay_returns_an_earlier_attempt_during_active_work(self):
        first = await self.service.start(WATER, "first", True)
        self.assertEqual((await self.finish())["status"], "completed")
        current = await self.service.start(
            recipe({"kind": "user", "instruction": "Place cup"}), "current", True
        )
        await until(lambda: self.service.gate is not None)
        async with self.service.guard:
            self.assertEqual(
                self.store.operation(current["id"])["status"], "waiting_for_user"
            )
        duplicate = await self.service.start(WATER, "first", True)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(duplicate["status"], "completed")
        with self.assertRaises(ConflictError):
            await self.service.start({**WATER, "volume_ml": 70}, "first", True)
        stopped = await self.service.stop(current["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertNotIn("stop_result", stopped["result"])
        self.assertEqual(self.machine.codes().count(POUR), 1)

    async def test_gate_stop_keeps_completed_step_evidence_separate(self):
        started = await self.service.start(
            recipe(WATER, {"kind": "user", "instruction": "Place cup"}),
            "step-gate",
            True,
        )
        await until(lambda: self.service.gate is not None, 4)
        async with self.service.guard:
            row = self.store.operation(started["id"])
            self.assertEqual(row["status"], "waiting_for_user")
            self.assertEqual(row["result"]["steps"][0]["status"], "completed")
        stopped = await self.service.stop(started["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["result"]["steps"], row["result"]["steps"])
        self.assertNotIn("stop_result", stopped["result"])
        self.assertEqual(self.machine.codes().count(POUR), 1)

    async def test_busy_machine_never_queues_another_start(self):
        await self.service.start(recipe({"kind": "wait", "duration_s": 60}), "w", True)
        with self.assertRaises(BusyError):
            await self.service.start(WATER, "compete", True)
        self.assertIsNone(self.store.operation_by_key("compete"))
        with self.assertRaises(BusyError):
            await self.service.command("back_home", {}, True)
        same = await self.service.start(
            recipe({"kind": "wait", "duration_s": 60}), "w", True
        )
        self.assertEqual(same["id"], self.service.operation_id)
        results = await asyncio.gather(
            self.service.stop(self.service.operation_id),
            self.service.stop(self.service.operation_id),
        )
        self.assertTrue(all(r["status"] == "stopped" for r in results))

    async def test_tare_step_owns_the_scale_and_releases_it(self):
        await self.service.start(recipe({"kind": "tare"}), "tare", True)
        self.assertEqual((await self.finish())["status"], "completed")
        self.assertFalse(self.service.session._scale_mode)

    async def test_pause_and_resume_reach_the_active_device_step(self):
        self.machine.studio.auto_complete = False
        await self.service.start(WATER, "pause", True)
        await until(lambda: POUR in self.machine.codes())
        ident = self.service.operation_id
        with self.assertRaises(ValueError):
            await self.service.pause_resume(ident, "yes")
        state = await self.service.pause_resume(ident)
        self.assertEqual(state["operation"]["status"], "running")
        await self.service.pause_resume(ident, resume=True)
        with self.assertRaises(BusyError):
            await self.service.pause_resume("other")
        self.assertEqual((await self.service.stop(ident))["status"], "stopped")

    async def test_close_stops_active_work_and_releases_the_link(self):
        await self.service.start(recipe({"kind": "wait", "duration_s": 60}), "c", True)
        ident = self.service.operation_id
        await self.service.close()
        self.assertEqual(self.store.operation(ident)["status"], "stopped")
        self.assertIsNone(self.service.session)
        self.assertEqual(self.machine.calls[-1], "disconnect")
        with self.assertRaises(BusyError):
            await self.service.connect()

    async def test_connect_reuses_session_and_rejects_bad_options(self):
        state = await self.service.connect()
        self.assertTrue(state["machine"]["machine"]["connected"])
        await self.service.connect()
        self.assertEqual(self.machine.calls.count("connect"), 1)
        with self.assertRaisesRegex(SessionError, "fresh session") as raised:
            await self.service.connect(allow_untested=True)
        detail = error_detail(raised.exception)
        self.assertEqual(detail["code"], "experimental_required")
        self.assertIn("Disconnect", detail["message"])
        with self.assertRaises(ValueError):
            await self.service.connect(allow_untested="yes")


class AdmissionTests(ServiceCase):
    async def test_rejected_requests_write_nothing_and_record_nothing(self):
        bad = [
            ({"kind": "water", "volume_ml": -1, "temperature_c": 20}, "bad"),
            (
                {
                    **NATIVE,
                    "document": {
                        **NATIVE["document"],
                        "native": {
                            "payload_hex": "00",
                            "dose_g": 10,
                            "grinder_enabled": True,
                        },
                    },
                },
                "exp",
            ),
            ({"kind": "teleport"}, "kind"),
            ({"kind": "repeat"}, "repeat"),
            ({"kind": "recipe", "extra": 1}, "fields"),
            ({"kind": "recipe"}, "source"),
            ("water", "shape"),
        ]
        for request, key in bad:
            with self.subTest(key=key), self.assertRaises(ValueError):
                await self.service.start(request, key, True)
        for args in (("", True), ("k", False), ("k", "yes")):
            with self.assertRaises(ValueError):
                await self.service.start(WATER, args[0], args[1])
        with self.assertRaises(ValueError):
            await self.service.start(WATER, "k", True, allow_untested="yes")
        with self.assertRaises(ConflictError):
            await self.service.start(WATER, "hash", True, expected_recipe_sha256="0")
        self.assertEqual(self.machine.calls, [])
        self.assertEqual(self.store.history(), [])
        self.assertIsNone(self.service.state()["machine"])

    async def test_idempotent_replay_returns_the_attempt_and_never_rewrites(self):
        row = self.store.create_recipe(
            {"name": "B", "steps": [{**WATER, "volume_ml": 10}]}
        )
        request = {"kind": "recipe", "reference": row["id"]}
        first = await self.service.start(request, "one", True)
        await self.finish()
        self.store.delete_recipe(row["id"], 1)
        again = await self.service.start(request, "one", True)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.machine.codes().count(POUR), 1)
        with self.assertRaises(ConflictError):
            await self.service.start(WATER, "one", True)
        self.assertEqual(self.machine.codes().count(POUR), 1)

    async def test_identity_spellings_share_attempts_and_other_machines_conflict(self):
        request = WATER
        plan = self.service.prepare(request)
        self.store.create_operation("00-00-5e-00-53-01", request, plan, "spelled")
        self.store.create_operation("00:00:5E:00:53:02", request, plan, "other")
        self.store.create_operation("", request, plan, "blank")
        replay = await self.service.start(request, "spelled", True)
        self.assertEqual(replay["status"], "unknown")  # recovered, never replayed
        for key in ("other", "blank"):
            with self.subTest(key=key), self.assertRaises(ConflictError):
                await self.service.start(request, key, True)
        self.assertEqual(self.machine.calls, [])

    def test_numerically_equal_requests_are_one_attempt(self):
        self.assertTrue(same_request({"v": 50, "x": [1.0]}, {"v": 50.0, "x": [1]}))
        for first, second in (
            ({"v": True}, {"v": 1}),
            ({"v": 1}, {"v": 2}),
            ({"v": [1]}, {"v": [1, 2]}),
            ({"v": 1}, {"w": 1}),
            ({"v": "1"}, {"v": 1}),
            ([1], {"v": 1}),
        ):
            with self.subTest(first=first):
                self.assertFalse(same_request(first, second))
                self.assertFalse(same_attempt(first, second))
        self.assertTrue(same_attempt([1], [1.0]))
        self.assertFalse(same_attempt(WATER, [WATER]))

    async def test_replay_with_numeric_spelling_returns_the_attempt(self):
        first = await self.service.start(WATER, "spelling", True)
        await self.finish()
        again = await self.service.start(
            {**WATER, "volume_ml": 50.0, "temperature_c": 20.0}, "spelling", True
        )
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.machine.codes().count(POUR), 1)

    async def test_replay_with_omitted_or_explicit_defaults_returns_the_attempt(self):
        explicit = {**WATER, "flow_ml_s": 3, "pattern": "center", "name": "water"}
        first = await self.service.start(WATER, "defaults", True)
        await self.finish()
        again = await self.service.start(explicit, "defaults", True)
        self.assertEqual(again["id"], first["id"])
        # Stored data and its fingerprint stay exactly as first accepted.
        self.assertEqual(self.store.operation(first["id"])["request"], WATER)
        second = await self.service.start(explicit, "explicit-first", True)
        await self.finish()
        again = await self.service.start(WATER, "explicit-first", True)
        self.assertEqual(again["id"], second["id"])
        self.assertEqual(self.store.operation(second["id"])["request"], explicit)
        self.assertEqual(self.machine.codes().count(POUR), 2)
        for changed in (
            {**WATER, "flow_ml_s": 4},
            {**WATER, "pattern": "ring"},
            {**WATER, "flow_ml_s": True},
            {**WATER, "name": "other"},
            {**WATER, "timeout": 61.0},
        ):
            with self.subTest(changed=changed), self.assertRaises(ConflictError):
                await self.service.start(changed, "defaults", True)
        self.assertEqual(self.machine.codes().count(POUR), 2)

    async def test_replay_of_the_active_attempt_accepts_explicit_defaults(self):
        self.machine.studio.auto_complete = False
        first = await self.service.start(WATER, "active", True)
        await until(lambda: POUR in self.machine.codes(), 2)
        explicit = {**WATER, "flow_ml_s": 3.0, "pattern": "center"}
        again = await self.service.start(explicit, "active", True)
        self.assertEqual(again["id"], first["id"])
        with self.assertRaises(ConflictError):
            await self.service.start({**WATER, "pattern": "ring"}, "active", True)
        self.machine.emit(40511)
        self.machine.state(1)
        self.assertEqual((await self.finish())["status"], "completed")
        self.assertEqual(self.machine.codes().count(POUR), 1)

    async def test_inline_document_defaults_name_one_attempt(self):
        omitted = recipe(WATER)
        explicit = {
            "kind": "recipe",
            "document": {
                "schema_version": 1,
                "mode": "host",
                "name": "Steps",
                "steps": [{**WATER, "flow_ml_s": 3.0, "pattern": "center"}],
            },
        }
        first = await self.service.start(omitted, "inline", True)
        completed = await self.finish()
        self.assertEqual(completed["status"], "completed", completed["result"])
        self.assertEqual(
            (await self.service.start(explicit, "inline", True))["id"], first["id"]
        )
        with self.assertRaises(ConflictError):
            await self.service.start(recipe({**WATER, "flow_ml_s": 4}), "inline", True)
        # A mutable reference is never resolved to compare it.
        saved = self.store.create_recipe(omitted["document"])
        by_name = {"kind": "recipe", "reference": "Steps"}
        await self.service.start(by_name, "reference", True)
        completed = await self.finish()
        self.assertEqual(completed["status"], "completed", completed["result"])
        for other in (
            {"kind": "recipe", "reference": saved["id"]},
            {**by_name, "revision": 1},
        ):
            with self.subTest(other=other), self.assertRaises(ConflictError):
                await self.service.start(other, "reference", True)
        self.assertEqual(self.machine.codes().count(POUR), 2)

    async def test_inline_native_defaults_replay_without_resending(self):
        first = await self.service.start(NATIVE, "native-defaults", True)
        self.assertEqual((await self.finish())["status"], "completed")
        document = NATIVE["document"]
        explicit = {
            **NATIVE,
            "document": {
                **document,
                "native": {
                    **document["native"],
                    "grinder_enabled": False,
                    "bypass_ml": 0,
                    "bypass_temperature_c": 20,
                },
            },
        }
        again = await self.service.start(explicit, "native-defaults", True)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["request"], NATIVE)
        self.assertEqual(self.machine.codes().count(8002), 1)

    async def test_legacy_persisted_requests_are_still_replayed(self):
        plan = self.service.prepare(WATER)
        mcp_style = {**WATER, "flow_ml_s": 3, "pattern": "center"}
        legacy, _ = self.store.create_operation(
            "00:00:5E:00:53:01", mcp_style, plan, "legacy"
        )
        self.store.finish(legacy["id"], "completed", {})
        self.assertEqual(
            (await self.service.start(WATER, "legacy", True))["id"], legacy["id"]
        )
        odd = {"kind": "recipe", "document": {"name": "Old", "steps": "retired"}}
        old, _ = self.store.create_operation("00:00:5E:00:53:01", odd, plan, "odd")
        self.store.finish(old["id"], "completed", {})
        self.assertEqual((await self.service.start(odd, "odd", True))["id"], old["id"])
        with self.assertRaises(ConflictError):
            await self.service.start(recipe(WATER), "odd", True)
        self.assertNotIn(POUR, self.machine.codes())

    def test_construction_validates_identity_and_options(self):
        with self.assertRaises(ValueError):
            MachineService(self.store, "00:00:5E:00:53:09", connector=self.machine)
        with self.assertRaises(TypeError):
            MachineService(self.store, connector=object())
        with self.assertRaises(ValueError):
            MachineService(self.store, "x", capture_idle_telemetry="no")
        same = MachineService(self.store, "00-00-5e-00-53-01", connector=self.machine)
        self.assertEqual(same.device, "00:00:5E:00:53:01")
        self.assertIsNone(MachineService(self.store).device)

    def test_inline_recipe_rejects_saved_revision_selection(self):
        with self.assertRaisesRegex(ValueError, "inline document"):
            self.service.prepare({**recipe(WATER), "revision": 2})

    async def test_machine_operations_need_a_configured_machine(self):
        service = MachineService(self.store)
        try:
            with self.assertRaisesRegex(ValueError, "machine address"):
                await service.start(WATER, "none", True)
            with self.assertRaisesRegex(SessionError, "connect first"):
                service._recorder  # noqa: B018
        finally:
            await service.close()

    async def test_default_link_is_local_bluetooth_for_the_given_address(self):
        service = MachineService(self.store, "00:00:5e:00:53:03")
        link = service._link()
        self.assertEqual(type(link).__name__, "BleakConnector")
        self.assertEqual(link.device, "00:00:5e:00:53:03")
        self.assertEqual(service.device, "00:00:5E:00:53:03")


class UncertaintyTests(ServiceCase):
    async def test_write_failure_is_possible_delivery_never_completion(self):
        self.machine.fail_commands[POUR] = ConnectionError("link dropped mid-write")
        await self.service.start(WATER, "fail", True)
        done = await self.finish()
        # Possible delivery is unresolved: the attempt is unknown, never failed.
        self.assertEqual(done["status"], "unknown")
        self.assertIn("unresolved", done["result"]["reason"])
        # The step keeps the machine layer's own outcome and evidence.
        step = done["result"]["steps"][0]
        self.assertEqual(step["status"], "failed")
        self.assertIn("link dropped", step["error"])
        self.assertEqual(step["cleanup_status"], "skipped_uncertain")
        self.assertFalse(step["start_observed"])
        self.assertIn("stop_acknowledged", step)
        self.assertTrue(done["result"]["state"]["recovery_required"])
        self.assertTrue(self.service.state()["machine"]["recovery_required"])
        kinds = self.kinds(done["id"])
        self.assertIn("command.write_failed", kinds)
        self.assertIn("operation.unknown", kinds)
        self.assertNotIn("operation.completed", kinds)
        self.assertNotIn("operation.failed", kinds)

    async def test_configuration_write_failure_is_unknown(self):
        self.machine.fail_commands[4510] = ConnectionError("lost during configure")
        await self.service.start(WATER, "configure", True)
        done = await self.finish()
        self.assertEqual(done["status"], "unknown")
        self.assertEqual(done["result"]["steps"][0]["status"], "failed")
        self.assertNotIn(POUR, self.machine.codes())

    async def test_definite_refusal_before_the_start_stays_failed(self):
        def low_water(packet):
            # Acknowledge the configuration, then report low water.
            return [*self.machine.studio(packet), reply(40522, u32(0))]

        self.machine.overrides[4510] = low_water
        await self.service.start(WATER, "refused", True)
        done = await self.finish()
        self.assertEqual(done["status"], "failed")
        self.assertNotIn("reason", done["result"])
        step = done["result"]["steps"][0]
        self.assertEqual(step["status"], "failed")
        self.assertIn("Water sufficiency", step["error"])
        self.assertEqual(step["cleanup_status"], "completed")
        self.assertFalse(done["result"]["state"]["recovery_required"])
        self.assertNotIn(POUR, self.machine.codes())

    async def test_withheld_final_start_chunk_is_failed_with_recovery_required(self):
        self.machine.block_code = POUR
        await self.service.start(WATER, "partial-start", True)
        try:
            await asyncio.wait_for(self.machine.blocked.wait(), 2)
            self.machine.emit(40522, u32(0))
            await until(
                lambda: (
                    self.service._session.state.observations["water_sufficient"].value
                    is False
                )
            )
        finally:
            self.machine.release.set()
        done = await self.finish()
        self.assertEqual(done["status"], "failed")
        self.assertTrue(done["result"]["state"]["recovery_required"])
        self.assertTrue(self.service._session._partial_frame)
        self.assertNotIn(POUR, self.machine.codes())

    async def test_missing_acknowledgment_is_not_completion(self):
        self.machine.studio.silent.add(POUR)
        await self.service.start(WATER, "silent", True)
        done = await self.finish()
        self.assertIn(done["status"], ("failed", "unknown"))
        self.assertNotEqual(done["status"], "completed")

    async def test_acknowledged_start_without_observed_finish_stays_running(self):
        self.machine.studio.auto_complete = False
        op = await self.service.start(WATER, "ack-only", True)
        await until(lambda: POUR in self.machine.codes())
        await asyncio.sleep(0.05)
        self.assertEqual(self.store.operation(op["id"])["status"], "running")
        self.machine.emit(40511)
        self.machine.state(1)
        self.assertEqual((await self.finish())["status"], "completed")

    async def test_recording_failure_stops_and_marks_history_unknown(self):
        await self.service.start(
            recipe({"kind": "user", "instruction": "Cup"}), "f", True
        )
        await until(lambda: self.service.gate is not None)
        self.service.recorder._fail(OSError("disk failure"))
        done = await self.finish()
        self.assertEqual(done["status"], "unknown")
        self.assertIn("recording_error", done["result"])
        self.assertNotIn(POUR, self.machine.codes())
        self.service.recorder.error = None  # Permit orderly fixture shutdown.

    async def test_recording_failure_during_a_device_step_stops_it(self):
        self.machine.studio.auto_complete = False
        await self.service.start(WATER, "disk", True)
        await until(lambda: POUR in self.machine.codes())
        self.service.recorder.error = OSError("disk failure")
        done = await self.finish()
        self.assertEqual(done["status"], "unknown")
        self.assertIn(4507, self.machine.codes())
        self.service.recorder.error = None

    async def test_failed_setup_disconnects_and_allows_a_fresh_session(self):
        machine = self.machine

        class FailingRecorder(Recorder):
            failed = False

            async def flush(self):
                await super().flush()
                if machine.sent and not self.failed:
                    FailingRecorder.failed = True
                    raise RuntimeError("Post-handshake storage failure")

        with patch("xbloom_studio.application.Recorder", FailingRecorder):
            with self.assertRaisesRegex(RuntimeError, "storage"):
                await self.service.connect()
        self.assertIsNone(self.service.session)
        self.assertEqual(self.machine.calls[-1], "disconnect")
        await self.service.connect()
        self.assertTrue(self.service.state()["machine"]["machine"]["connected"])

    async def test_failed_stop_leaves_the_outcome_unknown(self):
        await self.service.connect()

        async def broken():
            raise ConnectionError("stop unavailable")

        original = self.service.session.stop
        self.service.session.stop = broken
        run_step = self.service._run_step

        async def fail_after_launch(*args, **kwargs):
            await run_step(*args, **kwargs)
            raise RuntimeError("boom")

        with patch.object(self.service, "_run_step", side_effect=fail_after_launch):
            await self.service.start(WATER, "boom", True)
            done = await self.finish()
        self.service.session.stop = original
        self.assertEqual(done["status"], "unknown")
        self.assertEqual(done["result"]["error"], "boom")
        self.assertNotIn("stop_result", done["result"])


class CallerLossAndRecoveryTests(ServiceCase):
    async def test_operation_continues_after_its_caller_is_cancelled(self):
        self.machine.studio.auto_complete = False
        caller = asyncio.create_task(self.service.start(WATER, "lost", True))
        await until(lambda: self.store.operation_by_key("lost") is not None, 2)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        await until(lambda: POUR in self.machine.codes(), 2)
        self.machine.emit(40511)
        self.machine.state(1)
        done = await self.finish()
        self.assertEqual(done["status"], "completed")
        again = await self.service.start(WATER, "lost", True)
        self.assertEqual(again["id"], done["id"])
        self.assertEqual(self.machine.codes().count(POUR), 1)

    async def test_restart_recovers_interrupted_work_as_unknown_without_replay(self):
        plan = self.service.prepare(WATER)
        old, _ = self.store.create_operation(
            "00:00:5E:00:53:01", WATER, plan, "restart"
        )
        self.store.finish(old["id"], "running")
        result = await self.service.start(WATER, "restart", True)
        self.assertEqual(result["id"], old["id"])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("do not retry", result["result"]["reason"])
        self.assertEqual(self.machine.calls, [])
        kinds = self.kinds(old["id"])
        self.assertIn("operation.recovered_unknown", kinds)
        with self.assertRaises(ConflictError):
            self.store.finish(old["id"], "completed", {})

    def block_append(self, kind):
        """Hold the journal writer thread before it commits ``kind``."""
        # Finalization, not link timing, is under test: no fsync-bound expiry.
        self.service.options.update(ack_timeout=5, write_timeout=10)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        append = self.store.append

        def blocked(event_kind, *args, **kwargs):
            if event_kind == kind and not release.is_set():
                entered.set()
                release.wait(5)
            return append(event_kind, *args, **kwargs)

        self.store.append = blocked
        return entered, release

    async def test_stop_joins_an_operation_already_finalizing(self):
        entered, release = self.block_append("operation.finished")
        op = await self.service.start(WATER, "final-stop", True)
        await until(entered.is_set, 2)
        task = self.service.task
        stopper = asyncio.create_task(self.service.stop(op["id"]))
        await asyncio.sleep(0.05)
        self.assertFalse(stopper.done())
        release.set()
        stopped = await asyncio.wait_for(stopper, 5)
        self.assertEqual(stopped["status"], "completed", stopped["result"])
        self.assertFalse(task.cancelled())
        self.assertIsNone(self.service.recorder.operation_id)
        self.assertIn("operation.completed", self.kinds(op["id"]))

    async def test_cancelling_a_finalizing_worker_still_persists_its_outcome(self):
        entered, release = self.block_append("operation.finished")
        op = await self.service.start(WATER, "final-cancel", True)
        await until(entered.is_set, 2)
        task = self.service.task
        task.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        release.set()
        await asyncio.wait({task}, timeout=5)
        self.assertEqual(self.store.operation(op["id"])["status"], "completed")
        self.assertIsNone(self.service.recorder.operation_id)
        with self.assertRaises(BusyError):
            await self.service.stop(op["id"])

    async def test_close_during_finalization_persists_then_releases_ownership(self):
        entered, release = self.block_append("operation.finished")
        op = await self.service.start(WATER, "final-close", True)
        await until(entered.is_set, 2)
        closer = asyncio.create_task(self.service.close())
        await asyncio.sleep(0.05)
        closer.cancel()  # A further cancellation must not cut persistence short.
        await asyncio.sleep(0.05)
        self.assertFalse(closer.done())
        self.assertTrue(self.service.lease.held)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(closer, 5)
        self.assertEqual(self.store.operation(op["id"])["status"], "completed")
        self.assertFalse(self.service.lease.held)
        self.assertIsNone(self.service.session)
        self.assertEqual(self.kinds()[-1], "session.closed")
        successor = self.make(Machine("00:00:5E:00:53:01"))
        try:
            await successor.connect()
            self.assertEqual(self.store.operation(op["id"])["status"], "completed")
        finally:
            await successor.close()

    async def test_cancelled_recovery_drains_before_a_successor_owns(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        recover = self.store.recover

        def slow_recover():
            entered.set()
            release.wait(5)
            return recover()

        self.store.recover = slow_recover
        first = asyncio.create_task(self.service.connect())
        await until(entered.is_set, 2)
        first.cancel()
        await asyncio.sleep(0.05)
        machine = Machine("00:00:5E:00:53:01")
        other = Store(self.root / "xbloom-studio.sqlite3")
        successor = self.make(machine, store=other)
        try:
            started = None
            try:
                started = await successor.start(WATER, "successor", True)
            except ConnectionError:
                pass  # The cancelled owner keeps its lease until recovery ends.
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(first, 5)
            self.assertFalse(self.service.lease.held)
            self.assertIsNone(self.service.session)
            if started is None:
                started = await successor.start(WATER, "successor", True)
            await until(lambda: POUR in machine.codes(), 2)
            # The earlier recovery never reaches the successor's active row.
            self.assertEqual(other.operation(started["id"])["status"], "running")
            self.assertNotIn("operation.recovered_unknown", self.kinds(started["id"]))
            machine.emit(40511)
            machine.state(1)
            await asyncio.wait_for(asyncio.shield(successor.task), 5)
            done = other.operation(started["id"])
            self.assertEqual(done["status"], "completed")
            self.assertEqual(machine.codes().count(POUR), 1)
        finally:
            release.set()
            await successor.close()
            other.close()

    async def test_closed_service_start_cannot_reacquire_ownership(self):
        await self.service.connect()
        await self.service.close()
        with self.assertRaisesRegex(BusyError, "closed"):
            await self.service.start(WATER, "closed", True)
        with self.assertRaisesRegex(BusyError, "closed"):
            await self.service.start_uploaded("token", "closed-upload", True)
        self.assertFalse(self.service.lease.held)
        self.assertFalse(self.service.owned)
        rival = self.make(Machine("00:00:5E:00:53:02"))
        try:
            await rival.connect()
            self.assertTrue(rival.owned)
        finally:
            await rival.close()

    async def test_one_owner_per_database_and_per_machine(self):
        await self.service.connect()
        rival = self.make(Machine("00:00:5E:00:53:02"))
        try:
            with self.assertRaisesRegex(ConnectionError, "owns"):
                await rival.connect()
        finally:
            await rival.close()
        other_store = Store(self.root / "other.sqlite3")
        alias = self.make(Machine("00-00-5e-00-53-01"), store=other_store)
        try:
            with self.assertRaises(ConnectionError):
                await alias.connect()
            self.assertIsNone(alias.session)
        finally:
            await alias.close()
            other_store.close()


class UploadTests(ServiceCase):
    async def test_upload_reserves_until_one_start_and_never_claims_readback(self):
        prepared = await self.service.upload(NATIVE, True)
        self.assertFalse(prepared["readback_verified"])
        self.assertEqual(prepared["status"], "prepared")
        with self.assertRaises(BusyError):
            await self.service.start(WATER, "blocked", True)
        with self.assertRaises(BusyError):
            await self.service.upload(NATIVE, True)
        with self.assertRaises(ConflictError):
            await self.service.start_uploaded("wrong", "k", True)
        op = await self.service.start_uploaded(prepared["token"], "uploaded", True)
        self.assertEqual(
            op["request"], {"kind": "uploaded", "token": prepared["token"]}
        )
        self.assertIsNone(self.service.prepared)
        done = await self.finish()
        self.assertEqual(done["status"], "completed")
        replay = await self.service.start_uploaded(prepared["token"], "uploaded", True)
        self.assertEqual(replay["id"], op["id"])
        with self.assertRaises(ConflictError):
            await self.service.start_uploaded("other-token", "uploaded", True)
        self.assertEqual(self.machine.codes().count(8002), 1)

    async def test_disconnect_invalidates_the_token_and_cancel_releases_it(self):
        prepared = await self.service.upload(NATIVE, True)
        cancelled = await self.service.cancel_uploaded(prepared["token"], True)
        self.assertTrue(cancelled["cancelled"])
        with self.assertRaises(ConflictError):
            await self.service.cancel_uploaded(prepared["token"], True)
        prepared = await self.service.upload(NATIVE, True)
        await self.service.disconnect()
        with self.assertRaises(ConflictError):
            await self.service.start_uploaded(prepared["token"], "late", True)
        self.assertIsNone(self.store.operation_by_key("late"))

    async def test_machine_invalidation_is_reported_and_blocks_the_start(self):
        prepared = await self.service.upload(NATIVE, True)
        self.service.session._invalidate_preparation("machine_changed")
        state = self.service.state()["prepared"]
        self.assertEqual(state["status"], "invalidated")
        self.assertEqual(state["reason"], "machine_changed")
        with self.assertRaisesRegex(ConflictError, "no longer valid"):
            await self.service.start_uploaded(prepared["token"], "stale", True)
        self.assertIsNone(self.store.operation_by_key("stale"))
        before = list(self.machine.codes())
        result = await self.service.cancel_uploaded(prepared["token"], True)
        self.assertEqual(result, {"token": prepared["token"], "cancelled": False})
        self.assertIsNone(self.service.state()["prepared"])
        self.assertIsNone(self.service._prepared_plan)
        self.assertEqual(self.machine.codes(), before)

    async def test_upload_admission_and_failure(self):
        for kwargs in ({"allow_control": False}, {"allow_untested": "yes"}):
            with self.assertRaises(ValueError):
                await self.service.upload(NATIVE, **{"allow_control": True, **kwargs})
        with self.assertRaisesRegex(ValueError, "exactly one native"):
            await self.service.upload(WATER, True)
        with self.assertRaises(ConflictError):
            await self.service.upload(NATIVE, True, expected_recipe_sha256="0")
        tea = self.service.prepare(
            {
                "kind": "recipe",
                "document": {
                    "name": "T",
                    "mode": "tea",
                    "tea": {
                        "dose_g": 3,
                        "steeps": [
                            {"volume_ml": 60, "steep_seconds": 30, "temperature": 90}
                        ],
                    },
                },
            }
        )
        self.assertTrue(tea["requires_untested"])
        self.machine.fail_commands[8004] = ConnectionError("upload lost")
        with self.assertRaises(ConnectionError):
            await self.service.upload(NATIVE, True)
        self.assertTrue(self.service.session._uncertain)
        await self.service.recorder.flush()
        self.assertEqual(self.kinds()[-1], "recipe.upload_failed")
        for args in (("t", "k", False), ("", "k", True), ("t", " ", True)):
            with self.assertRaises(ValueError):
                await self.service.start_uploaded(*args)
        with self.assertRaises(ValueError):
            await self.service.cancel_uploaded("t", False)


class CommandTests(ServiceCase):
    async def test_parameter_queries_require_control_without_experimental_opt_in(self):
        commands = self.service.capabilities()["commands"]
        for name, expected in (("read_pour_radius", 700), ("read_vibration", 1300)):
            with self.subTest(name=name):
                self.assertTrue(commands[name]["requires_allow_control"])
                self.assertFalse(commands[name]["requires_allow_untested"])
                with self.assertRaisesRegex(ValueError, "allow_control"):
                    await self.service.command(name)
                result = await self.service.command(name, allow_control=True)
                self.assertTrue(result["readback_verified"])
                self.assertEqual(result["value"], expected)

    async def test_vibration_level_requires_control_without_experimental_opt_in(self):
        command = self.service.capabilities()["commands"]["set_vibration_level"]
        self.assertTrue(command["requires_allow_control"])
        self.assertFalse(command["requires_allow_untested"])
        with self.assertRaisesRegex(ValueError, "allow_control"):
            await self.service.command("set_vibration_level", {"level": 1})
        result = await self.service.command(
            "set_vibration_level", {"level": 1}, allow_control=True
        )
        self.assertTrue(result["acknowledged"])
        self.assertEqual(self.machine.payloads(11509), [u32(1100)])

    async def test_command_journals_request_then_intent_then_result(self):
        result = await self.service.command("set_brightness", {"level": 5}, True)
        self.assertTrue(result["acknowledged"])
        self.assertFalse(result["readback_verified"])
        kinds = self.kinds()
        requested = kinds.index("machine.command_requested")
        self.assertEqual(kinds[requested + 1], "command.intent")
        self.assertEqual(kinds[-1], "machine.command_finished")

    async def test_command_admission(self):
        cases = [
            (("set_brightness", {"level": 5}, False), {}),
            (("set_brightness", {"level": 5}, True), {"allow_untested": "yes"}),
            (("set_cup_limits", {"minimum": 40, "maximum": 80}, True), {}),
            (("unknown", {}, True), {}),
            (("set_brightness", [], True), {}),
            (("set_brightness", {"level": 99}, True), {}),
            (("set_brightness", {"level": 5, "backup_path": "/x"}, True), {}),
        ]
        for args, kwargs in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.service.command(*args, **kwargs)
        self.assertEqual(self.machine.calls, [])

    async def test_recipe_configuration_requires_opt_in_on_each_experimental_call(self):
        await self.service.connect(allow_untested=True)
        for settings in ({"bypass_ml": 10}, {"cup_limits": [40, 80]}):
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(ValueError, "allow_untested"):
                    await self.service.command(
                        "configure_recipe", {"dose_g": 15, **settings}, True
                    )
        self.assertNotIn(8102, self.machine.codes())
        self.assertNotIn(8104, self.machine.codes())
        await self.service.command("configure_recipe", {"dose_g": 15}, True)
        self.assertIn(8102, self.machine.codes())
        self.assertNotIn(8104, self.machine.codes())

    async def test_preset_backup_is_caller_supplied_and_never_readback(self):
        presets = [{"slot": s, "payload_hex": "414141", "flags": 4} for s in "ABC"]
        backup = [{"slot": s, "payload_hex": "6f6c64", "flags": 1} for s in "ABC"]
        self.assertTrue(
            self.service.capabilities()["commands"]["write_presets"][
                "requires_allow_untested"
            ]
        )
        await self.service.connect(allow_untested=True)
        with self.assertRaisesRegex(ValueError, "allow_untested"):
            await self.service.command(
                "write_presets", {"presets": presets, "backup": backup}, True
            )
        self.assertEqual(self.machine.codes(), [8100])
        self.assertFalse((self.root / "preset-backups").exists())
        result = await self.service.command(
            "write_presets",
            {"presets": presets, "backup": backup},
            True,
            allow_untested=True,
        )
        self.assertFalse(result["readback_verified"])
        path = Path(result["backup"]["path"])
        self.assertEqual(path.parent, self.root / "preset-backups")
        saved = json.loads(path.read_text())
        self.assertEqual(saved["kind"], "caller_supplied_preset_backup")
        with self.assertRaises(ValueError):
            await self.service.command(
                "write_preset",
                {"preset": presets[0], "backup": backup[0]},
                True,
            )

    async def test_incomplete_preset_write_reports_recovery_without_retry(self):
        self.machine.overrides[11510] = lambda p: [reply(11510, b"", 0xC2)]
        result = await self.service.command(
            "write_preset",
            {
                "preset": {"slot": "B", "payload_hex": "6e6577", "flags": 2},
                "backup": {"slot": "B", "payload_hex": "6f6c64", "flags": 2},
                "save_timeout": 0.05,
            },
            True,
            allow_untested=True,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["type"], "PresetWriteError")
        self.assertIn("backup", result["details"])
        self.assertIsInstance(result["recovery_required"], bool)
        self.assertEqual(self.machine.codes().count(11510), 3)
        self.assertEqual(result["details"]["outcome"], "unconfirmed")
        self.assertEqual(
            {k: result["attempt"][k] for k in ("outcome", "state", "attempts_used")},
            {"outcome": "unconfirmed", "state": "retryable", "attempts_used": 1},
        )

    async def test_failing_command_is_recorded(self):
        self.machine.fail_commands[8103] = ConnectionError("gone")
        with self.assertRaises(ConnectionError):
            await self.service.command("set_brightness", {"level": 5}, True)
        await self.service.recorder.flush()
        kinds = self.kinds()
        self.assertEqual(kinds[-2:], ["command.write_failed", "machine.command_failed"])

    async def test_queued_write_is_rejected_while_the_guard_is_held(self):
        async with self.service.guard:
            with self.assertRaises(BusyError):
                await self.service.command("back_home", {}, True)
            with self.assertRaises(BusyError):
                await self.service.start(WATER, "queued", True)

    async def test_closed_service_refuses_commands(self):
        await self.service.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.service.command("back_home", {}, True)


A = {"slot": "A", "payload_hex": "6e6577", "flags": 4}


class PresetRetryTests(ServiceCase):
    """Bounded identical repeats of single-slot writes, from the journal."""

    async def write(self, preset=A, **params):
        return await self.service.command(
            "write_preset", {"preset": preset, **params}, True, allow_untested=True
        )

    def frames(self):
        return self.machine.codes().count(11510)

    def latest(self, slot="A"):
        return self.service.preset_writes(slot)["writes"][0]

    def journal(self, kind, **data):
        self.store.append(kind, data, device=self.service.device)

    async def test_rejected_write_repeats_identically_in_a_fresh_session(self):
        self.machine.studio.reject_preset_attempts = 1
        first = await self.write()
        attempt = first["attempt"]
        self.assertEqual(first["status"], "failed")
        self.assertEqual(
            [attempt[k] for k in ("outcome", "state", "attempts_used")],
            ["rejected", "retryable", 1],
        )
        self.assertEqual((attempt["remaining_attempts"], attempt["preset"]), (2, A))
        self.assertTrue(attempt["retry_allowed"])
        with self.assertRaisesRegex(SessionError, "fresh session"):
            await self.write()
        await self.service.disconnect()
        # A fresh caller finds the exact contents and count without connecting.
        self.assertEqual(self.latest()["preset"], A)
        second = await self.write()
        self.assertTrue(second["saved_observed"])
        self.assertFalse(second["readback_verified"])
        self.assertEqual(second["attempt"]["write_id"], attempt["write_id"])
        self.assertEqual(
            [second["attempt"][k] for k in ("outcome", "state", "attempts_used")],
            ["saved", "saved", 2],
        )
        self.assertEqual(self.frames(), 6)
        # Every preset frame belongs to an attempt committed before it.
        open_attempt = False
        for event in self.store.events(limit=10000):
            if event["kind"] == "preset_write.started":
                open_attempt = True
            elif event["kind"] == "preset_write.finished":
                open_attempt = False
            elif (
                event["kind"] == "command.intent" and event["data"]["command"] == 11510
            ):
                self.assertTrue(open_attempt)
        await self.service.disconnect()
        with self.assertRaisesRegex(PresetWriteRefused, "already observed a save"):
            await self.write()
        self.assertEqual(self.frames(), 6)
        other = await self.write({**A, "payload_hex": "6f74686572"})
        self.assertNotEqual(other["attempt"]["write_id"], attempt["write_id"])

    async def test_exhausted_write_needs_an_acknowledged_new_write(self):
        self.machine.studio.reject_preset_attempts = 3
        for _ in range(3):
            result = await self.write()
            await self.service.disconnect()
        attempt = result["attempt"]
        self.assertEqual(
            [attempt[k] for k in ("state", "attempts_used", "remaining_attempts")],
            ["exhausted", 3, 0],
        )
        self.assertFalse(attempt["retry_allowed"])
        for preset, params in (
            (A, {}),
            ({**A, "flags": 5}, {}),
            (A, {"acknowledge_incomplete": "0" * 32}),
        ):
            with self.subTest(params=params), self.assertRaises(PresetWriteRefused):
                await self.write(preset, **params)
        self.assertEqual(self.frames(), 9)
        self.assertEqual(self.machine.calls.count("connect"), 3)
        result = await self.write(acknowledge_incomplete=attempt["write_id"])
        self.assertTrue(result["saved_observed"])
        self.assertNotEqual(result["attempt"]["write_id"], attempt["write_id"])
        self.assertEqual(result["attempt"]["attempts_used"], 1)
        # The acknowledgment is part of the new write's first record.
        self.assertNotIn("preset_write.acknowledged", self.kinds())
        started = [e for e in self.store.events(limit=10000) if e["kind"] == STARTED]
        self.assertEqual(started[-1]["data"]["acknowledges"], attempt["write_id"])

    async def test_incomplete_write_blocks_only_its_own_slot(self):
        self.machine.studio.reject_preset_attempts = 1
        await self.write()
        await self.service.disconnect()
        with self.assertRaisesRegex(PresetWriteRefused, "different contents") as caught:
            await self.write({**A, "flags": 5})
        detail = error_detail(caught.exception)
        self.assertEqual(detail["code"], "conflict")
        self.assertEqual(detail["details"]["preset_write"]["state"], "retryable")
        result = await self.write({**A, "slot": "B"})
        self.assertEqual(result["attempt"]["state"], "saved")
        await self.service.disconnect()
        await self.service.command("set_brightness", {"level": 5}, True)
        self.assertEqual(self.latest()["state"], "retryable")

    async def test_attempts_without_an_outcome_count_after_restart(self):
        for attempt_id in ("2" * 32, "3" * 32):
            self.journal(
                "preset_write.started", write_id="1" * 32, attempt_id=attempt_id, **A
            )
        self.store.append(
            "preset_write.started",
            {"write_id": "4" * 32, "attempt_id": "5" * 32, **A},
            device="ANOTHER-MACHINE",
        )
        await self.service.close()
        self.service = self.make(self.machine)
        write = self.latest()
        self.assertEqual(
            [a["outcome"] for a in write["attempts"]], ["interrupted", "interrupted"]
        )
        self.assertEqual(
            (write["state"], write["remaining_attempts"]), ("retryable", 1)
        )
        self.machine.studio.reject_preset_attempts = 1
        result = await self.write()
        self.assertEqual(result["attempt"]["write_id"], "1" * 32)
        self.assertEqual(result["attempt"]["state"], "exhausted")

    async def test_cancelled_attempt_stops_automatic_repeats(self):
        self.machine.block_code = 11510
        task = asyncio.create_task(self.write())
        await until(lambda: self.machine.blocked.is_set(), 5)
        self.assertEqual(self.latest()["state"], "in_progress")
        task.cancel()
        self.machine.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.machine.block_code = None
        await self.service.recorder.flush()
        write = self.latest()
        self.assertEqual(
            (write["state"], write["attempts"][-1]["outcome"]),
            ("cancelled", "cancelled"),
        )
        await self.service.disconnect()
        with self.assertRaisesRegex(PresetWriteRefused, "cancelled"):
            await self.write()
        result = await self.write(acknowledge_incomplete=write["write_id"])
        self.assertTrue(result["saved_observed"])

    async def test_a_repeat_needs_a_current_idle_report(self):
        self.machine.studio.reject_preset_attempts = 1
        await self.write()
        await self.service.disconnect()
        await self.service.connect(allow_untested=True)
        session = self.service.session
        self.machine.state(0x44)
        with self.assertRaisesRegex(PresetWriteRefused, "current idle"):
            await self.write()
        self.machine.state(1)
        observed = session.state.observations["machine_state_code"]
        session.state.observations["machine_state_code"] = dataclasses.replace(
            observed, monotonic_at=observed.monotonic_at - 60
        )
        with self.assertRaisesRegex(PresetWriteRefused, "current idle"):
            await self.write()
        self.assertEqual((self.frames(), self.latest()["attempts_used"]), (3, 1))
        self.machine.state(1)
        result = await self.write()
        self.assertEqual(result["attempt"]["attempts_used"], 2)

    async def test_an_unreadable_journal_refuses_preset_writes(self):
        self.journal(
            "preset_write.finished",
            write_id="1" * 32,
            attempt_id="2" * 32,
            slot="A",
            outcome="saved",
        )
        with self.assertRaisesRegex(PresetWriteRefused, "not readable"):
            self.service.preset_writes()
        with self.assertRaises(PresetWriteRefused):
            await self.write()
        self.assertEqual(self.machine.calls, [])

    async def test_backup_is_optional_and_batches_and_opt_ins_are_unchanged(self):
        with self.assertRaisesRegex(ValueError, "allow_untested"):
            await self.service.command("write_preset", {"preset": A}, True)
        with self.assertRaisesRegex(ValueError, "allow_control"):
            await self.service.command(
                "write_preset", {"preset": A}, False, allow_untested=True
            )
        with self.assertRaises(ValueError):
            self.service.preset_writes("D")
        self.assertEqual(self.machine.calls, [])
        result = await self.write()
        self.assertIsNone(result["backup"])
        self.assertFalse((self.root / "preset-backups").exists())
        await self.service.disconnect()
        b, old = {**A, "slot": "B"}, {**A, "slot": "B", "payload_hex": "6f6c64"}
        result = await self.write(b, backup=old)
        saved = json.loads(Path(result["backup"]["path"]).read_text())
        self.assertEqual(saved["presets"], [old])
        await self.service.disconnect()
        presets = [{**A, "slot": slot} for slot in "ABC"]
        with self.assertRaises(ValueError):
            await self.service.command(
                "write_presets", {"presets": presets}, True, allow_untested=True
            )
        result = await self.service.command(
            "write_presets",
            {"presets": presets, "backup": presets},
            True,
            allow_untested=True,
        )
        self.assertNotIn("attempt", result)
        writes = self.service.preset_writes()["writes"]
        self.assertEqual(
            [(w["preset"]["slot"], w["attempts_used"], w["state"]) for w in writes],
            [("A", 1, "superseded"), ("B", 1, "superseded")],
        )

    def respond_then(self, code, action):
        """Respond to ``code`` as simulated; run ``action`` on the third."""
        count = []

        def respond(packet):
            count.append(packet)
            if len(count) == 3:
                action()
            return self.machine.studio(packet)

        self.machine.overrides[code] = respond

    async def test_a_journaled_save_blocks_a_repeat_after_a_restart(self):
        # The save arrives; the home frame that follows blocks after its intent
        # committed everything queued before it, then the owner dies.
        self.respond_then(11510, lambda: setattr(self.machine, "block_code", 8022))
        task = asyncio.create_task(self.write())
        await until(lambda: self.machine.blocked.is_set(), 5)
        self.service.lease.release()  # The process is gone; FINISHED never commits.
        successor = self.make(Machine("00:00:5E:00:53:01"))
        try:
            write = successor.preset_writes("A")["writes"][0]
            self.assertEqual(write["attempts"][0]["outcome"], "saved_interrupted")
            self.assertEqual((write["state"], write["retry_allowed"]), ("saved", False))
            with self.assertRaisesRegex(PresetWriteRefused, "already observed a save"):
                await successor.command(
                    "write_preset", {"preset": A}, True, allow_untested=True
                )
            self.assertEqual(successor.connector.calls, [])
        finally:
            await successor.close()
            self.service.lease.acquire()
            self.machine.release.set()
            await task

    async def test_cancelled_preparation_stops_automatic_repeats(self):
        self.machine.block_code = 11511
        task = asyncio.create_task(self.write())
        await until(lambda: self.machine.blocked.is_set(), 5)
        task.cancel()
        self.machine.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The outcome committed before the guard admitted this reader.
        write = self.latest()
        self.assertEqual(self.frames(), 0)
        self.assertEqual(
            (write["state"], write["attempts"][-1]["outcome"], write["retry_allowed"]),
            ("cancelled", "cancelled", False),
        )
        self.assertEqual(write["attempts"][-1]["transmitted_write_count"], 0)

    async def test_cancellation_while_the_attempt_commits_is_cancelled(self):
        append, entered, gate = self.store.append, threading.Event(), threading.Event()

        def slow(kind, data, **context):
            if kind == STARTED:
                entered.set()
                gate.wait(5)
            return append(kind, data, **context)

        with patch.object(self.store, "append", slow):
            task = asyncio.create_task(self.write())
            await until(entered.is_set, 5)
            task.cancel()
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        write = self.latest()
        self.assertEqual(self.frames(), 0)
        self.assertEqual((write["state"], write["attempts_used"]), ("cancelled", 1))

    async def test_the_ledger_is_read_only_under_the_database_lease(self):
        await self.service.connect()
        other = self.make(Machine("00:00:5E:00:53:01"))
        reads = []
        ledger = other._preset_ledger

        def read():
            reads.append(other.lease.held)
            return ledger()

        other._preset_ledger = read
        try:
            with self.assertRaises(LeaseBusyError):
                await other.command(
                    "write_preset", {"preset": A}, True, allow_untested=True
                )
            self.assertEqual((reads, other.connector.calls), ([], []))
            await self.service.close()
            result = await other.command(
                "write_preset", {"preset": A}, True, allow_untested=True
            )
            self.assertEqual(result["attempt"]["state"], "saved")
            self.assertEqual(reads[0], True)
        finally:
            await other.close()

    async def batch(self, *slots):
        presets = [{**A, "slot": slot, "payload_hex": "62"} for slot in slots]
        return await self.service.command(
            "write_presets",
            {"presets": presets, "backup": presets},
            True,
            allow_untested=True,
        )

    async def test_a_batch_supersedes_saved_writes_before_its_first_frame(self):
        await self.write()
        await self.service.disconnect()
        # A partial batch: its first preset frame fails.
        self.machine.fail_commands[11510] = ConnectionError("lost mid-batch")
        result = await self.batch("A", "B", "C")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["details"]["transmitted_write_count"], 1)
        kinds = self.kinds()
        self.assertLess(
            kinds.index("preset_write.superseded"),
            max(i for i, k in enumerate(kinds) if k == "command.intent"),
        )
        self.assertEqual(self.latest()["state"], "superseded")
        del self.machine.fail_commands[11510]
        await self.service.disconnect()
        # The identical write is not "already saved" any more.
        result = await self.write()
        self.assertEqual(result["attempt"]["state"], "saved")
        self.assertEqual(result["attempt"]["attempts_used"], 1)

    async def test_a_batch_never_overwrites_an_unresolved_write(self):
        self.machine.studio.reject_preset_attempts = 1
        await self.write()
        await self.service.disconnect()
        connects, frames = self.machine.calls.count("connect"), self.frames()
        with self.assertRaisesRegex(PresetWriteRefused, "batch would overwrite"):
            await self.batch("A", "B", "C")
        self.assertEqual(self.machine.calls.count("connect"), connects)
        self.assertEqual(self.frames(), frames)
        write = self.latest()
        self.assertEqual((write["state"], write["retry_allowed"]), ("retryable", True))

    async def test_a_replacement_that_sends_nothing_keeps_the_old_write(self):
        self.machine.studio.reject_preset_attempts = 3
        for _ in range(3):
            exhausted = (await self.write())["attempt"]
            await self.service.disconnect()
        self.machine.fail_commands[8022] = ConnectionError("lost before writing")
        other = {**A, "flags": 5}
        result = await self.write(other, acknowledge_incomplete=exhausted["write_id"])
        self.assertEqual(result["attempt"]["outcome"], "refused")
        del self.machine.fail_commands[8022]
        await self.service.disconnect()
        write = self.latest()
        self.assertEqual(
            (write["write_id"], write["state"]), (exhausted["write_id"], "exhausted")
        )
        for preset in (other, A):
            with self.subTest(preset=preset), self.assertRaises(PresetWriteRefused):
                await self.write(preset)
        result = await self.write(other, acknowledge_incomplete=exhausted["write_id"])
        self.assertEqual(result["attempt"]["state"], "saved")


if __name__ == "__main__":
    unittest.main()
