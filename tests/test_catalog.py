import re
import unittest

from xbloom_studio import events
from xbloom_studio.protocol import catalog
from xbloom_studio.transport import CONTROL_COMMANDS, FUNCTION_2_COMMANDS, STOP_COMMANDS

DECODED_ELSEWHERE = {
    8100,  # acknowledgment
    65533,
    65534,  # error envelopes
    20501,
    10507,
    40523,
    8009,
    8011,
    40522,
    8023,
    40521,
    8015,
    9000,
    9001,
    9003,
    9005,
    40501,
    11518,
}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.entries = catalog()["commands"]

    def outgoing(self):
        return {e["code"]: e for e in self.entries if e["direction"] == "outgoing"}

    def test_entries_are_well_formed_and_unique(self):
        keys = set()
        for entry in self.entries:
            with self.subTest(entry=entry["name"]):
                self.assertRegex(entry["name"], r"^[a-z][a-z0-9_]*$")
                self.assertEqual(entry["hex"], f"0x{entry['code']:04x}")
                self.assertIn(entry["direction"], ("outgoing", "incoming"))
                self.assertTrue(entry["summary"].endswith("."))
                key = (entry["code"], entry["direction"])
                self.assertNotIn(key, keys)
                keys.add(key)
        names = [(e["name"], e["direction"]) for e in self.entries]
        self.assertEqual(len(names), len(set(names)))

    def test_outgoing_entries_match_the_command_gate(self):
        outgoing = self.outgoing()
        self.assertEqual(set(outgoing), CONTROL_COMMANDS | {8100})
        self.assertEqual(outgoing[8100]["gate"], "handshake")
        for code, entry in outgoing.items():
            with self.subTest(code=code):
                self.assertEqual(
                    entry["function"], 2 if code in FUNCTION_2_COMMANDS else 1
                )
                self.assertIn(entry["gate"], catalog()["gates"])
        self.assertTrue(STOP_COMMANDS <= set(outgoing))

    def test_untested_gate_marks_untested_only_commands(self):
        untested = {c for c, e in self.outgoing().items() if e["gate"] == "untested"}
        self.assertEqual(
            untested,
            {
                8104,
                8001,
                4513,
                4512,
                8017,
                4508,
                11510,
                11512,
                40525,
                40526,
                11506,
                11507,
                11508,
                11509,
            },
        )

    def test_every_decoded_incoming_code_is_named(self):
        incoming = {e["code"] for e in self.entries if e["direction"] == "incoming"}
        decoded = set(events.SCALARS) | set(events.SIGNALS) | DECODED_ELSEWHERE
        self.assertEqual(incoming, decoded)
        names = {
            e["code"]: e["name"] for e in self.entries if e["direction"] == "incoming"
        }
        for code, name in events.SIGNALS.items():
            self.assertEqual(names[code], name)
        for code, (_, name) in events.SCALARS.items():
            self.assertEqual(names[code], name)

    def test_catalog_text_has_no_foreign_identifiers(self):
        for entry in self.entries:
            self.assertFalse(re.search(r"[A-Z]{2,}_", entry["name"]))


if __name__ == "__main__":
    unittest.main()
