"""The one hardware-observed prepared coffee route, and everything it excludes.

Only a structured xDripper coffee plan from the current encoder, uploaded with
the grind-size command, the reference cup limits, no bypass, no vibration,
numeric pour temperatures and single-record pours, drops the upload opt-in.
Expected bytes are written out from the documented payload layout.
"""

from __future__ import annotations

import inspect
import struct
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tests.machine import Machine, interaction
from xbloom_studio import EncodedRecipe
from xbloom_studio.application import MachineService
from xbloom_studio.control import Interaction
from xbloom_studio.facade import Application
from xbloom_studio.machine_api import COMMANDS
from xbloom_studio.machine_controls import UntestedOperationError
from xbloom_studio.mcp_server import create_server
from xbloom_studio.recipes import (
    _compile_recipe,
    compile_recipe,
    upload_requires_untested,
)
from xbloom_studio.serialization import dumps
from xbloom_studio.storage import ConflictError, Store

FAST = {
    "settle": 0,
    "subscription_settle": 0,
    "transition_delay": 0,
    "ack_timeout": 0.2,
    "write_timeout": 3,
}
COFFEE_V1 = "studio-coffee-android-2.2.3-v1"
VALIDATED_LIMITS = (90, 110)


def coffee(pours=None, **changes):
    """15 g, 240 ml at 91 C and 3 ml/s in three single-record pours."""
    content = {
        "cup_type": "xdripper",
        "dose_g": 15,
        "grind_size": 60,
        "ratio": 16,
        "grinder_enabled": True,
        "pours": pours
        or [
            {"volume_ml": 40, "temperature": 91, "pause_seconds": 30},
            {"volume_ml": 100, "temperature": 91},
            {"volume_ml": 100, "temperature": 91},
        ],
    }
    content.update(changes)
    return {"name": "Validated coffee", "mode": "coffee", "coffee_recipe": content}


def request(document):
    return {"kind": "recipe", "document": document}


# 18            body length (24)
# 28 5b 00 00   40 ml, 91 C, center, no vibration
# e2 00 78 1e   pause 30 (negated), reserved, RPM 120, flow*10 = 30
# 64 5b 00 00   100 ml, 91 C, center, no vibration
# 00 00 00 1e   no pause, reserved, RPM only on the first pour, flow 30
# 64 5b 00 00 / 00 00 00 1e   the third pour
# 3c a0         grind 60, ratio*10 = 160
CANONICAL = bytes.fromhex("18285b0000e200781e645b00000000001e645b00000000001e3ca0")
CANONICAL_SEQUENCE = [
    (8102, struct.pack("<ffI", 0, 0, 15)),
    (8104, struct.pack("<ff", 110, 90)),
    (8001, CANONICAL),
]


def excluded():
    """Each one-property departure from the validated profile."""
    big = [
        {"volume_ml": 140, "temperature": 91},
        {"volume_ml": 100, "temperature": 91},
    ]
    named = coffee()["coffee_recipe"]["pours"]
    return {
        "pre-ground 8004": coffee(grinder_enabled=False),
        "xpod cup": coffee(cup_type="xpod", ratio=16),
        "other cup": coffee(cup_type="other"),
        "bypass": coffee(bypass={"volume_ml": 20, "temperature": 85}),
        "RT pour": coffee([{**named[0], "temperature": "RT"}, *named[1:]]),
        "BP pour": coffee([{**named[0], "temperature": "BP"}, *named[1:]]),
        "vibration before": coffee(
            [{**named[0], "vibration_before": True}, *named[1:]]
        ),
        "vibration after": coffee([*named[:2], {**named[2], "vibration_after": True}]),
        "fragmented pour": coffee(big),
    }


def native_override():
    """Opaque native content with settings identical to the validated upload."""
    return {
        "name": "Lookalike",
        "mode": "native",
        "native": {
            "payload_hex": CANONICAL.hex(),
            "dose_g": 15,
            "grinder_enabled": True,
            "cup_limits": list(VALIDATED_LIMITS),
            "bypass_ml": 0,
        },
    }


