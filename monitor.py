#!/usr/bin/env python3
"""Claude AIO Monitor — fullscreen TUI dashboard for Claude Code.

Single-file, zero-dependency terminal dashboard.
Reads shared state from statusline.py via temp files.

Usage:
    python monitor.py                  # auto-detect session
    python monitor.py --session ID     # specific session
    python monitor.py --list           # list active sessions
"""

import argparse
import atexit
import json
import pathlib
import platform
import re
import shutil
import signal
import sys
import tempfile
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Platform — keyboard input abstraction
# ---------------------------------------------------------------------------
IS_WIN = platform.system() == "Windows"
_term_state = None

if IS_WIN:
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
C_CYN = E + "38;2;136;192;208m"
C_WHT = E + "38;2;216;222;233m"
C_DIM = E + "38;2;76;86;106m"
C_FG = E + "38;2;180;186;200m"

VERSION = "1.4"
_SID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
_ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")
MAX_FILE_SIZE = 1_048_576  # 1 MB
STALE_THRESHOLD = 1800  # 30 min — Claude Code emits no events during idle


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
    if n is None or n == 0:
        return "--"
    if n < 1000:
        return f"{int(n):,}"
    if n < 100_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.0f}M"


def f_cost(usd):
    if usd is None or usd <= 0:
        return "--"
    if usd < 0.01:
        return f"{usd:.4f} $"
    return f"{usd:.2f} $"


def f_cd(epoch):
    if epoch is None:
        return "--"
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
    if ms is None or ms <= 0:
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


