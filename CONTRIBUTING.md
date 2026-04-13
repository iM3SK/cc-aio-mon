# Contributing

## Constraints

- **Stdlib only** — no pip installs, no node_modules.
- **Four runtime files** — `statusline.py`, `monitor.py`, `shared.py`, and `update.py`. No additional runtime modules (test files like `tests.py` are not runtime).
- **Cross-platform** — changes must work on Windows, macOS, and Linux.

## Before submitting

> On macOS/Linux use `python3`. On Windows use `py`.

1. Run the test suite — all tests must pass:
   ```bash
   python3 tests.py
   ```

2. Verify all files compile cleanly:
   ```bash
   python3 -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('shared.py', 'statusline.py', 'monitor.py', 'update.py')]"
   ```

3. Test manually on at least one platform with a live Claude Code session.

## What to keep in sync

- `shared.py` is the single source of truth for shared constants (`_SID_RE`, `_ANSI_RE`, `MAX_FILE_SIZE`, `DATA_DIR_NAME`), ANSI colors (`C_RED`, `C_GRN`, etc.), helpers (`_num`, `_sanitize`, `f_tok`, `f_cost`, `f_dur`), and `calc_rates`. Both `statusline.py` and `monitor.py` import from it.

## Pull requests

For anything non-trivial — new features, behavior changes, refactors beyond local cleanup — **open an issue first** so the approach can be discussed before work begins. Typo fixes, small doc edits, and obvious bug fixes can go directly to a PR.

- One logical change per PR.
- Include a description of what changed and why.
- Reference any related issues.
