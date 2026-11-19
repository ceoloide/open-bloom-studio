import contextlib
import io
import json
import os
import site
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from scripts import check_dist

VERSION = "1.2.3"
STEM = f"open_bloom_studio-{VERSION}"
PACKAGE_FILES = {
    "xbloom_studio/__init__.py": b'"""Doc."""\n',
    "xbloom_studio/py.typed": b"",
}
ROOT_FILES = {
    "LICENSE": b"MIT\n",
    "MANIFEST.in": b"",
    "README.md": b"# Readme\n",
    "pyproject.toml": b"",
}
SOURCES = {**PACKAGE_FILES, **ROOT_FILES}
METADATA = (
    "Metadata-Version: 2.4\n"
    "Name: open-bloom-studio\n"
    f"Version: {VERSION}\n"
    "License-Expression: MIT\n"
    "License-File: LICENSE\n"
    "Requires-Python: >=3.10\n"
)


def expected():
    return check_dist.expected_metadata(check_dist.parse_metadata(METADATA.encode()))


def wheel_files(**changes):
    info = f"{STEM}.dist-info"
    files = {
        **PACKAGE_FILES,
        f"{info}/METADATA": METADATA.encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\n",
        f"{info}/RECORD": b"",
        f"{info}/top_level.txt": b"xbloom_studio\n",
        f"{info}/licenses/LICENSE": b"MIT\n",
    }
    files.update(changes)
    return {name: data for name, data in files.items() if data is not None}


def sdist_files(**changes):
    egg = "open_bloom_studio.egg-info"
    files = {
        **SOURCES,
        "PKG-INFO": METADATA.encode(),
        "setup.cfg": b"",
        f"{egg}/PKG-INFO": METADATA.encode(),
        f"{egg}/SOURCES.txt": b"",
        f"{egg}/dependency_links.txt": b"\n",
        f"{egg}/top_level.txt": b"xbloom_studio\n",
    }
    files.update(changes)
    return {name: data for name, data in files.items() if data is not None}


def write_wheel(directory, files, name=f"{STEM}-py3-none-any.whl"):
    path = Path(directory) / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xbloom_studio/", b"")
        for member, data in files.items():
            archive.writestr(member, data)
    return path


