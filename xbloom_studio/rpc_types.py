"""Remote domain errors without any database, device or platform imports."""

from __future__ import annotations

from typing import Any


class SocketDirectoryError(PermissionError):
    """The daemon socket directory does not meet its owner-only requirements."""


class RemoteError(RuntimeError):
    """A sanitized error envelope returned by the serving side."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(detail.get("message", "Remote request failed"))
