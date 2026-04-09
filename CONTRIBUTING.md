# Contributing

## Constraints

- **Zero dependencies** — stdlib only. No pip installs, no node_modules.
- **Three entry files** — `statusline.py`, `monitor.py`, and shared `rates.py`. No additional modules.
- **Cross-platform** — changes must work on Windows, macOS, and Linux.

## Before submitting

1. Run the test suite — all tests must pass:
   ```bash
   python tests.py
   ```

2. Verify all files compile cleanly:
   ```bash
   python -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('rates.py', 'statusline.py', 'monitor.py')]"
   ```

3. Test manually on at least one platform with a live Claude Code session.

## What to keep in sync

- `MAX_FILE_SIZE` is defined in both `statusline.py` and `monitor.py` — update both if you change it.
- `calc_rates` lives in `rates.py` — imported by both `statusline.py` and `monitor.py`.
- ANSI color palette (`C_RED`, `C_GRN`, etc.) is duplicated — keep both files consistent.

## Pull requests

- One logical change per PR.
- Include a description of what changed and why.
- Reference any related issues.