def write_sdist(directory, files, name=f"{STEM}.tar.gz", prefix=STEM, link=None):
    path = Path(directory) / name
    with tarfile.open(path, "w:gz") as archive:
        folder = tarfile.TarInfo(prefix)
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
        for member, data in files.items():
            info = tarfile.TarInfo(f"{prefix}/{member}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(f"{prefix}/{link}")
            info.type = tarfile.SYMTYPE
            info.linkname = "README.md"
            archive.addfile(info)
    return path


class ArtifactTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def check(self, wheel=None, sdist=None, **names):
        wheel_path = write_wheel(self.dir, wheel or wheel_files(), **names)
        sdist_path = write_sdist(self.dir, sdist or sdist_files())
        return check_dist.check_artifacts(sdist_path, wheel_path, SOURCES, expected())

    def test_matching_artifacts_pass(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(
            check_dist.find_artifacts(self.dir),
            (self.dir / f"{STEM}.tar.gz", self.dir / f"{STEM}-py3-none-any.whl"),
        )

    def test_artifact_count_and_names(self):
        with self.assertRaisesRegex(check_dist.DistError, "found 0 and 0"):
            check_dist.find_artifacts(self.dir)
        self.assertIn(
            "unexpected wheel name: x-1.0-py3-none-linux.whl",
            self.check(name="x-1.0-py3-none-linux.whl"),
        )
        sdist = write_sdist(self.dir, sdist_files(), name="x.tar.gz")
        wheel = write_wheel(self.dir, wheel_files())
        problems = check_dist.check_artifacts(sdist, wheel, SOURCES, expected())
        self.assertEqual(problems, ["unexpected sdist name: x.tar.gz"])

    def test_unexpected_and_missing_files(self):
        problems = self.check(
            wheel=wheel_files(**{"xbloom_studio/extra.json": b"{}"}),
            sdist=sdist_files(**{"xbloom_studio/py.typed": None}),
        )
        self.assertEqual(
            problems,
            [
                "wheel unexpected file: xbloom_studio/extra.json",
                "sdist missing file: xbloom_studio/py.typed",
            ],
        )

    def test_copied_files_must_match_source(self):
        license_file = f"{STEM}.dist-info/licenses/LICENSE"
        problems = self.check(
            wheel=wheel_files(**{license_file: b"other\n"}),
            sdist=sdist_files(**{"pyproject.toml": b"[project]\n"}),
        )
        self.assertEqual(
            problems,
            [
                "sdist file differs from source: pyproject.toml",
                f"wheel file differs from source: {license_file}",
            ],
        )

    def test_package_content_must_match_source(self):
        problems = self.check(wheel=wheel_files(**{"xbloom_studio/py.typed": b"x"}))
        self.assertEqual(
            problems, ["wheel file differs from source: xbloom_studio/py.typed"]
        )

    def test_metadata_must_match_source(self):
        changed = METADATA.replace("License-Expression: MIT", "License-Expression: X")
        problems = self.check(sdist=sdist_files(**{"PKG-INFO": changed.encode()}))
        self.assertEqual(
            problems, ["sdist metadata differs from source: License-Expression"]
        )
        missing = wheel_files(**{f"{STEM}.dist-info/METADATA": None})
        self.assertEqual(
            self.check(wheel=missing),
            [f"wheel missing file: {STEM}.dist-info/METADATA"],
        )

    def test_artifact_content_passes_publication_policy(self):
        secret = ("-----BEGIN " + "RSA PRIVATE KEY-----").encode()
        problems = self.check(sdist=sdist_files(**{"setup.cfg": secret}))
        self.assertEqual(problems, ["sdist:setup.cfg:1: private key"])

    def test_publication_checks_root_archive_names(self):
        problems = self.check(sdist=sdist_files(**{".env.local": b"example"}))
        self.assertIn("sdist:.env.local:0: forbidden file type", problems)

    def test_sdist_layout_is_strict(self):
        for kwargs in ({"prefix": "other"}, {"link": "alias"}):
            path = write_sdist(self.dir, sdist_files(), **kwargs)
            with self.subTest(**kwargs):
                with self.assertRaisesRegex(check_dist.DistError, "unexpected sdist"):
                    check_dist.read_sdist(path, STEM)

    def test_duplicate_archive_members_are_rejected(self):
        wheel = write_wheel(self.dir, wheel_files())
        with warnings.catch_warnings(), zipfile.ZipFile(wheel, "a") as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("xbloom_studio/py.typed", b"other")
        with self.assertRaisesRegex(check_dist.DistError, "duplicate wheel member"):
            check_dist.read_wheel(wheel)

        sdist = self.dir / "duplicate.tar.gz"
        with tarfile.open(sdist, "w:gz") as archive:
            for _ in range(2):
                archive.addfile(tarfile.TarInfo(f"{STEM}/README.md"))
        with self.assertRaisesRegex(check_dist.DistError, "duplicate sdist member"):
            check_dist.read_sdist(sdist, STEM)

    def test_source_metadata_policy(self):
        self.assertEqual(expected()["Version"], [VERSION])
        bad = "Name: other\nLicense-Expression: Apache-2.0\nRequires-Python: >=3.9\n"
        with self.assertRaises(check_dist.DistError) as caught:
            check_dist.expected_metadata(check_dist.parse_metadata(bad.encode()))
        for text in ("project", "license", "Requires-Python", "version is missing"):
            self.assertIn(text, str(caught.exception))


class InstallTest(unittest.TestCase):
    def probe(self, workdir, **changes):
        venv = (workdir / "venv").resolve()
        result = {
            "module": str(venv / "lib/site-packages/xbloom_studio/__init__.py"),
            "prefix": str(venv),
            "version": VERSION,
            "files": [*PACKAGE_FILES, f"{STEM}.dist-info/RECORD"],
        }
        result.update(changes)
        commands = []

        def run(command):
            commands.append(command)
            return json.dumps(result) if command[1] == "-I" else ""

        problems = check_dist.check_install(
            Path("x.whl"), workdir, PACKAGE_FILES, VERSION, run
        )
        return problems, commands

    def test_install_is_isolated_and_complete(self):
        workdir = Path(tempfile.gettempdir())
        problems, commands = self.probe(workdir)
        self.assertEqual(problems, [])
        install = commands[1]
        self.assertIn("--no-index", install)
        self.assertIn("--no-deps", install)
        self.assertEqual(commands[2][1:3], ["-I", "-c"])

    def test_install_problems(self):
        workdir = Path(tempfile.gettempdir())
        problems, _ = self.probe(
            workdir,
            module=str(Path.cwd() / "xbloom_studio/__init__.py"),
            prefix=str(Path.cwd()),
            version="0",
            files=["xbloom_studio/__init__.py"],
        )
        self.assertEqual(
            problems,
            [
                "probe did not run in the new environment",
                "installed version is 0",
                "installed missing file: xbloom_studio/py.typed",
            ],
        )
        problems, _ = self.probe(workdir, module=str(Path.cwd() / "x.py"))
        self.assertEqual(
            problems, ["installed package was not imported from the new environment"]
        )

    def test_run_command_returns_output(self):
        code = "print('ok')"
        self.assertEqual(check_dist.run_command([sys.executable, "-c", code]), "ok\n")


class MainTest(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = check_dist.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_usage(self):
        self.assertEqual(self.run_main([])[0], 2)

    def test_source_metadata_comes_from_site_packages(self):
        dist = mock.Mock()
        dist.read_text.return_value = METADATA
        with mock.patch("importlib.metadata.distributions") as distributions:
            distributions.return_value = iter([dist])
            self.assertEqual(check_dist.source_metadata()["Version"], VERSION)
            self.assertEqual(
                distributions.call_args.kwargs["path"], site.getsitepackages()
            )
            distributions.return_value = iter([])
            with self.assertRaisesRegex(check_dist.DistError, "not installed"):
                check_dist.source_metadata()

    def test_source_files_rejects_links(self):
        listed = [("README.md", b""), ("docs/a.md", b""), ("xbloom_studio/a.py", b"x")]
        with mock.patch.object(check_dist.publication_policy, "repository_files") as rf:
            rf.return_value = listed
            self.assertEqual(
                check_dist.source_files(Path(".")),
                {"README.md": b"", "xbloom_studio/a.py": b"x"},
            )
            rf.assert_called_with(Path("."), tracked_only=True)
            rf.return_value = [("xbloom_studio/b.py", None)]
            with self.assertRaisesRegex(check_dist.DistError, "symbolic link"):
                check_dist.source_files(Path("."))

    def test_main_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            write_wheel(directory, wheel_files())
            write_sdist(directory, sdist_files())
            source = check_dist.parse_metadata(METADATA.encode())
            patches = (
                mock.patch.object(check_dist, "source_metadata", return_value=source),
                mock.patch.object(check_dist, "source_files", return_value=SOURCES),
                mock.patch.object(check_dist, "check_install", return_value=[]),
            )
            with patches[0], patches[1], patches[2] as install:
                status, out, _ = self.run_main([directory])
                self.assertEqual(status, 0)
                self.assertIn("distribution checks passed", out)
                relative = os.path.relpath(directory)
                self.assertEqual(self.run_main([relative])[0], 0)
                self.assertTrue(install.call_args.args[0].is_absolute())

                install.reset_mock()
                write_sdist(directory, sdist_files(**{"setup.cfg": b"\x00"}))
                self.assertEqual(self.run_main([directory])[0], 1)
                install.assert_not_called()
                write_sdist(directory, sdist_files())

                install.return_value = ["installed version is 0"]
                self.assertEqual(self.run_main([directory])[0], 1)

                error = subprocess.CalledProcessError(1, ["uv"], stderr="boom")
                install.side_effect = error
                status, _, err = self.run_main([directory])
                self.assertEqual(status, 1)
                self.assertIn("command failed: uv (exit 1)", err)

            status, _, err = self.run_main([str(Path(directory) / "missing")])
            self.assertEqual(status, 1)
            self.assertIn("expected one sdist", err)


if __name__ == "__main__":
    unittest.main()