TEA = {
    "name": "Tea",
    "mode": "tea",
    "tea": {
        "dose_g": 3,
        "steeps": [{"volume_ml": 60, "steep_seconds": 30, "temperature": 90}],
    },
}


class PredicateTests(unittest.TestCase):
    def test_canonical_plan_is_validated_without_changing_the_plan(self):
        plan = compile_recipe(coffee())
        before = dumps(plan)
        self.assertFalse(upload_requires_untested(plan))
        self.assertEqual(dumps(plan), before)
        # The compiled schema, flags and limitations stay as they were.
        self.assertTrue(plan["requires_untested"])
        self.assertNotIn("upload_requires_untested", plan)
        step = plan["execution"][0]
        self.assertEqual(step["payload_hex"], CANONICAL.hex())
        self.assertEqual(step["cup_limits"], list(VALIDATED_LIMITS))
        self.assertIs(step["grinder_enabled"], True)
        self.assertEqual(step["bypass_ml"], 0)
        # The alias selects the same upload; resolution metadata is ignored.
        alias = coffee()
        content = alias["coffee_recipe"]
        content["set_grinder_size"] = content.pop("grinder_enabled")
        resolved = {**compile_recipe(alias), "recipe_id": "r", "recipe_revision": 1}
        self.assertFalse(upload_requires_untested(resolved))

    def test_every_excluded_variant_keeps_the_opt_in(self):
        for name, document in excluded().items():
            with self.subTest(name):
                plan = compile_recipe(document)
                self.assertTrue(plan["requires_untested"])
                self.assertTrue(upload_requires_untested(plan))

    def test_other_encoders_modes_and_tampering_keep_the_opt_in(self):
        legacy = _compile_recipe(coffee(), coffee_encoder_version=COFFEE_V1)
        native = compile_recipe(native_override())
        self.assertTrue(native["requires_untested"])
        tampered = compile_recipe(coffee())
        tampered["execution"][0]["payload_hex"] = CANONICAL[:-1].hex() + "a1"
        snapshot = compile_recipe(coffee())
        snapshot["recipe_snapshot"]["coffee_recipe"]["pours"][0]["temperature"] = 92
        two_steps = compile_recipe(coffee())
        two_steps["execution"].append(deepcopy(two_steps["execution"][0]))
        malformed = {**compile_recipe(coffee()), "recipe_snapshot": None}
        for name, plan in {
            "legacy encoder": legacy,
            "tea": compile_recipe(TEA),
            "native lookalike": native,
            "tampered bytes": tampered,
            "tampered snapshot": snapshot,
            "two steps": two_steps,
            "malformed": malformed,
            "not a plan": None,
        }.items():
            with self.subTest(name):
                self.assertTrue(upload_requires_untested(plan))

    def test_plans_that_never_needed_opt_in_are_unchanged(self):
        plain = compile_recipe(
            {
                "name": "N",
                "mode": "native",
                "native": {"payload_hex": "01", "dose_g": 9},
            }
        )
        self.assertFalse(plain["requires_untested"])
        self.assertFalse(upload_requires_untested(plain))


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "xbloom-studio.sqlite3")
        self.machine = Machine("00:00:5E:00:53:01", auto_complete=True)
        self.service = MachineService(
            self.store, interaction_options=dict(FAST), connector=self.machine
        )

    async def asyncTearDown(self):
        await self.service.close()
        self.store.close()
        self.tmp.cleanup()

    def sent(self):
        return [
            (packet.command, packet.payload)
            for packet in self.machine.sent
            if packet.command in (8001, 8004, 8102, 8104)
        ]

    async def finish(self):
        await self.service.task
        return self.store.operation(self.service.operation_id)


