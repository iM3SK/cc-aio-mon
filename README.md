# CC AIO MON

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue) ![License MIT](https://img.shields.io/badge/license-MIT-green) ![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey) ![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)

**Real-time terminal monitor for Claude Code** — context window, API rate limits, session costs, and burn rate. Zero dependencies, single-file Python, cross-platform.

<img src="screenshots/setup-full.png" width="720" alt="CC AIO MON v1.3 — Claude Code left, full dashboard right">

*Claude Code with statusline (left) + fullscreen TUI dashboard (right)*

<details>
<summary>More screenshots</summary>
<br>

<img src="screenshots/statusline.png" width="600" alt="Statusline — Opus 4.6 │ CST │ CTX │ 5HL │ 7DL │ DUR">

*Statusline — single line below Claude Code input, text-only format*

<img src="screenshots/setup-compact.png" width="720" alt="CC AIO MON v1.3 — Claude Code left, compact dashboard right">

*Claude Code with statusline (left) + compact dashboard (right)*

<img src="screenshots/dashboard-compact.png" width="360" alt="Dashboard compact view">

*Dashboard — compact view with all metrics*

<img src="screenshots/dashboard-full.png" width="360" alt="Dashboard full view">

*Dashboard — full view with expanded sections*

<img src="screenshots/session-picker.png" width="360" alt="Session picker — select from active Claude Code sessions">

*Session picker — shown on launch when multiple sessions detected*

<img src="screenshots/legend.png" width="360" alt="Legend overlay — abbreviation definitions for all metrics">

*Legend overlay — toggle with `l` key*

</details>

## Quick Start

**1. Download**

```bash
git clone https://github.com/iM3SK/cc-aio-mon.git
```

**2. Configure statusline** — add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python \"/path/to/cc-aio-mon/statusline.py\""
  }
}
```

**3. Launch the dashboard**

```bash
python cc-aio-mon/monitor.py
```

Two files, zero dependencies, no install step.

---

## Table of Contents

- [Why CC AIO MON?](#why-cc-aio-mon)
- [Features](#features)
- [Installation](#installation)
- [Usage](#usage)
- [Metrics Reference](#metrics-reference)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Troubleshooting](#troubleshooting)
- [Alternatives](#alternatives)
- [Contributing](#contributing)
- [License](#license)
- [Changelog](#changelog)

## Why CC AIO MON?

Claude Code is powerful but opaque about resource consumption. You can't see how much context you've used, how close you are to rate limits, or what a session costs — until it's too late. CC AIO MON solves this with the **most information-dense layout** of any Claude Code monitor — every metric visible at once, no scrolling, no tabs, no wasted space:

- **Context window filling up?** See exactly how much is used, the fill rate, and an ETA to 100%.
- **Rate limited?** Track 5-hour and 7-day quota consumption with countdown to reset.
- **Expensive session?** Watch real-time cost and burn rate ($ per minute).
- **Multiple sessions?** Auto-detect and switch between active Claude Code sessions.

## Features

- **Most compact monitor** — all critical metrics in one screen. No scrolling, no tabs, no wasted space.
- **Zero dependencies** — stdlib-only Python. No pip install, no venv, no node_modules.
- **Two-tier architecture** — lightweight statusline (updates on each Claude Code event) + fullscreen TUI dashboard.
- **Real-time metrics** — context window with token counts, API ratio, 5-hour and 7-day rate limits, cost, burn rate, context full ETA.
- **Cross-platform** — Windows (Terminal, PowerShell, Git Bash), macOS (Terminal, iTerm2), Linux.
- **Nord color palette** — truecolor ANSI output with consistent color-coded sections.
- **Responsive layout** — statusline drops segments to fit narrow terminals. Dashboard adapts to any terminal size with ANSI-aware truncation.
- **Multi-session support** — auto-detects active sessions. Numbered picker when multiple sessions are running.
- **Animated spinner** — braille animation in dashboard header shows the monitor is alive.
- **Stale detection** — session data older than 5 minutes resets all bars to zero and shows `STALE` in the header.
- **Security hardened** — path traversal prevention, C1 escape injection protection, atomic file reads/writes, file size limits.

## Installation

### 1. Download

```bash
git clone https://github.com/iM3SK/cc-aio-mon.git
cd cc-aio-mon
```

Or just download `statusline.py` and `monitor.py` — that's all you need.

### 2. Configure Claude Code

Add the statusline to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python \"/path/to/statusline.py\""
  }
}
```

