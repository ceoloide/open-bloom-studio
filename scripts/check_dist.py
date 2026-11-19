"""Check built sdist and wheel contents, metadata and an isolated install.

Usage: python -m scripts.check_dist DIST_DIR

DIST_DIR must hold exactly one sdist and one wheel of this project. The
distributed package files must match the tracked files under PACKAGE exactly,
so additions to the package extend the check without editing this script.
"""

from __future__ import annotations

import email.message
import email.parser
import importlib.metadata
import json
import site
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from scripts import publication_policy

PROJECT = "open-bloom-studio"
PACKAGE = "xbloom_studio"
LICENSE_EXPRESSION = "MIT"
REQUIRES_PYTHON = ">=3.10"
WHEEL_TAG = "py3-none-any"
COMPARED_FIELDS = (
    "Name",
    "Version",
    "Requires-Python",
    "License-Expression",
    "License-File",
    "Requires-Dist",
    "Provides-Extra",
)
WHEEL_METADATA_FILES = (
    "METADATA",
    "WHEEL",
    "RECORD",
    "top_level.txt",
    "licenses/LICENSE",
)
SDIST_ROOT_FILES = (
    "PKG-INFO",
    "README.md",
    "LICENSE",
    "MANIFEST.in",
    "pyproject.toml",
    "setup.cfg",
)
COPIED_ROOT_FILES = ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml")
SDIST_EGG_INFO_FILES = (
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "top_level.txt",
)

Runner = Callable[[Sequence[str]], str]

PROBE = """
import importlib.metadata, json, sys
import {package}
dist = importlib.metadata.distribution({project!r})
print(json.dumps({{
    "module": {package}.__file__,
    "prefix": sys.prefix,
    "version": dist.version,
    "files": sorted(str(f) for f in dist.files or ()),
}}))
"""


class DistError(Exception):
    pass


def distribution_stem(version: str) -> str:
    return f"{PROJECT.replace('-', '_')}-{version}"


