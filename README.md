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
