# Changelog

## v1.15.0 — 2026-06-14

**Features — pricing coverage:**
- **Fast-mode pricing.** Cost estimates now distinguish fast-mode requests: a
  `pricing_fast` table adds Opus 4.8 ($10/$50), Opus 4.7 and Opus 4.6
  ($30/$150 each). `_get_pricing(model_id, speed)` returns the fast rates when
  `speed="fast"`; the transcript aggregator reads `usage.speed`
  (`monitor.py:_aggregate_session_cost`). The per-request CST modal stays on
  standard rates — the statusline `current_usage` payload carries no speed
  field (documented inline).
- **Claude Mythos 5 pricing entry** ($10/$50, code `MY 5`) added to the model
  table (`monitor.py:_MODELS`).

**Bug fixes — model-ID matching:**
- **Bare and dated model IDs now map to one pricing key.** A new
  `_model_base()` helper normalizes a model ID — strips the `[...]` suffix and a
  trailing `-YYYYMMDD` date — so the statusline (bare) and transcript (dated)
  forms resolve to the same `_MODELS` entry. Used by `_get_pricing`,
  `_model_label` and `_model_code`; replaces three duplicated `split("[")[0]`
  sites.
- **Dead Haiku keys corrected.** Haiku 4.5 is re-keyed to the bare
  `claude-haiku-4-5`, and the never-matching `claude-haiku-3-5` key becomes
  `claude-3-5-haiku` (the real ID is `claude-3-5-haiku-20241022`) — historical
  Haiku 3.5 transcripts now price correctly.

**Behavior — cost modal labels:**
- **Cost modal reflects the real context-window fields.** The header
  `SESSION TOTALS` becomes `CONTEXT WINDOW` and the `TIN:`/`TOT:` labels become
  `CIN:`/`COUT:`. As of Claude Code v2.1.132,
  `context_window.total_input_tokens`/`total_output_tokens` report the *current
  window*, not a cumulative session total.

**Tests:** 710 passing (+22).

## v1.14.0 — 2026-06-10

Cross-model review remediation: an independent multi-dimensional review of
v1.13.0; every finding was verified against the code before fixing.

**Features:**
- **Model table: Fable 5 and Opus 4.8 pricing entries** (`monitor.py:_MODELS`)
  — cost estimates for the newest models; the retired Haiku 3.5 entry stays
  for correct pricing of historical transcripts.
- **Gauge-ceiling env vars documented:** `CC_MON_BRN_MAX` / `CC_MON_CTR_MAX` /
  `CC_MON_CST_MAX` (docs/CONFIGURATION.md); FILE-IPC contract now names the
  model-ID field correctly (`model.id`, drives cost-estimate pricing).

**Security:**
- **Self-update origin pinning.** Both the `update.py` CLI and the TUI update
  modal verify `git remote get-url origin` against the canonical
  `iM3SK/cc-aio-mon` repo before any `git pull`
  (`shared.verify_origin_remote`) — a rewritten origin can no longer feed the
  self-updater foreign code. Forks: set the new **`CC_AIO_MON_REMOTE`** env
  var to your remote URL (see docs/CONFIGURATION.md).
- **TUI apply now enforces the CLI safety guards.** `_apply_update_worker`
  re-runs the checks (branch, clean tree, divergence, pinned origin) and
  blocks the pull on any warning; the modal's `apply (risky)` action became
  `apply (blocked by warnings above)`. Previously the warning list was
  advisory-only and `a` pulled anyway.
- **git resolved to an absolute path at import** (`shared.run_git`). A bare
  `"git"` argv was re-resolved at every call — on Windows CreateProcess even
  consults the CWD, so a `git.exe` planted in the repo root could win.
- **TOCTOU guard in `_aggregate_session_cost`** (CWE-367): fstat identity
  check after open, same pattern `_scan_ai_title` already used.
- **Junction-safe subagents scan.** `_subagents_dir_for` and the `workflows/`
  subdir check now use `shared.is_safe_dir`, which rejects NTFS junctions —
  the previous `S_ISLNK`/`is_symlink()` tests missed them, allowing a junction
  to point the scan outside `~/.claude/projects`.

**Bug fixes:**
- **Malformed transcript records can no longer crash the monitor.** A JSONL
  record whose `message` is a *string* containing the substring "usage" made
  `msg.get()` raise `AttributeError` past the OSError-only handler, killing
  the stats modal and the monitor. Both transcript aggregators now type-check
  `message` / `usage` / `model` and skip bad records.
- **Timestamps parse timezone-aware.** `_parse_ts` used to strip `Z`/offsets
  and parse the wall-clock as *local* time, shifting cutoff filters and daily
  aggregation by the local UTC offset. Now `Z` maps to `+00:00` and offsets
  are honoured (naive fallback kept for unparseable shapes).
- **History trim can no longer lose a concurrent append.** The statusline
  history append and the read→rewrite trim are serialized via a
  `<sid>.jsonl.lock` sidecar (new `shared.lock_file_handle` /
  `unlock_file_handle`, fcntl/msvcrt); the trim also drops malformed JSON
  lines, matching what the FILE-IPC contract already promised.
- **Fail-fast when the data dir is unusable.** The interactive monitor and
  `update.py --apply` exit with a clear error instead of silently proceeding
  without the singleton lock (IPC-contract violation: racing instances /
  file replacement against a live monitor).
- **`load_state()` rejects reserved SIDs** (`rls`/`stats`/`pulse`) per the
  FILE-IPC contract, matching `list_sessions` / `load_history`.
- **Windows HANDLE truncation fixed in monitor and update.**
  `GetStdHandle`/`GetConsoleMode`/`SetConsoleMode` get explicit
  `restype`/`argtypes` (a pointer-sized HANDLE was truncated to c_int on
  64-bit Windows); a failed VT enable in `update.py` now disables colors
  instead of spraying raw escape sequences.
- **`_window_buf` off-by-one at `rows=1`** — emitted 2 lines into a 1-row
  terminal; now exactly one.

**Docs & tests:**
- FILE-IPC contract corrections: `pulse.jsonl` is persistence-only (the modal
  renders from in-memory `get_pulse_snapshot()`, not the file); snapshots
  tagged with a newer `_schema_version` *are* gated by `load_state()`; the
  new history lock sidecar is documented.
- `CC_AIO_MON_REMOTE` documented in docs/CONFIGURATION.md; new shared helpers
  added to the CONTRIBUTING.md SSoT list.
- **+10 tests (688 total):** aggregator type guards, origin pinning (4 cases),
  blocked TUI apply, trim malformed-line drop, `rows=1` window, reserved-SID
  snapshots, absolute git argv, lock round-trip; the `_env_pct` test no
  longer leaks env state between tests.

## v1.13.0 — 2026-06-04

**Features:**
- **Agents fan-out modal (`a`).** A live view of the subagents / Workflow agents
  spawned by the watched session: active/total count, summed token usage, and a
  per-agent list (id, tokens, last tool) sorted by recency, refreshed off-thread
  while the modal is open. Press `f` to toggle an **active-only** filter that
  hides idle agents (a non-destructive "clear board" — nothing on disk is
  touched). Reads each agent's transcript under
  `~/.claude/projects/<proj>/<session>/subagents/agent-*.jsonl` (lazy,
  TTL-cached, containment-checked, with symlink/non-regular-leaf rejection and
  file-count/size DoS caps).
- **Scrollable detail modals.** Token stats, agents, legend, cost, pulse, update
  and menu modals now scroll when they outgrow the terminal — mouse wheel
  (alternate-scroll mode), arrows, `j`/`k`, `PgUp`/`PgDn`, `Home`/`End` — with a
  pinned header (the title never scrolls away) and a scroll-position hint
  (visible range + direction arrows). Replaces the old clip-from-bottom that
  made content unreachable on short terminals.

**Bug fixes (audit remediation):**
- **Escape-sequence parser no longer leaks bytes.** A split/bursty escape
  sequence is buffered across reads instead of surfacing its bytes as
  modal-closing key presses; SS3 arrows (`ESC O A/B`) are mapped; SGR mouse
  reports are dropped cleanly; and an X10 mouse report (`ESC [ M` + 3 coordinate
  bytes) is now fully consumed instead of leaking its coordinates as keystrokes
  (`monitor.py:_resolve_esc`).
