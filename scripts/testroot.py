"""Private test roots that keep the suite off the account's real state.

A root holds the account home, data and runtime directories for one test
process tree. ``XBLOOM_STUDIO_TEST_ROOT`` tells child processes that their
tree already has one. These are pure functions: importing this module has no
side effect and never imports the tests or the package under test.
"""

from __future__ import annotations

import tempfile
from collections.abc import MutableMapping
from pathlib import Path

ROOT = "XBLOOM_STUDIO_TEST_ROOT"
DEVICE = "XBLOOM_STUDIO_DEVICE"
PREFIX = "xbs-test-"
LAYOUT = {"HOME": "home", "XDG_DATA_HOME": "data", "XDG_RUNTIME_DIR": "run"}


def create(prefix: str = PREFIX) -> Path:
    """Make a new, empty, private root in the default temporary directory."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def environment(root: Path) -> dict[str, str]:
    """Make the root's directories and return the variables that select them."""
    variables = {ROOT: str(root)}
    for name, child in LAYOUT.items():
        path = root / child
        path.mkdir(mode=0o700, exist_ok=True)
        variables[name] = str(path)
    return variables


def apply(environ: MutableMapping[str, str], root: Path) -> None:
    """Point ``environ`` at ``root`` and drop the user's default device."""
    environ.update(environment(root))
    environ.pop(DEVICE, None)
