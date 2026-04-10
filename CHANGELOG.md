# Changelog

## v1.6.2 — 2026-04-10

**Features:**
- `update.py` — self-update script. Read-only by default (status + CHANGELOG preview); `--apply` flag runs `git pull --ff-only`. Safety guards: dirty tree, wrong branch, detached HEAD, divergence, downgrade, Python version. Cross-platform (Windows/macOS/Linux), stdlib only.

**Other:**
- VERSION bumped to `1.6.2`

## v1.6.1 — 2026-04-10

**Refactor:**
- Replaced `install.ps1` / `install.sh` with `check-requirements.ps1` / `check-requirements.sh` — pure read-only dependency checks (Python, Git, Claude Code CLI); no path hunting, no JSON block generation, no settings.json helpers
- `check-requirements.sh` prints detected Python command (`python3` or `python`) so users on fallback systems know which to substitute in setup docs
- Renamed `docs/install-*.md` → `docs/setup-*.md` to reflect that setup is manual; scripts are optional diagnostics only

**Documentation:**
- README: `## Install` section renamed to `## Setup` for consistency with file names
- README: `Metrics at a Glance` table now includes DUR, NOW, UPD (were missing)
- README: "zero dependencies" claim replaced with accurate "stdlib only" throughout — Python itself is a dependency
- README: Python dependency requirement clarified — pre-installed on macOS/Linux, separate install only on Windows
- README: statusline trigger description corrected per official Claude Code docs (`after each assistant message, permission mode change, or vim mode toggle`, 300ms debounce)
- README: platform-specific Python commands in Dashboard/Contributing examples (`python3` on macOS/Linux, `py` on Windows)
- README: badge updated from `pip_packages none` to `dependencies stdlib only`
- CONTRIBUTING: same stdlib only / python3-py consistency; "Three entry files" → "Three runtime files" (clarifies `tests.py` exclusion)
- `docs/setup-macos.md` and `docs/setup-linux.md`: added Python fallback note for users who only have `python` (no `python3`)
- All setup docs: added `cd` instruction before running `check-requirements` script
- `.github/PULL_REQUEST_TEMPLATE.md`: removed incorrect mention of `rates.py` in ANSI palette sync check (only `statusline.py` and `monitor.py` carry the palette)
- `.github/ISSUE_TEMPLATE/feature_request.md`: updated from "Single-file" to "Three runtime files"

**CI:**
- `tests.yml`: replaced inline Python-version ternary with proper `exclude:` block — removes redundant Windows 3.8 job that was collapsing to 3.12 at runtime
- `scorecard.yml`: removed dead `pull_request` branch from job `if:` condition (pull_request is not in `on:` triggers)

**Other:**
- VERSION bumped to `1.6.1`

## v1.6.0 — 2026-04-09

**Features:**
- BRN/CTR/CST progress bars with fixed ranges (0-1.0 $/min, 0-5.0 %/min, 0-$50) — same visual style as APR/CHR/CTX/5HL/7DL
- Smart warnings system — automatic header alerts for: CTF < 30 min, 5HL/7DL > 80%, BRN above configurable threshold
- Cross-session cost aggregation — TDY (today) and WEK (rolling 7-day) totals under CST, cached with 30s TTL
- `CLAUDE_WARN_BRN` env var — configurable burn rate warning threshold (default 0.50 $/min)

**Layout changes:**
- Compact layout — removed empty lines between metric sections
- Separators changed from `│` to `-` in sub-stat detail lines
- CTX sub-stat simplified: used tokens + in/out (removed redundant total, shown in header model name)
- Removed CTF (Context Full ETA) from dashboard — low value metric, context % + rate sufficient. Statusline CTF remains.
- TDY/WEK moved under CST as sub-stats
- LNS on own line below NOW/UPD: white label, green added count, red removed count
- Legend overlay: BG_BAR header background, metric ranges inline, cleaned up entries
- Footer uses full terminal height (no wasted bottom row)

**Other:**
- `tests.py` — 142 tests (IPC, mkbar, truncate, cross-session costs, fixed-range constants)
- Legend: added sub-stat descriptions (DUR, API, c.r, c.w, in, out)
- README: SEO/GEO overhaul — AI-readable Input/Output table, data flow, metrics at a glance
- PR template: updated compile check to include rates.py
- CI: actions updated to Node.js 24 (checkout v6.0.2, setup-python v6.2.0)
- VERSION bumped to `1.6.0`

## v1.5.2 — 2026-04-08

**Internal:**
- `rates.py` — shared `calc_rates()` for `monitor.py` and `statusline.py` (one implementation: both timestamps ≥ 2020, non-increasing cost/context → `None` for that metric)
- `tests.py` — parity/identity tests plus edge cases for decreasing cost/context and bad `t1`
- Removed dead `seg_lns()` from `statusline.py` (was never called in `build_line()`)
- Fixed `VERSION` constant: `"1.5"` → `"1.5.2"`
- Fixed legend colors: 5HL/7DL legend entries now use yellow (base color) instead of green
- Fixed README: screenshot alt texts updated from v1.4 to v1.5.2; 5HL/7DL color description corrected to yellow/orange/red

