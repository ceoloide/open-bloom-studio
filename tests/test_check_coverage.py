import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401 - installs the test guards
from scripts import check_coverage


def summary(lines, statements, branches, num_branches):
    return {
        "covered_lines": lines,
        "num_statements": statements,
        "covered_branches": branches,
        "num_branches": num_branches,
    }


def report(**files):
    return {name: {"summary": data} for name, data in files.items()}


class CoverageGateTest(unittest.TestCase):
    def test_line_and_branch_are_gated_separately(self):
        failures = check_coverage.failures
        self.assertEqual(failures({"p": summary(90, 100, 9, 10)}, 90), [])
        self.assertEqual(
            failures({"p": summary(100, 100, 8, 10)}, 90),
            ["p: branch coverage 80.00% is below 90.00%"],
        )
        self.assertEqual(
            failures({"p": summary(89, 100, 10, 10)}, 90),
            ["p: line coverage 89.00% is below 90.00%"],
        )

    def test_nothing_measured_passes(self):
        self.assertEqual(check_coverage.failures({"p": summary(0, 0, 0, 0)}, 90), [])

    def test_each_source_root_is_gated_alone(self):
        groups = check_coverage.group_totals(
            report(
                **{
                    "pkg/a.py": summary(1, 4, 0, 2),
                    "pkg/sub/b.py": summary(0, 0, 0, 0),
                    "tools/c.py": summary(1000, 1000, 100, 100),
                }
            )
        )
        self.assertEqual(groups["pkg"], summary(1, 4, 0, 2))
        self.assertEqual(
            check_coverage.failures(groups, 90),
            [
                "pkg: line coverage 25.00% is below 90.00%",
                "pkg: branch coverage 0.00% is below 90.00%",
            ],
        )

    def test_main_reads_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            for data, expected in (
                (summary(9, 10, 1, 1), 0),
                (summary(1, 2, 0, 0), 1),
            ):
                path.write_text(
                    json.dumps(
                        {
                            "files": report(
                                **{"xbloom_studio/a.py": data, "scripts/b.py": data}
                            )
                        }
                    )
                )
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(check_coverage.main([str(path)]), expected)
                self.assertIn("xbloom_studio: line", out.getvalue())
                self.assertEqual(bool(err.getvalue()), expected == 1)

    def test_main_rejects_missing_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            path.write_text(
                json.dumps({"files": report(**{"scripts/a.py": summary(1, 1, 0, 0)})})
            )
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(check_coverage.main([str(path)]), 1)
            self.assertIn("missing coverage data for xbloom_studio", err.getvalue())


if __name__ == "__main__":
    unittest.main()
