#!/usr/bin/env python3
"""Claude AIO Monitor — fullscreen TUI dashboard for Claude Code.

Terminal dashboard (monitor.py + rates.py). Stdlib only.
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
import sys
import tempfile
import time
from datetime import datetime

from rates import calc_rates

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
# ANSI
# ---------------------------------------------------------------------------
E = "\033["
HIDE_CUR = E + "?25l"
SHOW_CUR = E + "?25h"
ALT_ON = E + "?1049h"
ALT_OFF = E + "?1049l"
CLR = E + "2J"
HOME = E + "H"
EL = E + "K"
SYNC_ON = E + "?2026h"
SYNC_OFF = E + "?2026l"

R = E + "0m"
B = E + "1m"

# Nord-inspired truecolor
C_RED = E + "38;2;191;97;106m"
C_GRN = E + "38;2;163;190;140m"
C_YEL = E + "38;2;235;203;139m"
C_ORN = E + "38;2;208;135;112m"  # nord12 aurora orange — cost/finance
C_CYN = E + "38;2;136;192;208m"
C_WHT = E + "38;2;216;222;233m"
C_DIM = E + "38;2;76;86;106m"
C_FG = E + "38;2;180;186;200m"
BG_BAR = E + "48;2;46;52;64m"  # Nord polar night — header/bar background

VERSION = "1.6.0"
_SID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
_ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")
MAX_FILE_SIZE = 1_048_576  # 1 MB — keep in sync with statusline.py
STALE_THRESHOLD = 1800  # 30 min — Claude Code emits no events during idle

try:
    WARN_BRN = float(os.environ.get("CLAUDE_WARN_BRN", "0.50"))
except (ValueError, TypeError):
    WARN_BRN = 0.50


def _num(v, default=0):
    """Safely coerce value to float (handles None, strings, etc.)."""
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


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


def _sanitize(s):
    """Strip control characters to prevent terminal escape injection."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(s))


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
def f_tok(n):
    n = _num(n, 0)
    if n == 0:
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
    """Inverse color for reset countdown — green=lots of time, red=almost expired."""
    if resets_epoch <= 0:
        return C_DIM
    remaining = resets_epoch - time.time()
    if remaining <= 0:
        return C_GRN  # just reset
    pct_remaining = remaining / window_secs * 100
    if pct_remaining > 50:
        return C_GRN
    if pct_remaining > 20:
        return C_YEL
    return C_RED


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
                warnings.append(f"CTF <{int(eta_mins)}m")
    # 5HL
    rl = data.get("rate_limits") or {}
    fh = rl.get("five_hour") or {}
    fh_pct = _num(fh.get("used_percentage"))
    fh_resets = _num(fh.get("resets_at"), 0)
    if fh_resets > 0 and fh_resets < time.time():
        fh_pct = 0
    if fh_pct > 80:
        warnings.append(f"5HL {fh_pct:.0f}%")
    # 7DL
    sd = rl.get("seven_day") or {}
    sd_pct = _num(sd.get("used_percentage"))
    sd_resets = _num(sd.get("resets_at"), 0)
    if sd_resets > 0 and sd_resets < time.time():
        sd_pct = 0
    if sd_pct > 80:
        warnings.append(f"7DL {sd_pct:.0f}%")
    # BRN
    if cpm and cpm > WARN_BRN:
        warnings.append(f"BRN {cpm:.4f}$/m")
    return warnings


# ---------------------------------------------------------------------------
# Cross-session cost aggregation
# ---------------------------------------------------------------------------
_cost_cache = {"t": 0.0, "today": 0.0, "week": 0.0}


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
        # Today cost = last entry today - last entry before today (baseline)
        baseline_today = 0.0
        final_today = 0.0
        for e in entries:
            cost = _num(e.get("cost", {}).get("total_cost_usd"))
            if _num(e.get("t"), 0) < today_start:
                baseline_today = cost
            else:
                final_today = cost
        today_total += max(0.0, final_today - baseline_today)
        # Week cost
        baseline_week = 0.0
        final_week = 0.0
        for e in entries:
            cost = _num(e.get("cost", {}).get("total_cost_usd"))
            if _num(e.get("t"), 0) < week_start:
                baseline_week = cost
            else:
                final_week = cost
        week_total += max(0.0, final_week - baseline_week)
    return today_total, week_total


