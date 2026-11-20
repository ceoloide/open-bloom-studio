"""Bounded identical retries of single-slot preset writes, from the journal.

One ``write_preset`` call is one attempt: three planned frames, never retried
inside the call. The daemon records each attempt in the existing event
journal (SQLite, append-only, ``synchronous=FULL``): ``started`` is committed
before the attempt's first frame, ``finished`` carries its outcome. A torn
transaction never becomes a row. An attempt without a recorded outcome is
``interrupted``, unless its own session's committed packets show a save after
its first preset frame could reach the machine. Anything else unexpected
fails closed.

A write is the latest set of attempts for one machine and slot with identical
flags and payload. At most :data:`MAX_ATTEMPTS` attempts that may have sent a
frame belong to one write. Only an identical write may be repeated while it
is incomplete; an observed save or cancellation ends automatic repetition,
and only the user's explicit acknowledgment of the write ID (passed as
``acknowledge_incomplete``) starts a new set over an incomplete one. The
acknowledgment is part of the new write's first ``started`` record, and it
lapses while that write has no attempt that may have sent a frame. A batch
write that may have changed a slot first records ``superseded`` for the slot's
write. Outcomes are machine evidence, never a readback of stored contents.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .machine_controls import SLOTS
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
# "saved_interrupted" is derived: a journaled save, then no outcome.
SAVED = frozenset({"saved", "saved_cleanup_failed", "saved_interrupted"})
# States a batch must not overwrite: they await a repeat or the user.
UNRESOLVED = frozenset({"retryable", "exhausted", "cancelled", "in_progress"})
# Kinds an attempt's session evidence is read from, by the session index.
EVIDENCE_KINDS = (
    "command.intent",
    "ble.outgoing_chunk",
    "ble.outgoing_chunk_withheld",
    "ble.packet",
)
PRESET_FRAME = 11510
SAVE_PACKET = {
    "device": 2,
    "function": 7,
    "command": 8023,
    "marker": 0xC1,
    "payload_hex": "25000000",
}
Evidence = Callable[[str, int], bool]


class PresetWriteRefused(ConflictError):
    """A write the retry policy refuses before anything is sent."""

    def __init__(self, message: str, write: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.write = write


def saved_after_transmission(rows: Iterable[Mapping[str, Any]]) -> bool:
    """Whether committed session rows show a save after a preset frame was sent.

    ``rows`` are one session's :data:`EVIDENCE_KINDS` rows after an attempt's
    ``started`` record, in order. The correlation point matches the live
    attempt's: the transcript row of the chunk that completes a preset frame,
    unless the next row records that chunk as withheld. Rows are emitted in
    that order synchronously, so nothing interleaves between the two.
    """
    frame: str | None = None
    sent = ""
    pending = transmitted = False
    for row in rows:
        kind, data = row["kind"], json.loads(row["data"])
        if pending:
            pending = False
            transmitted = transmitted or kind != "ble.outgoing_chunk_withheld"
        if kind == "command.intent":
            sent = ""
            frame = data["frame_hex"] if data["command"] == PRESET_FRAME else None
        elif kind == "ble.outgoing_chunk" and frame is not None:
            sent += data["hex"]
            if sent == frame:
                pending, frame = True, None
        elif kind == "ble.packet" and transmitted:
            if all(data.get(key) == value for key, value in SAVE_PACKET.items()):
                return True
    return False


def _outcome(
    attempt: dict[str, Any], active: str | None, evidence: Evidence | None
) -> str:
    if attempt["outcome"] is not None:
        return str(attempt["outcome"])
    if attempt["attempt_id"] == active:
        return "in_progress"
    if "derived" not in attempt:
        saved = evidence is not None and evidence(
            attempt["session_id"], attempt["sequence"]
        )
        attempt["derived"] = "saved_interrupted" if saved else "interrupted"
    return str(attempt["derived"])


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
    """An acknowledgment lapses while its write may not have sent a frame."""
    while (
        write is not None
        and write["previous"] is not None
        and all(
            _outcome(attempt, active, evidence) == "refused"
            for attempt in write["attempts"]
        )
    ):
        write = write["previous"]
    return write


def fold(
    rows: Iterable[Mapping[str, Any]],
    active: str | None = None,
    evidence: Evidence | None = None,
) -> dict[str, Any]:
    """The latest write per slot from journal rows in order; fail closed.

    ``evidence(session_id, sequence)`` reports a journaled save for an
    attempt without an outcome; it is asked only for such attempts.
    """
    writes: dict[str, dict[str, Any] | None] = {}
    try:
        for row in rows:
            data = json.loads(row["data"])
            slot, write_id = data["slot"], data["write_id"]
            if slot not in SLOTS or not isinstance(write_id, str):
                raise ValueError("unknown slot or write ID")
            write = writes[slot] = _effective(writes.get(slot), active, evidence)
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
                attempt["transmitted_write_count"] = data.get("transmitted_write_count")
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
