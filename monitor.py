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
import json
import os
import pathlib
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

from shared import (calc_rates, _num, _sanitize, f_tok, f_cost, f_dur,
                    _SID_RE, _ANSI_RE, MAX_FILE_SIZE, DATA_DIR_NAME,
                    E, R, B, C_RED, C_GRN, C_YEL, C_ORN, C_CYN, C_WHT, C_DIM)

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

    models: {model_id: {"input": int, "output": int, "calls": int}}
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

    if not _CLAUDE_DIR.is_dir():
        empty_ov = {"sessions": 0, "active_days": set(), "longest_dur_ms": 0,
                     "first_date": None, "daily_tokens": {}}
        return models, empty_ov

    _file_count = 0
    for jl in _CLAUDE_DIR.glob("**/*.jsonl"):
        _file_count += 1
        if _file_count > 1000:
            break
        # Skip subagent transcripts for session counting
        is_subagent = "subagents" in str(jl)
        try:
            st = jl.stat()
            if st.st_size > 50_000_000:
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
                    u = msg["usage"]
                    inp = int(_num(u.get("input_tokens", 0)))
                    out = int(_num(u.get("output_tokens", 0)))
                    if model not in models:
                        models[model] = {"input": 0, "output": 0, "calls": 0}
                    models[model]["input"] += inp
                    models[model]["output"] += out
                    models[model]["calls"] += 1

                    # Track active days and daily tokens
                    if ts > 0:
                        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                        active_days.add(day)
                        daily_tokens[day] = daily_tokens.get(day, 0) + inp + out
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
        """Enable VT/ANSI processing on Windows console."""
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
            kernel32.SetConsoleMode(handle, new_mode)
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

VERSION = "1.8.0"
# _SID_RE, _ANSI_RE, MAX_FILE_SIZE imported from shared.py
STALE_THRESHOLD = 1800  # 30 min — Claude Code emits no events during idle

try:
    WARN_BRN = float(os.environ.get("CLAUDE_WARN_BRN", "0.50"))
except (ValueError, TypeError):
    WARN_BRN = 0.50


def vlen(s):
    """Visible length of string (ignoring ANSI escape codes)."""
    return len(_ANSI_RE.sub("", s))


def truncate(s, maxw):
    """Truncate string to maxw visible characters, preserving ANSI codes."""
    vis = 0
    i = 0
    truncated = False
    while i < len(s) and vis < maxw:
        m = _ANSI_RE.match(s, i)
        if m:
            i = m.end()
        else:
            vis += 1
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
H = "\u2500"   # ─
BF = "\u2588"  # █
SH = "\u2591"  # ░

BAR_W = 25     # fixed bar width for ALL metrics


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def f_cd(epoch):
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


# ---------------------------------------------------------------------------
# Progress bar — enclosed [████░░░░], fixed width, color by threshold
# ---------------------------------------------------------------------------
def mkbar(pct, color=None):
    """Returns colored [████░░░░░]  XX.X%"""
    pct = max(0.0, min(100.0, pct))
    if color is None:
        if pct >= 80:
            color = C_RED
        elif pct >= 50:
            color = C_YEL
        else:
            color = C_GRN
    filled = round(pct * BAR_W / 100)
    empty = BAR_W - filled
    return (
        f"{C_DIM}[{R}"
        f"{color}{BF * filled}{R}"
        f"{C_DIM}{SH * empty}{R}"
        f"{C_DIM}]{R}"
        f" {color}{pct:5.1f} %{R}"
    )


def _limit_color(pct):
    """Dynamic color for rate limit metrics — yellow base, red >= 80%."""
    if pct >= 80:
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
    if pct_remaining > 50:
        return C_RED
    if pct_remaining > 20:
        return C_YEL
    return C_GRN


# ---------------------------------------------------------------------------
# Fixed-range bar for rate/cost metrics
# ---------------------------------------------------------------------------
BRN_MAX = 1.0    # $/min ceiling
CTR_MAX = 5.0    # %/min ceiling
CST_MAX = 50.0   # $ ceiling


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
        warnings.append(f"BRN {cpm:.4f}$/m")
    return warnings


# ---------------------------------------------------------------------------
# Cross-session cost aggregation
# ---------------------------------------------------------------------------
_cost_cache = {"t": 0.0, "today": 0.0, "week": 0.0}


