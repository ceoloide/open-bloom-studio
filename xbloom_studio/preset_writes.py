"""Bounded identical retries of single-slot preset writes, from the journal.

One ``write_preset`` call is one attempt: three planned frames, never retried
inside the call. The daemon records each attempt in the existing event
journal (SQLite, append-only, ``synchronous=FULL``): ``started`` is committed
before the attempt's first frame, ``finished`` carries its outcome. A torn
transaction never becomes a row, and an attempt without a recorded outcome
counts as ``interrupted``. Anything else unexpected fails closed.

A write is the latest set of attempts for one machine and slot with identical
flags and payload. At most :data:`MAX_ATTEMPTS` attempts that may have sent a
frame belong to one write. Only an identical write may be repeated while it
is incomplete; an observed save or cancellation ends automatic repetition,
and only the user's explicit acknowledgment of the write ID (passed as
``acknowledge_incomplete``) starts a new set over an incomplete one. Outcomes
are machine evidence, never a readback of stored contents.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .machine_controls import SLOTS
from .storage import ConflictError

MAX_ATTEMPTS = 3
STARTED = "preset_write.started"
FINISHED = "preset_write.finished"
ACKNOWLEDGED = "preset_write.acknowledged"
KINDS = (STARTED, FINISHED, ACKNOWLEDGED)
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
SAVED = frozenset({"saved", "saved_cleanup_failed"})


class PresetWriteRefused(ConflictError):
    """A write the retry policy refuses before anything is sent."""

    def __init__(self, message: str, write: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.write = write


def _state(write: dict[str, Any], outcomes: list[str]) -> str:
    counted = [outcome for outcome in outcomes if outcome != "refused"]
    if write["acknowledged"]:
        return "acknowledged"
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
    "acknowledged": "Acknowledged after user direction; a new write may start.",
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


def _summary(write: dict[str, Any], active: str | None) -> dict[str, Any]:
    attempts = [
        {
            **attempt,
            "outcome": attempt["outcome"]
            or ("in_progress" if attempt["attempt_id"] == active else "interrupted"),
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


def fold(
    rows: Iterable[Mapping[str, Any]], active: str | None = None
) -> dict[str, Any]:
    """The latest write per slot from journal rows in order; fail closed."""
    writes: dict[str, dict[str, Any]] = {}
    try:
        for row in rows:
            data = json.loads(row["data"])
            slot, write_id = data["slot"], data["write_id"]
            if slot not in SLOTS or not isinstance(write_id, str):
                raise ValueError("unknown slot or write ID")
            write = writes.get(slot)
            if row["kind"] == STARTED:
                preset = {
                    "slot": slot,
                    "payload_hex": bytes.fromhex(data["payload_hex"]).hex(),
                    "flags": data["flags"],
                }
                if write is None or write["write_id"] != write_id:
                    write = writes[slot] = {
                        "write_id": write_id,
                        **preset,
                        "acknowledged": False,
                        "attempts": [],
                    }
                elif any(write[key] != value for key, value in preset.items()):
                    raise ValueError("one write ID with different contents")
                write["attempts"].append(
                    {
                        "attempt_id": data["attempt_id"],
                        "started_at": row["at"],
                        "outcome": None,
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
            elif row["kind"] == ACKNOWLEDGED:
                write["acknowledged"] = True
            else:
                raise ValueError("unknown record")
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        raise PresetWriteRefused(
            "The preset write journal is not readable; preset writes are refused "
            "until it is inspected"
        ) from exc
    return {slot: _summary(writes[slot], active) for slot in SLOTS if slot in writes}


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
            or current["state"] == "acknowledged"
            or acknowledge != current["write_id"]
        ):
            raise PresetWriteRefused(
                "acknowledge_incomplete must name this slot's latest "
                "unacknowledged write ID; nothing was sent",
                current,
            )
        return None, False
    if current is None or current["state"] == "acknowledged":
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
