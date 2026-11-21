"""Give every test process tree a private account home, data and runtime root.

A process that starts without ``XBLOOM_STUDIO_TEST_ROOT`` owns a new root: it
points ``HOME`` and the XDG directories at it, drops ``XBLOOM_STUDIO_DEVICE``
and deletes the root when it exits. A process that inherits the variable
changes no environment and deletes nothing. Both replace ``pwd.getpwuid`` so
the production lease resolver, which reads the account home rather than
``HOME``, also lands inside the root.
"""

from __future__ import annotations

import atexit
import os
import pwd
import shutil
from pathlib import Path

from scripts import testroot

root: Path | None = None
owner = False
account_getpwuid = pwd.getpwuid


def _getpwuid(uid: int) -> pwd.struct_passwd:
    entry = account_getpwuid(uid)
    if root is None or uid != os.getuid():
        return entry
    fields = list(entry)
    fields[5] = str(root / "home")
    return pwd.struct_passwd(fields)


def _remove(path: Path, pid: int) -> None:
    # A forked child inherits this handler; only the creator deletes the root.
    if os.getpid() == pid:
        shutil.rmtree(path, ignore_errors=True)


def install() -> None:
    global root, owner
    if root is not None:
        return
    inherited = os.environ.get(testroot.ROOT)
    if inherited:
        if not (Path(inherited) / "home").is_dir():
            raise RuntimeError(
                f"{testroot.ROOT}={inherited!r} is not a test root; unset it"
            )
        root = Path(inherited)
    else:
        root = testroot.create()
        testroot.apply(os.environ, root)
        owner = True
        atexit.register(_remove, root, os.getpid())
    pwd.getpwuid = _getpwuid
