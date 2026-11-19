"""Generic publication policy for repository files and release artifacts.

Findings name the file, line and rule but never echo the matched text, so a
failing check does not republish the value it found.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 512 * 1024

FORBIDDEN_NAMES = frozenset(
    {".env", ".netrc", ".pypirc", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}
)
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".btsnoop",
        ".cfa",
        ".db",
        ".key",
        ".log",
        ".p12",
        ".pcap",
        ".pcapng",
        ".pem",
        ".pfx",
        ".sqlite",
        ".sqlite3",
    }
)

LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    (
        "GitHub token",
        re.compile(r"\b(?:gh[opsur]_[A-Za-z0-9]{36,}|github_pat_\w{40,})"),
    ),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("PyPI token", re.compile(r"\bpypi-AgEI[A-Za-z0-9_-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    (
        "home directory path",
        re.compile(r"(?:/home/|/Users/|\b[A-Za-z]:(?:\\+|/)Users(?:\\+|/))\w"),
    ),
)

EMAIL = re.compile(r"\b[\w.%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.net", "example.org", "users.noreply.github.com"}
)
RESERVED_EMAIL_TLDS = frozenset({"example", "invalid", "test"})

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)

MAC = re.compile(
    r"(?<![\w:-])(?:[0-9A-Fa-f]{2}([:-]))(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![\w:-])"
)
DOCUMENTATION_MAC_PREFIX = "00005e0053"  # RFC 7042 documentation range


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def _email_allowed(domain: str) -> bool:
    domain = domain.lower()
    return (
        domain in ALLOWED_EMAIL_DOMAINS
        or domain.rsplit(".", 1)[-1] in RESERVED_EMAIL_TLDS
    )


def _ipv4_allowed(text: str) -> bool:
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return True  # not an address, e.g. an out-of-range dotted number
    return (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address == ipaddress.IPv4Address("255.255.255.255")
        or any(address in net for net in DOCUMENTATION_NETWORKS)
    )


def _mac_allowed(text: str) -> bool:
    digits = re.sub(r"[:-]", "", text).lower()
    return digits.startswith(DOCUMENTATION_MAC_PREFIX) or digits in {
        "000000000000",
        "ffffffffffff",
    }


def check_line(text: str) -> list[str]:
    """Return the rule names violated by one line of text."""
    rules = [name for name, pattern in LINE_RULES if pattern.search(text)]
    if any(not _email_allowed(m.group(1)) for m in EMAIL.finditer(text)):
        rules.append("email address")
    if any(not _ipv4_allowed(m.group(0)) for m in IPV4.finditer(text)):
        rules.append("IPv4 address")
    if any(not _mac_allowed(m.group(0)) for m in MAC.finditer(text)):
        rules.append("hardware address")
    return rules


def check_file(path: str, data: bytes) -> list[Finding]:
    """Check one file's name and content against the policy."""
    pure = PurePosixPath(path)
    basename = pure.name.lower()
    if (
        basename in FORBIDDEN_NAMES
        or basename.startswith(".env.")
        or pure.suffix.lower() in FORBIDDEN_SUFFIXES | {".env"}
    ):
        return [Finding(path, 0, "forbidden file type")]
    if len(data) > MAX_FILE_BYTES:
        return [Finding(path, 0, "file too large")]
    if b"\0" in data:
        return [Finding(path, 0, "binary content")]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding(path, 0, "not UTF-8 text")]
    return [
        Finding(path, number, rule)
        for number, line in enumerate(text.splitlines(), start=1)
        for rule in check_line(line)
    ]


def check_files(items: Iterable[tuple[str, bytes]]) -> list[Finding]:
    return [finding for path, data in items for finding in check_file(path, data)]


def git_environment() -> dict[str, str]:
    """Keep Git's working directory independent of a calling hook."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    return {**env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def repository_files(
    root: Path, *, tracked_only: bool = False
) -> list[tuple[str, bytes | None]]:
    """List repository files; symlinks map to ``None``."""
    command = ["git", "ls-files", "-z", "--cached"]
    if not tracked_only:
        command.extend(("--others", "--exclude-standard"))
    listed = subprocess.run(
        command,
        cwd=root,
        env=git_environment(),
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    files: list[tuple[str, bytes | None]] = []
    for name in sorted(set(filter(None, listed.split("\0")))):
        path = root / name
        if path.is_symlink():
            files.append((name, None))
        elif path.is_file():
            files.append((name, path.read_bytes()))
    return files


def check_repository(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for name, data in repository_files(root):
        if data is None:
            findings.append(Finding(name, 0, "symbolic link"))
        else:
            findings.extend(check_file(name, data))
    return findings


def report(findings: Sequence[Finding], subject: str) -> int:
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"{subject}: {len(findings)} publication finding(s)", file=sys.stderr)
        return 1
    print(f"{subject}: publication policy passed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()
    return report(check_repository(root), "repository")


if __name__ == "__main__":
    raise SystemExit(main())
