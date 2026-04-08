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

## Security Model

CC AIO MON reads data from Claude Code via stdin and writes session state to a local temp directory. It does not make network requests, store credentials, or execute arbitrary code. Key protections:

- Session ID validated against `[a-zA-Z0-9_-]{1,128}` — prevents path traversal
- All JSON fields sanitized before terminal output — prevents escape injection
- Atomic writes via `NamedTemporaryFile` + `os.replace()` — prevents partial reads
- File size limits on all reads — prevents memory exhaustion
- Temp directory created with `0o700` permissions where supported
