#!/usr/bin/env python3
"""Claude AIO Monitor — fullscreen TUI dashboard for Claude Code.

Terminal dashboard (monitor.py + shared.py). Stdlib only.
Reads shared state from statusline.py via temp files.

Usage:
    python monitor.py                     # auto-detect session
    python monitor.py --session ID        # specific session
    python monitor.py --list              # list active sessions
    python monitor.py --refresh 1000      # custom refresh interval (ms)
"""

import argparse
import atexit
import codecs
import json
import os
import pathlib
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime

from shared import (calc_rates, _num, _sanitize, f_tok, f_cost, f_dur, f_cd,
                    char_width, is_safe_dir, run_git,
                    _SID_RE, _ANSI_RE, MAX_FILE_SIZE, TRANSCRIPT_MAX_BYTES,
                    DATA_DIR, VERSION_RE,
                    RESERVED_SIDS, strip_context_suffix, compact_context_suffix,
                    extract_changelog_entry,
                    E, R, B, C_RED, C_GRN, C_YEL, C_ORN, C_CYN, C_WHT, C_DIM)
import pulse

# ---------------------------------------------------------------------------
# Transcript usage scanner — reads ~/.claude/projects/**/*.jsonl
# ---------------------------------------------------------------------------
_CLAUDE_DIR = pathlib.Path.home() / ".claude" / "projects"
_usage_cache = {}


def _parse_ts(ts_str):
    """Parse ISO timestamp to epoch, 3.8 compatible. Returns 0 on failure."""
    if not ts_str:
        return 0
    try:
        # Strip timezone suffix for 3.8 compat: Z, +HH:MM, -HH:MM
        clean = ts_str.replace("Z", "")
        # Remove +/-offset after the time portion (T required)
        t_pos = clean.find("T")
        if t_pos >= 0:
            tail = clean[t_pos + 1:]
            for sep in ("+", "-"):
                idx = tail.rfind(sep)
                # Offset separator is after HH:MM:SS (idx >= 8)
                if idx >= 8:
                    clean = clean[:t_pos + 1 + idx]
                    break
        return datetime.fromisoformat(clean).timestamp()
    except (ValueError, TypeError):
        return 0


def scan_transcript_stats(period="all", ttl=30.0):
    """Scan CC session transcripts, return (models, overview) tuple.

    models: {model_id: {"input": int, "output": int, "cache_read": int, "cache_write": int, "calls": int}}
    overview: {"sessions": int, "active_days": set, "longest_dur_ms": float,
               "first_date": str, "daily_tokens": {date_str: int}}
    """
    mono = time.monotonic()
    cached = _usage_cache.get(period)
    if cached and mono - cached["t"] < ttl:
        return cached["models"], cached["overview"]

    cutoff = 0
    wall = time.time()
    if period == "7d":
        cutoff = wall - 7 * 86400
    elif period == "30d":
        cutoff = wall - 30 * 86400

    models = {}
    active_days = set()
    daily_tokens = {}
    session_times = {}  # sid -> (first_ts, last_ts)
    session_count = 0

    if not is_safe_dir(_CLAUDE_DIR):
        empty_ov = {"sessions": 0, "active_days": set(), "longest_dur_ms": 0,
                     "first_date": None, "daily_tokens": {}, "truncated": False}
        return models, empty_ov

    _file_count = 0
    _truncated = False
    for jl in _CLAUDE_DIR.glob("**/*.jsonl"):
        _file_count += 1
        if _file_count > 1000:
            _truncated = True
            break
        # Skip subagent transcripts for session counting
        is_subagent = "subagents" in str(jl)
        try:
            st = jl.stat()
            if st.st_size > TRANSCRIPT_MAX_BYTES:
                continue
            if cutoff and st.st_mtime < cutoff:
                continue
        except OSError:
            continue

        sid = jl.stem
        if not is_subagent:
            session_count += 1

        try:
            with open(jl, encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts_str = obj.get("timestamp", "")
                    ts = _parse_ts(ts_str)

                    if cutoff and ts > 0 and ts < cutoff:
                        continue

                    # Track session timestamps for duration calc
                    if ts > 0 and not is_subagent:
                        if sid not in session_times:
                            session_times[sid] = [ts, ts]
                        else:
                            if ts < session_times[sid][0]:
                                session_times[sid][0] = ts
                            if ts > session_times[sid][1]:
                                session_times[sid][1] = ts

                    if obj.get("type") != "assistant":
                        continue
                    msg = obj.get("message")
                    if not msg or "usage" not in msg:
                        continue

                    model = msg.get("model", "unknown")
                    if model.startswith("<") or not model:
                        continue  # skip synthetic/internal entries
                    u = msg["usage"]
                    inp = int(_num(u.get("input_tokens", 0)))
                    out = int(_num(u.get("output_tokens", 0)))
                    cr = int(_num(u.get("cache_read_input_tokens", 0)))
                    cw = int(_num(u.get("cache_creation_input_tokens", 0)))
                    if model not in models:
                        models[model] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
                    models[model]["input"] += inp
                    models[model]["output"] += out
                    models[model]["cache_read"] += cr
                    models[model]["cache_write"] += cw
                    models[model]["calls"] += 1

                    # Track active days and daily tokens
                    if ts > 0:
                        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                        active_days.add(day)
                        daily_tokens[day] = daily_tokens.get(day, 0) + inp + out + cr + cw
        except (OSError, UnicodeDecodeError):
            continue

    # Compute longest session duration
    longest_dur_ms = 0
    for s_ts in session_times.values():
        dur = (s_ts[1] - s_ts[0]) * 1000
        if dur > longest_dur_ms:
            longest_dur_ms = dur

    # First date
    first_date = min(active_days) if active_days else None

    overview = {
        "sessions": session_count,
        "active_days": active_days,
        "longest_dur_ms": longest_dur_ms,
        "first_date": first_date,
        "daily_tokens": daily_tokens,
        "truncated": _truncated,
    }

    _usage_cache[period] = {"t": mono, "models": models, "overview": overview}
    return models, overview


def _calc_streaks(active_days):
    """Calculate current and longest streak from a set of date strings."""
    if not active_days:
        return 0, 0
    from datetime import timedelta
    days = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in active_days)
    today = datetime.now().date()

    longest = 1
    current_run = 1
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days == 1:
            current_run += 1
            longest = max(longest, current_run)
        else:
            current_run = 1

    # Current streak: count backwards from today
    current = 0
    check = today
    for d in reversed(days):
        if d == check:
            current += 1
            check -= timedelta(days=1)
        elif d < check:
            break
    return current, longest

# ---------------------------------------------------------------------------
# Platform — keyboard input abstraction
# ---------------------------------------------------------------------------
IS_WIN = platform.system() == "Windows"
_term_state = None

if IS_WIN:
    import ctypes
    import msvcrt

    def poll_key():
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\x00", b"\xe0"):
                msvcrt.getch()
                return None
            return ch.decode("utf-8", errors="ignore")
        return None

    def _setup_term():
        """Enable VT/ANSI processing on Windows console.

        If SetConsoleMode fails (pre-Win10, or redirected handle), ANSI
        sequences render as raw text — best-effort, can't recover here.
        """
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            if handle == -1 or handle == 0:
                return
            mode = ctypes.c_ulong(0)
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return
            new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
            if not kernel32.SetConsoleMode(handle, new_mode):
                # SetConsoleMode returned 0 — likely pre-Win10 or unsupported handle.
                # Fall through: caller's ANSI output may render as raw escape sequences.
                return
        except Exception:
            pass

    def _restore_term():
        pass

else:
    import select
    import termios
    import tty

    def poll_key():
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            return sys.stdin.read(1)
        return None

    def _setup_term():
        global _term_state
        try:
            _term_state = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except (termios.error, OSError):
            pass

    def _restore_term():
        if _term_state is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _term_state)
            except (termios.error, OSError):
                pass


# ---------------------------------------------------------------------------
# ANSI — E, R, B, C_RED..C_DIM imported from shared.py
# ---------------------------------------------------------------------------
HIDE_CUR = E + "?25l"
SHOW_CUR = E + "?25h"
ALT_ON = E + "?1049h"
ALT_OFF = E + "?1049l"
CLR = E + "2J"
HOME = E + "H"
EL = E + "K"
SYNC_ON = E + "?2026h"
SYNC_OFF = E + "?2026l"

C_FG = E + "38;2;180;186;200m"  # monitor-only: default foreground
BG_BAR = E + "48;2;46;52;64m"  # Nord polar night — header/bar background

VERSION = "1.10.1"
STALE_THRESHOLD = 1800  # 30 min — Claude Code emits no events during idle
DEAD_SESSION_TTL = 172800  # 48h — auto-purge dead session files from temp dir

def _env_float(name, default):
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default


WARN_BRN = _env_float("CLAUDE_WARN_BRN", 3.0)
WARN_PCT = _env_float("CLAUDE_STATUS_WARN", 50.0)
CRIT_PCT = _env_float("CLAUDE_STATUS_CRIT", 80.0)

# Reset-countdown color flip — 50% of window remaining (NOT the warn threshold)
RESET_HALFWAY_PCT = 50.0


def truncate(s, maxw):
    """Truncate string to maxw visible columns, preserving ANSI codes. CJK-aware."""
    vis = 0
    i = 0
    truncated = False
    while i < len(s) and vis < maxw:
        m = _ANSI_RE.match(s, i)
        if m:
            i = m.end()
        else:
            w = char_width(s[i])
            if vis + w > maxw:
                break
            vis += w
            i += 1
    if i < len(s):
        truncated = True
    # Include any trailing ANSI reset sequences
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            i = m.end()
        else:
            break
    result = s[:i]
    # Append reset if truncated mid-color to prevent bleed
    if truncated and R not in result[max(0, len(result) - 10):]:
        result += R
    return result


# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------
BF = "\u2588"  # █
SH = "\u2591"  # ░

BAR_W = 25     # fixed bar width for ALL metrics


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Progress bar — enclosed [████░░░░], fixed width, color by threshold
# ---------------------------------------------------------------------------
def mkbar(pct, color=None, show_pct=True):
    """Returns colored [████░░░░░]  XX.X%"""
    pct = max(0.0, min(100.0, pct))
    if color is None:
        if pct >= CRIT_PCT:
            color = C_RED
        elif pct >= WARN_PCT:
            color = C_YEL
        else:
            color = C_GRN
    filled = round(pct * BAR_W / 100)
    empty = BAR_W - filled
    bar = (
        f"{C_DIM}[{R}"
        f"{color}{BF * filled}{R}"
        f"{C_DIM}{SH * empty}{R}"
        f"{C_DIM}]{R}"
    )
    if show_pct:
        bar += f" {color}{pct:.1f}%{R}"
    return bar


def _limit_color(pct):
    """Dynamic color for rate limit metrics — yellow base, red >= CRIT_PCT."""
    if pct >= CRIT_PCT:
        return C_RED
    return C_YEL