class PreparedRouteTests(ServiceCase):
    async def test_preview_exposes_both_flags_and_start_never_persists_one(self):
        saved = self.store.create_recipe(coffee())
        preview = self.service.prepare({"kind": "recipe", "reference": saved["id"]})
        self.assertTrue(preview["requires_untested"])
        self.assertIs(preview["upload_requires_untested"], False)
        pinned = preview["resolved_request"]
        for document in [*excluded().values(), native_override(), TEA]:
            with self.subTest(document["name"]):
                other = self.service.prepare(request(document))
                self.assertIs(other["upload_requires_untested"], True)
        prepared = await self.service.upload(
            pinned, True, expected_recipe_sha256=preview["recipe_sha256"]
        )
        await self.service.start_uploaded(prepared["token"], "first", True)
        done = await self.finish()
        self.assertEqual(done["status"], "completed")
        # History keeps the raw plan: the compiled fields plus the pinned revision.
        self.assertEqual(
            done["plan"],
            {
                **compile_recipe(coffee()),
                "recipe_id": saved["id"],
                "recipe_revision": saved["revision"],
            },
        )

    async def test_upload_without_opt_in_sends_the_canonical_sequence_once(self):
        prepared = await self.service.upload(request(coffee()), True)
        self.assertFalse(self.service.session.allow_untested)
        self.assertFalse(prepared["readback_verified"])
        self.assertEqual(self.sent(), CANONICAL_SEQUENCE)
        self.assertNotIn(8002, self.machine.codes())
        operation = await self.service.start_uploaded(prepared["token"], "brew", True)
        self.assertEqual(operation["request"]["kind"], "uploaded")
        done = await self.finish()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.machine.codes().count(8002), 1)
        self.assertEqual(self.sent(), CANONICAL_SEQUENCE)
        state = self.service.state()
        self.assertIsNone(state["prepared"])
        self.assertFalse(state["machine"]["recovery_required"])
        self.assertEqual(state["machine"]["last_preparation"]["status"], "consumed")

    async def test_control_permission_and_digest_still_apply(self):
        document = coffee()
        digest = compile_recipe(document)["recipe_sha256"]
        with self.assertRaisesRegex(ValueError, "allow_control"):
            await self.service.upload(request(document), False)
        with self.assertRaises(ConflictError):
            await self.service.upload(
                request(document), True, expected_recipe_sha256="0" * 64
            )
        self.assertEqual(self.machine.calls, [])
        prepared = await self.service.upload(
            request(document), True, expected_recipe_sha256=digest
        )
        with self.assertRaisesRegex(ValueError, "allow_control"):
            await self.service.start_uploaded(prepared["token"], "k", False)
        self.assertNotIn(8002, self.machine.codes())

    async def test_direct_start_of_the_same_recipe_keeps_the_opt_in(self):
        for req in (request(coffee()), request(native_override())):
            with self.subTest(req["document"]["name"]):
                with self.assertRaisesRegex(ValueError, "allow_untested"):
                    await self.service.start(req, "direct", True)
        self.assertEqual(self.machine.calls, [])
        self.assertIsNone(self.store.operation_by_key("direct"))

    async def test_excluded_uploads_keep_the_opt_in_and_send_nothing(self):
        for name, document in [
            *excluded().items(),
            ("native lookalike", native_override()),
            ("tea", TEA),
        ]:
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, "allow_untested"):
                    await self.service.upload(request(document), True)
        self.assertEqual(self.machine.calls, [])

    async def test_repeat_reuses_the_saved_plan_through_validated_admission(self):
        prepared = await self.service.upload(request(coffee()), True)
        first = await self.service.start_uploaded(prepared["token"], "one", True)
        original = await self.finish()
        repeat = {"kind": "repeat", "operation_id": first["id"]}
        preview = self.service.prepare(repeat)
        self.assertIs(preview["upload_requires_untested"], False)
        with self.assertRaisesRegex(ValueError, "allow_untested"):
            await self.service.start(repeat, "direct-repeat", True)
        again = await self.service.upload(repeat, True)
        await self.service.start_uploaded(again["token"], "two", True)
        repeated = await self.finish()
        self.assertEqual(dumps(repeated["plan"]), dumps(original["plan"]))
        self.assertEqual(self.sent(), CANONICAL_SEQUENCE * 2)
        self.assertEqual(self.machine.codes().count(8002), 2)

    async def test_legacy_retained_plan_keeps_the_opt_in(self):
        plan = _compile_recipe(coffee(), coffee_encoder_version=COFFEE_V1)
        op, _ = self.store.create_operation(
            "00:00:5E:00:53:01", {"kind": "recipe"}, plan, "legacy"
        )
        repeat = {"kind": "repeat", "operation_id": op["id"]}
        self.assertIs(self.service.prepare(repeat)["upload_requires_untested"], True)
        with self.assertRaisesRegex(ValueError, "allow_untested"):
            await self.service.upload(repeat, True)
        self.assertEqual(self.machine.calls, [])

    async def test_generic_cup_limit_commands_keep_the_opt_in(self):
        for command, params in (
            ("set_cup_limits", {"minimum": 90, "maximum": 110}),
            ("configure_recipe", {"dose_g": 15, "cup_limits": [90, 110]}),
        ):
            with self.subTest(command):
                with self.assertRaisesRegex(ValueError, "allow_untested"):
                    await self.service.command(command, params, True)
        self.assertEqual(self.machine.calls, [])


class LowLevelGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_upload_has_no_opt_in_bypass(self):
        self.assertEqual(
            list(inspect.signature(Interaction.upload_recipe).parameters),
            ["self", "recipe"],
        )
        hidden = "_upload_validated_coffee"
        self.assertNotIn(hidden, Interaction.capabilities()["operations"])
        self.assertNotIn(hidden, COMMANDS)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "x.sqlite3")
            try:
                self.assertNotIn(hidden, Application(store).methods)
            finally:
                store.close()
        canonical = EncodedRecipe(CANONICAL, 15, True, VALIDATED_LIMITS)
        machine = Machine()
        async with interaction(machine) as session:
            with self.assertRaises(UntestedOperationError):
                await session.upload_recipe(canonical)
        self.assertEqual(machine.payloads(8102), [])

    async def test_internal_path_accepts_only_the_validated_options(self):
        refused = {
            "pre-ground": EncodedRecipe(CANONICAL, 15, False, VALIDATED_LIMITS),
            "no cup limits": EncodedRecipe(CANONICAL, 15, True, None),
            "other limits": EncodedRecipe(CANONICAL, 15, True, (40, 90)),
            "bypass": EncodedRecipe(CANONICAL, 15, True, VALIDATED_LIMITS, 20, 85),
        }
        machine = Machine()
        async with interaction(machine) as session:
            with self.assertRaises(TypeError):
                await session._upload_validated_coffee(CANONICAL)
            for name, recipe in refused.items():
                with self.subTest(name), self.assertRaises(UntestedOperationError):
                    await session._upload_validated_coffee(recipe)
            self.assertEqual(machine.payloads(8102), [])
            prepared = await session._upload_validated_coffee(
                EncodedRecipe(CANONICAL, 15, True, VALIDATED_LIMITS)
            )
            self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(
            [
                (p.command, p.payload)
                for p in machine.sent
                if p.command in (8001, 8004, 8102, 8104)
            ],
            CANONICAL_SEQUENCE,
        )


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_plan_carries_the_flag_and_tolerates_older_daemons(self):
        plan = compile_recipe(coffee())
        answers = {
            "machine_prepare": {**plan, "upload_requires_untested": False},
            "recipe_preview": plan,
        }

        async def caller(method, params):
            return answers[method]

        server = create_server(caller=caller)
        args = {"request": request(coffee())}
        current = await server.call_tool("machine_prepare", args)
        self.assertFalse(current.is_error, current.structured_content)
        self.assertIs(current.structured_content["upload_requires_untested"], False)
        answers["machine_prepare"] = plan  # An older daemon omits the field.
        older = await server.call_tool("machine_prepare", args)
        self.assertFalse(older.is_error, older.structured_content)
        self.assertNotIn("upload_requires_untested", older.structured_content)
        self.assertTrue(older.structured_content["requires_untested"])
        preview = await server.call_tool("recipe_preview", {"document": coffee()})
        self.assertNotIn("upload_requires_untested", preview.structured_content)
        schema = {t.name: t for t in await server.list_tools()}["machine_prepare"]
        self.assertNotIn(
            "upload_requires_untested", schema.output_schema.get("required", [])
        )


if __name__ == "__main__":
    unittest.main()
