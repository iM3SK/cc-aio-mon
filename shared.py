#!/usr/bin/env python3
"""Shared helpers and rate calculation for monitor.py and statusline.py."""

import os
import pathlib
import re
import stat as _stat_mod
import sys
import tempfile
import time
import unicodedata

MIN_EPOCH = 1_577_836_800  # 2020-01-01 — reject implausible timestamps

# Session IDs / file stems reserved for internal use. Never valid session names.
RESERVED_SIDS = frozenset({"rls", "stats", "pulse"})

# Shared constants — single source of truth for statusline.py + monitor.py
# Session ID: alphanumeric + underscore/hyphen, 1-128 chars.
# Negative lookahead rejects Windows reserved device names (CON, PRN, AUX, NUL,
# COM0-9, LPT0-9) case-insensitively. Opening CON.json on Windows opens the
# console device, not a file — reject cross-platform for consistency.
_SID_RE = re.compile(
    r"^(?!(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$)[a-zA-Z0-9_\-]{1,128}$"
)
_ANSI_RE = re.compile(r"\033(?:\[[0-9;?]*[a-zA-Z~]|\][^\x07]*\x07)")
MAX_FILE_SIZE = 1_048_576  # 1 MB
TRANSCRIPT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB — cap on per-transcript reads
DATA_DIR_NAME = "claude-aio-monitor"
DATA_DIR = pathlib.Path(tempfile.gettempdir()) / DATA_DIR_NAME
VERSION_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

# Single source of truth for app version — imported by monitor.py, pulse.py, update.py
VERSION = "1.10.2"

# Python files that ship with the app — used by syntax-check paths in update flow
# (monitor.py:_apply_update_worker + update.py:apply_update). Must stay in sync.
PY_FILES = ("monitor.py", "statusline.py", "shared.py", "pulse.py", "update.py")

# ANSI — Nord truecolor (shared palette for statusline.py + monitor.py)
E = "\033["
R = E + "0m"
B = E + "1m"
FAINT = E + "2m"
C_RED = E + "38;2;191;97;106m"
C_GRN = E + "38;2;163;190;140m"
C_YEL = E + "38;2;235;203;139m"
C_ORN = E + "38;2;208;135;112m"  # nord12 aurora orange — cost/finance
C_CYN = E + "38;2;136;192;208m"
C_WHT = E + "38;2;216;222;233m"
C_DIM = E + "38;2;76;86;106m"


_CONTEXT_SUFFIX_RE = re.compile(r"\s*\((\d+\w?)\s*context\)")


def strip_context_suffix(name):
    """Remove '(Nk context)' / '(NM context)' entirely. 'Opus 4.7 (1M context)' -> 'Opus 4.7'."""
    return _CONTEXT_SUFFIX_RE.sub("", name).strip()


def compact_context_suffix(name):
    """Compact to trailing unit. 'Opus 4.7 (1M context)' -> 'Opus 4.7 1M'."""
    return _CONTEXT_SUFFIX_RE.sub(r" \1", name).strip()


