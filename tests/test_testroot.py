"""Test roots: base directory, layout, variables and the dropped device default."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401 - installs the test guards
from scripts import testroot

MIB = 1024 * 1024


def statvfs(*, flag=0, free=testroot.MIN_FREE, missing=()):
    """A fake ``os.statvfs``: ``free`` bytes in 4 KiB blocks, given mount flags."""

    def probe(path):
        if path in missing:
            raise FileNotFoundError(path)
        return mock.Mock(f_flag=flag, f_bavail=free // 4096, f_frsize=4096)

    return probe


class BaseTests(unittest.TestCase):
    def test_usable_needs_exec_space_and_access(self):
        self.assertTrue(testroot.usable("/dev/shm", statvfs()))
        cases = {
            "no-exec": statvfs(flag=os.ST_NOEXEC),
            "under 64 MiB": statvfs(free=64 * MIB - 4096),
            "missing": statvfs(missing=("/dev/shm",)),
        }
        for name, probe in cases.items():
            with self.subTest(name):
                self.assertFalse(testroot.usable("/dev/shm", probe))
        with mock.patch.object(testroot.os, "access", return_value=False) as access:
            self.assertFalse(testroot.usable("/dev/shm", statvfs()))
        access.assert_called_once_with("/dev/shm", os.W_OK | os.X_OK)

    def test_base_takes_the_first_usable_candidate(self):
        self.assertEqual(testroot.base({}, statvfs()), "/dev/shm")
        shm_missing = statvfs(missing=("/dev/shm",))
        self.assertEqual(testroot.base({}, shm_missing), "/tmp")
        self.assertIsNone(testroot.base({}, statvfs(flag=os.ST_NOEXEC)))

    def test_set_base_variable_wins_and_must_be_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            environ = {testroot.TMPBASE: directory}
            rejected = statvfs(missing=(directory,))
            self.assertEqual(testroot.base(environ, rejected), directory)
            with self.assertRaisesRegex(RuntimeError, testroot.TMPBASE):
                testroot.base({testroot.TMPBASE: f"{directory}/missing"})
        self.assertEqual(testroot.base({testroot.TMPBASE: ""}, statvfs()), "/dev/shm")


class TestRootTests(unittest.TestCase):
    def setUp(self):
        self.root = testroot.create(os.environ, prefix="xbs-unit-")
        self.addCleanup(shutil.rmtree, self.root)

    def test_create_makes_an_empty_private_directory(self):
        expected = testroot.base(os.environ) or tempfile.gettempdir()
        self.assertEqual(self.root.parent, Path(expected))
        self.assertTrue(self.root.name.startswith("xbs-unit-"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        default = testroot.create(os.environ)
        self.addCleanup(shutil.rmtree, default)
        self.assertTrue(default.name.startswith(testroot.PREFIX))

    def test_create_uses_the_base_variable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = testroot.create({testroot.TMPBASE: directory})
            self.assertEqual(root.parent, Path(directory))

    def test_environment_makes_and_names_the_layout(self):
        variables = testroot.environment(self.root)
        self.assertEqual(
            variables,
            {
                testroot.ROOT: str(self.root),
                "HOME": str(self.root / "home"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_RUNTIME_DIR": str(self.root / "run"),
                "TMPDIR": str(self.root / "tmp"),
            },
        )
        for child in ("home", "data", "run", "tmp"):
            self.assertTrue((self.root / child).is_dir())
        self.assertEqual(testroot.environment(self.root), variables)

    def test_apply_replaces_the_variables_and_drops_the_device(self):
        environ = {
            "HOME": "/elsewhere",
            "TMPDIR": "/elsewhere/tmp",
            testroot.ROOT: "/stale",
            testroot.DEVICE: "00:00:5E:00:53:99",
            "PATH": "/bin",
        }
        testroot.apply(environ, self.root)
        self.assertEqual(environ, {"PATH": "/bin", **testroot.environment(self.root)})
        testroot.apply(environ, self.root)
        self.assertNotIn(testroot.DEVICE, environ)


if __name__ == "__main__":
    unittest.main()
