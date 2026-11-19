"""Cooperative, per-user machine leases shared by library processes on a host."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import TextIO


def lease_directory() -> Path:
    return Path(tempfile.gettempdir()) / f"xbloom-{os.getuid()}"


class AddressLease:
    """Exclusive advisory lock for one machine identity or other owned resource.

    The lock file name is a hash of the key, so any spelling that normalizes to
    the same identity shares one lease. Requires POSIX ``fcntl``.
    """

    def __init__(self, address: str) -> None:
        self.address = address
        self._file: TextIO | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        import fcntl

        directory = lease_directory()
        directory.mkdir(mode=0o700, exist_ok=True)
        digest = hashlib.sha256(self.address.encode()).hexdigest()
        handle = (directory / f"{digest}.lock").open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise ConnectionError(
                "Another library process owns this Bluetooth address"
            ) from None
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        # Never unlink the lock: another process may already have opened its inode.
