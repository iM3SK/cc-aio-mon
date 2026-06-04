#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared helpers and rate calculation for monitor.py and statusline.py."""

import codecs
import json
import os
import pathlib
import re
import stat as _stat_mod
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import IO, Iterable, List, Optional, Tuple

# Platform-conditional lock primitives used by acquire_singleton_lock().
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

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
HISTORY_READ_MAX = MAX_FILE_SIZE * 2   # 2 MB — per-session JSONL read cap (headroom over 1 MB trim target)
HISTORY_AGGREGATE_MAX = MAX_FILE_SIZE * 10  # 10 MB — cross-session cost aggregation cap
TRANSCRIPT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB — cap on per-transcript reads

# Named time constants — eliminate magic-number duplication across monitor/pulse/statusline.
# All values are seconds (wall-clock or monotonic context depends on call site).
SECONDS_1H = 3600
SECONDS_5H = 5 * SECONDS_1H        # 18000 — Claude rate-limit "5-hour" window
SECONDS_1D = 24 * SECONDS_1H       # 86400
SECONDS_7D = 7 * SECONDS_1D        # 604800 — Claude rate-limit "7-day" window

DATA_DIR_NAME = "claude-aio-monitor"
DATA_DIR = pathlib.Path(tempfile.gettempdir()) / DATA_DIR_NAME
VERSION_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

# Single source of truth for app version — imported by monitor.py, pulse.py, update.py
VERSION = "1.13.0"

# File-IPC contract version. Statusline writes this field on every snapshot
# and history entry; bumped when the JSON shape changes incompatibly. Monitor's
# load_state() gates on it: a snapshot tagged NEWER than this constant is
# treated as unreadable (degrades to None) rather than risk misreading an
# incompatible shape. Missing/older tags default to 0 and stay readable, so
# pre-versioning snapshots left on disk after a git pull are tolerated.
SCHEMA_VERSION = 1

# Default sample window for load_history(). At ~1 statusline event/min this
# yields a ~2-hour rolling window — enough for BRN/CTR rate smoothing without
# rereading the entire per-session JSONL.
HISTORY_RATE_SAMPLES = 120


def ensure_utf8_stdout():
    """Force sys.stdout to UTF-8 when the current encoding can't render app glyphs.

    Windows default console encodings (cp1250/cp1252) can't handle em-dashes,
    box-drawing, or CJK. Each entry point (statusline/monitor/update) calls
    this at the top of main() before any write.
    """
    try:
        is_utf8 = sys.stdout.encoding and codecs.lookup(sys.stdout.encoding).name == "utf-8"
    except LookupError:
        is_utf8 = False
    if is_utf8:
        return
    sys.stdout.flush()
    sys.stdout = open(
        sys.stdout.fileno(), mode="w", encoding="utf-8",
        errors="replace", closefd=False,
    )


def _env_pct(name, default):
    """Parse a percentage env var as float, fall back on empty/invalid input."""
    try:
        v = os.environ.get(name, "")
        if v:
            return float(v)
    except (ValueError, TypeError):
        pass
    return default


# Percentage thresholds shared by statusline + monitor (SSoT, previously parsed twice).
WARN_PCT = _env_pct("CLAUDE_STATUS_WARN", 50.0)
CRIT_PCT = _env_pct("CLAUDE_STATUS_CRIT", 80.0)

# Python files that ship with the app — used by syntax-check paths in update flow
# (monitor.py:_apply_update_worker + update.py:apply_update). Must stay in sync.
PY_FILES = ("monitor.py", "statusline.py", "shared.py", "pulse.py", "update.py")

# ANSI — Nord truecolor (shared palette for statusline.py + monitor.py)
E = "\033["
R = E + "0m"
B = E + "1m"
C_RED = E + "38;2;191;97;106m"
C_GRN = E + "38;2;163;190;140m"
C_YEL = E + "38;2;235;203;139m"
C_ORN = E + "38;2;208;135;112m"  # nord12 aurora orange — cost/finance
C_CYN = E + "38;2;136;192;208m"
C_WHT = E + "38;2;216;222;233m"
C_DIM = E + "38;2;76;86;106m"


_CONTEXT_SUFFIX_RE = re.compile(r"\s*\((\d+\w?)\s*context\)")


def strip_context_suffix(name: str) -> str:
    """Remove '(Nk context)' / '(NM context)' entirely. 'Opus 4.7 (1M context)' -> 'Opus 4.7'."""
    return _CONTEXT_SUFFIX_RE.sub("", name).strip()


def compact_context_suffix(name: str) -> str:
    """Compact to trailing unit. 'Opus 4.7 (1M context)' -> 'Opus 4.7 1M'."""
    return _CONTEXT_SUFFIX_RE.sub(r" \1", name).strip()


def badge_context_suffix(name: str) -> str:
    """Compact the verbose context suffix to a parenthesised badge for the
    dashboard header: 'Opus 4.7 (1M context)' -> 'Opus 4.7 (1M CTX)'.
    Generalises the previously hardcoded '(1M context)' literal to any unit
    (e.g. a future '(200k context)' becomes '(200k CTX)')."""
    return _CONTEXT_SUFFIX_RE.sub(r" (\1 CTX)", name).strip()


