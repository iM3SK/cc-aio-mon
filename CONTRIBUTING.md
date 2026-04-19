# Contributing

## Constraints

- **Stdlib only** — no pip installs, no node_modules.
- **Five runtime files** — `statusline.py`, `monitor.py`, `shared.py`, `update.py`, and `pulse.py`. No additional runtime modules (test files like `tests.py` are not runtime).
- **Cross-platform** — changes must work on Windows, macOS, and Linux.

## Before submitting

> On macOS/Linux use `python3`. On Windows use `py`.

1. Run the test suite — all tests must pass:
   ```bash
   python3 tests.py
   ```

2. Verify all files compile cleanly:
   ```bash
   python3 -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('shared.py', 'statusline.py', 'monitor.py', 'update.py', 'pulse.py')]"
   ```

3. Test manually on at least one platform with a live Claude Code session.

4. **Activate the pre-push hook (one-time setup per clone)** — scans outgoing commits for obvious secrets (`sk-ant-*`, `sk-proj-*`, `AKIA*`, `ghp_*`, PEM blocks) and sensitive filenames (`*.pem`, `*.env`, `credentials.json`):
   ```bash
   git config core.hooksPath .githooks
   ```

## What to keep in sync

- **`shared.py` is the single source of truth** — all cross-file constants, helpers, ANSI palette, and regexes live there. Never duplicate a literal or a helper in `statusline.py` / `monitor.py` / `pulse.py` / `update.py`. The shared surface includes:
  - **Constants:** `VERSION`, `PY_FILES`, `_SID_RE`, `_ANSI_RE`, `MAX_FILE_SIZE`, `TRANSCRIPT_MAX_BYTES`, `DATA_DIR`, `VERSION_RE`, `RESERVED_SIDS`.
  - **ANSI palette:** `E`, `R`, `B`, `FAINT`, `C_RED`, `C_GRN`, `C_YEL`, `C_ORN`, `C_CYN`, `C_WHT`, `C_DIM`.
  - **Helpers:** `_num`, `_sanitize`, `safe_read`, `f_tok`, `f_cost`, `f_dur`, `f_cd`, `char_width`, `is_safe_dir`, `ensure_data_dir`, `strip_context_suffix`, `compact_context_suffix`, `extract_changelog_entry`, `run_git`, `calc_rates`.
  - If you add a helper or constant that is (or could be) used by more than one module, put it in `shared.py` from day one.

## Pull requests

For anything non-trivial — new features, behavior changes, refactors beyond local cleanup — **open an issue first** so the approach can be discussed before work begins. Typo fixes, small doc edits, and obvious bug fixes can go directly to a PR.

- One logical change per PR.
- Include a description of what changed and why.
- Reference any related issues.
- Commit/PR title format: `<type>(<scope>): <short description>` — see `.claude/CLAUDE.md` Git Commit Policy.

## See also

- [README.md](README.md) — feature overview, metrics, keyboard shortcuts, architecture
- [CHANGELOG.md](CHANGELOG.md) — release history
- [.github/SECURITY.md](.github/SECURITY.md) — security model and vulnerability reporting
- [NOTICE](NOTICE) — legal notice and affiliation disclaimer
