# Contributing

## Constraints

- **Zero dependencies** — stdlib only. No pip installs, no node_modules.
- **Single-file** — `statusline.py` and `monitor.py` must remain self-contained.
- **Cross-platform** — changes must work on Windows, macOS, and Linux.

## Before submitting

1. Run the test suite — all tests must pass:
   ```bash
   python tests.py
   ```

2. Verify both files compile cleanly:
   ```bash
   python -c "import py_compile; py_compile.compile('statusline.py', doraise=True); py_compile.compile('monitor.py', doraise=True)"
   ```

3. Test manually on at least one platform with a live Claude Code session.

## What to keep in sync

- `MAX_FILE_SIZE` is defined in both `statusline.py` and `monitor.py` — update both if you change it.
- ANSI color palette (`C_RED`, `C_GRN`, etc.) is duplicated — keep both files consistent.

## Pull requests

- One logical change per PR.
- Include a description of what changed and why.
- Reference any related issues.
