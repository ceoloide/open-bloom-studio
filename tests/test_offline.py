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

    def test_remote_name_lookup_is_blocked(self):
        with self.assertRaises(offline.NetworkBlocked):
            socket.getaddrinfo("example.com", 443)
        with self.assertRaises(offline.NetworkBlocked):
            socket.create_connection((DOCUMENTATION_HOST, 9), timeout=1)

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
            env = {"DBUS_SYSTEM_BUS_ADDRESS": f"tcp:host=x;unix:path={custom},guid=0"}
            with mock.patch.dict(os.environ, env):
                for path in (*offline.SYSTEM_BUS_PATHS, custom, custom.encode()):
                    with socket.socket(socket.AF_UNIX) as client:
                        with self.assertRaises(offline.NetworkBlocked):
                            client.connect(path)


if __name__ == "__main__":
    unittest.main()