On Windows, use forward slashes:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python \"C:/path/to/statusline.py\""
  }
}
```

### 3. (Optional) Shell alias

```bash
alias mon='python /path/to/monitor.py'
```

## Usage

### Statusline

Runs automatically on each Claude Code status update. Outputs a single colored line below the input area: model, cost, context %, rate limits, duration. Segments drop from right when the terminal is narrow.

### Dashboard

```bash
python monitor.py              # auto-detect session
python monitor.py --session ID # specific session
python monitor.py --list       # list active sessions
python monitor.py --refresh 1000  # custom refresh interval (ms, default 500)
```

### Session Picker

When multiple Claude Code sessions are running, the monitor shows an interactive session picker on launch. Press `1-9` to select a session. Sessions marked `(stale)` haven't received updates in over 5 minutes. With a single active session, the monitor connects automatically.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Force refresh data |
| `l` | Toggle legend overlay |
| `1-9` | Select session (picker) |

## Metrics Reference

### Statusline Segments

| Code | Color | Metric |
|------|-------|--------|
| (model) | white | Model display name |
| CST | cyan | Total session cost (USD) |
| CTX | cyan | Context Window — percentage and token count (used/total) |
| 5HL | yellow | 5-Hour Rate Limit — quota consumed in current 5-hour window |
| 7DL | green | 7-Day Rate Limit — quota consumed in current 7-day window |
| DUR | green | Session duration |

### Dashboard Metrics

| Code | Color | Metric |
|------|-------|--------|
| APR | green | API Ratio — time in API calls vs total session duration |
| DUR | green | Session duration (sub-stat under APR) |
| API | green | API time (sub-stat under APR) |
| CHR | white | Cache Hit Rate — cache reads vs total cache operations |
| c.r | green | Cache read tokens (sub-stat under CHR) |
| c.w | green | Cache write tokens (sub-stat under CHR) |
| CTX | cyan | Context Window — percentage and token count (used/total) |
| 5HL | yellow | 5-Hour Rate Limit — quota consumed in current 5-hour window |
| 7DL | green | 7-Day Rate Limit — quota consumed in current 7-day window |
| LNS | dim | Lines added (green) / removed (red) in session |
| CST | cyan | Total session cost (USD) |
| BRN | yellow | Cost burn rate ($ / min) |
| CTR | yellow | Context consumption rate (% / min) |
| CTF | red | Context Full ETA — predicted time to 100% |
| NOW | white | Current local time |
| UPD | green | Time since last data update |

### Color Thresholds

All progress bars use the same thresholds:

- **Green** (< 50%) — healthy, plenty of headroom
- **Yellow** (50-79%) — approaching limits
- **Red** (>= 80%) — critical, take action

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_STATUS_WARN` | `50` | Yellow threshold (%) |
| `CLAUDE_STATUS_CRIT` | `80` | Red threshold (%) |

```bash
export CLAUDE_STATUS_WARN=60
export CLAUDE_STATUS_CRIT=90
```

## How It Works

### Architecture

```
Claude Code ──stdin──> statusline.py ──> terminal (one-line status)
                            |
                            v
                    $TMPDIR/claude-aio-monitor/
                    ├── {session_id}.json    (current state, atomic write)
                    └── {session_id}.jsonl   (timestamped history)
                            |
                            v
                      monitor.py ──> terminal (fullscreen TUI)
```

1. **statusline.py** receives JSON from Claude Code via stdin on each status update.
2. Outputs a colored one-line summary to the terminal.
3. Writes session state atomically to a temp directory for the monitor.
4. Appends timestamped entries to a JSONL history file for burn rate calculation.
5. **monitor.py** polls the temp directory, renders a fullscreen dashboard with bars, stats, and computed metrics.

### IPC Details

- State files: atomic write via `NamedTemporaryFile` + `os.replace()` (no partial reads)
- History: append-only JSONL, auto-trimmed when file exceeds 1 MB (keeps last 1000 entries)
- Stale `.tmp` files older than 60 seconds cleaned up automatically
- Session detection: files older than 5 minutes marked as stale — all progress bars reset to zero, header shows `STALE`

### Security

