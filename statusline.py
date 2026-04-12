#!/usr/bin/env python3
"""Claude AIO Monitor — statusline for Claude Code.

Stdlib-only status line script (uses shared.py for helpers and BRN/CTR).
Reads JSON from stdin (Claude Code status line protocol), outputs ANSI-colored text.
Responsive layout adapts to terminal width.

Config env vars:
    CLAUDE_STATUS_WARN  — yellow threshold % (default 50)
    CLAUDE_STATUS_CRIT  — red threshold % (default 80)
"""

import json
import os
import pathlib
import platform
import re
import struct
import sys
import tempfile
import time
from datetime import datetime

from shared import calc_rates as _calc_rates, _num, _sanitize, f_dur, f_tok, f_cost

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
try:
    WARN = int(os.environ.get("CLAUDE_STATUS_WARN", "50"))
except (ValueError, TypeError):
    WARN = 50
try:
    CRIT = int(os.environ.get("CLAUDE_STATUS_CRIT", "80"))
except (ValueError, TypeError):
    CRIT = 80

# Session ID validation — prevent path traversal
_SID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
_ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")

# ---------------------------------------------------------------------------
# ANSI — Nord truecolor (same palette as monitor.py)
# ---------------------------------------------------------------------------
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
BG_BAR = E + "48;2;46;52;64m"  # Nord polar night — full-width bar background
RB = R + BG_BAR                # Reset formatting but keep bar background
EL = E + "K"  # Erase to end of line (fills with current bg)


_IS_WIN = platform.system() == "Windows"


def _get_terminal_width(fallback: int = 80) -> int:
    """Reliable terminal width even when stdout/stdin/stderr are piped.

    Claude Code runs statusline.py as a subprocess with all fds piped, so
    shutil.get_terminal_size() always returns the fallback. We bypass this by
    opening the controlling terminal device directly:
      Windows: \\\\.\\CON  (always available, not affected by pipe)
      Unix:    /dev/tty   (controlling terminal of the process)
    """
    # 1. Caller-set env var (most reliable in pipe scenarios)
    try:
        val = int(os.environ.get("COLUMNS", ""))
        if val > 0:
            return val
    except (ValueError, TypeError):
        pass

    # 2. Standard fds — works when not piped
    for fd in (2, 0, 1):
        try:
            return os.get_terminal_size(fd).columns
        except OSError:
            continue

    # 3. Open controlling terminal directly — bypasses pipe redirection
    if _IS_WIN:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # CONOUT$ is the Windows console output device — works even when
            # stdout/stderr are piped (Claude Code subprocess context)
            h = kernel32.CreateFileW(
                "CONOUT$",
                0x80000000,  # GENERIC_READ
                0x3,         # FILE_SHARE_READ | FILE_SHARE_WRITE
                None,
                3,           # OPEN_EXISTING
                0,
                None,
            )
            if h not in (-1, 0):
                csbi = ctypes.create_string_buffer(22)
                if kernel32.GetConsoleScreenBufferInfo(h, csbi):
                    _, _, _, _, _, left, _, right, _, _, _ = struct.unpack("hhhhHhhhhhh", csbi.raw)
                    w = right - left + 1
                    kernel32.CloseHandle(h)
                    if w > 0:
                        return w
                kernel32.CloseHandle(h)
        except Exception:
            pass
    else:
        try:
            import fcntl
            import termios
            with open("/dev/tty") as tty:
                packed = fcntl.ioctl(tty, termios.TIOCGWINSZ, b"\x00" * 8)
                _, cols, _, _ = struct.unpack("HHHH", packed)
                if cols > 0:
                    return cols
        except Exception:
            pass

    return fallback


def cpc_base(pct, base):
    """Threshold color — uses metric's own base color below WARN (matches monitor mkbar behavior)."""
    if pct >= CRIT:
        return C_RED
    if pct >= WARN:
        return C_YEL
    return base


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
SEP = f" {C_DIM}\u2502{RB} "  # │
SEP_VLEN = 3  # " │ "


# ---------------------------------------------------------------------------
# Segment builders — each returns (text, visible_length) or None
# ---------------------------------------------------------------------------
def seg_model(data):
    name = _sanitize(data.get("model", {}).get("display_name", ""))
    # Shorten known model names
    name = name.replace(" (1M context)", "").replace(" (200k)", "")
    text = f"{B}{C_WHT}{name}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_ctx(data):
    cw = data.get("context_window", {})
    pct = round(_num(cw.get("used_percentage")))
    total = _num(cw.get("context_window_size"), 0)
    used = int(total * pct / 100) if total else 0
    c = cpc_base(pct, C_CYN)
    tok = f" {C_CYN}{f_tok(used)}/{f_tok(int(total))}{RB}" if total else ""
    text = f"{C_CYN}{B}CTX{RB} {c}{pct}%{RB}{tok}"
    return text, len(_ANSI_RE.sub("", text))


