"""MachineService: owned scheduling, durable intent and explicit uncertainty."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.frames import reply
from tests.machine import Machine, until
from xbloom_studio.application import MachineService, RecordingConnector
from xbloom_studio.control import BusyError
from xbloom_studio.journal import Recorder
from xbloom_studio.machine_api import same_request
from xbloom_studio.storage import ConflictError, Store

FAST = {
    "settle": 0,
    "subscription_settle": 0,
    "transition_delay": 0,
    "ack_timeout": 0.2,
    "write_timeout": 0.5,
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
        self.store = Store(self.root / "bloom.sqlite3")
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
        with self.assertRaisesRegex(RuntimeError, "fresh session"):
            await self.service.start(WATER, "after-loss", True)
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
        with self.assertRaisesRegex(ValueError, "fresh session"):
            await self.service.connect(allow_untested=True)
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

    async def test_replay_with_numeric_spelling_returns_the_attempt(self):
        first = await self.service.start(WATER, "spelling", True)
        await self.finish()
        again = await self.service.start(
            {**WATER, "volume_ml": 50.0, "temperature_c": 20.0}, "spelling", True
        )
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.machine.codes().count(POUR), 1)

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

    async def test_machine_operations_need_a_configured_machine(self):
        service = MachineService(self.store)
        try:
            with self.assertRaisesRegex(ValueError, "machine address"):
                await service.start(WATER, "none", True)
            with self.assertRaisesRegex(RuntimeError, "connect first"):
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
        # The step mirrors the machine layer; possible delivery stays explicit.
        self.assertEqual(done["status"], "failed")
        step = done["result"]["steps"][0]
        self.assertEqual(step["cleanup_status"], "skipped_uncertain")
        self.assertFalse(step["start_observed"])
        self.assertTrue(done["result"]["state"]["recovery_required"])
        self.assertTrue(self.service.state()["machine"]["recovery_required"])
        kinds = self.kinds(done["id"])
        self.assertIn("command.write_failed", kinds)
        self.assertNotIn("operation.completed", kinds)

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
        with patch.object(
            MachineService, "_run_step", side_effect=RuntimeError("boom")
        ):
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

    async def test_preset_backup_is_caller_supplied_and_never_readback(self):
        presets = [{"slot": s, "payload_hex": "414141", "flags": 4} for s in "ABC"]
        backup = [{"slot": s, "payload_hex": "6f6c64", "flags": 1} for s in "ABC"]
        result = await self.service.command(
            "write_presets", {"presets": presets, "backup": backup}, True
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
        self.machine.overrides[11510] = lambda p: [reply(11510, p.payload, 0xC2)]
        result = await self.service.command(
            "write_preset",
            {
                "preset": {"slot": "B", "payload_hex": "6e6577", "flags": 2},
                "backup": {"slot": "B", "payload_hex": "6f6c64", "flags": 2},
            },
            True,
            allow_untested=True,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["type"], "PresetWriteError")
        self.assertIn("backup", result["details"])
        self.assertIsInstance(result["recovery_required"], bool)
        self.assertEqual(self.machine.codes().count(11510), 3)

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


if __name__ == "__main__":
    unittest.main()
