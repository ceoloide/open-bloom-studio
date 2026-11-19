# Open Bloom Studio

Open Bloom Studio is an independent open-source project building local tools
for xBloom Studio coffee and tea machines.

This project is not affiliated with, endorsed by, or sponsored by xBloom.
Product names are used only to describe compatibility and remain the property
of their respective owners.

## Status

Early development. Nothing is released yet.

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
