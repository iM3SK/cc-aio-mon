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
import re
import shutil
import sys
import tempfile
import time

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
C_CYN = E + "38;2;136;192;208m"
C_WHT = E + "38;2;216;222;233m"
C_DIM = E + "38;2;76;86;106m"


def cpc(pct):
    if pct >= CRIT:
        return C_RED
    if pct >= WARN:
        return C_YEL
    return C_GRN


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
SEP = f" {C_DIM}\u2502{R} "  # │
SEP_VLEN = 3  # " │ "



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
    text = f"{B}{C_WHT}{name}{R}"
    return text, len(_ANSI_RE.sub("", text))


def _num(v, default=0):
    """Safely coerce value to float (handles None, strings, etc.)."""
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def seg_ctx(data):
    cw = data.get("context_window", {})
    pct = round(_num(cw.get("used_percentage")))
    total = cw.get("context_window_size", 0) or 0
    used = int(total * pct / 100) if total else 0
    c = cpc(pct)
    tok = f" {C_DIM}{f_tok(used)}/{f_tok(total)}{R}" if total else ""
    text = f"{C_CYN}{B}CTX{R} {c}{pct}%{R}{tok}"
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
    c = cpc(pct)
    text = f"{C_YEL}{B}5HL{R} {c}{pct}%{R}"
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
    c = cpc(pct)
    text = f"{C_GRN}{B}7DL{R} {c}{pct}%{R}"
    return text, len(_ANSI_RE.sub("", text))


def seg_cost(data):
    usd = _num(data.get("cost", {}).get("total_cost_usd"))
    if usd <= 0:
        return None
    text = f_cost(usd)
    out = f"{C_CYN}{B}CST{R} {C_CYN}{text}{R}"
    return out, len(_ANSI_RE.sub("", out))



def seg_dur(data):
    ms = _num(data.get("cost", {}).get("total_duration_ms"))
    if ms <= 0:
        return None
    d = f_dur(ms)
    text = f"{C_GRN}{B}DUR{R} {C_GRN}{d}{R}"
    return text, len(_ANSI_RE.sub("", text))


# ---------------------------------------------------------------------------
# Layout assembly
# ---------------------------------------------------------------------------
def build_line(data, cols):
    segments = []
    used = 0
    sep_vlen = SEP_VLEN

    def try_add(seg):
        nonlocal used
        if seg is None:
            return False
        text, vlen = seg
        needed = vlen + (sep_vlen if segments else 0)
        if used + needed > cols:
            return False
        segments.append(text)
        used += vlen + (sep_vlen if len(segments) > 1 else 0)
        return True

    try_add(seg_model(data))
    try_add(seg_cost(data))
    try_add(seg_ctx(data))
    try_add(seg_5hl(data))
    try_add(seg_7dl(data))
    try_add(seg_dur(data))

    return SEP.join(segments)


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

    cols = shutil.get_terminal_size((80, 24)).columns
    line = build_line(data, cols)
    if line:
        print(line)

    # Feed data to TUI monitor
    write_shared_state(data)


# ---------------------------------------------------------------------------
# IPC — shared state for monitor.py
# ---------------------------------------------------------------------------
HISTORY_TRIM_TO = 1000
MAX_FILE_SIZE = 1_048_576  # 1 MB


def write_shared_state(data: dict):
    sid = data.get("session_id", "default")
    if not _SID_RE.match(str(sid)):
        sid = "default"
    base = pathlib.Path(tempfile.gettempdir()) / "claude-aio-monitor"
    try:
        base.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        base.mkdir(exist_ok=True)

    # Atomic write of current state via unpredictable temp file
    target = base / f"{sid}.json"
    try:
        fd = tempfile.NamedTemporaryFile(
            dir=base, suffix=".tmp", delete=False, mode="w", encoding="utf-8"
        )
        fd.write(json.dumps(data))
        fd.close()
        pathlib.Path(fd.name).replace(target)
    except OSError:
        pass

    # Append to history JSONL + trim if needed
    hist = base / f"{sid}.jsonl"
    try:
        entry = json.dumps({**data, "t": time.time()})
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