def _num(v, default=0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def load_history(sid: str, n: int = HISTORY_RATE_SAMPLES, data_dir: Optional[pathlib.Path] = None) -> List[dict]:
    """Read last n JSONL history entries for session `sid`.

    Single source of truth for both monitor.py (BRN/CTR dashboard rates) and
    statusline.py (pre-write rate computation). Returns list of parsed dicts
    (best-effort — malformed lines skipped, not raised).
    Returns [] on invalid SID, unsafe data dir, or read failure.

    `data_dir` defaults to the shared DATA_DIR constant; callers can pass their
    own (monitor.py / statusline.py do, so test monkey-patching of their module
    DATA_DIR continues to work).
    """
    dd = data_dir if data_dir is not None else DATA_DIR
    sid_s = str(sid)
    if not _SID_RE.match(sid_s) or sid_s in RESERVED_SIDS:
        return []
    if not is_safe_dir(dd):
        return []
    raw = safe_read(dd / f"{sid_s}.jsonl", HISTORY_READ_MAX)
    if raw is None:
        return []
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


def safe_read(path, max_bytes: int) -> Optional[bytes]:
    """Bounded read — returns bytes or None. Never reads more than max_bytes + 1.

    Closes the size TOCTOU gap: caller doesn't have to stat first, and even if
    they do, an attacker growing the file between stat and read cannot exceed
    max_bytes. Does NOT validate containment — symlinks and junctions are
    followed as Python's default open() does. Callers must pre-validate paths
    (e.g. via is_safe_dir / _safe_transcript_path) when that matters.
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
    d, rem = divmod(diff, SECONDS_1D)
    h, rem = divmod(rem, SECONDS_1H)
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
    """Create data dir with 0o700 and verify safety. Returns True if usable.

    Unix only: verifies the directory is owned by the current effective UID
    (S-P2-1, CWE-377/732). On a multi-user host, an attacker who can predict
    `$TMPDIR/claude-aio-monitor/` could pre-create the directory under their
    own UID; subsequent writes by the legitimate user would then create
    files inside an attacker-owned dir, exposing IPC snapshots and JSONL
    history. We refuse to use the dir if ownership doesn't match.
    """
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
        # UID ownership guard (S-P2-1): refuse to use a directory created by
        # another user. `os.geteuid` is Unix-only and platform-guarded.
        if hasattr(os, "geteuid"):
            try:
                if d.stat().st_uid != os.geteuid():
                    return False
            except OSError:
                return False
    return True


_CHANGELOG_ENTRY_RE_TEMPLATE = r"## v{}\b.*?(?=\n## v|\Z)"


def extract_changelog_entry(text: str, version: str, max_lines: Optional[int] = None) -> str:
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


def run_git(args: Iterable[str], cwd, timeout: float = 15) -> "subprocess.CompletedProcess[str]":
    """Safe git invocation with minimal env whitelist.
    Returns subprocess.CompletedProcess. Raises FileNotFoundError if git missing."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout, env=_git_env(),
    )


# ---------------------------------------------------------------------------
# Update / lifecycle helpers — extracted from monitor.py + update.py to remove
# parallel implementations. CLI (update.py) and TUI (monitor.py) consume these
# differently (CLI: sys.exit on error; TUI: warn-and-continue), but the
# underlying parsing / IO is identical.
# ---------------------------------------------------------------------------


def check_syntax_after_pull(repo_root: pathlib.Path, py_files: Optional[Iterable[str]] = None) -> List[str]:
    """Compile each .py file under ``repo_root`` to catch syntax errors after
    a self-update. Returns a list of relative filenames that failed to compile
    (empty when all files pass). Files missing from disk are silently skipped;
    unreadable / oversized files (>MAX_FILE_SIZE) count as failures.

    Shared by update.py:apply_update() (CLI) and monitor.py:_apply_update_worker()
    (TUI background thread) — they used to carry byte-for-byte duplicate loops.
    """
    if py_files is None:
        py_files = PY_FILES
    bad = []
    for f in py_files:
        fp = repo_root / f
        if not fp.exists():
            continue
        raw = safe_read(fp, MAX_FILE_SIZE)
        if raw is None:
            bad.append(f)
            continue
        try:
            compile(raw.decode("utf-8", errors="replace"), str(fp), "exec")
        except SyntaxError:
            bad.append(f)
    return bad


def parse_ahead_behind(rev_list_output: str) -> Tuple[int, int]:
    """Parse the output of ``git rev-list --left-right --count HEAD...origin/main``
    and return ``(ahead, behind)``. Raises ValueError if the output cannot be
    parsed into two integers.

    Left side = HEAD = ahead. Right side = origin/main = behind.
    Callers that need ``(behind, ahead)`` (e.g. update.py:get_ahead_behind)
    can swap on the return — keeping the parser canonical here.
    """
    parts = rev_list_output.strip().split()
    if len(parts) != 2:
        raise ValueError(f"Unexpected rev-list output: {rev_list_output!r}")
    return int(parts[0]), int(parts[1])


