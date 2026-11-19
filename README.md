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
byte. `xbloom_studio.journal.JournalHooks` records each command durably before
its first byte is written.

The bundled starter recipes, coffee examples and tea profiles are original to
this project. The example and tea lookups also accept your own collection
through a `path` argument. Recipe imports are user-directed: one public recipe
page at a time, or an owner-supplied xRecipe export read offline.

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