def find_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    wheels = sorted(dist_dir.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise DistError(
            f"expected one sdist and one wheel, found {len(sdists)} and {len(wheels)}"
        )
    return sdists[0], wheels[0]


def parse_metadata(data: bytes) -> email.message.Message:
    return email.parser.BytesParser().parsebytes(data)


def metadata_fields(message: email.message.Message) -> dict[str, list[str]]:
    return {
        field: sorted(str(value) for value in message.get_all(field, []))
        for field in COMPARED_FIELDS
    }


def read_wheel(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.filename in files:
                raise DistError("duplicate wheel member")
            files[info.filename] = archive.read(info)
    return files


def read_sdist(path: Path, stem: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            prefix, _, name = member.name.partition("/")
            extracted = archive.extractfile(member) if member.isfile() else None
            if prefix != stem or not name or extracted is None:
                raise DistError(f"unexpected sdist member: {member.name}")
            if name in files:
                raise DistError("duplicate sdist member")
            files[name] = extracted.read()
    return files


def compare_names(kind: str, actual: set[str], expected: set[str]) -> list[str]:
    return [f"{kind} unexpected file: {name}" for name in sorted(actual - expected)] + [
        f"{kind} missing file: {name}" for name in sorted(expected - actual)
    ]


def check_artifacts(
    sdist: Path,
    wheel: Path,
    sources: Mapping[str, bytes],
    expected: Mapping[str, list[str]],
) -> list[str]:
    """Return problems with artifact names, contents and metadata.

    ``sources`` maps tracked package files and COPIED_ROOT_FILES to content.
    """
    package_files = package_only(sources)
    version = expected["Version"][0]
    stem = distribution_stem(version)
    problems = []
    if sdist.name != f"{stem}.tar.gz":
        problems.append(f"unexpected sdist name: {sdist.name}")
    if wheel.name != f"{stem}-{WHEEL_TAG}.whl":
        problems.append(f"unexpected wheel name: {wheel.name}")

    wheel_files = read_wheel(wheel)
    sdist_files = read_sdist(sdist, stem)
    dist_info = f"{stem}.dist-info"
    egg_info = f"{PROJECT.replace('-', '_')}.egg-info"
    problems += compare_names(
        "wheel",
        set(wheel_files),
        set(package_files) | {f"{dist_info}/{name}" for name in WHEEL_METADATA_FILES},
    )
    problems += compare_names(
        "sdist",
        set(sdist_files),
        set(package_files)
        | set(SDIST_ROOT_FILES)
        | {f"{egg_info}/{name}" for name in SDIST_EGG_INFO_FILES},
    )
    copies = [("sdist", sdist_files, name, name) for name in sorted(sources)]
    copies += [("wheel", wheel_files, name, name) for name in sorted(package_files)]
    copies.append(("wheel", wheel_files, f"{dist_info}/licenses/LICENSE", "LICENSE"))
    for kind, files, member, source in copies:
        if member in files and files[member] != sources.get(source):
            problems.append(f"{kind} file differs from source: {member}")

    for kind, raw in (
        ("wheel", wheel_files.get(f"{dist_info}/METADATA")),
        ("sdist", sdist_files.get("PKG-INFO")),
    ):
        if raw is None:
            continue
        fields = metadata_fields(parse_metadata(raw))
        for field, value in expected.items():
            if fields[field] != value:
                problems.append(f"{kind} metadata differs from source: {field}")

    findings = publication_policy.check_files(
        [(f"wheel:{name}", data) for name, data in sorted(wheel_files.items())]
        + [(f"sdist:{name}", data) for name, data in sorted(sdist_files.items())]
    )
    problems += [str(finding) for finding in findings]
    return problems


def expected_metadata(source: email.message.Message) -> dict[str, list[str]]:
    """Expected artifact metadata: the source checkout's, plus fixed policy."""
    expected = metadata_fields(source)
    problems = []
    if expected["Name"] != [PROJECT]:
        problems.append("source project name differs from policy")
    if expected["License-Expression"] != [LICENSE_EXPRESSION]:
        problems.append("source license differs from policy")
    if expected["Requires-Python"] != [REQUIRES_PYTHON]:
        problems.append("source Requires-Python differs from policy")
    if len(expected["Version"]) != 1:
        problems.append("source version is missing")
    if problems:
        raise DistError("; ".join(problems))
    return expected


def source_metadata() -> email.message.Message:
    """Metadata of the project as synced into this environment by uv.

    Only site-packages is searched: build tools leave stale metadata in the
    working tree, which is first on ``sys.path`` under ``python -m``.
    """
    for dist in importlib.metadata.distributions(
        name=PROJECT, path=site.getsitepackages()
    ):
        return parse_metadata((dist.read_text("METADATA") or "").encode())
    raise DistError(f"{PROJECT} is not installed in this environment")


def run_command(command: Sequence[str]) -> str:
    return subprocess.run(
        command, check=True, capture_output=True, text=True, cwd=tempfile.gettempdir()
    ).stdout


def check_install(
    wheel: Path,
    workdir: Path,
    package_files: Mapping[str, bytes],
    version: str,
    run: Runner = run_command,
) -> list[str]:
    """Install the wheel alone into a fresh environment and probe it."""
    venv = workdir / "venv"
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    run(["uv", "venv", "--quiet", "--python", sys.executable, str(venv)])
    run(
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(python),
            "--no-index",
            "--no-deps",
            str(wheel),
        ]
    )
    probe = PROBE.format(package=PACKAGE, project=PROJECT)
    result = json.loads(run([str(python), "-I", "-c", probe]))
    problems = []
    prefix = Path(result["prefix"]).resolve()
    if not Path(result["module"]).resolve().is_relative_to(prefix):
        problems.append("installed package was not imported from the new environment")
    if prefix != venv.resolve():
        problems.append("probe did not run in the new environment")
    if result["version"] != version:
        problems.append(f"installed version is {result['version']}")
    installed = {name for name in result["files"] if name.startswith(f"{PACKAGE}/")}
    problems += compare_names("installed", installed, set(package_files))
    return problems


def package_only(sources: Mapping[str, bytes]) -> dict[str, bytes]:
    return {n: d for n, d in sources.items() if n.startswith(f"{PACKAGE}/")}


def source_files(root: Path) -> dict[str, bytes]:
    """Tracked files that artifacts must reproduce byte for byte."""
    files = {}
    for name, data in publication_policy.repository_files(root, tracked_only=True):
        if name.startswith(f"{PACKAGE}/") or name in COPIED_ROOT_FILES:
            if data is None:
                raise DistError(f"distributed file is a symbolic link: {name}")
            files[name] = data
    return files


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        sdist, wheel = find_artifacts(Path(args[0]).resolve())
        expected = expected_metadata(source_metadata())
        sources = source_files(Path.cwd())
        problems = check_artifacts(sdist, wheel, sources, expected)
        if not problems:
            with tempfile.TemporaryDirectory() as workdir:
                problems = check_install(
                    wheel, Path(workdir), package_only(sources), expected["Version"][0]
                )
    except DistError as error:
        problems = [str(error)]
    except subprocess.CalledProcessError as error:
        problems = [f"command failed: {error.cmd[0]} (exit {error.returncode})"]
        print(error.stderr or "", file=sys.stderr)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"distribution checks passed: {sdist.name}, {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