def _reset_color(resets_epoch, window_secs):
    """Color for reset countdown — green=close to reset, red=far from reset."""
    if resets_epoch <= 0:
        return C_DIM
    remaining = resets_epoch - time.time()
    if remaining <= 0:
        return C_GRN  # just reset
    pct_remaining = remaining / window_secs * 100
    if pct_remaining > RESET_HALFWAY_PCT:
        return C_RED
    if pct_remaining > 20:
        return C_YEL
    return C_GRN


# ---------------------------------------------------------------------------
# Fixed-range bar for rate/cost metrics
# ---------------------------------------------------------------------------
BRN_MAX = _env_float("CC_MON_BRN_MAX", 10.0)   # $/min ceiling
CTR_MAX = _env_float("CC_MON_CTR_MAX", 10.0)   # %/min ceiling
CST_MAX = _env_float("CC_MON_CST_MAX", 1000.0) # $ ceiling


# ---------------------------------------------------------------------------
# Smart warnings
# ---------------------------------------------------------------------------
def collect_warnings(data, cpm, xpm):
    """Returns list of warning label strings for active conditions."""
    warnings = []
    # CTF — context filling fast
    if xpm and xpm > 0:
        ctx_pct = _num(data.get("context_window", {}).get("used_percentage"))
        if ctx_pct < 100:
            eta_mins = (100 - ctx_pct) / xpm
            if eta_mins < 30:
                warnings.append(f"CTF <{max(1, int(eta_mins))}m")
    # BRN
    if cpm and cpm > WARN_BRN:
        warnings.append(f"BRN {cpm:.4f}$/min")
    return warnings


# ---------------------------------------------------------------------------
# Cross-session cost aggregation
# ---------------------------------------------------------------------------
# Main-thread only — read/written exclusively from render loop. No lock needed.
# (Unlike _rls_cache which IS locked because a daemon thread updates it.)
_cost_cache = {"t": 0.0, "today": 0.0, "week": 0.0}


# ---------------------------------------------------------------------------
# RLS — background release check
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).parent.resolve()
_RLS_TTL = 3600  # check once per hour
_RLS_BLINK_INTERVAL = 0.5
_rls_cache = {"t": -_RLS_TTL, "status": None, "remote_ver": None}
_rls_lock = threading.Lock()       # worker-spawn coordination (one check at a time)
_rls_data_lock = threading.Lock()  # cache field coherence across read/write threads
_rls_blink_last = 0.0
_rls_blink_on = True


def _rls_snapshot():
    """Thread-safe atomic snapshot of _rls_cache. Returns a shallow dict copy."""
    with _rls_data_lock:
        return dict(_rls_cache)


def _rls_write(status, remote_ver=None):
    """Thread-safe write of all three _rls_cache fields in one critical section."""
    with _rls_data_lock:
        _rls_cache["t"] = time.monotonic()
        _rls_cache["status"] = status
        _rls_cache["remote_ver"] = remote_ver
def _parse_version(ver_str):
    """Parse version string to comparable tuple, ignoring non-numeric suffixes."""
    parts = []
    for p in ver_str.split("."):
        m = re.match(r"(\d+)", p)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts)


def _rls_check_worker():
    """Background worker: git fetch + compare VERSION. Writes result atomically.
    Uses shared.run_git for consistent env whitelist (blocks GIT_SSH_COMMAND / LD_PRELOAD).
    Writes via _rls_write() to keep three fields coherent under _rls_data_lock."""
    try:
        r = run_git(["fetch", "origin", "main"], cwd=_REPO_ROOT, timeout=15)
        if r.returncode != 0:
            _rls_write("error")
            return
        r = run_git(["show", "origin/main:monitor.py"], cwd=_REPO_ROOT, timeout=15)
        if r.returncode != 0:
            _rls_write("error")
            return
        m = VERSION_RE.search(r.stdout)
        if not m:
            _rls_write("error")
            return
        remote_ver = m.group(1)
        local_t = _parse_version(VERSION)
        remote_t = _parse_version(remote_ver)
        status = "update" if remote_t > local_t else "ok"
        _rls_write(status, remote_ver=remote_ver)
    except FileNotFoundError:
        _rls_write("no_git")
    except subprocess.TimeoutExpired:
        _rls_write("timeout")
    except Exception:
        _rls_write("error")
    finally:
        _rls_lock.release()


def _rls_maybe_check():
    """Trigger background check if TTL expired. Non-blocking.

    Lock ownership: acquired here, released by _rls_check_worker's `finally`
    OR here if the Thread spawn itself fails. Worker must not return
    without releasing. Keep in sync with _rls_check_worker:524.
    """
    if os.environ.get("CC_AIO_MON_NO_UPDATE_CHECK") == "1":
        return
    if time.monotonic() - _rls_snapshot()["t"] < _RLS_TTL:
        return
    if not _rls_lock.acquire(blocking=False):
        return
    try:
        t = threading.Thread(target=_rls_check_worker, daemon=True)
        t.start()
    except Exception:
        _rls_lock.release()


def _rls_blink():
    """Toggle blink state for update-available indicator."""
    global _rls_blink_last, _rls_blink_on
    now = time.monotonic()
    if now - _rls_blink_last >= _RLS_BLINK_INTERVAL:
        _rls_blink_on = not _rls_blink_on
        _rls_blink_last = now
    return _rls_blink_on


def calc_cross_session_costs():
    """Aggregate cost across all sessions for today and this week."""
    if not DATA_DIR.exists() or not is_safe_dir(DATA_DIR):
        return 0.0, 0.0
    today_start = datetime.combine(datetime.today().date(), datetime.min.time()).timestamp()
    week_start = today_start - 6 * 86400
    today_total = 0.0
    week_total = 0.0
    for jl in DATA_DIR.glob("*.jsonl"):
        sid = jl.stem
        if not _SID_RE.match(sid):
            continue
        if sid in RESERVED_SIDS:
            continue
        try:
            st = jl.stat()
            if st.st_size > MAX_FILE_SIZE * 10:
                continue
            raw = jl.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        entries = []
        for ln in raw.splitlines():
            try:
                entries.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        if not entries:
            continue
        entries.sort(key=lambda e: _num(e.get("t"), 0))
        # Today cost = last entry today - baseline (last entry before today, or first entry today)
        baseline_today = None
        final_today = 0.0
        first_today = None
        for e in entries:
            cost = _num(e.get("cost", {}).get("total_cost_usd"))
            if _num(e.get("t"), 0) < today_start:
                baseline_today = cost
            else:
                if first_today is None:
                    first_today = cost
                final_today = cost
        if baseline_today is None:
            baseline_today = first_today or 0.0
        today_total += max(0.0, final_today - baseline_today)
        # Week cost
        baseline_week = None
        final_week = 0.0
        first_week = None
        for e in entries:
            cost = _num(e.get("cost", {}).get("total_cost_usd"))
            if _num(e.get("t"), 0) < week_start:
                baseline_week = cost
            else:
                if first_week is None:
                    first_week = cost
                final_week = cost
        if baseline_week is None:
            baseline_week = first_week or 0.0
        week_total += max(0.0, final_week - baseline_week)
    return today_total, week_total


def cached_cross_session_costs(ttl=30.0):
    """Cached version — refreshes every ttl seconds."""
    now = time.monotonic()
    if now - _cost_cache["t"] < ttl:
        return _cost_cache["today"], _cost_cache["week"]
    today, week = calc_cross_session_costs()
    _cost_cache.update({"t": now, "today": today, "week": week})
    return today, week


# ---------------------------------------------------------------------------
# Layout helpers — no borders, just lines
# ---------------------------------------------------------------------------
def sep(w):
    return C_DIM + "-" * w + R


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def list_sessions():
    if not DATA_DIR.exists() or not is_safe_dir(DATA_DIR):
        return []
    now = time.time()
    # Clean up stale .tmp files (orphans from crashed writes)
    for tmp in DATA_DIR.glob("*.tmp"):
        try:
            if now - tmp.stat().st_mtime > 60:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
    # Auto-purge dead sessions older than 48h (.json + .jsonl pair)
    for f in DATA_DIR.glob("*.json"):
        sid = f.stem
        if sid in RESERVED_SIDS or not _SID_RE.match(sid):
            continue
        try:
            if now - f.stat().st_mtime > DEAD_SESSION_TTL:
                f.unlink(missing_ok=True)
                hist = DATA_DIR / f"{sid}.jsonl"
                hist.unlink(missing_ok=True)
        except OSError:
            pass
    sessions = []
    for f in DATA_DIR.glob("*.json"):
        sid = f.stem
        if not _SID_RE.match(sid):
            continue
        if sid in RESERVED_SIDS:
            continue
        try:
            st = f.stat()
            if st.st_size > MAX_FILE_SIZE:
                continue
            mt = st.st_mtime
            age = now - mt
            with open(f, "rb") as fh:
                raw = fh.read(MAX_FILE_SIZE + 1)
            if len(raw) > MAX_FILE_SIZE:
                continue
            d = json.loads(raw.decode("utf-8"))
            # Skip snapshots without usable model info (test artifacts / incomplete writes)
            display_name = _sanitize(d.get("model", {}).get("display_name", "")).strip()
            if not display_name:
                # Cleanup: dead artifact older than 1 hour
                if (now - mt) > 3600:
                    try:
                        f.unlink()
                        f.with_suffix(".jsonl").unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            sessions.append({
                "id": sid, "mtime": mt, "age": age,
                "stale": age > STALE_THRESHOLD,
                "model": display_name,
                "session_name": _sanitize(d.get("session_name", "")),
                "cwd": _sanitize(d.get("cwd", "")),
            })
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def load_state(sid):
    if not _SID_RE.match(str(sid)):
        return None
    if not is_safe_dir(DATA_DIR):
        return None
    try:
        p = DATA_DIR / f"{sid}.json"
        with open(p, "rb") as fh:
            raw = fh.read(MAX_FILE_SIZE + 1)
        if len(raw) > MAX_FILE_SIZE:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def load_history(sid, n=120):
    if not _SID_RE.match(str(sid)):
        return []
    if not is_safe_dir(DATA_DIR):
        return []
    try:
        p = DATA_DIR / f"{sid}.jsonl"
        with open(p, "rb") as fh:
            raw = fh.read(MAX_FILE_SIZE * 2 + 1)
        if len(raw) > MAX_FILE_SIZE * 2:
            return []
        lines = raw.decode("utf-8").splitlines()
        out = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        return out
    except (OSError, UnicodeDecodeError):
        return []


# ---------------------------------------------------------------------------
# Spinners
# ---------------------------------------------------------------------------
# Session spinner — braille dots, 10 frames, 80ms
_SPIN_SESSION = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_spin_session_idx = 0
_spin_session_last = 0.0
_SPIN_SESSION_INTERVAL = 0.08

