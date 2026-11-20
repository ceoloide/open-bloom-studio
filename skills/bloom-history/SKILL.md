---
name: bloom-history
description: Find past Open Bloom brews, record tasting feedback, inspect what was sent and save a past brew as a recipe. Use when the user refers to an earlier brew, says how one tasted or asks what was brewed. Repeating a brew belongs to bloom-operate.
license: MIT
compatibility: Needs the open-bloom-studio xbloom command or the history tools of xbloom-mcp.
---

# Open Bloom history

Every attempt is stored with its exact recipe snapshot, compiled plan,
outcome, events and feedback. Use these records, not conversation memory, to
identify a brew. Reading history never connects to the machine.

Tool names are those of `xbloom-mcp --group history`; a client may prefix
them. Without MCP, the `history` commands of `xbloom` do the same. Do not
start a daemon only to read history.

## Find the brew

`history_list(query, status, min_rating, liked, ...)` returns pages of
compact summaries (`xbloom history list --page`). Follow `next_cursor` with
identical filters while `has_more` is true, even after an empty page. If
several brews fit, show the candidates and let the user pick before adding
feedback or repeating anything. `liked=true` only matches explicit ratings of
4 or more; search the text for unrated notes.

`history_get(ident)` (`xbloom history get ID`) returns one attempt in full:
the request, the retained plan, the recorded status and all feedback.
Statuses mean different things: `completed` is an observed completion,
`failed` and `stopped` are recorded outcomes, and `unknown` means delivery or
completion could not be confirmed. Never present `unknown` as success or as
failure.

## Feedback

`history_feedback(ident, note, rating)`
(`xbloom history feedback ID "note" --rating N`) appends a note to an
explicit operation ID. "That was good" is a note; add a 1 to 5 rating only
when the user gives one. Feedback is append-only: correct it with a new note.

## Inspect, save and repeat

- `history_events(operation_id, cursor, limit, detail)` pages the recorded
  events. Follow `next_cursor`; pass either `after` or `cursor`, not both.
  Gaps and recording faults stay visible. For a full analysis, redirect
  `xbloom history export ID` (one JSON event per line) to a file instead of
  loading every event into the conversation.
- `history_save_recipe(operation_id, name)` saves the brew's snapshot as a
  named local recipe. It does not write machine presets or publish anything.
- "Brew that again" means repeating the stored plan exactly, not the latest
  revision of a recipe with the same name. Hand
  `{"kind": "repeat", "operation_id": "<id>"}` to the `bloom-operate` skill;
  repeating is a new machine action that needs the user's go-ahead. The
  machine's own settings and calibration may still differ from last time.
