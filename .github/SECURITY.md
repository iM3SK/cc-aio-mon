# Security Policy

## Supported Versions

Only the latest release receives security fixes.

## Reporting a Vulnerability

Do **not** open a public GitHub issue for security vulnerabilities.

Report privately via GitHub's [Security Advisories](../../security/advisories/new) feature. Include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact

You will receive a response within 7 days. If confirmed, a fix will be released as soon as possible.

## Scope

**In scope:**
- `monitor.py`, `statusline.py`, `shared.py`, `update.py`
- `check-requirements.ps1`, `check-requirements.sh`
- Setup documentation under `docs/`
- CI workflows under `.github/workflows/`

**Out of scope:**
- Claude Code CLI itself — report to [anthropics/claude-code](https://github.com/anthropics/claude-code)
- Python standard library — report to [python.org/security](https://www.python.org/security/)
- GitHub Actions runners and the GitHub platform — report to [GitHub](https://github.com/security)
- Third-party packages pinned in `.github/workflows/requirements-bandit.txt` — report upstream to the respective maintainers

## Security Model

CC AIO MON reads session data from Claude Code via stdin and writes snapshots to a local temp directory. The Python code itself makes no network requests and stores no credentials. The optional `update.py --apply` command invokes `git pull --ff-only` as a subprocess only when explicitly requested by the user.

Key protections:

- Session ID validated against `[a-zA-Z0-9_-]{1,128}` — prevents path traversal
- All JSON fields sanitized before terminal output — prevents escape injection
- Atomic writes via `NamedTemporaryFile` + `os.replace()` — prevents partial reads
- File size limits on all reads — prevents memory exhaustion
- Temp directory created with `0o700` permissions where supported
- `update.py` guards: dirty tree, wrong branch, detached HEAD, divergence, downgrade, Python version mismatch
