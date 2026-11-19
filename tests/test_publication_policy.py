import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import publication_policy as policy

# Sensitive-looking samples are assembled at runtime so this file passes the
# policy it tests.
AT = "@"
KEY_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
SECRET_SAMPLES = {
    "private key": KEY_HEADER,
    "GitHub token": "gh" + "p_" + "a" * 36,
    "AWS access key": "AK" + "IA" + "A" * 16,
    "Slack token": "xo" + "xb-" + "1" * 12,
    "API key": "s" + "k-" + "x" * 24,
    "PyPI token": "py" + "pi-AgEI" + "x" * 30,
    "Google API key": "AI" + "za" + "x" * 35,
    "home directory path": "/" + "home/someone/project",
    "email address": "person" + AT + "mail.invalid-domain.net",
    "IPv4 address": "192." + "168.1.20",
    "hardware address": ":".join(["AA", "BB", "CC", "DD", "EE", "01"]),
}


def git(root, *args):
    subprocess.run(
        ["git", *args],
        cwd=root,
        env=policy.git_environment(),
        check=True,
        capture_output=True,
    )


class LineRulesTest(unittest.TestCase):
    def test_each_rule_detects_its_sample(self):
        for rule, sample in SECRET_SAMPLES.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, policy.check_line(f"value = {sample!r}"))

    def test_home_path_variants(self):
        for sample in ("/" + "Users/someone/x", "C:" + "\\Users\\someone"):
            self.assertEqual(policy.check_line(sample), ["home directory path"])

    def test_allowed_values(self):
        allowed = [
            "contact " + "someone" + AT + "example.com",
            "1234+name" + AT + "users.noreply.github.com",
            "host" + AT + "machine.test",
            "127.0.0.1 0.0.0.0 255.255.255.255 224.0.0.251",
            "192.0.2.10 198.51.100.7 203.0.113.99 version 1.2.3",
            "dotted 999.1.1.1",
            "00:00:5E:00:53:2A 00-00-5e-00-53-01 00:00:00:00:00:00",
            "frame 58:01:02:03:04:05:06:07 time 12:34:56",
            "mixed AA:BB-CC:DD:EE:FF",
            "/home/ without a user, and /homepage/x",
        ]
        for line in allowed:
            with self.subTest(line=line):
                self.assertEqual(policy.check_line(line), [])


class FileRulesTest(unittest.TestCase):
    def test_forbidden_names_and_types(self):
        for name in ("a/.env", "id_rsa", "trace.PCAPNG", "state.sqlite3"):
            with self.subTest(name=name):
                self.assertEqual(
                    [f.rule for f in policy.check_file(name, b"")],
                    ["forbidden file type"],
                )

    def test_content_shape(self):
        big = b"x" * (policy.MAX_FILE_BYTES + 1)
        self.assertEqual(policy.check_file("a", big)[0].rule, "file too large")
        self.assertEqual(policy.check_file("a", b"a\0b")[0].rule, "binary content")
        self.assertEqual(policy.check_file("a", b"\xff")[0].rule, "not UTF-8 text")

    def test_findings_name_location_without_value(self):
        data = f"ok\nkey = {SECRET_SAMPLES['API key']}\n".encode()
        findings = policy.check_files([("docs/a.md", data), ("b.txt", b"fine")])
        self.assertEqual(findings, [policy.Finding("docs/a.md", 2, "API key")])
        self.assertEqual(str(findings[0]), "docs/a.md:2: API key")


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        git(self.root, "init", "-q")
        (self.root / ".gitignore").write_text("ignored.txt\n")
        (self.root / "tracked.txt").write_text("fine\n")
        git(self.root, "add", ".")
        (self.root / "untracked.txt").write_text("also fine\n")
        (self.root / "ignored.txt").write_text(KEY_HEADER)

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = policy.main([str(self.root)])
        return status, out.getvalue(), err.getvalue()

    def test_scans_tracked_and_unignored_files(self):
        names = [name for name, _ in policy.repository_files(self.root)]
        self.assertEqual(names, [".gitignore", "tracked.txt", "untracked.txt"])
        self.assertEqual(self.run_main()[0], 0)

    def test_git_hook_environment_cannot_redirect_scan_or_fixture_setup(self):
        external = self.root / "external"
        env = {"GIT_DIR": str(external), "GIT_INDEX_FILE": str(external / "index")}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(
                [
                    name
                    for name, _ in policy.repository_files(self.root, tracked_only=True)
                ],
                [".gitignore", "tracked.txt"],
            )
            with tempfile.TemporaryDirectory() as sandbox:
                git(sandbox, "init", "-q")
                self.assertTrue((Path(sandbox) / ".git").exists())
        self.assertFalse(external.exists())

    def test_reports_findings_and_skips_deleted_files(self):
        (self.root / "untracked.txt").write_text(KEY_HEADER)
        (self.root / "tracked.txt").unlink()
        status, _, err = self.run_main()
        self.assertEqual(status, 1)
        self.assertIn("untracked.txt:1: private key", err)
        self.assertNotIn(KEY_HEADER, err)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinks_are_rejected(self):
        os.symlink("tracked.txt", self.root / "link")
        self.assertIn(
            policy.Finding("link", 0, "symbolic link"),
            policy.check_repository(self.root),
        )

    def test_main_defaults_to_current_directory(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(policy.main([]), 0)


if __name__ == "__main__":
    unittest.main()