def _num(v, default=0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def safe_read(path, max_bytes):
    """Bounded read — returns bytes or None. Never reads more than max_bytes + 1.

    Closes TOCTOU gap: caller doesn't have to stat first, and even if they do,
    an attacker growing the file between stat and read cannot exceed max_bytes.
    Returns None on OSError, on >max_bytes overflow, or on missing file.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    return raw


def _sanitize(s):
    """Strip control characters and bidi overrides to prevent terminal escape injection."""
    s = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(s))
    return re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", s)


def f_dur(ms):
    ms = _num(ms, 0)
    if ms <= 0:
        return "--"
    s = int(ms / 1000)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def f_tok(n):
    n = _num(n, 0)
    if n <= 0:
        return "--"
    if n < 1000:
        return f"{int(n):,}"
    if n < 100_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.0f}M"


def f_cost(usd):
    usd = _num(usd, 0)
    if usd <= 0:
        return "--"
    if usd < 0.01:
        return f"{usd:.4f} $"
    return f"{usd:.2f} $"


def f_cd(epoch):
    """Countdown from now to `epoch`. Returns compact form (e.g. '2h 15m', '6d 12h')."""
    if epoch is None:
        return "--"
    epoch = _num(epoch, 0)
    diff = int(epoch - time.time())
    if diff <= 0:
        return "now"
    d, rem = divmod(diff, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d {h:02d}h"
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def char_width(ch):
    """Display width: 2 for CJK fullwidth/wide, 1 for everything else."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def is_safe_dir(p):
    """Verify path is a real directory (not symlink/junction). TOCTOU-resistant via lstat."""
    try:
        st = p.lstat()
    except OSError:
        return False
    if not _stat_mod.S_ISDIR(st.st_mode):
        return False
    # Windows: junctions are reparse points but is_symlink() misses them
    if sys.platform == "win32":
        try:
            if getattr(st, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                return False
        except AttributeError:
            pass
    return True


def ensure_data_dir(d):
    """Create data dir with 0o700 and verify safety. Returns True if usable."""
    try:
        d.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        try:
            d.mkdir(exist_ok=True)
        except OSError:
            return False
    if not is_safe_dir(d):
        return False
    # Fix permissions on Unix (mkdir mode is masked by umask)
    if sys.platform != "win32":
        try:
            st = d.stat()
            if _stat_mod.S_IMODE(st.st_mode) & 0o077:
                os.chmod(d, 0o700)
        except OSError:
            pass
    return True


_CHANGELOG_ENTRY_RE_TEMPLATE = r"## v{}\b.*?(?=\n## v|\Z)"


def extract_changelog_entry(text, version, max_lines=None):
    """Extract single '## v{version}' section from CHANGELOG text."""
    pattern = _CHANGELOG_ENTRY_RE_TEMPLATE.format(re.escape(version))
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return ""
    entry = m.group(0).strip()
    if max_lines is not None:
        lines = entry.splitlines()
        if len(lines) > max_lines:
            entry = "\n".join(lines[:max_lines])
    return entry


# Minimal env for git subprocess — drops any injected GIT_SSH_COMMAND, LD_PRELOAD,
# HTTP(S)_PROXY, GIT_EXEC_PATH etc. Keeps only what git genuinely needs.
_GIT_ENV_WHITELIST = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SystemRoot",
    "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE",
    "APPDATA", "LOCALAPPDATA",  # git-for-windows config lookup
)


def _git_env():
    src = os.environ
    env = {k: src[k] for k in _GIT_ENV_WHITELIST if k in src}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_git(args, cwd, timeout=15):
    """Safe git invocation with minimal env whitelist.
    Returns subprocess.CompletedProcess. Raises FileNotFoundError if git missing."""
    import subprocess
    return subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout, env=_git_env(),
    )


def calc_rates(hist):
    """Return (brn $/min, ctr %/min) from history, or (None, None) if insufficient data."""
    if len(hist) < 2:
        return None, None
    try:
        t0 = float(hist[0].get("t", 0))
        t1 = float(hist[-1].get("t", 0))
    except (TypeError, ValueError, AttributeError):
        return None, None
    if t0 < MIN_EPOCH or t1 < MIN_EPOCH:
        return None, None
    dt = t1 - t0
    if dt < 10:
        return None, None
    c0 = _num(hist[0].get("cost", {}).get("total_cost_usd"))
    c1 = _num(hist[-1].get("cost", {}).get("total_cost_usd"))
    x0 = _num(hist[0].get("context_window", {}).get("used_percentage"))
    x1 = _num(hist[-1].get("context_window", {}).get("used_percentage"))
    brn = (c1 - c0) / dt * 60 if c1 >= c0 else None
    ctr = (x1 - x0) / dt * 60 if x1 >= x0 else None
    return brn, ctr