- **Period-scoped token stats exclude records with no parseable timestamp.** A
  `ts==0` record could slip into the 7d/30d model totals even though the
  per-day aggregation beside it already excluded it
  (`monitor.py:_aggregate_transcript`).
- **A corrupt/huge transcript timestamp no longer aborts aggregation.**
  `datetime.fromtimestamp()` on an out-of-range epoch raises `OverflowError`
  (not `OSError`) — it is now caught locally so one bad record skips only its
  own day attribution instead of discarding the rest of the transcript.
- **Burn/context rate is computed over the true time span.** `calc_rates()` now
  orders history by timestamp before reading endpoints, so a wall-clock step
  backwards (NTP adjustment between snapshots) can't silently skew the rate
  (`shared.calc_rates`).
- **Cost-window baseline ignores unplaceable entries.** `_baseline_delta()`
  skips entries with a missing/invalid `t` rather than treating them as a
  pre-cutoff baseline (`monitor.py:_baseline_delta`).
- **Self-update integrity check survives Ctrl-C / SIGTERM.** Exit paths now join
  an in-flight update worker for a bounded moment so its post-pull syntax check
  finishes; the git pull is atomic and the join is time-capped, so quitting
  stays prompt even if the pull hangs (`monitor.py:_join_update_worker`).
- **Statusline survives a broken pipe.** If Claude Code closes the statusline
  pipe early, `print()`'s `BrokenPipeError` is swallowed instead of crashing the
  subprocess with a traceback (`statusline.py`).
- **Short-terminal modal overflow.** `_window_buf` caps the pinned header so the
  emitted header + window + indicator never exceeds the terminal height.

**UI:**
- The dashboard footer now hints the entry-point keys — `[m] menu` `[l] legend`
  `[q] quit` — and the menu (`m`) gains a **Navigation** section listing the
  scroll keys, so the controls are discoverable without opening the legend.
- The session-status spinner moves to the **start** of the `Session Active` /
  `Session Inactive` line (was mid-line) so it reads cleaner.
- The agents modal shows **compact 4-letter tool codes** (e.g. `READ`, `BASH`,
  `WFCH`; long `mcp__server__method` names collapse to the method's first four
  letters) so each agent row stays short.

**Quality / perf:**
- **Cross-session cost aggregation moved off the render thread.** `TDY`/`WEK`
  was recomputed inline in `render_frame` every 30 s — a glob + per-line JSONL
  re-parse of every session's history that stalled the 50 ms loop, most visible
  right after waking from stale (largest transcripts + active interaction). It
  now runs in a `cost-scan` daemon (`_cost_refresh_async`); the dashboard reads
  the last cached value and never blocks. Resolves the deferred P1-4.
- **Token-stats scan moved off the render thread.** Re-opening the token-stats
  modal past its 30 s cache TTL used to re-scan and parse every transcript
  synchronously (~0.6 s freeze). Now only the first open per period scans
  synchronously; later opens read `_usage_cache` and refresh in a `stats-scan`
  daemon (`_stats_refresh_async`).
- `truncate()` gains a fast path for plain ASCII lines that already fit (skips
  the per-char width scan in the 20 Hz render hot loop).
- The dashboard model badge uses `shared.badge_context_suffix()` instead of a
  hardcoded `(1M context)` literal, so any context-window unit is compacted.
- Session-picker ordering is extracted to `_picker_order()`, shared by
  `render_picker` and the digit-selection in `main()` — removes the risk that
  the two hand-synced sort+slice copies desync and `[3]` selects the wrong
  session.
- The render path resolves the subagents dir once per tick instead of three
  times.

**Architecture:**
- **ADR-002 resolved (variant C).** The 3500-LOC tripwire on `monitor.py` first
  fired with this batch. Rather than split an event-loop module across files
  (higher risk; erodes the "5 stdlib files, no pip" product constraint), the
  module keeps its single-file shape, gains an explicit section index in its
  docstring, and the `test_debt016` discussion trigger is raised to 3800.

**Tests:** 678 passing (+57) — escape parser (X10 mouse, split sequences),
scroll window clamps, subagents scan, timestamp robustness, `calc_rates`
ordering, bounded update-worker join, async cost-scan refresh,
`atomic_write_text` cleanup, and `update.main()` flow-control exit codes.

## v1.12.6 — 2026-06-03

**Bug fixes:**
- **Terminal-width detection works on 64-bit Windows.** `statusline.py`'s
  last-resort width probe (opening `CONOUT$` directly when stdout is piped)
  called `CreateFileW` without declaring its ctypes signature. Foreign
  functions default to a 32-bit `c_int` return type, so the pointer-sized
  console HANDLE was truncated and `GetConsoleScreenBufferInfo` always failed —
  the branch silently fell back to a fixed width of 120. The handle types are
  now declared (`restype`/`argtypes` = `c_void_p`) and checked against
  `INVALID_HANDLE_VALUE` (`statusline.py:_get_terminal_width`).
- **Console code page is restored correctly on Windows.** `_set_console_utf8()`
  runs unconditionally before the `--list` branch and saved the original output
  code page; interactive setup then called `_setup_term()`, which saved it again
  — by then already `65001`, so the user's locale code page (e.g. CP1250) was
  lost and the console was left in UTF-8 after quitting. Both saves are now
  idempotent (`monitor.py:_set_console_utf8`, `_setup_term`).

**CI / tooling:**
- **Dependabot can update the bandit lockfile again.** `requirements-bandit.txt`
  was a flat `--require-hashes` file that mixed the direct `bandit[sarif]` pin
  with its transitive deps, so Dependabot failed weekly trying to bump a
  transitive package (`jsonpickle`) in isolation. It is now generated from
  `requirements-bandit.in` via `pip-compile --generate-hashes`; Dependabot
  recognises the header and regenerates the whole hash tree. Hash pinning is
  preserved.

**Tests:** 621 passing (+4) — behavioural coverage for two safety guards
(`check_syntax_after_pull` post-update integrity check; `write_shared_state`
snapshot/history alignment).

## v1.12.5 — 2026-05-30

**Bug fixes:**
- **Pulse "PULSE ERROR" no longer shows stale "all green" data.** When the
  background Pulse worker hits an unexpected exception, its last-resort handler
  now clears the volatile snapshot fields (incidents, components, latency,
  scores, indicator) instead of leaving values from the last healthy fetch in
  place. A crash during a real outage no longer renders an error verdict next
  to stale "operational" incidents/components (`pulse.py:_crash_snapshot`).
- **Self-update can no longer be interrupted mid-pull.** Pressing `q` while a
  self-update is applying is now ignored until the worker finishes its
  post-pull syntax check, so quitting can't kill the daemon after
  `git pull --ff-only` has rewritten files but before the integrity check runs
  (`monitor.py` event loop + `_apply_update_action`).

**Security hardening:**
- **IPC schema gate.** `monitor.load_state()` now refuses a session snapshot
  tagged with a newer `_schema_version` than the running build understands
  (degrades to "no data") rather than risk misreading an incompatible shape;
  missing or older tags stay readable (`monitor.py:load_state`).

**Tests:** 617 passing (+6).

## v1.12.4 — 2026-05-30

**Bug fixes:**
- **`calc_rates` no longer crashes the render loop on an explicit JSON `null`
  `cost` / `context_window`.** `shared.calc_rates` read these via
  `.get(key, {})`, which only substitutes the default for a *missing* key — an
  explicit `"cost": null` / `"context_window": null` in a corrupt or
  hand-edited `.jsonl` reached `None.get(...)` and raised `AttributeError`
  inside `monitor.main()`'s per-frame render. The lookups now coalesce with
  `or {}`, so an explicit null degrades to the same default-zero path as a
  missing key (`shared.py:calc_rates`).
- **Monitor render loop now also catches `AttributeError`.** The per-frame
  `except` in `monitor.main()` adds `AttributeError` to the existing
  `TypeError`/`ValueError`/`KeyError`/… set, so any future null-shape
  regression degrades to a counted render error instead of tearing down the
  alt-screen TUI.

