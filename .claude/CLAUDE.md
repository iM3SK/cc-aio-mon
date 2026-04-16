# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

CC AIO MON — real-time terminal monitoring dashboard for Claude Code CLI. Pure Python, stdlib only.

## Architecture

```
Claude Code → stdin JSON → statusline.py → $TMPDIR/claude-aio-monitor/ → monitor.py → TUI
```

- **statusline.py** — reads Claude Code statusLine JSON from stdin, renders single-line ANSI status bar (Model │ CTX │ 5HL │ 7DL │ CST │ BRN │ APR │ CHR), writes atomic snapshots + JSONL history to temp dir
- **monitor.py** — fullscreen TUI dashboard, polls temp files every 500ms, renders live metrics, keyboard shortcuts, usage stats modal (reads `~/.claude/projects/` transcripts), background RLS release check (daemon thread, git fetch, 1h TTL), Pulse modal (`p` key)
- **shared.py** — shared helpers (`_num`, `_sanitize`, `f_dur`, `f_tok`, `f_cost`, `calc_rates`, `char_width`, `is_safe_dir`, `ensure_data_dir`), ANSI color constants, and regexes (`_SID_RE`, `_ANSI_RE`) used by statusline.py, monitor.py, and pulse.py
- **pulse.py** — Anthropic backend stability monitor. Daemon worker (30s fetch interval) polls `status.anthropic.com/api/v2/summary.json` + HTTPS probes `api.anthropic.com/v1/messages`. Weighted score 0-100 (indicator 50% + incidents 30% + latency 20%), rolling-median smoothed over last 5 samples, p50/p95 latency over 60 samples (~30 min). Per-model incident tagging via regex (opus/sonnet/haiku). JSONL persistence in `$TMPDIR/claude-aio-monitor/pulse.jsonl` with hybrid cleanup (startup: drop >24h + cap 2000 records; runtime: trim to last 500 lines when file >1 MB, checked every 100 appends). Stdlib only, zero token cost.
- **update.py** — self-update checker with git pull --ff-only safety guards
- **tests.py** — unit tests, stdlib unittest

## Commands

- Test: `py tests.py` (Windows) / `python3 tests.py` (Unix)
- Test single: `py -m unittest tests.TestClassName.test_method`
- Lint: `py -m py_compile monitor.py statusline.py shared.py update.py pulse.py`
- Run monitor: `py monitor.py`
- Run statusline: configured via Claude Code settings.json, reads stdin

## statusLine config

Claude Code statusLine command MUST be wrapped in `bash -c '...'` — externé binárky (py, python, python3) nefungujú priamo, len bash builtiny. Toto platí pre všetky platformy.

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash -c 'py C:/path/to/statusline.py'"
  }
}
```

## Rules

- Stdlib only — no pip packages, no external dependencies
- All file I/O confined to temp directory ($TMPDIR/claude-aio-monitor/) and ~/.claude/projects/ (read-only, for usage stats)
- Session IDs validated with regex: `^[a-zA-Z0-9_\-]{1,128}$`
- Atomic writes via NamedTemporaryFile + os.replace()
- File size limits: JSON 1MB, JSONL 2MB read / 1MB trim (cross-session cost aggregation: 10MB)
- Cross-platform: Windows (py, ctypes, msvcrt) + Unix (python3, termios, select)
- ANSI 24-bit color — Windows Terminal required on Windows
- Python 3.8+ compatibility
