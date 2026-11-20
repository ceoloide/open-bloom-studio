"""Bounded identical retries of single-slot preset writes, from the journal.

One ``write_preset`` call is one attempt: three planned frames, never retried
inside the call. The daemon records each attempt in the existing event
journal (SQLite, append-only, ``synchronous=FULL``): ``started`` is committed
before the attempt's first frame, ``finished`` carries its outcome. A torn
transaction never becomes a row. An attempt without a recorded outcome is
``interrupted``, unless its own session's committed packets or raw incoming
chunks show a save after its first preset frame could reach the machine.
The same session evidence, up to its command's terminal record, also
reconciles a recorded outcome: a save that arrived while the outcome was
committing wins (``saved_late``), and a cancellation committed before the
command released its guard makes it ``cancelled``. Anything else unexpected
fails closed.

A write is the latest set of attempts for one machine and slot with identical
flags and payload. At most :data:`MAX_ATTEMPTS` attempts that may have sent a
frame belong to one write. Only an identical write may be repeated while it
is incomplete; an observed save or cancellation ends automatic repetition,
and only the user's explicit acknowledgment of the write ID (passed as
``acknowledge_incomplete``) starts a new set over an incomplete one. The
acknowledgment is part of the new write's first ``started`` record, and it
lapses while that write has no attempt that may have sent a frame: refused,
or, over a write that already stops repetition, cancelled or interrupted with
proof that no preset frame was sent. Before its first frame for a slot, a
batch write records ``superseded`` for that slot's write. Outcomes are machine
evidence, never a readback of stored contents.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .machine_controls import SLOTS
from .protocol import FrameBuffer, FrameError
from .storage import ConflictError

MAX_ATTEMPTS = 3
STARTED = "preset_write.started"
FINISHED = "preset_write.finished"
SUPERSEDED = "preset_write.superseded"
# Outcomes an attempt can record; "interrupted" is derived from no outcome.
OUTCOMES = frozenset(
    {
        "refused",
        "rejected",
        "unconfirmed",
        "cancelled",
        "saved",
        "saved_cleanup_failed",
    }
)
RETRYABLE = frozenset({"rejected", "unconfirmed", "interrupted"})
# Derived: "saved_interrupted" is a journaled save, then no outcome;
# "saved_late" a journaled save in the attempt's window despite its outcome.
SAVED = frozenset({"saved", "saved_cleanup_failed", "saved_interrupted", "saved_late"})
# States a batch must not overwrite: they await a repeat or the user.
UNRESOLVED = frozenset({"retryable", "exhausted", "cancelled", "in_progress"})
# States that already stop automatic repetition; preserving one reopens nothing.
STOPPED = frozenset({"exhausted", "cancelled"})
# Kinds an attempt's session evidence is read from, by the session index.
EVIDENCE_KINDS = (
    "command.intent",
    "command.write_failed",
    "ble.outgoing_chunk",
    "ble.outgoing_chunk_withheld",
    "ble.incoming_chunk",
    "ble.packet",
    "capture.gap",
    "machine.command_requested",
    "machine.command_finished",
    "machine.command_failed",
    STARTED,
)
# The command's terminal records: they end an attempt's evidence window.
TERMINAL = frozenset({"machine.command_finished", "machine.command_failed"})
PASSIVE = frozenset({"ble.incoming_chunk", "ble.packet"})
PRESET_FRAME = 11510
SAVE_PACKET = {
    "device": 2,
    "function": 7,
    "command": 8023,
    "marker": 0xC1,
    "payload_hex": "25000000",
}
# What an attempt's session proves: "saved", "cancelled", "sent" or "unsent".
Evidence = Callable[[str, int], str]


class PresetWriteRefused(ConflictError):
    """A write the retry policy refuses before anything is sent."""

    def __init__(self, message: str, write: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.write = write


def _is_save(fields: Mapping[str, Any]) -> bool:
    return all(fields.get(key) == value for key, value in SAVE_PACKET.items())


def _saves(frames: FrameBuffer, chunk: str) -> bool:
    try:
        packets = frames.feed(bytes.fromhex(chunk))
    except FrameError as exc:
        packets = exc.packets
    return any(
        _is_save(
            {
                "device": packet.device,
                "function": packet.function,
                "command": packet.command,
                "marker": packet.marker,
                "payload_hex": packet.payload.hex(),
            }
        )
        for packet in packets
    )


def attempt_evidence(rows: Iterable[Mapping[str, Any]]) -> str:
    """What committed session rows prove about one attempt.

    ``rows`` are one session's :data:`EVIDENCE_KINDS` rows after an attempt's
    ``started`` record, in order. Its window ends at its command's first
    terminal record; nothing after another command's request or another
    ``started`` record is the attempt's.

    ``"saved"``: in the window, a save followed a preset frame that could
    reach the machine. The correlation point matches the live attempt's: the
    transcript row of the chunk that completes a preset frame, unless the next
    row records that chunk as withheld. Rows are emitted in that order
    synchronously, so nothing interleaves between the two. A save counts from
    its decoded packet or, since a restart may find the raw chunk committed
    without the packet decoded from it, from the raw incoming chunks, decoded
    as the client does. Otherwise ``"cancelled"``: the command's terminal
    records include its cancellation. Otherwise ``"unsent"``: every preset
    frame's committed intent is followed by explicit proof that none of its
    bytes was written (its first chunk observed, withheld at once and the
    write failed, all emitted synchronously) and no row was lost. A bare
    intent stays ambiguous: ``"sent"``.
    """
    frame: str | None = None
    sent = ""
    pending = transmitted = intended = closed = cancelled = gap = False
    ambiguous = False
    # How far the current preset intent's proof of no bytes has come.
    proof: str | None = None
    proof_chunk = ""
    frames = FrameBuffer()
    for row in rows:
        kind, data = row["kind"], json.loads(row["data"])
        if kind in (STARTED, "machine.command_requested"):
            break
        if kind in TERMINAL:
            closed = True
            if kind == "machine.command_failed" and data.get("type") == (
                "CancelledError"
            ):
                cancelled = True
            continue
        if closed:
            continue
        if proof is not None and not (proof == "intent" and kind in PASSIVE):
            if (
                proof == "intent"
                and kind == "ble.outgoing_chunk"
                and (data["hex"] and (frame or "").startswith(data["hex"]))
            ):
                proof, proof_chunk = "chunk", data["hex"]
            elif (
                proof == "chunk"
                and kind == "ble.outgoing_chunk_withheld"
                and data["hex"] == proof_chunk
            ):
                proof = "withheld"
            elif (
                proof == "withheld"
                and kind == "command.write_failed"
                and data["command"] == PRESET_FRAME
            ):
                proof = None
            else:
                proof, ambiguous = None, True
        if pending:
            pending = False
            transmitted = transmitted or kind != "ble.outgoing_chunk_withheld"
        if kind == "command.intent":
            if proof is not None:
                proof, ambiguous = None, True
            sent = ""
            frame = data["frame_hex"] if data["command"] == PRESET_FRAME else None
            if frame is not None:
                proof, intended = "intent", True
        elif kind == "ble.outgoing_chunk" and frame is not None:
            sent += data["hex"]
            if sent == frame:
                pending, frame = True, None
        elif kind == "capture.gap":
            gap = True
        elif kind == "ble.packet" and transmitted and _is_save(data):
            return "saved"
        elif kind == "ble.incoming_chunk" and _saves(frames, data["hex"]):
            if transmitted:
                return "saved"
    if cancelled:
        return "cancelled"
    if proof is not None:
        ambiguous = True
    return "sent" if ambiguous or (gap and intended) else "unsent"


def _outcome(
    attempt: dict[str, Any], active: str | None, evidence: Evidence | None
) -> str:
    # Running, or still settling its outcome and command: not final yet.
    if attempt["attempt_id"] == active:
        return "in_progress"
    if "derived" not in attempt:
        recorded = attempt["outcome"]
        proof = "sent"
        if recorded not in SAVED and evidence is not None:
            proof = evidence(attempt["session_id"], attempt["sequence"])
        if recorded is None:
            derived = "saved_interrupted" if proof == "saved" else "interrupted"
        elif proof == "saved":
            # A save takes precedence over any other recorded outcome.
            derived = "saved_late"
        elif proof == "cancelled":
            derived = "cancelled"
        else:
            derived = recorded
        attempt["derived"] = derived
        attempt["unsent"] = recorded is None and proof == "unsent"
    return str(attempt["derived"])


def _unsent(
    attempt: dict[str, Any], active: str | None, evidence: Evidence | None
) -> bool:
    """Whether an attempt provably sent no preset frame."""
    outcome = _outcome(attempt, active, evidence)
    if outcome == "cancelled":
        return (
            attempt["transmitted_write_count"] == 0
            and attempt["partial_frame"] is False
        )
    return outcome == "refused" or (
        outcome == "interrupted" and attempt["unsent"] is True
    )


def _state(write: dict[str, Any], outcomes: list[str]) -> str:
    counted = [outcome for outcome in outcomes if outcome != "refused"]
    if write["superseded"]:
        return "superseded"
    if SAVED & set(counted):
        return "saved"
    if not counted:
        return "not_sent"
    if "cancelled" in counted:
        return "cancelled"
    if counted[-1] == "in_progress":
        return "in_progress"
    return "exhausted" if len(counted) >= MAX_ATTEMPTS else "retryable"


NEXT_STEPS = {
    "superseded": "A batch write may have replaced this slot since; its contents "
    "are unknown. A new write may start.",
    "saved": "A save event was observed (not a readback); no further write is "
    "needed. Identical contents are not written again without "
    "acknowledge_incomplete={write_id}.",
    "not_sent": "No frame was sent; the identical write may be submitted.",
    "cancelled": "Cancelled: stop and ask the user. Only after their direction, "
    "start a new write with acknowledge_incomplete={write_id}.",
    "in_progress": "An attempt is running; wait for its result.",
    "exhausted": "All {max} attempts were used without an observed save: stop "
    "and ask the user. Only after their direction, start a new write with "
    "acknowledge_incomplete={write_id}.",
    "retryable": "Disconnect, then resubmit write_preset with exactly this "
    "preset in a fresh session; {remaining} attempt(s) remain. Different "
    "contents need the user's direction and acknowledge_incomplete={write_id}.",
}


def _summary(
    write: dict[str, Any], active: str | None, evidence: Evidence | None
) -> dict[str, Any]:
    attempts = [
        {
            "attempt_id": attempt["attempt_id"],
            "started_at": attempt["started_at"],
            "outcome": _outcome(attempt, active, evidence),
            "transmitted_write_count": attempt["transmitted_write_count"],
        }
        for attempt in write["attempts"]
    ]
    outcomes = [attempt["outcome"] for attempt in attempts]
    state = _state(write, outcomes)
    used = sum(outcome != "refused" for outcome in outcomes)
    remaining = MAX_ATTEMPTS - used if state in ("retryable", "not_sent") else 0
    preset = {key: write[key] for key in ("slot", "payload_hex", "flags")}
    return {
        "write_id": write["write_id"],
        "preset": preset,
        "state": state,
        "attempts_used": used,
        "max_attempts": MAX_ATTEMPTS,
        "remaining_attempts": remaining,
        "retry_allowed": state in ("retryable", "not_sent"),
        "attempts": attempts,
        "next_step": NEXT_STEPS[state].format(
            write_id=write["write_id"], max=MAX_ATTEMPTS, remaining=remaining
        ),
        "readback_verified": False,
    }


def _effective(
    write: dict[str, Any] | None, active: str | None, evidence: Evidence | None
) -> dict[str, Any] | None:
    """An acknowledgment lapses while its write may not have sent a frame.

    Refusals always lapse it. Attempts cancelled or interrupted before any
    preset frame lapse it only over a write that already stops automatic
    repetition, so a cancellation never reopens one.
    """
    while write is not None and write["previous"] is not None:
        attempts = write["attempts"]
        if not all(
            _outcome(attempt, active, evidence) == "refused" for attempt in attempts
        ) and not (
            _summary(write["previous"], active, evidence)["state"] in STOPPED
            and all(_unsent(attempt, active, evidence) for attempt in attempts)
        ):
            break
        write = write["previous"]
    return write


def fold(
    rows: Iterable[Mapping[str, Any]],
    active: str | None = None,
    evidence: Evidence | None = None,
) -> dict[str, Any]:
    """The latest write per slot from journal rows in order; fail closed.

    ``evidence(session_id, sequence)`` reports what an attempt's session
    journaled (see :func:`attempt_evidence`); it is asked once per attempt
    without an observed save.
    """
    writes: dict[str, dict[str, Any] | None] = {}
    try:
        for row in rows:
            data = json.loads(row["data"])
            slot, write_id = data["slot"], data["write_id"]
            if slot not in SLOTS or not isinstance(write_id, str):
                raise ValueError("unknown slot or write ID")
            write = writes.get(slot)
            # An outcome belongs to the latest started write, even one whose
            # session evidence would let it lapse once it is unfinished.
            if row["kind"] != FINISHED:
                write = writes[slot] = _effective(write, active, evidence)
            if row["kind"] == STARTED:
                preset = {
                    "slot": slot,
                    "payload_hex": bytes.fromhex(data["payload_hex"]).hex(),
                    "flags": data["flags"],
                }
                acknowledges = data.get("acknowledges")
                if write is not None and write["write_id"] == write_id:
                    if acknowledges is not None or any(
                        write[key] != value for key, value in preset.items()
                    ):
                        raise ValueError("one write ID with different contents")
                else:
                    state = None
                    if write is not None:
                        state = _summary(write, active, evidence)["state"]
                    if acknowledges is None and state not in (
                        None,
                        "superseded",
                        "saved",
                        "not_sent",
                    ):
                        raise ValueError("a new write over an incomplete one")
                    if acknowledges is not None and (
                        write is None
                        or state == "superseded"
                        or acknowledges != write["write_id"]
                    ):
                        raise ValueError("acknowledgment of another write")
                    write = writes[slot] = {
                        "write_id": write_id,
                        **preset,
                        "superseded": False,
                        "previous": None if acknowledges is None else write,
                        "attempts": [],
                    }
                write["attempts"].append(
                    {
                        "attempt_id": data["attempt_id"],
                        "started_at": row["at"],
                        "session_id": row.get("session_id"),
                        "sequence": row.get("sequence"),
                        "outcome": None,
                        "transmitted_write_count": None,
                        "partial_frame": None,
                    }
                )
            elif write is None or write["write_id"] != write_id:
                raise ValueError("record for a write that is not the latest")
            elif row["kind"] == FINISHED:
                attempt = next(
                    item
                    for item in write["attempts"]
                    if item["attempt_id"] == data["attempt_id"]
                )
                if attempt["outcome"] is not None or data["outcome"] not in OUTCOMES:
                    raise ValueError("duplicate or unknown outcome")
                attempt["outcome"] = data["outcome"]
                # Derived while it was unfinished; derive it again.
                attempt.pop("derived", None)
                attempt["transmitted_write_count"] = data.get("transmitted_write_count")
                attempt["partial_frame"] = data.get("partial_frame")
            elif row["kind"] == SUPERSEDED:
                write["superseded"] = True
            else:
                raise ValueError("unknown record")
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        raise PresetWriteRefused(
            "The preset write journal is not readable; preset writes are refused "
            "until it is inspected"
        ) from exc
    result = {}
    for slot in SLOTS:
        write = _effective(writes.get(slot), active, evidence)
        if write is not None:
            result[slot] = _summary(write, active, evidence)
    return result


def admit(
    current: dict[str, Any] | None,
    preset: dict[str, Any],
    acknowledge: str | None,
) -> tuple[str | None, bool]:
    """Decide one new attempt: (write ID to continue or None for new, repeat).

    Raises :class:`PresetWriteRefused` when policy forbids sending anything.
    """
    if acknowledge is not None:
        if (
            current is None
            or current["state"] == "superseded"
            or acknowledge != current["write_id"]
        ):
            raise PresetWriteRefused(
                "acknowledge_incomplete must name this slot's latest "
                "unsuperseded write ID; nothing was sent",
                current,
            )
        return None, False
    if current is None or current["state"] == "superseded":
        return None, False
    same = current["preset"] == preset
    state, write_id = current["state"], current["write_id"]
    if state == "not_sent":
        return (write_id if same else None), False
    if state == "saved" and not same:
        return None, False
    if state == "retryable" and same:
        return write_id, True
    if state == "retryable":
        message = (
            f"Incomplete write {write_id} for slot {preset['slot']} has different "
            "contents; nothing was sent. Ask the user before acknowledging it."
        )
    elif state == "saved":
        message = (
            f"Write {write_id} already observed a save for these contents; "
            "nothing was sent."
        )
    else:
        message = (
            f"Write {write_id} for slot {preset['slot']} is {state}; nothing was "
            "sent. Stop and ask the user."
        )
    raise PresetWriteRefused(message, current)


def supersede(ledger: dict[str, Any], slots: Iterable[str]) -> list[dict[str, Any]]:
    """Records a batch over ``slots`` commits first; refuse unresolved writes.

    A batch stays outside the retry budget: it may not overwrite a write that
    awaits a repeat or the user, and every recorded write it may change is
    superseded, so no stale save or retry outlives it.
    """
    records = []
    for slot in dict.fromkeys(slots):
        write = ledger.get(slot)
        if write is None or write["state"] == "superseded":
            continue
        if write["state"] in UNRESOLVED:
            raise PresetWriteRefused(
                f"Write {write['write_id']} for slot {slot} is {write['state']}; "
                "a batch would overwrite it. Nothing was sent. Finish or "
                "acknowledge it with write_preset first.",
                write,
            )
        records.append({"write_id": write["write_id"], "slot": slot})
    return records