# RLS spinner — pulse dot, 4 frames, 500ms
_SPIN_RLS = ["∙", "○", "●", "○"]
_spin_rls_idx = 0
_spin_rls_last = 0.0
_SPIN_RLS_INTERVAL = 0.5


def spin_session():
    """Return 1-char braille spinner frame for session status."""
    global _spin_session_idx, _spin_session_last
    now = time.monotonic()
    if now - _spin_session_last >= _SPIN_SESSION_INTERVAL:
        _spin_session_idx += 1
        _spin_session_last = now
    return _SPIN_SESSION[_spin_session_idx % len(_SPIN_SESSION)]


def spin_rls():
    """Return 1-char pulse spinner frame for RLS status."""
    global _spin_rls_idx, _spin_rls_last
    now = time.monotonic()
    if now - _spin_rls_last >= _SPIN_RLS_INTERVAL:
        _spin_rls_idx += 1
        _spin_rls_last = now
    return _SPIN_RLS[_spin_rls_idx % len(_SPIN_RLS)]


def _fit_buf_height(buf, rows, *, clip_tail=False):
    """Fit buffer to terminal height. Dashboard (clip_tail=False): protects last 2 lines (footer separator + keys). Legend/picker/stats (clip_tail=True): preserves header, clips from bottom."""
    try:
        rows = int(rows)
    except (TypeError, ValueError):
        rows = 24
    rows = max(1, rows)
    target = max(1, rows)
    tail = []
    if not clip_tail:
        n = min(2, max(0, target - 1))
        if n and len(buf) >= n:
            tail = buf[-n:]
            del buf[-n:]
    sub_target = target - len(tail)
    while len(buf) > sub_target:
        shrunk = False
        for i in range(len(buf) - 1, -1, -1):
            if buf[i] == "":
                buf.pop(i)
                shrunk = True
                break
        if not shrunk:
            break
    if len(buf) > sub_target:
        buf[:] = buf[:sub_target]
    while len(buf) < sub_target:
        buf.append("")
    buf.extend(tail)


# ---------------------------------------------------------------------------
# Render — main dashboard
# ---------------------------------------------------------------------------
def render_frame(data, hist, cols, rows, show_legend=False, show_menu=False, show_cost=False, stale=False):
    if show_menu:
        return render_menu(cols, rows)
    if show_cost:
        return render_cost_breakdown(data, hist, cols, rows)
    if show_legend:
        return render_legend(cols, rows)

    SW = cols
    buf = []

    # -- Extract data (sanitize to prevent terminal escape injection) --
    m = data.get("model", {})
    model_str = _sanitize(m.get("display_name", "?")).replace("(1M context)", "(1M CTX)")
    sname = _sanitize(data.get("session_name", ""))

    cw = data.get("context_window", {})
    ctx_pct = round(_num(cw.get("used_percentage")), 1)
    ctx_total = _num(cw.get("context_window_size"), 0)
    usage = cw.get("current_usage") or {}

    rl = data.get("rate_limits")
    cost_d = data.get("cost", {})
    usd = _num(cost_d.get("total_cost_usd"))
    dur = _num(cost_d.get("total_duration_ms"))
    api_dur = _num(cost_d.get("total_api_duration_ms"))
    added = int(_num(cost_d.get("total_lines_added")))
    removed = int(_num(cost_d.get("total_lines_removed")))
    cpm, xpm = calc_rates(hist)

    # -- Stale session: dim color palette, keep last known values --
    _C = C_DIM if stale else None  # override color when stale

    def c(normal):
        """Return dim color when stale, normal color otherwise."""
        return _C if _C else normal

    # ── Header ──────────────────────────────────────────────
    sid_str = str(data.get("session_id") or "default")
    session_label = sname or (sid_str[:16] if _SID_RE.match(sid_str) else "default")

    buf.append(sep(SW))
    hp_plain = f"CC AIO MON {VERSION}  {model_str}"
    hp_pad = max(0, SW - len(hp_plain))
    hp_text = f"{C_WHT}{B}CC AIO MON {VERSION}{R}{BG_BAR}  {C_CYN}{model_str}{R}{BG_BAR}"
    buf.append(f"{BG_BAR}{hp_text}{' ' * hp_pad}{R}")

    # ── Session status line (always visible) ────────────────
    if stale:
        _stale_age = ""
        _sid_safe = str(data.get("session_id") or "default")
        if _SID_RE.match(_sid_safe):
            try:
                _mt = (DATA_DIR / f"{_sid_safe}.json").stat().st_mtime
                _idle = int(time.time() - _mt)
                _stale_age = f" ({_idle // 60}m)" if _idle >= 60 else f" ({_idle}s)"
            except OSError:
                pass
        buf.append(f"{C_RED}{B}Session Inactive {spin_session()}{R}{_stale_age}  {c(C_FG)}{session_label}{R}")
    else:
        buf.append(f"{C_GRN}{B}Session Active {spin_session()}{R} {C_FG}{session_label}{R}")

    buf.append(sep(SW))

    # ── Smart warnings (suppressed when stale, blink 500ms) ──
    _warns = [] if stale else collect_warnings(data, cpm, xpm)
    if _warns:
        wc = f"{C_RED}{B}" if _rls_blink() else C_DIM
        warn_parts = [f"{wc}{w}{R}" for w in _warns]
        buf.append(f"{'   '.join(warn_parts)}")
        buf.append(sep(SW))

    inp = _num(usage.get("input_tokens", 0))
    out = _num(usage.get("output_tokens", 0))
    cr = _num(usage.get("cache_read_input_tokens", 0))
    cwt = _num(usage.get("cache_creation_input_tokens", 0))

    # ── APR — API Ratio ─────────────────────────────────────
    if dur > 0:
        apr_pct = min(100.0, round(api_dur / dur * 100, 1))
        buf.append(f"{c(C_GRN)}{B}APR{R} {mkbar(apr_pct, c(C_GRN))}")
        buf.append(f"    {C_DIM}DUR:{R} {c(C_WHT)}{f_dur(dur)}{R} {C_DIM}API:{R} {c(C_GRN)}{f_dur(api_dur)}{R}")
    else:
        buf.append(f"{c(C_GRN)}{B}APR{R} {mkbar(0, C_DIM)}")
    buf.append(sep(SW))

    # ── CHR — Cache Hit Rate ────────────────────────────────
    if any([cr, cwt]):
        total_cache = cr + cwt
        chr_pct = round(cr / total_cache * 100, 1) if total_cache > 0 else 0
        buf.append(f"{c(C_GRN)}{B}CHR{R} {mkbar(chr_pct, c(C_GRN))}")
        buf.append(f"    {C_DIM}CRD:{R} {c(C_GRN)}{f_tok(cr)}{R} {C_DIM}CWR:{R} {c(C_GRN)}{f_tok(cwt)}{R}")
    else:
        buf.append(f"{c(C_GRN)}{B}CHR{R} {mkbar(0, C_DIM)}")
    buf.append(sep(SW))

    # ── CTX ─────────────────────────────────────────────────
    ctx_used = int(ctx_total * ctx_pct / 100) if ctx_total else 0
    buf.append(f"{c(C_CYN)}{B}CTX{R} {mkbar(ctx_pct, c(C_CYN))}")
    warn = f" {c(C_RED)}{B}!CTX>{int(CRIT_PCT)}%{R}" if ctx_pct >= CRIT_PCT else ""
    if any([inp, out]):
        buf.append(f"    {c(C_CYN)}{f_tok(ctx_used)}{R}{warn} {C_DIM}INP:{R} {c(C_CYN)}{f_tok(inp)}{R} {C_DIM}OUT:{R} {c(C_CYN)}{f_tok(out)}{R}")
    else:
        buf.append(f"    {c(C_CYN)}{f_tok(ctx_used)}{R}{warn}")
    buf.append(sep(SW))

    # ── 5HL ─────────────────────────────────────────────────
    if rl is not None:
        fh = rl.get("five_hour")
        if fh:
            pct = round(_num(fh.get("used_percentage")), 1)
            resets = _num(fh.get("resets_at"), 0)
            expired = resets > 0 and resets < time.time()
            if expired:
                pct = 0.0
            lc = c(_limit_color(pct))
            expired_tag = f"  {C_DIM}(expired){R}" if expired else ""
            buf.append(f"{lc}{B}5HL{R} {mkbar(pct, lc)}{expired_tag}")
            rc = c(_reset_color(resets, 18000))  # 5h window
            buf.append(f"    {C_DIM}RST:{R} {rc}{f_cd(resets if resets > 0 else None)}{R}")

        # ── 7DL ─────────────────────────────────────────────
        sd = rl.get("seven_day")
        if sd:
            pct = round(_num(sd.get("used_percentage")), 1)
            resets = _num(sd.get("resets_at"), 0)
            expired = resets > 0 and resets < time.time()
            if expired:
                pct = 0.0
            lc = c(_limit_color(pct))
            expired_tag = f"  {C_DIM}(expired){R}" if expired else ""
            buf.append(f"{lc}{B}7DL{R} {mkbar(pct, lc)}{expired_tag}")
            rc = c(_reset_color(resets, 604800))  # 7d window
            buf.append(f"    {C_DIM}RST:{R} {rc}{f_cd(resets if resets > 0 else None)}{R}")

        if not fh and not sd:
            buf.append(f"{C_DIM}Rate limits: no data{R}")
    else:
        buf.append(f"{C_DIM}Rate limits: subscription data unavailable{R}")

    buf.append(sep(SW))

    # ── Stats (BRN/CTR/CST/TDY/WEK/NOW/UPD/LNS) ─────────────
    brn_val = f"{cpm:.4f} $/min" if cpm and cpm > 0.0001 else "collecting..."
    ctr_val = f"{xpm:.2f} %/min" if xpm and xpm > 0.001 else "--"
    now = datetime.now().strftime("%H:%M:%S")
    if _SID_RE.match(sid_str):
        try:
            mt = (DATA_DIR / f"{sid_str}.json").stat().st_mtime
            age = int(time.time() - mt)
            age_s = f"{age}s" if age < 120 else f"{age // 60}m"
        except OSError:
            age_s = "?"
    else:
        age_s = "?"
    # ── BRN — burn rate bar (scales to BRN_MAX $/min) ──────
    brn_pct = min(100, cpm / BRN_MAX * 100) if cpm and cpm > 0 else 0
    buf.append(f"{c(C_ORN)}{B}BRN{R} {mkbar(brn_pct, c(C_ORN))}")
    buf.append(f"    {C_DIM}RTE:{R} {c(C_ORN)}{brn_val}{R}")
    # ── CTR — context rate bar (scales to CTR_MAX %/min) ───
    ctr_pct = min(100, xpm / CTR_MAX * 100) if xpm and xpm > 0 else 0
    buf.append(f"{c(C_YEL)}{B}CTR{R} {mkbar(ctr_pct, c(C_YEL))}")
    buf.append(f"    {C_DIM}RTE:{R} {c(C_YEL)}{ctr_val}{R}")
    # ── CST — session cost bar (scales to CST_MAX $) ───────
    cst_pct = min(100, usd / CST_MAX * 100) if usd > 0 else 0
    buf.append(f"{c(C_ORN)}{B}CST{R} {mkbar(cst_pct, c(C_ORN))}")
    buf.append(f"    {C_DIM}CST:{R} {c(C_ORN)}{f_cost(usd)}{R}")
    # ── Cross-session cost (TDY / WEK) ─────────────────────
    tdy, wek = cached_cross_session_costs()
    tdy_s = f_cost(tdy) if tdy > 0 else "--"
    wek_s = f_cost(wek) if wek > 0 else "--"
    buf.append(f"    {C_DIM}TDY:{R} {c(C_ORN)}{tdy_s}{R} {C_DIM}WEK:{R} {c(C_ORN)}{wek_s}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}NOW:{R} {c(C_WHT)}{now}{R} {C_DIM}UPD:{R} {c(C_WHT)}{age_s}{R}")
    if added or removed:
        buf.append(f"{C_DIM}LNS:{R} {c(C_GRN)}{added:,}{R} {c(C_RED)}{removed:,}{R}")

    # ── RLS (release check) ────────────────────────────────
    _rls_maybe_check()
    rls = _rls_snapshot()  # atomic read: status + remote_ver coherent
    rls_s = rls["status"]
    if rls_s == "update" and rls["remote_ver"]:
        rv = rls["remote_ver"]
        if _rls_blink():
            buf.append(f"{c(C_RED)}{B}RLS{R} {c(C_RED)}{B}{spin_rls()} v{rv} available{R}")
        else:
            buf.append(f"{c(C_RED)}{B}RLS{R} {C_DIM}{spin_rls()} v{rv} available{R}")
    elif rls_s == "ok":
        buf.append(f"{c(C_GRN)}RLS{R} {c(C_GRN)}{spin_rls()} Up to date{R}")
    elif rls_s is None:
        buf.append(f"{C_DIM}RLS {spin_rls()} Checking...{R}")
    # error/no_git/timeout — ticho, nič nezobrazí

    # ── Footer ──────────────────────────────────────────────
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}m{R}{C_DIM}] menu{R}")

    _fit_buf_height(buf, rows, clip_tail=False)
    return buf