**Tests:** 611 passing (+3).

## v1.12.3 — 2026-05-25

Second audit-cleanup release. Closes the remaining actionable P2/P3
findings from the 24.05.2026 audit; the rest stay DEFER with explicit
triggers (per the audit's own "Nepriorita" / "OK ako je" verdicts on
those items). No new features, no user-visible behavior change.

**Tests:**
- **Vague test names renamed (T-P2-3).** 13 occurrences of `test_basic`
  / `test_success` / `test_failure` across `test_update.py`,
  `test_statusline.py`, and `test_monitor.py` are now
  `test_<subject>_<condition>_<expectation>` per the project convention
  (e.g. `TestGitCmd.test_success` → `test_returns_stripped_stdout_on_zero_rc`).
  Failure messages are now self-describing.
- **Defensive `tearDown` for module-level cache state (T-P2-5).**
  `TestAggregateSessionCost` and `TestAggregateSessionCostSecurity` now
  explicitly clear `_SESSION_COST_CACHE` in `tearDown` in addition to
  `setUp`. Closes the audit-noted "drobné riziko" where a mid-test
  failure could leak cache entries into the next class in the same
  process.
- **`pulse._atomic_replace_log` OSError cleanup test (P3-4).** Pins
  that a simulated `Path.replace` failure swallows the exception
  (best-effort contract) and unlinks the `.tmp` file so it doesn't
  leak into `DATA_DIR`.
- **`is_safe_dir` Windows junction mock tests (P3-3).** Two new tests
  that mock `st_file_attributes` to exercise the Windows-only reparse-
  point branch (`shared.py:269-274`) cross-platform: one verifies a
  junction is rejected, the complement verifies a real directory
  without the bit is accepted. Previously only native symlinks were
  exercised, leaving the junction code path unverified on CI.

**Code hygiene:**
- **`r` / `w` → `cr_inc` / `cw_inc` in `_aggregate_session_cost`
  (P3 STYLE-002).** The single-letter loop locals visually collided
  with the module-level `R = "\033[0m"` ANSI reset. Renamed to
  `in_inc` / `out_inc` / `cr_inc` / `cw_inc` to make the per-record
  semantics explicit.

**Documentation:**
- **Type hints on `shared.py` public API (P3 DOC-001).** Added
  annotations to the helpers the audit specifically called out as
  "would improve IDE autocomplete for contributors":
  `safe_read`, `run_git`, `acquire_singleton_lock`, `load_history`,
  `calc_rates`, plus `extract_changelog_entry`, `parse_ahead_behind`,
  `check_syntax_after_pull`, `rotate_crash_log`,
  `strip_context_suffix`, `compact_context_suffix`. Imports
  `Optional` / `Tuple` / `List` / `Iterable` / `IO` from `typing`
  (Python 3.8 compatible — no PEP 585 `list[dict]` syntax).
  No `mypy --strict` in CI; the annotations are documentary.

**Tests:** 608 passing (+3 from this batch).

## v1.12.2 — 2026-05-25

Audit-cleanup release: outstanding P2 + P3 findings from the 24.05.2026
five-dimension audit (REPORT.md backed up to `D:\backups\audit-24.05.2026-cc-aio-mon.zip`).
No new features, no user-visible behavior change beyond two niche edges
(crash log always-rotates, `pulse.indicator_label` is now a public name).

**Reliability:**
- **Crash log always rotates (P3 ARCH).** `rotate_crash_log` gains an
  `always=False` parameter; `monitor.py:_install_crash_logger` passes
  `always=True` so the previous traceback is preserved (as `.log.1`) even
  when both crashes are well under the 1 MB size threshold. Prevents
  silent loss of diagnostics when two crashes happen in quick succession.
  +2 regression tests.

**API tidy-up:**
- **`pulse.indicator_label` is public (P3 ARCH).** Renamed from
  `_indicator_label` because `monitor.render_pulse_modal` was already
  reaching across module boundaries to call it. The mapping
  (Statuspage indicator → human label) is a stable contract; the leading
  underscore was misleading.

**Code hygiene — internal:**
- **Named time constants (P3 STYLE-003).** `shared.SECONDS_1H` /
  `SECONDS_5H` / `SECONDS_1D` / `SECONDS_7D` replace the bare `3600`
  / `18000` / `86400` / `604800` magic numbers in `monitor.py` (rate-limit
  windows, dead-snapshot cleanup, ago-time formatter, cross-session week
  cutoff) and `pulse.py` (`LOG_AGE_CUTOFF`). No behavior change.
- **`HISTORY_RATE_SAMPLES` constant (P3 ARCH).** The magic `n=120` default
  in `load_history` (≈ a 2-hour rolling window at one statusline event per
  minute) is now a named constant in `shared.py`, propagated to the
  `monitor.load_history` and `statusline._load_history_for_rates` wrappers.
- **`_MODELS` defined in one place (P3 ARCH).** The placeholder
  `_MODELS = {}` at the top of the cost-breakdown section plus the
  `_MODELS.update({...})` call 900 LOC later in the token-stats section
  are gone — the model registry is now a single dict literal next to
  `_DEFAULT_PRICING` where it is first used.
- **`flush(cols)` is now a required arg (P3 ARCH).** The `cols=None`
  default that fell back to `shutil.get_terminal_size()` was unreachable
  in production (every call site already had the value); removed the dead
  branch.
- **`isinstance(..., bool)` exclusion (P3 STYLE-005).** `pulse.py`
  `_classify_incident` now explicitly excludes `bool` from its
  `isinstance(impact_override, (str, int, float))` check — `bool` is an
  `int` subclass, so `True` / `False` would otherwise leak through into
  the model-family string match.
- **`# noqa: BLE001` on `_rls_maybe_check` swallow (P3 STYLE-004).** The
  best-effort `except Exception` on `Thread().start()` failure is
  intentional; the comment makes that explicit.

**Security — defense in depth:**
- **`.gitignore` extended (P3 SEC).** Added `id_ecdsa*`, `.ssh/`,
  `known_hosts`, `service-account*.json`, `.netrc`, `*.kdbx`, and
  `.aws/credentials` to the credentials block.

**Tests:**
- **`extract_changelog_entry` direct unit tests (P3 TEST).** Six new
  cases in `test_shared.py`: middle entry extraction, last-entry-at-EOF
  anchor, missing version, empty input, `max_lines` truncation, and
  regex-metachar escaping in the version string.
- **`strip_context_suffix` / `compact_context_suffix` direct tests
  (P3 TEST).** Eight cases in `test_shared.py` covering 1M / 200k
  suffixes, no-suffix passthrough, and empty input for both helpers.
- **`update.check_python_version` test (P3 TEST).** Three cases pinning
  the exit-on-too-old behavior, the at-minimum no-op, and the
  newer-version no-op — using a `namedtuple` stand-in for the
  non-constructible `sys.version_info` structseq.
- **`safe_read` `PermissionError` path (P3 TEST).** Pins that any
  `OSError`, not just `FileNotFoundError`, returns `None` cleanly.
- **`TestApplyUpdateAction.test_syntax_check_uses_safe_read` no longer
  patches `pathlib.Path.exists` globally (P2 T-P2-4).** Uses a real
  tmpdir + real stub file, so unrelated `Path.exists` calls in the
  test body are unaffected.
- **`TestCalcRates.test_shared_module_identity` renamed (P2 T-P2-7)**
  to `test_monitor_and_statusline_alias_shared_calc_rates` with a
  docstring stating the SSoT invariant it guards.

**Tests:** 605 passing (+20).

## v1.12.1 — 2026-05-25

