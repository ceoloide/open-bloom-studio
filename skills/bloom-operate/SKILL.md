---
name: bloom-operate
description: Prepare, start, watch, stop and recover xBloom Studio machine actions (water, grinding, stored or inline recipes, exact repeats, settings and scale) through the Open Bloom daemon. Use when the user asks the machine to do something or asks about a running or uncertain operation, not for recipe authoring.
license: MIT
compatibility: Needs a running open-bloom-studio daemon (xbloom serve) reached through the machine tools of xbloom-mcp or the xbloom command, and access to the machine.
---

# Operate the machine

Machine tools are those of `xbloom-mcp --group machine`; a client may prefix
them. They reach one daemon (`xbloom serve`), locally or through
`--ssh HOST`, which is the machine's only owner. If the daemon is not
reachable, report the setup problem. Never fall back to a second Bluetooth
connection, a direct mode or another host.

## Permission comes first

Physical actions need the user's explicit request for that action in this
conversation. Installing the tools, saving a recipe, a recipe link or an
earlier unrelated brew is not permission. Pass `allow_control=true` only for
an action the user asked for, and `allow_untested=true` only when the user
accepts that the action is experimental (native coffee and tea recipes and
some commands, as reported by the tools). Do not ask again for something the
user already answered in this session.

## Look before acting

- `machine_state` returns the cached state and never connects. Check the
  device, freshness and recording health; an empty or stale cache is not a
  current reading. `detail="full"` returns everything.
- `machine_connect` opens and keeps a monitoring connection. A new
  connection can beep, and the machine accepts one Bluetooth owner at a time.
- `machine_prepare(request)` previews offline and never actuates. Requests:
  - water: `{"kind": "water", "volume_ml": 150, "temperature_c": 90, "flow_ml_s": 3, "pattern": "spiral"}`
  - grind: `{"kind": "grind", "size": 40, "rpm": 100, "duration_s": 5}`
    from settings the user gave; never guess a grind profile
  - stored recipe: `{"kind": "recipe", "reference": "ID or alias", "revision": 2}`
  - inline recipe: `{"kind": "recipe", "document": {...}}`
  - exact repeat: `{"kind": "repeat", "operation_id": "previous-id"}`

Start the preview's `resolved_request`, which pins the recipe ID and
revision you showed, and pass the preview's digest as
`expected_recipe_sha256` so changed content is rejected. Re-sending an alias
could select a newer revision. A requested temperature is a target, not a
measured outlet temperature.

## Start, watch, stop

`operation_start(request, idempotency_key, allow_control, allow_untested,
expected_recipe_sha256)` starts one attempt and returns its operation ID.
Use a new unique key for each intended attempt and keep it.

The daemon keeps running the operation if the caller disconnects. Follow it
with `history_get(ident)` for the recorded status and `machine_state` for
live readings; page `history_events` only when diagnosing.

- `operation_stop(operation_id)` stops it through the owning session, also
  during waits and host gates.
- `operation_pause_resume(operation_id, resume)` works only during an active
  device step.
- A host recipe step of kind `user` waits at a gate. Ask the user to do the
  physical step and confirm it, then call
  `operation_acknowledge(operation_id, token)` with that exact gate's token.
  It never advances a prompt shown on the machine itself; those are handled on
  the machine.

Report outcomes precisely: a rejected request did nothing; an ACK means the
machine received a command; observed activity and observed completion come
from telemetry; `unknown` means the outcome could not be confirmed.

## After any uncertainty

If a start times out, the connection drops, a response is lost or a status is
`unknown`:

1. Call read-only `operation_lookup(idempotency_key)` with the original key,
   then `history_get` and `machine_state`.
2. Never start again automatically and never switch to a new key to force a
   second attempt. Reusing the same key only returns the recorded attempt. A
   missing lookup result does not prove the request never arrived.
3. Explain what is known and let the user check the machine. A retry needs
   fresh state and the user's explicit go-ahead.

## Upload now, start later

`recipe_upload(request, allow_control, allow_untested,
expected_recipe_sha256)` uploads one native coffee or tea recipe without
starting it and returns a single-use token. `recipe_start_uploaded(token,
idempotency_key, allow_control)` starts it once;
`recipe_cancel_uploaded(token, allow_control)` leaves preparation. Other
writes are rejected while a recipe is prepared. A disconnect invalidates the
token without proving preparation was cancelled, and a token is not a readback
of the stored recipe.

## Settings and scale

Read `machine_capabilities` first: it lists each command's parameter schema
and required opt-ins. Then call `machine_command(command, params,
allow_control, allow_untested)`. Unknown fields are rejected. An ACK confirms
receipt, not the physical effect. Scale use is `enter_scale`, `tare`, then
`exit_scale` on the same connection. Calibration and firmware commands are not
available.

Preset writes are experimental and need backups of the slots' previous
contents that the user supplies; never build them from local aliases. A
successful write still reports `readback_verified=false`: nothing read the
preset back. If a write fails partway, report `recovery_required` and the
saved backup and stop.

## Errors

Tool errors carry `error.code`, `details` and `next_step`. If an error says a
start may already have happened, inspect its operation ID or look it up by
key; do not start again.

From the command line, the `water`, `grind`, `brew`, `history repeat` and
`machine` commands of `xbloom` call the same methods, with `--key`,
`--allow-control`, `--allow-untested` and `--wait`. For example
`xbloom history repeat ID --key KEY --allow-control --wait` repeats a past
brew exactly; recover with `xbloom operation lookup KEY`.