def seg_5hl(data, with_reset=False):
    rl = data.get("rate_limits")
    if not rl:
        return None
    fh = rl.get("five_hour")
    if not fh:
        return None
    pct = round(_num(fh.get("used_percentage")))
    resets = _num(fh.get("resets_at"), 0)
    if resets > 0 and resets < time.time():
        pct = 0
    c = cpc_base(pct, C_YEL)
    reset_s = ""
    if with_reset and resets > 0:
        remaining = resets - time.time()
        if remaining > 0:
            reset_s = f" {C_WHT}RST {f_dur(remaining * 1000)}{RB}"
    text = f"{c}{B}5HL{RB} {c}{pct}%{RB}{reset_s}"
    return text, len(_ANSI_RE.sub("", text))


def seg_7dl(data, with_reset=False):
    rl = data.get("rate_limits")
    if not rl:
        return None
    sd = rl.get("seven_day")
    if not sd:
        return None
    pct = round(_num(sd.get("used_percentage")))
    resets = _num(sd.get("resets_at"), 0)
    if resets > 0 and resets < time.time():
        pct = 0
    c = cpc_base(pct, C_YEL)
    reset_s = ""
    if with_reset and resets > 0:
        remaining = resets - time.time()
        if remaining > 0:
            reset_s = f" {C_WHT}RST {f_dur(remaining * 1000)}{RB}"
    text = f"{c}{B}7DL{RB} {c}{pct}%{RB}{reset_s}"
    return text, len(_ANSI_RE.sub("", text))


def seg_cost(data):
    usd = _num(data.get("cost", {}).get("total_cost_usd"))
    if usd <= 0:
        return None
    text = f"{C_ORN}CST{RB} {C_ORN}{B}{f_cost(usd)}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_dur(data):
    ms = _num(data.get("cost", {}).get("total_duration_ms"))
    if ms <= 0:
        return None
    text = f"{C_WHT}DUR{RB} {C_WHT}{f_dur(ms)}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_chr(data):
    usage = data.get("context_window", {}).get("current_usage", {})
    cr = _num(usage.get("cache_read_input_tokens"))
    cw = _num(usage.get("cache_creation_input_tokens"))
    total = cr + cw
    if total <= 0:
        return None
    pct = round(cr / total * 100, 1)
    # CHR: high = good (cache saves tokens) — inverted: green≥WARN, red<(100-CRIT)
    if pct >= WARN:
        c = C_GRN
    elif pct < (100 - CRIT):
        c = C_RED
    else:
        c = C_YEL
    text = f"{C_GRN}{B}CHR{RB} {c}{pct}%{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_brn(brn):
    if brn is None or brn <= 0.0001:
        return None
    text = f"{C_ORN}BRN{RB} {C_ORN}{B}{brn:.4f} $/m{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_ctr(ctr):
    if ctr is None or ctr <= 0.001:
        return None
    text = f"{C_YEL}CTR{RB} {C_YEL}{ctr:.2f} %/m{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_apr(data):
    dur_ms = _num(data.get("cost", {}).get("total_duration_ms"))
    api_ms = _num(data.get("cost", {}).get("total_api_duration_ms"))
    if dur_ms <= 0:
        return None
    pct = round(api_ms / dur_ms * 100, 1)
    c = cpc_base(pct, C_GRN)
    text = f"{C_GRN}{B}APR{RB} {c}{pct}%{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_ctf(ctr, data):
    if ctr is None or ctr <= 0:
        return None
    ctx_pct = _num(data.get("context_window", {}).get("used_percentage"))
    if ctx_pct >= 100:
        return None
    rem_pct = 100 - ctx_pct
    try:
        eta = datetime.fromtimestamp(time.time() + (rem_pct / ctr) * 60).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return None
    text = f"{C_RED}CTF{RB} {C_RED}{B}{eta}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_tdy(tdy):
    if tdy is None or tdy <= 0:
        return None
    text = f"{C_ORN}{B}TDY{RB} {C_ORN}{f_cost(tdy)}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_wek(wek):
    if wek is None or wek <= 0:
        return None
    text = f"{C_ORN}WEK{RB} {C_ORN}{f_cost(wek)}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_lns(data):
    added = int(_num(data.get("cost", {}).get("total_lines_added")))
    removed = int(_num(data.get("cost", {}).get("total_lines_removed")))
    if not added and not removed:
        return None
    text = f"{C_WHT}LNS{RB} {C_GRN}+{added}{RB} {C_RED}-{removed}{RB}"
    return text, len(_ANSI_RE.sub("", text))