**Bug fixes — Windows / non-UTF-8 locale rendering:**
- **Diacritic mojibake fix for `statusline.py` stdin (NEW-002).** `statusline.py:main` previously called `sys.stdin.read()`, which on non-UTF-8 Windows locales (CP1250 on SK, CP1252 on US, CP852 on legacy DOS-derived) used the locale codec to decode the bytes Claude Code emits on stdin. Slovak / Czech / Polish session names and `aiTitle` strings were mangled (`Kompletný` → `KompletnĂ˝`, `Vytvoriť` → `VytvoriĹĄ`) before `json.loads` ever ran, then persisted into the IPC snapshot — so the `monitor.py` NEW-001 console code-page fix in v1.12.0 alone could not recover them. `statusline.py` now reads stdin at the byte level (`sys.stdin.buffer.read()`) and decodes UTF-8 explicitly. End-to-end UTF-8 pipeline restored without any user-side `PYTHONUTF8=1` requirement. New regression test `TestStatuslineMainE2E::test_main_utf8_session_name_preserved_through_pipeline` pins the round-trip.
- **Diacritic mojibake fix for `monitor.py --list` non-interactive output (NEW-003).** The one-shot `--list` mode bailed before `_setup_term()` (which only runs for the interactive TUI), so it never switched the Windows console output code page to 65001. Python wrote correct UTF-8 bytes but the console reinterpreted them through the locale CP, mangling diacritics in the listing even though the underlying snapshot file was correct. Added a slim `_set_console_utf8()` helper (Windows: `SetConsoleOutputCP(65001)`; Unix: no-op) and `main()` now calls it for every entry point before any print. Discovered during self-audit of the v1.12.1 batch; same UTF-8 hardening category as NEW-001 / NEW-002 so it ships together.

**Hardening — defence in depth:**
- **TOCTOU mitigation on transcript reads (S-P2-2, CWE-367).** `_scan_ai_title` and `scan_transcript_stats` now compare `os.fstat(fh.fileno()).st_ino + st_dev` against the pre-open `lstat` after each `open()`. A symlink or hard-link flip between path resolution and read no longer lets a different inode slip through; the file is skipped instead.
- **UID-ownership guard on the data dir (S-P2-1, CWE-377/732).** `shared.ensure_data_dir` now refuses to use `$TMPDIR/claude-aio-monitor/` on Unix if `st_uid` differs from `os.geteuid()`. Defeats the predictable-temp-path pre-create attack on multi-user hosts. Guarded by `hasattr(os, "geteuid")` so Windows behaviour is unchanged.
- **Windows: explicit exit on missing ANSI / VT support (A-P2-4).** `monitor.py:_setup_term` no longer silently falls through when `SetConsoleMode` rejects `ENABLE_VIRTUAL_TERMINAL_PROCESSING` (pre-Win10 conhost). Users now get a clear diagnostic listing workarounds (Windows Terminal, ConEmu, Cmder, mintty, or a Windows upgrade) instead of a TUI of raw escape sequences.

**Performance — render-path discipline:**
- **Release check moved out of `render_frame` (A-P2-1).** `_rls_maybe_check` is now invoked from the main event loop, not the render function. Per-frame trigger is gone (the helper was internally rate-limited, but the call itself paid a Python function-call cost on every 50 ms tick). Test render assertions are now deterministic without requiring `CC_AIO_MON_NO_UPDATE_CHECK=1` in each setUp.
- **Pulse opener no longer process-global (A-P2-2).** `pulse.py` used `urllib.request.install_opener(...)` to scrub `HTTP(S)_PROXY` env vars from its fetches — but the install was process-global, surprising any other module that touched urllib. Replaced with a module-local `_OPENER` used directly by `_fetch_summary` / `_ping_api`. The env-scrub guarantee is unchanged; the cross-module side effect is gone.

**Refactor — readability:**
- **Rate-limit rendering DRY'd up (SIZE-002).** `5HL` and `7DL` blocks in `render_frame` were byte-for-byte identical except for data key, label, and window length. Both call sites now go through a single closure `_render_rate_limit(data_obj, label, window_sec)` colocated with its only caller.
- **`scan_transcript_stats` split into three pieces (SIZE-003).** The 148-LOC monolith is now an orchestrator + `_iter_safe_transcripts` (containment + symlink + size + cutoff filter, yields `None` as truncation sentinel) + `_aggregate_transcript` (per-file parse + in-place aggregate). Security-sensitive path resolution and data-aggregation arithmetic are independently testable.
- **`update.py` ANSI palette exception documented (DUP-002).** The basic 16-color palette in `update.py` looks like a duplicate of `shared.C_*` Nord truecolor, but it is intentional: `update.py` runs before any TUI / VT enablement and must remain readable on legacy consoles without 24-bit truecolor. Both palettes are pinned with a comment block and a `CONTRIBUTING.md` "What to keep in sync" bullet.
- **Platform detection unified (DRY-001).** `platform.system() == "Windows"` → `sys.platform == "win32"` in `monitor.py` and `statusline.py`; `import platform` removed from both. DEBT-014 regression tests guard `signal` / `subprocess` / `bisect` / `traceback` module-level binding, **not** `platform`, so the unification is safe.
- **Test helpers consolidated (T-P2-2).** `tests/_helpers.py::_strip_ansi` now uses `shared._ANSI_RE` (same canonical pattern as `_vlen`). The local `_ANSI_STRIP_RE` duplicate and its `re` import are gone — the shared pattern covers OSC sequences too, so coverage strictly increases.

**Documentation:**
- **`docs/CONFIGURATION.md` added (A-P2-3).** Authoritative catalog of every environment variable the app reads (`CC_AIO_MON_NO_UPDATE_CHECK`, `CC_AIO_MON_NO_PULSE`, `CLAUDE_STATUS_WARN` / `_CRIT`, `CLAUDE_WARN_BRN`, `TERM`, `COLUMNS`, `TMPDIR` / `TMP` / `TEMP`, `HOME` / `USERPROFILE`, `PYTHONUTF8`, `PYTHONIOENCODING`) with type, default, read site, effect, and the naming convention for new vars (`CC_AIO_MON_*` prefix).

**Tests:**
- **`time.time()` mocked in flaky countdown tests (T-P2-1).** `TestSeg5hl` / `TestSeg7dl::test_future_resets_shows_countdown` and `TestStartupCleanup::test_cleanup_drops_old_entries` now freeze the clock via `patch("statusline.time.time", ...)` / `patch("pulse.time.time", ...)` so test setup and code under test observe the same epoch. Eliminates sub-second race that could flip `"2h"` → `"1h 59m"` on slow CI.
- **`datetime.now()` mocked in `TestCalcStreaks` (T-P2-6).** Class-level `setUp` patches `monitor.datetime` to a frozen `2026-05-25 12:00:00`, with `.strptime` delegated to the real implementation. Streak arithmetic is now deterministic regardless of when the suite runs.

**Tests:** 585 passing (+2).

## v1.12.0 — 2026-05-25

**New features — operational reliability:**
- **Singleton lock for `monitor.py`.** The interactive dashboard now acquires an exclusive lock on `$TMPDIR/claude-aio-monitor/monitor.lock` at startup; running a second `monitor.py` instance exits with a clear error instead of racing the first on snapshot polling and crash-log writes. The `--list` mode is exempt (one-shot, non-interactive). Cross-platform via `fcntl.flock` (Unix) and `msvcrt.locking` (Windows); the OS releases the lock on process exit, including hard kills.
- **Crash-log rotation.** `monitor-crash.log` is rotated to `monitor-crash.log.1` once it grows past 1 MB, preventing unbounded growth across repeated crash cycles. Best-effort: any rotation failure is silently swallowed so a broken filesystem cannot prevent crash recording.
- **File-IPC schema version.** Statusline snapshots and JSONL history entries now carry a `_schema_version: 1` field. Monitor tolerates the new field because all snapshot reads use `dict.get(...)` for known keys and never enumerate the whole dict — it lays the groundwork for non-backward-compatible JSON shape changes in future releases.

**Bug fixes — Windows console rendering:**
- **Diacritic mojibake fixed (no more `chcp 65001` required).** On Windows locales whose default console output code page isn't UTF-8 (e.g. CP1250 on Slovak, CP1252 on US, CP852 on legacy DOS-derived locales), Python's UTF-8 byte stream was being interpreted by the console as the locale code page, mangling session names, AI titles, and model labels containing diacritics (`Vytvoriť` rendered as `VytvoriĹĄ`). `monitor.py:_setup_term` now calls `kernel32.SetConsoleOutputCP(65001)` alongside the existing `SetConsoleMode` ANSI/VT enablement, and `_restore_term` (already registered via `atexit.register(cleanup)`) restores the original code page on exit so the user's shell isn't left in UTF-8 mode after monitor quits. No user-side `chcp 65001` workaround needed; the troubleshooting line in `docs/setup-windows.md` has been updated accordingly.