| Measure | Protection |
|---------|------------|
| Session ID validation | Strict regex `[a-zA-Z0-9_-]{1,128}` prevents path traversal |
| Input sanitization | C0 and C1 control characters (`\x00–\x1f`, `\x7f–\x9f`) stripped from all JSON fields before terminal output |
| File size limits | JSON capped at 1 MB, JSONL at 10 MB — oversized files skipped |
| Atomic writes | Unpredictable temp filenames prevent symlink attacks |
| TOCTOU prevention | File reads use single open + bounded read instead of separate stat + read |
| Directory permissions | Temp directory created with `0o700` where supported |
| Graceful shutdown | SIGTERM handler + atexit ensure terminal state is always restored |
| Render isolation | Corrupted data caught per-frame — does not crash the TUI |

## Requirements

- **Python 3.8+** (stdlib only — no pip install needed)
- **Claude Code** with statusline support
- **Terminal with truecolor** — Windows Terminal, iTerm2, Alacritty, Kitty, most modern terminals
- **80 columns** minimum recommended

## Troubleshooting

**Monitor shows "Waiting for Claude Code session..."**
- Ensure Claude Code is running with an active session.
- Check that `statusLine.command` is configured in `~/.claude/settings.json`.
- Verify temp files exist: `%TEMP%/claude-aio-monitor/` (Windows) or `/tmp/claude-aio-monitor/` (macOS/Linux).

**Statusline not appearing**
- Verify the path in `statusLine.command` is correct and uses forward slashes.
- Test manually: `echo '{"context_window": {"used_percentage": 42}}' | python statusline.py`

**Garbled output / encoding errors on Windows**
- Run `chcp 65001` in your terminal for UTF-8 mode.
- Both scripts auto-detect and override stdout encoding, but the terminal must support UTF-8 fonts.

**Monitor not responding to keyboard**
- On Windows, the terminal window must have focus for `msvcrt.getch()` to work.
- Press `q` to quit, `Ctrl+C` as fallback.

## Alternatives

| Project | Approach | Limitation |
|---------|----------|------------|
| claude-monitor | Reads JSONL cost logs | Estimated data, not real-time |
| ccusage | CLI usage aggregator | Historical only, no live dashboard |
| ccstatusline | Status line script | No TUI, no multi-session |
| **CC AIO MON** | Official statusline JSON | Real-time, zero deps, most compact |

## Contributing

Contributions welcome. Please:

1. Keep zero-dependency — stdlib only, no pip packages.
2. Keep single-file — `statusline.py` and `monitor.py` should remain self-contained.
3. Test on Windows and at least one Unix platform.
4. Run `python -c "import py_compile; py_compile.compile('statusline.py', doraise=True); py_compile.compile('monitor.py', doraise=True)"` before submitting.

## License

MIT License. See [LICENSE](LICENSE) for details.

## Changelog

### v1.3 — 2026-04-08

**Statusline redesign:**
- Removed progress bars — text-only segments for maximum density
- Removed CHR, LNS, !200k segments — statusline now shows only: model, CST, CTX, 5HL, 7DL, DUR
- Shortened model name (dropped context size suffix)
- Separator changed from `─` to `│`
- Compact formatting (no space before `%`)
- All 6 segments fit in 80 columns (previously only 3 of 8 were visible)

**Bug fixes:**
- DUR segment label now bold (consistent with all other segment labels)
- Spinner comment corrected (50ms, not 80ms)
- Legend LNS color fixed to match render (dim, not green)
- `f_tok` and `f_dur` formatting synchronized between statusline and dashboard
- `used_percentage` no longer crashes on non-numeric values (safe coercion via `_num()`)
- Stale detection works correctly after session file disappears (uses `last_seen` timestamp)
- Session picker truncates long `cwd` paths to terminal width
- `truncate()` appends ANSI reset to prevent color bleed
- Dead `HISTORY_MAX_LINES` constant removed from trim logic

### v1.2 — 2026-04-08

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

### v1.1 — 2026-04-07

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

### v1.0 — 2026-04-07

- Initial release
- Statusline: Nord truecolor, 3-letter codes, enclosed bars, responsive segments
- Monitor: fullscreen TUI, 5 bar metrics (APR/CHR/CTX/5HL/7DL), stats, legend overlay
- Responsive resize with 50ms tick, empty line trimming
- IPC via atomic JSON + JSONL history, burn rate calculation
- Zero dependencies, cross-platform (Windows/macOS/Linux)
