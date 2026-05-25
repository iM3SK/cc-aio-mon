# Contributing

## Constraints

- **Stdlib only** — no pip installs, no node_modules.
- **Five runtime files** — `statusline.py`, `monitor.py`, `shared.py`, `update.py`, and `pulse.py`. No additional runtime modules (test files like `tests.py` are not runtime).
- **Cross-platform** — changes must work on Windows, macOS, and Linux.

## Code style

- **No `import` inside function bodies in production code** (`monitor.py`, `statusline.py`, `shared.py`, `update.py`, `pulse.py`). Import at module level only. Test methods in `tests.py` and `tests/test_*.py` may use per-test inline imports for dependency isolation. This rule is **load-bearing** — `tests/test_monitor.py::TestAuditRegressionV1105::test_debt014_*` enforce module-level binding for `signal`, `subprocess`, `bisect`, `traceback` to prevent the v1.10.3 Windows `UnboundLocalError` regression caused by function-local `import signal` shadowing the module-level name. Cold-start performance optimizations that propose moving these imports must first amend the DEBT-014 regression tests *and* document why the shadowing class of bug cannot recur — currently no such case has met that bar.
- **Guard platform-specific *attributes*** (e.g. `os.geteuid`, `signal.SIGPIPE`) with `hasattr(...)`, not function-local conditional imports. Module-top-level conditional imports for whole modules that exist only on one platform (e.g. `if sys.platform == "win32": import msvcrt`, or `else: import termios, tty`) are permitted and necessary, since `import termios` raises `ImportError` on Windows.

## Before submitting

> On macOS/Linux use `python3`. On Windows use `py`.

1. Run the test suite — all tests must pass:
   ```bash
   python3 tests.py
   ```
   The suite lives in the `tests/` package — one module per source file:
   `test_statusline.py`, `test_monitor.py`, `test_shared.py`, `test_pulse.py`,
   `test_update.py`. The root-level `tests.py` is a thin wrapper that runs
   `unittest discover tests/`, so the `py tests.py` invocation continues to work
   unchanged.

   **Baseline: 583 tests passing (3 skipped on platforms missing optional artifacts).**
   Contributions must not reduce the passing count without explanation. If you add
   tests, put them in the file that matches the module under test — helpers go in
   `test_shared.py`, TUI logic in `test_monitor.py`, and so on.

2. Verify all files compile cleanly:
   ```bash
   python3 -c "import py_compile, shared; [py_compile.compile(f, doraise=True) for f in shared.PY_FILES]"
   ```

3. Test manually on at least one platform with a live Claude Code session.

4. **Activate the pre-push hook (one-time setup per clone)** — scans outgoing commits for obvious secrets (`sk-ant-*`, `sk-proj-*`, `AKIA*`, `ghp_*`, PEM blocks) and sensitive filenames (`*.pem`, `*.env`, `credentials.json`):
   ```bash
   git config core.hooksPath .githooks
   ```

## What to keep in sync

- **`shared.py` is the single source of truth** — all cross-file constants, helpers, ANSI palette, and regexes live there. Never duplicate a literal or a helper in `statusline.py` / `monitor.py` / `pulse.py` / `update.py`. The shared surface includes:
  - **Constants:** `VERSION`, `PY_FILES`, `_SID_RE`, `_ANSI_RE`, `MIN_EPOCH`, `MAX_FILE_SIZE`, `HISTORY_READ_MAX`, `HISTORY_AGGREGATE_MAX`, `TRANSCRIPT_MAX_BYTES`, `DATA_DIR`, `DATA_DIR_NAME`, `VERSION_RE`, `RESERVED_SIDS`, `WARN_PCT`, `CRIT_PCT`.
  - **ANSI palette:** `E`, `R`, `B`, `C_RED`, `C_GRN`, `C_YEL`, `C_ORN`, `C_CYN`, `C_WHT`, `C_DIM`.
  - **Helpers:** `_num`, `_sanitize`, `safe_read`, `f_tok`, `f_cost`, `f_dur`, `f_cd`, `char_width`, `is_safe_dir`, `ensure_data_dir`, `ensure_utf8_stdout`, `load_history`, `strip_context_suffix`, `compact_context_suffix`, `extract_changelog_entry`, `run_git`, `calc_rates`.
  - If you add a helper or constant that is (or could be) used by more than one module, put it in `shared.py` from day one.
  - **No parallel implementations** — a regression-guard test (`tests/test_shared.py::TestPyFilesSingleSourceOfTruth`) fails if the post-pull syntax-check loop reappears inline in `monitor.py` or `update.py` instead of delegating to `shared.check_syntax_after_pull`. Apply the same discipline to any future helper: extract to `shared.py`, have both consumers delegate.
- **`DATA_DIR`-dependent helpers need per-module wrappers.** If you add a helper to `shared.py` that resolves a path inside `DATA_DIR`, expose a thin wrapper in `monitor.py` and (if relevant) `statusline.py` that forwards `data_dir=DATA_DIR`. This preserves test monkey-patchability of the consumer module's `DATA_DIR` constant. Current examples: `monitor.load_history` and `statusline._load_history_for_rates`, both forwarding to `shared.load_history(sid, n, data_dir=DATA_DIR)`.

## File-IPC schema changes

When changing the JSON shape that `statusline.py` writes and `monitor.py` reads:

1. Bump `shared.SCHEMA_VERSION` (currently `1`).
2. Document the new field or structural change in [docs/FILE-IPC-CONTRACT.md](FILE-IPC-CONTRACT.md).
3. Read new fields via `dict.get(key, default)` so older snapshots (without the field) remain loadable — forward-compat reads are required, not optional.

## Pull requests

For anything non-trivial — new features, behavior changes, refactors beyond local cleanup — **open an issue first** so the approach can be discussed before work begins. Typo fixes, small doc edits, and obvious bug fixes can go directly to a PR.

- One logical change per PR.
- Include a description of what changed and why.
- Reference any related issues.
- Commit/PR title format: `<type>(<scope>): <short description>`.
  Allowed scopes: `monitor`, `statusline`, `pulse`, `shared`, `update`, `tests`, `changelog`, `audit`, `security`, `license`, `docs`, `ci`, `repo`.

## See also

- [README.md](Archyv/cc-aio-mon/README.md) — feature overview, metrics, keyboard shortcuts, architecture
- [docs/ARCHITECTURE.md](ARCHITECTURE.md) — module map, data-flow diagram, and "where to look for X" guide; read this before opening `monitor.py` (~2 670 LOC)
- [docs/FILE-IPC-CONTRACT.md](FILE-IPC-CONTRACT.md) — canonical field schema for the statusline→monitor JSON contract and JSONL history entries
- [CHANGELOG.md](Archyv/cc-aio-mon/CHANGELOG.md) — release history
- [.github/SECURITY.md](.github/SECURITY.md) — security model and vulnerability reporting
- [NOTICE](NOTICE.md) — legal notice and affiliation disclaimer