**Performance — render path freezes eliminated:**
- **Update modal no longer freezes the keyboard.** `render_update_modal` previously issued 5 synchronous `git` subprocess calls per 50 ms render tick (`_get_new_commits`, `_get_remote_changelog_preview`, and 3× `_update_checks`), each with a 15 s timeout — worst-case 75 s frozen TUI on a slow filesystem or network. Helpers now go through TTL-cached wrappers (`_cached_get_new_commits`, `_cached_get_remote_changelog_preview`, `_cached_update_checks`) keyed by remote version with a 30 s TTL. `_apply_update_worker` invokes `_invalidate_update_modal_cache` on successful `git pull` so post-update state is reflected on the next render.
- **Session picker CPU drop from ~10 % to ~1 %.** Picker mode (`sid is None`) renders at 20 Hz; previously each tick triggered a full `DATA_DIR.glob()` + per-session JSON parse + AI-title extraction via `list_sessions()`. New `cached_list_sessions(ttl=1.0)` (mirrors the existing `cached_cross_session_costs` pattern) caches the result for 1 s — visually fresh (sessions don't appear/disappear faster than ~1 Hz) but with ~20× fewer disk operations. The direct `list_sessions()` call site for the one-shot CLI `--list` mode is unchanged.
- **AI-title scan 8× faster on cache miss.** `_AI_TITLE_SCAN_BYTES` lowered from 512 KiB to 64 KiB; Claude Code writes the `ai-title` record within the first ~20 transcript entries (<50 KiB), so the previous cap was over-provisioned.

**Refactor — single source of truth:**
- The byte-for-byte duplicate post-pull syntax-check loop in `monitor.py:_apply_update_worker` and `update.py:apply_update` has been extracted to `shared.check_syntax_after_pull(repo_root)`. Both call sites now delegate to the shared helper, and a regression-guard test enforces that the loop never reappears in either consumer.
- The `git rev-list --left-right --count` parser used by both `monitor._update_checks` and `update.get_ahead_behind` is now `shared.parse_ahead_behind(output)`. Same canonical `(ahead, behind)` return; `update.py` swaps locally to preserve its `(behind, ahead)` public contract.
- Cross-session cost aggregation in `calc_cross_session_costs` had two identical baseline-subtraction loops (one for today, one for the week, differing only in the cutoff timestamp). Extracted into `_baseline_delta(entries, cutoff_ts)` — net −15 LOC and a uniform cutoff semantics for any future windowing.

**Repository structure — developer experience:**
- The monolithic `tests.py` (5 701 LOC, 110 test classes) has been split into a `tests/` package — one module per source file (`test_statusline.py`, `test_monitor.py`, `test_shared.py`, `test_pulse.py`, `test_update.py`). The root-level `tests.py` is now a thin wrapper that runs `unittest discover tests/`, so existing `py tests.py` invocations continue to work unchanged.

**Security hardening — pre-push hook patterns extended:**
- `.githooks/pre-push` now blocks pushes containing additional credential prefixes: GitHub server/user/OAuth/refresh tokens (`ghs_`, `ghu_`, `gho_`, `ghr_`) alongside the existing `ghp_`/`github_pat_`; AWS STS temporary keys (`ASIA*`) alongside `AKIA*`; Google API keys (`AIza...`); Slack tokens (`xox[baprs]-...`); Stripe live/test keys (`sk_live_`/`sk_test_`/`rk_live_`/`pk_live_`). Hook remains best-effort (`--no-verify` still bypasses).

**Documentation:**
- New `docs/ARCHITECTURE.md` — contributor-oriented architecture overview (two-process model, five modules, data flow Mermaid, threading model, "where to look for X" pointer table).
- New `docs/FILE-IPC-CONTRACT.md` — exhaustive reference for the statusline ↔ monitor file-IPC contract: full schemas for `<sid>.json`, `<sid>.jsonl`, `pulse.jsonl`, `monitor-crash.log`, `monitor.lock`, plus atomicity guarantees, session ID validation rules, and the `_schema_version` evolution policy.
- New `docs/RELEASE.md` — release process checklist: SemVer policy with worked examples, CHANGELOG entry format derived from this changelog's actual entries, pre-release verification steps, tag + push order, self-update integration constraints, rollback procedure.
- Updated `.github/SECURITY.md` — appended v1.12.0 primitives (singleton lock, crash-log rotation, schema_version tag, deduplicated post-pull syntax check).
- Updated `CONTRIBUTING.md` — documents the new `tests/` package layout, 583-test baseline, no-parallel-implementations policy enforced by regression-guard test, file-IPC schema bump procedure, and architecture-orientation pointer for new contributors.
- Updated `README.md` — added v1.12.0 user-facing notes (singleton lock UX, crash-log rotation, schema versioning) and a Documentation index linking to the new architecture / IPC / release / security references.

**Internal — test suite DRY:**
- Extracted `_vlen`, `_strip_ansi`, `_full_data`, `_write_session`, `_write_transcript`, and `_make_assistant_record` helper duplicates (originally cloned into two test files by the v1.12.0 monolithic-tests split) into a single `tests/_helpers.py`. Net 95 LOC removed from `tests/test_statusline.py` + `tests/test_monitor.py` (one helper pair was unexpectedly duplicated _twice_ in `test_monitor.py` — both inline copies removed).

**Post-release audit follow-ups (same v1.12.0 cycle):**
- `update.py --apply` now acquires the same singleton lock as `monitor.py` before any rollback tag or `git pull`. Prevents concurrent CLI updates from racing the running TUI on `.py` file replacement. Bails out with a friendly error and `sys.exit(1)` if monitor.py is already running. Closes a security audit gap where `.github/SECURITY.md` claimed lock coverage that did not match update.py reality.
- Added direct test coverage for v1.12.0 primitives that had only indirect (regression-guard) coverage: `TestParseAheadBehind` (6 methods covering malformed input), `TestAcquireSingletonLock` (3 methods covering happy path, contention, missing dir), `TestRotateCrashLog` (4 methods covering boundary, missing path, .1 pre-existing), `TestMainSingleton` (3 methods covering monitor.main exit on lock contention), and a `_schema_version` field assertion in `TestWriteSharedState`. Plus `TestApplyUpdateSingletonLock` validating the new update.py lock behavior.

**Second-pass audit (2026-05-22):**
- `TestParseAheadBehind` extended to 8 methods: added `test_tab_separated_valid` (tab-delimited ahead/behind is valid input) and `test_negative_integer_rejected` (negative integers must be rejected as malformed).
- `TestMainSingleton` extended to 4 methods: added `test_main_list_mode_skips_singleton_lock` (list-mode flag bypasses lock acquisition entirely).
- `TestApplyUpdateSingletonLock` extended to 2 methods: added `test_apply_update_proceeds_when_lock_acquired` (happy-path lock acquisition in update.py --apply).
- `TestPrePushHook` skip guard added: test skips when `.githooks/pre-push` is absent (covers Windows and repos without the hook installed) instead of failing.
- `update.py --apply` now emits a stderr warning (`"lock dir unavailable; proceeding without singleton guard"`) when the data dir cannot be created (`ensure_data_dir` returns False), instead of silently proceeding without the singleton lock. Surfaces a previously invisible gap in lock coverage that `.github/SECURITY.md` did not document.

**Third-pass audit (2026-05-25) — regression-guard coverage for bug fixes & perf:**
- `tests/test_shared.py::TestLoadHistory` — `RESERVED_SIDS` guard (rls/stats/pulse cannot be read via session history API) and `UnicodeDecodeError` branch (binary garbage in a `.jsonl` returns `[]` rather than raising). Previous test suite had neither path.
- `tests/test_shared.py::TestEnvPct` — 5 cases for `_env_pct` (valid float, integer string, empty, invalid, missing env var) which is the single source of truth for `WARN_PCT` / `CRIT_PCT` threshold parsing.
- `tests/test_shared.py::TestEnsureUtf8Stdout` — positive path (already utf-8 → no-op) and reconfigure path (non-utf-8 → reopen stdout with `encoding="utf-8"`, `errors="replace"`, `closefd=False`).
- `tests/test_shared.py::TestAcquireSingletonLockCrossPlatform` — both `fcntl`/`msvcrt` branches now exercised on any host via mocked `sys.platform`, not only the runner's native platform.
- `tests/test_statusline.py::TestStatuslineMainE2E` — end-to-end smoke for `statusline.main()` (stdin → JSON → `build_line` → `write_shared_state` → stdout) plus empty-stdin and invalid-JSON early-return paths.
- `tests/test_statusline.py::TestIPCForwardCompatNoSchemaVersion` — `load_state` tolerates pre-v1.10 snapshots that lack `_schema_version` (the contract is documented in `docs/FILE-IPC-CONTRACT.md` but had no test pinning it).
- `tests/test_pulse.py::TestWorkerLoopCrashRecovery` — `_worker_loop`'s `except Exception` last-resort guard writes a `worker crashed` snapshot rather than letting the daemon thread die silently.
- `tests/test_monitor.py::TestAuditRegressionV1105::test_debt016_monitor_loc_tripwire` — DEBT-016 trigger: `monitor.py` exceeding 3 500 LOC fails the test with a pointer to `PROJECTS/cc-aio-mon/ROZHODNUTIA.md` ADR-002, requiring an ADR before further growth.

**Tests:** 583 passing (+41).

## v1.11.1 — 2026-05-06

**Security hardening:**
- Session cost aggregation now validates the `~/.claude/projects/` root itself before accepting `transcript_path` values. A symlinked or junctioned projects root is rejected instead of only checking containment against its resolved target.
- The dashboard's `ai-title` scanner now respects the shared 50 MiB transcript size cap even when the title appears at the start of an oversized transcript.
- The in-app update worker now uses the same bounded `safe_read(..., MAX_FILE_SIZE)` syntax-check path as `update.py --apply`, preventing unbounded reads of post-pull Python files.

**Repository safety:**
- The pre-push hook now scans the pushed tip tree for brand-new remote branches. First pushes no longer bypass the secret/sensitive-file scan on a clean worktree.

**Documentation:**
- Local agent guidance was synced back to Claude Code terminology and paths so future workspace sessions do not confuse this project with a Codex monitor.
- README and security policy wording now document the stricter transcript-root validation and bounded source-file checks.

**Tests:** 542 passing (+3).

## v1.11.0 — 2026-05-05

**New features — surface fresh data from Claude Code:**
- **AI-generated session titles in the picker and dashboard header.** Claude Code writes auto-generated titles into transcript JSONL (`{"type":"ai-title","aiTitle":"..."}`). The session picker (`s`) and dashboard session label now show that title instead of the bare UUID when no `session_name` is set. CLI `--list` also benefits. Fallback chain: `session_name` → `ai_title` → `id[:N]`.
- **Lifetime activity panel in the token-stats modal (`t`).** A new section reads `~/.claude/stats-cache.json` (CC's pre-aggregated lifetime stats) and renders: total sessions / messages / tool-call count, first session date, longest session duration, a 24-hour heatmap of activity (UTC), and the last 5 daily activity rows. The panel auto-collapses on small terminals (DAILY first, then the whole block) so it never clips the rest of the modal. The cached date is shown in the panel header.
- **Server-side tool calls and 1h/5m cache split in the cost-breakdown modal (`c`).** The session aggregator now counts `web_search_requests`, `web_fetch_requests` (`server_tool_use`) and `ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens` (`cache_creation`). The cost modal shows new `WSR/WFR` and `TIE/T5M` rows in `SESSION TOTALS` only when the values are non-zero — old transcripts and zero-tool sessions render unchanged.

**Tests:** 539 passing (+38).

## v1.10.6 — 2026-04-22

**Statusline — reset countdown color:**
- Removed ANSI faint (`\033[2m`) from the 5HL and 7DL reset countdown. The reset time (`→ 2h 58m`, `→ 1d 22h`) now renders in the same color as the percentage — yellow below the warn threshold, red at or above the critical threshold. Previously the countdown was intentionally dimmed to signal supplementary information; the effect rendered inconsistently across terminals and made the countdown harder to read.

**Bug fixes:**
- **Release check / self-update.** Since v1.10.2 the `VERSION` constant moved into `shared.py`, but the release-check worker in `monitor.py` and `update.py --apply` both still greped `monitor.py` for it. Result: the dashboard permanently showed `error` in the release indicator, and every `py update.py --apply` invocation raised `RuntimeError: VERSION constant not found in monitor.py` before doing anything. Both paths now read from `shared.py`.
- **Dashboard crash on null payload fields.** A Claude Code status JSON with `"model": null`, `"context_window": null`, or `"cost": null` (rare but syntactically valid) raised `AttributeError` inside `render_frame` / `scan_transcript_stats`. `AttributeError` was not in the render loop's except allow-list, so the dashboard crashed via the excepthook and `monitor-crash.log`. Sixteen call sites switched from the `data.get("k", {})` default to the `data.get("k") or {}` pattern, which treats explicit `null` the same as a missing key.

## v1.10.5 — 2026-04-19

**Security hardening:**
- Transcript scanner (`scan_transcript_stats`) now resolves `~/.claude/projects/` to a canonical root once per scan and rejects symlinked `.jsonl` files that escape it. Aligns with the existing hardening on `transcript_path` from statusline JSON.
- `update.py` enforces a 1 MB size cap on every local file it reads (version lookup + post-pull syntax check) via `shared.safe_read`. A malformed or oversized `PY_FILES` member now raises a clear "too large" error instead of triggering unbounded memory reads during the update flow.
- `pulse.py` scrubs `HTTP_PROXY` / `HTTPS_PROXY` env vars at import time — Anthropic Pulse fetches (`status.claude.com`, `api.anthropic.com`) can no longer be silently routed through an attacker-controlled intermediary. Mirrors the env-whitelist rationale already applied to `shared.run_git`.

**Documentation:**
- README security table now documents Windows reserved device name rejection (`CON`/`PRN`/`AUX`/`NUL`/`COM0-9`/`LPT0-9`, case-insensitive) — the protection has been present since v1.9.1 but was undocumented on the user-facing side.
- README Known Limitations: added a **color accessibility caveat** (Nord red/green pair is not colorblind-safe; every metric carries redundant text labels + numeric values so color is supplementary signal) and a **local-timezone contract note** (rollback tags, daily aggregation, pulse JSONL timestamps all use machine local time, not UTC).

## v1.10.4 — 2026-04-19

Internal license hygiene release; no user-visible changes.

## v1.10.3 — 2026-04-19

**Windows startup hotfix:**
- `monitor.py` on Windows crashed immediately at startup with `UnboundLocalError` for the `signal` module. The alt-screen buffer swallowed the traceback, so the failure appeared silent. Fixed by keeping `signal` as a module-level import and guarding SIGPIPE registration with `hasattr(signal, "SIGPIPE")`.

**New diagnostic:**
- Any uncaught exception in `monitor.main()` is now written to `$TMPDIR/claude-aio-monitor/monitor-crash.log` with full traceback, platform, Python version, and encoding details. Startup crashes no longer vanish when the terminal restores its normal buffer.

**Tests:** 490 passing.

## v1.10.2 — 2026-04-18

**Security:**
- New `shared.safe_read(path, max_bytes)` primitive — bounded read returning `bytes` or `None` on error or overflow. Used across cross-session cost aggregation, history trim, and pulse log cleanup, closing several `stat()`→`read()` race windows.
- `monitor.py --session <value>` now sanitizes the value before echoing it in an "Invalid session ID" error (ANSI-escape hardening).
- `update.py` sanitizes the post-pull `VERSION` string before displaying it.

**Consistency:**
- `VERSION` is now declared once in `shared.py`. `monitor.py` and `pulse.py` both import it — `pulse.USER_AGENT` always tracks the current release.
- `PY_FILES` tuple (all source files) lives in `shared.py`. The in-app post-update syntax check and `update.py --apply` both iterate the shared constant, so modules can't be omitted from one side.

**Tests:** 486 passing.

## v1.10.1 — 2026-04-18

**Security:**
- `update.py` sanitizes exception text in the "Could not verify new VERSION" warning before writing to the terminal.
- `scan_transcript_stats()` and session cost aggregation validate `~/.claude/projects/` through `is_safe_dir()` — symlinks and Windows junction points are now rejected.
- `list_sessions()` uses a bounded read for session snapshots, aligning with `load_state()`.

**Correctness:**
- Pulse worker reverts its startup flag if thread creation fails, so a subsequent call can retry instead of being stuck in permanent "AWAITING DATA".
- `_ping_api()` wraps `urlopen()` in a `with`-block to prevent socket leaks on async exceptions.
- `monitor.py --list` installs a SIGPIPE handler on non-Windows, matching `statusline.py` and `update.py`. Piping `--list` to `head` or `less` no longer prints a `BrokenPipeError` traceback.
- `_setup_term()` checks the `SetConsoleMode` return value and falls through cleanly on pre-Win10 or unsupported handles.

**Tests:** 474 passing.

## v1.10.0 — 2026-04-18

**Statusline — reset countdown in rate-limit segments:**
- 5HL and 7DL segments now show a dimmed reset countdown next to the percentage: `5HL 42% → 2h 15m`, `7DL 30% → 6d 12h`. Tells you at a glance how long until the rate-limit window resets.
- Countdown renders via ANSI faint (SGR `\033[2m`) so the percentage remains the primary signal and the reset time reads as supplementary information.
- The `f_cd()` formatter is shared between the statusline and the dashboard, so both surfaces display reset countdowns in the same format.
- The arrow is only rendered when `resets_at` is in the future. Expired or absent values fall back to the previous display (no arrow).

**Statusline — layout change:**
- The `APR` (API-time ratio) and `CHR` (cache-hit ratio) segments are removed from the statusline to free horizontal space for the reset countdown. Both metrics remain fully available in the fullscreen dashboard (`py monitor.py`).
- Segment order (left → right): Model │ CTX │ 5HL │ 7DL │ CST │ BRN. Trailing segments still drop from the right when the terminal is too narrow.
- Net width change: roughly −8 chars overall. The statusline fits into 80-column terminals more comfortably than before.

**Tests:** 470 passing.

**Credit:** reset countdown idea originally proposed by @digizensk.

## v1.9.1 — 2026-04-17

**Security hardening:**
- Hourly background release check now uses the same minimal git env whitelist as update.py (blocks `GIT_SSH_COMMAND` / `LD_PRELOAD` / proxy env injection). Previously this one path inherited the full parent environment, contradicting the project's documented subprocess policy.
- Session IDs matching Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM0-9`, `LPT0-9`) are now rejected case-insensitively at validation time. Prevents accidentally opening the console/printer device instead of a file on Windows. Valid SIDs like `Conrad`, `console`, `COM10` are unaffected.
- Added `.gitattributes` to enforce LF line endings for shell/Python/Markdown files and CRLF for PowerShell. Stops cross-platform line-ending pollution when contributors on Windows commit with `core.autocrlf=false`.

**Resource management:**
- Per-session cost cache is now bounded (LRU cap at 64 entries). Long-running monitor processes that observe many rotating session IDs no longer accumulate memory indefinitely.
- Release-check cache reads are now atomic across threads — the render loop always sees a coherent snapshot of status + remote version + timestamp, even if the background worker updates mid-read.

**Cross-platform polish:**
- `statusline.py` and `update.py` now handle SIGPIPE gracefully on Unix — piping output to `head` or `less` exits silently instead of dumping a BrokenPipeError traceback. Windows is unaffected (SIGPIPE doesn't exist there).
- Pulse module's HTTP User-Agent now tracks the project version automatically instead of staying pinned to `1.0`. Anthropic server-side logs see the real client version.

**Internal cleanup:**
- Removed deprecated `_RESERVED_FILES` / `_RESERVED_SIDS` aliases — all call sites use the single `RESERVED_SIDS` constant from `shared.py`.
- Extracted the 50 MiB transcript size cap into a named constant (`TRANSCRIPT_MAX_BYTES`) so the two call sites in `monitor.py` can't drift apart.
- Release-check and update-apply git calls are now routed through a single entry point (`run_git`), making both the production code and its tests easier to reason about.
- `.github/SECURITY.md` User-Agent wording synced with actual UA value.

**CI:**
- Python test matrix on Ubuntu now covers 3.8, 3.10, 3.11, 3.12 (previously only 3.8 + 3.12). Windows and macOS runners stay on 3.12. Catches regressions on intermediate Python versions.

**Tests:**
- Added regression coverage for every fix above:
  - Windows reserved-name rejection (uppercase / lowercase / mixed-case / prefixes-still-allowed edge cases)
  - LRU cache eviction behavior (populates cap+5 entries, asserts oldest evicted, recently-touched keys survive)
  - SIGPIPE handler installation on Unix (skipped on Windows)
  - Release-check worker delegation to `run_git` (asserts exactly 2 calls: fetch + show, and no direct `subprocess.run`)
  - `_rls_snapshot` / `_rls_write` helper correctness
- Full suite: 477 tests, all passing (one skipped on Windows — the Unix-only SIGPIPE test).

## v1.9.0 — 2026-04-17

**New feature — Anthropic Pulse modal:**
- New `pulse.py` module — real-time Anthropic backend stability monitor. Press `p` to open modal. Stdlib only, zero dependencies, zero token cost, cross-platform.
- **Stability score (0-100)** with weighted composite: status indicator (50%), active incidents (30%), API latency (20%). Verdict bands: ≥80 `SAFE TO CODE` (green), 50-79 `DEGRADED` (yellow), <50 `NOT SAFE TO CODE` (red), error states render in dim.
- **Two-tier signal** — (1) passive: `status.claude.com/api/v2/summary.json` for indicator + components + incidents; (2) active: HTTPS GET to `api.anthropic.com/v1/messages` measures real TLS handshake + HTTP round-trip (any HTTP status = endpoint alive, only network/TLS errors = down).
- **Rolling median smoothing** — `deque(maxlen=10)` keeps last 10 scores; verdict derived from median of last 5. Absorbs single-sample outliers (one slow probe doesn't flip verdict). Warm-up below 3 samples passes raw through.
- **Latency percentiles** — p50 / p95 over up to 60 recent successful probes (~30 min window) displayed in modal when ≥3 samples available.
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

**Correctness + scaling fixes (post-release follow-up, same release):**
- **Pricing table rewritten** — `_MODEL_PRICING` aligned with official Anthropic rates (platform.claude.com/pricing, 2026-04). Opus 4.6 corrected from $15/$75 to $5/$25 per 1M tokens (was 3x over-reported). Haiku 4.5 corrected from $0.80/$4 to $1.00/$5. Added entries for Opus 4.7, Opus 4.5, Opus 4.1, Sonnet 4.5, Haiku 3.5. `_DEFAULT_PRICING` changed from Opus-tier to Sonnet-tier (less alarming when unknown model appears).
- **Model name/code maps extended** — added Opus 4.7 / 4.5 / 4.1, Sonnet 4.5, Haiku 3.5. New `_MODEL_ID_RE` regex enables dynamic family+version extraction for future models (`claude-opus-5-0` → `Opus 5.0` / `OP 5.0`) without code changes. Session picker now uses regex extraction instead of hardcoded map iteration.
- **Transcript scanner includes cache tokens** — `scan_transcript_stats` now sums `cache_read_input_tokens` + `cache_creation_input_tokens`. Previously under-reported usage by 20-80% depending on cache hit ratio. Token Stats modal now shows `CRD:` (cache read) and `CWR:` (cache write) rows per model and in the ALL totals line when non-zero.
- **Bar ceilings raised for heavy/API users** — defaults tuned for 24/7 Opus API coding:
  - `BRN_MAX` 2.0 → 10.0 $/min (env: `CC_MON_BRN_MAX`)
  - `CST_MAX` 200 → 1000 $ (env: `CC_MON_CST_MAX`)
  - `CTR_MAX` 5.0 → 10.0 %/min (env: `CC_MON_CTR_MAX`)
  - `WARN_BRN` 1.0 → 3.0 $/min (env: `CLAUDE_WARN_BRN`, unchanged name)
  Previously pinned to 100% during normal Opus sessions, destroying signal. All configurable via env vars so power users can tune.
- **WARN/CRIT thresholds unified across statusline + monitor** — monitor.py hardcoded `50` / `80` literals in `mkbar()`, `_limit_color()`, and CTX warn line replaced with `WARN_PCT` / `CRIT_PCT` constants derived from `CLAUDE_STATUS_WARN` / `CLAUDE_STATUS_CRIT` env vars (single source of truth; previously only statusline honored them).
- **Session picker "?" artifacts filtered** — snapshots without `model.display_name` skipped (test artifacts / incomplete writes). Orphans older than 1h auto-deleted alongside their `.jsonl` twin.
- **Pulse source updated** — `SUMMARY_URL` now points at `status.claude.com` (canonical) instead of deprecated `status.anthropic.com` (which served redirects). Incident→model tagging prefers `incidents[].components[]` array (canonical Statuspage schema) with regex-on-title as legacy fallback.
- **Audit cleanup** — `_RESERVED_FILES` extended to include `pulse` (prevents session-name collision with pulse.jsonl). `DATA_DIR` consolidated into `shared.py` (removed 3x duplication). Dead code removed: `_write_shared_stats`, `rls.json` writer (no consumer). Alias `_VERSION_RE` removed. Dead constant `H = "\u2500"` removed. `statistics.median` used for p50 (was index-based).
- **Test suite** — 441 tests, all passing (up from 421; +20 new behaviors covered, net +20 after removals).

**Modal UX fixes (post-release follow-up, same release):**
- **Token Stats bar** — now counts `input + output + cache_read + cache_write` for model percentage (was only input+output). Fixes mis-sized bars for cache-heavy models. `daily_tokens` aggregation in overview also updated — TOP day peak now reflects full token volume. Helper `_total_tokens(m)` added to monitor.py.
- **Cost Breakdown modal — LAST REQUEST + SESSION BREAKDOWN split:**
  - Section "TOKEN COSTS (est.)" renamed to **"LAST REQUEST (est.)"** — clarifies it shows last-message tokens only (from `current_usage`).
  - New **"SESSION BREAKDOWN (est.)"** section — aggregates entire session from transcript JSONL (via `transcript_path` in statusline JSON or `~/.claude/projects/` fallback), applies per-record model pricing, reconciles with server-reported CST (warn tag if estimate delta >15%). Answers "where did my session's $X actually go".
  - Cache: 5-second TTL (`_SESSION_COST_CACHE`), 50 MB transcript cap, `_SID_RE` validation on path. Helper `_aggregate_session_cost(data)` in monitor.py.
- **Pulse modal UX** — component names now adaptive-width (`SW - 16` instead of hardcoded 20 chars), parenthetical suffixes stripped (`Claude API (api.anthropic.com)` → `Claude API`), components flush-left aligned with rest of modal, footer falls back to short form (`source: status.claude.com + api ping`) when terminal is narrow.
- **Update modal** — added "Checked Xm ago" freshness indicator (shows age of last release check in s/m/h, based on monotonic timestamp from `_rls_cache["t"]`), plus a cyan `github.com/iM3SK/cc-aio-mon` link in the info block above the separator. Plain text (OSC 8 hyperlinks removed — caused width truncation issues in narrow modals).
- **Legend** — new sub-labels explain LAST REQUEST vs SESSION BREAKDOWN scope, plus SUM reconciliation warn tag. Bar-range comments in `mkbar` render block updated from obsolete fixed values to reflect that BRN/CTR/CST scale dynamically to their respective `*_MAX` constants.
- **Test suite: 462 tests, all passing** (up from 441).

**Consistency sweep (post-release follow-up, same release):**
- **Dead code pruned** — removed unused `import tempfile` from `monitor.py`, unused `ensure_data_dir` import from `monitor.py`, `DATA_DIR_NAME` import from `monitor.py` / `statusline.py` / `pulse.py`. Dead function `_tag_incident_models` removed from `pulse.py` (superseded by `_tag_models_from_incident` earlier in v1.9.0). Dead helper `vlen` removed from `monitor.py` (never called in production; visible-length logic lives in `truncate`).
- **Shared helpers consolidated** — `RESERVED_SIDS` (frozenset) moved to `shared.py` (was `_RESERVED_FILES` in monitor + `_RESERVED_SIDS` in statusline; both now alias `shared.RESERVED_SIDS`). `strip_context_suffix` / `compact_context_suffix` helpers introduced in `shared.py` — replace 3 divergent inline regex/replace patterns across monitor + statusline. `run_git` subprocess wrapper lives in `shared.py` (update + monitor both delegate to it). `extract_changelog_entry` single-source in `shared.py` (monitor + update both use it).
- **Update modal UI polish** — `[a] apply` bracket pattern now consistent across all 4 states (highlighted white key letter, dim brackets). `REM unknown` label aligned with the known-version color pattern.
- **Named semantic constant** — `RESET_HALFWAY_PCT = 50.0` introduced in `monitor.py` for `_reset_color` (distinct from `WARN_PCT`/`CRIT_PCT`).
- **Docs accuracy** — README env-var table: `CLAUDE_STATUS_WARN`/`CLAUDE_STATUS_CRIT` scope corrected to "statusline + dashboard". Env-var prefix convention documented. `update.py` module header converted from `#` comments to docstring.
- **Test suite: 452 tests, all passing** (net -10: removed 3 `TestVlen` + 7 `TestPulseModelTagging._tag_incident_models` tests alongside the dead code).

**Security hardening (post-release follow-up, same release):**
- **Transcript path containment** — `_aggregate_session_cost()` now validates `transcript_path` from statusline JSON: must be a regular file (not symlink) inside `~/.claude/projects/`. Rejects absolute paths outside root, `..` traversal, symlink redirects. New helper `_safe_transcript_path()`. Prevents hostile JSON payloads from forcing the monitor to read arbitrary files (up to 50 MB every 5 s) for existence probing / DoS.
- **ANSI injection via unknown model IDs** — `_model_code()` now sanitizes the 3-char fallback for unknown model strings pulled from `~/.claude/projects/**/*.jsonl`. Control characters in a transcript's `message.model` field can no longer reach the terminal.
- **Subprocess env whitelist** — `shared.run_git()` passes a minimal env (PATH, HOME/USERPROFILE, SYSTEMROOT, TEMP/TMP, LANG/LC_*, APPDATA/LOCALAPPDATA, GIT_TERMINAL_PROMPT=0) instead of the full parent environment. Blocks pre-injected `GIT_SSH_COMMAND`, `LD_PRELOAD`, `HTTP(S)_PROXY`, `GIT_EXEC_PATH` from reaching git during release checks. Defense-in-depth (attacker already owns env), but removes a semi-persistent trigger surface.
- **Test suite: 459 tests, all passing** (+7 security tests: transcript path traversal, symlink rejection, non-string input, ANSI injection in model code, git env whitelist × 2).

**Update reliability (post-release follow-up, same release):**
- **Pre-update rollback tag** — `update.py --apply` now creates `pre-update-YYYYMMDD-HHMMSS` git tag before running `git pull`. If the pull or post-pull syntax check fails, the user is shown a `git reset --hard pre-update-*` hint for instant recovery. Tag creation failure is non-fatal (warns, continues).
- **`pulse.py` added to post-pull syntax check** — was missing from `py_files` list, meaning a syntax error introduced in `pulse.py` by an update wouldn't be caught until monitor crashed at startup. Now verified alongside monitor/statusline/shared/update.
- **Test suite: 461 tests, all passing** (+2 regression guards for the rollback tag format and pulse.py inclusion).

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
- `docs/setup-macos.md`: removed stale "not included in CI" note (macOS CI added in v1.8.1).
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