# ---------------------------------------------------------------------------
# Legend overlay
# ---------------------------------------------------------------------------
def render_legend(cols, rows):
    SW = cols
    buf = []
    buf.append(sep(SW))
    lg_pad = max(0, SW - 6)  # "LEGEND" = 6 chars
    buf.append(f"{BG_BAR}{C_WHT}{B}LEGEND{R}{BG_BAR}{' ' * lg_pad}{R}")
    buf.append(sep(SW))
    # ── Dashboard metrics ──
    buf.append(f"{C_GRN}APR{R} {C_DIM}API Ratio{R}")
    buf.append(f"{C_DIM} DUR  Duration - API  API Time{R}")
    buf.append(f"{C_GRN}CHR{R} {C_DIM}Cache Hit Rate{R}")
    buf.append(f"{C_DIM} CRD  Cache Read - CWR  Cache Write{R}")
    buf.append(f"{C_CYN}CTX{R} {C_DIM}Context Window{R}")
    buf.append(f"{C_DIM} INP  Input Tokens - OUT  Output Tokens{R}")
    buf.append(f"{C_YEL}5HL{R} {C_DIM}5-Hour Rate Limit{R}")
    buf.append(f"{C_YEL}7DL{R} {C_DIM}7-Day Rate Limit{R}")
    buf.append(f"{C_DIM} RST  Reset Countdown{R}")
    buf.append(f"{C_ORN}BRN{R} {C_DIM}Burn Rate{R} {C_DIM}0-{BRN_MAX} $/min{R}")
    buf.append(f"{C_YEL}CTR{R} {C_DIM}Context Rate{R} {C_DIM}0-{CTR_MAX} %/min{R}")
    buf.append(f"{C_DIM} RTE  Rate Value{R}")
    buf.append(f"{C_ORN}CST{R} {C_DIM}Session Cost{R} {C_DIM}0-{CST_MAX:.0f} ${R}")
    buf.append(f"{C_ORN}TDY{R} {C_DIM}Today Cost{R} {C_ORN}WEK{R} {C_DIM}7-Day Cost{R}")
    buf.append(f"{C_WHT}LNS{R} {C_DIM}Lines Changed{R} {C_GRN}+{R}{C_DIM}added{R} {C_RED}-{R}{C_DIM}removed{R}")
    buf.append(f"{C_WHT}NOW{R} {C_DIM}Current Time{R} {C_WHT}UPD{R} {C_DIM}Last Update{R}")
    buf.append(f"{C_WHT}RLS{R} {C_DIM}Release Status{R}")
    # ── Hotkeys ──
    buf.append(sep(SW))
    hp = max(0, SW - 7)
    buf.append(f"{BG_BAR}{C_WHT}{B}HOTKEYS{R}{BG_BAR}{' ' * hp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}]{R}   {C_DIM}Quit{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}m{R}{C_DIM}]{R}   {C_DIM}Menu{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}r{R}{C_DIM}]{R}   {C_DIM}Refresh{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}s{R}{C_DIM}]{R}   {C_DIM}Session Picker{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}t{R}{C_DIM}]{R}   {C_DIM}Token Stats{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}c{R}{C_DIM}]{R}   {C_DIM}Cost Breakdown{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}p{R}{C_DIM}]{R}   {C_DIM}Anthropic Pulse{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}u{R}{C_DIM}]{R}   {C_DIM}Update Manager{R} {C_DIM}a=apply{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}l{R}{C_DIM}]{R}   {C_DIM}Legend{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}1-9{R}{C_DIM}]{R} {C_DIM}Select Session / Period{R}")
    # ── Token Stats ──
    buf.append(sep(SW))
    tp = max(0, SW - 11)
    buf.append(f"{BG_BAR}{C_WHT}{B}TOKEN STATS{R}{BG_BAR}{' ' * tp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_WHT}SES{R} {C_DIM}Sessions{R} {C_WHT}DAY{R} {C_DIM}Active Days{R}")
    buf.append(f"{C_WHT}STK{R} {C_DIM}Streak{R} {C_WHT}LSS{R} {C_DIM}Longest Session{R}")
    buf.append(f"{C_WHT}TOP{R} {C_DIM}Most Active Day{R}")
    buf.append(f"{C_DIM} INP  Input - OUT  Output - CLS  Calls{R}")
    # ── Cost Breakdown ──
    buf.append(sep(SW))
    cp = max(0, SW - 14)
    buf.append(f"{BG_BAR}{C_WHT}{B}COST BREAKDOWN{R}{BG_BAR}{' ' * cp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM} LAST REQUEST — current message tokens{R}")
    buf.append(f"{C_ORN}INP{R} {C_DIM}Input Cost{R} {C_ORN}OUT{R} {C_DIM}Output Cost{R}")
    buf.append(f"{C_ORN}CRD{R} {C_DIM}Cache Read Cost{R} {C_ORN}CWR{R} {C_DIM}Cache Write Cost{R}")
    buf.append(f"{C_GRN}SAV{R} {C_DIM}Cache Savings{R}")
    buf.append(f"{C_DIM} SESSION BREAKDOWN — whole session, aggregated from transcript{R}")
    buf.append(f"{C_DIM} SUM  Sum of estimates (delta warn if >15% off CST){R}")
    buf.append(f"{C_WHT}TIN{R} {C_DIM}Total Input{R} {C_WHT}TOT{R} {C_DIM}Total Output{R}")
    buf.append(f"{C_ORN}CPM{R} {C_DIM}Cost/Min{R}")
    buf.append(f"{C_ORN}ERL{R} {C_DIM}Early 1/3{R} {C_ORN}MID{R} {C_DIM}Mid 1/3{R} {C_ORN}LAT{R} {C_DIM}Late 1/3{R}")
    # ── Update ──
    buf.append(sep(SW))
    up = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}UPDATE{R}{BG_BAR}{' ' * up}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_GRN}CUR{R} {C_DIM}Local Version{R} {C_WHT}REM{R} {C_DIM}Remote Version{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}press any key to close{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Cost breakdown modal
# ---------------------------------------------------------------------------
# Prices per 1M tokens (USD). Sources: official Anthropic pricing page.
# cache_write = 5-minute TTL price. 1h cache write adds ~60% — documented separately.
_MODEL_PRICING = {
    "claude-opus-4-7":             {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6":             {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5":             {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-1":             {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6":           {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5":           {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5-20251001":   {"input": 1.0,  "output": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-3-5":            {"input": 0.8,  "output": 4.0,  "cache_read": 0.08, "cache_write": 1.00},
}
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}  # Sonnet-tier fallback


def _get_pricing(model_id):
    """Get pricing for model, stripping suffixes like [1m]."""
    base = model_id.split("[")[0] if model_id else ""
    return _MODEL_PRICING.get(base, _DEFAULT_PRICING)


_SESSION_COST_CACHE = OrderedDict()  # LRU: {session_id: (ts, breakdown_dict)}
_SESSION_COST_CACHE_MAX = 64  # cap — prevents unbounded growth across rotating session IDs
_SESSION_COST_TTL = 5.0   # refresh every 5s

CLAUDE_PROJECTS_DIR = (pathlib.Path.home() / ".claude" / "projects").resolve()


def _safe_transcript_path(tp):
    """Validate that transcript path is a regular file inside ~/.claude/projects/.
    Rejects symlinks, relative escapes, and absolute paths outside the allowed root."""
    if not tp or not isinstance(tp, str):
        return None
    try:
        cand = pathlib.Path(tp)
        try:
            st = cand.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        try:
            resolved = cand.resolve(strict=True)
        except OSError:
            return None
        # Python 3.8 compat: is_relative_to not available
        try:
            resolved.relative_to(CLAUDE_PROJECTS_DIR)
        except ValueError:
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _aggregate_session_cost(data):
    """Walk the current session's transcript JSONL, sum per-category tokens
    across all assistant records, apply pricing per-record model.
    Returns dict {input, output, cache_read, cache_write, cost_total,
                  cost_input, cost_output, cost_cache_read, cost_cache_write}
    or None if transcript unreachable.
    """
    sid = (data.get("session_id") or "").strip()
    if not sid or not _SID_RE.match(sid):
        return None

    now = time.time()
    cached = _SESSION_COST_CACHE.get(sid)
    if cached and (now - cached[0]) < _SESSION_COST_TTL:
        _SESSION_COST_CACHE.move_to_end(sid)  # LRU touch
        return cached[1]

    tp = data.get("transcript_path")
    path = _safe_transcript_path(tp)
    if path is None:
        # Fallback: scan ~/.claude/projects/*/{sid}.jsonl (first match wins)
        try:
            home = CLAUDE_PROJECTS_DIR
            if is_safe_dir(home):
                for cand in home.glob(f"*/{sid}.jsonl"):
                    if cand.is_file():
                        path = _safe_transcript_path(str(cand))
                        if path:
                            break
        except OSError:
            path = None
    if path is None:
        return None

    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > TRANSCRIPT_MAX_BYTES:
        return None

    inp = out = cr = cw = 0.0
    ci = co = ccr = ccw = 0.0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                u = msg.get("usage") or {}
                mid = msg.get("model") or ""
                pricing = _get_pricing(mid)
                i = _num(u.get("input_tokens", 0))
                o = _num(u.get("output_tokens", 0))
                r = _num(u.get("cache_read_input_tokens", 0))
                w = _num(u.get("cache_creation_input_tokens", 0))
                inp += i; out += o; cr += r; cw += w
                ci += i * pricing["input"] / 1_000_000
                co += o * pricing["output"] / 1_000_000
                ccr += r * pricing["cache_read"] / 1_000_000
                ccw += w * pricing["cache_write"] / 1_000_000
    except OSError:
        return None

    result = {
        "input": int(inp), "output": int(out),
        "cache_read": int(cr), "cache_write": int(cw),
        "cost_input": ci, "cost_output": co,
        "cost_cache_read": ccr, "cost_cache_write": ccw,
        "cost_total": ci + co + ccr + ccw,
    }
    _SESSION_COST_CACHE[sid] = (now, result)
    _SESSION_COST_CACHE.move_to_end(sid)
    while len(_SESSION_COST_CACHE) > _SESSION_COST_CACHE_MAX:
        _SESSION_COST_CACHE.popitem(last=False)  # evict LRU
    return result


def _cost_thirds(hist):
    """Split session into 3 equal time slices. Returns list of (label, cost, rate_per_min) or []."""
    costs = []
    for entry in hist:
        t = _num(entry.get("t", 0))
        c = _num(entry.get("cost", {}).get("total_cost_usd", 0))
        if t > 0:
            costs.append((t, c))
    if len(costs) < 2:
        return []
    costs.sort(key=lambda x: x[0])
    t_start, t_end = costs[0][0], costs[-1][0]
    span = t_end - t_start
    if span < 30:  # need at least 30s of data
        return []
    import bisect
    times = [x[0] for x in costs]
    vals = [x[1] for x in costs]
    third = span / 3
    boundaries = [t_start, t_start + third, t_start + 2 * third, t_end]
    labels = ["early", "mid", "late"]
    result = []
    for i in range(3):
        idx_s = max(0, bisect.bisect_right(times, boundaries[i]) - 1)
        idx_e = max(0, bisect.bisect_right(times, boundaries[i + 1]) - 1)
        delta = max(0.0, vals[idx_e] - vals[idx_s])
        rate = delta / (third / 60) if third > 0 else 0.0  # $/min
        result.append((labels[i], delta, rate))
    return result


def render_cost_breakdown(data, hist, cols, rows):
    """Render session cost breakdown modal."""
    SW = cols
    buf = []
    buf.append(sep(SW))
    cb_pad = max(0, SW - 14)
    buf.append(f"{BG_BAR}{C_WHT}{B}COST BREAKDOWN{R}{BG_BAR}{' ' * cb_pad}{R}")
    buf.append(sep(SW))

    cost_d = data.get("cost", {})
    usd = _num(cost_d.get("total_cost_usd"))
    dur = _num(cost_d.get("total_duration_ms"))
    cw = data.get("context_window", {})
    usage = cw.get("current_usage") or {}
    model_id = data.get("model", {}).get("id", "")
    pricing = _get_pricing(model_id)

    # Token counts
    inp = _num(usage.get("input_tokens", 0))
    out = _num(usage.get("output_tokens", 0))
    cr = _num(usage.get("cache_read_input_tokens", 0))
    cwt = _num(usage.get("cache_creation_input_tokens", 0))
    total_in = _num(cw.get("total_input_tokens", 0))
    total_out = _num(cw.get("total_output_tokens", 0))

    model_name = _sanitize(data.get("model", {}).get("display_name", "?"))
    # Strip verbose context suffix: "Opus 4.6 (1M context)" → "Opus 4.6 1M"
    model_short = compact_context_suffix(model_name)
    buf.append(f"{C_ORN}{B}CST{R} {C_ORN}{B}{f_cost(usd)}{R} {C_DIM}{f_dur(dur)} - {model_short}{R}")

    # Cost estimates per token type
    inp_cost = inp * pricing["input"] / 1_000_000
    out_cost = out * pricing["output"] / 1_000_000
    cr_cost = cr * pricing["cache_read"] / 1_000_000
    cw_cost = cwt * pricing["cache_write"] / 1_000_000

    # What would cache reads cost at full input price?
    cr_full_price = cr * pricing["input"] / 1_000_000
    cache_savings = cr_full_price - cr_cost

    buf.append(sep(SW))
    tc_title = "LAST REQUEST (est.)"
    tc_pad = max(0, SW - len(tc_title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{tc_title}{R}{BG_BAR}{' ' * tc_pad}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_ORN}INP{R} {C_WHT}{f_tok(inp)}{R} {C_DIM}~{f_cost(inp_cost)}{R}")
    buf.append(f"{C_ORN}OUT{R} {C_WHT}{f_tok(out)}{R} {C_DIM}~{f_cost(out_cost)}{R}")
    buf.append(f"{C_ORN}CRD{R} {C_WHT}{f_tok(cr)}{R} {C_DIM}~{f_cost(cr_cost)}{R}")
    buf.append(f"{C_ORN}CWR{R} {C_WHT}{f_tok(cwt)}{R} {C_DIM}~{f_cost(cw_cost)}{R}")

    if cache_savings > 0.001:
        sav_pct = round(cache_savings / (cache_savings + cr_cost) * 100) if (cache_savings + cr_cost) > 0 else 0
        buf.append(f"{C_GRN}SAV{R} {C_GRN}~{f_cost(cache_savings)}{R} {C_DIM}({sav_pct}% vs uncached){R}")

    # Session-wide breakdown (aggregates transcript)
    sess = _aggregate_session_cost(data)
    if sess:
        buf.append(sep(SW))
        sb_title = "SESSION BREAKDOWN (est.)"
        sb_pad = max(0, SW - len(sb_title))
        buf.append(f"{BG_BAR}{C_WHT}{B}{sb_title}{R}{BG_BAR}{' ' * sb_pad}{R}")
        buf.append(sep(SW))
        buf.append(f"{C_ORN}INP{R} {C_WHT}{f_tok(sess['input'])}{R} {C_DIM}~{f_cost(sess['cost_input'])}{R}")
        buf.append(f"{C_ORN}OUT{R} {C_WHT}{f_tok(sess['output'])}{R} {C_DIM}~{f_cost(sess['cost_output'])}{R}")
        buf.append(f"{C_ORN}CRD{R} {C_WHT}{f_tok(sess['cache_read'])}{R} {C_DIM}~{f_cost(sess['cost_cache_read'])}{R}")
        buf.append(f"{C_ORN}CWR{R} {C_WHT}{f_tok(sess['cache_write'])}{R} {C_DIM}~{f_cost(sess['cost_cache_write'])}{R}")
        delta = sess["cost_total"] - usd if usd > 0 else 0
        if usd > 0:
            pct_diff = abs(delta) / usd * 100 if usd > 0 else 0
            if pct_diff > 15:
                buf.append(f"{C_DIM}SUM ~{f_cost(sess['cost_total'])} vs CST {f_cost(usd)} — {C_YEL}delta {pct_diff:.0f}%{R}")
            else:
                buf.append(f"{C_DIM}SUM ~{f_cost(sess['cost_total'])} (~= CST {f_cost(usd)}){R}")

    buf.append(sep(SW))
    st_title = "SESSION TOTALS"
    st_pad = max(0, SW - len(st_title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{st_title}{R}{BG_BAR}{' ' * st_pad}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}TIN:{R} {C_WHT}{f_tok(total_in)}{R} {C_DIM}TOT:{R} {C_WHT}{f_tok(total_out)}{R}")
    if dur > 0:
        cpm_val = usd / (dur / 60000)
        buf.append(f"{C_DIM}CPM:{R} {C_ORN}{cpm_val:.4f} $/min{R}")

    # Burn rate over time — 3 equal time slices, bar scaled to BRN_MAX
    thirds = _cost_thirds(hist)
    if thirds:
        br_title = f"BURN RATE OVER TIME (0-{BRN_MAX} $/min)"
        br_pad = max(0, SW - len(br_title))
        buf.append(sep(SW))
        buf.append(f"{BG_BAR}{C_WHT}{B}{br_title}{R}{BG_BAR}{' ' * br_pad}{R}")
        buf.append(sep(SW))
        _COT_LABELS = {"early": "ERL", "mid": "MID", "late": "LAT"}
        for label, cost, rate in thirds:
            pct = min(100.0, rate / BRN_MAX * 100) if BRN_MAX > 0 else 0
            code = _COT_LABELS.get(label, label.upper()[:3])
            buf.append(f"{C_ORN}{B}{code}{R} {mkbar(pct, C_ORN)}")
            buf.append(f"    {C_DIM}RTE:{R} {C_ORN}{rate:.4f} $/min{R} {C_DIM}CST:{R} {C_ORN}{f_cost(cost)}{R}")

    buf.append(sep(SW))
    buf.append(f"{C_DIM}press any key to close{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Anthropic Pulse modal
# ---------------------------------------------------------------------------
_PULSE_LEVEL_COLOR = {
    "ok":       C_GRN,
    "degraded": C_YEL,
    "bad":      C_RED,
    "error":    C_DIM,
}

_PULSE_COMPONENT_COLOR = {
    "operational":             C_GRN,
    "degraded_performance":    C_YEL,
    "partial_outage":          C_ORN,
    "major_outage":            C_RED,
    "under_maintenance":       C_CYN,
}


def _pulse_age(snap):
    """Return human-readable age of the snapshot."""
    wall_t = snap.get("wall_t", 0) or 0
    if wall_t <= 0:
        return "--"
    age_s = max(0, int(time.time() - wall_t))
    if age_s < 60:
        return f"{age_s}s ago"
    if age_s < 3600:
        return f"{age_s // 60}m ago"
    return f"{age_s // 3600}h ago"


def render_pulse_modal(cols, rows):
    """Render Anthropic backend stability modal (P key)."""
    snap = pulse.get_pulse_snapshot()
    SW = cols
    buf = []

    buf.append(sep(SW))
    title = "ANTHROPIC PULSE"
    t_pad = max(0, SW - len(title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{title}{R}{BG_BAR}{' ' * t_pad}{R}")
    buf.append(sep(SW))

    level = snap.get("level") or "error"
    color = _PULSE_LEVEL_COLOR.get(level, C_DIM)
    score = snap.get("score")
    verdict = _sanitize(snap.get("verdict") or "AWAITING DATA")
    reason = _sanitize(snap.get("reason") or "")

    if score is None:
        buf.append(f"{C_DIM}{B}STB{R} {mkbar(0, C_DIM)}")
    else:
        buf.append(f"{color}{B}STB{R} {mkbar(float(score), color)}")

    # Verdict line
    buf.append("")
    buf.append(f"{color}{B}>> {verdict} <<{R}")
    if reason:
        buf.append(f"{C_DIM}reason: {reason}{R}")

    buf.append(sep(SW))
    # Details
    indicator = snap.get("indicator")
    if indicator:
        ind_label = pulse._indicator_label(indicator)
        if indicator == "none":
            ind_color = C_GRN
        elif indicator in ("minor", "maintenance"):
            ind_color = C_YEL
        elif indicator in ("major", "critical"):
            ind_color = C_RED
        else:
            # Unknown indicator from a future status schema — stay neutral rather than alarming red
            ind_color = C_DIM
        buf.append(f"{C_DIM}INDICATOR {R} {ind_color}{_sanitize(indicator)}{R} {C_DIM}({_sanitize(ind_label)}){R}")
    else:
        buf.append(f"{C_DIM}INDICATOR {R} {C_DIM}--{R}")

    incidents = snap.get("incidents") or []
    inc_color = C_GRN if not incidents else (C_YEL if len(incidents) < 3 else C_RED)
    buf.append(f"{C_DIM}INCIDENTS {R} {inc_color}{len(incidents)}{R}")

    # Per-model rollup — any model mentioned in any active incident = affected.
    # Silent when no incidents mention models.
    affected = set()
    for inc in incidents:
        for m in (inc.get("affected_models") or []):
            affected.add(m)
    if affected or incidents:
        parts = []
        for m in ("opus", "sonnet", "haiku"):
            if m in affected:
                parts.append(f"{C_RED}{B}{m}{R}")
            else:
                parts.append(f"{C_GRN}{m}{R}")
        buf.append(f"{C_DIM}MODELS    {R} " + f" {C_DIM}/{R} ".join(parts))

    latency = snap.get("latency_ms")
    if latency is None:
        buf.append(f"{C_DIM}LATENCY   {R} {C_RED}timeout{R}")
    else:
        if latency < 300:
            lc = C_GRN
        elif latency < 800:
            lc = C_YEL
        else:
            lc = C_RED
        buf.append(f"{C_DIM}LATENCY   {R} {lc}{int(latency)} ms{R}")

    # p50 / p95 — appears once we have >=3 samples
    p50 = snap.get("latency_p50_ms")
    p95 = snap.get("latency_p95_ms")
    if p50 is not None and p95 is not None:
        buf.append(f"{C_DIM}P50 / P95 {R} {C_WHT}{p50} ms{R} {C_DIM}/{R} {C_WHT}{p95} ms{R}")

    # Raw (instant) score — shown when it diverges from smoothed by > 5 points
    raw_score = snap.get("raw_score")
    if score is not None and raw_score is not None and abs(int(raw_score) - int(score)) > 5:
        buf.append(f"{C_DIM}INSTANT   {R} {C_DIM}{int(raw_score)}% (smoothed: {int(score)}%){R}")

    buf.append(f"{C_DIM}UPDATED   {R} {C_WHT}{_pulse_age(snap)}{R}")

    # Error detail (if any)
    err = snap.get("error")
    if err:
        buf.append(sep(SW))
        buf.append(f"{C_RED}{B}ERROR{R} {C_DIM}{_sanitize(err)}{R}")

    # Active incidents (first 3)
    if incidents:
        buf.append(sep(SW))
        ih = "ACTIVE INCIDENTS"
        ih_pad = max(0, SW - len(ih))
        buf.append(f"{BG_BAR}{C_WHT}{B}{ih}{R}{BG_BAR}{' ' * ih_pad}{R}")
        buf.append(sep(SW))
        for inc in incidents[:3]:
            name = _sanitize(inc.get("name") or "?")[:SW - 8]
            impact = _sanitize(inc.get("impact") or "minor")
            ic = C_RED if impact in ("major", "critical") else C_YEL
            models = inc.get("affected_models") or []
            tag = ""
            if models:
                tag = f" {C_DIM}[{R}{C_ORN}{','.join(models)}{R}{C_DIM}]{R}"
            buf.append(f"{ic}{impact.upper()[:4]:<4}{R} {C_WHT}{name}{R}{tag}")

    # Components
    components = snap.get("components") or []
    if components:
        buf.append(sep(SW))
        ch = "COMPONENTS"
        ch_pad = max(0, SW - len(ch))
        buf.append(f"{BG_BAR}{C_WHT}{B}{ch}{R}{BG_BAR}{' ' * ch_pad}{R}")
        buf.append(sep(SW))
        # Longest possible status label ("partial outage" = 14). Reserve sep + label.
        name_w = max(18, SW - 16)
        for c in components[:10]:
            name = _sanitize(c.get("name") or "?")
            # Strip parenthetical suffixes (e.g. "Claude API (api.anthropic.com)" → "Claude API")
            name = re.sub(r"\s*\([^)]*\)?\s*$", "", name).strip() or name
            cstatus = _sanitize(c.get("status") or "unknown")
            cc = _PULSE_COMPONENT_COLOR.get(cstatus, C_DIM)
            buf.append(f"{C_WHT}{name[:name_w]:<{name_w}}{R} {cc}{cstatus.replace('_', ' ')}{R}")

    buf.append(sep(SW))
    footer = "source: status.claude.com + api.anthropic.com ping"
    if len(footer) > SW:
        footer = "source: status.claude.com + api ping"
    buf.append(f"{C_DIM}{footer[:SW]}{R}")
    buf.append(f"{C_DIM}press any key to close{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Menu modal
# ---------------------------------------------------------------------------
def render_menu(cols, rows):
    SW = cols
    buf = []
    buf.append(sep(SW))
    mn_pad = max(0, SW - 6)  # "≡ MENU" = 6 chars
    buf.append(f"{BG_BAR}{C_WHT}{B}\u2261 MENU{R}{BG_BAR}{' ' * mn_pad}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}]{R}   {C_DIM}Quit{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}r{R}{C_DIM}]{R}   {C_DIM}Refresh{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}s{R}{C_DIM}]{R}   {C_DIM}Session Picker{R}")
    buf.append(sep(SW))
    vp = max(0, SW - 5)
    buf.append(f"{BG_BAR}{C_WHT}{B}VIEWS{R}{BG_BAR}{' ' * vp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}t{R}{C_DIM}]{R}   {C_DIM}Token Stats{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}c{R}{C_DIM}]{R}   {C_DIM}Cost Breakdown{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}p{R}{C_DIM}]{R}   {C_DIM}Anthropic Pulse{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}l{R}{C_DIM}]{R}   {C_DIM}Legend{R}")
    buf.append(sep(SW))
    sp = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}SYSTEM{R}{BG_BAR}{' ' * sp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}u{R}{C_DIM}]{R}   {C_DIM}Update Manager{R} {C_DIM}a=apply{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}press any key to close{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Update modal
# ---------------------------------------------------------------------------
_update_result = None  # None=not run, str=output message
_update_lock = threading.Lock()


def _git_cmd(args, timeout=15):
    """Run git command in repo root, return (returncode, stdout, stderr).
    Uses module-level run_git for consistent env whitelist + mockable patch target."""
    try:
        r = run_git(args, cwd=_REPO_ROOT, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "git not found"
    except subprocess.TimeoutExpired:
        return -2, "", "timeout"


def _update_checks():
    """Return list of warning strings for update safety."""
    warns = []
    rc, out, _ = _git_cmd(["rev-parse", "--abbrev-ref", "HEAD"])
    if rc == 0 and out != "main":
        warns.append(f"Not on main branch (current: {out})")
    rc, out, _ = _git_cmd(["status", "--porcelain", "-uno"])
    if rc == 0 and out:
        warns.append("Uncommitted changes in working tree")
    rc, out, _ = _git_cmd(["rev-list", "--left-right", "--count", "HEAD...origin/main"])
    if rc == 0:
        parts = out.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
            if ahead > 0 and behind > 0:
                warns.append(f"Diverged: {ahead} ahead, {behind} behind origin/main")
    return warns


def _get_new_commits(max_lines=10):
    """Return list of oneline commit strings from HEAD to origin/main."""
    rc, out, _ = _git_cmd(["log", "--oneline", f"--max-count={max_lines}", "HEAD..origin/main"])
    if rc != 0 or not out:
        return []
    return out.split("\n")


def _get_remote_changelog_preview(version, max_lines=15):
    """Extract changelog section for a version from origin/main."""
    rc, out, _ = _git_cmd(["show", "origin/main:CHANGELOG.md"])
    if rc != 0:
        return []
    entry = extract_changelog_entry(out, version, max_lines=max_lines)
    if not entry:
        return []
    return entry.split("\n")


def _set_update_result(value):
    global _update_result
    with _update_lock:
        _update_result = value


def _get_update_result():
    with _update_lock:
        return _update_result


def _apply_update_worker():
    """Background worker: git pull --ff-only + syntax check. Sets _update_result."""
    try:
        rc, out, err = _git_cmd(["pull", "--ff-only", "origin", "main"], timeout=30)
        if rc == 0:
            # Syntax check via compile() — avoids interpreter version mismatch
            bad = []
            for f in ["monitor.py", "statusline.py", "shared.py", "update.py"]:
                fp = _REPO_ROOT / f
                if fp.exists():
                    try:
                        compile(fp.read_text(encoding="utf-8"), str(fp), "exec")
                    except SyntaxError:
                        bad.append(f)
            if bad:
                _set_update_result(f"Updated but syntax errors in: {', '.join(bad)}")
            else:
                _set_update_result("Update complete. Restart monitor to apply.")
        else:
            _set_update_result(f"Update failed: {_sanitize(err or out or 'unknown error')}")
    except Exception as e:
        _set_update_result(f"Update error: {_sanitize(str(e))}")


def _apply_update_action():
    """Spawn background thread for update. Non-blocking."""
    _set_update_result("Updating...")
    t = threading.Thread(target=_apply_update_worker, daemon=True)
    t.start()


def render_update_modal(cols, rows):
    """Render the update manager modal."""
    SW = cols
    buf = []
    buf.append(sep(SW))
    up_pad = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}UPDATE{R}{BG_BAR}{' ' * up_pad}{R}")
    buf.append(sep(SW))

    rls = _rls_snapshot()  # atomic coherent read
    rls_s = rls["status"]
    remote_ver = rls.get("remote_ver")

    buf.append(f"{C_GRN}CUR{R} {C_GRN}v{VERSION}{R}")
    if remote_ver:
        buf.append(f"{C_WHT}REM{R} {C_WHT}v{remote_ver}{R}")
    else:
        buf.append(f"{C_DIM}REM{R} {C_DIM}unknown{R}")

    # Last check freshness — show only after worker has actually run.
    # Using status gate (not "t > 0") because time.monotonic() may be small on
    # freshly-started processes where tests set t = monotonic() - 125 < 0.
    if rls_s is not None:
        age_s = max(0, int(time.monotonic() - rls.get("t", 0)))
        if age_s < 60:
            age_str = f"{age_s}s ago"
        elif age_s < 3600:
            age_str = f"{age_s // 60}m ago"
        else:
            age_str = f"{age_s // 3600}h ago"
        buf.append(f"{C_DIM}Checked {age_str}{R}")

    buf.append(f"{C_CYN}github.com/iM3SK/cc-aio-mon{R}")

    if rls_s == "update" and remote_ver:
        # Show new commits
        commits = _get_new_commits()
        if commits:
            buf.append("")
            buf.append(f"{C_WHT}{B}NEW COMMITS{R}")
            buf.append(sep(SW))
            for c_line in commits:
                buf.append(f"{C_DIM}{_sanitize(c_line)}{R}")

        # Changelog preview
        cl = _get_remote_changelog_preview(remote_ver)
        if cl:
            buf.append("")
            buf.append(f"{C_WHT}{B}CHANGELOG{R}")
            buf.append(sep(SW))
            for c_line in cl:
                buf.append(f"{C_DIM}{_sanitize(c_line)}{R}")

        # Safety warnings
        warns = _update_checks()
        if warns:
            buf.append("")
            buf.append(f"{C_RED}{B}WARNINGS{R}")
            buf.append(sep(SW))
            for w in [_sanitize(x) for x in warns]:
                buf.append(f"{C_RED}{w}{R}")

        ur = _get_update_result()
        if ur:
            buf.append("")
            if "complete" in ur:
                buf.append(f"{C_GRN}{B}{ur}{R}")
            else:
                buf.append(f"{C_RED}{B}{ur}{R}")
            buf.append(sep(SW))
            buf.append(f"{C_DIM}press any key to close{R}")
        elif warns:
            buf.append(sep(SW))
            buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply (risky){R}")
            buf.append(f"{C_DIM}press any key to close{R}")
        else:
            buf.append(sep(SW))
            buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply{R}")
            buf.append(f"{C_DIM}press any key to close{R}")

    elif rls_s == "ok":
        buf.append(f"{C_GRN}{spin_rls()} Up to date{R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply (no update available){R}")
        buf.append(f"{C_DIM}press any key to close{R}")

    elif rls_s is None:
        buf.append(f"{C_DIM}{spin_rls()} Checking for updates...{R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply (checking...){R}")
        buf.append(f"{C_DIM}press any key to close{R}")

    else:
        buf.append(f"{C_DIM}Could not check for updates.{R}")
        if rls_s == "no_git":
            buf.append(f"{C_DIM}Git is not installed or not on PATH.{R}")
        elif rls_s == "timeout":
            buf.append(f"{C_DIM}Network timeout — check your connection.{R}")
        else:
            buf.append(f"{C_DIM}Unknown error during check.{R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply (check failed){R}")
        buf.append(f"{C_DIM}press any key to close{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Token stats modal
# ---------------------------------------------------------------------------
_PERIOD_LABELS = {"all": "All Time", "7d": "Last 7 Days", "30d": "Last 30 Days"}
_PERIOD_CYCLE = ["all", "7d", "30d"]

# Short display names for known model IDs
_MODEL_NAMES = {
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-5": "Opus 4.5",
    "claude-opus-4-1": "Opus 4.1",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-5": "Sonnet 4.5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "claude-haiku-3-5": "Haiku 3.5",
    "haiku": "Haiku",
    "sonnet": "Sonnet",
    "opus": "Opus",
}

# 3-char codes for stats/legend
_MODEL_CODES = {
    "claude-opus-4-7": ("OP", "4.7"),
    "claude-opus-4-6": ("OP", "4.6"),
    "claude-opus-4-5": ("OP", "4.5"),
    "claude-opus-4-1": ("OP", "4.1"),
    "claude-sonnet-4-6": ("SO", "4.6"),
    "claude-sonnet-4-5": ("SO", "4.5"),
    "claude-haiku-4-5-20251001": ("HA", "4.5"),
    "claude-haiku-3-5": ("HA", "3.5"),
    "haiku": ("HA", ""),
    "sonnet": ("SO", ""),
    "opus": ("OP", ""),
}

_MODEL_ID_RE = re.compile(r"^claude-(opus|sonnet|haiku)-(\d+)-(\d+)")


def _model_label(model_id):
    base = model_id.split("[")[0] if model_id else ""
    if base in _MODEL_NAMES:
        return _MODEL_NAMES[base]
    m = _MODEL_ID_RE.match(base)
    if m:
        fam = m.group(1).capitalize()
        return f"{fam} {m.group(2)}.{m.group(3)}"
    return base or "?"


def _model_code(model_id):
    """Return (short_code, version) tuple for stats display."""
    base = model_id.split("[")[0] if model_id else ""
    if base in _MODEL_CODES:
        return _MODEL_CODES[base]
    m = _MODEL_ID_RE.match(base)
    if m:
        short = {"opus": "OP", "sonnet": "SO", "haiku": "HA"}[m.group(1)]
        return (short, f"{m.group(2)}.{m.group(3)}")
    # Unknown model — sanitize raw input to prevent ANSI injection via transcript
    safe = _sanitize(base[:3]).upper() if base else ""
    return (safe or "?", "")


# Bar colors per model (consistent mapping)
_MODEL_COLORS = [C_CYN, C_GRN, C_YEL, C_ORN, C_RED]


def _total_tokens(m):
    """Total token volume for a model: input + output + cache_read + cache_write."""
    return m.get("input", 0) + m.get("output", 0) + m.get("cache_read", 0) + m.get("cache_write", 0)


def render_stats(cols, rows, period="all"):
    SW = cols
    buf = []
    buf.append(sep(SW))
    title = f"TOKEN STATS  {_PERIOD_LABELS.get(period, period)}"
    tp = max(0, SW - len(title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{title}{R}{BG_BAR}{' ' * tp}{R}")
    buf.append(sep(SW))

    models, overview = scan_transcript_stats(period)
    if not models:
        buf.append(f"{C_DIM}No transcript data found in ~/.claude/projects/{R}")
        buf.append(f"{C_DIM}(stats appear after at least one CC session){R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[{R}{C_WHT}1{R}{C_DIM}]all [{R}{C_WHT}2{R}{C_DIM}]7d [{R}{C_WHT}3{R}{C_DIM}]30d{R}")
        buf.append(f"{C_DIM}press any key to close{R}")
        _fit_buf_height(buf, rows, clip_tail=True)
        return buf

    # -- Overview section --
    n_sessions = overview["sessions"]
    n_days = len(overview["active_days"])
    longest_ms = overview["longest_dur_ms"]
    daily = overview["daily_tokens"]
    current_streak, longest_streak = _calc_streaks(overview["active_days"])

    # Most active day
    most_active = "--"
    if daily:
        top_day = max(daily, key=daily.get)
        most_active = f"{top_day} ({f_tok(daily[top_day])})"

    trunc_tag = f"  {C_YEL}(1000 file limit){R}" if overview.get("truncated") else ""
    buf.append(f"{C_WHT}SES{R} {C_WHT}{n_sessions}{R} {C_WHT}DAY{R} {C_WHT}{n_days}{R} {C_WHT}STK{R} {C_WHT}{current_streak}d{R}{C_DIM}/{longest_streak}d{R}{trunc_tag}")
    buf.append(f"{C_WHT}LSS{R} {C_WHT}{f_dur(longest_ms)}{R} {C_WHT}TOP{R} {C_WHT}{most_active}{R}")

    # -- Models section --
    total_all = sum(_total_tokens(m) for m in models.values())
    sorted_models = sorted(
        models.items(), key=lambda kv: _total_tokens(kv[1]), reverse=True
    )

    buf.append(sep(SW))
    mp = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}MODELS{R}{BG_BAR}{' ' * mp}{R}")
    buf.append(sep(SW))
    for i, (mid, st) in enumerate(sorted_models):
        color = _MODEL_COLORS[i % len(_MODEL_COLORS)]
        code, ver = _model_code(mid)
        total_m = _total_tokens(st)
        pct = total_m / total_all * 100 if total_all else 0
        ver_tag = f" {C_DIM}{ver}{R}" if ver else ""
        buf.append(f"{color}{B}{code}{R}{ver_tag} {mkbar(pct, color)}")
        buf.append(
            f"    {C_DIM}INP:{R} {color}{f_tok(st['input'])}{R}"
            f" {C_DIM}OUT:{R} {color}{f_tok(st['output'])}{R}"
            f" {C_DIM}CLS:{R} {color}{st['calls']:,}{R}"
        )
        if st.get("cache_read", 0) or st.get("cache_write", 0):
            buf.append(
                f"    {C_DIM}CRD:{R} {color}{f_tok(st.get('cache_read', 0))}{R}"
                f" {C_DIM}CWR:{R} {color}{f_tok(st.get('cache_write', 0))}{R}"
            )
        buf.append(sep(SW))

    # Totals
    total_in = sum(m["input"] for m in models.values())
    total_out = sum(m["output"] for m in models.values())
    total_cr = sum(m.get("cache_read", 0) for m in models.values())
    total_cw = sum(m.get("cache_write", 0) for m in models.values())
    total_calls = sum(m["calls"] for m in models.values())
    buf.append(
        f"{C_WHT}{B}ALL{R}"
        f" {C_DIM}INP:{R} {C_WHT}{f_tok(total_in)}{R}"
        f" {C_DIM}OUT:{R} {C_WHT}{f_tok(total_out)}{R}"
        f" {C_DIM}CLS:{R} {C_WHT}{total_calls:,}{R}"
    )
    if total_cr or total_cw:
        buf.append(
            f"    {C_DIM}CRD:{R} {C_WHT}{f_tok(total_cr)}{R}"
            f" {C_DIM}CWR:{R} {C_WHT}{f_tok(total_cw)}{R}"
        )
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}1{R}{C_DIM}]all [{R}{C_WHT}2{R}{C_DIM}]7d [{R}{C_WHT}3{R}{C_DIM}]30d{R}")
    buf.append(f"{C_DIM}press any key to close{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Session picker
# ---------------------------------------------------------------------------
def render_picker(sessions, cols, rows):
    W = cols
    buf = []
    buf.append(sep(W))
    hp = max(0, W - len(f"CC AIO MON {VERSION}"))
    buf.append(f"{BG_BAR}{C_WHT}{B}CC AIO MON {VERSION}{R}{BG_BAR}{' ' * hp}{R}")
    buf.append(sep(W))

    if not sessions:
        buf.append(f"{C_DIM}Waiting for Claude Code session...{R}")
        buf.append(f"{C_DIM}Start a session, then come back here.{R}")
    else:
        buf.append(f"{C_WHT}{B}SESSIONS{R}")
        buf.append(sep(W))
        # Sort: active first, then stale. Limit to 9 (keyboard limit).
        sorted_s = sorted(sessions, key=lambda s: s["stale"])
        shown = sorted_s[:9]
        for i, s in enumerate(shown):
            tag = f"{C_RED}stale{R}" if s["stale"] else f"{C_GRN}live{R}"
            nm = s["session_name"] or s["id"][:8]
            # Short model: "Opus 4.6 (1M context)" → "OP 4.6"
            # Note: duplicate of _model_label logic — intentional (operates on display_name not id)
            model_raw = s["model"]
            mm = re.search(r"(Opus|Sonnet|Haiku)\s+(\d+)\.(\d+)", model_raw)
            if mm:
                code = {"Opus": "OP", "Sonnet": "SO", "Haiku": "HA"}[mm.group(1)]
                model_short = f"{code} {mm.group(2)}.{mm.group(3)}"
            else:
                # legacy fallback
                model_short = re.sub(r"\s*\(.*?\)", "", model_raw).strip()
            line = f"{C_WHT}[{i + 1}]{R} {B}{nm}{R} {C_DIM}{model_short}{R} {tag}"
            buf.append(truncate(line, W))
        if len(sessions) > 9:
            buf.append(f"{C_DIM}+{len(sessions) - 9} more{R}")

    buf.append(sep(W))
    buf.append(f"{C_DIM}[{R}{C_WHT}1-9{R}{C_DIM}] select{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}] quit{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Screen flush
# ---------------------------------------------------------------------------
def flush(buf, cols=None):
    if cols is None:
        cols = shutil.get_terminal_size((80, 24)).columns
    out = [SYNC_ON, HOME]
    for i, line in enumerate(buf):
        out.append(truncate(line, cols))
        out.append(EL)
        if i < len(buf) - 1:
            out.append("\n")
    # Clear any leftover lines below the buffer from previous frames
    out.append(E + "J")  # erase from cursor to end of screen
    out.append(SYNC_OFF)
    sys.stdout.write("".join(out))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # SIGPIPE: silent exit when --list output is piped to head/less on Unix
    # (matches statusline.py + update.py for consistency)
    if sys.platform != "win32":
        try:
            import signal
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Claude AIO Monitor")
    parser.add_argument("--session", help="Session ID")
    parser.add_argument("--list", action="store_true", help="List sessions")
    parser.add_argument("--refresh", type=int, default=500, help="Refresh ms")
    args = parser.parse_args()

    args.refresh = max(100, min(60000, args.refresh))

    try:
        is_utf8 = sys.stdout.encoding and codecs.lookup(sys.stdout.encoding).name == "utf-8"
    except LookupError:
        is_utf8 = False
    if not is_utf8:
        sys.stdout.flush()
        sys.stdout = open(
            sys.stdout.fileno(), mode="w", encoding="utf-8",
            errors="replace", closefd=False,
        )

    if args.list:
        for s in list_sessions():
            tag = "(stale)" if s["stale"] else "(live)"
            nm = s["session_name"] or "--"
            print(f"  {s['id'][:16]}  {s['model']:>8}  {nm}  {s['cwd']}  {tag}")
        return

    # Terminal capability checks — must run before any ANSI output
    if not sys.stdout.isatty():
        sys.exit(
            "Error: stdout is not a TTY.\n"
            "Run monitor.py directly in a terminal — do not pipe or redirect output."
        )
    if os.environ.get("TERM") == "dumb":
        sys.exit(
            "Error: dumb terminal detected (TERM=dumb).\n"
            "Use a terminal with ANSI support: Windows Terminal, iTerm2, xterm, Kitty, Alacritty."
        )

    _setup_term()
    sys.stdout.write(ALT_ON + HIDE_CUR + CLR)
    sys.stdout.flush()

    def cleanup(*_args):
        _restore_term()
        sys.stdout.write(SHOW_CUR + ALT_OFF)
        sys.stdout.flush()

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    sid = args.session
    if sid and not _SID_RE.match(sid):
        print(f"Invalid session ID: {sid}")
        return
    show_legend = False
    show_menu = False
    show_cost = False
    show_stats = None  # None=off, "all"/"7d"/"30d"=active period
    force_picker = False
    show_update = False
    show_pulse = False
    # Opt-out: CC_AIO_MON_NO_PULSE=1 disables the background Anthropic Pulse worker.
    # Mirrors CC_AIO_MON_NO_UPDATE_CHECK=1 pattern for the release checker.
    if os.environ.get("CC_AIO_MON_NO_PULSE") != "1":
        pulse.start_pulse_worker()
    _set_update_result(None)
    _render_errors = 0
    last_mt = 0
    last_seen = 0  # monotonic timestamp of last successful data load
    last_data = None
    last_size = (0, 0)
    last_hist_mt = 0
    last_hist = []
    data_interval = args.refresh / 1000
    tick = 0.05  # 50ms tick for responsive resize
    last_data_load = 0.0  # monotonic timestamp of last data file read

    try:
        while True:
            cols, rows = shutil.get_terminal_size((80, 24))
            size_changed = (cols, rows) != last_size
            last_size = (cols, rows)
            now_mono = time.monotonic()
            since_data = now_mono - last_data_load

            # Always poll keyboard
            k = poll_key()
            if k == "q":
                break
            # ── Modal-specific handlers first (priority) ──
            elif show_update and k == "a" and _get_update_result() is None:
                _apply_update_action()
            elif show_update and k is not None:
                show_update = False
                _set_update_result(None)
            elif show_stats is not None and k in ("1", "2", "3"):
                show_stats = _PERIOD_CYCLE[int(k) - 1]
            elif show_stats is not None and k is not None:
                show_stats = None
            elif show_menu and k is not None:
                # Menu modal: dispatch key or close
                if k == "r":
                    last_mt = 0
                    last_seen = time.monotonic()
                elif k == "s":
                    sid = None
                    force_picker = True
                    last_data = None
                    last_mt = 0
                    last_seen = 0
                    last_hist_mt = 0
                    last_hist = []
                elif k == "l":
                    show_legend = True
                elif k == "t":
                    show_stats = "all"
                elif k == "u":
                    show_update = True
                elif k == "c":
                    show_cost = True
                elif k == "p":
                    show_pulse = True
                show_menu = False
            elif show_cost and k is not None:
                show_cost = False
            elif show_pulse and k is not None:
                show_pulse = False
            elif show_legend and k is not None:
                show_legend = False
            # ── Global handlers ──
            elif k == "r":
                last_mt = 0
                last_seen = time.monotonic()
            elif k == "s":
                sid = None
                force_picker = True
                last_data = None
                last_mt = 0
                last_seen = 0
                last_hist_mt = 0
                last_hist = []
            elif k == "m":
                show_menu = not show_menu
                show_legend = False
                show_stats = None
                show_update = False
                _set_update_result(None)
            elif k == "l":
                show_legend = not show_legend
                show_menu = False
                show_stats = None
                show_update = False
                _set_update_result(None)
            elif k == "t":
                if show_stats is not None:
                    show_stats = None
                else:
                    show_stats = "all"
                    show_legend = False
                    show_update = False
                    _set_update_result(None)
            elif k == "c":
                show_cost = not show_cost
                show_menu = False
                show_legend = False
                show_stats = None
                show_update = False
                _set_update_result(None)
            elif k == "u":
                show_update = not show_update
                if not show_update:
                    _set_update_result(None)
                show_legend = False
                show_stats = None
                show_pulse = False
            elif k == "p":
                show_pulse = not show_pulse
                show_menu = False
                show_legend = False
                show_stats = None
                show_cost = False
                show_update = False
                _set_update_result(None)

            # Render every tick when we have data (for spinner), reload data on interval
            need_render = size_changed or last_data is not None or since_data >= data_interval
            if not need_render:
                time.sleep(tick)
                continue

            # Update modal
            if show_update:
                try:
                    flush(render_update_modal(cols, rows), cols)
                except (TypeError, ValueError, KeyError, OSError):
                    pass
                time.sleep(tick)
                continue

            # Pulse modal (no session required)
            if show_pulse:
                try:
                    flush(render_pulse_modal(cols, rows), cols)
                except (TypeError, ValueError, KeyError, OSError):
                    pass
                time.sleep(tick)
                continue

            # Stats modal can render without a session (global data)
            if show_stats is not None:
                try:
                    flush(render_stats(cols, rows, show_stats), cols)
                except (TypeError, ValueError, KeyError, OSError):
                    pass
                time.sleep(tick)
                continue

            # Auto-detect / pick session
            if sid is None:
                sessions = list_sessions()
                active = [s for s in sessions if not s["stale"]]
                if len(active) == 1 and len(sessions) == 1 and not force_picker:
                    sid = active[0]["id"]
                elif not sessions:
                    flush(render_picker([], cols, rows), cols)
                    time.sleep(tick)
                    continue
                else:
                    sorted_s = sorted(sessions, key=lambda s: s["stale"])[:9]
                    flush(render_picker(sessions, cols, rows), cols)
                    if k and k.isdigit():
                        idx = int(k) - 1
                        if 0 <= idx < len(sorted_s):
                            sid = sorted_s[idx]["id"]
                            force_picker = False
                            last_seen = time.monotonic()
                    time.sleep(tick)
                    continue

            # Load state (only on data interval, not resize)
            if since_data >= data_interval:
                last_data_load = now_mono
                jp = DATA_DIR / f"{sid}.json"
                try:
                    mt = jp.stat().st_mtime
                except OSError:
                    mt = 0

                if mt != last_mt or last_data is None:
                    d = load_state(sid)
                    if d:
                        last_data = d
                        last_mt = mt
                        last_seen = time.monotonic()
                    elif mt == 0:
                        last_mt = 0

            if last_data is None:
                flush(render_picker([], cols, rows), cols)
                time.sleep(tick)
                continue

            # Only reload history when file has changed
            try:
                hmt = (DATA_DIR / f"{sid}.jsonl").stat().st_mtime
            except OSError:
                hmt = 0
            if hmt != last_hist_mt:
                # Always replace (even []) so BRN/CTR don't stay stale when jsonl is cleared/truncated
                last_hist = load_history(sid)
                last_hist_mt = hmt
            is_stale = (time.monotonic() - last_seen) > STALE_THRESHOLD if last_seen else False
            try:
                flush(render_frame(last_data, last_hist, cols, rows, show_legend, show_menu, show_cost, stale=is_stale), cols)
            except (TypeError, ValueError, KeyError, ZeroDivisionError, OverflowError, OSError) as e:
                _render_errors += 1
                if _render_errors <= 3:
                    sys.stderr.write(f"render error #{_render_errors}: {e}\n")

            time.sleep(tick)

    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
