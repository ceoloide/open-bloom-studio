"""Test roots: layout, variables and the dropped device default."""

from __future__ import annotations

import shutil
import stat
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401 - installs the test guards
from scripts import testroot


class TestRootTests(unittest.TestCase):
    def setUp(self):
        self.root = testroot.create(prefix="xbs-unit-")
        self.addCleanup(shutil.rmtree, self.root)

    def test_create_makes_an_empty_private_directory(self):
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()))
        self.assertTrue(self.root.name.startswith("xbs-unit-"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        default = testroot.create()
        self.addCleanup(shutil.rmtree, default)
        self.assertTrue(default.name.startswith(testroot.PREFIX))

    def test_environment_makes_and_names_the_layout(self):
        variables = testroot.environment(self.root)
        self.assertEqual(
            variables,
            {
                testroot.ROOT: str(self.root),
                "HOME": str(self.root / "home"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_RUNTIME_DIR": str(self.root / "run"),
            },
        )
        for child in ("home", "data", "run"):
            self.assertTrue((self.root / child).is_dir())
        self.assertEqual(testroot.environment(self.root), variables)

    def test_apply_replaces_the_variables_and_drops_the_device(self):
        environ = {
            "HOME": "/elsewhere",
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
