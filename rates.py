#!/usr/bin/env python3
"""Shared BRN/CTR rate calculation for monitor.py and statusline.py."""

MIN_EPOCH = 1_577_836_800  # 2020-01-01 — reject implausible timestamps


def _num(v, default=0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def calc_rates(hist):
    """Return (brn $/min, ctr %/min) from history, or (None, None) if insufficient data."""
    if len(hist) < 2:
        return None, None
    try:
        t0 = float(hist[0].get("t", 0))
        t1 = float(hist[-1].get("t", 0))
    except (TypeError, ValueError):
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
