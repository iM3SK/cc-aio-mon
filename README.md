# CC AIO MON — Claude Code Terminal Monitor

![Python 3.8+](https://img.shields.io/badge/requires_python-3.8%2B-blue) ![License MIT](https://img.shields.io/badge/license-MIT-green) ![Dependencies](https://img.shields.io/badge/dependencies-stdlib_only-brightgreen) ![Tests](https://github.com/iM3SK/cc-aio-mon/workflows/Tests/badge.svg) ![CodeQL](https://github.com/iM3SK/cc-aio-mon/actions/workflows/github-code-scanning/codeql/badge.svg) ![Scorecard](https://github.com/iM3SK/cc-aio-mon/workflows/Scorecard%20supply-chain%20security/badge.svg) ![Bandit](https://github.com/iM3SK/cc-aio-mon/workflows/Bandit%20Security%20Scan/badge.svg)

**Real-time terminal monitor for Claude Code CLI.** Track context window usage, API rate limits, session costs, burn rate, and cache performance — all in one compact TUI dashboard. Stdlib only (Python 3.8+), cross-platform.

> **How it works:** Claude Code pipes session telemetry as JSON to `statusline.py` via **stdin** after each assistant message, permission mode change, or vim mode toggle (300ms debounce). The script parses the JSON, renders a single ANSI-colored status line in the terminal, and writes the data to `$TMPDIR/claude-aio-monitor/` as atomic JSON snapshots + append-only JSONL history. A separate `monitor.py` process polls these temp files and renders a fullscreen TUI dashboard. Both scripts share `shared.py` for burn rate ($/min) and context rate (%/min) calculation. A `pulse.py` background worker probes Anthropic backend stability (status page + HTTPS endpoint) for a "safe to code / not safe to code" verdict — disable with `CC_AIO_MON_NO_PULSE=1`. **Five Python files, stdlib only, no build step.**

| | |
|---|---|
| **Input** | Claude Code `statusLine` JSON protocol via stdin — model info, context window, rate limits, cost, token counts, session metadata |
| **Output** | ANSI truecolor terminal — single-line statusline + fullscreen TUI dashboard with progress bars, smart alerts, cross-session cost aggregation |
| **Data flow** | `Claude Code → stdin JSON → statusline.py → temp files → monitor.py → TUI` |
| **Files** | `statusline.py` (statusline renderer + IPC writer), `monitor.py` (TUI dashboard), `shared.py` (shared helpers + rate math), `update.py` (self-updater), `pulse.py` (Anthropic backend stability monitor) |
| **IPC** | Atomic JSON snapshots + JSONL history in `$TMPDIR/claude-aio-monitor/` — no sockets, no databases |

<p align="center"><a href="screenshots/cc-aio-mon-dashboard.png"><img src="screenshots/cc-aio-mon-dashboard.png" alt="CC AIO MON — fullscreen TUI dashboard showing context window, API ratio, cache hit rate, rate limits, burn rate, cost, and cross-session totals with Nord color scheme"></a></p>

<p align="center"><a href="screenshots/cc-aio-mon-statusline.png"><img src="screenshots/cc-aio-mon-statusline.png" alt="CC AIO MON — single-line ANSI status bar showing Model, CTX, 5HL, 7DL, CST, BRN, APR, CHR segments"></a></p>

## Why CC AIO MON?

| Project | Data source | Limitation |
|---------|-------------|------------|
| claude-monitor | Reads JSONL cost logs | Estimated data, not real-time |
| ccusage | CLI usage aggregator | Historical only, no live view |
| ccstatusline | Status line script | No TUI, no multi-session |
| **CC AIO MON** | **Official stdin JSON protocol** | **Real-time, stdlib only, most complete** |

Other monitors scrape log files or estimate costs from token counts. CC AIO MON reads the **official Claude Code statusline JSON** — the same data Claude Code uses internally. No estimation, no guessing, no stale logs.

<p align="center"><a href="screenshots/cc-aio-mon-menu.png"><img src="screenshots/cc-aio-mon-menu.png" alt="CC AIO MON — menu modal with navigation hub, views, and system sections"></a></p>

<p align="center"><a href="screenshots/cc-aio-mon-legend.png"><img src="screenshots/cc-aio-mon-legend.png" alt="CC AIO MON — legend overlay showing all metric codes, hotkeys, token stats, cost breakdown, and update sections"></a></p>

## Setup

| Platform | Guide |
|----------|-------|
| [Windows](docs/setup-windows.md) | Python Launcher (`py`), Windows Terminal, PowerShell |
| [macOS](docs/setup-macos.md) | python3, Terminal.app or iTerm2 |
| [Linux](docs/setup-linux.md) | python3, any truecolor terminal |

## Features

- **Compact** — all critical metrics in one screen. No scrolling, no tabs, no wasted space.
- **Stdlib only** — Python 3.8+. No pip install, no venv, no node_modules.
- **Simple setup** — clone the repo, add one block to `~/.claude/settings.json`, launch the monitor. See [platform setup guide](#setup).
- **Official stdin JSON** — reads Claude Code's `statusLine` JSON protocol via stdin. No log scraping, no file watching, no API polling. Real data, real-time.
- **Two-tier architecture** — `statusline.py` (single-line status bar, triggered per Claude Code event) + `monitor.py` (fullscreen TUI, polls temp files independently).
- **Temp file IPC** — atomic JSON snapshots + JSONL history in `$TMPDIR/claude-aio-monitor/`. No sockets, no databases, no shared memory. Works across terminal sessions.
- **Progress bars with fixed ranges** — BRN (0-2.0 $/min), CTR (0-5.0 %/min), CST (0-$200) plus standard 0-100% bars for APR, CHR, CTX, 5HL, 7DL.
- **Smart warnings** — header alerts when context fills in < 30 min or burn rate exceeds threshold.
- **Cross-session cost tracking** — TDY (today) and WEK (rolling 7-day) aggregate cost across all active Claude Code sessions.
- **Token usage stats** — press `t` for a per-model token breakdown (In/Out/Calls), session count, active days, streaks, longest session, and most active day. Reads `~/.claude/projects/` transcripts. Filterable by All Time / Last 7 Days / Last 30 Days.
- **Update manager** — press `u` to check for updates. Shows current vs remote version, new commits, changelog preview, and safety warnings. Press `a` to apply.
<p align="center">
<a href="screenshots/cc-aio-mon-stats.png"><img src="screenshots/cc-aio-mon-stats.png" alt="CC AIO MON — token stats modal with per-model breakdown using 3-char codes"></a>
<a href="screenshots/cc-aio-mon-cost.png"><img src="screenshots/cc-aio-mon-cost.png" alt="CC AIO MON — cost breakdown modal with token costs, cache savings, burn rate over time"></a>
<a href="screenshots/cc-aio-mon-update.png"><img src="screenshots/cc-aio-mon-update.png" alt="CC AIO MON — update manager showing current vs remote version with apply option"></a>
</p>

- **Cross-platform** — Windows (Terminal, PowerShell, Git Bash), macOS (Terminal, iTerm2), Linux. CI-tested: Ubuntu (Python 3.8 + 3.12), Windows (Python 3.12), macOS (Python 3.12).
- **Nord truecolor palette** — ANSI 24-bit color with semantic grouping: green = performance, cyan = context, yellow = rate limits, orange = cost/finance, red = critical.
- **Responsive layout** — statusline drops right segments for narrow terminals. Dashboard compresses sections automatically.
- **Multi-session** — auto-detects sessions via temp files. Numbered picker for multiple sessions. Press `s` to switch anytime.
- **Stale detection** — sessions idle > 30 minutes get dimmed metrics with last known values preserved. See [Session States](#session-states) for a visual example.
- **Auto-purge** — dead session files older than 48 hours are automatically cleaned up from the temp directory.
- **Release check (RLS)** — background version check against GitHub once per hour. Shows green "up to date" or blinking red "update available" in the dashboard. Disable with `CC_AIO_MON_NO_UPDATE_CHECK=1`.
- **Menu modal** — press `m` to open the navigation hub. Quick access to all features: refresh, session switch, legend, token stats, cost breakdown, update manager.
- **Cost breakdown** — press `c` for per-model cost estimation with token-level pricing (input, output, cache read, cache write), cache savings percentage, and burn rate over time bars.
- **Anthropic Pulse** — press `p` for real-time Anthropic backend stability. Weighted score (0-100) from `status.anthropic.com` (indicator + incidents) + HTTPS probe on `api.anthropic.com/v1/messages` (TLS + HTTP latency). Rolling-median smoothed verdict (`SAFE TO CODE` / `DEGRADED` / `NOT SAFE TO CODE`). Per-model tagging of active incidents (opus/sonnet/haiku). JSONL history in `$TMPDIR/claude-aio-monitor/pulse.jsonl` with hybrid cleanup (24h age cutoff on startup + runtime rotation at 1 MB). **Zero token cost, zero API key required.**
- **Security hardened** — session ID regex validation (`[a-zA-Z0-9_-]{1,128}`), C0/C1 control character sanitization, atomic writes via `NamedTemporaryFile`, symlink rejection on data directory, file size limits (1MB JSON, 2MB JSONL, 10MB cross-session).

## Session States

The dashboard distinguishes **active** and **inactive** sessions. An active session receives fresh JSON snapshots from `statusline.py` on every Claude Code event. When no update arrives for more than 30 minutes, `monitor.py` marks the session as stale: the header switches to `Session Inactive`, the time-since-last-update is shown in parentheses (e.g. `(617m)`), and every metric is rendered in the dimmed variant of its color. Last known values are preserved — nothing is zeroed out — so you can still see where the session left off (context used, cost accumulated, rate-limit buckets, burn rate at time of freeze).

<p align="center"><a href="screenshots/cc-aio-mon-picker.png"><img src="screenshots/cc-aio-mon-picker.png" alt="CC AIO MON — session picker with compact display, 8-char UUID, model codes, live/stale tags, max 9 sessions"></a></p>

Press `r` to force a refresh (resets the stale timer if new data has arrived), or `s` to switch to a different session from the picker. If the session has truly ended and you want it out of the picker, delete its JSON/JSONL pair from `$TMPDIR/claude-aio-monitor/`.

## Metrics at a Glance

| Metric | What it shows | Range | Where |
|--------|--------------|-------|-------|
| **APR** | API time / total session time | 0-100% | statusline + dashboard |
| **DUR** | Session duration (sub-stat of APR) | — | dashboard |
| **CHR** | Cache read tokens / total cache | 0-100% | statusline + dashboard |
| **CTX** | Context window usage | 0-100% | statusline + dashboard |
| **5HL** | 5-hour rate limit usage | 0-100% | statusline + dashboard |
| **7DL** | 7-day rate limit usage | 0-100% | statusline + dashboard |
| **BRN** | Cost burn rate | 0-2.0 $/min | statusline + dashboard |
| **CTR** | Context consumption rate | 0-5.0 %/min | dashboard |
| **CST** | Session cost | 0-$200 | statusline + dashboard |
| **TDY** | Today's cost (all sessions) | — | dashboard |
| **WEK** | Rolling 7-day cost (all sessions) | — | dashboard |
| **LNS** | Lines added / removed | — | dashboard |
| **NOW** | Current clock time | HH:MM:SS | dashboard |
| **UPD** | Last data update age | — | dashboard |
| **RLS** | Release status (up to date / update available) | — | dashboard |
| **SES** | Total sessions | — | usage stats modal |
| **DAY** | Active days | — | usage stats modal |
| **STK** | Streak (current/best) | — | usage stats modal |
| **LSS** | Longest session | — | usage stats modal |
| **TOP** | Most active day | — | usage stats modal |

## Usage

### Statusline

Runs automatically on each Claude Code status update via stdin JSON. Outputs a single ANSI-colored line: Model │ CTX │ 5HL │ 7DL │ CST │ BRN │ APR │ CHR. Trailing segments drop when terminal is narrow. No background padding — CC notifications share the right side of the row.

### Dashboard

> On macOS/Linux use `python3`. On Windows use `py`.

```bash
python3 monitor.py              # auto-detect session
python3 monitor.py --session ID # specific session
python3 monitor.py --list       # list active sessions
python3 monitor.py --refresh 1000  # custom refresh interval (ms, default 500)
```

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit |
| `m` | Menu (navigation hub) |
| `r` | Force refresh (resets stale timer) |
| `s` | Switch session (picker) |
| `t` | Token usage stats (per-model breakdown) |
| `c` | Cost breakdown (token costs, cache savings, burn rate over time) |
| `u` | Update manager (version check, changelog, apply) |
| `p` | Anthropic Pulse (backend stability modal) |
| `l` | Toggle legend overlay |
| `1-9` | Select session (picker) |
| `1/2/3` | Switch period in token usage stats (all/7d/30d) |

### Session Picker

Shown on launch when multiple session files exist. Press `1-9` to select. Active sessions sorted first, max 9 shown. Auto-connects only when exactly one session exists (no stale sessions). Press `s` to force picker from dashboard or menu.

## How It Works

```
Claude Code ──stdin JSON──> statusline.py ──> terminal (single-line ANSI status bar)
                                 |
                                 v
                         $TMPDIR/claude-aio-monitor/        (macOS/Linux: /tmp | Windows: %TEMP%)
                         ├── {session_id}.json    (atomic snapshot)
                         └── {session_id}.jsonl   (append-only history)
                                 |
                                 v
                           monitor.py ──> terminal (fullscreen TUI)

Both scripts import shared.py for shared BRN/CTR calculation.
```

1. **Claude Code** emits JSON telemetry to `statusline.py` via stdin after each assistant message, permission mode change, or vim mode toggle (300ms debounce).
2. **statusline.py** parses JSON, renders single-line ANSI status bar (model, context, rate limits, cost, burn rate, APR, CHR), writes atomic snapshot (`.json`) + appends to history (`.jsonl`).
3. **monitor.py** polls temp directory (default 500ms), reads snapshots + history, renders fullscreen TUI with progress bars and computed metrics.
4. **shared.py** provides `calc_rates()` — computes BRN ($/min) and CTR (%/min) from JSONL history timestamps.

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
| `CLAUDE_WARN_BRN` | `1.00` | dashboard | Burn rate warning threshold ($/min) |
| `CC_AIO_MON_NO_UPDATE_CHECK` | *(unset)* | dashboard | Set to `1` to disable background release check |
| `CC_AIO_MON_NO_PULSE` | *(unset)* | dashboard | Set to `1` to disable background Anthropic Pulse worker (no outbound network) |

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
- Dead sessions older than 48 hours auto-purged (`.json` + `.jsonl` pair deleted)
- Session detection: files older than 30 minutes marked as stale — metrics dimmed, `Session Inactive (Nm)` header shown with minutes-since-last-update, last known values preserved (see [Session States](#session-states))

### Security

| Measure | Protection |
|---------|------------|
| Session ID validation | Strict regex `[a-zA-Z0-9_-]{1,128}` prevents path traversal |
| Input sanitization | C0/C1 control characters + bidi overrides stripped before terminal output |
| File size limits | JSON capped at 1 MB, JSONL at 2 MB (10 MB for cross-session aggregation) — oversized files skipped |
| Atomic writes | Unpredictable temp filenames prevent symlink attacks |
| Directory validation | `lstat()` + `S_ISDIR` verification — rejects symlinks, NTFS junctions, and defends against TOCTOU races |
| TOCTOU prevention | Single open + bounded read instead of separate stat + read |
| Directory permissions | Temp directory created with `0o700` where supported |
| CJK-aware truncation | `unicodedata.east_asian_width()` prevents terminal overflow from fullwidth characters |
| Thread safety | `_update_result` protected by `threading.Lock` — safe across Python implementations |
| Graceful shutdown | SIGTERM handler + atexit restore terminal state |
| Render isolation | Corrupted data caught per-frame — TUI never crashes |

</details>

## Requirements

- **Python 3.8+**
  - macOS / Linux: usually pre-installed (`python3 --version` to verify)
  - Windows: must be installed separately (not bundled with Windows)
  - No pip packages needed — stdlib only. See [platform setup guide](#setup).
- **Claude Code CLI** with statusline support
- **Truecolor terminal** — Windows Terminal, iTerm2, Alacritty, Kitty, or any terminal supporting ANSI 24-bit color
- **80 columns** minimum recommended

## Known Limitations

- **Delayed refresh after context compaction** — when Claude Code compacts the context window, the dashboard continues showing the pre-compaction CTX value until Claude Code emits the next statusline event (typically the next assistant message). This is a Claude Code protocol limitation — `statusline.py` is only invoked on assistant messages, permission mode changes, or vim mode toggles. There is no external API to trigger a refresh on demand.

## Troubleshooting

Platform-specific troubleshooting is in the setup guides:

- [Windows — Troubleshooting](docs/setup-windows.md#troubleshooting)
- [macOS — Troubleshooting](docs/setup-macos.md#troubleshooting)
- [Linux — Troubleshooting](docs/setup-linux.md#troubleshooting)

## Updating

**Recommended:** use the bundled `update.py` script — it safely checks for updates, previews changes, and applies them:

```bash
# macOS / Linux
cd ~/.cc-aio-mon
python3 update.py             # check only (no changes)
python3 update.py --apply     # check + git pull
```

```powershell
# Windows (PowerShell)
cd "$env:USERPROFILE\.cc-aio-mon"
py update.py                  # check only (no changes)
py update.py --apply          # check + git pull
```

The script is **read-only by default** — it shows you what would change (version, new commits, CHANGELOG preview) and only pulls when you add `--apply`. It aborts safely if your working tree is dirty, you're on a different branch, or history has diverged.

**Manual fallback** — if you prefer plain git:

```bash
# macOS / Linux
git -C ~/.cc-aio-mon pull

# Windows (PowerShell)
git -C "$env:USERPROFILE\.cc-aio-mon" pull
```

The path in `settings.json` does not change between versions — `git pull` updates the project source, and Claude Code picks up the new code on the next session. No changes to `settings.json` needed.

After updating, restart Claude Code to pick up the new statusline. Optionally re-run `check-requirements.ps1` / `check-requirements.sh` from the repo directory to verify system requirements still pass.

## Contributing

Contributions welcome. Keep it stdlib only, ship `shared.py` alongside entry scripts, test on Windows and Unix. Before submitting, run the test suite and the compile check (`python3` on macOS/Linux, `py` on Windows):

```bash
python3 tests.py
python3 -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('shared.py','statusline.py','monitor.py','update.py','pulse.py')]"
```

Open an issue first for anything non-trivial so the approach can be discussed before work begins. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for full guidelines.

## License

MIT License. See [LICENSE](LICENSE) for details.

---

[Changelog](CHANGELOG.md)
