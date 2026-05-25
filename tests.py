#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Unit tests for CC AIO MON — stdlib only, no pytest required.

Thin wrapper that discovers and runs all tests under the tests/ package, so
existing invocations (`py tests.py`) keep working after the per-module split.

Run:
    python tests.py
"""

import pathlib
import sys
import unittest


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent
    print("Running cc-aio-mon test suite via tests/ discovery...")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(root / "tests"), top_level_dir=str(root))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