def rotate_crash_log(path: pathlib.Path, max_bytes: int = MAX_FILE_SIZE, always: bool = False) -> None:
    """Rotate ``path`` to ``path.1`` when size exceeds ``max_bytes``, or
    unconditionally when ``always=True``.

    ``always=True`` preserves the prior crash even when the current log is
    well under ``max_bytes`` — otherwise two crashes in quick succession (both
    small) would silently overwrite the first via ``open("w")``. Callers that
    only care about disk-growth bounds keep the default.

    Best-effort: any OSError is silently swallowed so the calling crash-log
    writer never crashes the process it is trying to record. Drops any
    pre-existing ``path.1`` before rotating. Idempotent on missing files.
    """
    try:
        if not path.exists():
            return
        if not always and path.stat().st_size <= max_bytes:
            return
        backup = path.with_suffix(path.suffix + ".1")
        if backup.exists():
            try:
                backup.unlink()
            except OSError:
                pass
        path.replace(backup)
    except OSError:
        pass


def atomic_write_text(target, data, *, writelines: bool = False) -> bool:
    """Atomically write UTF-8 text to ``target`` via an unpredictable temp file
    in the same directory, then ``os.replace()`` onto the target (a same-
    filesystem rename — atomic on POSIX and Windows). Returns ``True`` on
    success, ``False`` on any OSError.

    ``data`` is a single string by default; pass ``writelines=True`` to write an
    iterable of strings (each must already carry its own newline). On failure
    the temp file is closed and unlinked so a botched write never leaks a
    ``.tmp`` file. Best-effort by design — callers that must keep two files
    aligned (e.g. snapshot vs history) branch on the bool return.

    ``target.parent`` must already exist and be writable; callers run
    ``ensure_data_dir`` first. Consolidates the previously-duplicated temp-file
    scaffold in statusline.write_shared_state / _trim_history and
    pulse._atomic_replace_log (M-2).
    """
    target = pathlib.Path(target)
    fd = None
    try:
        fd = tempfile.NamedTemporaryFile(
            dir=str(target.parent), suffix=".tmp", delete=False,
            mode="w", encoding="utf-8",
        )
        if writelines:
            fd.writelines(data)
        else:
            fd.write(data)
        fd.close()
        pathlib.Path(fd.name).replace(target)
        return True
    except OSError:
        if fd is not None:
            try:
                fd.close()
            except OSError:
                pass
            try:
                os.unlink(fd.name)
            except OSError:
                pass
        return False


def acquire_singleton_lock(lock_path) -> Optional[IO]:
    """Try to acquire an exclusive non-blocking file lock at ``lock_path``.

    Returns the open file handle on success — the caller MUST keep a strong
    reference (typically a module-level variable) so the lock is held for the
    process lifetime. The OS releases the lock when the process exits.

    Returns None if another process already holds the lock, or if the lock
    file cannot be opened. Cross-platform: msvcrt on Windows, fcntl elsewhere.
    Best-effort PID write into the file for human inspection — failure to
    write the PID does not invalidate the held lock.
    """
    try:
        fh = open(lock_path, "a+")
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            # msvcrt.locking requires the byte range to actually exist in the
            # file. On a fresh / empty lock file LK_NBLCK on byte 0 is OS-
            # dependent; write a placeholder byte first so the lock has a
            # region to grab regardless of prior file size.
            try:
                fh.seek(0, 2)  # end of file
                if fh.tell() == 0:
                    fh.write("\0")
                    fh.flush()
                fh.seek(0)
            except OSError:
                pass
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        try:
            fh.close()
        except OSError:
            pass
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    return fh


def calc_rates(hist: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    """Return (brn $/min, ctr %/min) from history, or (None, None) if insufficient data."""
    # Drop non-dict rows up front so one corrupt entry can't void the whole
    # computation, then order by time: the first/last entries drive the rate, and
    # callers' append-ordered JSONL can be scrambled by a wall-clock step backwards
    # (NTP adjustment between two snapshots), which would otherwise compute the
    # rate over the wrong interval.
    hist = sorted((e for e in hist if isinstance(e, dict)), key=lambda e: _num(e.get("t"), 0))
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
    # `or {}` handles explicit JSON null on disk (default {} only triggers on missing key).
    c0 = _num((hist[0].get("cost") or {}).get("total_cost_usd"))
    c1 = _num((hist[-1].get("cost") or {}).get("total_cost_usd"))
    x0 = _num((hist[0].get("context_window") or {}).get("used_percentage"))
    x1 = _num((hist[-1].get("context_window") or {}).get("used_percentage"))
    brn = (c1 - c0) / dt * 60 if c1 >= c0 else None
    ctr = (x1 - x0) / dt * 60 if x1 >= x0 else None
    return brn, ctr
