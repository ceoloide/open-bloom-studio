# Open Bloom Studio

Open Bloom Studio is an independent open-source project building local tools
for xBloom Studio coffee and tea machines.

This project is not affiliated with, endorsed by, or sponsored by xBloom.
Product names are used only to describe compatibility and remain the property
of their respective owners.

## Status

Early development. Nothing is released yet.

## Usage

The core package has no runtime dependencies. Local Bluetooth support uses
[Bleak](https://github.com/hbldh/bleak) through the optional `ble` extra:

```sh
pip install "open-bloom-studio[ble]"
```

Read-only sessions register a telemetry session and never change settings.
Controls require an explicit `allow_control=True`; commands that are not
hardware-tested additionally require `allow_untested=True`.

```python
import asyncio

from xbloom_studio import Interaction, ScriptedConnector, Studio


async def main():
    # Any byte link implementing xbloom_studio.Connector can replace the
    # simulator, for example BleakConnector("<machine address>").
    connector = ScriptedConnector()
    print(await Studio(connector=connector).read_device_info())
    async with Interaction(connector=connector, allow_control=True) as machine:
        print(await machine.dispense_water(volume_ml=100))


asyncio.run(main())
```

`ScriptedConnector` with `SimulatedStudio` is a deterministic, in-memory
simulator for tests and examples; it does not model physical timing. The
library validates the session handshake, gates every control frame, waits for
acknowledgments, serializes and segments writes, and reports uncertain
outcomes above the connector, so every link runs behind the same checks.

## Recipes and history

Recipes are JSON documents validated offline with `check_recipe`, which
reports every problem with its path and allowed values. `compile_recipe`
turns a coffee or tea recipe into a plan holding the exact bytes to upload;
hardware execution of encoded recipes remains untested and requires
`allow_untested=True`.

```python
from xbloom_studio import check_recipe, recipe_templates
from xbloom_studio.recipes import compile_recipe

document = recipe_templates()[0]["document"]
assert check_recipe(document)["valid"]
plan = compile_recipe(document)
```

`xbloom_studio.storage.Store` keeps a local SQLite library of recipe
revisions, aliases and brew history, by default under
`$XDG_DATA_HOME/open-bloom/`. A repeated brew reuses its stored plan byte for
byte. `xbloom_studio.journal.JournalHooks` commits the intent before a normal
command writes its first byte. Stop commands proceed after a bounded journal
wait so a stalled disk cannot block a stop.

The bundled starter recipes, coffee examples and tea profiles are original to
this project. The example and tea lookups also accept your own collection
through a `path` argument. Recipe imports are user-directed: one public recipe
page at a time, or an owner-supplied xRecipe export read offline.

## Command line, daemon and MCP

Every interface calls the same allowlisted application methods, so Python,
`xbloom`, the daemon and `xbloom-mcp` behave alike. Try them offline with the
simulator:

```sh
xbloom --connector simulator --direct water --volume-ml 100 --key first --allow-control
xbloom operation lookup first
```

`xbloom serve` keeps one owner of the machine and the database behind a
private Unix socket; other `xbloom` commands then use it automatically.
Run the daemon and CLI under the same account with access to its home
directory. They share ownership locks in `~/.local/state/open-bloom/leases`,
where `~` is the service account's home. When upgrading from a build that
used temporary-directory locks, stop existing daemons and finish any
direct-mode operations before starting the new version.
Pass `--device <address>` (or set `BLOOM_DEVICE`) for local Bluetooth, or
`--connector package.module:factory` for any other connector.

```sh
xbloom --connector simulator serve &
xbloom machine connect
xbloom water --volume-ml 100 --key second --allow-control --wait
```

Every start needs an idempotency key. Reusing a key returns the recorded
attempt and never starts the machine again, whichever adapter sends it and
whether declared defaults are omitted or spelled out; after any uncertainty,
inspect `xbloom operation lookup <key>` instead of starting with a new key. A
start whose command delivery stays unconfirmed is recorded `unknown`, not
`failed`. A daemon that restarts marks interrupted work `unknown` and does not
replay it.

For a daemon on another host, `xbloom --ssh <host-alias> ...` runs
`xbloom relay` there over your existing SSH configuration with strict host-key
checking. Pin the relay as a forced command for that key, for example
`restrict,command="xbloom relay --socket /run/open-bloom/bloom.sock"`; it forwards only
a fixed method list, never backups. The MCP server needs the `mcp` extra and a
running daemon:

```sh
pip install "open-bloom-studio[mcp]"
xbloom-mcp            # or: xbloom-mcp --ssh <host-alias>
```

In Python, `xbloom_studio.facade.Application(store, connector=...)` exposes
the same `call(method, params)` dispatch.

## Development

The repository uses [uv](https://docs.astral.sh/uv/) with a committed lockfile.
One command runs every check that CI runs: formatting, lint, strict typing,
the publication policy, the offline test suite with line and branch coverage
gates, and the build, content and installation checks for the sdist and wheel.

```sh
uv run --locked python -m scripts.presubmit
```

Tests use no real machine. An in-process Python socket guard permits loopback
and local Unix sockets, with the system bus excluded.

## License

MIT. See [LICENSE](LICENSE).
