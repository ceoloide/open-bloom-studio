"""Keep tests offline: only loopback and local Unix sockets may connect.

Name lookups for anything but local names fail, other address families
(for example Bluetooth) cannot connect, and Unix sockets cannot reach the
D-Bus system bus that local Bluetooth stacks are controlled through.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

LOCAL_NAMES = frozenset({None, "", "localhost", "localhost.localdomain"})
SYSTEM_BUS_PATHS = frozenset(
    {"/run/dbus/system_bus_socket", "/var/run/dbus/system_bus_socket"}
)


class NetworkBlocked(OSError):
    pass


_installed = False
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _is_local_host(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode()
    if host in LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _system_bus_paths() -> set[str]:
    paths = set(SYSTEM_BUS_PATHS)
    for part in os.environ.get("DBUS_SYSTEM_BUS_ADDRESS", "").split(";"):
        for option in part.partition(":")[2].split(","):
            key, _, value = option.partition("=")
            if key == "path":
                paths.add(value)
    return paths


def check_address(family: int, address: Any) -> None:
    if family == getattr(socket, "AF_UNIX", None):
        path = os.fsdecode(address) if isinstance(address, (str, bytes)) else ""
        if path and os.path.realpath(path) in {
            os.path.realpath(p) for p in _system_bus_paths()
        }:
            raise NetworkBlocked("tests may not connect to the system bus")
        return
    if family in (socket.AF_INET, socket.AF_INET6) and _is_local_host(address[0]):
        return
    raise NetworkBlocked(f"tests may not connect to {family!r} {address!r}")


def _connect(self: socket.socket, address: Any) -> None:
    check_address(self.family, address)
    _real_connect(self, address)


def _connect_ex(self: socket.socket, address: Any) -> int:
    check_address(self.family, address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if not _is_local_host(host):
        raise NetworkBlocked(f"tests may not resolve {host!r}")
    return _real_getaddrinfo(host, *args, **kwargs)


def install() -> None:
    global _installed
    if _installed:
        return
    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = _getaddrinfo
    _installed = True


def installed() -> bool:
    return (
        _installed
        and socket.socket.connect is _connect
        and socket.getaddrinfo is _getaddrinfo
    )
