"""Interaction lifecycle and actuator failure tests with a simulated peripheral."""

import asyncio
import struct
import time
import unittest

from tests.frames import frame, identity_block, reply, u32
from tests.machine import Machine, blocked_write, interaction, transmitted, until
from xbloom_studio import (
    BusyError,
    EncodedRecipe,
    Interaction,
    ReadOnlyClient,
    SessionError,
    Studio,
    UnsupportedStageError,
    UntestedOperationError,
)
from xbloom_studio.protocol import decode


class Hooks:
    def __init__(self, session=None, fail=()):
        self.session = session
        self.fail = set(fail)
        self.log = []

    async def before_write(self, packet, frame_bytes):
        prepared = self.session._prepared if self.session else None
        self.log.append(("before", packet.command, prepared is not None))
        if packet.command in self.fail or "all" in self.fail:
            raise OSError("journal unavailable")

    def write_completed(self, packet):
        self.log.append(("completed", packet.command))

    def write_failed(self, packet, error):
        self.log.append(("failed", packet.command))


class FrameFake:
    """Transport-level fake that replies like the simulated machine."""

    def __init__(self):
        self.machine = Machine("frame-fake")
        self.frames = []
        self.callback = None

    async def connect(self, on_disconnect):
        await self.machine.connect(on_disconnect)

    async def subscribe(self, callback):
        await self.machine.start_notify(callback)

    async def unsubscribe(self):
        pass

    async def open_session(self):
        await self.machine.write(frame(8100, bytes.fromhex("b900000001000000")))

    async def read_information(self, name):
        return None

    async def disconnect(self):
        await self.machine.disconnect()

    async def _write_frame(self, raw):
        self.frames.append(raw)
        await self.machine.write(raw)


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pod_identification_is_not_operation_start_evidence(self):
        m = Machine()
        m.overrides[3500] = lambda p: [reply(3500), reply(40501, b"ABCDEF")]
        async with interaction(m) as session:
            op = await session.start_grinder(size=12, rpm=60, duration_s=0.1)
            result = await op.wait()
            self.assertFalse(result["start_observed"])
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["stop_acknowledged"])

    async def test_water_activity_is_not_grinder_start_evidence(self):
        m = Machine()
        m.overrides[3500] = lambda p: [
            reply(3500),
            reply(40502),
            reply(8023, u32(0x23)),
        ]
        async with interaction(m) as session:
            result = await (
                await session.start_grinder(size=12, rpm=60, duration_s=0.1)
            ).wait()
            self.assertFalse(result["start_observed"])
            self.assertEqual(result["status"], "failed")

    async def test_unobserved_manual_start_is_not_reported_completed(self):
        m = Machine()
        m.overrides[4506] = lambda p: [reply(4506)]
        async with interaction(m, start_timeout=0.02) as session:
            result = await (await session.start_water(volume_ml=10)).wait()
            self.assertEqual(result["status"], "failed")
            self.assertIn("activity not observed", result["error"])
            self.assertFalse(result["start_observed"])
            self.assertTrue(result["stop_acknowledged"])

    async def test_held_connection_observes_water_and_multiple_operations(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=155, temperature_c=20)
            await transmitted(m, 4506)
            m.emit(20501, struct.pack("<f", 12.5))
            m.emit(40523, struct.pack("<f", 14000))
            state = session.get_state()
            self.assertEqual(state["operation"]["phase"], "pouring")
            self.assertEqual(state["machine"]["fields"]["scale_weight"]["value"], 12.5)
            self.assertEqual(
                state["machine"]["fields"]["dispensed_water_estimate"]["value"], 14
            )
            self.assertTrue(state["operation"]["start_observed"])
            # Flow, volume and temperature in tenths; tank source; centre pattern.
            start = struct.pack("<fffII", 30.0, 1550.0, 200.0, 0, 0)
            self.assertIn(frame(4506, start), b"".join(m.writes))
            self.assertEqual(m.payloads(4506), [start])
            self.assertEqual(m.payloads(8007), [u32(0) + struct.pack("<f", 200.0)])
            self.assertEqual(m.payloads(4510), [u32(200)])
            m.emit(40511)
            m.state(1)
            result = await op.wait()
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["stop_acknowledged"])
            self.assertTrue(result["stop_observed"])
            self.assertEqual(result["cleanup_status"], "completed")
            self.assertEqual(m.calls, ["connect", "start_notify"])
            op2 = await session.start_water(volume_ml=10, pattern="spiral")
            await until(lambda: m.codes().count(4506) == 2)
            await session.stop()
            self.assertEqual(op2.status, "stopped")
            self.assertEqual(m.payloads(4506)[1][-4:], u32(2))
        self.assertEqual(m.calls.count("connect"), 1)
        self.assertEqual(m.calls.count("disconnect"), 1)
        self.assertEqual(
            m.codes(),
            [8100, 8022, 11511, 8007, 4510, 4506, 4507, 8013, 8022]
            + [8022, 11511, 8007, 4510, 4506, 4507, 8013, 8022],
        )

    async def test_every_written_chunk_fits_the_link_and_reassembles(self):
        m = Machine()
        async with interaction(m) as session:
            await session.upload_recipe(EncodedRecipe(bytes(range(60)), 15))
        self.assertTrue(all(1 <= len(chunk) <= 20 for chunk in m.writes))
        expected = b"".join(
            frame(code, payload, function=2 if code == 11511 else 1)
            for code, payload in (
                (8100, bytes.fromhex("b900000001000000")),
                (8022, b""),
                (11511, b"\0" * 4),
                (8102, struct.pack("<ffI", 0, 0, 15)),
                (8004, bytes(range(60))),
            )
        )
        self.assertEqual(b"".join(m.writes), expected)

    async def test_equivalent_commands_produce_identical_bytes_on_both_paths(self):
        async def run(session):
            await session.set_brightness(8)
            op = await session.start_water(
                volume_ml=20, temperature_c=93, pattern="ring"
            )
            await session.stop()
            await op.wait()
            await session.start_recipe(EncodedRecipe(b"\x01\x02", 12))
            await session.stop()

        m = Machine()
        async with interaction(m) as session:
            await run(session)
        fake = FrameFake()
        async with Interaction(
            "frame-fake",
            transport=fake,
            allow_control=True,
            settle=0,
            subscription_settle=0,
            transition_delay=0,
            ack_timeout=0.03,
        ) as session:
            await run(session)
        self.assertEqual([p.command for p in fake.machine.sent], m.codes())
        self.assertEqual(b"".join(fake.frames), b"".join(m.writes)[20:])
        self.assertEqual(b"".join(fake.machine.writes), b"".join(m.writes))

    async def test_telemetry_cannot_be_disabled_during_operation(self):
        m = Machine()
        async with interaction(m) as session:
            await session.start_water(volume_ml=10)
            with self.assertRaises(BusyError):
                await session.unsubscribe()
            self.assertNotIn("stop_notify", m.calls)
            await session.stop()
            await session.upload_recipe(EncodedRecipe(b"x", 10))
            with self.assertRaises(BusyError):
                await session.unsubscribe()

    async def test_unsubscribe_when_idle(self):
        m = Machine()
        async with interaction(m) as session:
            await session.unsubscribe()
            self.assertIn("stop_notify", m.calls)
            with self.assertRaises(SessionError):
                await session.start_water(volume_ml=10)

    async def test_physical_pause_can_be_resumed_via_same_session(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_grinder(size=12, rpm=60, duration_s=1)
            await transmitted(m, 3500)
            m.state(2)
            self.assertEqual(op.phase, "paused")
            with self.assertRaises(BusyError):
                await session.pause()
            await session.resume()
            self.assertIn(8020, m.codes())
            m.emit(9009)
            self.assertEqual(op.phase, "paused")
            m.emit(9003, u32(1000) + u32(60))
            self.assertEqual(op.phase, "grinding")
            await session.pause()
            self.assertEqual(m.codes().count(8018), 1)

    async def test_water_pause_and_brewer_events(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            m.emit(9010)
            self.assertEqual(op.phase, "paused")
            await session.resume()
            m.emit(9005, u32(100) + u32(200) + u32(0))
            self.assertEqual(op.phase, "pouring")
            m.state(3)
            self.assertEqual(op.phase, "paused")
            self.assertEqual(m.codes()[-1], 8021)

    async def test_cancelled_convenience_calls_request_stop(self):
        m = Machine()
        async with interaction(m) as session:
            pouring = asyncio.create_task(session.dispense_water(volume_ml=10))
            await transmitted(m, 4506)
            pouring.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pouring
            self.assertTrue(session.operation.done)
            self.assertTrue(session.operation.stop_acknowledged)
            grinding = asyncio.create_task(session.grind(size=5, rpm=80, duration_s=5))
            await transmitted(m, 3500)
            grinding.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await grinding
            self.assertIn(3505, m.codes())

    async def test_convenience_calls_return_results(self):
        m = Machine(auto_complete=True)
        async with interaction(m) as session:
            self.assertEqual(
                (await session.dispense_water(volume_ml=10))["status"], "completed"
            )
            result = await session.grind(size=5, rpm=80, duration_s=0.1)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["stop_observed"])

    async def test_cancellation_of_owning_context_stops_before_disconnect(self):
        m = Machine()

        async def owner():
            async with interaction(m) as session:
                await (await session.start_water(volume_ml=10)).wait()

        owning = asyncio.create_task(owner())
        await transmitted(m, 4506)
        owning.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owning
        self.assertIn(4507, m.codes())
        self.assertEqual(m.calls[-1], "disconnect")

    async def test_cancelled_disconnect_still_finishes_cleanup(self):
        m = Machine()
        session = interaction(m)
        await session.__aenter__()
        await session.start_water(volume_ml=10)
        await transmitted(m, 4506)
        closing = asyncio.create_task(session.disconnect())
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertEqual(m.calls[-1], "disconnect")
        self.assertIn(4507, m.codes())

    async def test_readonly_client_cannot_compete_with_interaction(self):
        async with interaction(Machine("00:00:5E:00:53:01")):
            rival = ReadOnlyClient("00-00-5e-00-53-01", transport=object())
            with self.assertRaisesRegex(SessionError, "owns"):
                await rival.connect()

    async def test_controls_require_strict_control_optin_before_connecting(self):
        m = Machine()
        with self.assertRaisesRegex(SessionError, "allow_control"):
            async with Interaction(connector=m):
                pass
        self.assertEqual(m.calls, [])
        for kwargs in (
            {"allow_control": 1},
            {"allow_control": True, "allow_untested": "yes"},
        ):
            with self.assertRaisesRegex(ValueError, "booleans"):
                Interaction(connector=m, **kwargs)
        for kwargs in (
            {"ack_timeout": 0},
            {"write_timeout": float("nan")},
            {"transition_delay": -1},
            {"start_timeout": 61},
        ):
            with self.assertRaises(ValueError):
                Interaction(connector=m, allow_control=True, **kwargs)

    async def test_context_cannot_be_reentered(self):
        session = interaction()
        async with session:
            with self.assertRaises(BusyError):
                await session.__aenter__()
        failing = Machine()
        failing.connect_error = OSError("radio")
        session = interaction(failing)
        with self.assertRaises(SessionError):
            await session.__aenter__()
        self.assertFalse(session._entered)

    async def test_command_gate_runs_above_injected_transports(self):
        fake = FrameFake()
        for session in (
            interaction(),
            Interaction(
                "frame-fake",
                transport=fake,
                allow_control=True,
                settle=0,
                subscription_settle=0,
                transition_delay=0,
            ),
        ):
            async with session:
                link = (
                    session.transport.connector
                    if hasattr(session.transport, "connector")
                    else fake.machine
                )
                before = len(link.writes)
                for code, function in ((8101, 1), (8022, 2), (11511, 1), (8100, 1)):
                    with self.assertRaisesRegex(ValueError, "allowlist|function"):
                        await session._command(code, function=function)
                with self.assertRaises(ValueError):
                    await session._command(0x10000)
                self.assertEqual(len(link.writes), before)
                self.assertFalse(session.get_state()["recovery_required"])
        self.assertEqual(fake.frames, [])

    async def test_write_failure_reports_unknown_if_stop_cannot_be_sent(self):
        m = Machine()
        m.fail_commands = {
            4506: ConnectionError("radio"),
            4507: ConnectionError("radio"),
        }
        async with interaction(m) as session:
            result = await (await session.start_water(volume_ml=10)).wait()
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["stop_acknowledged"])
            self.assertIn("Stop unconfirmed", result["error"])
            self.assertEqual(result["cleanup_status"], "skipped_uncertain")
            self.assertTrue(session.get_state()["recovery_required"])
            with self.assertRaisesRegex(SessionError, "uncertain"):
                await session.start_water(volume_ml=10)

    async def test_wrong_mode_ack_payload_cannot_allow_start(self):
        m = Machine()
        m.overrides[11511] = lambda p: [reply(11511, bytes.fromhex("91327856"), 0xC2)]
        async with interaction(m) as session:
            result = await (await session.start_water(volume_ml=10)).wait()
            self.assertEqual(result["status"], "failed")
            self.assertNotIn(4506, m.codes())
            self.assertFalse(session.operation._armed)

    async def test_recipe_upload_settings_complete_without_changing_presets(self):
        m = Machine()
        recipe = EncodedRecipe(
            bytes(range(21)), 15, grinder_enabled=True, cup_limits=(5, 100)
        )
        async with interaction(m, allow_untested=True) as session:
            op = await session.start_recipe(recipe, timeout=1)
            await transmitted(m, 8002)
            self.assertEqual(m.codes(), [8100, 8022, 11511, 8102, 8104, 8001, 8002])
            self.assertEqual(m.payloads(8104), [struct.pack("<ff", 100, 5)])
            self.assertEqual(m.payloads(8102), [struct.pack("<ffI", 0, 0, 15)])
            m.state(0x77)
            self.assertEqual(op.phase, "unknown_machine_stage")
            with self.assertRaises(UnsupportedStageError):
                await session.advance(
                    expected_stage="prepare",
                    revision=session.get_state()["event_revision"],
                )
            m.state(0x22)
            self.assertEqual(op.phase, "grinding")
            m.emit(40520)
            self.assertEqual(op.phase, "bypass")
            m.emit(40527)
            self.assertEqual(op.phase, "before_vibration")
            m.state(0x23)
            self.assertEqual(op.phase, "pouring")
            m.state(0x24)
            result = await op.wait()
            self.assertEqual(result["status"], "completed")
            self.assertNotIn(40519, m.codes())
            self.assertNotIn(11510, m.codes())
            self.assertTrue(session.get_state()["machine"]["connected"])

    async def test_recipe_options_require_untested_optin(self):
        m = Machine()
        async with interaction(m) as session:
            for recipe in (
                EncodedRecipe(b"x", 10, grinder_enabled=True),
                EncodedRecipe(b"x", 10, set_grinder_size=True),
                EncodedRecipe(b"x", 10, bypass_ml=5),
                EncodedRecipe(b"x", 10, cup_limits=(1, 2)),
            ):
                with self.assertRaises(UntestedOperationError):
                    await session.start_recipe(recipe)
                with self.assertRaises(UntestedOperationError):
                    await session.upload_recipe(recipe)
            self.assertEqual(m.codes(), [8100])

    async def test_recipe_alternate_completion(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_recipe(EncodedRecipe(b"example", 10))
            await transmitted(m, 8002)
            m.emit(40513)
            m.state(1)
            self.assertEqual((await op.wait())["status"], "completed")

    async def test_pause_resume_and_stop_during_wait_do_not_reconnect(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_recipe(EncodedRecipe(b"example", 10))
            await transmitted(m, 8002)
            await session.pause()
            self.assertEqual(op.phase, "pause_acknowledged")
            await session.resume()
            self.assertEqual(op.phase, "resume_acknowledged")
            with self.assertRaises(BusyError):
                await session.resume()
            await asyncio.gather(session.stop(), session.stop())
            self.assertEqual(op.status, "stopped")
            self.assertEqual(m.codes().count(40519), 1)
            with self.assertRaises(BusyError):
                await session.pause()
            self.assertEqual(await session.stop(), await op.wait())
        self.assertEqual(m.calls.count("connect"), 1)

    async def test_competing_operation_and_scale_rejected(self):
        m = Machine()
        async with interaction(m) as session:
            await session.start_water(volume_ml=10)
            with self.assertRaises(BusyError):
                await session.start_grinder(size=12, rpm=60, duration_s=1)
            with self.assertRaises(BusyError):
                await session.enter_scale()
            with self.assertRaises(BusyError):
                await session.tare()
            with self.assertRaises(BusyError):
                await session.exit_scale()

    async def test_external_motion_or_unknown_state_blocks_start(self):
        m = Machine()
        async with interaction(m) as session:
            m.state(0x22)
            with self.assertRaisesRegex(BusyError, "motion"):
                await session.start_water(volume_ml=10)
            m.state(0x77)
            with self.assertRaisesRegex(BusyError, "idle"):
                await session.start_water(volume_ml=10)
            self.assertEqual(m.codes(), [8100])

    async def test_stop_during_upload_finishes_frame_and_never_executes(self):
        m = Machine()
        m.block_code = 8004
        async with interaction(m) as session:
            op = await session.start_recipe(EncodedRecipe(b"x" * 40, 10))
            try:
                await blocked_write(m, op._task)
                stopping = asyncio.create_task(session.stop())
                await asyncio.sleep(0)
                self.assertTrue(m.partial)
                self.assertNotIn(40519, m.codes())
            finally:
                m.release.set()
            await stopping
            self.assertIn(8004, m.codes())
            self.assertNotIn(8002, m.codes())
            self.assertEqual(op.status, "stopped")

    async def test_stop_during_commit_uses_same_connection(self):
        m = Machine()
        m.block_code = 8002
        async with interaction(m) as session:
            op = await session.start_recipe(EncodedRecipe(b"x" * 40, 10))
            try:
                await blocked_write(m, op._task)
                stopping = asyncio.create_task(session.stop())
                await asyncio.sleep(0)
            finally:
                m.release.set()
            result = await stopping
            self.assertEqual(m.codes()[-3:], [8002, 40519, 8022])
            self.assertTrue(result["stop_acknowledged"])
            self.assertTrue(result["stop_observed"])

    async def test_missing_start_ack_stops_and_prevents_another_start(self):
        m = Machine()
        m.studio.silent.add(4506)
        async with interaction(m) as session:
            result = await (await session.start_water(volume_ml=10)).wait()
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["stop_acknowledged"])
            self.assertEqual(result["cleanup_status"], "skipped_uncertain")
            self.assertEqual(m.codes().count(4506), 1)
            with self.assertRaisesRegex(SessionError, "uncertain"):
                await session.start_water(volume_ml=10)

    async def test_missing_stop_ack_is_unknown(self):
        m = Machine()
        m.studio.silent.add(40519)
        async with interaction(m) as session:
            await session.start_recipe(EncodedRecipe(b"example", 10))
            await transmitted(m, 8002)
            result = await session.stop()
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["stop_acknowledged"])

    async def test_uncertain_pause_requests_stop_before_returning_error(self):
        m = Machine()
        m.studio.silent.add(40518)
        async with interaction(m) as session:
            op = await session.start_recipe(EncodedRecipe(b"example", 10))
            await transmitted(m, 8002)
            with self.assertRaises(asyncio.TimeoutError):
                await session.pause()
            self.assertTrue(op.done)
            self.assertTrue(op.stop_acknowledged)
            self.assertEqual(op.status, "stopped")

    async def test_link_loss_reports_unknown_without_reconnecting(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            m.drop()
            result = await op.wait()
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["state"]["connected"])
            self.assertEqual(m.calls.count("connect"), 1)
            self.assertTrue(session.get_state()["recovery_required"])

    async def test_link_loss_fails_a_pending_command(self):
        m = Machine()
        m.studio.silent.add(8022)
        async with interaction(m, ack_timeout=1) as session:
            pending = asyncio.create_task(session.back_home())
            await transmitted(m, 8022)
            m.drop()
            with self.assertRaisesRegex(SessionError, "Connection lost"):
                await pending
            with self.assertRaises(SessionError):
                await session._command(8022)

    async def test_recipe_timeout_sends_stop(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_recipe(EncodedRecipe(b"example", 10), timeout=0.1)
            result = await op.wait()
            self.assertEqual(result["status"], "failed")
            self.assertIn("deadline", result["error"])
            self.assertTrue(result["stop_acknowledged"])
            self.assertEqual(result["cleanup_status"], "completed")
            self.assertEqual(m.codes()[-2:], [40519, 8022])

    async def test_grinder_duration_is_bounded_and_stop_observed(self):
        m = Machine()
        async with interaction(m) as session:
            result = await (
                await session.start_grinder(size=12, rpm=60, duration_s=0.1)
            ).wait()
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["start_observed"])
            self.assertTrue(result["stop_observed"])
            self.assertEqual(m.payloads(3500), [struct.pack("<III", 1000, 12, 60)])
            self.assertEqual(m.payloads(8006), [struct.pack("<II", 12, 60)])
            self.assertEqual(m.codes()[-3:], [3505, 8012, 8022])

    async def test_grinder_start_event_counts(self):
        m = Machine()
        m.overrides[3500] = lambda p: [reply(3500), reply(9003, u32(1000) + u32(60))]
        async with interaction(m) as session:
            result = await (
                await session.start_grinder(size=3, rpm=60, duration_s=0.1)
            ).wait()
            self.assertTrue(result["start_observed"])

    async def test_scale_lifecycle_keeps_connection_and_never_calibrates(self):
        m = Machine()
        async with interaction(m) as session:
            await session.enter_scale()
            await session.tare()
            await session.exit_scale()
            await session.exit_scale()  # not in scale mode: nothing sent
            self.assertEqual(m.codes(), [8100, 8022, 11511, 8003, 8500, 8014, 8022])
            self.assertTrue(session.get_state()["machine"]["connected"])
            with self.assertRaisesRegex(BusyError, "scale mode"):
                await session.tare()

    async def test_disconnect_exits_owned_scale(self):
        m = Machine()
        async with interaction(m) as session:
            await session.enter_scale()
            m.state(4)
            await session.set_brightness(3)  # exits scale before the setting
            await session.enter_scale()
        self.assertEqual(m.codes()[-1], 8014)
        self.assertEqual(m.calls[-1], "disconnect")

    async def test_invalid_inputs_cannot_send(self):
        m = Machine()
        async with interaction(m) as session:
            for kwargs in (
                {"volume_ml": float("nan")},
                {"volume_ml": -1},
                {"volume_ml": True},
                {"volume_ml": 10, "pattern": "bad"},
                {"volume_ml": 10, "flow_ml_s": 0},
                {"volume_ml": 10, "temperature_c": 100},
                {"volume_ml": 10, "timeout": 0},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await session.start_water(**kwargs)
            for kwargs in (
                {"size": 1.5, "rpm": 60, "duration_s": 1},
                {"size": 1, "rpm": 60.5, "duration_s": 1},
                {"size": 0, "rpm": 60, "duration_s": 1},
                {"size": 1, "rpm": 60, "duration_s": 121},
            ):
                with self.assertRaises(ValueError):
                    await session.start_grinder(**kwargs)
            for bad in ("B", b"raw"):
                with self.assertRaises(TypeError):
                    await session.start_recipe(bad)
                with self.assertRaises(TypeError):
                    await session.upload_recipe(bad)
            with self.assertRaises(ValueError):
                await session.wait_for_state(0, timeout=0)
            self.assertEqual(m.codes(), [8100])

    async def test_water_requires_confirmed_sufficiency_and_known_source(self):
        for info, message in (
            (identity_block(water=0), "sufficiency"),
            (identity_block(source=9), "configuration unknown"),
            (identity_block(size=41), "configuration unknown"),
        ):
            m = Machine()
            m.studio.info = info
            async with interaction(m) as session:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(SessionError, message):
                        await session.start_water(volume_ml=10)
                    with self.assertRaisesRegex(SessionError, message):
                        await session.start_recipe(EncodedRecipe(b"x", 10))
                    self.assertEqual(m.codes(), [8100])

    async def test_water_fault_preflight_and_running_fault(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            m.emit(40522, b"\0" * 4)
            result = await op.wait()
            self.assertEqual(result["status"], "failed")
            self.assertIn("fault", result["error"])
            self.assertTrue(result["stop_acknowledged"])
            with self.assertRaisesRegex(SessionError, "Water sufficiency"):
                await session.start_water(volume_ml=10)

    async def test_malformed_event_during_operation_is_not_lifecycle_data(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            m.emit(9005, b"\x01")
            self.assertEqual(op.last_machine_event["command"], 9005)
            self.assertNotIn("name", op.last_machine_event)
            m.emit(20501, struct.pack("<f", 1))  # measurements are not events
            self.assertEqual(op.last_machine_event["command"], 9005)
            await session.stop()

    async def test_consumer_cancel_does_not_release_operation_context_exit_stops(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            consumer = asyncio.create_task(op.wait())
            await asyncio.sleep(0)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            self.assertFalse(op._task.done())
        self.assertEqual(op.status, "stopped")
        self.assertIn(4507, m.codes())

    async def test_await_fresh_state_and_stale_advance_revision(self):
        m = Machine()
        async with interaction(m) as session:
            old = session.get_state()
            revision = old["machine"]["revision"]
            waiting = asyncio.create_task(session.wait_for_state(revision))
            await asyncio.sleep(0)
            m.emit(20501, struct.pack("<f", 42))
            fresh = await waiting
            self.assertGreater(fresh["machine"]["revision"], revision)
            self.assertEqual(fresh["query_source"], "notifications_cache")
            m.state(0x77)
            with self.assertRaises(BusyError):
                await session.advance(
                    expected_stage="any", revision=old["event_revision"]
                )

    async def test_wait_event_disconnect_and_timeout_are_explicit(self):
        m = Machine()
        async with interaction(m) as session:
            cursor = session._event_revision
            with self.assertRaises(TimeoutError):
                await session._wait_event(cursor, lambda p: p.command == 1, 0.01)
            m.emit(40511)
            found = await session._wait_event(
                cursor, lambda p: p.command == 40511, 0.01
            )
            self.assertEqual(found.command, 40511)
            waiting = asyncio.create_task(
                session._wait_event(cursor, lambda p: False, 1)
            )
            await asyncio.sleep(0)
            m.drop()
            with self.assertRaisesRegex(SessionError, "Disconnected"):
                await waiting

    async def test_studio_factory_is_explicit_lazy_and_reuses_connector(self):
        m = Machine()
        studio = Studio(client=ReadOnlyClient(connector=m))
        session = studio.interaction(
            allow_control=True, settle=0, subscription_settle=0, transition_delay=0
        )
        self.assertIs(session.transport.connector, m)
        self.assertEqual(m.calls, [])
        async with session:
            self.assertTrue(session.get_state()["machine"]["connected"])
        fake = FrameFake()
        session = Studio("frame-fake").interaction(
            allow_control=True, transport=fake, settle=0, subscription_settle=0
        )
        self.assertIs(session.transport, fake)
        self.assertFalse(session.allow_untested)

    async def test_capabilities_describe_explicit_limits(self):
        info = Interaction.capabilities()
        self.assertFalse(info["read_only"])
        self.assertFalse(info["automatic_reconnect"])
        self.assertFalse(info["firmware_update"])
        self.assertIn("write_preset", info["untested_operations"])
        self.assertIn("set_brightness", info["operations"])


class ProtocolErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_start_error_fails_without_retry_and_requests_stop(self):
        m = Machine()
        m.overrides[4506] = lambda p: [reply(65534, u32(4361), 0xCD)]
        async with interaction(m) as session:
            result = await (await session.start_water(volume_ml=10)).wait()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["protocol_error"]["pending_command"], 4506)
            self.assertEqual(m.codes().count(4506), 1)
            self.assertTrue(result["stop_acknowledged"])
            self.assertTrue(session.get_state()["recovery_required"])

    async def test_unsolicited_error_during_operation_stops_without_guessing(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            await until(lambda: session._waiter is None)
            m.emit(65533, marker=0xCD)
            result = await op.wait()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["protocol_error"]["correlation"], "unsolicited")
            self.assertIsNone(result["protocol_error"]["pending_command"])
            self.assertTrue(result["stop_acknowledged"])

    async def test_error_before_session_ready_does_not_mark_uncertain(self):
        m = Machine()
        session = interaction(m)
        session._packet_received(decode(reply(65533, marker=0xCD)))
        self.assertFalse(session._uncertain)


class PreparedRecipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_then_start_prepared_sends_no_mode_change(self):
        m = Machine()
        async with interaction(m) as session:
            prepared = await session.upload_recipe(EncodedRecipe(b"\x0a\x0b", 12))
            self.assertEqual(prepared["status"], "prepared")
            self.assertEqual(
                session.get_state()["prepared"]["token"], prepared["token"]
            )
            with self.assertRaisesRegex(BusyError, "prepared recipe"):
                await session.start_water(volume_ml=10)
            before = len(m.sent)
            op = await session.start_prepared(prepared["token"], timeout=5)
            await transmitted(m, 8002)
            self.assertEqual(m.codes()[before:], [8002])
            self.assertEqual(
                session.get_state()["last_preparation"]["status"], "consumed"
            )
            m.state(0x24)
            self.assertEqual((await op.wait())["status"], "completed")
            with self.assertRaisesRegex(SessionError, "Stale"):
                await session.start_prepared(prepared["token"])

    async def test_cancel_prepared_and_invalid_tokens(self):
        m = Machine()
        async with interaction(m) as session:
            for token in (None, "nope"):
                with self.assertRaisesRegex(SessionError, "Stale"):
                    await session.cancel_prepared(token)
            prepared = await session.upload_recipe(EncodedRecipe(b"x", 10))
            result = await session.cancel_prepared(prepared["token"])
            self.assertTrue(result["cancelled"])
            self.assertEqual(m.codes()[-2:], [8017, 8022])
            self.assertIsNone(session.get_state()["prepared"])

    async def test_failed_cancel_is_uncertain(self):
        m = Machine()
        m.studio.silent.add(8017)
        async with interaction(m) as session:
            prepared = await session.upload_recipe(EncodedRecipe(b"x", 10))
            with self.assertRaises(asyncio.TimeoutError):
                await session.cancel_prepared(prepared["token"])
            self.assertEqual(
                session.get_state()["last_preparation"]["reason"], "cancel_failed"
            )
            self.assertTrue(session.get_state()["recovery_required"])

    async def test_machine_activity_invalidates_preparation(self):
        for code, payload, reason, uncertain in (
            (40502, b"", "machine_motion", True),
            (8023, u32(0x23), "machine_motion", True),
            (40517, b"", "machine_fault", True),
            (8111, b"\x01", "machine_context_changed", False),
            (8023, u32(1), "machine_context_changed", False),
        ):
            m = Machine()
            async with interaction(m) as session:
                prepared = await session.upload_recipe(EncodedRecipe(b"x", 10))
                m.emit(code, payload)
                state = session.get_state()
                self.assertIsNone(state["prepared"])
                self.assertEqual(state["last_preparation"]["reason"], reason)
                self.assertEqual(state["recovery_required"], uncertain)
                with self.assertRaisesRegex(SessionError, "Stale|uncertain"):
                    await session.start_prepared(prepared["token"])

    async def test_known_low_water_keeps_preparation_but_blocks_start(self):
        m = Machine()
        async with interaction(m) as session:
            prepared = await session.upload_recipe(EncodedRecipe(b"x", 10))
            m.emit(40522, u32(0))
            self.assertIsNotNone(session.get_state()["prepared"])
            with self.assertRaisesRegex(SessionError, "Water sufficiency"):
                await session.start_prepared(prepared["token"])
            m.emit(40522, u32(5))  # an unknown fault code invalidates
            self.assertEqual(
                session.get_state()["last_preparation"]["reason"], "machine_fault"
            )

    async def test_context_change_during_upload_stops_before_next_frame(self):
        m = Machine()
        m.overrides[8102] = lambda p: [reply(8102), reply(9001, u32(0) * 3)]
        async with interaction(m) as session:
            with self.assertRaisesRegex(SessionError, "context changed"):
                await session.upload_recipe(EncodedRecipe(b"x", 10))
            self.assertNotIn(8004, m.codes())
            self.assertTrue(session.get_state()["recovery_required"])
            self.assertEqual(
                session.get_state()["last_preparation"]["reason"],
                "machine_context_changed",
            )

    async def test_stop_before_prepared_transmission_is_not_sent(self):
        m = Machine()
        async with interaction(m) as session:
            prepared = await session.upload_recipe(EncodedRecipe(b"x", 10))
            op = await session.start_prepared(prepared["token"])
            await session.stop()
            self.assertIn(op.status, ("failed", "stopped"))
            self.assertNotIn(8002, m.codes())
            self.assertEqual(
                session.get_state()["last_preparation"]["reason"],
                "execution_not_transmitted",
            )
            self.assertTrue(session.get_state()["recovery_required"])

    async def test_disconnect_invalidates_preparation(self):
        m = Machine()
        session = interaction(m)
        async with session:
            await session.upload_recipe(EncodedRecipe(b"x", 10))
        self.assertEqual(
            session.get_state()["last_preparation"]["reason"], "disconnect"
        )


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_guard_runs_after_hook_before_bytes(self):
        m = Machine()
        session = interaction(m)
        hooks = Hooks(session)
        session.transport.hooks = hooks
        async with session:
            prepared = await session.upload_recipe(EncodedRecipe(b"x", 10))
            op = await session.start_prepared(prepared["token"])
            await transmitted(m, 8002)
            m.state(0x24)
            await op.wait()
        # The journal saw the start while the preparation was still owned: the
        # guard that consumes it ran after the hook and before transmission.
        self.assertIn(("before", 8002, True), hooks.log)
        index = hooks.log.index(("before", 8002, True))
        self.assertEqual(hooks.log[index + 1], ("completed", 8002))
        self.assertEqual(hooks.log[0], ("before", 8100, False))

    async def test_hooks_reach_default_transport_and_journal_failure_still_stops(self):
        m = Machine()
        hooks = Hooks(fail={4506})
        async with interaction(m, command_hooks=hooks) as session:
            result = await (await session.start_water(volume_ml=10)).wait()
            # A journal failure before the start frame keeps the pre-write
            # failure behavior; nothing was transmitted, so nothing is stopped.
            self.assertEqual(result["status"], "failed")
            self.assertNotIn(4506, m.codes())
            self.assertNotIn(4507, m.codes())
            self.assertIsNone(result["stop_acknowledged"])
            self.assertTrue(session.get_state()["recovery_required"])
        m = Machine()
        hooks = Hooks(fail={4507})
        async with interaction(m, command_hooks=hooks) as session:
            await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            result = await session.stop()
            self.assertIn(4507, m.codes())
            self.assertTrue(result["stop_acknowledged"])
        hooks = Hooks(fail={"all"})
        with self.assertRaises(SessionError):
            async with interaction(Machine(), command_hooks=hooks):
                pass

    async def test_injected_guard_without_journal_runs_before_write(self):
        fake = FrameFake()
        calls = []
        async with Interaction(
            "frame-fake",
            transport=fake,
            allow_control=True,
            settle=0,
            subscription_settle=0,
            transition_delay=0,
        ) as session:
            await session._command(
                8022, before_write=lambda: calls.append(len(fake.frames))
            )
        self.assertEqual(calls, [0])


class Gate(Hooks):
    """Journal hook that holds one command until released."""

    def __init__(self, code, action=None):
        super().__init__()
        self.code = code
        self.action = action
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def before_write(self, packet, frame_bytes):
        await super().before_write(packet, frame_bytes)
        if packet.command == self.code:
            self.entered.set()
            if self.action is not None:
                self.action()
            await self.release.wait()


class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_rival_interaction_leaves_incumbent_link(self):
        # S1
        m = Machine("00:00:5E:00:53:03")
        async with interaction(m) as session:
            with self.assertRaisesRegex(SessionError, "owns"):
                async with interaction(m):
                    pass
            self.assertTrue(m.is_connected)
            self.assertNotIn("disconnect", m.calls)
            await session.back_home()

    async def test_low_water_or_fault_during_configuration_blocks_start(self):
        # S2: admission is rechecked after configuration, before byte one.
        m = Machine()
        m.overrides[4510] = lambda p: [reply(4510), reply(40522, u32(0))]
        async with interaction(m) as session:
            result = await (await session.start_water(volume_ml=10)).wait()
            self.assertEqual(result["status"], "failed")
            self.assertIn("Water sufficiency", result["error"])
            self.assertNotIn(4506, m.codes())
            self.assertNotIn(4507, m.codes())
            self.assertEqual(result["cleanup_status"], "completed")
            self.assertFalse(session.get_state()["recovery_required"])
        m = Machine()
        m.overrides[8006] = lambda p: [reply(8006), reply(40517)]
        async with interaction(m) as session:
            result = await (
                await session.start_grinder(size=5, rpm=80, duration_s=1)
            ).wait()
            self.assertIn("fault", result["error"])
            self.assertNotIn(3500, m.codes())
            self.assertTrue(session.get_state()["recovery_required"])

    async def test_stop_or_lost_ownership_during_start_hook_sends_no_start(self):
        # S2: a stop requested, or ownership lost, while the start's journal
        # hook runs is seen by the guard at the real write boundary.
        m = Machine()
        gate = Gate(4506)
        async with interaction(m, command_hooks=gate) as session:
            op = await session.start_water(volume_ml=10)
            await asyncio.wait_for(gate.entered.wait(), 1)
            stopping = asyncio.create_task(session.stop())
            await asyncio.sleep(0)
            gate.release.set()
            result = await stopping
            self.assertIn("stopped before its start", result["error"])
            self.assertFalse(op._armed)
            self.assertNotIn(4506, m.codes())
            self.assertNotIn(4507, m.codes())
            self.assertEqual(result["cleanup_status"], "completed")
        m = Machine()
        session = interaction(m)
        gate = Gate(4506, action=lambda: session._lease.release())
        session.transport.hooks = gate
        async with session:
            op = await session.start_water(volume_ml=10)
            await asyncio.wait_for(gate.entered.wait(), 1)
            gate.release.set()
            result = await op.wait()
            self.assertIn("ownership lost", result["error"])
            self.assertNotIn(4506, m.codes())

    async def test_unsubscribe_and_start_are_serialized(self):
        # S3: no operation is admitted between the unsubscribe check and call.
        m = Machine()
        async with interaction(m) as session:
            unsubscribing = asyncio.create_task(session.unsubscribe())
            starting = asyncio.create_task(session.start_water(volume_ml=10))
            results = await asyncio.gather(
                unsubscribing, starting, return_exceptions=True
            )
            self.assertIsNone(results[0])
            self.assertIsInstance(results[1], SessionError)
            self.assertIsNone(session.operation)
            self.assertNotIn(4506, m.codes())

    async def test_stop_survives_cancellation_of_its_requester(self):
        # S4: the stop commitment is not lost with the requesting coroutine,
        # and a repeat caller shares the same attempt.
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            first = asyncio.create_task(session.stop())
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            result = await asyncio.wait_for(session.stop(), 1)
            self.assertEqual(op.status, "stopped")
            self.assertTrue(result["stop_acknowledged"])
            self.assertEqual(m.codes().count(4507), 1)
            Interaction._cancel_worker(op, op._task)  # late callback: no-op
            self.assertFalse(op._task.cancelled())

    async def test_stop_and_ack_survive_slow_journals_and_failing_observers(self):
        # S5: a cancellation-resistant stop journal cannot exhaust the bounded
        # stop write; post-write hook failures cannot drop an ACK or a stop.
        class Resisting(Hooks):
            async def before_write(self, packet, frame_bytes):
                if packet.command == 4507:
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        await asyncio.sleep(0.2)
                        raise

            def write_completed(self, packet):
                raise RuntimeError("observer")

        m = Machine()
        async with interaction(m, command_hooks=Resisting(), write_timeout=0.1) as s:
            await s.start_water(volume_ml=10)
            await transmitted(m, 4506)
            result = await s.stop()
            self.assertTrue(result["stop_acknowledged"])
            self.assertTrue(result["stop_observed"])
            self.assertEqual(result["cleanup_status"], "completed")
            self.assertFalse(s.get_state()["recovery_required"])
            await asyncio.sleep(0.25)  # let the detached journal finish

    async def test_ack_is_armed_only_when_the_frame_can_reach_the_machine(self):
        # S6: matching notifications from a pre-write hook, between chunks or
        # from a legacy guard are stale; a write-time ACK still counts.
        m = Machine()
        m.studio.silent.add(8022)
        hooks = Hooks()
        hooks.before_write = lambda packet, raw: _emit(m, packet.command)
        async with interaction(m) as session:
            session.transport.hooks = hooks
            with self.assertRaises(asyncio.TimeoutError):
                await session._command(8022)
            self.assertEqual(m.codes()[-1], 8022)

        class Early(Machine):
            async def write(self, data):
                await super().write(data)
                if data[:1] == b"\x58" and len(data) == self.write_size:
                    self.emit(8102)  # after the first of two chunks

        m = Early()
        m.studio.silent.add(8102)
        async with interaction(m) as session:
            with self.assertRaises(asyncio.TimeoutError):
                await session._command(8102, b"\0" * 12)
            self.assertEqual(len(m.writes[-2]), 20)
            m.studio.silent.clear()
            await session._command(8102, b"\0" * 12)  # synchronous ACK
        fake = FrameFake()
        fake.machine.studio.silent.add(8022)
        async with Interaction(
            "frame-fake",
            transport=fake,
            allow_control=True,
            settle=0,
            subscription_settle=0,
            transition_delay=0,
            ack_timeout=0.03,
        ) as session:
            with self.assertRaises(asyncio.TimeoutError):
                await session._command(
                    8022, before_write=lambda: fake.machine.emit(8022)
                )
            fake.machine.studio.silent.clear()
            await session._command(8022)

    async def test_cancel_after_unacknowledged_write_requires_recovery(self):
        # S8: the cancellation propagates and marks the outcome uncertain.
        m = Machine()
        m.studio.silent.add(8022)
        async with interaction(m, ack_timeout=1) as session:
            pending = asyncio.create_task(session._command(8022))
            await transmitted(m, 8022)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(session.get_state()["recovery_required"])
        m = Machine()
        m.studio.silent.add(8007)
        async with interaction(m, ack_timeout=1) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 8007)
            result = await session.stop()
            self.assertEqual(op.status, "stopped")
            self.assertEqual(result["cleanup_status"], "skipped_uncertain")
            self.assertNotIn(4506, m.codes())

    async def test_stop_evidence_comes_only_after_the_stop_write(self):
        # S11: stale idle before the stop is not proof; motion after stop
        # evidence voids it; completion evidence before the stop is kept.
        for replies, observed in (
            ([reply(4507)], False),
            ([reply(4507), reply(8023, u32(1)), reply(8023, u32(0x23))], False),
            ([reply(4507), reply(40511)], True),
        ):
            m = Machine()
            m.overrides[4507] = lambda p, replies=replies: replies
            async with interaction(m) as session:
                await session.start_water(volume_ml=10)
                await transmitted(m, 4506)
                m.state(1)
                m.state(0x23)
                result = await session.stop()
                self.assertTrue(result["stop_acknowledged"])
                self.assertEqual(result["stop_observed"], observed)
        m = Machine()
        m.overrides[4507] = lambda p: [reply(4507)]
        async with interaction(m) as session:
            op = await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            m.emit(40511)
            result = await op.wait()
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["stop_observed"])

    async def test_grinder_pulse_is_budgeted_from_the_start_frame(self):
        # A4: a delayed start ACK counts against the pulse; if the budget
        # expires before the ACK, stop is requested with no false completion
        # and no replayed start.
        loop = asyncio.get_running_loop()
        for delay, duration, completed in ((0.2, 0.3, True), (0.4, 0.1, False)):
            m = Machine()
            times = {}
            handles = []

            def start(packet, m=m, times=times, handles=handles, delay=delay):
                times[3500] = time.monotonic()
                handles.append(
                    loop.call_later(delay, lambda: (m.emit(3500), m.state(0x22)))
                )
                return []

            def stop(packet, m=m, times=times):
                times[3505] = time.monotonic()
                return m.studio(packet)

            m.overrides[3500] = start
            m.overrides[3505] = stop
            async with interaction(m, ack_timeout=1) as session:
                result = await (
                    await session.start_grinder(size=5, rpm=80, duration_s=duration)
                ).wait()
                elapsed = times[3505] - times[3500]
                self.assertGreaterEqual(elapsed, duration)
                self.assertLess(elapsed, duration + 0.15)
                self.assertEqual(m.codes().count(3500), 1)
                self.assertTrue(result["stop_acknowledged"])
                if completed:
                    self.assertEqual(result["status"], "completed")
                    self.assertTrue(result["start_observed"])
                else:
                    self.assertEqual(result["status"], "failed")
                    self.assertIn("budget", result["error"])
                    self.assertFalse(result["start_observed"])
                    self.assertTrue(session.get_state()["recovery_required"])
            for handle in handles:
                handle.cancel()

    async def test_studio_interaction_keeps_client_journal_and_observer(self):
        # S5-related: the client's journal and transcript observer carry over.
        m = Machine()
        hooks = Hooks()
        events = []
        studio = Studio(
            client=ReadOnlyClient(
                connector=m,
                command_hooks=hooks,
                event_sink=lambda kind, details: events.append(kind),
            )
        )
        session = studio.interaction(
            allow_control=True, settle=0, subscription_settle=0, transition_delay=0
        )
        self.assertIs(session.transport.hooks, hooks)
        async with session:
            await session.back_home()
        self.assertIn(("completed", 8022), hooks.log)
        self.assertIn("ble.packet", events)
        explicit = studio.interaction(
            connector=Machine(), command_hooks=None, event_sink=None
        )
        self.assertIsNone(explicit.transport.hooks)
        self.assertIsNone(explicit.event_sink)


async def _emit(machine, code):
    machine.emit(code)


class EncodedRecipeTests(unittest.TestCase):
    def test_validation_and_settings_bytes(self):
        for kwargs in (
            {"payload": b""},
            {"payload": "text"},
            {"payload": b"x" * 513},
            {"dose_g": 2.5},
            {"dose_g": 0},
            {"grinder_enabled": 1},
            {"set_grinder_size": "yes"},
            {"grinder_enabled": True, "set_grinder_size": False},
            {"cup_limits": (100, 5)},
            {"cup_limits": (float("nan"), 5)},
            {"cup_limits": [1, 2]},
            {"bypass_ml": 1001},
            {"bypass_temperature_c": 19},
        ):
            values = {"payload": b"example", "dose_g": 10, **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                EncodedRecipe(**values)
        recipe = EncodedRecipe(b"example", 10, bypass_ml=10, bypass_temperature_c=95)
        self.assertEqual(
            recipe.settings_payload(), struct.pack("<ffI", 10.0, 950.0, 10)
        )
        self.assertEqual(recipe.settings_payload().hex(), "0000204100806d440a000000")
        plain = EncodedRecipe(b"example", 10, bypass_temperature_c=95)
        self.assertEqual(plain.settings_payload().hex(), "00000000000000000a000000")

    def test_grinder_option_alias(self):
        self.assertFalse(EncodedRecipe(b"x", 10).set_grinder_size)
        alias = EncodedRecipe(b"x", 10, set_grinder_size=True)
        self.assertTrue(alias.grinder_enabled)
        self.assertEqual(alias, EncodedRecipe(b"x", 10, True, set_grinder_size=True))
        self.assertEqual(alias.upload_options()["upload_command"], 8001)
        self.assertEqual(
            EncodedRecipe(b"x", 10).upload_options()["upload_command"], 8004
        )


if __name__ == "__main__":
    unittest.main()
