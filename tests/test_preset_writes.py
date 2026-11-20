"""Folding journal rows into per-slot writes, and the repeat policy."""

import json
import unittest

from xbloom_studio.preset_writes import (
    ACKNOWLEDGED,
    FINISHED,
    STARTED,
    PresetWriteRefused,
    admit,
    fold,
)

A = {"slot": "A", "payload_hex": "6e6577", "flags": 4}
W, X = "1" * 32, "2" * 32


def row(kind, **data):
    return {"kind": kind, "at": "t", "data": json.dumps(data)}


def started(attempt, write=W, **preset):
    return row(STARTED, write_id=write, attempt_id=attempt, **{**A, **preset})


def finished(attempt, outcome, write=W):
    return row(FINISHED, write_id=write, attempt_id=attempt, slot="A", outcome=outcome)


class FoldTests(unittest.TestCase):
    def test_refusals_do_not_count_and_running_attempts_are_in_progress(self):
        write = fold([started("a"), finished("a", "refused"), started("b")], "b")["A"]
        self.assertEqual(
            [a["outcome"] for a in write["attempts"]], ["refused", "in_progress"]
        )
        self.assertEqual((write["state"], write["attempts_used"]), ("in_progress", 1))
        only_refused = fold([started("a"), finished("a", "refused")])["A"]
        self.assertEqual(only_refused["state"], "not_sent")
        self.assertEqual(only_refused["remaining_attempts"], 3)

    def test_an_acknowledgment_closes_the_write(self):
        rows = [
            started("a"),
            finished("a", "rejected"),
            row(ACKNOWLEDGED, write_id=W, slot="A"),
        ]
        self.assertEqual(fold(rows)["A"]["state"], "acknowledged")
        self.assertEqual(fold(rows + [started("b", X)])["A"]["write_id"], X)

    def test_anything_unexpected_fails_closed(self):
        for rows in (
            [{"kind": STARTED, "at": "t", "data": "{"}],
            [started("a", slot="D")],
            [started("a"), started("b", flags=5)],
            [started("a"), finished("a", "saved"), finished("a", "saved")],
            [started("a"), finished("a", "readback")],
            [started("a"), finished("z", "saved")],
            [started("a"), row(ACKNOWLEDGED, write_id=X, slot="A")],
            [started("a"), row("preset_write.other", write_id=W, slot="A")],
            [finished("a", "saved")],
        ):
            with self.subTest(rows=rows), self.assertRaises(PresetWriteRefused):
                fold(rows)


class AdmitTests(unittest.TestCase):
    def current(self, *rows):
        return fold(rows)["A"]

    def test_new_and_unsent_writes(self):
        self.assertEqual(admit(None, A, None), (None, False))
        unsent = self.current(started("a"), finished("a", "refused"))
        self.assertEqual(admit(unsent, A, None), (W, False))
        self.assertEqual(admit(unsent, {**A, "flags": 1}, None), (None, False))
        closed = self.current(started("a"), row(ACKNOWLEDGED, write_id=W, slot="A"))
        self.assertEqual(admit(closed, A, None), (None, False))

    def test_acknowledgment_names_the_latest_open_write(self):
        saved = self.current(started("a"), finished("a", "saved"))
        self.assertEqual(admit(saved, A, W), (None, False))
        self.assertEqual(admit(saved, {**A, "flags": 1}, None), (None, False))
        closed = self.current(started("a"), row(ACKNOWLEDGED, write_id=W, slot="A"))
        for current, acknowledge in ((None, W), (closed, W), (saved, X)):
            with self.subTest(acknowledge=acknowledge):
                with self.assertRaises(PresetWriteRefused):
                    admit(current, A, acknowledge)


if __name__ == "__main__":
    unittest.main()
