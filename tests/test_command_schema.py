import unittest

import tests  # noqa: F401 - installs the test guards
from xbloom_studio import Interaction
from xbloom_studio.command_schema import (
    COMMAND_SCHEMAS,
    CommandValidationError,
    command_schema,
    validate_command_params,
)
from xbloom_studio.machine_controls import MachineControls

PRESET = {"slot": "A", "payload_hex": "0a0b", "flags": 4}


def errors(command, params):
    with unittest.TestCase().assertRaises(CommandValidationError) as caught:
        validate_command_params(command, params)
    return caught.exception.report["errors"]


class CommandSchemaTests(unittest.TestCase):
    def test_every_named_control_has_a_contract(self):
        expected = set(MachineControls.METHODS) - {
            "write_preset",
            "write_presets",
            "start_grinder_calibration",
            "calibrate_grinder",
            "start_tea",
        }
        self.assertTrue(expected <= set(COMMAND_SCHEMAS))
        for name in COMMAND_SCHEMAS:
            self.assertTrue(
                hasattr(Interaction, name), f"{name} has no Interaction method"
            )

    def test_independent_value_examples(self):
        for command, params in (
            ("set_brightness", {"level": 15}),
            ("set_weight_unit", {"unit": "oz"}),
            ("set_pour_pattern", {"pattern": "ring"}),
            ("set_pour_pattern", {"pattern": "ring", "temperature_c": 92.5}),
            ("back_home", {}),
            ("set_mode", {"mode": "recipes"}),
            ("set_preset_order", {"order": "CAB", "backup_order": ["A", "B", "C"]}),
            ("configure_recipe", {"dose_g": 15, "cup_limits": None}),
            ("configure_recipe", {"dose_g": 15, "cup_limits": [100, 300.5]}),
            ("set_cup_limits", {"minimum": 0, "maximum": 0}),
            ("initialize_pour_radius", {"value": 2147483647}),
            ("write_preset", {"preset": PRESET, "backup": {**PRESET, "flags": 0}}),
            ("write_preset", {"preset": PRESET}),
            ("write_preset", {"preset": PRESET, "acknowledge_incomplete": "0a" * 16}),
            (
                "write_presets",
                {
                    "presets": [{**PRESET, "slot": s} for s in "CAB"],
                    "backup": [{**PRESET, "slot": s} for s in "ABC"],
                    "save_timeout": 0.5,
                },
            ),
        ):
            with self.subTest(command=command, params=params):
                validate_command_params(command, params)

    def test_nested_diagnostics(self):
        cases = (
            ("set_brightness", {"level": 0}, "/params/level", "bounds"),
            ("set_brightness", {"level": 8.0}, "/params/level", "Expected integer"),
            ("set_brightness", {"level": True}, "/params/level", "Expected integer"),
            ("set_brightness", {}, "/params/level", "missing"),
            ("set_brightness", {"level": 1, "x/y": 2}, "/params/x~1y", "Unknown"),
            ("set_brightness", None, "/params", "Expected object"),
            ("set_weight_unit", {"unit": "kg"}, "/params/unit", "allowed value"),
            (
                "set_pour_pattern",
                {"pattern": "ring", "temperature_c": float("nan")},
                "/params/temperature_c",
                "finite",
            ),
            (
                "set_cup_limits",
                {"minimum": 5, "maximum": 1},
                "/params/maximum",
                "at least",
            ),
            (
                "configure_recipe",
                {"dose_g": 5, "cup_limits": [3, 1]},
                "/params/cup_limits",
                "[minimum, maximum]",
            ),
            (
                "configure_recipe",
                {"dose_g": 5, "cup_limits": [1]},
                "/params/cup_limits",
                "any allowed shape",
            ),
            (
                "set_preset_order",
                {"order": ["A", "A", "B"], "backup_order": "ABC"},
                "/params/order",
                "any allowed shape",
            ),
            (
                "write_preset",
                {"preset": {**PRESET, "payload_hex": "abc"}, "backup": PRESET},
                "/params/preset/payload_hex",
                "format",
            ),
            (
                "write_preset",
                {"preset": {**PRESET, "payload_hex": ""}, "backup": PRESET},
                "/params/preset/payload_hex",
                "length",
            ),
            (
                "write_preset",
                {"preset": PRESET, "backup": {**PRESET, "slot": "B"}},
                "/params/backup/slot",
                "replacement slot",
            ),
            (
                "write_preset",
                {"preset": PRESET, "acknowledge_incomplete": "write-1"},
                "/params/acknowledge_incomplete",
                "format",
            ),
            (
                "write_presets",
                {"presets": [PRESET] * 3, "backup": [PRESET] * 3},
                "/params/presets",
                "one preset per",
            ),
            (
                "write_presets",
                {"presets": [PRESET], "backup": [PRESET] * 3},
                "/params/presets",
                "item count",
            ),
            (
                "write_presets",
                {"presets": "AAA", "backup": [PRESET] * 3},
                "/params/presets",
                "Expected array",
            ),
        )
        for command, params, path, message in cases:
            with self.subTest(command=command, params=params):
                found = errors(command, params)
                self.assertIn(path, [e["path"] for e in found])
                self.assertTrue(any(message in e["message"] for e in found))

    def test_error_report_and_message(self):
        with self.assertRaises(CommandValidationError) as caught:
            validate_command_params("set_mode", {"mode": "x"})
        self.assertFalse(caught.exception.report["valid"])
        self.assertIn("/params/mode", str(caught.exception))
        self.assertIsInstance(caught.exception, ValueError)

    def test_schema_lookup_and_union(self):
        schema = command_schema("set_brightness")
        schema["properties"].clear()
        self.assertIn("level", command_schema("set_brightness")["properties"])
        with self.assertRaisesRegex(ValueError, "Unknown machine command"):
            command_schema("firmware_update")
        union = command_schema()
        names = [b["properties"]["command"]["const"] for b in union["oneOf"]]
        self.assertEqual(names, list(COMMAND_SCHEMAS))
        home = union["oneOf"][names.index("back_home")]
        self.assertEqual(home["required"], ["command"])
        self.assertIn("anyOf", home["properties"]["params"])
        level = union["oneOf"][names.index("set_brightness")]
        self.assertEqual(level["required"], ["command", "params"])
        self.assertFalse(level["properties"]["allow_untested"]["default"])


if __name__ == "__main__":
    unittest.main()
