"""Keep tests offline: only loopback and local Unix sockets may connect.

Name lookups for anything but local names fail, other address families
(for example Bluetooth) cannot connect, and Unix sockets cannot reach the
D-Bus system bus that local Bluetooth stacks are controlled through. This
guards audited socket operations in the test process.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from collections.abc import Callable
from functools import wraps
from typing import Any

LOCAL_NAMES = frozenset({None, "", "localhost", "localhost.localdomain"})
SYSTEM_BUS_PATHS = frozenset(
    {"/run/dbus/system_bus_socket", "/var/run/dbus/system_bus_socket"}
)


class NetworkBlocked(OSError):
    pass


_installed = False


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
            elif key == "abstract":
                paths.add("\0" + value)
    return paths


def check_address(family: int, address: Any) -> None:
    if family == getattr(socket, "AF_UNIX", None):
        path = os.fsdecode(address) if isinstance(address, (str, bytes)) else ""
        bus_paths = _system_bus_paths()
        if path.startswith("\0"):
            system_bus = path in bus_paths
        else:
            system_bus = bool(path) and os.path.realpath(path) in {
                os.path.realpath(item)
                for item in bus_paths
                if not item.startswith("\0")
            }
        if system_bus:
            raise NetworkBlocked("tests may not connect to the system bus")
        return
    if family in (socket.AF_INET, socket.AF_INET6) and _is_local_host(address[0]):
        return
    raise NetworkBlocked(f"tests may not connect to {family!r} {address!r}")


def _audit_socket(event: str, args: tuple[Any, ...]) -> None:
    if event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
        sock, address = args
        if address is not None:
            check_address(sock.family, address)
    elif event in {
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
    }:
        if not _is_local_host(args[0]):
            raise NetworkBlocked("tests may only resolve local hosts")
    elif event == "socket.getnameinfo" and not _is_local_host(args[0][0]):
        raise NetworkBlocked("tests may only resolve local hosts")


def _before_address_resolution(
    operation: Callable[..., Any], address_index: int, *, optional: bool = False
) -> Callable[..., Any]:
    @wraps(operation)
    def guarded(sock: socket.socket, *args: Any) -> Any:
        if args and (not optional or len(args) > address_index):
            address = args[address_index]
            if address is not None:
                check_address(sock.family, address)
        return operation(sock, *args)

    return guarded


def install() -> None:
    global _installed
    if _installed:
        return
    # CPython may resolve hostnames before it emits a socket audit event.
    for method, index in (
        ("bind", 0),
        ("connect", 0),
        ("connect_ex", 0),
        ("sendto", -1),
    ):
        operation = getattr(socket.socket, method)
        setattr(socket.socket, method, _before_address_resolution(operation, index))
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = _before_address_resolution(
            socket.socket.sendmsg, 3, optional=True
        )
    sys.addaudithook(_audit_socket)
    _installed = True


def installed() -> bool:
    return _installed