def cached_cross_session_costs(ttl=30.0):
    """Cached version — refreshes every ttl seconds."""
    now = time.time()
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
DATA_DIR = pathlib.Path(tempfile.gettempdir()) / "claude-aio-monitor"


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
# Spinner
# ---------------------------------------------------------------------------
# dots12 spinner (cli-spinners) — 56 frames, 50ms interval
_SPIN = [
    "⢀⠀","⡀⠀","⠄⠀","⢂⠀","⡂⠀","⠅⠀","⢃⠀","⡃⠀",
    "⠍⠀","⢋⠀","⡋⠀","⠍⠁","⢋⠁","⡋⠁","⠍⠉","⠋⠉",
    "⠋⠉","⠉⠙","⠉⠙","⠉⠩","⠈⢙","⠈⡙","⢈⠩","⡀⢙",
    "⠄⡙","⢂⠩","⡂⢘","⠅⡘","⢃⠨","⡃⢐","⠍⡐","⢋⠠",
    "⡋⢀","⠍⡁","⢋⠁","⡋⠁","⠍⠉","⠋⠉","⠋⠉","⠉⠙",
    "⠉⠙","⠉⠩","⠈⢙","⠈⡙","⠈⠩","⠀⢙","⠀⡙","⠀⠩",
    "⠀⢘","⠀⡘","⠀⠨","⠀⢐","⠀⡐","⠀⠠","⠀⢀","⠀⡀",
]
_spin_idx = 0
_spin_last = 0.0
SPIN_INTERVAL = 0.05


def spin():
    """Return 2-char dots12 spinner frame, auto-advance on interval."""
    global _spin_idx, _spin_last
    now = time.monotonic()
    if now - _spin_last >= SPIN_INTERVAL:
        _spin_idx += 1
        _spin_last = now
    return _SPIN[_spin_idx % len(_SPIN)]


# line spinner (cli-spinners) — 4 frames, 130ms interval
_LINE = ["-", "\\", "|", "/"]
_line_idx = 0
_line_last = 0.0
LINE_INTERVAL = 0.13


