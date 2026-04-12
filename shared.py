#!/usr/bin/env python3
"""Shared helpers and rate calculation for monitor.py and statusline.py."""

import re

MIN_EPOCH = 1_577_836_800  # 2020-01-01 — reject implausible timestamps


def _num(v, default=0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _sanitize(s):
    """Strip control characters to prevent terminal escape injection."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(s))


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