# ---------------------------------------------------------------------------
# Layout helpers — no borders, just lines
# ---------------------------------------------------------------------------
def sep(w):
    return C_DIM + H * w + R


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
        if f.name.endswith(".tmp"):
            continue
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
# Burn rate
# ---------------------------------------------------------------------------
def calc_rates(hist):
    if len(hist) < 2:
        return None, None
    t0, t1 = hist[0].get("t", 0), hist[-1].get("t", 0)
    dt = t1 - t0
    if dt < 10:
        return None, None
    c0 = _num(hist[0].get("cost", {}).get("total_cost_usd"))
    c1 = _num(hist[-1].get("cost", {}).get("total_cost_usd"))
    x0 = _num(hist[0].get("context_window", {}).get("used_percentage"))
    x1 = _num(hist[-1].get("context_window", {}).get("used_percentage"))
    return (c1 - c0) / dt * 60, (x1 - x0) / dt * 60


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
    ctx_total = cw.get("context_window_size", 0)
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
    hp = [f"{C_WHT}{B}CC AIO MON {VERSION}{R}", f"{C_CYN}{model_str}{R}"]
    buf.append("  ".join(hp))

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

    buf.append(sep(SW))
    buf.append("")

    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cr = usage.get("cache_read_input_tokens", 0)
    cwt = usage.get("cache_creation_input_tokens", 0)

    # ── APR — API Ratio ─────────────────────────────────────
    if dur > 0:
        apr_pct = round(api_dur / dur * 100, 1)
        buf.append(f"{c(C_GRN)}{B}APR{R} {mkbar(apr_pct, c(C_GRN))}")
        buf.append("")
        buf.append(f"    {c(C_GRN)}DUR {f_dur(dur)}{R} {C_DIM}/{R} {c(C_GRN)}API {f_dur(api_dur)}{R}")
    else:
        buf.append(f"{c(C_GRN)}{B}APR{R} {mkbar(0, C_DIM)}")
        buf.append("")
        buf.append(f"    {C_DIM}no active session{R}")
    buf.append("")
    buf.append(sep(SW))
    buf.append("")

    # ── CHR — Cache Hit Rate ────────────────────────────────
    if any([cr, cwt]):
        total_cache = cr + cwt
        chr_pct = round(cr / total_cache * 100, 1) if total_cache > 0 else 0
        buf.append(f"{c(C_WHT)}{B}CHR{R} {mkbar(chr_pct, c(C_WHT))}")
        buf.append("")
        buf.append(f"    {c(C_GRN)}c.r:{R} {c(C_GRN)}{f_tok(cr)}{R} {C_DIM}/{R} {c(C_GRN)}c.w:{R} {c(C_GRN)}{f_tok(cwt)}{R}")
    else:
        buf.append(f"{c(C_WHT)}{B}CHR{R} {mkbar(0, C_DIM)}")
        buf.append("")
        buf.append(f"    {C_DIM}no cache data{R}")
    buf.append("")
    buf.append(sep(SW))
    buf.append("")

    # ── CTX ─────────────────────────────────────────────────
    ctx_used = int(ctx_total * ctx_pct / 100) if ctx_total else 0
    buf.append(f"{c(C_CYN)}{B}CTX{R} {mkbar(ctx_pct, c(C_CYN))}")
    buf.append("")
    warn = f"  {c(C_RED)}{B}! >200k{R}" if exceeds else ""
    if any([inp, out]):
        buf.append(f"    {c(C_CYN)}{f_tok(ctx_used)}{R} {C_DIM}/{R} {c(C_CYN)}{f_tok(ctx_total)}{R}{warn} {C_DIM}/{R} {c(C_WHT)}in:{R} {c(C_WHT)}{f_tok(inp)}{R} {C_DIM}/{R} {c(C_WHT)}out:{R} {c(C_WHT)}{f_tok(out)}{R}")
    else:
        buf.append(f"    {c(C_CYN)}{f_tok(ctx_used)}{R} {C_DIM}/{R} {c(C_CYN)}{f_tok(ctx_total)}{R}{warn}")
        buf.append(f"    {C_DIM}awaiting first api call{R}")
    buf.append("")
    buf.append(sep(SW))
    buf.append("")

    # ── 5HL ─────────────────────────────────────────────────
    if rl:
        fh = rl.get("five_hour")
        if fh:
            pct = round(_num(fh.get("used_percentage")), 1)
            resets = fh.get("resets_at")
            if resets and resets < time.time():
                pct = 0.0
            buf.append(f"{c(C_YEL)}{B}5HL{R} {mkbar(pct, c(C_YEL))}")
            buf.append("")
            buf.append(f"    {c(C_FG)}{f_cd(resets)}{R} {c(C_RED)}to reset{R}")
            buf.append("")

        # ── 7DL ─────────────────────────────────────────────
        sd = rl.get("seven_day")
        if sd:
            pct = round(_num(sd.get("used_percentage")), 1)
            resets = sd.get("resets_at")
            if resets and resets < time.time():
                pct = 0.0
            buf.append(f"{c(C_GRN)}{B}7DL{R} {mkbar(pct, c(C_GRN))}")
            buf.append("")
            buf.append(f"    {c(C_FG)}{f_cd(resets)}{R} {c(C_RED)}to reset{R}")
    else:
        buf.append(f"{C_DIM}Rate limits: subscription data unavailable{R}")

    buf.append("")
    buf.append(sep(SW))

    # ── LNS ─────────────────────────────────────────────────
    if added or removed:
        buf.append(f"{C_DIM}LNS{R}  {c(C_GRN)}+{added:,}{R} {c(C_RED)}-{removed:,}{R}")

    buf.append(sep(SW))
    buf.append("")

    # ── Stats ───────────────────────────────────────────────
    buf.append(f"{c(C_CYN)}CST  {B}{f_cost(usd)}{R}")
    brn_val = f"{cpm:.4f} $ / min" if cpm and cpm > 0.0001 else "collecting..."
    buf.append(f"{c(C_YEL)}BRN  {B}{brn_val}{R}")
    ctr_val = f"{xpm:.2f} % / min" if xpm and xpm > 0.001 else "--"
    buf.append(f"{c(C_YEL)}CTR  {ctr_val}{R}")
    ctf_val = "--"
    if xpm and xpm > 0 and ctx_pct < 100:
        rem_pct = 100 - ctx_pct
        ctf_val = datetime.fromtimestamp(time.time() + (rem_pct / xpm) * 60).strftime("%H:%M")
    buf.append(f"{c(C_RED)}CTF  {B}{ctf_val}{R}")
    buf.append("")
    now = datetime.now().strftime("%H:%M:%S")
    buf.append(f"{c(C_WHT)}NOW  {B}{now}{R}")

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
    buf.append(f"{c(C_GRN)}UPD  {age_s}{R}")

    buf.append("")

    # ── Footer ──────────────────────────────────────────────
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}]{R} {C_DIM}quit{R}  {C_DIM}[{R}{C_WHT}r{R}{C_DIM}]{R} {C_DIM}refresh{R}  {C_DIM}[{R}{C_WHT}s{R}{C_DIM}]{R} {C_DIM}sessions{R}  {C_DIM}[{R}{C_WHT}l{R}{C_DIM}]{R} {C_DIM}legend{R}")

    # Shrink: remove empty lines bottom-up (sections compress smoothly)
    target = rows - 1
    while len(buf) > target:
        _shrunk = False
        for i in range(len(buf) - 1, -1, -1):
            if buf[i] == "":
                buf.pop(i)
                _shrunk = True
                break
        if not _shrunk:
            break
    # Last resort: clip from bottom if still too tall
    if len(buf) > target:
        buf = buf[:target]

    # Pad if terminal is bigger
    while len(buf) < target:
        buf.append("")

    return buf


