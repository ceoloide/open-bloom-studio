import os
import socket
import tempfile
import unittest
from unittest import mock

from tests import offline

DOCUMENTATION_HOST = "192.0.2.1"


class OfflineGuardTest(unittest.TestCase):
    def test_suite_runs_with_guard_installed(self):
        offline.install()  # idempotent
        self.assertTrue(offline.installed())
        self.assertTrue(hasattr(socket.socket.connect, "__wrapped__"))

    def test_remote_name_lookup_is_blocked(self):
        with self.assertRaises(offline.NetworkBlocked):
            socket.getaddrinfo("example.com", 443)
        with self.assertRaises(offline.NetworkBlocked):
            socket.create_connection((DOCUMENTATION_HOST, 9), timeout=1)

    def test_hostname_is_rejected_before_socket_operation(self):
        action = mock.Mock()
        for index, args in (
            (0, ("remote.invalid", 9)),
            (-1, (b"test", ("remote.invalid", 9))),
            (3, ([b"test"], [], 0, ("remote.invalid", 9))),
        ):
            destination = (args,) if index == 0 else args
            with socket.socket() as client:
                check = offline._before_address_resolution(action, index)
                with self.assertRaises(offline.NetworkBlocked):
                    check(client, *destination)
        action.assert_not_called()

    def test_remote_datagram_is_blocked(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            with self.assertRaises(offline.NetworkBlocked):
                client.sendto(b"test", (DOCUMENTATION_HOST, 9))

    def test_other_remote_dns_entry_points_are_blocked(self):
        for lookup, address in (
            (socket.gethostbyname, "example.com"),
            (socket.gethostbyname_ex, "example.com"),
            (socket.gethostbyaddr, DOCUMENTATION_HOST),
            (socket.getnameinfo, (DOCUMENTATION_HOST, 9)),
        ):
            with self.subTest(lookup=lookup.__name__):
                with self.assertRaises(offline.NetworkBlocked):
                    if lookup is socket.getnameinfo:
                        lookup(address, 0)
                    else:
                        lookup(address)

    def test_remote_connect_is_blocked(self):
        for family, address in (
            (socket.AF_INET, (DOCUMENTATION_HOST, 9)),
            (socket.AF_INET6, ("2001:db8::1", 9, 0, 0)),
        ):
            with socket.socket(family, socket.SOCK_STREAM) as client:
                with self.assertRaises(offline.NetworkBlocked):
                    client.connect(address)
                with self.assertRaises(offline.NetworkBlocked):
                    client.connect_ex(address)

    def test_other_address_families_are_blocked(self):
        with self.assertRaises(offline.NetworkBlocked):
            offline.check_address(-1, ("00:00:5E:00:53:01", 1))

    def test_loopback_is_allowed(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            for host in ("127.0.0.1", "localhost"):
                port = server.getsockname()[1]
                with socket.create_connection((host, port), timeout=5):
                    server.accept()[0].close()
            with socket.socket() as client:
                self.assertEqual(client.connect_ex(server.getsockname()), 0)
                server.accept()[0].close()
        self.assertTrue(offline._is_local_host(b"localhost"))
        self.assertTrue(offline._is_local_host("::1"))
        self.assertFalse(offline._is_local_host("fe80::1%lo"))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets unavailable")
    def test_abstract_unix_address_is_allowed_except_system_bus(self):
        env = {"DBUS_SYSTEM_BUS_ADDRESS": "unix:abstract=system%2dbus"}
        with mock.patch.dict(os.environ, env):
            offline.check_address(socket.AF_UNIX, b"\0test-socket")
            with self.assertRaises(offline.NetworkBlocked):
                offline.check_address(socket.AF_UNIX, b"\0system-bus")

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets unavailable")
    def test_local_unix_socket_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "s")
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path)
                server.listen()
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(path)
                    server.accept()[0].close()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets unavailable")
    def test_system_bus_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            custom = os.path.join(directory, "bus")
            env = {"DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={custom}%2dname,guid=0"}
            custom += "-name"
            with mock.patch.dict(os.environ, env):
                for path in (
                    *offline.SYSTEM_BUS_PATHS,
                    custom,
                    custom.encode(),
                    memoryview(custom.encode()),
                    bytearray(custom.encode()),
                    custom.encode() + b"\0padding",
                ):
                    with socket.socket(socket.AF_UNIX) as client:
                        with self.assertRaises(offline.NetworkBlocked):
                            client.connect(path)


if __name__ == "__main__":
    unittest.main()
