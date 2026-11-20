# Open Bloom Studio

Open Bloom Studio is an independent open-source project building local tools
for xBloom Studio coffee and tea machines: a Python library, the `xbloom`
command line with a local daemon, the `xbloom-mcp` server and agent skills.

This project is not affiliated with, endorsed by, or sponsored by xBloom.
Product names are used only to describe compatibility and remain the property
of their respective owners.

## Status

Alpha (0.0.1a0). Interfaces may change. Brewing coffee or tea from encoded
recipes, preset writes and some settings commands are not validated on
hardware and stay behind an explicit experimental opt-in.

## Install

Python 3.10 or newer. Install the tagged source with the extras you need:
`ble` for local Bluetooth through [Bleak](https://github.com/hbldh/bleak) and
`mcp` for the MCP server. The core library has no dependencies.

```sh
git clone --branch 0.0.1-alpha https://github.com/ceoloide/open-xbloom-studio-tools
pip install "./open-xbloom-studio-tools[ble,mcp]"
```

## Try it offline

The bundled simulator is a deterministic, in-memory machine for tests and
examples; it does not model physical timing.

```sh
xbloom recipe templates
xbloom --connector simulator --direct water --volume-ml 100 --key first --allow-control
xbloom operation lookup first
```

Every start needs an idempotency key. Repeating a matching request returns
its recorded attempt if one exists. After any uncertainty, run
`xbloom operation lookup KEY` with the original key. If it returns an
operation ID, use `xbloom history get ID` for details. If no attempt is found,
submitting another start can run the machine even with the original key.
Wait for fresh machine state and explicit authorization before retrying.
A start whose delivery stays unconfirmed is recorded as `unknown`.

## Use a machine

Controls require `--allow-control` (`allow_control=True` in Python); commands
that are not hardware-tested additionally require `--allow-untested`.
Read-only sessions never change settings.

`xbloom serve` keeps one owner of the machine and the database behind a
private Unix socket; other `xbloom` commands and `xbloom-mcp` then use it.
Set `BLOOM_DEVICE` to your Bluetooth address, then run the daemon in its
own terminal:

```sh
xbloom --device "$BLOOM_DEVICE" serve
```

After the daemon prints a JSON response with `serving`, use a second terminal:

```sh
xbloom machine connect
xbloom water --volume-ml 100 --key second --allow-control --wait
```

For experimental work, open the session with
`xbloom machine connect --allow-untested`. If you already opened a standard
session, run `xbloom machine disconnect` before reconnecting. Experimental
actions still need their own `--allow-untested`.

For an offline run, start the daemon with `xbloom --connector simulator serve`. Run the daemon and the CLI under the same account; they share
ownership locks in `~/.local/state/open-bloom/leases` under that account's
home. Stop any older daemon and finish direct-mode work before upgrading.
The default socket is `$XDG_RUNTIME_DIR/open-bloom/bloom.sock` when configured,
or `~/.local/state/open-bloom/run/bloom.sock` otherwise. A daemon that restarts
marks interrupted work `unknown` and does not replay it.

For a daemon on another host, add `--ssh <host-alias>` to any `xbloom`
command. It runs `xbloom relay` there over your SSH configuration with strict
host-key checking. Pin the relay as a forced command for that key, for example
`restrict,command="xbloom relay --socket /run/open-bloom/bloom.sock"`; it
forwards only a fixed method list.

### Other links

Any byte link implementing `xbloom_studio.Connector` (lifecycle, write size,
notifications and a stable machine identity; see `xbloom_studio/connector.py`)
runs behind the same handshake, control gates, acknowledgments, serialization
and uncertainty tracking. Pass `--connector package.module:factory` to `serve`
or `--direct`; the factory is called with the configured device (or `None`)
and returns the connector. In Python:

```python
import asyncio

from xbloom_studio import Interaction, ScriptedConnector, Studio


async def main():
    connector = ScriptedConnector()  # or BleakConnector("<address>"), or yours
    print(await Studio(connector=connector).read_device_info())
    async with Interaction(connector=connector, allow_control=True) as machine:
        print(await machine.dispense_water(volume_ml=100))


asyncio.run(main())
```

`xbloom_studio.facade.Application(store, connector=...)` exposes the same
`call(method, params)` dispatch as the CLI, daemon and MCP server.

## Recipes and history

Recipes are JSON documents validated offline (`xbloom recipe validate FILE`,
or `check_recipe` in Python), which reports every problem with its path and
allowed values. `xbloom recipe constraints` lists every field and bound. A
local SQLite library, by default under `$XDG_DATA_HOME/open-bloom/`, keeps
recipe revisions, aliases and every attempt with its exact plan, so a repeat
reuses the stored plan byte for byte.

The bundled starter recipes, coffee examples and tea profiles are original to
this project. Recipe imports are user-directed: one public recipe page at a
time, or an export you supply, read offline.

## MCP server and agent skills

`xbloom-mcp` serves the same methods as MCP tools over stdio and needs a
running daemon (`xbloom-mcp --ssh <host-alias>` for a remote one). Tools come
in three groups, so a client can be given only the roles it needs:

```json
{
  "mcpServers": {
    "bloom-recipes": {"command": "xbloom-mcp", "args": ["--group", "recipes"]},
    "bloom-history": {"command": "xbloom-mcp", "args": ["--group", "history"]},
    "bloom-machine": {"command": "xbloom-mcp", "args": ["--group", "machine"]}
  }
}
```

If the MCP host uses a different environment from the daemon, add
`--socket` and the daemon's absolute socket path to each local server's
`args`. This matters when the host does not inherit `XDG_RUNTIME_DIR`.

The `skills/` folder holds four standalone skills that follow the
[Agent Skills specification](https://agentskills.io/specification) and teach
an agent to use these tools safely: `bloom-recipes` (author, store and import
recipes), `bloom-tea` (craft tea drafts), `bloom-history` (find past brews and
add feedback) and `bloom-operate` (run, watch, stop and recover machine work).
Copy the folders into your agent's skills directory, for example
`~/.claude/skills/` for Claude Code. The skills ask for explicit user
permission before any physical action and never retry an uncertain start.

## Development

The repository uses [uv](https://docs.astral.sh/uv/) with a committed
lockfile. One command runs every check that CI runs: formatting, lint, strict
typing, the publication policy, the offline tests with coverage gates, and the
build and installed-artifact checks for the sdist and wheel, including the
CLI, daemon and MCP stdio server against the simulator.

```sh
uv run --locked python -m scripts.presubmit
```

Tests use no real machine.

## License

MIT. See [LICENSE](LICENSE).