_RLS_PULSE = ["∙", "○", "●", "○"]


def seg_rls():
    """Read RLS status from shared temp file (written by monitor.py)."""
    try:
        rls_file = _DATA_DIR / "rls.json"
        if not rls_file.exists():
            return None
        raw = rls_file.read_text(encoding="utf-8")
        rls = json.loads(raw)
        status = rls.get("status")
        remote_ver = rls.get("remote_ver")
        pulse = _RLS_PULSE[int(time.time() * 2) % len(_RLS_PULSE)]
        if status == "update" and remote_ver:
            text = f"{C_RED}{B}{pulse} v{remote_ver}!{RB}"
            return text, len(_ANSI_RE.sub("", text))
        elif status == "ok":
            text = f"{C_GRN}{pulse} Up to date{RB}"
            return text, len(_ANSI_RE.sub("", text))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def seg_model_usage():
    """Read model usage percentages from shared temp file (written by monitor.py)."""
    try:
        stats_file = _DATA_DIR / "stats.json"
        if not stats_file.exists():
            return None
        raw = stats_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        models = data.get("models", {})
        if not models:
            return None
        # Fixed order: OP SN HK
        _ORDER = [("Opus 4.6", "OP"), ("Sonnet 4.6", "SN"), ("Haiku 4.5", "HK")]
        parts = []
        for name, short in _ORDER:
            pct = models.get(name)
            if pct is not None and pct > 0:
                parts.append(f"{C_WHT}{short} {pct:.0f}%{RB}")
        if not parts:
            return None
        text = " ".join(parts)
        return text, len(_ANSI_RE.sub("", text))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# Layout assembly — 4-line status bar
# ---------------------------------------------------------------------------
def _build_row(left_segs, right_segs, cols):
    """Build one row: left segments │ spacer │ right segments."""
    sv = SEP_VLEN

    def vlen(segs):
        if not segs:
            return 0
        return sum(s[1] for s in segs) + sv * (len(segs) - 1)

    # Drop right segments if too wide
    while right_segs:
        if cols - vlen(left_segs) - vlen(right_segs) - 1 >= 0:
            break
        right_segs.pop()

    # Drop left segments if still too wide
    while left_segs and vlen(left_segs) + vlen(right_segs) + (1 if right_segs else 0) > cols:
        left_segs.pop()

    left_text = SEP.join(s[0] for s in left_segs)
    right_text = SEP.join(s[0] for s in right_segs)

    if right_segs:
        spacer = max(1, cols - vlen(left_segs) - vlen(right_segs))
        return left_text + " " * spacer + right_text
    return left_text


def build_lines(data, cols, brn=None, ctr=None, tdy=None, wek=None):
    """Build 4-line status bar. Returns list of formatted strings."""
    def f(segs):
        return [s for s in segs if s is not None]

    # R1: left=Model │ CTX │ APR │ CHR    right=model usage %
    r1 = _build_row(
        f([seg_model(data), seg_ctx(data), seg_apr(data), seg_chr(data)]),
        f([seg_model_usage()]),
        cols)

    # R2: left=5HL+RST │ 7DL+RST         right=CTF
    r2 = _build_row(
        f([seg_5hl(data, with_reset=True), seg_7dl(data, with_reset=True)]),
        f([seg_ctf(ctr, data)]),
        cols)

    # R3: left=BRN │ CTR │ CST            right=DUR
    r3 = _build_row(
        f([seg_brn(brn), seg_ctr(ctr), seg_cost(data)]),
        f([seg_dur(data)]),
        cols)

    # R4: left=TDY │ WEK │ LNS           right=RLS
    r4 = _build_row(
        f([seg_tdy(tdy), seg_wek(wek), seg_lns(data)]),
        f([seg_rls()]),
        cols)

    return [r for r in [r1, r2, r3, r4] if r]


# Keep build_line for backward compatibility (tests)
def build_line(data, cols, brn=None, ctr=None):
    """Legacy single-line builder."""
    lines = build_lines(data, cols, brn=brn, ctr=ctr)
    return lines[0] if lines else ""


