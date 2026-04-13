#!/usr/bin/env python3
"""Shared helpers and rate calculation for monitor.py and statusline.py."""

import re

MIN_EPOCH = 1_577_836_800  # 2020-01-01 — reject implausible timestamps

# Shared constants — single source of truth for statusline.py + monitor.py
_SID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
_ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")
MAX_FILE_SIZE = 1_048_576  # 1 MB
DATA_DIR_NAME = "claude-aio-monitor"

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


def _num(v, default=0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


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
