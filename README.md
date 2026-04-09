# CC AIO MON — Claude Code Terminal Monitor

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue) ![License MIT](https://img.shields.io/badge/license-MIT-green) ![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen) ![Tests](https://github.com/iM3SK/cc-aio-mon/workflows/Tests/badge.svg) ![CodeQL](https://github.com/iM3SK/cc-aio-mon/actions/workflows/github-code-scanning/codeql/badge.svg) ![Scorecard](https://github.com/iM3SK/cc-aio-mon/workflows/Scorecard%20supply-chain%20security/badge.svg) ![Bandit](https://github.com/iM3SK/cc-aio-mon/workflows/Bandit%20Security%20Scan/badge.svg)

**Real-time terminal monitor for Claude Code CLI.** Track context window usage, API rate limits, session costs, burn rate, and cache performance — all in one compact TUI dashboard. Zero dependencies, stdlib-only Python, cross-platform.

> **How it works:** Claude Code pipes session telemetry as JSON to `statusline.py` via **stdin** on every status update (~300ms debounce). The script parses the JSON, renders a one-line ANSI status bar in the terminal, and writes the data to `$TMPDIR/claude-aio-monitor/` as atomic JSON snapshots + append-only JSONL history. A separate `monitor.py` process polls these temp files and renders a fullscreen TUI dashboard. Both scripts share `rates.py` for burn rate ($/min) and context rate (%/min) calculation. **Three Python files, zero dependencies, no build step, no install step.**

| | |
|---|---|
| **Input** | Claude Code `statusLine` JSON protocol via stdin — model info, context window, rate limits, cost, token counts, session metadata |
| **Output** | ANSI truecolor terminal — one-line statusline bar + fullscreen TUI dashboard with progress bars, smart alerts, cross-session cost aggregation |
| **Data flow** | `Claude Code → stdin JSON → statusline.py → temp files → monitor.py → TUI` |
| **Files** | `statusline.py` (statusline renderer + IPC writer), `monitor.py` (TUI dashboard), `rates.py` (shared rate math) |
| **IPC** | Atomic JSON snapshots + JSONL history in `$TMPDIR/claude-aio-monitor/` — no sockets, no databases |

<img src="screenshots/cc-aio-mon-dashboard.png" alt="CC AIO MON v1.6.0 — fullscreen TUI dashboard showing context window, API ratio, cache hit rate, rate limits, burn rate, cost, and cross-session totals with Nord color scheme">

## Why CC AIO MON?

| Project | Data source | Limitation |
|---------|-------------|------------|
| claude-monitor | Reads JSONL cost logs | Estimated data, not real-time |
| ccusage | CLI usage aggregator | Historical only, no live view |
| ccstatusline | Status line script | No TUI, no multi-session |
| **CC AIO MON** | **Official stdin JSON protocol** | **Real-time, zero deps, most complete** |

Other monitors scrape log files or estimate costs from token counts. CC AIO MON reads the **official Claude Code statusline JSON** — the same data Claude Code uses internally. No estimation, no guessing, no stale logs.

<img src="screenshots/cc-aio-mon-statusline.png" alt="CC AIO MON v1.6.0 — one-line ANSI status bar showing model, APR, CTX, CHR, 5HL, 7DL, BRN, CTR, CTF, CST, DUR, NOW segments with Nord truecolor palette">

<img src="screenshots/cc-aio-mon-legend.png" alt="CC AIO MON v1.6.0 — legend overlay showing all metric codes, descriptions, and fixed ranges for BRN CTR CST">

## Quick Start

**1. Clone**

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

On Windows, use forward slashes: `"python \"C:/path/to/statusline.py\""`

**3. Launch the dashboard**

```bash
python cc-aio-mon/monitor.py
```

Three files, zero dependencies, no install step. Optionally add a shell alias: `alias mon='python /path/to/monitor.py'`

## Features

- **Compact** — all critical metrics in one screen. No scrolling, no tabs, no wasted space.
- **Zero dependencies** — stdlib-only Python 3.8+. No pip install, no venv, no node_modules. Just copy three `.py` files.
- **Easy setup** — one line in `~/.claude/settings.json` + `python monitor.py`. Done.
- **Official stdin JSON** — reads Claude Code's `statusLine` JSON protocol via stdin. No log scraping, no file watching, no API polling. Real data, real-time.
- **Two-tier architecture** — `statusline.py` (one-line status bar, triggered per Claude Code event) + `monitor.py` (fullscreen TUI, polls temp files independently).
- **Temp file IPC** — atomic JSON snapshots + JSONL history in `$TMPDIR/claude-aio-monitor/`. No sockets, no databases, no shared memory. Works across terminal sessions.
- **Progress bars with fixed ranges** — BRN (0-1.0 $/min), CTR (0-5.0 %/min), CST (0-$50) plus standard 0-100% bars for APR, CHR, CTX, 5HL, 7DL.
- **Smart warnings** — header alerts when context fills in < 30 min, rate limits > 80%, or burn rate exceeds threshold.
- **Cross-session cost tracking** — TDY (today) and WEK (rolling 7-day) aggregate cost across all active Claude Code sessions.
- **Cross-platform** — Windows (Terminal, PowerShell, Git Bash), macOS (Terminal, iTerm2), Linux. CI-tested on Python 3.8 + 3.12, Ubuntu + Windows.
- **Nord truecolor palette** — ANSI 24-bit color with semantic grouping: green = performance, cyan = context, yellow = rate limits, orange = cost/finance, red = critical.
- **Responsive layout** — statusline drops right segments for narrow terminals. Dashboard compresses sections automatically.
- **Multi-session** — auto-detects sessions via temp files. Numbered picker for multiple sessions. Press `s` to switch anytime.
- **Stale detection** — sessions idle > 30 minutes get dimmed metrics with last known values preserved.
- **Security hardened** — session ID regex validation (`[a-zA-Z0-9_-]{1,128}`), C0/C1 control character sanitization, atomic writes via `NamedTemporaryFile`, file size limits (1MB JSON, 10MB JSONL).

## Metrics at a Glance

| Metric | What it shows | Range | Where |
|--------|--------------|-------|-------|
| **APR** | API time / total session time | 0-100% | statusline + dashboard |
| **CHR** | Cache read tokens / total cache | 0-100% | statusline + dashboard |
| **CTX** | Context window usage | 0-100% | statusline + dashboard |
| **5HL** | 5-hour rate limit usage | 0-100% | statusline + dashboard |
| **7DL** | 7-day rate limit usage | 0-100% | statusline + dashboard |
| **BRN** | Cost burn rate | 0-1.0 $/min | statusline + dashboard |
| **CTR** | Context consumption rate | 0-5.0 %/min | statusline + dashboard |
| **CST** | Session cost | 0-$50 | statusline + dashboard |
| **TDY** | Today's cost (all sessions) | — | dashboard |
| **WEK** | Rolling 7-day cost (all sessions) | — | dashboard |
| **CTF** | Context full ETA | HH:MM | statusline |
| **LNS** | Lines added / removed | — | dashboard |

## Usage

### Statusline

Runs automatically on each Claude Code status update via stdin JSON. Outputs a single ANSI-colored line with Nord bar background. Left side: model, APR, CTX, CHR, 5HL, 7DL. Right side: BRN, CTR, CTF, CST, DUR, NOW. Right segments drop when terminal is narrow.

### Dashboard

```bash
python monitor.py              # auto-detect session
python monitor.py --session ID # specific session
python monitor.py --list       # list active sessions
python monitor.py --refresh 1000  # custom refresh interval (ms, default 500)
```

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Force refresh (resets stale timer) |
| `l` | Toggle legend overlay |
| `s` | Switch session (picker) |
| `1-9` | Select session (picker) |

### Session Picker

Shown on launch when multiple session files exist. Press `1-9` to select. Lists both live and stale sessions. With exactly one session file (active, not stale), connects automatically.

## How It Works

```
Claude Code ──stdin JSON──> statusline.py ──> terminal (one-line ANSI bar)
                                 |
                                 v
                         $TMPDIR/claude-aio-monitor/
                         ├── {session_id}.json    (atomic snapshot)
                         └── {session_id}.jsonl   (append-only history)
                                 |
                                 v
                           monitor.py ──> terminal (fullscreen TUI)

Both scripts import rates.py for shared BRN/CTR calculation.
```

1. **Claude Code** emits JSON telemetry to `statusline.py` via stdin on each status event (~300ms debounce).
2. **statusline.py** parses JSON, renders one-line ANSI status bar, writes atomic snapshot (`.json`) + appends to history (`.jsonl`).
3. **monitor.py** polls temp directory (default 500ms), reads snapshots + history, renders fullscreen TUI with progress bars and computed metrics.
4. **rates.py** provides `calc_rates()` — computes BRN ($/min) and CTR (%/min) from JSONL history timestamps.

### Color Thresholds

| Range | Color | Meaning |
|-------|-------|---------|
| < 50% | Green | Healthy |
| 50-79% | Yellow | Approaching limits |
| >= 80% | Red | Critical |

Exception: 5HL/7DL labels use yellow as base color (even below 50%) to visually distinguish rate limits from performance metrics.

## Configuration

| Variable | Default | Scope | Description |
|----------|---------|-------|-------------|
| `CLAUDE_STATUS_WARN` | `50` | statusline | Yellow threshold (%) |
| `CLAUDE_STATUS_CRIT` | `80` | statusline | Red threshold (%) |
| `CLAUDE_WARN_BRN` | `0.50` | dashboard | Burn rate warning threshold ($/min) |

```bash
export CLAUDE_STATUS_WARN=60
export CLAUDE_STATUS_CRIT=90
```

<details>
<summary>IPC and security details</summary>

### IPC Details

- State files: atomic write via `NamedTemporaryFile` + `os.replace()` (no partial reads)
- History: append-only JSONL, written only after snapshot succeeds — keeps `.json` and `.jsonl` in sync
- Auto-trimmed when file exceeds 1 MB (keeps last 1000 entries)
- Stale `.tmp` files older than 60 seconds cleaned up automatically
- Session detection: files older than 30 minutes marked as stale — metrics dimmed, `Session Inactive` shown

### Security

| Measure | Protection |
|---------|------------|
| Session ID validation | Strict regex `[a-zA-Z0-9_-]{1,128}` prevents path traversal |
| Input sanitization | C0/C1 control characters stripped from string fields before terminal output |
| File size limits | JSON capped at 1 MB, JSONL at 10 MB — oversized files skipped |
| Atomic writes | Unpredictable temp filenames prevent symlink attacks |
| TOCTOU prevention | Single open + bounded read instead of separate stat + read |
| Directory permissions | Temp directory created with `0o700` where supported |
| Graceful shutdown | SIGTERM handler + atexit restore terminal state |
| Render isolation | Corrupted data caught per-frame — TUI never crashes |

</details>

## Requirements

- **Python 3.8+** (stdlib only — no pip install needed)
- **Claude Code CLI** with statusline support
- **Truecolor terminal** — Windows Terminal, iTerm2, Alacritty, Kitty, or any terminal supporting ANSI 24-bit color
- **80 columns** minimum recommended

## Troubleshooting

**Monitor shows "Waiting for Claude Code session..."**
- Ensure Claude Code is running with an active session.
- Check `statusLine.command` in `~/.claude/settings.json`.
- Verify temp files: `%TEMP%/claude-aio-monitor/` (Windows) or `/tmp/claude-aio-monitor/` (macOS/Linux).

**Statusline not appearing**
- Verify the path in `statusLine.command` uses forward slashes.
- Test: `echo '{"context_window": {"used_percentage": 42}}' | python statusline.py`

**Raw escape codes visible**
- Terminal lacks ANSI support. Monitor checks `isatty()` and `TERM=dumb` on startup.
- Use: **Windows Terminal**, iTerm2, xterm, Kitty, Alacritty.
- Test: `python -c "print('\033[32mGREEN\033[0m')"`

**Garbled output on Windows**
- Run `chcp 65001` for UTF-8 mode.

**Keyboard not responding**
- Terminal window must have focus (Windows `msvcrt.getch()` requirement).
- Fallback: `Ctrl+C`.

## Contributing

Contributions welcome. Keep zero-dependency (stdlib only), ship `rates.py` alongside entry scripts, test on Windows and Unix. Run `python -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('rates.py','statusline.py','monitor.py')]"` before submitting.

## License

MIT License. See [LICENSE](LICENSE) for details.

---

[Changelog](CHANGELOG.md)