def _calc_cross_session_costs():
    """Lightweight cross-session cost aggregation for TDY/WEK."""
    if not _DATA_DIR.exists():
        return 0.0, 0.0
    today_start = datetime.combine(datetime.today().date(), datetime.min.time()).timestamp()
    week_start = today_start - 6 * 86400
    today_total = 0.0
    week_total = 0.0
    for jl in _DATA_DIR.glob("*.jsonl"):
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
        for ln in raw.splitlines()[-200:]:
            try:
                entries.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        if not entries:
            continue
        entries.sort(key=lambda e: _num(e.get("t"), 0))
        # Today
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
        # Week
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Force UTF-8 on Windows (cp1250/cp1252 can't handle unicode box-drawing)
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout.flush()
        sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", closefd=False)

    try:
        raw = sys.stdin.read(1_048_576)  # 1 MB limit
        if not raw.strip():
            return
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    sid = data.get("session_id", "default")
    if not _SID_RE.match(str(sid)):
        sid = "default"

    # Read history BEFORE writing — needed for BRN/CTR rate computation
    hist = _load_history_for_rates(sid)
    brn, ctr = _calc_rates(hist)

    # Cross-session costs (TDY/WEK) — lightweight scan of JSONL files
    tdy, wek = _calc_cross_session_costs()

    cols = _get_terminal_width(fallback=200)
    lines = build_lines(data, cols, brn=brn, ctr=ctr, tdy=tdy, wek=wek)
    for ln in lines:
        if ln:
            # Pad to full width so background fills entire line
            plain_len = len(_ANSI_RE.sub("", ln))
            pad = max(0, cols - plain_len)
            print(f"{BG_BAR}{ln}{' ' * pad}{R}")

    # Feed data to TUI monitor
    write_shared_state(data)


# ---------------------------------------------------------------------------
# IPC — shared state for monitor.py
# ---------------------------------------------------------------------------
HISTORY_TRIM_TO = 1000
MAX_FILE_SIZE = 1_048_576  # 1 MB — keep in sync with monitor.py
_DATA_DIR = pathlib.Path(tempfile.gettempdir()) / "claude-aio-monitor"


def _load_history_for_rates(sid, n=120):
    """Read last n history entries for BRN/CTR rate computation. Call BEFORE write_shared_state."""
    try:
        p = _DATA_DIR / f"{sid}.jsonl"
        with open(p, "rb") as fh:
            raw = fh.read(MAX_FILE_SIZE * 10 + 1)
        if len(raw) > MAX_FILE_SIZE * 10:
            return []
        out = []
        for ln in raw.decode("utf-8").splitlines()[-n:]:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        return out
    except (OSError, UnicodeDecodeError):
        return []


def write_shared_state(data: dict):
    sid = str(data.get("session_id", "default"))
    if not _SID_RE.match(sid):
        sid = "default"
    try:
        _DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        _DATA_DIR.mkdir(exist_ok=True)
    # Verify permissions on Unix (mkdir mode ignored on Windows)
    if sys.platform != "win32":
        try:
            import stat
            st = _DATA_DIR.stat()
            if stat.S_IMODE(st.st_mode) & 0o077:
                os.chmod(_DATA_DIR, 0o700)
        except OSError:
            pass
    base = _DATA_DIR

    # Serialize once — same rules for snapshot and history (avoid TypeError mid-write)
    try:
        snapshot = json.dumps(data)
        entry = json.dumps({**data, "t": time.time()})
    except (TypeError, ValueError):
        return

    # Atomic write of current state via unpredictable temp file
    target = base / f"{sid}.json"
    snapshot_ok = False
    try:
        fd = tempfile.NamedTemporaryFile(
            dir=base, suffix=".tmp", delete=False, mode="w", encoding="utf-8"
        )
        fd.write(snapshot)
        fd.close()
        pathlib.Path(fd.name).replace(target)
        snapshot_ok = True
    except OSError:
        pass

    # History must stay aligned with the latest snapshot (avoid BRN/CTR vs stale JSON)
    if not snapshot_ok:
        return

    # Append to history JSONL + trim if needed
    hist = base / f"{sid}.jsonl"
    try:
        with open(hist, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
        # Trim based on actual file size
        if hist.stat().st_size > MAX_FILE_SIZE:
            _trim_history(hist)
    except OSError:
        pass


def _trim_history(path: pathlib.Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > HISTORY_TRIM_TO:
            trimmed = "\n".join(lines[-HISTORY_TRIM_TO:]) + "\n"
            fd = tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False,
                mode="w", encoding="utf-8",
            )
            fd.write(trimmed)
            fd.close()
            pathlib.Path(fd.name).replace(path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
