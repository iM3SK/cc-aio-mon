# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

CC AIO MON — real-time terminal monitoring dashboard for Claude Code CLI. Pure Python, stdlib only.

## Architecture

```
Claude Code → stdin JSON → statusline.py → $TMPDIR/claude-aio-monitor/ → monitor.py → TUI
```

- **statusline.py** — reads Claude Code statusLine JSON from stdin, renders single-line ANSI status bar (Model │ CTX │ 5HL+→countdown │ 7DL+→countdown │ CST │ BRN), writes atomic snapshots + JSONL history to temp dir. 5HL/7DL reset countdown uses `shared.f_cd()` with ANSI faint (`\033[2m` — `shared.FAINT`) for subdued styling, e.g. `→ 2h 15m` / `→ 6d 12h` — same formatter as monitor.py RST display. ANSI blink (`\033[5m`) is NOT used: Ink (Claude Code's TUI renderer) doesn't support blink in its Text component, and Windows Terminal doesn't render SGR 5 either. APR and CHR are dashboard-only (removed from statusline in v1.10 to free horizontal space).
- **monitor.py** — fullscreen TUI dashboard, polls temp files every 500ms, renders live metrics, keyboard shortcuts. Modals: Token Stats (`t` — per-model bars count input+output+cache_read+cache_write via `_total_tokens()`), Cost Breakdown (`c` — LAST REQUEST from `current_usage` + SESSION BREAKDOWN aggregated from transcript JSONL via `_aggregate_session_cost()`, 5s TTL cache, 50MB cap), Pulse (`p`), Update (`u` — includes "Checked Xm ago" freshness indicator + cyan `github.com/iM3SK/cc-aio-mon` repo link). Background RLS release check (daemon thread, git fetch, 1h TTL).
- **shared.py** — shared helpers (`_num`, `_sanitize`, `f_dur`, `f_tok`, `f_cost`, `f_cd`, `calc_rates`, `char_width`, `is_safe_dir`, `ensure_data_dir`, `strip_context_suffix`, `compact_context_suffix`, `extract_changelog_entry`, `run_git`), constants (`RESERVED_SIDS`, ANSI `FAINT`), ANSI color constants, and regexes (`_SID_RE`, `_ANSI_RE`) used by statusline.py, monitor.py, pulse.py, and update.py
- **pulse.py** — Anthropic backend stability monitor. Daemon worker (30s fetch interval) polls `status.claude.com/api/v2/summary.json` + HTTPS probes `api.anthropic.com/v1/messages`. Weighted score 0-100 (indicator 50% + incidents 30% + latency 20%), rolling-median smoothed over last 5 samples, p50/p95 latency over 60 samples (~30 min). Per-model incident tagging — prefers `incidents[].components[]` array (canonical Statuspage schema), regex on title+body as fallback (opus/sonnet/haiku). Pricing verified 2026-04: Opus 4.7/4.6/4.5 $5/$25, Sonnet 4.6/4.5 $3/$15, Haiku 4.5 $1/$5 per 1M tokens. JSONL persistence in `$TMPDIR/claude-aio-monitor/pulse.jsonl` with hybrid cleanup (startup: drop >24h + cap 2000 records; runtime: trim to last 500 lines when file >1 MB, checked every 100 appends). Stdlib only, zero token cost.
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
- File size limits: JSON 1MB (`MAX_FILE_SIZE`), JSONL 2MB read / 1MB trim, cross-session cost aggregation 10MB (`MAX_FILE_SIZE * 10`), per-transcript read cap 50MB (`TRANSCRIPT_MAX_BYTES`)
- Cross-platform: Windows (py, ctypes, msvcrt) + Unix (python3, termios, select)
- ANSI 24-bit color — Windows Terminal required on Windows
- Python 3.8+ compatibility
- `transcript_path` from statusline JSON must be containment-validated (inside ~/.claude/projects/, no symlinks) before open
- All subprocess calls use `shared.run_git` with minimal env whitelist (blocks GIT_SSH_COMMAND / LD_PRELOAD injection)

## Audit

Audit postupy a audit logs live in `docs/audits/` (local-only, gitignored). Start with `AUDIT-PLAN.md`.

## Git Commit Policy

**SAFE TO COMMIT** (tracked source + docs):
- Python source: `monitor.py`, `statusline.py`, `shared.py`, `pulse.py`, `update.py`, `tests.py`
- Top-level docs: `README.md`, `CHANGELOG.md`, `LICENSE`, `CODEOWNERS`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`
- `.github/` — `SECURITY.md`, workflows, PR/issue templates, `dependabot.yml`
- `docs/setup-windows.md`, `docs/setup-linux.md`, `docs/setup-macos.md`
- `screenshots/*.png`
- `check-requirements.sh`, `check-requirements.ps1`
- `.gitignore`
- `.claude/CLAUDE.md` — **tracked despite `.claude/` in .gitignore** (grandfathered; this is project-wide rules for contributors, not local Claude config)

**NEVER COMMIT** (gitignored or sensitive):
- Compile artifacts: `__pycache__/`, `*.pyc`, `*.pyo`, `*.egg-info/`, `dist/`, `build/`
- Secrets: `.env`, any credential files, API keys
- Temp files: `*.tmp`, anything in `$TMPDIR/claude-aio-monitor/`
- Local planning: `TASKS.md`, `PROMO.md`
- Editor/OS artifacts: `.vscode/`, `.idea/`, `*.swp`, `*.swo`, `.DS_Store`, `Thumbs.db`
- Anything new under `.claude/` other than already-tracked `CLAUDE.md` (gitignore blocks it)

**REVIEW BEFORE COMMIT:**
- Hardcoded user paths (`C:\Users\0\...`, `/home/<username>/...`, `$TMPDIR`)
- Session data / transcripts
- Personal identifiers (emails except `help@digitalcoach.sk` owner, GitHub handles)
- Binaries > 500 KB (screenshots are OK up to ~200 KB)

**PRE-COMMIT CHECKLIST (run every time):**
1. `git status --short` — review all changes
2. `py tests.py` — all pass
3. `py -m py_compile monitor.py statusline.py shared.py update.py pulse.py`
4. Stage explicitly by filename — **never** `git add .` or `git add -A`
5. `git diff --cached` — final visual review
6. Commit message format: `feat/fix/chore(scope): short description`
7. **Forbidden flags**: `--no-verify`, `--force`, `--no-gpg-sign`, `-c commit.gpgsign=false`
8. Never amend already-pushed commits; create new commit instead
