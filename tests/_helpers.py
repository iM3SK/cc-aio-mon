#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared test helpers for cc-aio-mon — stdlib only, no pytest required.

Houses functions duplicated across multiple test modules so each helper
has a single canonical definition. Imported by `tests/test_*.py` via the
sys.path shim those modules install (parent of `tests/` on sys.path).
"""

import json
import pathlib
import sys

# Ensure source modules in repo root are importable even when this helper
# module is loaded before any test module installs its own sys.path shim.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared import _ANSI_RE


# ---- ANSI helpers ---------------------------------------------------------
# Both helpers delegate to shared._ANSI_RE (single canonical pattern, T-P2-2).

def _vlen(text):
    """Strip ANSI escapes and return visible length."""
    return len(_ANSI_RE.sub("", text))


def _strip_ansi(lines):
    """Strip ANSI escapes from each string in *lines*; returns a list."""
    return [_ANSI_RE.sub("", ln) for ln in lines]


# ---- Statusline data ------------------------------------------------------

def _full_data(**overrides):
    """Minimal realistic data dict for statusline segments."""
    d = {
        "model": {"display_name": "Opus 4"},
        "context_window": {
            "used_percentage": 42,
            "context_window_size": 200000,
            "current_usage": {
                "cache_read_input_tokens": 8000,
                "cache_creation_input_tokens": 2000,
            },
        },
        "cost": {
            "total_cost_usd": 1.23,
            "total_duration_ms": 120000,
            "total_api_duration_ms": 90000,
            "total_lines_added": 150,
            "total_lines_removed": 30,
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 25},
            "seven_day": {"used_percentage": 10},
        },
    }
    d.update(overrides)
    return d


# ---- Transcript fixtures --------------------------------------------------

def _write_session(tmpdir, project, sid, lines, subagent=False):
    """Write a transcript JSONL under tmpdir/project[/sid/subagents]/<file>.

    Mirrors the on-disk layout scanned by monitor.scan_transcript_stats.
    `tmpdir` may be str or Path; lines are joined with newlines + trailing NL.
    """
    if subagent:
        d = pathlib.Path(tmpdir) / project / sid / "subagents"
    else:
        d = pathlib.Path(tmpdir) / project
    d.mkdir(parents=True, exist_ok=True)
    fn = f"agent-{sid}.jsonl" if subagent else f"{sid}.jsonl"
    (d / fn).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_transcript(parent, sid, lines):
    """Write *lines* as a transcript JSONL named <sid>.jsonl under *parent*.

    Returns the resulting pathlib.Path so callers can pass it to the
    function under test.
    """
    jl = pathlib.Path(parent) / f"{sid}.jsonl"
    jl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jl


def _make_assistant_record(model, **usage):
    """Serialize a minimal `assistant` transcript line as JSON.

    Accepts arbitrary `usage` kwargs that are merged into the message's
    `usage` dict; callers pass raw transcript field names directly (e.g.
    `input_tokens=`, `cache_read_input_tokens=`, `server_tool_use=`,
    `cache_creation=`).
    """
    return json.dumps({
        "type": "assistant",
        "message": {"model": model, "usage": dict(usage)},
    })