# ---------------------------------------------------------------------------
# RLS — background release check
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).parent.resolve()
_RLS_TTL = 3600  # check once per hour
_RLS_BLINK_INTERVAL = 0.5
_rls_cache = {"t": 0.0, "status": None, "remote_ver": None}
_rls_fetching = False
_rls_blink_last = 0.0
_rls_blink_on = True
_VERSION_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _parse_version(ver_str):
    """Parse version string to comparable tuple, ignoring non-numeric suffixes."""
    parts = []
    for p in ver_str.split("."):
        m = re.match(r"(\d+)", p)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts)


def _rls_check_worker():
    """Background worker: git fetch + compare VERSION. Writes result atomically."""
    global _rls_fetching
    try:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        kw = dict(cwd=_REPO_ROOT, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=15, env=env)
        r = subprocess.run(["git", "fetch", "origin", "main"], **kw)
        if r.returncode != 0:
            _rls_cache.update({"t": time.monotonic(), "status": "error", "remote_ver": None})
            return
        r = subprocess.run(["git", "show", "origin/main:monitor.py"], **kw)
        if r.returncode != 0:
            _rls_cache.update({"t": time.monotonic(), "status": "error", "remote_ver": None})
            return
        m = _VERSION_RE.search(r.stdout)
        if not m:
            _rls_cache.update({"t": time.monotonic(), "status": "error", "remote_ver": None})
            return
        remote_ver = m.group(1)
        local_t = _parse_version(VERSION)
        remote_t = _parse_version(remote_ver)
        if remote_t > local_t:
            status = "update"
        else:
            status = "ok"
        # Atomic swap — safe under GIL
        new = {"t": time.monotonic(), "status": status, "remote_ver": remote_ver}
        _rls_cache.update(new)
    except FileNotFoundError:
        _rls_cache.update({"t": time.monotonic(), "status": "no_git", "remote_ver": None})
    except subprocess.TimeoutExpired:
        _rls_cache.update({"t": time.monotonic(), "status": "timeout", "remote_ver": None})
    except Exception:
        _rls_cache.update({"t": time.monotonic(), "status": "error", "remote_ver": None})
    finally:
        _rls_fetching = False
        # Write RLS status to temp file for statusline.py
        try:
            DATA_DIR.mkdir(exist_ok=True)
            rls_file = DATA_DIR / "rls.json"
            rls_data = {"status": _rls_cache.get("status"), "remote_ver": _rls_cache.get("remote_ver")}
            fd = tempfile.NamedTemporaryFile(dir=DATA_DIR, suffix=".tmp", delete=False, mode="w", encoding="utf-8")
            fd.write(json.dumps(rls_data))
            fd.close()
            pathlib.Path(fd.name).replace(rls_file)
        except OSError:
            pass


def _rls_maybe_check():
    """Trigger background check if TTL expired. Non-blocking."""
    global _rls_fetching
    if os.environ.get("CC_AIO_MON_NO_UPDATE_CHECK") == "1":
        return
    if _rls_fetching:
        return
    if time.monotonic() - _rls_cache["t"] < _RLS_TTL:
        return
    _rls_fetching = True
    t = threading.Thread(target=_rls_check_worker, daemon=True)
    t.start()


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
    if not DATA_DIR.exists():
        return 0.0, 0.0
    today_start = datetime.combine(datetime.today().date(), datetime.min.time()).timestamp()
    week_start = today_start - 6 * 86400
    today_total = 0.0
    week_total = 0.0
    for jl in DATA_DIR.glob("*.jsonl"):
        sid = jl.stem
        if not _SID_RE.match(sid):
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
    # Write model stats to temp file for statusline.py (piggyback on cost TTL)
    _write_shared_stats()
    return today, week


