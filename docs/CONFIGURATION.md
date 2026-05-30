# Configuration — environment variables

Authoritative catalog of every environment variable `cc-aio-mon` reads.
Grounded in observable code: every entry below has a file:line citation.
If you add a new env var, add it here in the same commit.

---

## Naming convention

- **`CC_AIO_MON_*`** — preferred prefix for **all new** project-specific
  toggles and overrides.
- **`CLAUDE_STATUS_*` / `CLAUDE_WARN_*`** — legacy prefixes inherited from
  the v1.x line. Kept for backward compatibility; **do not introduce new
  vars under these prefixes**. If you need to add a threshold or feature
  flag, use `CC_AIO_MON_*`.
- Generic Unix/Windows / Python variables (`TMPDIR`, `HOME`, `TERM`,
  `COLUMNS`, `PYTHONUTF8`, …) keep their standard names — never re-prefix.

---

## Project-specific variables

### `CC_AIO_MON_NO_UPDATE_CHECK`

- **Type:** flag (`"1"` disables, anything else enables)
- **Default:** unset → release-check worker enabled
- **Read by:** `monitor.py` (`_rls_maybe_check`)
- **Effect:** disables the hourly background `git fetch` + remote-version
  poll that powers the `RLS` (Release Status) line and the update modal's
  "new version available" notification.
- **When to set:** offline use, slow / metered network, CI containers
  without git push access, integration tests that assert deterministic
  render output.

### `CC_AIO_MON_NO_PULSE`

- **Type:** flag (`"1"` disables, anything else enables)
- **Default:** unset → Pulse worker enabled
- **Read by:** `monitor.py` (`main`)
- **Effect:** suppresses the `pulse.py` background thread that probes
  `status.claude.com` + the Anthropic API ping endpoint. The `STB`
  (Stability) modal becomes unavailable.
- **When to set:** offline use, restricted egress, CI environments,
  privacy-sensitive setups that should not emit any outbound HTTP.

---

## Threshold variables (legacy `CLAUDE_*` prefix)

These tune color thresholds in the statusline and dashboard. Kept on the
legacy prefix because flipping the name now would break existing user
shells; new thresholds go under `CC_AIO_MON_*`.

### `CLAUDE_STATUS_WARN`

- **Type:** float (percent, 0–100)
- **Default:** `50.0`
- **Read by:** `shared.py` → exported as `shared.WARN_PCT`
- **Effect:** percentage threshold at which `CTX` / `5HL` / `7DL` / `CTR`
  bars flip from green to yellow.
- **Parser:** `shared._env_pct` — invalid / empty values silently fall back
  to the default (see `tests/test_shared.py::TestEnvPct`).

### `CLAUDE_STATUS_CRIT`

- **Type:** float (percent, 0–100)
- **Default:** `80.0`
- **Read by:** `shared.py` → exported as `shared.CRIT_PCT`
- **Effect:** percentage threshold at which the same bars flip from yellow
  to red. Also drives the `!CTX>X%` warning glyph on the dashboard
  (`monitor.py`).

### `CLAUDE_WARN_BRN`

- **Type:** float ($/min)
- **Default:** `3.0`
- **Read by:** `monitor.py` (`_env_float` → `WARN_BRN`)
- **Effect:** burn-rate threshold above which the `BRN` segment switches
  to its alert color. The dashboard `BRN OVER TIME` axis is fixed at
  `0–10 $/min` regardless of this value (axis is set by `BRN_MAX`, not
  the warn threshold).

---

## Inherited / standard variables

`cc-aio-mon` reads several environment variables defined by Python, the
shell, or the OS. They are listed here so platform-specific surprises
have a documented diagnostic anchor.

### `TERM`

- **Type:** string
- **Default:** set by the terminal emulator
- **Read by:** `monitor.py`
- **Effect:** if `TERM=dumb`, `monitor.py` aborts with an explanatory
  message instead of trying to drive an interactive TUI on a non-capable
  terminal (CI logs, redirected output, etc.).

### `COLUMNS`

- **Type:** int
- **Default:** unset → `os.get_terminal_size()` fallback
- **Read by:** `statusline.py` (`_get_terminal_width`)
- **Effect:** explicit terminal-width override for the one-line
  statusline. Useful when the parent process (Claude Code) reports a
  width that differs from what the user sees, or for fixed-width tests.

### `TMPDIR` / `TMP` / `TEMP`

- **Type:** path
- **Default:** OS default (`/tmp` on Unix, `%LOCALAPPDATA%\Temp` on
  Windows)
- **Read by:** `tempfile.gettempdir()` indirectly via `shared.DATA_DIR`
- **Effect:** root for `$TMPDIR/claude-aio-monitor/` — the IPC directory
  that holds session snapshots, JSONL history, the singleton lock, the
  crash log, and the rotated crash log. Whitelisted in `shared.run_git`'s
  env scrub (`shared.py`).

### `HOME` / `USERPROFILE`

- **Type:** path
- **Default:** OS default
- **Read by:** `pathlib.Path.home()` indirectly when reading
  `~/.claude/projects/<project>/<session>.jsonl`
- **Effect:** root for Claude Code's transcript directory, which
  `monitor.py` scans read-only for AI-generated session titles
  (`_scan_ai_title`) and cross-session token/cost aggregates
  (`scan_transcript_stats`).

### `PYTHONUTF8`

- **Type:** flag (`"1"` enables UTF-8 mode)
- **Default:** unset (Python uses the locale codec for stdin/stdout)
- **Recommended:** `1` on non-UTF-8 Windows locales (SK, CZ, PL, …) when
  invoking `statusline.py` or `monitor.py` directly. The shipped
  `ccaiomon.bat` sets this; users on bare `python statusline.py` should
  consider the wrapper instead.
- **Why:** Claude Code emits its statusline JSON as UTF-8 bytes. Without
  `PYTHONUTF8=1` (or the byte-level `sys.stdin.buffer.read()` path added
  in NEW-002), Python on CP1250 / CP1252 Windows locales would
  mis-decode diacritics in `session_name` / `aiTitle` before
  `json.loads` ever ran. The fix is already in `statusline.py`; this
  env var is belt-and-braces for any future stdin handling that adds
  text-mode reads.

### `PYTHONIOENCODING`

- **Type:** string (e.g. `utf-8`)
- **Default:** unset
- **Recommended:** `utf-8` on non-UTF-8 Windows locales — covers the
  cases `PYTHONUTF8=1` does not (notably some embedded Python launchers
  and older 3.8 setups). The shipped `ccaiomon.bat` sets this too.
- **Effect:** forces `sys.stdout.encoding` to a known value;
  `shared.ensure_utf8_stdout()` is the runtime safety net if neither env
  var is set.

---

## Test / development variables

These are used by the test suite to make assertions deterministic. They
are not part of the user-facing contract and may change without notice.

### `_TEST_ENV_FLOAT` / `_TEST_ENV_FLOAT_BAD`

- **Read by:** `tests/test_monitor.py` — `_env_float` parser
  smoke tests.
- **Lifecycle:** set + unset within a single `setUp`/`tearDown` pair.
  Never leaks across test boundaries.

---

## See also

- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — what to keep in sync between
  modules, including env-var single-source-of-truth pattern in
  `shared.py`.
- [`FILE-IPC-CONTRACT.md`](FILE-IPC-CONTRACT.md) — companion document
  for the file-based IPC contract that `TMPDIR` roots.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — where these vars are consumed
  in the runtime data flow.