## v1.5.1 — 2026-04-08

**Bug fixes:**
- Fixed: spacing around `│` separators in detail lines — now display as ` │ ` with spaces instead of bare `│`
- Fixed: stats section (CST, BRN, CTR, CTF, NOW, UPD) now vertically stacked on individual lines instead of paired side-by-side for better readability

## v1.5 — 2026-04-08

**Visual overhaul:**
- Rate limits (5HL, 7DL) now use dynamic colors based on usage % — green (<50%), yellow (50-79%), red (>=80%) — in both statusline and dashboard. 7DL base color changed from green to yellow (same category as 5HL)
- "to reset" countdown reformatted: `reset in: 3d 23h` with inverse time coloring — green when plenty of time remains, red when window is almost expired
- Context alert changed from hardcoded `! >200k` to dynamic `! CTX>80%` — works for any context size (200k, 1M)
- Header line now has `BG_BAR` background extending to full terminal width (same Nord polar night as statusline)
- Sub-stat separators changed from ` / ` (dim slash with spaces) to `│` (dim vertical bar, no spaces) — more compact
- Stats section (CST, BRN, CTR, CTF, NOW, UPD) compacted from 6 rows to 3 rows with `│` separators
- BRN/CTR unit format compacted: `$ / min` → `$/min`, `% / min` → `%/min`
- Legend updated with dynamic color notes for rate limits

**New helpers:**
- `_limit_color(pct)` — dynamic color for rate limit metrics
- `_reset_color(resets_epoch, window_secs)` — inverse countdown color

**Other:**
- `tests.py` expanded from 96 to 107 tests — added `_limit_color`, `_reset_color`, dynamic color assertions for 5HL/7DL segments

## v1.4.2 — 2026-04-08

**Color scheme redesign:**
- Colors now grouped by semantic category instead of arbitrary assignment
- Added `C_ORN` (nord12 aurora orange, 208/135/112) for cost/finance metrics
- CST and BRN changed from cyan/yellow to **orange** — finance metrics visually distinct
- CHR changed from white to **green** — performance metric, grouped with APR
- DUR changed from green to **dim** — utility/time, not a health metric
- NOW changed from white to **dim** — lowest visual priority
- UPD changed from green to **dim** — utility metric
- Legend overlay updated to match new color scheme
- Restored CHR segment to statusline (was removed in v1.3)
- CST moved from left to right side in statusline layout
- Both `statusline.py` and `monitor.py` palettes synchronized

**Bug fixes:**
- Statusline bar background now extends to full terminal width in fullscreen — `R` (full ANSI reset) inside segments was killing `BG_BAR` background color; replaced with `RB` (reset + re-apply bar bg) so background persists through all segments, separators, spacer, and `EL` erase-to-end-of-line

**Other:**
- `tests.py` expanded from 41 to 96 tests — added 55 statusline tests: `_sanitize`, `_get_terminal_width`, all 13 segment builders with color assertions, `build_line` layout, `RB` bar background persistence (regression tests for the fullscreen fix), `_calc_rates`

## v1.4.1 — 2026-04-08

**Bug fixes:**
- `rate_limits: {}` (empty object) now shows "Rate limits: no data" instead of silently rendering nothing — distinct from the `null` branch which shows "subscription data unavailable"
- `calc_rates` rejects timestamps older than 2020-01-01 — prevents nonsense BRN/CTR values when `"t"` field is missing or corrupt in history

**Other:**
- `MAX_FILE_SIZE` comments in both files note keep-in-sync requirement
- `tests.py` added — 41 stdlib unittest cases: `_fit_buf_height` (clip modes, edge rows), `calc_rates` (sanity guards), `_num`, formatters (`f_tok`, `f_cost`, `f_dur`, `f_cd`)

## v1.4 — 2026-04-08

**Features:**
- Session status line — new always-visible line below header showing session state (active/inactive with duration, animated line spinner)
- Session switching at runtime — press `s` to return to session picker and switch sessions anytime
- Session picker shows all sessions — both live and stale sessions now listed (auto-select triggers only when exactly one live session)
- Manual refresh resets stale — pressing `r` now resets stale timer for immediate recovery if session is still alive

**Layout changes:**
- Header now text-only (removed dots12 braille spinner) — displays `CC AIO MON 1.4  model`
- Removed `STALE` tag from header (replaced by session status line showing inactive duration)
- Separator line moved below session status (was between header and content)
- Footer shortcuts condensed: `[q]qt [r]rf [s]se [l]le`
- Legend overlay now includes KEYS section with all keyboard shortcuts

**Bug fixes:**
- Fixed ghost header duplication at bottom of screen — flush now clears remaining lines below buffer (`\033[J`)
- `seg_cost` and `seg_dur` in statusline.py now use `_num()` — prevents TypeError when values arrive as strings
- `resets_at` timestamps in 5HL/7DL normalized via `_num()` in both statusline.py and monitor.py — prevents TypeError on non-numeric values
- History JSONL timestamp `"t"` can no longer be overwritten by upstream data (`{**data, "t": ...}` instead of `{"t": ..., **data}`)
- `rate_limits: {}` (empty dict) no longer treated as missing — uses `is not None` check in monitor.py
- `write_shared_state` serializes data once before both writes — `TypeError`/`ValueError` during `json.dumps` aborts early; `.jsonl` append is skipped when the atomic `.json` write fails (`snapshot_ok` guard), keeping snapshot and history in sync
- `render_legend` and `render_picker` now respect terminal height via `_fit_buf_height` — overlay and picker no longer overflow short terminals (same trimming logic as the main dashboard)

