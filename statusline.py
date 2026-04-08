#!/usr/bin/env python3
"""Claude AIO Monitor — statusline for Claude Code.

Single-file, zero-dependency status line script.
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
import shutil
import struct
import sys
import tempfile
import time
from datetime import datetime

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


def cpc(pct):
    """Threshold color — base is always C_GRN (for APR, 7DL)."""
    if pct >= CRIT:
        return C_RED
    if pct >= WARN:
        return C_YEL
    return C_GRN


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


def _num(v, default=0):
    """Safely coerce value to float (handles None, strings, etc.)."""
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


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


# ---------------------------------------------------------------------------
# Segment builders — each returns (text, visible_length) or None
# ---------------------------------------------------------------------------
def _sanitize(s):
    """Strip control characters to prevent terminal escape injection."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(s))


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


def seg_5hl(data):
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
    text = f"{c}{B}5HL{RB} {c}{pct}%{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_7dl(data):
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
    text = f"{c}{B}7DL{RB} {c}{pct}%{RB}"
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
    text = f"{C_DIM}DUR{RB} {C_DIM}{f_dur(ms)}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_chr(data):
    usage = data.get("context_window", {}).get("current_usage", {})
    cr = _num(usage.get("cache_read_input_tokens"))
    cw = _num(usage.get("cache_creation_input_tokens"))
    total = cr + cw
    if total <= 0:
        return None
    pct = round(cr / total * 100, 1)
    # CHR: high = good (cache saves tokens) — inverted thresholds
    if pct >= WARN:
        c = C_GRN
    elif pct >= (100 - CRIT):
        c = C_YEL
    else:
        c = C_RED
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


def seg_lns(data):
    added = int(_num(data.get("cost", {}).get("total_lines_added")))
    removed = int(_num(data.get("cost", {}).get("total_lines_removed")))
    if not added and not removed:
        return None
    text = f"{C_DIM}LNS{RB} {C_GRN}+{added:,}{RB} {C_RED}-{removed:,}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_ctf(ctr, data):
    if ctr is None or ctr <= 0:
        return None
    ctx_pct = _num(data.get("context_window", {}).get("used_percentage"))
    if ctx_pct >= 100:
        return None
    rem_pct = 100 - ctx_pct
    eta = datetime.fromtimestamp(time.time() + (rem_pct / ctr) * 60).strftime("%H:%M")
    text = f"{C_RED}CTF{RB} {C_RED}{B}{eta}{RB}"
    return text, len(_ANSI_RE.sub("", text))


def seg_now():
    now_str = datetime.now().strftime("%H:%M:%S")
    text = f"{C_DIM}NOW{RB} {C_DIM}{now_str}{RB}"
    return text, len(_ANSI_RE.sub("", text))


# ---------------------------------------------------------------------------
# Layout assembly
# ---------------------------------------------------------------------------
def build_line(data, cols, brn=None, ctr=None):
    sv = SEP_VLEN

    def vlen(segs):
        if not segs:
            return 0
        return sum(s[1] for s in segs) + sv * (len(segs) - 1)

    left_segs = [s for s in [
        seg_model(data),
        seg_apr(data),
        seg_ctx(data),
        seg_chr(data),
        seg_5hl(data),
        seg_7dl(data),
    ] if s is not None]

    right_segs = [s for s in [
        seg_brn(brn),
        seg_ctr(ctr),
        seg_ctf(ctr, data),
        seg_cost(data),
        seg_dur(data),
        seg_now(),
    ] if s is not None]

    # Drop right segments from right until spacer >= 1
    while right_segs:
        if cols - vlen(left_segs) - vlen(right_segs) >= 1:
            break
        right_segs.pop()

    spacer = cols - vlen(left_segs) - vlen(right_segs)
    if spacer < 1:
        spacer = 1

    left_text = SEP.join(s[0] for s in left_segs)
    right_text = SEP.join(s[0] for s in right_segs)

    if right_segs:
        return left_text + " " * spacer + right_text
    return left_text


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

    cols = _get_terminal_width(fallback=200)
    line = build_line(data, cols, brn=brn, ctr=ctr)
    if line:
        print(f"{BG_BAR}{line}{EL}{R}")

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


def _calc_rates(hist):
    """Return (brn $/min, ctr %/min) from history, or (None, None) if insufficient data."""
    if len(hist) < 2:
        return None, None
    try:
        t0 = float(hist[0].get("t", 0))
        t1 = float(hist[-1].get("t", 0))
        dt = t1 - t0
        if dt < 10 or t0 < 1577836800:  # minimum 10s window, timestamps post-2020
            return None, None
        c0 = _num(hist[0].get("cost", {}).get("total_cost_usd"))
        c1 = _num(hist[-1].get("cost", {}).get("total_cost_usd"))
        brn = (c1 - c0) / dt * 60 if c1 >= c0 else None
        x0 = _num(hist[0].get("context_window", {}).get("used_percentage"))
        x1 = _num(hist[-1].get("context_window", {}).get("used_percentage"))
        ctr = (x1 - x0) / dt * 60 if x1 >= x0 else None
        return brn, ctr
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None


def write_shared_state(data: dict):
    sid = data.get("session_id", "default")
    if not _SID_RE.match(str(sid)):
        sid = "default"
    base = pathlib.Path(tempfile.gettempdir()) / "claude-aio-monitor"
    try:
        base.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        base.mkdir(exist_ok=True)

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
