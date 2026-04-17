# Security Policy

## Supported Versions

Only the latest release receives security fixes.

## Reporting a Vulnerability

Do **not** open a public GitHub issue for security vulnerabilities.

Report privately via GitHub's [Security Advisories](../../security/advisories/new) feature. Include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact

You will receive a response within 72 hours. If confirmed, a fix will be released as soon as possible.

## Scope

**In scope:**
- `monitor.py`, `statusline.py`, `shared.py`, `update.py`, `pulse.py`
- `check-requirements.ps1`, `check-requirements.sh`
- Setup documentation under `docs/`
- CI workflows under `.github/workflows/`

**Out of scope:**
- Claude Code CLI itself — report to [anthropics/claude-code](https://github.com/anthropics/claude-code)
- Python standard library — report to [python.org/security](https://www.python.org/security/)
- GitHub Actions runners and the GitHub platform — report to [GitHub](https://github.com/security)
- Third-party packages pinned in `.github/workflows/requirements-bandit.txt` — report upstream to the respective maintainers

## Security Model

CC AIO MON reads session data from Claude Code via stdin and writes snapshots to a local temp directory. The `update.py --apply` command invokes `git pull --ff-only` as a subprocess only when explicitly requested. The `pulse.py` module performs outbound HTTPS requests to Anthropic's public status endpoints (see below) — **no credentials, no API keys, no user data** are transmitted.

**Outbound network (pulse.py):**

- `GET https://status.claude.com/api/v2/summary.json` — public status page JSON (every 30 s)
- `GET https://api.anthropic.com/v1/messages` — unauthenticated liveness probe; expects 401/405 (every 30 s)
- Request body: none. Headers: `User-Agent: cc-aio-mon-pulse/1.0` only.
- Response size capped at 512 KB; socket timeouts 4–5 s.
- Opt-out: set `CC_AIO_MON_NO_PULSE=1` to disable the background worker entirely.

### Monitored Surfaces

- **statusLine JSON input** (stdin) — size-capped (1 MB), parse-error bounded.
- **Session IDs** — regex-validated (`^[a-zA-Z0-9_\-]{1,128}$`), reserved names (`rls`, `stats`, `pulse`) blocked.
- **transcript_path** (statusline JSON) — containment-checked: must be regular file inside `~/.claude/projects/`, symlinks rejected. (added 2026-04)
- **Transcript content** (read-only, `~/.claude/projects/`) — per-file 50 MB cap, model IDs sanitized before terminal render. (added 2026-04)
- **File I/O** — confined to `$TMPDIR/claude-aio-monitor/` (writes) and `~/.claude/projects/` (reads). Atomic writes via `NamedTemporaryFile` + `os.replace`.
- **Subprocess** — git only. `shell=False`, list args, `GIT_TERMINAL_PROMPT=0`, bounded timeouts, minimal env whitelist (added 2026-04). Never uses `--no-verify` / `--force`.
- **Network** — Pulse: `status.claude.com` + `api.anthropic.com` HTTPS GET, no auth, bounded size (512 KB) + timeout (5 s). Update: git fetch origin main (HTTPS or SSH, configurable by user's git remote).

Opt-out env vars: `CC_AIO_MON_NO_PULSE=1`, `CC_AIO_MON_NO_UPDATE_CHECK=1`.

Key protections:

- Session ID validated against `[a-zA-Z0-9_-]{1,128}` — prevents path traversal
- All JSON fields sanitized before terminal output — prevents escape injection
- Atomic writes via `NamedTemporaryFile` + `os.replace()` — prevents partial reads
- File size limits on all reads (1 MB JSON, 2 MB JSONL, 10 MB cross-session, 512 KB pulse response, 1 MB `pulse.jsonl`) — prevents memory exhaustion
- Symlink and NTFS junction rejection on temp data directory — `lstat()` + `S_ISDIR` verification with `FILE_ATTRIBUTE_REPARSE_POINT` check on Windows. Defends against TOCTOU races between mkdir and validation.
- Temp directory created with `0o700` permissions where supported
- `update.py` guards: dirty tree, wrong branch, detached HEAD, divergence, downgrade, Python version mismatch
- `pulse.py` uses `urllib.request.urlopen` with default CA verification (no `ssl._create_unverified_context`); errors are caught by specific type, never broadly suppressed in probe logic