def _write_shared_stats():
    """Write model usage percentages to temp file for statusline.py."""
    try:
        models, _ = scan_transcript_stats(period="all", ttl=30.0)
        if not models:
            return
        total = sum(m.get("input", 0) + m.get("output", 0) for m in models.values())
        if total <= 0:
            return
        pcts = {}
        for mid, m in models.items():
            tokens = m.get("input", 0) + m.get("output", 0)
            pct = round(tokens / total * 100, 1)
            label = _model_label(mid)
            pcts[label] = pct
        stats_file = DATA_DIR / "stats.json"
        fd = tempfile.NamedTemporaryFile(
            dir=DATA_DIR, suffix=".tmp", delete=False, mode="w", encoding="utf-8"
        )
        fd.write(json.dumps({"models": pcts}))
        fd.close()
        pathlib.Path(fd.name).replace(stats_file)
    except (OSError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Layout helpers — no borders, just lines
# ---------------------------------------------------------------------------
def sep(w):
    return C_DIM + "-" * w + R


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
DATA_DIR = pathlib.Path(tempfile.gettempdir()) / DATA_DIR_NAME


_RESERVED_FILES = {"rls", "stats"}  # non-session JSON files written by monitor


def list_sessions():
    if not DATA_DIR.exists():
        return []
    # Clean up stale .tmp files (orphans from crashed writes)
    for tmp in DATA_DIR.glob("*.tmp"):
        try:
            if time.time() - tmp.stat().st_mtime > 60:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
    sessions = []
    for f in DATA_DIR.glob("*.json"):
        sid = f.stem
        if not _SID_RE.match(sid):
            continue
        if sid in _RESERVED_FILES:
            continue
        try:
            st = f.stat()
            if st.st_size > MAX_FILE_SIZE:
                continue
            mt = st.st_mtime
            age = time.time() - mt
            d = json.loads(f.read_text(encoding="utf-8"))
            sessions.append({
                "id": sid, "mtime": mt, "age": age,
                "stale": age > STALE_THRESHOLD,
                "model": _sanitize(d.get("model", {}).get("display_name", "?")),
                "session_name": _sanitize(d.get("session_name", "")),
                "cwd": _sanitize(d.get("cwd", "")),
            })
        except (OSError, json.JSONDecodeError):
            pass
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def load_state(sid):
    if not _SID_RE.match(str(sid)):
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
    try:
        p = DATA_DIR / f"{sid}.jsonl"
        with open(p, "rb") as fh:
            raw = fh.read(MAX_FILE_SIZE * 10 + 1)
        if len(raw) > MAX_FILE_SIZE * 10:
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
    """Fit buffer to terminal height. Dashboard (clip_tail=False): protects last 2 lines (footer separator + keys). Legend/picker (clip_tail=True): clips from top."""
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
        if clip_tail:
            buf[:] = buf[-sub_target:]
        else:
            buf[:] = buf[:sub_target]
    while len(buf) < sub_target:
        buf.append("")
    buf.extend(tail)


# ---------------------------------------------------------------------------
# Render — main dashboard
# ---------------------------------------------------------------------------
def render_frame(data, hist, cols, rows, show_legend=False, stale=False):
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
    sid_str = str(data.get("session_id", "default"))
    session_label = sname or (sid_str[:16] if _SID_RE.match(sid_str) else "default")

    buf.append(sep(SW))
    hp_plain = f"CC AIO MON {VERSION}  {model_str}"
    hp_pad = max(0, SW - len(hp_plain))
    hp_text = f"{C_WHT}{B}CC AIO MON {VERSION}{R}{BG_BAR}  {C_CYN}{model_str}{R}{BG_BAR}"
    buf.append(f"{BG_BAR}{hp_text}{' ' * hp_pad}{R}")

    # ── Session status line (always visible) ────────────────
    if stale:
        _stale_age = ""
        _sid_safe = str(data.get("session_id", "default"))
        if _SID_RE.match(_sid_safe):
            try:
                _mt = (DATA_DIR / f"{_sid_safe}.json").stat().st_mtime
                _idle = int(time.time() - _mt)
                _stale_age = f" ({_idle // 60}m)" if _idle >= 60 else f" ({_idle}s)"
            except OSError:
                pass
        buf.append(f"{C_RED}{B}Session Inactive {spin_session()}{R}{_stale_age}  {c(C_FG)}{session_label}{R}")
    else:
        buf.append(f"{C_GRN}{B}Session Active {spin_session()}{R}  {C_FG}{session_label}{R}")

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
        apr_pct = round(api_dur / dur * 100, 1)
        buf.append(f"{c(C_GRN)}{B}APR{R} {mkbar(apr_pct, c(C_GRN))}")
        buf.append(f"    {c(C_DIM)}DUR {f_dur(dur)}{R} {C_DIM}-{R} {c(C_DIM)}API {f_dur(api_dur)}{R}")
    else:
        buf.append(f"{c(C_GRN)}{B}APR{R} {mkbar(0, C_DIM)}")
    buf.append(sep(SW))

    # ── CHR — Cache Hit Rate ────────────────────────────────
    if any([cr, cwt]):
        total_cache = cr + cwt
        chr_pct = round(cr / total_cache * 100, 1) if total_cache > 0 else 0
        buf.append(f"{c(C_GRN)}{B}CHR{R} {mkbar(chr_pct, c(C_GRN))}")
        buf.append(f"    {c(C_GRN)}c.r:{R} {c(C_GRN)}{f_tok(cr)}{R} {C_DIM}-{R} {c(C_GRN)}c.w:{R} {c(C_GRN)}{f_tok(cwt)}{R}")
    else:
        buf.append(f"{c(C_GRN)}{B}CHR{R} {mkbar(0, C_DIM)}")
    buf.append(sep(SW))

    # ── CTX ─────────────────────────────────────────────────
    ctx_used = int(ctx_total * ctx_pct / 100) if ctx_total else 0
    buf.append(f"{c(C_CYN)}{B}CTX{R} {mkbar(ctx_pct, c(C_CYN))}")
    warn = f"  {c(C_RED)}{B}! CTX>80%{R}" if ctx_pct >= 80 else ""
    if any([inp, out]):
        buf.append(f"    {c(C_CYN)}{f_tok(ctx_used)}{R}{warn} {C_DIM}-{R} {c(C_WHT)}in:{R} {c(C_WHT)}{f_tok(inp)}{R} {C_DIM}-{R} {c(C_WHT)}out:{R} {c(C_WHT)}{f_tok(out)}{R}")
    else:
        buf.append(f"    {c(C_CYN)}{f_tok(ctx_used)}{R}{warn}")
    buf.append(sep(SW))

    # ── 5HL ─────────────────────────────────────────────────
    if rl is not None:
        fh = rl.get("five_hour")
        if fh:
            pct = round(_num(fh.get("used_percentage")), 1)
            resets = _num(fh.get("resets_at"), 0)
            if resets > 0 and resets < time.time():
                pct = 0.0
            lc = c(_limit_color(pct))
            buf.append(f"{lc}{B}5HL{R} {mkbar(pct, lc)}")
            rc = c(_reset_color(resets, 18000))  # 5h window
            buf.append(f"    {c(C_WHT)}reset in:{R} {rc}{f_cd(resets if resets > 0 else None)}{R}")

        # ── 7DL ─────────────────────────────────────────────
        sd = rl.get("seven_day")
        if sd:
            pct = round(_num(sd.get("used_percentage")), 1)
            resets = _num(sd.get("resets_at"), 0)
            if resets > 0 and resets < time.time():
                pct = 0.0
            lc = c(_limit_color(pct))
            buf.append(f"{lc}{B}7DL{R} {mkbar(pct, lc)}")
            rc = c(_reset_color(resets, 604800))  # 7d window
            buf.append(f"    {c(C_WHT)}reset in:{R} {rc}{f_cd(resets if resets > 0 else None)}{R}")

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
    # ── BRN — burn rate bar (0 — 1.0 $/min) ──────────────────
    brn_pct = min(100, cpm / BRN_MAX * 100) if cpm and cpm > 0 else 0
    buf.append(f"{c(C_ORN)}{B}BRN{R} {mkbar(brn_pct, c(C_ORN))}")
    buf.append(f"    {c(C_ORN)}{brn_val}{R}")
    # ── CTR — context rate bar (0 — 5.0 %/min) ─────────────
    ctr_pct = min(100, xpm / CTR_MAX * 100) if xpm and xpm > 0 else 0
    buf.append(f"{c(C_YEL)}{B}CTR{R} {mkbar(ctr_pct, c(C_YEL))}")
    buf.append(f"    {c(C_YEL)}{ctr_val}{R}")
    # ── CST — session cost bar (0 — $50) ────────────────────
    cst_pct = min(100, usd / CST_MAX * 100) if usd > 0 else 0
    buf.append(f"{c(C_ORN)}{B}CST{R} {mkbar(cst_pct, c(C_ORN))}")
    buf.append(f"    {c(C_ORN)}{f_cost(usd)}{R}")
    # ── Cross-session cost (TDY / WEK) ─────────────────────
    tdy, wek = cached_cross_session_costs()
    tdy_s = f_cost(tdy) if tdy > 0 else "--"
    wek_s = f_cost(wek) if wek > 0 else "--"
    buf.append(f"    {c(C_ORN)}TDY {tdy_s} {C_DIM}-{R} {c(C_ORN)}WEK {wek_s}{R}")
    buf.append(sep(SW))
    buf.append(f"{c(C_WHT)}NOW {now} {C_DIM}-{R} {c(C_WHT)}UPD {age_s}{R}")
    if added or removed:
        buf.append(f"{c(C_WHT)}LNS{R} {c(C_GRN)}{added:,}{R} {C_DIM}-{R} {c(C_RED)}{removed:,}{R}")

    # ── RLS (release check) ────────────────────────────────
    _rls_maybe_check()
    rls_s = _rls_cache["status"]
    if rls_s == "update" and _rls_cache["remote_ver"]:
        rv = _rls_cache["remote_ver"]
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
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}]qt{R}  {C_DIM}[{R}{C_WHT}r{R}{C_DIM}]rf{R}  {C_DIM}[{R}{C_WHT}s{R}{C_DIM}]se{R}  {C_DIM}[{R}{C_WHT}t{R}{C_DIM}]tk{R}  {C_DIM}[{R}{C_WHT}u{R}{C_DIM}]up{R}  {C_DIM}[{R}{C_WHT}l{R}{C_DIM}]le{R}")

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
    buf.append(f"{C_GRN}APR{R}  {C_DIM}API Ratio (API time / total){R}")
    buf.append(f"{C_DIM} DUR  Session Duration  API  API Time{R}")
    buf.append(f"{C_GRN}CHR{R}  {C_DIM}Cache Hit Rate (read / total){R}")
    buf.append(f"{C_DIM} c.r  Cache Read Tokens  c.w  Cache Write Tokens{R}")
    buf.append(f"{C_CYN}CTX{R}  {C_DIM}Context Window{R}")
    buf.append(f"{C_DIM} in   Input Tokens  out  Output Tokens{R}")
    buf.append(f"{C_YEL}5HL{R}  {C_DIM}5-Hour Rate Limit{R}")
    buf.append(f"{C_YEL}7DL{R}  {C_DIM}7-Day Rate Limit{R}")
    buf.append(f"{C_ORN}BRN{R}  {C_DIM}Burn Rate{R}  {C_DIM}(0 - {BRN_MAX} $/min){R}")
    buf.append(f"{C_YEL}CTR{R}  {C_DIM}Context Rate{R}  {C_DIM}(0 - {CTR_MAX} %/min){R}")
    buf.append(f"{C_ORN}CST{R}  {C_DIM}Session Cost{R}  {C_DIM}(0 - {CST_MAX:.0f} $){R}")
    buf.append(f"{C_ORN}TDY{R}  {C_DIM}Today's Cost (all sessions){R}")
    buf.append(f"{C_ORN}WEK{R}  {C_DIM}Rolling 7-Day Cost (all sessions){R}")
    buf.append(f"{C_WHT}LNS{R}  {C_DIM}Lines Changed ({C_GRN}added{R} {C_RED}removed{R}{C_DIM}){R}")
    buf.append(f"{C_WHT}NOW{R}  {C_DIM}Current Time{R}")
    buf.append(f"{C_WHT}UPD{R}  {C_DIM}Last Data Update{R}")
    buf.append(f"{C_GRN}RLS{R}  {C_DIM}Release Status ({C_GRN}●{R} {C_DIM}Up to date / {C_RED}▶{R} {C_DIM}update available){R}")
    buf.append("")
    buf.append(f"{C_WHT}{B}KEYS{R}")
    buf.append(sep(SW))
    buf.append(f"{C_WHT}q{R}    {C_DIM}Quit{R}")
    buf.append(f"{C_WHT}r{R}    {C_DIM}Refresh (reset stale){R}")
    buf.append(f"{C_WHT}s{R}    {C_DIM}Session picker{R}")
    buf.append(f"{C_WHT}t{R}    {C_DIM}Token usage stats (period: 1=all 2=7d 3=30d){R}")
    buf.append(f"{C_WHT}u{R}    {C_DIM}Update manager (a=apply){R}")
    buf.append(f"{C_WHT}1-9{R}  {C_DIM}Select session / period filter in stats{R}")
    buf.append(f"{C_WHT}l{R}    {C_DIM}Legend toggle{R}")
    buf.append("")
    buf.append(f"{C_WHT}{B}TOKEN USAGE STATS (t){R}")
    buf.append(sep(SW))
    buf.append(f"{C_WHT}SES{R}  {C_DIM}Total Sessions{R}")
    buf.append(f"{C_WHT}DAY{R}  {C_DIM}Active Days{R}")
    buf.append(f"{C_WHT}STK{R}  {C_DIM}Streak (current/best){R}")
    buf.append(f"{C_WHT}LSS{R}  {C_DIM}Longest Session{R}")
    buf.append(f"{C_WHT}TOP{R}  {C_DIM}Most Active Day{R}")
    buf.append("")
    buf.append(f"{C_WHT}{B}UPDATE MANAGER (u){R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}Shows current vs remote version, new commits,{R}")
    buf.append(f"{C_DIM}changelog preview, safety warnings. Press a to apply.{R}")
    buf.append("")
    buf.append(f"{C_WHT}{B}WHY CC AIO MON?{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}claude-monitor   JSONL cost logs     Estimated, not real-time{R}")
    buf.append(f"{C_DIM}ccusage          CLI aggregator      Historical only, no live view{R}")
    buf.append(f"{C_DIM}ccstatusline     Status line script   No TUI, no multi-session{R}")
    buf.append(f"{C_CYN}{B}CC AIO MON       Official stdin JSON  Real-time, stdlib only{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}press any key to close{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Update modal
# ---------------------------------------------------------------------------
_update_result = None  # None=not run, str=output message


def _git_cmd(args, timeout=15):
    """Run git command in repo root, return (returncode, stdout, stderr)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        r = subprocess.run(
            ["git"] + args, cwd=_REPO_ROOT,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
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
    pattern = rf"## v{re.escape(version)}\b.*?(?=\n## v|\Z)"
    m = re.search(pattern, out, re.DOTALL)
    if not m:
        return []
    lines = m.group(0).strip().split("\n")
    return lines[:max_lines]


def _apply_update_action():
    """Run git pull --ff-only and store result."""
    global _update_result
    rc, out, err = _git_cmd(["pull", "--ff-only", "origin", "main"], timeout=30)
    if rc == 0:
        # Syntax check
        bad = []
        for f in ["monitor.py", "statusline.py", "shared.py", "update.py"]:
            fp = _REPO_ROOT / f
            if fp.exists():
                r = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(fp)],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    bad.append(f)
        if bad:
            _update_result = f"Updated but syntax errors in: {', '.join(bad)}"
        else:
            _update_result = "Update complete. Restart monitor to apply."
    else:
        _update_result = f"Update failed: {_sanitize(err or out or 'unknown error')}"


def render_update_modal(cols, rows):
    """Render the update manager modal."""
    global _update_result
    SW = cols
    buf = []
    buf.append(sep(SW))
    up_pad = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}UPDATE{R}{BG_BAR}{' ' * up_pad}{R}")
    buf.append(sep(SW))

    rls_s = _rls_cache["status"]
    remote_ver = _rls_cache.get("remote_ver")

    buf.append(f"{C_WHT}Current:{R}  v{VERSION}")
    if remote_ver:
        buf.append(f"{C_WHT}Remote:{R}   v{remote_ver}")
    else:
        buf.append(f"{C_DIM}Remote:   unknown{R}")
    buf.append("")

    if rls_s == "update" and remote_ver:
        # Show new commits
        commits = _get_new_commits()
        if commits:
            buf.append(f"{C_WHT}{B}New commits:{R}")
            for c_line in commits:
                buf.append(f"  {C_DIM}{_sanitize(c_line)}{R}")
            buf.append("")

        # Changelog preview
        cl = _get_remote_changelog_preview(remote_ver)
        if cl:
            buf.append(f"{C_WHT}{B}Changelog:{R}")
            for c_line in cl:
                buf.append(f"  {C_DIM}{_sanitize(c_line)}{R}")
            buf.append("")

        # Safety warnings
        warns = _update_checks()
        if warns:
            buf.append(f"{C_RED}{B}Warnings:{R}")
            for w in [_sanitize(x) for x in warns]:
                buf.append(f"  {C_RED}{w}{R}")
            buf.append("")

        if _update_result:
            if "complete" in _update_result:
                buf.append(f"{C_GRN}{B}{_update_result}{R}")
            else:
                buf.append(f"{C_RED}{B}{_update_result}{R}")
            buf.append("")
            buf.append(f"{C_DIM}press any key to close{R}")
        elif warns:
            buf.append(sep(SW))
            buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] Apply update (risky){R}  {C_DIM}[any key] Back{R}")
        else:
            buf.append(sep(SW))
            buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] Apply update{R}  {C_DIM}[any key] Back{R}")

    elif rls_s == "ok":
        buf.append(f"{C_GRN}{spin_rls()} Up to date — nothing to do.{R}")
        buf.append("")
        buf.append(f"{C_DIM}press any key to close{R}")

    elif rls_s is None:
        buf.append(f"{C_DIM}{spin_rls()} Checking for updates...{R}")
        buf.append("")
        buf.append(f"{C_DIM}press any key to close{R}")

    else:
        buf.append(f"{C_DIM}Could not check for updates.{R}")
        if rls_s == "no_git":
            buf.append(f"{C_DIM}Git is not installed or not on PATH.{R}")
        elif rls_s == "timeout":
            buf.append(f"{C_DIM}Network timeout — check your connection.{R}")
        else:
            buf.append(f"{C_DIM}Unknown error during check.{R}")
        buf.append("")
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
    "claude-opus-4-6": "Opus 4.6",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
}


def _model_label(model_id):
    return _MODEL_NAMES.get(model_id, model_id)


# Bar colors per model (consistent mapping)
_MODEL_COLORS = [C_CYN, C_GRN, C_YEL, C_ORN, C_RED]


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
        buf.append(f"  {C_DIM}No transcript data found in ~/.claude/projects/{R}")
        buf.append(f"  {C_DIM}(stats appear after at least one CC session){R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[1]all  [2]7d  [3]30d{R}")
        buf.append("")
        buf.append(f"{C_DIM}press any other key to close{R}")
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

    buf.append(f"  {C_WHT}SES{R} {C_CYN}{n_sessions}{R}  {C_WHT}DAY{R} {C_CYN}{n_days}{R}  {C_WHT}STK{R} {C_CYN}{current_streak}d{R}{C_DIM}/{longest_streak}d{R}")
    buf.append(f"  {C_WHT}LSS{R} {C_CYN}{f_dur(longest_ms)}{R}  {C_WHT}TOP{R} {C_CYN}{most_active}{R}")
    buf.append(sep(SW))

    # -- Models section --
    # Sort by total tokens descending
    total_all = sum(m["input"] + m["output"] for m in models.values())
    sorted_models = sorted(
        models.items(), key=lambda kv: kv[1]["input"] + kv[1]["output"], reverse=True
    )

    for i, (mid, st) in enumerate(sorted_models):
        color = _MODEL_COLORS[i % len(_MODEL_COLORS)]
        label = _model_label(mid)
        total_m = st["input"] + st["output"]
        pct = total_m / total_all * 100 if total_all else 0
        buf.append(f"  {color}{B}{label}{R}  {C_DIM}({pct:.1f}%){R}")
        buf.append(f"  {mkbar(pct, color)}")
        buf.append(
            f"    {C_DIM}In:{R} {color}{f_tok(st['input'])}{R}"
            f"  {C_DIM}Out:{R} {color}{f_tok(st['output'])}{R}"
            f"  {C_DIM}Calls:{R} {color}{st['calls']:,}{R}"
        )
        buf.append("")

    # Totals
    total_in = sum(m["input"] for m in models.values())
    total_out = sum(m["output"] for m in models.values())
    total_calls = sum(m["calls"] for m in models.values())
    buf.append(sep(SW))
    buf.append(
        f"  {C_WHT}{B}Total{R}"
        f"  {C_DIM}In:{R} {C_WHT}{f_tok(total_in)}{R}"
        f"  {C_DIM}Out:{R} {C_WHT}{f_tok(total_out)}{R}"
        f"  {C_DIM}Calls:{R} {C_WHT}{total_calls:,}{R}"
    )

    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}1{R}{C_DIM}]all  [{R}{C_WHT}2{R}{C_DIM}]7d  [{R}{C_WHT}3{R}{C_DIM}]30d{R}")
    buf.append("")
    buf.append(f"{C_DIM}press any other key to close{R}")

    _fit_buf_height(buf, rows, clip_tail=True)
    return buf


# ---------------------------------------------------------------------------
# Session picker
# ---------------------------------------------------------------------------
def render_picker(sessions, cols, rows):
    W = cols
    buf = []
    buf.append(sep(W))
    buf.append(f"  {C_WHT}{B}CC AIO MON {VERSION}{R}")
    buf.append(sep(W))
    buf.append("")

    if not sessions:
        buf.append(f"  {C_DIM}Waiting for Claude Code session...{R}")
        buf.append(f"  {C_DIM}Start a session, then come back here.{R}")
    else:
        buf.append(f"  {C_WHT}{B}Active Sessions{R}")
        buf.append("")
        for i, s in enumerate(sessions):
            stale = f" {C_RED}(stale){R}" if s["stale"] else f" {C_GRN}(live){R}"
            nm = s["session_name"] or s["id"][:16]
            line = f"  {C_CYN}[{i + 1}]{R}  {B}{nm}{R}  {C_DIM}{s['model']}{R}  {C_DIM}{s['cwd']}{R}{stale}"
            buf.append(truncate(line, W))

    buf.append("")
    buf.append(sep(W))
    buf.append(f"  {C_DIM}press 1-9 to select {H} q to quit{R}")

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
    parser = argparse.ArgumentParser(description="Claude AIO Monitor")
    parser.add_argument("--session", help="Session ID")
    parser.add_argument("--list", action="store_true", help="List sessions")
    parser.add_argument("--refresh", type=int, default=500, help="Refresh ms")
    args = parser.parse_args()

    args.refresh = max(100, min(60000, args.refresh))

    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
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
    signal.signal(signal.SIGTERM, lambda *a: (cleanup(), sys.exit(0)))

    sid = args.session
    if sid and not _SID_RE.match(sid):
        print(f"Invalid session ID: {sid}")
        return
    global _update_result
    show_legend = False
    show_stats = None  # None=off, "all"/"7d"/"30d"=active period
    show_update = False
    _update_result = None
    _render_errors = 0
    last_mt = 0
    last_seen = 0  # monotonic timestamp of last successful data load
    last_data = None
    last_size = (0, 0)
    last_hist_mt = 0
    last_hist = []
    data_interval = args.refresh / 1000
    tick = 0.05  # 50ms tick for responsive resize
    since_data = 0

    try:
        while True:
            cols, rows = shutil.get_terminal_size((80, 24))
            size_changed = (cols, rows) != last_size
            last_size = (cols, rows)
            since_data += tick

            # Always poll keyboard
            k = poll_key()
            if k == "q":
                break
            # ── Modal-specific handlers first (priority) ──
            elif show_update and k == "a" and _update_result is None:
                _apply_update_action()
            elif show_update and k is not None:
                show_update = False
                _update_result = None
            elif show_stats is not None and k in ("1", "2", "3"):
                show_stats = _PERIOD_CYCLE[int(k) - 1]
            elif show_stats is not None and k is not None:
                show_stats = None
            elif show_legend and k is not None:
                show_legend = False
            # ── Global handlers ──
            elif k == "r":
                last_mt = 0
                last_seen = time.monotonic()
            elif k == "s":
                sid = None
                last_data = None
                last_mt = 0
                last_seen = 0
                last_hist_mt = 0
                last_hist = []
            elif k == "l":
                show_legend = not show_legend
                show_stats = None
                show_update = False
                _update_result = None
            elif k == "t":
                if show_stats is not None:
                    show_stats = None
                else:
                    show_stats = "all"
                    show_legend = False
                    show_update = False
                    _update_result = None
            elif k == "u":
                show_update = not show_update
                if not show_update:
                    _update_result = None
                show_legend = False
                show_stats = None

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
                if len(active) == 1:
                    sid = active[0]["id"]
                elif not sessions:
                    flush(render_picker([], cols, rows), cols)
                    time.sleep(tick)
                    continue
                else:
                    flush(render_picker(sessions, cols, rows), cols)
                    if k and k.isdigit():
                        idx = int(k) - 1
                        if 0 <= idx < len(sessions):
                            sid = sessions[idx]["id"]
                            last_seen = time.monotonic()
                    time.sleep(tick)
                    continue

            # Load state (only on data interval, not resize)
            if since_data >= data_interval:
                since_data = 0
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
                flush(render_frame(last_data, last_hist, cols, rows, show_legend, stale=is_stale), cols)
            except (TypeError, ValueError, KeyError, ZeroDivisionError, OverflowError, OSError) as e:
                _render_errors += 1
                if _render_errors <= 3:
                    sys.stderr.write(f"render error #{_render_errors}: {e}\n")

            time.sleep(tick)

    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
