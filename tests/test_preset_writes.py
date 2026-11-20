"""Folding journal rows into per-slot writes, and the repeat policy."""

import json
import unittest

from tests.frames import reply, u32
from xbloom_studio.preset_writes import (
    FINISHED,
    STARTED,
    SUPERSEDED,
    PresetWriteRefused,
    admit,
    attempt_evidence,
    fold,
    supersede,
)

A = {"slot": "A", "payload_hex": "6e6577", "flags": 4}
W, X = "1" * 32, "2" * 32


def row(kind, **data):
    return {"kind": kind, "at": "t", "data": json.dumps(data)}


def started(attempt, write=W, **preset):
    return row(STARTED, write_id=write, attempt_id=attempt, **{**A, **preset})


def finished(attempt, outcome, write=W, **sent):
    return row(
        FINISHED, write_id=write, attempt_id=attempt, slot="A", outcome=outcome, **sent
    )


def acknowledging(attempt, write, acknowledges, **preset):
    return row(
        STARTED,
        write_id=write,
        attempt_id=attempt,
        acknowledges=acknowledges,
        **{**A, **preset},
    )


def exhausted(write=W):
    rows = []
    for attempt in "abc":
        rows += [started(attempt, write), finished(attempt, "rejected", write)]
    return rows


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

    def test_an_acknowledgment_is_part_of_the_new_writes_first_record(self):
        rows = exhausted()
        self.assertEqual(fold(rows)["A"]["state"], "exhausted")
        replaced = fold(rows + [acknowledging("d", X, W, flags=5)], "d")["A"]
        self.assertEqual((replaced["write_id"], replaced["state"]), (X, "in_progress"))
        # A replacement that sent nothing leaves the old write in force.
        refused = rows + [
            acknowledging("d", X, W, flags=5),
            finished("d", "refused", X),
        ]
        self.assertEqual(fold(refused)["A"]["write_id"], W)
        self.assertEqual(fold(refused)["A"]["state"], "exhausted")
        self.assertEqual(admit(fold(refused)["A"], A, W), (None, False))
        # An interrupted replacement may have sent a frame: it stays in force.
        interrupted = rows + [acknowledging("d", X, W, flags=5)]
        self.assertEqual(fold(interrupted)["A"]["state"], "retryable")
        self.assertEqual(fold(interrupted)["A"]["write_id"], X)

    def test_a_replacement_that_provably_sent_nothing_keeps_a_stopped_write(self):
        rows = [*exhausted(), {**acknowledging("d", X, W, flags=5), "session_id": "s"}]
        rows[-1]["sequence"] = 9
        unsent = {"transmitted_write_count": 0, "partial_frame": False}
        # Cancelled before any preset byte, or interrupted before any durable
        # preset intent: the exhausted write stays in force and still stops.
        for tail, proof in (
            ([finished("d", "cancelled", X, **unsent)], "sent"),
            ([], "unsent"),
        ):
            with self.subTest(tail=tail):
                write = fold(rows + tail, evidence=lambda *_, p=proof: p)["A"]
                self.assertEqual((write["write_id"], write["state"]), (W, "exhausted"))
                self.assertEqual(admit(write, A, W), (None, False))
                with self.assertRaises(PresetWriteRefused):
                    admit(write, A, None)
        # A frame may have been sent, or its proof is missing: it stays.
        for tail, proof, state in (
            ([finished("d", "cancelled", X, transmitted_write_count=1)], "sent", 0),
            ([finished("d", "cancelled", X, partial_frame=True)], "sent", 0),
            ([finished("d", "cancelled", X)], "unsent", 0),
            ([], "sent", 1),
        ):
            with self.subTest(tail=tail, proof=proof):
                write = fold(rows + tail, evidence=lambda *_, p=proof: p)["A"]
                self.assertEqual(
                    (write["write_id"], write["state"]),
                    (X, ("cancelled", "retryable")[state]),
                )
        # Over a write that may still repeat, a cancellation never reopens it,
        # nor is a new first write's cancellation forgotten.
        retryable = [started("a"), finished("a", "rejected")]
        for prefix, rows_ in (
            (
                retryable,
                [acknowledging("d", X, W), finished("d", "cancelled", X, **unsent)],
            ),
            ([], [started("d", X), finished("d", "cancelled", X, **unsent)]),
        ):
            with self.subTest(prefix=prefix):
                write = fold(prefix + rows_)["A"]
                self.assertEqual(
                    (write["write_id"], write["state"], write["retry_allowed"]),
                    (X, "cancelled", False),
                )

    def test_journaled_save_evidence_resolves_an_interrupted_attempt(self):
        rows = [{**started("a"), "session_id": "s", "sequence": 7}]
        asked = []

        def evidence(session, sequence):
            asked.append((session, sequence))
            return "saved"

        write = fold(rows, evidence=evidence)["A"]
        self.assertEqual(asked, [("s", 7)])
        self.assertEqual(write["attempts"][0]["outcome"], "saved_interrupted")
        self.assertEqual((write["state"], write["retry_allowed"]), ("saved", False))
        self.assertEqual(fold(rows, "a", evidence)["A"]["state"], "in_progress")
        finished_rows = [*rows, finished("a", "rejected")]
        self.assertEqual(
            fold(finished_rows, evidence=evidence)["A"]["state"], "retryable"
        )
        self.assertEqual(len(asked), 1)

    def test_a_superseded_write_reopens_the_slot(self):
        rows = [
            started("a"),
            finished("a", "saved"),
            row(SUPERSEDED, write_id=W, slot="A"),
        ]
        current = fold(rows)["A"]
        self.assertEqual(
            (current["state"], current["retry_allowed"]), ("superseded", False)
        )
        self.assertEqual(admit(current, A, None), (None, False))
        with self.assertRaises(PresetWriteRefused):
            admit(current, A, W)
        self.assertEqual(fold(rows + [started("b", X)])["A"]["write_id"], X)

    def test_anything_unexpected_fails_closed(self):
        for rows in (
            [{"kind": STARTED, "at": "t", "data": "{"}],
            [started("a", slot="D")],
            [started("a"), started("b", flags=5)],
            [started("a"), finished("a", "saved"), finished("a", "saved")],
            [started("a"), finished("a", "readback")],
            [started("a"), finished("z", "saved")],
            [started("a"), row(SUPERSEDED, write_id=X, slot="A")],
            # A separate acknowledgment record never unlocks anything.
            [started("a"), row("preset_write.acknowledged", write_id=W, slot="A")],
            # A new write over an incomplete one needs its acknowledgment.
            [*exhausted(), started("d", X)],
            [started("a"), finished("a", "cancelled"), started("b", X)],
            [*exhausted(), acknowledging("d", X, "3" * 32)],
            [acknowledging("d", X, W)],
            [*exhausted(), acknowledging("d", W, W)],
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

    def test_acknowledgment_names_the_latest_open_write(self):
        saved = self.current(started("a"), finished("a", "saved"))
        self.assertEqual(admit(saved, A, W), (None, False))
        self.assertEqual(admit(saved, {**A, "flags": 1}, None), (None, False))
        for current, acknowledge in ((None, W), (saved, X)):
            with self.subTest(acknowledge=acknowledge):
                with self.assertRaises(PresetWriteRefused):
                    admit(current, A, acknowledge)


class BatchTests(unittest.TestCase):
    def test_a_batch_supersedes_resolved_writes_and_refuses_unresolved_ones(self):
        ledger = fold(
            [
                started("a"),
                finished("a", "saved"),
                row(STARTED, write_id=X, attempt_id="b", **{**A, "slot": "B"}),
                row(FINISHED, write_id=X, attempt_id="b", slot="B", outcome="refused"),
            ]
        )
        self.assertEqual(
            supersede(ledger, "ABCA"),
            [{"write_id": W, "slot": "A"}, {"write_id": X, "slot": "B"}],
        )
        for rows in (
            [started("a"), finished("a", "rejected")],
            [started("a"), finished("a", "cancelled")],
            exhausted(),
            [started("a")],
        ):
            with self.subTest(rows=rows), self.assertRaises(PresetWriteRefused):
                supersede(fold(rows), "AB")
        self.assertEqual(supersede(fold([started("a")]), "BC"), [])


def evidence(*rows):
    return [{"kind": kind, "data": json.dumps(data)} for kind, data in rows]


FRAME = "aabbccdd"
SAVE = {"device": 2, "function": 7, "command": 8023, "marker": 0xC1}


def packet(payload="25000000", **fields):
    return ("ble.packet", {**SAVE, "payload_hex": payload, **fields})


def intent(command=11510, frame=FRAME):
    return ("command.intent", {"command": command, "frame_hex": frame})


def chunk(hex_, kind="ble.outgoing_chunk"):
    return (kind, {"hex": hex_})


def incoming(*frames):
    return [("ble.incoming_chunk", {"hex": data.hex()}) for data in frames]


SAVED_FRAME = reply(8023, u32(0x25))


class EvidenceTests(unittest.TestCase):
    def test_a_raw_save_counts_before_its_decoded_packet_commits(self):
        sent = [intent(), chunk("aabb"), chunk("ccdd")]
        # A restart finds the raw chunk committed and the packet still queued.
        for rows in (
            [*sent, *incoming(SAVED_FRAME)],
            [*sent, *incoming(SAVED_FRAME[:5], SAVED_FRAME[5:])],
            # Corrupt input clears the buffer as the client does.
            [*sent, *incoming(b"\x00", SAVED_FRAME)],
            [*incoming(SAVED_FRAME[:5]), *sent, *incoming(SAVED_FRAME[5:])],
        ):
            with self.subTest(rows=rows):
                self.assertEqual(attempt_evidence(evidence(*rows)), "saved")
        for rows in (
            [*incoming(SAVED_FRAME), *sent],
            [intent(), chunk("aabb"), *incoming(SAVED_FRAME), chunk("ccdd")],
            [*sent, *incoming(reply(8023, u32(0x44)), reply(8023, u32(0x25), 0xC2))],
        ):
            with self.subTest(rows=rows):
                self.assertEqual(attempt_evidence(evidence(*rows)), "sent")

    def test_no_committed_preset_intent_proves_nothing_was_sent(self):
        for rows in ([], [packet()], [intent(8022, FRAME), chunk(FRAME)]):
            with self.subTest(rows=rows):
                self.assertEqual(attempt_evidence(evidence(*rows)), "unsent")
        self.assertEqual(attempt_evidence(evidence(intent())), "sent")

    def test_a_save_counts_only_after_a_preset_frame_could_reach_the_machine(self):
        sent = [intent(), chunk("aabb"), chunk("ccdd")]
        self.assertEqual(attempt_evidence(evidence(*sent, packet())), "saved")
        # The save followed the next frame's intent while its hook waited.
        self.assertEqual(
            attempt_evidence(evidence(*sent, intent(), packet(), chunk("aa"))),
            "saved",
        )
        for rows in (
            # Stale: before any preset frame, or during its unfinished chunks.
            [packet(), *sent],
            [intent(), chunk("aabb"), packet(), chunk("ccdd")],
            # The final chunk was withheld.
            [
                intent(),
                chunk("aabb"),
                chunk("ccdd"),
                chunk("ccdd", "ble.outgoing_chunk_withheld"),
                packet(),
            ],
            # Another command's frame, or not a save from the control channel.
            [intent(8022, "aabbccdd"), chunk("aabbccdd"), packet()],
            [*sent, packet("44000000")],
            [*sent, packet(marker=0xC2)],
            [*sent, packet(function=1)],
        ):
            with self.subTest(rows=rows):
                self.assertNotEqual(attempt_evidence(evidence(*rows)), "saved")


if __name__ == "__main__":
    unittest.main()