def spin_line():
    """Return 1-char line spinner frame, auto-advance on interval."""
    global _line_idx, _line_last
    now = time.monotonic()
    if now - _line_last >= LINE_INTERVAL:
        _line_idx += 1
        _line_last = now
    return _LINE[_line_idx % len(_LINE)]


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

    W = cols
    buf = []

    # -- Extract data (sanitize to prevent terminal escape injection) --
    m = data.get("model", {})
    model_str = _sanitize(m.get("display_name", "?")).replace("(1M context)", "(1M CTX)")
    sname = _sanitize(data.get("session_name", ""))

    cw = data.get("context_window", {})
    ctx_pct = round(_num(cw.get("used_percentage")), 1)
    ctx_total = _num(cw.get("context_window_size"), 0)
    usage = cw.get("current_usage") or {}
    exceeds = data.get("exceeds_200k_tokens", False)

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

    SW = W

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
        buf.append(f"{C_RED}{B}Session Inactive {spin_line()}{R}{_stale_age}  {c(C_FG)}{session_label}{R}")
    else:
        buf.append(f"{C_GRN}{B}Session Active {spin_line()}{R}  {C_FG}{session_label}{R}")

    # ── Smart warnings (suppressed when stale) ─────────────
    _warns = [] if stale else collect_warnings(data, cpm, xpm)
    if _warns:
        warn_parts = [f"{C_RED}{B}{w}{R}" for w in _warns]
        buf.append(f"  {'   '.join(warn_parts)}")

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
            buf.append(f"{lc}{B}5HL{R} {mkbar(pct)}")
            rc = c(_reset_color(resets, 18000))  # 5h window
            buf.append(f"    {C_WHT}reset in:{R} {rc}{f_cd(resets if resets > 0 else None)}{R}")

        # ── 7DL ─────────────────────────────────────────────
        sd = rl.get("seven_day")
        if sd:
            pct = round(_num(sd.get("used_percentage")), 1)
            resets = _num(sd.get("resets_at"), 0)
            if resets > 0 and resets < time.time():
                pct = 0.0
            lc = c(_limit_color(pct))
            buf.append(f"{lc}{B}7DL{R} {mkbar(pct)}")
            rc = c(_reset_color(resets, 604800))  # 7d window
            buf.append(f"    {C_WHT}reset in:{R} {rc}{f_cd(resets if resets > 0 else None)}{R}")

        if not fh and not sd:
            buf.append(f"{C_DIM}Rate limits: no data{R}")
    else:
        buf.append(f"{C_DIM}Rate limits: subscription data unavailable{R}")

    buf.append(sep(SW))

    # ── Stats (BRN/CTR/CST/TDY/WEK/NOW/UPD/LNS) ─────────────
    brn_val = f"{cpm:.4f} $/min" if cpm and cpm > 0.0001 else "collecting..."
    ctr_val = f"{xpm:.2f} %/min" if xpm and xpm > 0.001 else "--"
    now = datetime.now().strftime("%H:%M:%S")
    sid_str = str(data.get("session_id", "default"))
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

    # ── Footer ──────────────────────────────────────────────
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}]qt{R}  {C_DIM}[{R}{C_WHT}r{R}{C_DIM}]rf{R}  {C_DIM}[{R}{C_WHT}s{R}{C_DIM}]se{R}  {C_DIM}[{R}{C_WHT}l{R}{C_DIM}]le{R}")

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
    buf.append(f"{C_GRN}CHR{R}  {C_DIM}Cache Hit Rate (read / total){R}")
    buf.append(f"{C_CYN}CTX{R}  {C_DIM}Context Window{R}")
    buf.append(f"{C_YEL}5HL{R}  {C_DIM}5-Hour Rate Limit{R}")
    buf.append(f"{C_YEL}7DL{R}  {C_DIM}7-Day Rate Limit{R}")
    buf.append(f"{C_ORN}BRN{R}  {C_DIM}Burn Rate{R}  {C_DIM}(0 - {BRN_MAX} $/min){R}")
    buf.append(f"{C_YEL}CTR{R}  {C_DIM}Context Rate{R}  {C_DIM}(0 - {CTR_MAX} %/min){R}")
    buf.append(f"{C_ORN}CST{R}  {C_DIM}Session Cost{R}  {C_DIM}(0 - {CST_MAX:.0f} $){R}")
    buf.append(f"{C_ORN}TDY{R}  {C_DIM}Today's Cost (all sessions){R}")
    buf.append(f"{C_ORN}WEK{R}  {C_DIM}This Week's Cost (all sessions){R}")
    buf.append(f"{C_WHT}LNS{R}  {C_DIM}Lines Changed ({C_GRN}added{R} {C_RED}removed{R}{C_DIM}){R}")
    buf.append(f"{C_WHT}NOW{R}  {C_DIM}Current Time{R}")
    buf.append(f"{C_WHT}UPD{R}  {C_DIM}Last Data Update{R}")
    buf.append("")
    buf.append(f"{C_WHT}{B}KEYS{R}")
    buf.append(sep(SW))
    buf.append(f"{C_WHT}q{R}    {C_DIM}Quit{R}")
    buf.append(f"{C_WHT}r{R}    {C_DIM}Refresh (reset stale){R}")
    buf.append(f"{C_WHT}s{R}    {C_DIM}Session picker{R}")
    buf.append(f"{C_WHT}l{R}    {C_DIM}Legend toggle{R}")
    buf.append(f"{C_WHT}1-9{R}  {C_DIM}Select session{R}")
    buf.append(sep(SW))
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
    show_legend = False
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
            elif k == "r":
                last_mt = 0
                last_seen = time.monotonic()  # reset stale on manual refresh
            elif k == "s":
                sid = None
                last_data = None
                last_mt = 0
                last_seen = 0
                last_hist_mt = 0
                last_hist = []
            elif k == "l":
                show_legend = not show_legend
            elif show_legend and k is not None:
                show_legend = False

            # Render every tick when we have data (for spinner), reload data on interval
            need_render = size_changed or last_data is not None or since_data >= data_interval
            if not need_render:
                time.sleep(tick)
                continue

            # Auto-detect / pick session
            if sid is None:
                sessions = list_sessions()
                active = [s for s in sessions if not s["stale"]]
                if len(active) == 1 and len(sessions) == 1:
                    sid = active[0]["id"]
                elif not sessions:
                    flush(render_picker([], cols, rows), cols)
                    if k == "q":
                        break
                    time.sleep(tick)
                    continue
                else:
                    flush(render_picker(sessions, cols, rows), cols)
                    if k == "q":
                        break
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
