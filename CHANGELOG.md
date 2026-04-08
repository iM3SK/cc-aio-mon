# Changelog

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
