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
H = "\u2500"  # ─
BF = "\u2588"  # █
SH = "\u2591"  # ░
SEP = f" {C_DIM}{H}{R} "
BAR_W = 10


def mkbar(pct, color=None):
    pct = max(0.0, min(100.0, pct))
    if color is None:
        color = cpc(pct)
    filled = round(pct * BAR_W / 100)
    empty = BAR_W - filled
    return f"{C_DIM}[{R}{color}{BF * filled}{R}{C_DIM}{SH * empty}]{R}"


def f_dur(ms):
    if ms is None or ms <= 0:
        return "--"
    s = int(ms / 1000)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


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
    return re.sub(r"[\x00-\x1f\x7f]", "", str(s))


def seg_model(data):
    name = _sanitize(data.get("model", {}).get("display_name", ""))
    name = name.replace("(1M context)", "(1M CTX)")
    text = f"{B}{C_WHT}{name}{R}"
    return text, len(name)


def seg_ctx(data):
    cw = data.get("context_window", {})
    pct = round(cw.get("used_percentage") or 0)
    c = cpc(pct)
    bar = mkbar(pct, C_CYN)
    text = f"{C_CYN}{B}CTX{R} {bar} {c}{pct} %{R}"
    return text, 4 + BAR_W + 2 + 1 + len(str(pct)) + 2


def seg_5hl(data):
    rl = data.get("rate_limits")
    if not rl:
        return None
    fh = rl.get("five_hour")
    if not fh:
        return None
    pct = round(fh.get("used_percentage", 0))
    c = cpc(pct)
    bar = mkbar(pct, C_YEL)
    text = f"{C_YEL}{B}5HL{R} {bar} {c}{pct} %{R}"
    return text, 4 + BAR_W + 2 + 1 + len(str(pct)) + 2


def seg_7dl(data):
    rl = data.get("rate_limits")
    if not rl:
        return None
    sd = rl.get("seven_day")
    if not sd:
        return None
    pct = round(sd.get("used_percentage", 0))
    c = cpc(pct)
    text = f"{C_GRN}{B}7DL{R} {c}{pct} %{R}"
    return text, 4 + len(str(pct)) + 2


def seg_cost(data):
    usd = data.get("cost", {}).get("total_cost_usd", 0) or 0
    if usd <= 0:
        return None
    text = f_cost(usd)
    return f"{C_CYN}{B}CST{R} {C_CYN}{text}{R}", 4 + len(text)


def seg_lns(data):
    cost = data.get("cost", {})
    added = cost.get("total_lines_added", 0) or 0
    removed = cost.get("total_lines_removed", 0) or 0
    if added == 0 and removed == 0:
        return None
    text = f"{C_DIM}LNS{R} {C_GRN}+{added:,}{R} {C_RED}-{removed:,}{R}"
    vlen = 4 + 1 + len(f"{added:,}") + 1 + 1 + len(f"{removed:,}")
    return text, vlen


def seg_dur(data):
    ms = data.get("cost", {}).get("total_duration_ms", 0) or 0
    if ms <= 0:
        return None
    d = f_dur(ms)
    return f"{C_GRN}DUR{R} {C_GRN}{d}{R}", 4 + len(d)


# ---------------------------------------------------------------------------
# Layout assembly
# ---------------------------------------------------------------------------
def build_line(data, cols):
    segments = []
    used = 0
    sep_vlen = 3  # " ─ "

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
    try_add(seg_ctx(data))
    try_add(seg_5hl(data))
    try_add(seg_7dl(data))
    try_add(seg_cost(data))
    try_add(seg_lns(data))
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
HISTORY_MAX_LINES = 2000
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
        entry = json.dumps({"t": time.time(), **data})
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
        if len(lines) > HISTORY_MAX_LINES:
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