## v1.3 — 2026-04-08

**Statusline redesign:**
- Removed progress bars — text-only segments for maximum density
- Removed CHR, LNS, !200k segments — statusline now shows only: model, CST, CTX, 5HL, 7DL, DUR
- Shortened model name (dropped context size suffix)
- Separator changed from `─` to `│`
- Compact formatting (no space before `%`)
- All 6 segments fit in 80 columns (previously only 3 of 8 were visible)

**Bug fixes:**
- Stale sessions no longer zero out all metrics — last known values preserved with dimmed colors instead of blank bars
- Stale threshold increased from 5 minutes to 30 minutes (`STALE_THRESHOLD` constant) — Claude Code emits no events during idle, 5 min was too aggressive
- `load_history` error no longer replaces good history with empty list — prevents BRN/CTR/CTF from disappearing on transient I/O errors
- APR and CHR sections show 0% placeholder bar with descriptive text when no data available (independent of stale state)
- DUR segment label now bold (consistent with all other segment labels)
- Spinner comment corrected (50ms, not 80ms)
- Legend LNS color fixed to match render (dim, not green)
- `f_tok` and `f_dur` formatting synchronized between statusline and dashboard
- `used_percentage` no longer crashes on non-numeric values (safe coercion via `_num()`)
- Stale detection works correctly after session file disappears (uses `last_seen` timestamp)
- Session picker truncates long `cwd` paths to terminal width
- `truncate()` appends ANSI reset to prevent color bleed
- Dead `HISTORY_MAX_LINES` constant removed from trim logic

## v1.2 — 2026-04-08

**Features:**
- CTX now shows used/total token count (e.g., `420k/1M`) in both statusline and dashboard
- CHR (Cache Hit Rate) segment added to statusline with progress bar
- 7DL progress bar added to statusline (was text-only)
- `!200k` warning segment in statusline when context exceeds 200k tokens
- `STALE` indicator in dashboard header when session data is outdated
- Version constant — single source of truth, displayed in header and session picker

**Bug fixes:**
- Session picker: digit keypresses no longer silently dropped (double `poll_key()` removed)
- Stale detection now works when session file is deleted (`last_mt` reset to 0)
- All progress bars (CTX, APR, CHR) reset when session data is stale (>5 min without update)
- 5HL/7DL show 0% when `resets_at` timestamp is in the past — fixed in both files
- 5HL/7DL handle `used_percentage: null` without crash (`or 0` guard)
- History trim now fires on every call when triggered by size (was only trimming when >2000 lines)
- Statusline segment width calculations use dynamic ANSI-strip instead of fragile hardcoded formulas
- `removed` variable name collision in shrink loop renamed to `_shrunk`

**Security:**
- TOCTOU fix: `load_state` and `load_history` now use single `open()` + bounded `read()` instead of separate `stat()` + `read()`
- `_sanitize` now strips C1 control characters (`\x80–\x9f`) in addition to C0 — blocks 8-bit CSI injection on VT220 terminals

## v1.1 — 2026-04-07

**Security:**
- Path traversal prevention via session ID validation
- Terminal escape injection protection (control character sanitization)
- Atomic writes via unpredictable temp filenames (NamedTemporaryFile)
- File size limits on all JSON/JSONL reads
- SIGTERM handler for graceful terminal cleanup
- Temp directory created with restricted permissions (0o700)

**Bug fixes:**
- History trim now triggers on file size (was never firing due to per-process counter reset)
- Off-by-1 in statusline segment width calculation (seg_ctx, seg_5hl)
- Keyboard input (`q`, `r`, `l`) always responsive (polling moved before render check)
- Render errors caught per-frame (corrupted data no longer crashes TUI)

**Features:**
- dots12 braille spinner animation in dashboard header (56 frames, 50ms)
- Full-width separator lines (previously capped at 72 chars)
- ANSI-aware line truncation (prevents terminal overflow)
- Smooth resize with gradual section compression
- Stale .tmp file cleanup in session listing
- --refresh argument validated and clamped (100-60000ms)

**Cleanup:**
- Removed dead code (unused imports, variables, functions)
- Environment variable parsing with safe fallback defaults
- History file cached by mtime (no unnecessary reloads)

## v1.0 — 2026-04-07

- Initial release
- Statusline: Nord truecolor, 3-letter codes, enclosed bars, responsive segments
- Monitor: fullscreen TUI, 5 bar metrics (APR/CHR/CTX/5HL/7DL), stats, legend overlay
- Responsive resize with 50ms tick, empty line trimming
- IPC via atomic JSON + JSONL history, burn rate calculation
- Zero dependencies, cross-platform (Windows/macOS/Linux)