# ---------------------------------------------------------------------------
# Legend overlay
# ---------------------------------------------------------------------------
def render_legend(cols, rows):
    SW = cols
    buf = []
    buf.append(sep(SW))
    buf.append(f"{C_WHT}{B}LEGEND{R}")
    buf.append(sep(SW))
    buf.append(f"{C_GRN}APR  API Ratio (API time / total){R}")
    buf.append(f"{C_GRN}DUR  Session Duration{R}")
    buf.append(f"{C_GRN}API  API Time{R}")
    buf.append(f"{C_WHT}CHR  Cache Hit Rate (read / total){R}")
    buf.append(f"{C_GRN}c.r  Cache Read Tokens{R}")
    buf.append(f"{C_GRN}c.w  Cache Write Tokens{R}")
    buf.append(f"{C_CYN}CTX  Context Window{R}")
    buf.append(f"{C_YEL}5HL  5-Hour Rate Limit{R}")
    buf.append(f"{C_GRN}7DL  7-Day Rate Limit{R}")
    buf.append(f"{C_DIM}LNS  Lines Changed{R}")
    buf.append(f"{C_CYN}CST  Session Cost{R}")
    buf.append(f"{C_YEL}BRN  Burn Rate ($ / min){R}")
    buf.append(f"{C_YEL}CTR  Context Rate (% / min){R}")
    buf.append(f"{C_RED}CTF  Context Full (ETA){R}")
    buf.append(f"{C_WHT}NOW  Current Time{R}")
    buf.append(f"{C_GRN}UPD  Last Data Update{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}press any key to close{R}")

    while len(buf) < rows - 1:
        buf.append("")

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

    while len(buf) < rows - 1:
        buf.append("")

    return buf


# ---------------------------------------------------------------------------
# Screen flush
# ---------------------------------------------------------------------------
def flush(buf, cols=None):
    if cols is None:
        cols = shutil.get_terminal_size((80, 24)).columns
    out = [SYNC_ON, HOME]
    for line in buf:
        out.append(truncate(line, cols))
        out.append(EL)
        out.append("\n")
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
                h = load_history(sid)
                if h:
                    last_hist = h
                last_hist_mt = hmt
            is_stale = (time.monotonic() - last_seen) > STALE_THRESHOLD if last_seen else False
            try:
                flush(render_frame(last_data, last_hist, cols, rows, show_legend, stale=is_stale), cols)
            except (TypeError, ValueError, KeyError, ZeroDivisionError):
                pass  # corrupted data — skip frame, retry next tick

            time.sleep(tick)

    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
