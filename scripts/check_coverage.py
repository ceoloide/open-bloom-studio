"""Enforce line and branch coverage minimums from a coverage JSON report.

Each top-level source root (the package, the repository tools) is gated on
its own, so well-covered tooling cannot hide untested package code.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

MINIMUM_PERCENT = 90.0
REQUIRED_ROOTS = {"scripts", "xbloom_studio"}
COUNTS = ("covered_lines", "num_statements", "covered_branches", "num_branches")


def percentages(totals: Mapping[str, int]) -> dict[str, float]:
    """Return line and branch percentages; nothing measured counts as covered."""
    result = {}
    for kind, covered, total in (
        ("line", "covered_lines", "num_statements"),
        ("branch", "covered_branches", "num_branches"),
    ):
        count = totals[total]
        result[kind] = 100.0 if count == 0 else 100.0 * totals[covered] / count
    return result


def group_totals(files: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = {}
    for name, data in files.items():
        group = groups.setdefault(PurePath(name).parts[0], dict.fromkeys(COUNTS, 0))
        for count in COUNTS:
            group[count] += data["summary"][count]
    return groups


def failures(groups: Mapping[str, Mapping[str, int]], minimum: float) -> list[str]:
    return [
        f"{group}: {kind} coverage {value:.2f}% is below {minimum:.2f}%"
        for group, totals in sorted(groups.items())
        for kind, value in percentages(totals).items()
        if value < minimum
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--minimum", type=float, default=MINIMUM_PERCENT)
    args = parser.parse_args(argv)
    groups = group_totals(json.loads(args.report.read_text("utf-8"))["files"])
    problems = [
        f"missing coverage data for {root}"
        for root in sorted(REQUIRED_ROOTS - groups.keys())
    ]
    problems += failures(groups, args.minimum)
    for problem in problems:
        print(problem, file=sys.stderr)
    for group, totals in sorted(groups.items()):
        values = percentages(totals)
        print(f"{group}: line {values['line']:.2f}%, branch {values['branch']:.2f}%")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
