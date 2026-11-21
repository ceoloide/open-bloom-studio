"""Run a module as ``__main__`` with the test guards installed.

Usage: python -m tests.child MODULE ARGS...
"""

from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m tests.child MODULE [ARGS...]", file=sys.stderr)
        sys.exit(2)
    module = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
