"""Folding journal rows into per-slot writes, and the repeat policy."""

import json
import unittest

from tests.frames import reply, u32
from xbloom_studio.preset_writes import (
    CLOSE_PENDING,
    CLOSED,
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

    def test_a_saved_write_survives_a_replacement_that_only_refused(self):
        saved = [started("a"), finished("a", "saved")]
        other = {"flags": 5}
        refused = saved + [started("b", X, **other), finished("b", "refused", X)]
        write = fold(refused)["A"]
        self.assertEqual((write["write_id"], write["state"]), (W, "saved"))
        with self.assertRaisesRegex(PresetWriteRefused, "already observed"):
            admit(write, A, None)
        # Still in force across further refused replacements.
        twice = refused + [started("c", "3" * 32), finished("c", "refused", "3" * 32)]
        self.assertEqual(fold(twice)["A"]["write_id"], W)
        # Preparation without a preset frame counts and keeps the replacement.
        prepared = saved + [
            started("b", X, **other),
            finished("b", "no_preset_frame", X),
        ]
        write = fold(prepared)["A"]
        self.assertEqual(
            [write[k] for k in ("write_id", "state", "attempts_used")],
            [X, "retryable", 1],
        )
        self.assertIn("No attempt sent a preset frame", write["next_step"])
        for preset in (A, {**A, "flags": 6}):
            with self.subTest(preset=preset), self.assertRaises(PresetWriteRefused):
                admit(write, preset, None)

    def test_preparation_without_a_preset_frame_is_bounded(self):
        rows = []
        for attempt in "abc":
            rows += [started(attempt), finished(attempt, "no_preset_frame")]
        write = fold(rows)["A"]
        self.assertEqual(
            [write[k] for k in ("state", "remaining_attempts", "retry_allowed")],
            ["exhausted", 0, False],
        )
        self.assertIn("No attempt sent a preset frame", write["next_step"])
        # Under an acknowledgment it never lapses back to the stopped write.
        replaced = fold(
            exhausted()
            + [acknowledging("d", X, W, flags=5), finished("d", "no_preset_frame", X)]
        )["A"]
        self.assertEqual((replaced["write_id"], replaced["state"]), (X, "retryable"))
        with self.assertRaises(PresetWriteRefused):
            admit(replaced, {**A, "flags": 5}, W)
        mixed = fold(
            [started("a"), finished("a", "rejected")]
            + [started("b"), finished("b", "no_preset_frame")]
        )["A"]
        self.assertNotIn("No attempt sent a preset frame", mixed["next_step"])

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
        self.assertEqual(len(asked), 1)

    def test_window_evidence_reconciles_a_recorded_outcome(self):
        rows = [{**started("a"), "session_id": "s", "sequence": 7}]
        sent = {"transmitted_write_count": 3, "partial_frame": False}
        for recorded in ("unconfirmed", "rejected", "cancelled"):
            tail = [*rows, finished("a", recorded, **sent)]
            with self.subTest(recorded=recorded):
                # A save in the attempt's window wins over its recorded outcome.
                write = fold(tail, evidence=lambda *_: "saved")["A"]
                self.assertEqual(write["attempts"][0]["outcome"], "saved_late")
                self.assertEqual(
                    (write["state"], write["retry_allowed"]), ("saved", False)
                )
                self.assertIn("no further write", write["next_step"])
                with self.assertRaises(PresetWriteRefused):
                    admit(write, A, None)
                # A committed cancellation of its command stops repetition.
                write = fold(tail, evidence=lambda *_: "cancelled")["A"]
                self.assertEqual(
                    (write["attempts"][0]["outcome"], write["state"]),
                    ("cancelled", "cancelled"),
                )
                self.assertFalse(write["retry_allowed"])
                self.assertIn("Cancelled", write["next_step"])
                # Nothing else changes it.
                for proof in ("sent", "unsent"):
                    write = fold(tail, evidence=lambda *_, p=proof: p)["A"]
                    self.assertEqual(write["attempts"][0]["outcome"], recorded)
        # A recorded save is final: its evidence is never read.
        asked = []
        write = fold(
            [*rows, finished("a", "saved", **sent)],
            evidence=lambda *args: asked.append(args) or "cancelled",
        )["A"]
        self.assertEqual((write["state"], asked), ("saved", []))
        # An outcome derived while unfinished is derived again once recorded.
        write = fold(
            [*rows, started("b", "3" * 32, slot="B"), finished("a", "unconfirmed")],
            evidence=lambda *_: "sent",
        )["A"]
        self.assertEqual(write["attempts"][0]["outcome"], "unconfirmed")
        # Still settling its command: in progress, not yet final.
        self.assertEqual(
            fold([*rows, finished("a", "unconfirmed")], "a")["A"]["state"],
            "in_progress",
        )

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

    def test_a_supersession_lapses_only_on_proof_that_no_preset_byte_was_sent(
        self,
    ):
        saved = [started("a"), finished("a", "saved")]
        record = {**row(SUPERSEDED, write_id=W, slot="A"), "session_id": "s"}
        asked = []

        def proving(*proofs):
            def evidence(session, sequence, cancellation=True):
                asked.append((session, sequence, cancellation))
                return proofs[sequence]

            return evidence

        # The batch's frame for the slot was withheld (or never intended):
        # the saved write stays in force and blocks an identical replay.
        current = fold([*saved, {**record, "sequence": 0}], evidence=proving("unsent"))[
            "A"
        ]
        self.assertEqual(asked, [("s", 0, False)])
        self.assertEqual((current["write_id"], current["state"]), (W, "saved"))
        with self.assertRaises(PresetWriteRefused):
            admit(current, A, None)
        self.assertEqual(supersede({"A": current}, "A"), [{"write_id": W, "slot": "A"}])
        # Anything else, any later batch that may have sent, or no session
        # to read keeps it superseded.
        for proof in ("sent", "saved", "cancelled"):
            with self.subTest(proof=proof):
                rows = [*saved, {**record, "sequence": 0}]
                current = fold(rows, evidence=proving(proof))["A"]
                self.assertEqual(current["state"], "superseded")
        later = [{**record, "sequence": 0}, {**record, "sequence": 1}]
        current = fold([*saved, *later], evidence=proving("unsent", "sent"))["A"]
        self.assertEqual(current["state"], "superseded")
        asked.clear()
        for rows, evidence in (
            (
                [*saved, {**record, "session_id": None, "sequence": 0}],
                proving("unsent"),
            ),
            ([*saved, {**record, "sequence": 0}], None),
        ):
            with self.subTest(rows=rows):
                self.assertEqual(
                    fold(rows, evidence=evidence)["A"]["state"], "superseded"
                )
        self.assertEqual(asked, [])

    def test_a_committed_cancellation_stops_an_attempt_without_an_outcome(self):
        rows = [{**started("a"), "session_id": "s", "sequence": 7}]
        write = fold(rows, evidence=lambda *_: "cancelled")["A"]
        self.assertEqual(
            (write["attempts"][0]["outcome"], write["state"], write["retry_allowed"]),
            ("cancelled", "cancelled", False),
        )
        with self.assertRaises(PresetWriteRefused):
            admit(write, A, None)
        # Never proof that it sent nothing: a replacement over a stopped
        # write keeps its acknowledgment.
        replaced = fold(
            [
                *exhausted(),
                {**acknowledging("d", X, W, flags=5), "session_id": "s", "sequence": 9},
            ],
            evidence=lambda *_: "cancelled",
        )["A"]
        self.assertEqual((replaced["write_id"], replaced["state"]), (X, "cancelled"))

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


def failed(command=11510):
    return ("command.write_failed", {"command": command, "error": "x"})


def terminal(kind="machine.command_finished", type_=None):
    data = {"command": "write_preset"}
    if type_ is not None:
        data.update(type=type_, error="")
    return (kind, data)


CANCELLED = terminal("machine.command_failed", "CancelledError")
# A preset intent whose first chunk was observed, then withheld before any
# byte was written, and whose write failed: explicit proof of no bytes.
EXCLUDED = [
    intent(),
    chunk("aabb"),
    chunk("aabb", "ble.outgoing_chunk_withheld"),
    failed(),
]


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

    def test_a_withheld_first_chunk_proves_no_preset_byte_was_sent(self):
        for rows in (
            EXCLUDED,
            # Reports may commit while the intent's hook waits.
            [intent(), packet(), *EXCLUDED[1:]],
            [*EXCLUDED, *incoming(SAVED_FRAME), terminal()],
            # A single-chunk frame withheld whole.
            [
                intent(frame="aabb"),
                chunk("aabb"),
                chunk("aabb", "ble.outgoing_chunk_withheld"),
                failed(),
                packet(),
            ],
            [*EXCLUDED, intent(8022, "58"), chunk("58")],
        ):
            with self.subTest(rows=rows):
                self.assertEqual(attempt_evidence(evidence(*rows)), "unsent")
        # Anything short of that explicit proof stays ambiguous.
        for rows in (
            [intent()],
            EXCLUDED[:2],
            EXCLUDED[:3],
            [*EXCLUDED[:3], failed(8022)],
            [intent(), failed()],
            [intent(), chunk("bbcc"), chunk("bbcc", "ble.outgoing_chunk_withheld")],
            [intent(), chunk("aabb"), chunk("aa", "ble.outgoing_chunk_withheld")],
            [intent(), chunk("aabb"), packet(), *EXCLUDED[2:]],
            [intent(), chunk("aabb"), chunk("ccdd"), *EXCLUDED[1:]],
            [intent(), chunk("aabb"), chunk("cc", "ble.outgoing_chunk_withheld")],
            [*EXCLUDED, *EXCLUDED[:1]],
            [*EXCLUDED, ("capture.gap", {"lost_events": 1})],
            [intent(), *EXCLUDED],
        ):
            with self.subTest(rows=rows):
                self.assertEqual(attempt_evidence(evidence(*rows)), "sent")

    def test_the_window_ends_at_the_commands_terminal_record(self):
        sent = [intent(), chunk("aabb"), chunk("ccdd")]
        # Saves before the terminal record count, even after the outcome.
        for end in (terminal(), CANCELLED, terminal("machine.command_failed", "X")):
            with self.subTest(end=end):
                self.assertEqual(
                    attempt_evidence(evidence(*sent, packet(), end)), "saved"
                )
                self.assertEqual(
                    attempt_evidence(evidence(*sent, *incoming(SAVED_FRAME), end)),
                    "saved",
                )
                # A later save is not this attempt's.
                self.assertNotEqual(
                    attempt_evidence(evidence(*sent, end, packet())), "saved"
                )
        # A save takes precedence over the command's cancellation.
        self.assertEqual(
            attempt_evidence(evidence(*sent, packet(), CANCELLED)), "saved"
        )
        # The command's cancellation, even after its terminal record.
        for rows in (
            [*sent, CANCELLED],
            [*sent, terminal(), CANCELLED],
            [*EXCLUDED, CANCELLED],
        ):
            with self.subTest(rows=rows):
                self.assertEqual(attempt_evidence(evidence(*rows)), "cancelled")
        # Nothing after another command's request or another attempt counts.
        for boundary in (
            ("machine.command_requested", {"command": "set_mode"}),
            (STARTED, {}),
        ):
            with self.subTest(boundary=boundary):
                self.assertEqual(
                    attempt_evidence(evidence(*sent, terminal(), boundary, CANCELLED)),
                    "sent",
                )
                self.assertEqual(
                    attempt_evidence(evidence(*sent, boundary, packet())), "sent"
                )
                self.assertEqual(
                    attempt_evidence(evidence(*EXCLUDED, boundary, intent())),
                    "unsent",
                )

    def test_a_pending_close_keeps_the_window_open_until_a_boundary(self):
        # The process stopped after a marked terminal record committed and
        # before CLOSED did: the capture was still open, so passive rows that
        # committed meanwhile are the attempt's.
        sent = [intent(), chunk("aabb"), chunk("ccdd")]
        marked, failed_marked = (
            (kind, {**data, CLOSE_PENDING: True})
            for kind, data in (terminal(), CANCELLED)
        )
        closed = (CLOSED, {"command": "write_preset"})
        for end in (marked, failed_marked):
            with self.subTest(end=end):
                for save in ([packet()], incoming(SAVED_FRAME)):
                    self.assertEqual(
                        attempt_evidence(evidence(*sent, end, *save)), "saved"
                    )
                # A save wins over a durable cancellation.
                self.assertEqual(
                    attempt_evidence(evidence(*sent, end, CANCELLED, packet())),
                    "saved",
                )
        # No committed save stays unknown; a cancellation still counts.
        self.assertEqual(attempt_evidence(evidence(*sent, marked)), "sent")
        self.assertEqual(
            attempt_evidence(evidence(*sent, marked, CANCELLED)), "cancelled"
        )
        # Only after a preset frame could reach the machine.
        for rows in (
            [*EXCLUDED, marked, packet()],
            [intent(), chunk("aabb"), marked, packet()],
            [*sent, chunk("ccdd", "ble.outgoing_chunk_withheld"), marked, packet()],
        ):
            with self.subTest(rows=rows):
                self.assertNotEqual(attempt_evidence(evidence(*rows)), "saved")
        # A committed close, the next command or attempt, another frame's
        # write or lost rows end it; an unmarked record ends it itself, even
        # when a later terminal record is marked.
        for rows in (
            [*sent, marked, closed, packet()],
            [*sent, marked, ("machine.command_requested", {}), packet()],
            [*sent, marked, (STARTED, {}), packet()],
            [*sent, marked, intent(8022, "58"), packet()],
            [*sent, marked, chunk("58"), packet()],
            [*sent, marked, chunk("58", "ble.outgoing_chunk_withheld"), packet()],
            [*sent, marked, failed(8022), packet()],
            [*sent, marked, ("capture.gap", {"lost_events": 1}), packet()],
            [*sent, terminal(), packet()],
            [*sent, terminal(), marked, packet()],
            [*sent, CANCELLED, packet()],
        ):
            with self.subTest(rows=rows):
                self.assertNotEqual(attempt_evidence(evidence(*rows)), "saved")

    def test_a_supersession_asks_only_whether_a_preset_byte_was_sent(self):
        for rows, proof in (
            ([*EXCLUDED, CANCELLED], "unsent"),
            ([CANCELLED], "unsent"),
            ([intent(), CANCELLED], "sent"),
            ([intent(), chunk("aabb"), failed(), CANCELLED], "sent"),
            ([intent(), chunk("aabb"), chunk("ccdd"), packet(), CANCELLED], "saved"),
        ):
            with self.subTest(rows=rows):
                self.assertEqual(attempt_evidence(evidence(*rows), False), proof)
        self.assertEqual(attempt_evidence(evidence(*EXCLUDED, CANCELLED)), "cancelled")

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
