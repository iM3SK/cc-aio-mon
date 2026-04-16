# Changelog

## v1.9.0 — 2026-04-16

**New feature — Anthropic Pulse modal:**
- New `pulse.py` module — real-time Anthropic backend stability monitor. Press `p` to open modal. Stdlib only, zero dependencies, zero token cost, cross-platform.
- **Stability score (0-100)** with weighted composite: status indicator (50%), active incidents (30%), API latency (20%). Verdict bands: ≥80 `SAFE TO CODE` (green), 50-79 `DEGRADED` (yellow), <50 `NOT SAFE TO CODE` (red), error states render in dim.
- **Two-tier signal** — (1) passive: `status.anthropic.com/api/v2/summary.json` for indicator + components + incidents; (2) active: HTTPS GET to `api.anthropic.com/v1/messages` measures real TLS handshake + HTTP round-trip (any HTTP status = endpoint alive, only network/TLS errors = down).
- **Rolling median smoothing** — `deque(maxlen=10)` keeps last 10 scores; verdict derived from median of last 5. Absorbs single-sample outliers (one slow probe doesn't flip verdict). Warm-up below 3 samples passes raw through.
- **Latency percentiles** — p50 / p95 over ~30 min window (60 samples) displayed in modal when ≥3 samples available.
- **Per-model tagging** — regex word-boundary match (`opus` / `sonnet` / `haiku`) against incident titles + first incident update body. Affected models displayed inline next to incidents (`[opus]`) and as a top-level rollup row (`MODELS  opus / sonnet / haiku` — red when affected, green when clear). Honest limitation: signal only available when Anthropic flag the model publicly.
- **JSONL persistence** — every probe appended to `$TMPDIR/claude-aio-monitor/pulse.jsonl`. Schema: `{ts, score, level, indicator, incidents, latency_ms, error}`. Stores raw_score (truth), not smoothed.
- **Hybrid cleanup** — (1) startup: drop entries older than 24h + cap at 2000 records; (2) runtime: check size every 100 appends, trim to last 500 lines if file exceeds 1 MB (aligned with `shared.MAX_FILE_SIZE`). Atomic rewrite via `NamedTemporaryFile` + `os.replace`.
- **Error taxonomy** — `HTTPError` code (`HTTP 503`), `socket.timeout` (`timeout`), `socket.gaierror` (`DNS fail`), `URLError` (`net: <type>`), `JSONDecodeError` (`parse: JSONDecodeError`). Distinguishes API-side failures from client-side code bugs in the UI.
- **Thread-safe** — daemon worker (`pulse.start_pulse_worker()`) fetches every 30s. All shared state guarded by `threading.Lock` (`_snapshot_lock`, `_history_lock`, `_log_lock`, `_worker_lock`). Best-effort I/O: all `OSError` silently swallowed to prevent worker death.
- **Modal integration** — new `[p]` hotkey (global + menu), added to legend hotkeys section. Render dispatch is session-independent (works without any Claude Code session). Closes on any key.
- **Bounded resources** — `MAX_RESPONSE_BYTES = 512 KB` cap on status.json response, HTTP timeout 5s, probe timeout 4s, fetch interval 30s. `User-Agent: cc-aio-mon-pulse/1.0`.

**Refactor:**
- `compute_score(raw)` refactored to use new pure helper `_score_to_verdict(score)` — enables verdict derivation from both raw and smoothed scores without code duplication.
- `_ping_api()` replaced TCP connect with HTTPS probe (measures TLS + HTTP, not just socket) — realistic edge latency, catches Cloudflare 502/503 + TLS issues that pure TCP connects miss.

**Tests:**
- 53 new tests covering: scoring buckets + verdict mapping (incl. exact thresholds 50/80 and latency boundaries 300/800/2000 ms), indicator/incident extraction, snapshot schema + thread safety, modal rendering (empty/ok/error states), rolling median smoothing (outlier absorption, sustained drop, None handling, bounded history), latency percentiles (empty/below-min/basic/skip-none/bounded), JSONL persistence (append + line-delimited + bad data), startup cleanup (age cutoff + count cap + malformed lines + missing file), runtime rotation (over-max + only-every-N + noop-under-max + noop-at-exact-max-size), model tagging (case insensitive + word boundary + multi-model + empty + extract integration), network-layer error taxonomy (mocked `urlopen`: HTTPError 401/404/503, URLError+timeout/gaierror/other, direct socket.timeout, oversized response, JSONDecodeError, OSError), `_ping_api` HTTP-alive semantics (401/405 = alive), `_refresh_once` end-to-end (success path, fetch-error tag propagation, malformed `incident_updates` regression guard).
- Total suite: 421 tests, all passing.

**Hardening (post-audit fixes, same release):**
- `CC_AIO_MON_NO_PULSE=1` environment variable — opt-out switch for the background Pulse worker. Mirrors the existing `CC_AIO_MON_NO_UPDATE_CHECK=1` pattern. Required for strict-firewall / air-gapped deployments. Documented in README env-var table + all three platform setup guides.
- `_extract()` hardened against malformed `incident_updates` — wraps first-element access in `isinstance(..., dict)` guard to prevent `AttributeError` escape into the worker's last-resort handler when the status API returns non-dict elements.
- `SECURITY.md` updated — stale claim removed, outbound network surface now documented (URLs, cadence, data sent = UA header only, opt-out env var).
- CI workflows now include `pulse.py` — Bandit security scan, compile check in `tests.yml`, PR template tuple, `CONTRIBUTING.md` compile command, `README.md` compile command. Previously the highest-risk (network-facing) module was unscanned.
- Indicator color fallback — unknown status indicators (future schema) render as `C_DIM` instead of alarming red.
- `TestRenderMenu.test_contains_all_keys` now asserts on all 8 menu hotkeys (was checking only 6 — wouldn't catch silent removal of `[p]` / `[m]` / `[c]`).
- `test_snapshot_has_schema` expanded to assert on `raw_score`, `latency_p50_ms`, `latency_p95_ms` (prevents silent breakage of modal rendering).

**Other:**
- VERSION bumped to `1.9.0`

## v1.8.4 — 2026-04-15

**UI:**
- Redesigned all modals to unified design language — `BG_BAR` header bands on all section headers, `[key]` bracket pattern for hotkeys, single-space separators, consistent footer. Applied across: legend, menu, cost breakdown, token stats, update manager, session picker.
- Legend: `KEYS` → `HOTKEYS` with `[key]` bracket format. All sub-sections (HOTKEYS, TOKEN STATS, COST BREAKDOWN, UPDATE) now have `BG_BAR` header bands. Added RST (Reset Countdown) and RTE (Rate Value) sub-codes. Complete set: 32 metric codes + 9 hotkeys.
- Menu: `[key]` bracket format matching legend. Removed `[1-9] Select Session` (picker-only). Sub-sections VIEWS and SYSTEM have `BG_BAR` headers.
- Session picker: compact display — UUID truncated to 8 chars, model as short code (OP 4.6), live/stale tag. Active sessions sorted first, max 9 shown (+N more). `force_picker` flag prevents auto-connect bypass when pressing `[s]`.
- Cost breakdown: `BURN RATE OVER TIME` section — 3 equal time slices (ERL/MID/LAT) with `mkbar` bars scaled to `BRN_MAX`. All sub-sections (TOKEN COSTS, SESSION TOTALS, BURN RATE) have `BG_BAR` header bands. Removed padding/right-alignment from token values. Model context suffix stripped (`Opus 4.6 (1M context)` → `Opus 4.6 1M`). Fixed O(n×buckets) → O(n + log n) via bisect.
- Token stats: model labels as 3-char codes (OP 4.6, HA 4.5, SO 4.6). `MODELS` section has `BG_BAR` header, models separated by `sep()`. Sub-values: `In:`→`INP:`, `Out:`→`OUT:`, `Calls:`→`CLS:`. `Total` → `ALL`.
- Update modal: `CUR`/`REM` 3-letter codes. `[a] apply` shown in all states (disabled with reason when no update). Section headers UPPERCASE with `BG_BAR` bands.
- All sub-value labels uppercase 3-char: `DUR:`, `API:`, `CRD:`, `CWR:`, `INP:`, `OUT:`, `RST:`, `RTE:`, `CST:`, `TDY:`, `WEK:`, `NOW:`, `UPD:`, `LNS:`, `CLS:`, `TIN:`, `TOT:`, `CPM:`.
- Unified color scheme — labels always C_DIM, values in parent metric color. Fixed 21 mismatches.
- Unified spacing — single space between all values, no padding/right-alignment, no dash separators.
- `mkbar` percentage format: `5.1f` → `.1f` (no leading space), `%` without space.
- Session auto-connect: only when exactly 1 total session (not 1 active + stale). `force_picker` flag ensures picker shows after `[s]`.

**Bug fixes:**
- Fixed release check never triggering on freshly booted systems — `_rls_cache["t"]` initialized to `0.0` caused TTL check to pass when `time.monotonic()` (system uptime) was under 1 hour. Now initialized to `-_RLS_TTL` to guarantee immediate first check.
- Fixed `_model_label()` not stripping `[1m]` context suffix — model IDs like `claude-opus-4-6[1m]` displayed as raw strings instead of "Opus 4.6". Now strips `[...]` suffix consistently with `_get_pricing()`.
- Fixed `session_id: null` in JSON creating file `None.json` — `data.get("session_id", "default")` returned `None` for explicit null. Changed to `data.get("session_id") or "default"` in both statusline locations.
- Fixed double `CloseHandle` on Windows in `_get_terminal_width()` — when `GetConsoleScreenBufferInfo` succeeded but width was ≤0, handle was closed twice (undefined behavior). Restructured to single `CloseHandle` in `finally` block.
- Fixed `_apply_update_action()` blocking main thread for up to 30+ seconds — git pull + syntax check now runs in background daemon thread. UI remains responsive during update.
- Fixed SIGTERM handler calling `cleanup()` twice — explicit call + `atexit` handler. Now SIGTERM just calls `sys.exit(0)`, letting `atexit` handle cleanup once.
- Fixed potential lock deadlock if `Thread.start()` fails in `_rls_maybe_check()` — lock is now released in except block if thread spawn fails.
- Fixed floating-point drift in data reload interval — `since_data += tick` accumulated float error over long sessions. Now uses `time.monotonic()` difference for accurate interval tracking.
- Fixed rate limit bars showing 0% indefinitely after session ends — expired `resets_at` timestamps now show `(expired)` indicator.

- Fixed `stale` parameter shadowed by local variable in `render_frame()` rate limit section — renamed to `expired_tag` to prevent future bugs if code is reordered.
- Fixed `truncate()` and `vlen()` miscounting CJK fullwidth characters — East Asian Wide/Fullwidth characters (CJK, fullwidth punctuation) are now counted as 2 columns via `unicodedata.east_asian_width()`. Prevents terminal overflow on lines with CJK text.
- Fixed `codecs.lookup()` crashing on exotic/unrecognized encoding names — unhandled `LookupError` could crash all three scripts on systems with non-standard `sys.stdout.encoding`. Now caught with fallback to UTF-8 re-wrapping.

**Security:**
- Centralized data directory validation into `is_safe_dir()` and `ensure_data_dir()` in `shared.py` — replaces scattered `is_symlink()` calls with `lstat()` + `S_ISDIR` verification. Defends against symlinks, NTFS junctions (`FILE_ATTRIBUTE_REPARSE_POINT`), and TOCTOU races between `mkdir` and symlink checks. Applied across all file I/O paths in `monitor.py` (6 locations) and `statusline.py` (2 locations).
- Model names from transcript data now sanitized via `_sanitize()` before terminal output in `render_stats()` — prevents ANSI escape injection from crafted transcript files.
- `_ANSI_RE` regex expanded to match CSI sequences with `?` parameter bytes and OSC sequences — prevents escape leakage from malformed input.
- `_update_result` global is now thread-safe — read/write access wrapped with `threading.Lock` via `_get_update_result()` / `_set_update_result()`. Previously relied on CPython GIL atomicity for correctness.
- `.github/SECURITY.md` response SLA updated from 7 days to 72 hours.

**Refactor:**
- New shared helpers in `shared.py`: `char_width()` (CJK-aware character width), `is_safe_dir()` (lstat-based directory validation), `ensure_data_dir()` (mkdir + validate + chmod in one call). Replaces inline mkdir/symlink/chmod logic duplicated across `statusline.py` and `monitor.py`.
- Removed dead `_rls_fetching` variable — was set in 5 places but never read.
- Removed unreachable `k == "q"` check inside menu modal handler — already caught by global quit handler.
- Encoding check uses `codecs.lookup()` for robust codec comparison — previous `.replace("-", "")` approach missed Python's `utf_8` normalized form. Applied across `monitor.py`, `statusline.py`, `update.py`. Guarded with `try/except LookupError`.
- `NamedTemporaryFile` writes now clean up on failure across all locations — `fd.close()` + `os.unlink()` in except blocks prevent orphan temp files on disk-full errors. Applied to `statusline.py` (`write_shared_state`, `_trim_history`) and `monitor.py` (`_rls_check_worker`, `_write_shared_stats`).
- History JSONL read limit reduced from `MAX_FILE_SIZE * 10` (10 MB) to `MAX_FILE_SIZE * 2` (2 MB) in `load_history()` and `_load_history_for_rates()` — files are trimmed at 1 MB, 10x over-read was wasteful. `calc_cross_session_costs()` retains 10 MB limit for broader aggregation.
- Syntax check in `update.py` and `_apply_update_worker()` uses `compile()` with source text instead of `subprocess.run` + `py_compile` — avoids interpreter version mismatch on updates.
- File scan truncation warning — `scan_transcript_stats` now reports `truncated: True` in overview when 1000-file limit is hit, shown as `(1000 file limit)` in stats modal.
- Fixed stale CST comment: `$50` → `$200` to match actual `CST_MAX` constant.

**Docs:**
- Removed duplicate root `SECURITY.md` — `.github/SECURITY.md` (more detailed) is the canonical version displayed by GitHub.
- Untracked `PROMO.md` from git — was tracked despite `.gitignore` rule (added before ignore took effect).
- `.claude/CLAUDE.md`: updated JSONL file size limits, shared.py description updated with new helpers.
- `docs/setup-macos.md`: removed stale "not included in CI" note (macOS CI added in v1.8.1).
- `docs/ROADMAP.md`: cost breakdown marked as Done (v1.8.0), multi-session keybinding changed from `m` to `v` (conflict with menu modal).
- `README.md`: added menu modal and cost breakdown features, `m`/`c` keyboard shortcuts, updated security table (NTFS junction, lstat TOCTOU, CJK truncation), updated file size limits.

**Tests:**
- 325 → 354 tests (+46 new, -3 redundant). 8 new test classes: `TestModelCode`, `TestCostThirds`, `TestGetPricing`, `TestCharWidth`, `TestIsSafeDir`, `TestEnsureDataDir`, `TestSessionAutoConnect`, `TestRenderPickerLimit`.
- Removed 3 redundant `*_positive` constant tests from `TestFixedRangeConstants`.
- Updated `TestRlsCheckWorker` and `TestRlsMaybeCheck` — removed all `_rls_fetching` assertions (variable removed).
- Updated `TestApplyUpdateAction` — tests now call `_apply_update_worker()` directly (synchronous) instead of the thread-spawning `_apply_update_action()`.

**Other:**
- VERSION bumped to `1.8.4`

## v1.8.3 — 2026-04-14

**Bug fixes:**
- Fixed `<synthetic>` internal model appearing in Token Stats — these are Claude Code internal entries with 0 tokens that inflated the Calls count
- Added short model ID mappings (`"haiku"`, `"sonnet"`, `"opus"`) to `_MODEL_NAMES` — some transcript entries use abbreviated IDs instead of full `claude-*` identifiers

**Other:**
- VERSION bumped to `1.8.3`

## v1.8.2 — 2026-04-14

**Bug fixes:**
- Fixed BRN and CST progress bar ceilings undersized for Opus 4.6 1M (Max 20 plan) — `BRN_MAX` raised from 1.0 to **2.0** $/min, `CST_MAX` raised from 50.0 to **200.0** $. Previous ceilings caused both bars to pin at 100% during normal usage on higher-tier models.
- Fixed `WARN_BRN` default too low for higher-tier models — raised from 0.50 to **1.00** $/min. Previous threshold triggered BRN smart warning constantly on Opus 4.6 1M.

**Docs:**
- README: updated BRN range (0-2.0 $/min), CST range (0-$200), `CLAUDE_WARN_BRN` default (1.00) in Features, Metrics table, and Configuration table
- README: added Known Limitations section — documents delayed metric refresh after context compaction (Claude Code protocol limitation)

**Other:**
- VERSION bumped to `1.8.2`

## v1.8.1 — 2026-04-13

**Features:**
- Auto-purge dead sessions — `.json` + `.jsonl` pairs older than 48h are automatically deleted from temp dir on session list refresh (`DEAD_SESSION_TTL` constant). Reserved files (`rls.json`, `stats.json`) are skipped.

**Bug fixes:**
- Fixed `_fit_buf_height` clip direction — legend/picker/stats modals now clip content from bottom (preserving header) instead of from top (losing header on small terminals)
- Removed competitor comparison table from legend overlay (belongs in README docs, not in the TUI)
- Fixed BRN unit inconsistency — `$/m` → `$/min` in statusline `seg_brn` and `collect_warnings` (consistent with dashboard)
- Fixed `f_tok` accepting negative token counts — now returns `"--"` (consistent with `f_cost`/`f_dur`)
- Fixed `render_frame` APR not clamped to 100% (statusline was clamped, dashboard was not)
- Fixed `DATA_DIR.mkdir()` in monitor.py missing `mode=0o700` — default permissions were world-readable on shared Unix systems
- Added symlink check on `DATA_DIR` in `list_sessions()` — rejects symlinked data directory (statusline.py already had this)
- Fixed stray backslash in README session detection description
- Fixed `_rls_fetching` race condition — now uses `threading.Lock` instead of bare boolean

**Refactor:**
- `VERSION_RE` regex deduplicated into `shared.py` — used by monitor.py and update.py (was defined 3 times)
- Removed unused imports: `E` from statusline.py, `C_WHT` and `M_*` aliases from tests.py
- Removed stale sync comments from statusline.py and monitor.py
- Cleaned up `_ANSI_RE`/`M_ANSI_RE` dual import in tests.py — single name throughout
- `update.py` `apply_update()` now captures and sanitizes git output via `_sanitize()` (was printing raw)

**Docs:**
- PROMO.md: updated LOC (1700 → 2400), test count (142 → 280), "zero dependencies" → "stdlib only"
- README: added token stats + update manager screenshots, fixed stray backslash, updated macOS CI status
- Added orphaned `cc-aio-mon-stats.png` and `cc-aio-mon-update.png` references to README

**CI:**
- Added macOS to test matrix (`macos-latest`, Python 3.12)

**Tests:**
- 278 → 280: added `test_dead_session_purged_after_48h`, `test_recent_session_not_purged`
- Updated `TestRlsCheckWorker` and `TestRlsMaybeCheck` for `threading.Lock` refactor

**Other:**
- VERSION bumped to `1.8.1`

## v1.8.0 — 2026-04-13

**Features:**
- RLS (release check) — background version check against GitHub once per hour. Shows green "Up to date" or blinking red "update available" in the dashboard. Uses daemon thread with 15s timeout, `GIT_TERMINAL_PROMPT=0`, spawn guard. Disable with `CC_AIO_MON_NO_UPDATE_CHECK=1`.
- Update manager modal (`u` key) — shows current vs remote version, new commits, changelog preview, safety warnings (dirty tree, wrong branch, diverged). Press `a` to apply `git pull --ff-only` with post-pull syntax verification.
- New spinners — braille dots for session status (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏), pulse dot for RLS (∙○●○)
- Keybinding changes: `t` = token usage stats (was `u`), `u` = update manager (new)
- Smart warnings (CTF/BRN) now blink and are visually separated from header
- monitor.py writes `rls.json` and `stats.json` to temp dir for cross-process state sharing
- Statusline segments streamlined: Model │ CTX │ 5HL │ 7DL │ CST │ BRN │ APR │ CHR — trailing segments drop on narrow terminals. No background padding (CC notifications share the row).


**Bug fixes:**
- Fixed inverted color logic in `_reset_color()` — reset countdown now shows green when close to reset (good) and red when far from reset (bad)
- Fixed `scan_transcript_stats` period filters (7d/30d) — cutoff used `time.monotonic()` instead of `time.time()`, causing filters to never exclude old data
- Fixed `calc_cross_session_costs` baseline bug — when all JSONL entries fall after day/week start (trimmed history), cost was overstated. Now uses first entry as baseline when no pre-cutoff entry exists.
- Fixed crash bug: `_update_result` lacked `global` declaration in `main()`, causing `UnboundLocalError` on first `u` → `a` keypress
- Fixed keybinding priority: modal-specific handlers (update/stats/legend) now checked before global handlers — prevents 't', 'l', 's' from bypassing modal close logic
- Removed 5HL/7DL header warnings — redundant with colored bars (red at >=80%), caused unexpected layout shifts. CTF and BRN warnings kept.
- Fixed phantom sessions — `rls.json` and `stats.json` no longer appear in session picker (`_RESERVED_FILES` filter)
- Fixed CTF warning showing `<0m` — now clamps to `<1m` minimum
- Fixed `seg_apr` in statusline exceeding 100% when `api_ms > dur_ms` — now clamped
- Fixed session picker auto-connect — now triggers with 1 active session regardless of stale session count (was requiring total sessions == 1)
- Fixed `_limit_color(pct)` called twice with same arg in 5HL/7DL render blocks
- Removed dead `k == "q"` checks in session picker (already handled by global quit handler)
- `update.py`: `check_clean()` now ignores untracked files (`-uno`) — previously blocked updates due to untracked screenshots etc.
- `update.py`: added post-pull syntax verification (`py_compile`) to catch broken updates early
- `update.py`: guarded module-level side effects (stdout replacement, VT enable) behind `main()` — safe to import without clobbering terminal state
- Legend: WEK description corrected
- Git error output now sanitized via `_sanitize()` before display
- Unix: temp directory permissions verified and enforced to `0o700` after creation
- `statusline.py`: `write_shared_state` now uses `_DATA_DIR` instead of recomputing path (test isolation fix)
- `statusline.py`: `seg_chr` threshold logic fixed — no overlapping color ranges with non-default WARN/CRIT values
- `statusline.py`: removed full-width background padding — CC notifications share the status line row

**Refactor:**
- Deduplicated `_SID_RE`, `_ANSI_RE`, `MAX_FILE_SIZE`, `DATA_DIR_NAME`, and all ANSI color constants into `shared.py` — single source of truth, imported by both `statusline.py` and `monitor.py`
- Removed unused statusline exports: `RB`, `EL`, `BG_BAR`, `_R` alias

**Security:**
- `_sanitize()` now strips Unicode bidirectional overrides (U+200E/F, U+202A-E, U+2066-69) in addition to C0/C1 controls
- Unix: symlink check on temp data directory — refuses to write if `_DATA_DIR` is a symlink
- `scan_transcript_stats` capped at 1000 files to prevent DoS via large transcript directories

**Tests:**
- Added 108 net new tests (181 → 278): `TestParseVersion`, `TestRlsBlink`, `TestRlsCache`, `TestRlsInDashboard`, `TestRlsCheckWorker`, `TestRlsMaybeCheck`, `TestUpdate`, `TestSpinSession`, `TestSpinRls`, `TestGitCmd`, `TestUpdateChecks`, `TestGetNewCommits`, `TestGetRemoteChangelogPreview`, `TestApplyUpdateAction`, `TestRenderUpdateModal`, `TestCpcBase`, `TestListSessions`, `TestLoadState`, `TestLoadHistory`, `TestRenderPicker`, `TestSegAprClamp`, `TestCollectWarningsCTFMin`, `TestSanitizeBidi`, `TestFormatterEdgeCases`, `TestReservedFiles`; 4 renamed, 1 removed
- Fixed `TestRlsMaybeCheck` CI flaky failure — `time.monotonic()` on fresh CI runners can be < `_RLS_TTL` (3600s), making `t=0.0` appear unexpired. Now uses relative monotonic time.

**Docs:**
- README: fixed 4 broken screenshot references, added DUR to Metrics at a Glance table, fixed smart warnings text, fixed session picker auto-connect description
- README: screenshots renamed to descriptive English names, clickable for fullsize view, added statusline screenshot
- CHANGELOG: fixed test count
- CLAUDE.md: updated `shared.py` description to reflect new shared constants and regexes

**Other:**
- VERSION bumped to `1.8.0`

## v1.7.0 — 2026-04-12

**Features:**
- Usage stats modal (`u` key) — per-model token breakdown (In/Out/Calls) with progress bars, overview metrics (SES, DAY, STK, LSS, TOP), period filter (All Time / 7 Days / 30 Days). Reads session transcripts from `~/.claude/projects/`
- New dashboard footer shortcut: `[u]us`
- Legend updated with USAGE STATS section documenting new metrics

**Bug fixes:**
- `_parse_ts` now handles negative UTC offsets (`-HH:MM`) on Python 3.8+
- Removed duplicate `sid_str` assignment in `render_frame`
- Removed duplicate `day` calculation in transcript scanner
- Cache TTL switched from `time.time()` to `time.monotonic()` for clock-jump robustness

**Tests:**
- Added 39 new tests (142 → 181): `TestParseTs`, `TestCalcStreaks`, `TestModelLabel`, `TestScanTranscriptStats`, `TestRenderStats`, `TestRenderLegend`, `TestRenderFrame`

**Other:**
- VERSION bumped to `1.7.0`

## v1.6.4 — 2026-04-12

**Refactor:**
- Renamed `rates.py` → `shared.py` — now contains all shared helpers (`_num`, `_sanitize`, `f_dur`, `f_tok`, `f_cost`, `calc_rates`) used by both `statusline.py` and `monitor.py`
- Removed duplicate function definitions from `statusline.py` and `monitor.py` — single source of truth in `shared.py`
- Updated all documentation, CI workflows, and templates to reference `shared.py`

**Other:**
- VERSION bumped to `1.6.4`

## v1.6.3 — 2026-04-12

**Bug fixes:**
- Fixed statusLine command in all setup guides — must be wrapped in `bash -c '...'` for external binaries to work (Claude Code does not capture stdout from direct `py`/`python3` invocations)
- Added "Statusline not appearing" troubleshooting entry to all platform setup guides with correct/wrong examples
- Fixed PowerShell helper snippet in `docs/setup-windows.md` to generate the `bash -c` wrapped command
- Fixed typo in `tests.py` — `TestCalcCrossSesionCosts` → `TestCalcCrossSessionCosts`

**Other:**
- VERSION bumped to `1.6.3`

## v1.6.2 — 2026-04-10

**Features:**
- `update.py` — self-update script. Read-only by default (status + CHANGELOG preview); `--apply` flag runs `git pull --ff-only`. Safety guards: dirty tree, wrong branch, detached HEAD, divergence, downgrade, Python version. Cross-platform (Windows/macOS/Linux), stdlib only.

**Security:**
- `bandit.yml` now installs Bandit via `pip install --require-hashes` against a generated `requirements-bandit.txt` covering all 14 transitive deps with SHA256 hashes from PyPI. Fixes OSSF Scorecard `Pinned-Dependencies` finding on the `pipCommand` check.

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
