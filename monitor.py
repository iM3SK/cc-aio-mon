#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Claude AIO Monitor — fullscreen TUI dashboard for Claude Code.

Terminal dashboard (monitor.py + shared.py). Stdlib only.
Reads shared state from statusline.py via temp files.

Usage:
    python monitor.py                     # auto-detect session
    python monitor.py --session ID        # specific session
    python monitor.py --list              # list active sessions
    python monitor.py --refresh 1000      # custom refresh interval (ms)

Section map (ordered; jump via your editor outline or grep the `# ----` rules).
This file is intentionally a single large module — see ADR-002 in
PROJECTS/cc-aio-mon/ROZHODNUTIA.md for why the "5 runtime files" constraint is
kept and the LOC tripwire (test_debt016) is set to a higher discussion limit
rather than splitting an event-loop module across files:

    1.  Transcript usage scanner   — scan_transcript_stats + aggregation
    2.  Platform                   — keyboard input (poll_key, esc parser, term)
    3.  ANSI / characters / format — colours, char_width, formatting helpers
    4.  Bars & warnings            — mkbar, collect_warnings
    5.  Cost aggregation & RLS     — cross-session cost, release check worker
    6.  Layout & data loading      — sep, list_sessions, load_state/_history
    7.  Spinners                   — session / RLS spinners
    8.  Render — main dashboard    — render_frame (+ _window_buf scroll engine)
    9.  Legend overlay             — render_legend
    10. Cost breakdown modal       — render_cost_breakdown + pricing
    11. Anthropic Pulse modal      — render_pulse_modal
    12. Menu modal                 — render_menu
    13. Update modal               — render_update_modal + apply worker
    14. Token stats modal          — render_stats + lifetime/daily blocks
    15. Agents modal               — render_agents + scan_subagents fan-out
    16. Session picker             — render_picker (+ _picker_order SSoT)
    17. Screen flush               — flush (synchronized-output frame writer)
    18. Main                       — event loop, key handling, lifecycle
"""

import argparse
import atexit
import bisect
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime, timedelta

from shared import (calc_rates, _num, _sanitize, safe_read, f_tok, f_cost, f_dur, f_cd,
                    char_width, is_safe_dir, ensure_data_dir, ensure_utf8_stdout, run_git,
                    load_history as _shared_load_history,
                    _SID_RE, _ANSI_RE, MAX_FILE_SIZE, HISTORY_AGGREGATE_MAX, TRANSCRIPT_MAX_BYTES,
                    SECONDS_1H, SECONDS_5H, SECONDS_1D, SECONDS_7D,
                    HISTORY_RATE_SAMPLES,
                    WARN_PCT, CRIT_PCT,
                    DATA_DIR, VERSION_RE, VERSION, SCHEMA_VERSION,
                    PY_FILES,  # noqa: F401 — pinned by TestPyFilesSingleSourceOfTruth (SSoT regression guard)
                    RESERVED_SIDS, strip_context_suffix, compact_context_suffix,
                    badge_context_suffix,
                    extract_changelog_entry,
                    check_syntax_after_pull, parse_ahead_behind, verify_origin_remote,
                    rotate_crash_log, acquire_singleton_lock,
                    E, R, B, C_RED, C_GRN, C_YEL, C_ORN, C_CYN, C_WHT, C_DIM)
import pulse

# ---------------------------------------------------------------------------
# Transcript usage scanner — reads ~/.claude/projects/**/*.jsonl
# ---------------------------------------------------------------------------
_CLAUDE_DIR = pathlib.Path.home() / ".claude" / "projects"
MAX_TRANSCRIPT_FILES = 1000  # hard cap on scan to prevent DoS via oversized projects dir
_usage_cache = {}


def _parse_ts(ts_str):
    """Parse ISO timestamp to epoch, 3.8 compatible. Returns 0 on failure."""
    if not ts_str:
        return 0
    try:
        # Timezone-aware parse. 3.8's fromisoformat() accepts ±HH:MM offsets
        # but not "Z", so map Z → +00:00. Stripping the offset and parsing
        # naive (pre-fix behaviour) interpreted the wall-clock as *local*
        # time, shifting cutoff filters and daily aggregation by the local
        # UTC offset.
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        pass
    try:
        # Fallback for shapes fromisoformat can't parse with an offset
        # attached (e.g. non-colon offsets on 3.8): strip the suffix and
        # parse naive — approximate, but better than dropping the record.
        clean = ts_str.replace("Z", "")
        # Remove +/-offset after the time portion (T required)
        t_pos = clean.find("T")
        if t_pos >= 0:
            tail = clean[t_pos + 1:]
            for sep in ("+", "-"):
                idx = tail.rfind(sep)
                # Offset separator is after HH:MM:SS (idx >= 8)
                if idx >= 8:
                    clean = clean[:t_pos + 1 + idx]
                    break
        return datetime.fromisoformat(clean).timestamp()
    except (ValueError, TypeError):
        return 0


def _iter_safe_transcripts(claude_root, cutoff):
    """Yield `(jl, st, sid, is_subagent)` for each transcript that passes
    containment, symlink, size, and cutoff filtering. After
    MAX_TRANSCRIPT_FILES candidates have been seen, yields the sentinel
    value `None` to signal truncation, then stops.

    SIZE-003 split: this is the security-sensitive path-resolution half
    of `scan_transcript_stats`. Keeping it separate from the aggregation
    arithmetic in `_aggregate_transcript` lets each half be reasoned
    about (and tested) in isolation.
    """
    file_count = 0
    for jl in _CLAUDE_DIR.glob("**/*.jsonl"):
        file_count += 1
        if file_count > MAX_TRANSCRIPT_FILES:
            yield None
            return
        is_subagent = "subagents" in str(jl)
        try:
            st = jl.lstat()
            # Reject symlinked transcript files: resolve must land inside
            # claude_root, and the lstat must be a regular file.
            if stat.S_ISLNK(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            try:
                resolved = jl.resolve(strict=True)
                resolved.relative_to(claude_root)
            except (OSError, ValueError, RuntimeError):
                continue
            if st.st_size > TRANSCRIPT_MAX_BYTES:
                continue
            if cutoff and st.st_mtime < cutoff:
                continue
        except OSError:
            continue
        yield (jl, st, jl.stem, is_subagent)


def _aggregate_transcript(jl, st, sid, is_subagent, cutoff,
                          models, active_days, daily_tokens, session_times):
    """Parse one transcript and mutate the four aggregate containers
    in place. Returns silently on OS / UnicodeDecodeError — caller
    treats partial data as best-effort.

    S-P2-2 (CWE-367): TOCTOU guard via `os.fstat(fh.fileno())` vs the
    pre-open `lstat` (`st`); if the inode/device pair diverged between
    the iterator's path resolution and this open, the file was swapped
    and we skip it rather than aggregate from an unverified inode.
    """
    try:
        with open(jl, encoding="utf-8") as f:
            try:
                fst = os.fstat(f.fileno())
            except OSError:
                return
            if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
                return
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = obj.get("timestamp", "")
                ts = _parse_ts(ts_str)

                # When a period cutoff is active, skip both out-of-window AND
                # unplaceable (ts<=0) records, so the model totals match the
                # daily_tokens / active_days aggregation below (which already
                # requires ts>0). period="all" (cutoff=0) keeps every record.
                if cutoff and (ts <= 0 or ts < cutoff):
                    continue

                # Track session timestamps for duration calc
                if ts > 0 and not is_subagent:
                    if sid not in session_times:
                        session_times[sid] = [ts, ts]
                    else:
                        if ts < session_times[sid][0]:
                            session_times[sid][0] = ts
                        if ts > session_times[sid][1]:
                            session_times[sid][1] = ts

                if obj.get("type") != "assistant":
                    continue
                # `"usage" in msg` on a *string* message is a substring test —
                # msg.get() would then raise AttributeError, which the outer
                # except (OSError, UnicodeDecodeError) does not catch.
                msg = obj.get("message")
                if not isinstance(msg, dict) or "usage" not in msg:
                    continue

                model = msg.get("model") or "unknown"
                if not isinstance(model, str) or model.startswith("<"):
                    continue  # skip synthetic/internal entries
                u = msg["usage"]
                if not isinstance(u, dict):
                    continue
                inp = int(_num(u.get("input_tokens", 0)))
                out = int(_num(u.get("output_tokens", 0)))
                cr = int(_num(u.get("cache_read_input_tokens", 0)))
                cw = int(_num(u.get("cache_creation_input_tokens", 0)))
                if model not in models:
                    models[model] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
                models[model]["input"] += inp
                models[model]["output"] += out
                models[model]["cache_read"] += cr
                models[model]["cache_write"] += cw
                models[model]["calls"] += 1

                if ts > 0:
                    # A corrupt/huge ts raises OverflowError (not OSError, so
                    # the outer guard wouldn't catch it) or OSError on Windows.
                    # Skip just this record's day attribution rather than abort
                    # the whole transcript's aggregation.
                    try:
                        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    except (OverflowError, OSError, ValueError):
                        continue
                    active_days.add(day)
                    daily_tokens[day] = daily_tokens.get(day, 0) + inp + out + cr + cw
    except (OSError, UnicodeDecodeError):
        return


def scan_transcript_stats(period="all", ttl=30.0):
    """Scan CC session transcripts, return (models, overview) tuple.

    models: {model_id: {"input": int, "output": int, "cache_read": int, "cache_write": int, "calls": int}}
    overview: {"sessions": int, "active_days": set, "longest_dur_ms": float,
               "first_date": str, "daily_tokens": {date_str: int}}

    SIZE-003: the original 148-LOC monolith was split into three pieces:
    this orchestrator, `_iter_safe_transcripts` (containment + symlink +
    size + cutoff filtering, yields `None` as the truncation sentinel),
    and `_aggregate_transcript` (per-file parse + in-place aggregate
    mutation). The split keeps the security-sensitive path-resolution
    logic separately testable from the data-aggregation arithmetic.
    """
    mono = time.monotonic()
    cached = _usage_cache.get(period)
    if cached and mono - cached["t"] < ttl:
        return cached["models"], cached["overview"]

    cutoff = 0
    wall = time.time()
    if period == "7d":
        cutoff = wall - SECONDS_7D
    elif period == "30d":
        cutoff = wall - 30 * SECONDS_1D

    models = {}
    active_days = set()
    daily_tokens = {}
    session_times = {}  # sid -> (first_ts, last_ts)
    session_count = 0

    if not is_safe_dir(_CLAUDE_DIR):
        empty_ov = {"sessions": 0, "active_days": set(), "longest_dur_ms": 0,
                     "first_date": None, "daily_tokens": {}, "truncated": False}
        return models, empty_ov

    # Resolve once to establish a canonical root. Transcripts that resolve
    # outside this root (symlinks escaping via reparse points) are rejected
    # per-file below — consistent with _safe_transcript_path hardening.
    try:
        claude_root = _CLAUDE_DIR.resolve(strict=True)
    except (OSError, RuntimeError):
        empty_ov = {"sessions": 0, "active_days": set(), "longest_dur_ms": 0,
                     "first_date": None, "daily_tokens": {}, "truncated": False}
        return models, empty_ov

    _truncated = False
    for item in _iter_safe_transcripts(claude_root, cutoff):
        if item is None:
            _truncated = True
            break
        jl, st, sid, is_subagent = item
        if not is_subagent:
            session_count += 1
        _aggregate_transcript(
            jl, st, sid, is_subagent, cutoff,
            models, active_days, daily_tokens, session_times,
        )

    # Compute longest session duration
    longest_dur_ms = 0
    for s_ts in session_times.values():
        dur = (s_ts[1] - s_ts[0]) * 1000
        if dur > longest_dur_ms:
            longest_dur_ms = dur

    # First date
    first_date = min(active_days) if active_days else None

    overview = {
        "sessions": session_count,
        "active_days": active_days,
        "longest_dur_ms": longest_dur_ms,
        "first_date": first_date,
        "daily_tokens": daily_tokens,
        "truncated": _truncated,
    }

    _usage_cache[period] = {"t": mono, "models": models, "overview": overview}
    return models, overview


def _calc_streaks(active_days):
    """Calculate current and longest streak from a set of date strings."""
    if not active_days:
        return 0, 0
    days = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in active_days)
    today = datetime.now().date()

    longest = 1
    current_run = 1
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days == 1:
            current_run += 1
            longest = max(longest, current_run)
        else:
            current_run = 1

    # Current streak: count backwards from today
    current = 0
    check = today
    for d in reversed(days):
        if d == check:
            current += 1
            check -= timedelta(days=1)
        elif d < check:
            break
    return current, longest

# ---------------------------------------------------------------------------
# Platform — keyboard input abstraction
# ---------------------------------------------------------------------------
IS_WIN = sys.platform == "win32"
_term_state = None
_orig_console_output_cp = None  # Windows: saved by _setup_term, restored by _restore_term

# Navigation keys returned by poll_key as symbolic tokens so the key handler
# can scroll modals on arrows / Page keys instead of treating each escape byte
# as a separate (modal-closing) key press.
_NAV_SEQ = {  # Unix: bytes after the ESC introducer
    "[A": "<UP>", "[B": "<DOWN>", "[5~": "<PGUP>", "[6~": "<PGDN>",
    "[H": "<HOME>", "[F": "<END>", "OH": "<HOME>", "OF": "<END>",
    # SS3 / application-cursor variants (DECCKM, and what ?1007h wheel emits on
    # many terminals): ESC O A/B instead of ESC [ A/B.
    "OA": "<UP>", "OB": "<DOWN>",
}
_WIN_NAV = {  # Windows: scancode after the \x00 / \xe0 prefix
    b"H": "<UP>", b"P": "<DOWN>", b"I": "<PGUP>", b"Q": "<PGDN>",
    b"G": "<HOME>", b"O": "<END>",
}
_SCROLL_KEYS = {"<UP>", "<DOWN>", "<PGUP>", "<PGDN>", "<HOME>", "<END>", "j", "k"}
# A non-scroll key read during the scroll-burst drain is stashed here so the
# next poll_key() returns it instead of dropping it.
_key_pushback = [None]

if IS_WIN:
    import ctypes
    import msvcrt

    def poll_key():
        if _key_pushback[0] is not None:
            k = _key_pushback[0]
            _key_pushback[0] = None
            return k
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\x00", b"\xe0"):
                return _WIN_NAV.get(msvcrt.getch())  # nav key or None
            return ch.decode("utf-8", errors="ignore")
        return None

    def _set_console_utf8():
        """NEW-003: switch Windows console output CP to 65001 (UTF-8) without
        touching VT mode or alt-screen state. Used by `--list` and any other
        non-interactive mode that emits diacritics but does not call the
        full `_setup_term`. Safe to call standalone — Python's
        `sys.stdout.encoding=utf-8` (set by `ensure_utf8_stdout`) is
        necessary but not sufficient on Windows: the byte stream is
        UTF-8 but the console reinterprets it through the locale CP
        (CP1250 on SK, CP1252 on US) and renders mojibake unless we
        switch CP here. Original CP is saved into the same global so
        `_restore_term` (registered by interactive setup) puts it back."""
        global _orig_console_output_cp
        try:
            kernel32 = ctypes.windll.kernel32
        except Exception:
            return
        # Save the *real* original CP only once. _set_console_utf8 runs
        # unconditionally before the --list branch, and interactive setup later
        # calls _setup_term which saves again — by then the CP is already 65001,
        # so an unguarded re-save would persist 65001 as the "original" and
        # _restore_term would never put the user's locale CP back.
        if _orig_console_output_cp is None:
            try:
                _orig_console_output_cp = int(kernel32.GetConsoleOutputCP())
            except Exception:
                _orig_console_output_cp = None
        try:
            kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass

    _WIN10_ANSI_REQUIRED_MSG = (
        "Error: this console does not support ANSI / VT escape sequences.\n"
        "Windows 10 build 10586 (Threshold 2, Nov 2015) or later is required —\n"
        "older Windows (7/8/8.1, early Win10) and the legacy `conhost.exe` host\n"
        "without ConPTY render the TUI as raw escape text and it is unusable.\n"
        "Workarounds: run monitor.py inside Windows Terminal, ConEmu, Cmder,\n"
        "Git Bash (mintty), or upgrade to a current Windows build."
    )

    def _setup_term():
        """Enable VT/ANSI processing AND switch console output to UTF-8.

        Without code-page 65001 (CP_UTF8), Slovak/Czech/etc. characters in
        session names, AI titles, model labels — which `statusline.py` writes
        as UTF-8 bytes — render as mojibake on Windows locales whose default
        console code page isn't UTF-8 (e.g. CP1250 on SK Windows: `ť` displays
        as `Ĺĺ`). Saves the original CP for _restore_term.

        If the console handle is invalid or the kernel rejects
        SetConsoleMode (pre-Win10 conhost / unsupported terminal), exit
        with a clear diagnostic instead of falling through to a broken
        TUI rendering as raw escape sequences (A-P2-4).
        """
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001
        CP_UTF8 = 65001
        global _orig_console_output_cp
        try:
            kernel32 = ctypes.windll.kernel32
        except Exception:
            sys.exit(_WIN10_ANSI_REQUIRED_MSG)
        # Save current output CP first so _restore_term can put it back.
        # Guard against overwriting a CP already saved by an earlier
        # _set_console_utf8 (which by now switched the console to 65001).
        if _orig_console_output_cp is None:
            try:
                _orig_console_output_cp = int(kernel32.GetConsoleOutputCP())
            except Exception:
                _orig_console_output_cp = None
        # Switch console output to UTF-8 — `ensure_utf8_stdout()` makes
        # Python write UTF-8 bytes, this makes the Windows console
        # interpret them as UTF-8 instead of the locale default.
        try:
            kernel32.SetConsoleOutputCP(CP_UTF8)
        except Exception:
            pass
        try:
            # HANDLE is pointer-sized — the default ctypes restype (c_int)
            # truncates/sign-extends it on 64-bit Windows and the console-mode
            # calls then operate on a corrupted handle (same fix as
            # statusline's CONOUT$ branch and update.py's VT enable).
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            kernel32.GetConsoleMode.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        except Exception:
            sys.exit(_WIN10_ANSI_REQUIRED_MSG)
        if handle is None or handle == 0 or handle == ctypes.c_void_p(-1).value:
            sys.exit(_WIN10_ANSI_REQUIRED_MSG)
        mode = ctypes.c_ulong(0)
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            sys.exit(_WIN10_ANSI_REQUIRED_MSG)
        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
        if not kernel32.SetConsoleMode(handle, new_mode):
            sys.exit(_WIN10_ANSI_REQUIRED_MSG)

    def _restore_term():
        """Restore the original console output code page on exit so we don't
        leave the user's console in UTF-8 mode after monitor quits."""
        global _orig_console_output_cp
        if _orig_console_output_cp is None:
            return
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(_orig_console_output_cp)
        except Exception:
            pass
        _orig_console_output_cp = None

else:
    import select
    import termios
    import tty

    # In-progress escape sequence carried across poll_key calls. A sequence
    # split over multiple reads (slow SSH/tmux, bursty wheel) is buffered here
    # so its bytes are NEVER surfaced as standalone keys — which would close the
    # open modal and snap back to the dashboard ("main screen" symptom).
    _esc_buf = [""]

    def _resolve_esc():
        """Interpret the buffered escape sequence. Returns a nav token when it
        is a complete known sequence (and clears the buffer), None + clears for
        a complete unknown / mouse / Alt sequence, or None + keeps buffering
        while it is still incomplete."""
        b = _esc_buf[0]
        if not b:
            return None
        if len(b) > 32:  # runaway — drop
            _esc_buf[0] = ""
            return None
        if b == "\x1b":
            return None  # bare ESC so far — keep buffering
        intro = b[1]
        if len(b) == 2:
            if intro in ("[", "O"):
                return None  # CSI / SS3 introducer — keep buffering
            # ESC + other: a bare ESC followed by a real key (or Alt-<key>).
            # Surface that key instead of swallowing it.
            _esc_buf[0] = ""
            return intro
        last = b[-1]
        if intro == "O":  # SS3: ESC O <char> — complete at 3 bytes
            _esc_buf[0] = ""
            return _NAV_SEQ.get(b[1:])
        if intro == "[":  # CSI: ends on a final byte 0x40-0x7e
            second = b[2:3]
            if second == "M":
                # X10 mouse report: ESC [ M + exactly 3 raw coordinate bytes
                # (each >= 0x20, so they never contain ESC). The 'M' is itself a
                # CSI final byte, so without this we'd resolve at ESC[M and leak
                # the 3 coordinate bytes as keystrokes. Buffer until all arrive,
                # then drop the whole report.
                if len(b) < 6:
                    return None
                _esc_buf[0] = ""
                return None
            if "\x40" <= last <= "\x7e":
                seq = b[1:]
                _esc_buf[0] = ""
                if second == "<":
                    return None  # SGR mouse report (ESC [ < … M/m) — ignore cleanly
                return _NAV_SEQ.get(seq)
            return None  # still accumulating CSI parameters
        _esc_buf[0] = ""
        return None

    def poll_key():
        if _key_pushback[0] is not None:
            k = _key_pushback[0]
            _key_pushback[0] = None
            return k
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return None
        ch = sys.stdin.read(1)
        if _esc_buf[0]:  # mid-sequence from a previous (split) read
            _esc_buf[0] += ch
            return _resolve_esc()
        if ch == "\x1b":
            _esc_buf[0] = "\x1b"
            # Fast path: pull immediately-available bytes so atomic sequences
            # resolve in this one call; anything not yet arrived stays buffered.
            for _ in range(32):
                r2, _, _ = select.select([sys.stdin], [], [], 0)
                if not r2:
                    break
                _esc_buf[0] += sys.stdin.read(1)
                tok = _resolve_esc()
                if not _esc_buf[0]:  # resolved (token or dropped)
                    return tok
            return _resolve_esc()  # leave any partial for the next poll
        return ch

    def _set_console_utf8():
        """Unix counterpart — no-op. Unix terminals get encoding from
        LANG/LC_ALL locale and the user's terminal emulator; there is no
        Windows-style console CP API to flip. `ensure_utf8_stdout`
        already handles the Python side."""
        return

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
# ANSI — E, R, B, C_RED..C_DIM imported from shared.py
# ---------------------------------------------------------------------------
HIDE_CUR = E + "?25l"
SHOW_CUR = E + "?25h"
ALT_ON = E + "?1049h"
ALT_OFF = E + "?1049l"
# Alternate-scroll mode: in the alt-screen buffer, makes the terminal translate
# mouse-wheel scroll into arrow-key sequences (ESC[A / ESC[B) instead of
# swallowing it — without this the wheel does nothing in a fullscreen TUI.
ALT_SCROLL_ON = E + "?1007h"
ALT_SCROLL_OFF = E + "?1007l"
CLR = E + "2J"
HOME = E + "H"
EL = E + "K"
SYNC_ON = E + "?2026h"
SYNC_OFF = E + "?2026l"

C_FG = E + "38;2;180;186;200m"  # monitor-only: default foreground
BG_BAR = E + "48;2;46;52;64m"  # Nord polar night — header/bar background

# VERSION is imported from shared.py — single source of truth (shared.VERSION)
STALE_THRESHOLD = 1800  # 30 min — Claude Code emits no events during idle
DEAD_SESSION_TTL = 172800  # 48h — auto-purge dead session files from temp dir

def _env_float(name, default):
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default


WARN_BRN = _env_float("CLAUDE_WARN_BRN", 3.0)
# WARN_PCT/CRIT_PCT imported from shared.py (SSoT; previously parsed per module)

# Reset-countdown color flip — 50% of window remaining (NOT the warn threshold)
RESET_HALFWAY_PCT = 50.0


def truncate(s, maxw):
    """Truncate string to maxw visible columns, preserving ANSI codes. CJK-aware."""
    # Fast path: a plain ASCII string with no ANSI escapes and no wide/zero-
    # width chars has visible width == len(s), so when it already fits there is
    # nothing to do. Skips the per-char scan for the common short-line case in
    # the 20Hz render hot path. isprintable() is False for ESC and every other
    # C0 control, so any ANSI-coloured or control-bearing line falls through to
    # the accurate scan below.
    if len(s) <= maxw and s.isascii() and s.isprintable():
        return s
    vis = 0
    i = 0
    truncated = False
    while i < len(s) and vis < maxw:
        m = _ANSI_RE.match(s, i)
        if m:
            i = m.end()
        else:
            w = char_width(s[i])
            if vis + w > maxw:
                break
            vis += w
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


# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------
BF = "\u2588"  # █
SH = "\u2591"  # ░

BAR_W = 25     # fixed bar width for ALL metrics


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Progress bar — enclosed [████░░░░], fixed width, color by threshold
# ---------------------------------------------------------------------------
def mkbar(pct, color=None, show_pct=True):
    """Returns colored [████░░░░░]  XX.X%"""
    pct = max(0.0, min(100.0, pct))
    if color is None:
        if pct >= CRIT_PCT:
            color = C_RED
        elif pct >= WARN_PCT:
            color = C_YEL
        else:
            color = C_GRN
    filled = round(pct * BAR_W / 100)
    empty = BAR_W - filled
    bar = (
        f"{C_DIM}[{R}"
        f"{color}{BF * filled}{R}"
        f"{C_DIM}{SH * empty}{R}"
        f"{C_DIM}]{R}"
    )
    if show_pct:
        bar += f" {color}{pct:.1f}%{R}"
    return bar


def _limit_color(pct):
    """Dynamic color for rate limit metrics — yellow base, red >= CRIT_PCT."""
    if pct >= CRIT_PCT:
        return C_RED
    return C_YEL


def _reset_color(resets_epoch, window_secs):
    """Color for reset countdown — green=close to reset, red=far from reset."""
    if resets_epoch <= 0:
        return C_DIM
    remaining = resets_epoch - time.time()
    if remaining <= 0:
        return C_GRN  # just reset
    pct_remaining = remaining / window_secs * 100
    if pct_remaining > RESET_HALFWAY_PCT:
        return C_RED
    if pct_remaining > 20:
        return C_YEL
    return C_GRN


# ---------------------------------------------------------------------------
# Fixed-range bar for rate/cost metrics
# ---------------------------------------------------------------------------
BRN_MAX = _env_float("CC_MON_BRN_MAX", 10.0)   # $/min ceiling
CTR_MAX = _env_float("CC_MON_CTR_MAX", 10.0)   # %/min ceiling
CST_MAX = _env_float("CC_MON_CST_MAX", 1000.0) # $ ceiling


# ---------------------------------------------------------------------------
# Smart warnings
# ---------------------------------------------------------------------------
def collect_warnings(data, cpm, xpm):
    """Returns list of warning label strings for active conditions."""
    warnings = []
    # CTF — context filling fast
    if xpm and xpm > 0:
        ctx_pct = _num((data.get("context_window") or {}).get("used_percentage"))
        if ctx_pct < 100:
            eta_mins = (100 - ctx_pct) / xpm
            if eta_mins < 30:
                warnings.append(f"CTF <{max(1, int(eta_mins))}m")
    # BRN
    if cpm and cpm > WARN_BRN:
        warnings.append(f"BRN {cpm:.4f}$/min")
    return warnings


# ---------------------------------------------------------------------------
# Cross-session cost aggregation
# ---------------------------------------------------------------------------
# Main-thread only — read/written exclusively from render loop. No lock needed.
# (Unlike _rls_cache which IS locked because a daemon thread updates it.)
_cost_cache = {"t": 0.0, "today": 0.0, "week": 0.0}


# ---------------------------------------------------------------------------
# RLS — background release check
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).parent.resolve()
_RLS_TTL = SECONDS_1H  # check once per hour
_RLS_BLINK_INTERVAL = 0.5
_rls_cache = {"t": -_RLS_TTL, "status": None, "remote_ver": None}
_rls_lock = threading.Lock()       # worker-spawn coordination (one check at a time)
_rls_data_lock = threading.Lock()  # cache field coherence across read/write threads
_rls_blink_last = 0.0
_rls_blink_on = True


def _rls_snapshot():
    """Thread-safe atomic snapshot of _rls_cache. Returns a shallow dict copy."""
    with _rls_data_lock:
        return dict(_rls_cache)


def _rls_write(status, remote_ver=None):
    """Thread-safe write of all three _rls_cache fields in one critical section."""
    with _rls_data_lock:
        _rls_cache["t"] = time.monotonic()
        _rls_cache["status"] = status
        _rls_cache["remote_ver"] = remote_ver
def _parse_version(ver_str):
    """Parse version string to comparable tuple, ignoring non-numeric suffixes."""
    parts = []
    for p in ver_str.split("."):
        m = re.match(r"(\d+)", p)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts)


def _rls_check_worker():
    """Background worker: git fetch + compare VERSION. Writes result atomically.
    Uses shared.run_git for consistent env whitelist (blocks GIT_SSH_COMMAND / LD_PRELOAD).
    Writes via _rls_write() to keep three fields coherent under _rls_data_lock."""
    try:
        r = run_git(["fetch", "origin", "main"], cwd=_REPO_ROOT, timeout=15)
        if r.returncode != 0:
            _rls_write("error")
            return
        r = run_git(["show", "origin/main:shared.py"], cwd=_REPO_ROOT, timeout=15)
        if r.returncode != 0:
            _rls_write("error")
            return
        m = VERSION_RE.search(r.stdout)
        if not m:
            _rls_write("error")
            return
        remote_ver = m.group(1)
        local_t = _parse_version(VERSION)
        remote_t = _parse_version(remote_ver)
        status = "update" if remote_t > local_t else "ok"
        _rls_write(status, remote_ver=remote_ver)
    except FileNotFoundError:
        _rls_write("no_git")
    except subprocess.TimeoutExpired:
        _rls_write("timeout")
    except Exception:
        _rls_write("error")
    finally:
        _rls_lock.release()


def _rls_maybe_check():
    """Trigger background check if TTL expired. Non-blocking.

    Lock ownership: acquired here, released by _rls_check_worker's `finally`
    OR here if the Thread spawn itself fails. Worker must not return
    without releasing. Keep in sync with `_rls_check_worker` finally block.
    """
    if os.environ.get("CC_AIO_MON_NO_UPDATE_CHECK") == "1":
        return
    if time.monotonic() - _rls_snapshot()["t"] < _RLS_TTL:
        return
    if not _rls_lock.acquire(blocking=False):
        return
    try:
        t = threading.Thread(target=_rls_check_worker, daemon=True)
        t.start()
    except Exception:  # noqa: BLE001 — RLS check is best-effort; must not crash main loop
        _rls_lock.release()


def _rls_blink():
    """Toggle blink state for update-available indicator."""
    global _rls_blink_last, _rls_blink_on
    now = time.monotonic()
    if now - _rls_blink_last >= _RLS_BLINK_INTERVAL:
        _rls_blink_on = not _rls_blink_on
        _rls_blink_last = now
    return _rls_blink_on


def _baseline_delta(entries, cutoff_ts):
    """Compute cost delta within a time window via baseline subtraction.

    Walks ``entries`` (sorted ascending by ``t``) and partitions on ``cutoff_ts``:
    baseline = last entry strictly before cutoff (or first entry at/after
    cutoff if none precedes), final = last entry at/after cutoff. Returns
    ``max(0.0, final - baseline)`` so negative deltas (CC bug or reset) clip
    to zero. Returns 0.0 for an empty window.
    """
    baseline = None
    final = 0.0
    first_in_window = None
    for e in entries:
        t = _num(e.get("t"), 0)
        if t <= 0:
            continue  # unplaceable (missing/invalid t) — can't partition it
        cost = _num((e.get("cost") or {}).get("total_cost_usd"))
        if t < cutoff_ts:
            baseline = cost
        else:
            if first_in_window is None:
                first_in_window = cost
            final = cost
    if baseline is None:
        baseline = first_in_window or 0.0
    return max(0.0, final - baseline)


def calc_cross_session_costs():
    """Aggregate cost across all sessions for today and this week."""
    if not DATA_DIR.exists() or not is_safe_dir(DATA_DIR):
        return 0.0, 0.0
    today_start = datetime.combine(datetime.today().date(), datetime.min.time()).timestamp()
    week_start = today_start - 6 * SECONDS_1D
    today_total = 0.0
    week_total = 0.0
    for jl in DATA_DIR.glob("*.jsonl"):
        sid = jl.stem
        if not _SID_RE.match(sid):
            continue
        if sid in RESERVED_SIDS:
            continue
        # Bounded read — stat check + safe_read cap closes TOCTOU window
        try:
            st = jl.stat()
            if st.st_size > HISTORY_AGGREGATE_MAX:
                continue
        except OSError:
            continue
        raw_bytes = safe_read(jl, HISTORY_AGGREGATE_MAX)
        if raw_bytes is None:
            continue
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
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
        today_total += _baseline_delta(entries, today_start)
        week_total += _baseline_delta(entries, week_start)
    return today_total, week_total


_cost_scan_thread = None
_cost_scan_lock = threading.Lock()


def _cost_scan_worker():
    """Off-thread cross-session cost aggregation. Writes _cost_cache."""
    today, week = calc_cross_session_costs()
    _cost_cache.update({"t": time.monotonic(), "today": today, "week": week})


def _cost_refresh_async(ttl=30.0):
    """Kick a background calc_cross_session_costs when the cache is stale and
    none is in flight. The glob + per-line JSONL re-parse of every session's
    history (up to HISTORY_AGGREGATE_MAX each) used to run inline in render_frame
    every TTL — a ~tens-of-ms render-thread stall that was most visible right
    after waking from stale (largest transcripts, active interaction). That was
    the deferred P1-4, re-opened on user feedback. Mirrors _stats_refresh_async
    / _subagents_refresh_async; render reads the last _cost_cache and never
    blocks."""
    global _cost_scan_thread
    if time.monotonic() - _cost_cache["t"] < ttl:
        return  # fresh enough — no scan needed
    with _cost_scan_lock:
        if _cost_scan_thread is not None and _cost_scan_thread.is_alive():
            return
        t = threading.Thread(target=_cost_scan_worker, name="cost-scan", daemon=True)
        _cost_scan_thread = t
        t.start()


def cached_cross_session_costs(ttl=30.0):
    """Non-blocking: kick an off-thread refresh when the TTL has expired, then
    return the last cached (today, week). The aggregation runs in the cost-scan
    daemon so the render thread never re-parses transcripts inline."""
    _cost_refresh_async(ttl)
    return _cost_cache["today"], _cost_cache["week"]


# Session picker cache — main-thread only, mirrors _cost_cache contract.
# Picker mode renders 20x/sec; list_sessions() does full DATA_DIR scan + per-session
# JSON parse + AI-title extraction. 1 s TTL drops repeated scans to ~1 Hz while
# remaining visually fresh (sessions don't appear or disappear faster than ~1 Hz
# in practice). Audit P1-3 (24.05.2026).
_sessions_cache = {"t": 0.0, "sessions": None}


def cached_list_sessions(ttl=1.0):
    """Cached version of list_sessions for the picker hot loop.

    Returns the cached list (same reference) if within ttl; otherwise rescans.
    Use this from the picker render loop; direct list_sessions() callers
    (one-shot CLI --list, etc.) can stay direct to avoid stale-cache reads.
    """
    now = time.monotonic()
    cached = _sessions_cache["sessions"]
    if cached is not None and now - _sessions_cache["t"] < ttl:
        return cached
    sessions = list_sessions()
    _sessions_cache.update({"t": now, "sessions": sessions})
    return sessions


# ---------------------------------------------------------------------------
# Layout helpers — no borders, just lines
# ---------------------------------------------------------------------------
def sep(w):
    return C_DIM + "-" * w + R


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def list_sessions():
    if not DATA_DIR.exists() or not is_safe_dir(DATA_DIR):
        return []
    now = time.time()
    # Clean up stale .tmp files (orphans from crashed writes)
    for tmp in DATA_DIR.glob("*.tmp"):
        try:
            if now - tmp.stat().st_mtime > 60:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
    # Auto-purge dead sessions older than 48h (.json + .jsonl pair)
    for f in DATA_DIR.glob("*.json"):
        sid = f.stem
        if sid in RESERVED_SIDS or not _SID_RE.match(sid):
            continue
        try:
            if now - f.stat().st_mtime > DEAD_SESSION_TTL:
                f.unlink(missing_ok=True)
                hist = DATA_DIR / f"{sid}.jsonl"
                hist.unlink(missing_ok=True)
        except OSError:
            pass
    sessions = []
    for f in DATA_DIR.glob("*.json"):
        sid = f.stem
        if not _SID_RE.match(sid):
            continue
        if sid in RESERVED_SIDS:
            continue
        try:
            st = f.stat()
            mt = st.st_mtime
            age = now - mt
            raw = safe_read(f, MAX_FILE_SIZE)
            if raw is None:
                continue
            d = json.loads(raw.decode("utf-8"))
            # Skip snapshots without usable model info (test artifacts / incomplete writes)
            display_name = _sanitize((d.get("model") or {}).get("display_name", "")).strip()
            if not display_name:
                # Cleanup: dead artifact older than 1 hour
                if (now - mt) > SECONDS_1H:
                    try:
                        f.unlink()
                        f.with_suffix(".jsonl").unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            sessions.append({
                "id": sid, "mtime": mt, "age": age,
                "stale": age > STALE_THRESHOLD,
                "model": display_name,
                "session_name": _sanitize(d.get("session_name", "")),
                "ai_title": _scan_ai_title(d.get("transcript_path")) or "",
                "cwd": _sanitize(d.get("cwd", "")),
            })
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def load_state(sid):
    # FILE-IPC "Invalid SID Handling": reserved stems (rls/stats/pulse) are
    # internal files, never session snapshots — early-return like
    # list_sessions / load_history already do.
    if sid in RESERVED_SIDS or not _SID_RE.match(str(sid)):
        return None
    if not is_safe_dir(DATA_DIR):
        return None
    raw = safe_read(DATA_DIR / f"{sid}.json", MAX_FILE_SIZE)
    if raw is None:
        return None
    try:
        d = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    # IPC schema gate (M-cross-2): statusline tags every snapshot with
    # _schema_version. A snapshot written by a NEWER build than this one —
    # possible briefly mid self-update before the monitor restarts — may have
    # an incompatible shape, so treat it as unreadable (degrade to None) rather
    # than risk misreading fields. A missing/older tag (pre-versioning snapshots
    # left on disk after a git pull) defaults to 0 and stays readable.
    if (isinstance(d, dict) and isinstance(d.get("_schema_version"), int)
            and d["_schema_version"] > SCHEMA_VERSION):
        return None
    return d


# Thin wrapper — shared.load_history is the single source of truth (v1.10.5+).
# Passes monitor.DATA_DIR explicitly so tests that monkey-patch it still hit the fixture.
def load_history(sid, n=HISTORY_RATE_SAMPLES):
    return _shared_load_history(sid, n, data_dir=DATA_DIR)


# ---------------------------------------------------------------------------
# Spinners
# ---------------------------------------------------------------------------
# Session spinner — braille dots, 10 frames, 80ms
_SPIN_SESSION = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_spin_session_idx = 0
_spin_session_last = 0.0
_SPIN_SESSION_INTERVAL = 0.08

# RLS spinner — pulse dot, 4 frames, 500ms
_SPIN_RLS = ["∙", "○", "●", "○"]
_spin_rls_idx = 0
_spin_rls_last = 0.0
_SPIN_RLS_INTERVAL = 0.5


def spin_session():
    """Return 1-char braille spinner frame for session status."""
    global _spin_session_idx, _spin_session_last
    now = time.monotonic()
    if now - _spin_session_last >= _SPIN_SESSION_INTERVAL:
        _spin_session_idx += 1
        _spin_session_last = now
    return _SPIN_SESSION[_spin_session_idx % len(_SPIN_SESSION)]


def spin_rls():
    """Return 1-char pulse spinner frame for RLS status."""
    global _spin_rls_idx, _spin_rls_last
    now = time.monotonic()
    if now - _spin_rls_last >= _SPIN_RLS_INTERVAL:
        _spin_rls_idx += 1
        _spin_rls_last = now
    return _SPIN_RLS[_spin_rls_idx % len(_SPIN_RLS)]


def _fit_buf_height(buf, rows, *, clip_tail=False):
    """Fit buffer to terminal height.

    Dashboard (clip_tail=False) protects the last 2 lines (footer separator + keys).
    Legend/picker/stats (clip_tail=True) preserves the header and clips from the bottom.
    """
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
        buf[:] = buf[:sub_target]
    while len(buf) < sub_target:
        buf.append("")
    buf.extend(tail)


_modal_scroll = 0  # vertical scroll offset for the active modal (clamped in _window_buf)
_MODAL_HEADER_LINES = 3  # sep + title bar + sep — every modal opens with these


def _scroll_indicator(off, vis, total):
    """One-line scroll hint at the bottom of a windowed modal: a direction arrow
    (more above ↑ / below ↓), the visible range, and how to scroll. No
    proportional bar — the plain hint reads cleaner."""
    max_off = max(0, total - vis)
    up = "↑" if off > 0 else " "
    dn = "↓" if off < max_off else " "
    return (f"{C_DIM}{up}{dn} {off + 1}-{off + vis}/{total}"
            f"   ↑↓/jk · PgUp/Dn · Home/End{R}")


def _window_buf(buf, rows, header_n=_MODAL_HEADER_LINES):
    """Fit a modal buffer to terminal height WITH vertical scrolling.

    Replaces clip-from-bottom on modals: keeps the first ``header_n`` lines
    (sep / title bar / sep) PINNED at the top so the title never scrolls away,
    scrolls only the body at the module-level ``_modal_scroll`` offset (clamped
    here so the key handler can blindly inc/dec it), and appends a proportional
    scroll-bar indicator. Buffers that already fit are returned unchanged.
    Modifies ``buf`` in place and returns it.
    """
    global _modal_scroll
    try:
        rows = int(rows)
    except (TypeError, ValueError):
        rows = 24
    rows = max(1, rows)
    if len(buf) <= rows:
        _modal_scroll = 0
        return buf
    if rows == 1:
        # rows=1: no room for header or indicator — emit exactly one body
        # line at the scroll offset (the general math below always reserves
        # an indicator line, which would emit 2 lines into a 1-row terminal).
        body = buf[header_n:] or buf
        off = min(max(0, _modal_scroll), len(body) - 1)
        _modal_scroll = off
        buf[:] = body[off:off + 1]
        return buf
    # Cap pinned-header lines at rows-2 (reserve >=1 body + 1 indicator line) so
    # the emitted head+window+indicator never exceeds the terminal height on a
    # very short terminal — without this, a 3-line pinned header on rows<=4
    # overflowed past the bottom.
    hn = max(0, min(header_n, len(buf) - 2, rows - 2))
    head = buf[:hn]
    body = buf[hn:]
    visible = max(1, rows - hn - 1)  # reserve one line for the indicator
    max_off = max(0, len(body) - visible)
    off = min(max(0, _modal_scroll), max_off)
    _modal_scroll = off  # persist the clamped value
    win = body[off:off + visible]
    indicator = _scroll_indicator(off, len(win), len(body))
    buf[:] = head + win + [indicator]
    return buf


def _apply_scroll(off, k, rows):
    """Map a navigation key to a new scroll offset. The upper bound is clamped
    in _window_buf at render time, so DOWN/PGDN/END may overshoot here."""
    try:
        page = max(1, int(rows) - 3)
    except (TypeError, ValueError):
        page = 10
    if k in ("<UP>", "k"):
        return max(0, off - 1)
    if k in ("<DOWN>", "j"):
        return off + 1
    if k == "<PGUP>":
        return max(0, off - page)
    if k == "<PGDN>":
        return off + page
    if k == "<HOME>":
        return 0
    if k == "<END>":
        return 10 ** 9  # clamped to the bottom by _window_buf
    return off


# ---------------------------------------------------------------------------
# Render — main dashboard
# ---------------------------------------------------------------------------
def render_frame(data, hist, cols, rows, show_legend=False, show_menu=False, show_cost=False, stale=False, show_agents=False, agents_active_only=False):
    if show_menu:
        return render_menu(cols, rows)
    if show_cost:
        return render_cost_breakdown(data, hist, cols, rows)
    if show_legend:
        return render_legend(cols, rows)
    if show_agents:
        return render_agents(data, cols, rows, active_only=agents_active_only)

    SW = cols
    buf = []

    # -- Extract data (sanitize to prevent terminal escape injection) --
    # `or {}` pattern guards against explicit JSON `null` values (not just missing keys).
    m = data.get("model") or {}
    model_str = badge_context_suffix(_sanitize(m.get("display_name", "?")))
    sname = _sanitize(data.get("session_name", ""))

    cw = data.get("context_window") or {}
    ctx_pct = round(_num(cw.get("used_percentage")), 1)
    ctx_total = _num(cw.get("context_window_size"), 0)
    usage = cw.get("current_usage") or {}

    rl = data.get("rate_limits")
    cost_d = data.get("cost") or {}
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

    # ── Header ──
    sid_str = str(data.get("session_id") or "default")
    ai_title = _scan_ai_title(data.get("transcript_path")) or ""
    # Cap dashboard label so the header line doesn't force a wider terminal floor.
    # Picker (s) renders the full title.
    if ai_title and len(ai_title) > 24:
        ai_title = ai_title[:23] + "…"
    session_label = sname or ai_title or (sid_str[:16] if _SID_RE.match(sid_str) else "default")

    buf.append(sep(SW))
    hp_plain = f"CC AIO MON {VERSION}  {model_str}"
    hp_pad = max(0, SW - len(hp_plain))
    hp_text = f"{C_WHT}{B}CC AIO MON {VERSION}{R}{BG_BAR}  {C_CYN}{model_str}{R}{BG_BAR}"
    buf.append(f"{BG_BAR}{hp_text}{' ' * hp_pad}{R}")

    # ── Session status line (always visible) ────────────────
    if stale:
        _stale_age = ""
        _sid_safe = str(data.get("session_id") or "default")
        if _SID_RE.match(_sid_safe):
            try:
                _mt = (DATA_DIR / f"{_sid_safe}.json").stat().st_mtime
                _idle = int(time.time() - _mt)
                _stale_age = f" ({_idle // 60}m)" if _idle >= 60 else f" ({_idle}s)"
            except OSError:
                pass
        buf.append(f"{C_RED}{B}{spin_session()} Session Inactive{R}{_stale_age}  {c(C_FG)}{session_label}{R}")
    else:
        buf.append(f"{C_GRN}{B}{spin_session()} Session Active{R} {C_FG}{session_label}{R}")

    buf.append(sep(SW))

    # ── Smart warnings (suppressed when stale, blink 500ms) ──
    _warns = [] if stale else collect_warnings(data, cpm, xpm)
    if _warns:
        wc = f"{C_RED}{B}" if _rls_blink() else C_DIM
        warn_parts = [f"{wc}{w}{R}" for w in _warns]
        buf.append(f"{'   '.join(warn_parts)}")
        buf.append(sep(SW))

    inp = _num(usage.get("input_tokens", 0))
    out = _num(usage.get("output_tokens", 0))
    cr = _num(usage.get("cache_read_input_tokens", 0))
    cwt = _num(usage.get("cache_creation_input_tokens", 0))

    # ── APR — API Ratio ──
    if dur > 0:
        apr_pct = min(100.0, round(api_dur / dur * 100, 1))
        buf.append(f"{c(C_GRN)}{B}APR{R} {mkbar(apr_pct, c(C_GRN))}")
        buf.append(f"    {C_DIM}DUR:{R} {c(C_WHT)}{f_dur(dur)}{R} {C_DIM}API:{R} {c(C_GRN)}{f_dur(api_dur)}{R}")
    else:
        buf.append(f"{c(C_GRN)}{B}APR{R} {mkbar(0, C_DIM)}")
    buf.append(sep(SW))

    # ── CHR — Cache Hit Rate ──
    if any([cr, cwt]):
        total_cache = cr + cwt
        chr_pct = round(cr / total_cache * 100, 1) if total_cache > 0 else 0
        buf.append(f"{c(C_GRN)}{B}CHR{R} {mkbar(chr_pct, c(C_GRN))}")
        buf.append(f"    {C_DIM}CRD:{R} {c(C_GRN)}{f_tok(cr)}{R} {C_DIM}CWR:{R} {c(C_GRN)}{f_tok(cwt)}{R}")
    else:
        buf.append(f"{c(C_GRN)}{B}CHR{R} {mkbar(0, C_DIM)}")
    buf.append(sep(SW))

    # ── CTX ──
    ctx_used = int(ctx_total * ctx_pct / 100) if ctx_total else 0
    buf.append(f"{c(C_CYN)}{B}CTX{R} {mkbar(ctx_pct, c(C_CYN))}")
    warn = f" {c(C_RED)}{B}!CTX>{int(CRIT_PCT)}%{R}" if ctx_pct >= CRIT_PCT else ""
    if any([inp, out]):
        buf.append(
            f"    {c(C_CYN)}{f_tok(ctx_used)}{R}{warn} "
            f"{C_DIM}INP:{R} {c(C_CYN)}{f_tok(inp)}{R} "
            f"{C_DIM}OUT:{R} {c(C_CYN)}{f_tok(out)}{R}"
        )
    else:
        buf.append(f"    {c(C_CYN)}{f_tok(ctx_used)}{R}{warn}")
    buf.append(sep(SW))

    # ── 5HL / 7DL ──
    if rl is not None:
        def _render_rate_limit(data_obj, label, window_sec):
            """SIZE-002: shared renderer for 5-hour and 7-day rate-limit
            blocks. Both differ only in the data key, label, and reset
            window length; rendering logic (pct, expired tag, color,
            countdown) is identical. Closure over `buf`, `c`, `mkbar`
            keeps the helper colocated with its only caller."""
            if not data_obj:
                return
            pct = round(_num(data_obj.get("used_percentage")), 1)
            resets = _num(data_obj.get("resets_at"), 0)
            expired = resets > 0 and resets < time.time()
            if expired:
                pct = 0.0
            lc = c(_limit_color(pct))
            expired_tag = f"  {C_DIM}(expired){R}" if expired else ""
            buf.append(f"{lc}{B}{label}{R} {mkbar(pct, lc)}{expired_tag}")
            rc = c(_reset_color(resets, window_sec))
            buf.append(f"    {C_DIM}RST:{R} {rc}{f_cd(resets if resets > 0 else None)}{R}")

        fh = rl.get("five_hour")
        sd = rl.get("seven_day")
        _render_rate_limit(fh, "5HL", SECONDS_5H)
        _render_rate_limit(sd, "7DL", SECONDS_7D)
        if not fh and not sd:
            buf.append(f"{C_DIM}Rate limits: no data{R}")
    else:
        buf.append(f"{C_DIM}Rate limits: subscription data unavailable{R}")

    buf.append(sep(SW))

    # ── Stats (BRN/CTR/CST/TDY/WEK/NOW/UPD/LNS) ─────────────
    brn_val = f"{cpm:.4f} $/min" if cpm and cpm > 0.0001 else "collecting..."
    ctr_val = f"{xpm:.2f} %/min" if xpm and xpm > 0.001 else "--"
    now = datetime.now().strftime("%H:%M:%S")
    if _SID_RE.match(sid_str):
        try:
            mt = (DATA_DIR / f"{sid_str}.json").stat().st_mtime
            age = int(time.time() - mt)
            age_s = f"{age}s" if age < 120 else f"{age // 60}m"
        except OSError:
            age_s = "?"
    else:
        age_s = "?"
    # ── BRN — burn rate bar (scales to BRN_MAX $/min) ──────
    brn_pct = min(100, cpm / BRN_MAX * 100) if cpm and cpm > 0 else 0
    buf.append(f"{c(C_ORN)}{B}BRN{R} {mkbar(brn_pct, c(C_ORN))}")
    buf.append(f"    {C_DIM}RTE:{R} {c(C_ORN)}{brn_val}{R}")
    # ── CTR — context rate bar (scales to CTR_MAX %/min) ───
    ctr_pct = min(100, xpm / CTR_MAX * 100) if xpm and xpm > 0 else 0
    buf.append(f"{c(C_YEL)}{B}CTR{R} {mkbar(ctr_pct, c(C_YEL))}")
    buf.append(f"    {C_DIM}RTE:{R} {c(C_YEL)}{ctr_val}{R}")
    # ── CST — session cost bar (scales to CST_MAX $) ───────
    cst_pct = min(100, usd / CST_MAX * 100) if usd > 0 else 0
    buf.append(f"{c(C_ORN)}{B}CST{R} {mkbar(cst_pct, c(C_ORN))}")
    buf.append(f"    {C_DIM}CST:{R} {c(C_ORN)}{f_cost(usd)}{R}")
    # ── Cross-session cost (TDY / WEK) ─────────────────────
    tdy, wek = cached_cross_session_costs()
    tdy_s = f_cost(tdy) if tdy > 0 else "--"
    wek_s = f_cost(wek) if wek > 0 else "--"
    buf.append(f"    {C_DIM}TDY:{R} {c(C_ORN)}{tdy_s}{R} {C_DIM}WEK:{R} {c(C_ORN)}{wek_s}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}NOW:{R} {c(C_WHT)}{now}{R} {C_DIM}UPD:{R} {c(C_WHT)}{age_s}{R}")
    if added or removed:
        buf.append(f"{C_DIM}LNS:{R} {c(C_GRN)}{added:,}{R} {c(C_RED)}{removed:,}{R}")

    # ── RLS (release check) ──
    # NOTE: _rls_maybe_check() is invoked from the main event loop, NOT here.
    # Keeping the trigger out of the render path (A-P2-1) avoids spawning a
    # background git fetch on every frame and lets test render assertions
    # stay deterministic without setting CC_AIO_MON_NO_UPDATE_CHECK env-flag
    # in each setUp.
    rls = _rls_snapshot()  # atomic read: status + remote_ver coherent
    rls_s = rls["status"]
    if rls_s == "update" and rls["remote_ver"]:
        rv = rls["remote_ver"]
        if _rls_blink():
            buf.append(f"{c(C_RED)}{B}RLS{R} {c(C_RED)}{B}{spin_rls()} v{rv} available{R}")
        else:
            buf.append(f"{c(C_RED)}{B}RLS{R} {C_DIM}{spin_rls()} v{rv} available{R}")
    elif rls_s == "ok":
        buf.append(f"{c(C_GRN)}RLS{R} {c(C_GRN)}{spin_rls()} Up to date{R}")
    elif rls_s is None:
        buf.append(f"{C_DIM}RLS {spin_rls()} Checking...{R}")
    # error/no_git/timeout — silent, render nothing

    # ── Footer ──
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}m{R}{C_DIM}] menu   [{R}{C_WHT}l{R}{C_DIM}] legend   [{R}{C_WHT}q{R}{C_DIM}] quit{R}")

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
    # ── Hotkeys (most-used — kept at the top) ──
    hp = max(0, SW - 7)
    buf.append(f"{BG_BAR}{C_WHT}{B}HOTKEYS{R}{BG_BAR}{' ' * hp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}]{R}   {C_DIM}Quit{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}m{R}{C_DIM}]{R}   {C_DIM}Menu{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}r{R}{C_DIM}]{R}   {C_DIM}Refresh{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}s{R}{C_DIM}]{R}   {C_DIM}Session Picker{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}t{R}{C_DIM}]{R}   {C_DIM}Token Stats{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}]{R}   {C_DIM}Agents (fan-out){R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}c{R}{C_DIM}]{R}   {C_DIM}Cost Breakdown{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}p{R}{C_DIM}]{R}   {C_DIM}Anthropic Pulse{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}u{R}{C_DIM}]{R}   {C_DIM}Update Manager{R} {C_DIM}a=apply{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}l{R}{C_DIM}]{R}   {C_DIM}Legend{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}1-9{R}{C_DIM}]{R} {C_DIM}Select Session / Period{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}f{R}{C_DIM}]{R}   {C_DIM}Agents: {R}{C_GRN}●{R}{C_DIM} active {R}{C_DIM}○ idle, toggle filter{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}↑↓ jk{R}{C_DIM}]{R} {C_DIM}Scroll modal{R} {C_DIM}PgUp/Dn Home/End{R}")
    # ── Dashboard metrics ──
    buf.append(sep(SW))
    dp = max(0, SW - 9)  # "DASHBOARD" = 9 chars
    buf.append(f"{BG_BAR}{C_WHT}{B}DASHBOARD{R}{BG_BAR}{' ' * dp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_GRN}APR{R} {C_DIM}API Ratio{R}")
    buf.append(f"{C_DIM} DUR  Duration - API  API Time{R}")
    buf.append(f"{C_GRN}CHR{R} {C_DIM}Cache Hit Rate{R}")
    buf.append(f"{C_DIM} CRD  Cache Read - CWR  Cache Write{R}")
    buf.append(f"{C_CYN}CTX{R} {C_DIM}Context Window{R}")
    buf.append(f"{C_DIM} INP  Input Tokens - OUT  Output Tokens{R}")
    buf.append(f"{C_YEL}5HL{R} {C_DIM}5-Hour Rate Limit{R}")
    buf.append(f"{C_YEL}7DL{R} {C_DIM}7-Day Rate Limit{R}")
    buf.append(f"{C_DIM} RST  Reset Countdown{R}")
    buf.append(f"{C_ORN}BRN{R} {C_DIM}Burn Rate{R} {C_DIM}0-{BRN_MAX} $/min{R}")
    buf.append(f"{C_YEL}CTR{R} {C_DIM}Context Rate{R} {C_DIM}0-{CTR_MAX} %/min{R}")
    buf.append(f"{C_DIM} RTE  Rate Value{R}")
    buf.append(f"{C_ORN}CST{R} {C_DIM}Session Cost{R} {C_DIM}0-{CST_MAX:.0f} ${R}")
    buf.append(f"{C_ORN}TDY{R} {C_DIM}Today Cost{R} {C_ORN}WEK{R} {C_DIM}7-Day Cost{R}")
    buf.append(f"{C_WHT}LNS{R} {C_DIM}Lines Changed{R} {C_GRN}+{R}{C_DIM}added{R} {C_RED}-{R}{C_DIM}removed{R}")
    buf.append(f"{C_WHT}NOW{R} {C_DIM}Current Time{R} {C_WHT}UPD{R} {C_DIM}Last Update{R}")
    buf.append(f"{C_WHT}RLS{R} {C_DIM}Release Status{R}")
    # ── Token Stats ──
    buf.append(sep(SW))
    tp = max(0, SW - 11)
    buf.append(f"{BG_BAR}{C_WHT}{B}TOKEN STATS{R}{BG_BAR}{' ' * tp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_WHT}SES{R} {C_DIM}Sessions{R} {C_WHT}DAY{R} {C_DIM}Active Days{R}")
    buf.append(f"{C_WHT}STK{R} {C_DIM}Streak{R} {C_WHT}LSS{R} {C_DIM}Longest Session{R}")
    buf.append(f"{C_WHT}TOP{R} {C_DIM}Most Active Day{R}")
    buf.append(f"{C_DIM} INP  Input - OUT  Output - CLS  Calls{R}")
    buf.append(f"{C_DIM} LIFETIME — pre-aggregated stats from CC cache{R}")
    buf.append(f"{C_WHT}MSG{R} {C_DIM}Total Messages{R} {C_WHT}TLC{R} {C_DIM}Tool Calls{R}")
    buf.append(f"{C_WHT}1ST{R} {C_DIM}First Session Date{R}")
    buf.append(f"{C_DIM} HRS / ACT  hour-of-day heatmap (UTC){R}")
    buf.append(f"{C_DIM} DAILY  per-day SES / MSG / TLC, last 5 days{R}")
    # ── Cost Breakdown ──
    buf.append(sep(SW))
    cp = max(0, SW - 14)
    buf.append(f"{BG_BAR}{C_WHT}{B}COST BREAKDOWN{R}{BG_BAR}{' ' * cp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM} LAST REQUEST — current message tokens{R}")
    buf.append(f"{C_ORN}INP{R} {C_DIM}Input Cost{R} {C_ORN}OUT{R} {C_DIM}Output Cost{R}")
    buf.append(f"{C_ORN}CRD{R} {C_DIM}Cache Read Cost{R} {C_ORN}CWR{R} {C_DIM}Cache Write Cost{R}")
    buf.append(f"{C_GRN}SAV{R} {C_DIM}Cache Savings{R}")
    buf.append(f"{C_DIM} SESSION BREAKDOWN — whole session, aggregated from transcript{R}")
    buf.append(f"{C_DIM} SUM  Sum of estimates (delta warn if >15% off CST){R}")
    buf.append(f"{C_WHT}TIN{R} {C_DIM}Total Input{R} {C_WHT}TOT{R} {C_DIM}Total Output{R}")
    buf.append(f"{C_ORN}CPM{R} {C_DIM}Cost/Min{R}")
    buf.append(f"{C_WHT}WSR{R} {C_DIM}Web Search Reqs{R} {C_WHT}WFR{R} {C_DIM}Web Fetch Reqs{R}")
    buf.append(f"{C_WHT}TIE{R} {C_DIM}Cache 1h-TTL{R} {C_WHT}T5M{R} {C_DIM}Cache 5m-TTL{R}")
    buf.append(f"{C_ORN}ERL{R} {C_DIM}Early 1/3{R} {C_ORN}MID{R} {C_DIM}Mid 1/3{R} {C_ORN}LAT{R} {C_DIM}Late 1/3{R}")
    # ── Update ──
    buf.append(sep(SW))
    up = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}UPDATE{R}{BG_BAR}{' ' * up}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_GRN}CUR{R} {C_DIM}Local Version{R} {C_WHT}REM{R} {C_DIM}Remote Version{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}press any key to close{R}")

    _window_buf(buf, rows)
    return buf


# ---------------------------------------------------------------------------
# Cost breakdown modal
# ---------------------------------------------------------------------------
# Prices per 1M tokens (USD). Sources: official Anthropic pricing page.
# cache_write = 5-minute TTL price. 1h cache write adds ~60% — documented separately.
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}  # Sonnet-tier fallback

# Single source of truth for model metadata — keyed by Anthropic model ID.
# Each entry: {"name": "...", "code": ("XX", "V.v"), "pricing": {...} | None}
# Pricing is per-1M-token USD (input / output / cache_read / cache_write).
# Adding a new model = one entry here, not three. Used by _get_pricing (below)
# and _model_code (token-stats modal section).
_MODELS = {
    "claude-fable-5": {"name": "Fable 5", "code": ("FA", "5"),
                       "pricing": {"input": 10.0, "output": 50.0, "cache_read": 1.00, "cache_write": 12.50}},
    # Project Glasswing only — same tier as Fable 5. Rare in transcripts.
    "claude-mythos-5": {"name": "Mythos 5", "code": ("MY", "5"),
                        "pricing": {"input": 10.0, "output": 50.0, "cache_read": 1.00, "cache_write": 12.50}},
    "claude-opus-4-8": {"name": "Opus 4.8", "code": ("OP", "4.8"),
                        "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
                        "pricing_fast": {"input": 10.0, "output": 50.0,  "cache_read": 1.00, "cache_write": 12.50}},
    "claude-opus-4-7": {"name": "Opus 4.7", "code": ("OP", "4.7"),
                        "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
                        "pricing_fast": {"input": 30.0, "output": 150.0, "cache_read": 3.00, "cache_write": 37.50}},
    "claude-opus-4-6": {"name": "Opus 4.6", "code": ("OP", "4.6"),
                        "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
                        "pricing_fast": {"input": 30.0, "output": 150.0, "cache_read": 3.00, "cache_write": 37.50}},
    "claude-opus-4-5": {"name": "Opus 4.5", "code": ("OP", "4.5"),
                        "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25}},
    "claude-opus-4-1": {"name": "Opus 4.1", "code": ("OP", "4.1"),
                        "pricing": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75}},
    "claude-sonnet-4-6": {"name": "Sonnet 4.6", "code": ("SO", "4.6"),
                          "pricing": {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}},
    "claude-sonnet-4-5": {"name": "Sonnet 4.5", "code": ("SO", "4.5"),
                          "pricing": {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}},
    "claude-haiku-4-5": {"name": "Haiku 4.5", "code": ("HA", "4.5"),
                         "pricing": {"input": 1.0,  "output": 5.0,  "cache_read": 0.10, "cache_write": 1.25}},
    # Retired model — kept for correct pricing of historical transcripts. Real ID
    # is claude-3-5-haiku-20241022; _model_base strips the -YYYYMMDD to this key.
    "claude-3-5-haiku": {"name": "Haiku 3.5", "code": ("HA", "3.5"),
                         "pricing": {"input": 0.8,  "output": 4.0,  "cache_read": 0.08, "cache_write": 1.00}},
    # Short-ID fallbacks (some transcript entries use abbreviated IDs). No pricing.
    "haiku":  {"name": "Haiku",  "code": ("HA", ""), "pricing": None},
    "sonnet": {"name": "Sonnet", "code": ("SO", ""), "pricing": None},
    "opus":   {"name": "Opus",   "code": ("OP", ""), "pricing": None},
}


def _model_base(model_id):
    """Normalize a model ID for _MODELS lookup: strip the [..] context-tier
    suffix and a trailing -YYYYMMDD date snapshot. Statusline sends bare IDs
    (claude-opus-4-8), transcripts send dated ones (claude-haiku-4-5-20251001) —
    both must resolve to the same key."""
    base = (model_id or "").split("[")[0]
    return re.sub(r"-\d{8}$", "", base)


def _get_pricing(model_id, speed=None):
    """Get pricing for model. speed="fast" selects fast-mode rates when the
    model defines them; otherwise standard. Falls back to _DEFAULT_PRICING."""
    entry = _MODELS.get(_model_base(model_id))
    if entry:
        if speed == "fast" and entry.get("pricing_fast"):
            return entry["pricing_fast"]
        if entry.get("pricing"):
            return entry["pricing"]
    return _DEFAULT_PRICING


_SESSION_COST_CACHE = OrderedDict()  # LRU: {session_id: (ts, breakdown_dict)}
_SESSION_COST_CACHE_MAX = 64  # cap — prevents unbounded growth across rotating session IDs
_SESSION_COST_TTL = 5.0   # refresh every 5s

CLAUDE_PROJECTS_DIR = _CLAUDE_DIR


def _safe_claude_projects_root():
    """Return resolved ~/.claude/projects root only when the root itself is safe."""
    root = pathlib.Path(CLAUDE_PROJECTS_DIR)
    if not is_safe_dir(root):
        return None
    try:
        return root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _safe_transcript_path(tp):
    """Validate that transcript path is a regular file inside ~/.claude/projects/.
    Rejects symlinks, relative escapes, and absolute paths outside the allowed root."""
    if not tp or not isinstance(tp, str):
        return None
    try:
        root = _safe_claude_projects_root()
        if root is None:
            return None
        cand = pathlib.Path(tp)
        try:
            st = cand.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        try:
            resolved = cand.resolve(strict=True)
        except OSError:
            return None
        # Python 3.8 compat: is_relative_to not available
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved
    except (OSError, ValueError):
        return None


_AI_TITLE_CACHE = OrderedDict()  # LRU: {sid: (ts, mtime, title_or_None)}
_AI_TITLE_CACHE_MAX = 64
_AI_TITLE_TTL = 30.0
_AI_TITLE_SCAN_BYTES = 64 * 1024  # CC writes ai-title within first ~20 records (<50 KiB); 64 KiB head suffices and is ~8x faster on cache miss than the prior 512 KiB cap


def _scan_ai_title(transcript_path):
    """Return sanitized aiTitle from transcript JSONL, or None.

    CC writes `{"type":"ai-title","aiTitle":"..."}` records early in the file
    (within the first dozen records). We bound-read the head to keep this fast
    on multi-megabyte transcripts called per render tick. Last record in the
    scanned window wins. Path containment via _safe_transcript_path.
    """
    path = _safe_transcript_path(transcript_path)
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > TRANSCRIPT_MAX_BYTES:
        return None

    sid = path.stem
    if not _SID_RE.match(sid):
        return None

    now = time.time()
    cached = _AI_TITLE_CACHE.get(sid)
    if cached and cached[1] == st.st_mtime and (now - cached[0]) < _AI_TITLE_TTL:
        _AI_TITLE_CACHE.move_to_end(sid)
        return cached[2]

    title = None
    try:
        with open(path, "rb") as fh:
            # S-P2-2 (CWE-367): TOCTOU mitigation — re-stat the *open*
            # file descriptor and require (st_ino, st_dev) to match the
            # pre-open stat. If they differ, the underlying inode was
            # swapped between resolve() (in _safe_transcript_path) and open(), which on
            # a shared filesystem could let another user point us at a
            # different file via fast unlink/link or symlink-flip races.
            # st_dev guard ensures the swap can't move us to a different
            # mount either.
            try:
                fst = os.fstat(fh.fileno())
            except OSError:
                raw = None
            else:
                if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
                    raw = None
                else:
                    raw = fh.read(_AI_TITLE_SCAN_BYTES)
    except OSError:
        raw = None
    if raw:
        try:
            text = raw.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, ValueError):
            text = ""
        # Drop the trailing fragment if our read sliced mid-line.
        if len(raw) >= _AI_TITLE_SCAN_BYTES:
            nl = text.rfind("\n")
            if nl >= 0:
                text = text[:nl]
        for line in text.splitlines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("type") == "ai-title":
                t = obj.get("aiTitle")
                if isinstance(t, str) and t.strip():
                    title = _sanitize(t.strip())

    while len(_AI_TITLE_CACHE) >= _AI_TITLE_CACHE_MAX:
        _AI_TITLE_CACHE.popitem(last=False)
    _AI_TITLE_CACHE[sid] = (now, st.st_mtime, title)
    return title


def _aggregate_session_cost(data):
    """Walk the current session's transcript JSONL, sum per-category tokens
    across all assistant records, apply pricing per-record model.
    Returns dict {input, output, cache_read, cache_write, cost_total,
                  cost_input, cost_output, cost_cache_read, cost_cache_write}
    or None if transcript unreachable.
    """
    sid = (data.get("session_id") or "").strip()
    if not sid or not _SID_RE.match(sid):
        return None

    now = time.time()
    cached = _SESSION_COST_CACHE.get(sid)
    if cached and (now - cached[0]) < _SESSION_COST_TTL:
        _SESSION_COST_CACHE.move_to_end(sid)  # LRU touch
        return cached[1]

    tp = data.get("transcript_path")
    path = _safe_transcript_path(tp)
    if path is None:
        # Fallback: scan ~/.claude/projects/*/{sid}.jsonl (first match wins)
        try:
            home = pathlib.Path(CLAUDE_PROJECTS_DIR)
            if is_safe_dir(home):
                for cand in home.glob(f"*/{sid}.jsonl"):
                    if cand.is_file():
                        path = _safe_transcript_path(str(cand))
                        if path:
                            break
        except OSError:
            path = None
    if path is None:
        return None

    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > TRANSCRIPT_MAX_BYTES:
        return None

    inp = out = cr = cw = 0.0
    ci = co = ccr = ccw = 0.0
    wsr = wfr = 0
    c1h = c5m = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # S-P2-2 (CWE-367): TOCTOU guard — same fstat identity check as
            # _scan_ai_title: if the inode/device pair diverged between the
            # stat above and this open, the file was swapped underneath us.
            try:
                fst = os.fstat(f.fileno())
            except OSError:
                return None
            if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
                return None
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("type") != "assistant":
                    continue
                # Same crash class as the stats-modal aggregator: a string
                # "message" would turn the .get() calls into AttributeError.
                msg = rec.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage") or {}
                if not isinstance(u, dict):
                    u = {}
                mid = msg.get("model") or ""
                if not isinstance(mid, str):
                    mid = ""
                pricing = _get_pricing(mid, u.get("speed"))
                # Per-record token deltas — name with cr_inc/cw_inc (not r/w)
                # to avoid visual collision with module-level `R` ANSI reset.
                in_inc = _num(u.get("input_tokens", 0))
                out_inc = _num(u.get("output_tokens", 0))
                cr_inc = _num(u.get("cache_read_input_tokens", 0))
                cw_inc = _num(u.get("cache_creation_input_tokens", 0))
                inp += in_inc; out += out_inc; cr += cr_inc; cw += cw_inc
                ci += in_inc * pricing["input"] / 1_000_000
                co += out_inc * pricing["output"] / 1_000_000
                ccr += cr_inc * pricing["cache_read"] / 1_000_000
                ccw += cw_inc * pricing["cache_write"] / 1_000_000
                stu = u.get("server_tool_use") or {}
                if isinstance(stu, dict):
                    wsr += int(_num(stu.get("web_search_requests", 0)))
                    wfr += int(_num(stu.get("web_fetch_requests", 0)))
                cc = u.get("cache_creation") or {}
                if isinstance(cc, dict):
                    c1h += int(_num(cc.get("ephemeral_1h_input_tokens", 0)))
                    c5m += int(_num(cc.get("ephemeral_5m_input_tokens", 0)))
    except OSError:
        return None

    result = {
        "input": int(inp), "output": int(out),
        "cache_read": int(cr), "cache_write": int(cw),
        "cost_input": ci, "cost_output": co,
        "cost_cache_read": ccr, "cost_cache_write": ccw,
        "cost_total": ci + co + ccr + ccw,
        "web_search_requests": wsr,
        "web_fetch_requests": wfr,
        "cache_1h": c1h, "cache_5m": c5m,
    }
    _SESSION_COST_CACHE[sid] = (now, result)
    _SESSION_COST_CACHE.move_to_end(sid)
    while len(_SESSION_COST_CACHE) > _SESSION_COST_CACHE_MAX:
        _SESSION_COST_CACHE.popitem(last=False)  # evict LRU
    return result


def _cost_thirds(hist):
    """Split session into 3 equal time slices. Returns list of (label, cost, rate_per_min) or []."""
    costs = []
    for entry in hist:
        t = _num(entry.get("t", 0))
        c = _num((entry.get("cost") or {}).get("total_cost_usd", 0))
        if t > 0:
            costs.append((t, c))
    if len(costs) < 2:
        return []
    costs.sort(key=lambda x: x[0])
    t_start, t_end = costs[0][0], costs[-1][0]
    span = t_end - t_start
    if span < 30:  # need at least 30s of data
        return []
    times = [x[0] for x in costs]
    vals = [x[1] for x in costs]
    third = span / 3
    boundaries = [t_start, t_start + third, t_start + 2 * third, t_end]
    labels = ["early", "mid", "late"]
    result = []
    for i in range(3):
        idx_s = max(0, bisect.bisect_right(times, boundaries[i]) - 1)
        idx_e = max(0, bisect.bisect_right(times, boundaries[i + 1]) - 1)
        delta = max(0.0, vals[idx_e] - vals[idx_s])
        rate = delta / (third / 60) if third > 0 else 0.0  # $/min
        result.append((labels[i], delta, rate))
    return result


def render_cost_breakdown(data, hist, cols, rows):
    """Render session cost breakdown modal."""
    SW = cols
    buf = []
    buf.append(sep(SW))
    cb_pad = max(0, SW - 14)
    buf.append(f"{BG_BAR}{C_WHT}{B}COST BREAKDOWN{R}{BG_BAR}{' ' * cb_pad}{R}")
    buf.append(sep(SW))

    cost_d = data.get("cost") or {}
    usd = _num(cost_d.get("total_cost_usd"))
    dur = _num(cost_d.get("total_duration_ms"))
    cw = data.get("context_window") or {}
    usage = cw.get("current_usage") or {}
    model_id = (data.get("model") or {}).get("id", "")
    # Statusline current_usage carries no `speed` field, so per-request CST cannot
    # detect fast mode — standard rates only. Session breakdown (transcript) does.
    pricing = _get_pricing(model_id)

    # Token counts
    inp = _num(usage.get("input_tokens", 0))
    out = _num(usage.get("output_tokens", 0))
    cr = _num(usage.get("cache_read_input_tokens", 0))
    cwt = _num(usage.get("cache_creation_input_tokens", 0))
    total_in = _num(cw.get("total_input_tokens", 0))
    total_out = _num(cw.get("total_output_tokens", 0))

    model_name = _sanitize((data.get("model") or {}).get("display_name", "?"))
    # Strip verbose context suffix: "Opus 4.6 (1M context)" → "Opus 4.6 1M"
    model_short = compact_context_suffix(model_name)
    buf.append(f"{C_ORN}{B}CST{R} {C_ORN}{B}{f_cost(usd)}{R} {C_DIM}{f_dur(dur)} - {model_short}{R}")

    # Cost estimates per token type
    inp_cost = inp * pricing["input"] / 1_000_000
    out_cost = out * pricing["output"] / 1_000_000
    cr_cost = cr * pricing["cache_read"] / 1_000_000
    cw_cost = cwt * pricing["cache_write"] / 1_000_000

    # What would cache reads cost at full input price?
    cr_full_price = cr * pricing["input"] / 1_000_000
    cache_savings = cr_full_price - cr_cost

    buf.append(sep(SW))
    tc_title = "LAST REQUEST (est.)"
    tc_pad = max(0, SW - len(tc_title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{tc_title}{R}{BG_BAR}{' ' * tc_pad}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_ORN}INP{R} {C_WHT}{f_tok(inp)}{R} {C_DIM}~{f_cost(inp_cost)}{R}")
    buf.append(f"{C_ORN}OUT{R} {C_WHT}{f_tok(out)}{R} {C_DIM}~{f_cost(out_cost)}{R}")
    buf.append(f"{C_ORN}CRD{R} {C_WHT}{f_tok(cr)}{R} {C_DIM}~{f_cost(cr_cost)}{R}")
    buf.append(f"{C_ORN}CWR{R} {C_WHT}{f_tok(cwt)}{R} {C_DIM}~{f_cost(cw_cost)}{R}")

    if cache_savings > 0.001:
        sav_pct = round(cache_savings / (cache_savings + cr_cost) * 100) if (cache_savings + cr_cost) > 0 else 0
        buf.append(f"{C_GRN}SAV{R} {C_GRN}~{f_cost(cache_savings)}{R} {C_DIM}({sav_pct}% vs uncached){R}")

    # Session-wide breakdown (aggregates transcript)
    sess = _aggregate_session_cost(data)
    if sess:
        buf.append(sep(SW))
        sb_title = "SESSION BREAKDOWN (est.)"
        sb_pad = max(0, SW - len(sb_title))
        buf.append(f"{BG_BAR}{C_WHT}{B}{sb_title}{R}{BG_BAR}{' ' * sb_pad}{R}")
        buf.append(sep(SW))
        buf.append(f"{C_ORN}INP{R} {C_WHT}{f_tok(sess['input'])}{R} {C_DIM}~{f_cost(sess['cost_input'])}{R}")
        buf.append(f"{C_ORN}OUT{R} {C_WHT}{f_tok(sess['output'])}{R} {C_DIM}~{f_cost(sess['cost_output'])}{R}")
        buf.append(f"{C_ORN}CRD{R} {C_WHT}{f_tok(sess['cache_read'])}{R} {C_DIM}~{f_cost(sess['cost_cache_read'])}{R}")
        buf.append(
            f"{C_ORN}CWR{R} {C_WHT}{f_tok(sess['cache_write'])}{R} "
            f"{C_DIM}~{f_cost(sess['cost_cache_write'])}{R}"
        )
        delta = sess["cost_total"] - usd if usd > 0 else 0
        if usd > 0:
            pct_diff = abs(delta) / usd * 100 if usd > 0 else 0
            sum_cost = f_cost(sess['cost_total'])
            cst_cost = f_cost(usd)
            if pct_diff > 15:
                buf.append(
                    f"{C_DIM}SUM ~{sum_cost} vs CST {cst_cost} — "
                    f"{C_YEL}delta {pct_diff:.0f}%{R}"
                )
            else:
                buf.append(f"{C_DIM}SUM ~{sum_cost} (~= CST {cst_cost}){R}")

    buf.append(sep(SW))
    st_title = "CONTEXT WINDOW"
    st_pad = max(0, SW - len(st_title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{st_title}{R}{BG_BAR}{' ' * st_pad}{R}")
    buf.append(sep(SW))
    # total_input_tokens/total_output_tokens are the CURRENT context window
    # (Claude Code v2.1.132+), not cumulative session totals — label accordingly.
    buf.append(f"{C_DIM}CIN:{R} {C_WHT}{f_tok(total_in)}{R} {C_DIM}COUT:{R} {C_WHT}{f_tok(total_out)}{R}")
    if dur > 0:
        cpm_val = usd / (dur / 60000)
        buf.append(f"{C_DIM}CPM:{R} {C_ORN}{cpm_val:.4f} $/min{R}")

    # Server-side tool calls + cache TTL split (only if non-zero)
    if sess:
        wsr_v = int(sess.get("web_search_requests", 0))
        wfr_v = int(sess.get("web_fetch_requests", 0))
        c1h_v = int(sess.get("cache_1h", 0))
        c5m_v = int(sess.get("cache_5m", 0))
        if wsr_v or wfr_v:
            buf.append(
                f"{C_DIM}WSR:{R} {C_WHT}{wsr_v}{R}    "
                f"{C_DIM}WFR:{R} {C_WHT}{wfr_v}{R}"
            )
        if c1h_v or c5m_v:
            buf.append(
                f"{C_DIM}TIE:{R} {C_WHT}{f_tok(c1h_v)}{R}  "
                f"{C_DIM}T5M:{R} {C_WHT}{f_tok(c5m_v)}{R}"
            )

    # Burn rate over time — 3 equal time slices, bar scaled to BRN_MAX
    thirds = _cost_thirds(hist)
    if thirds:
        br_title = f"BURN RATE OVER TIME (0-{BRN_MAX} $/min)"
        br_pad = max(0, SW - len(br_title))
        buf.append(sep(SW))
        buf.append(f"{BG_BAR}{C_WHT}{B}{br_title}{R}{BG_BAR}{' ' * br_pad}{R}")
        buf.append(sep(SW))
        _COT_LABELS = {"early": "ERL", "mid": "MID", "late": "LAT"}
        for label, cost, rate in thirds:
            pct = min(100.0, rate / BRN_MAX * 100) if BRN_MAX > 0 else 0
            code = _COT_LABELS.get(label, label.upper()[:3])
            buf.append(f"{C_ORN}{B}{code}{R} {mkbar(pct, C_ORN)}")
            buf.append(f"    {C_DIM}RTE:{R} {C_ORN}{rate:.4f} $/min{R} {C_DIM}CST:{R} {C_ORN}{f_cost(cost)}{R}")

    buf.append(sep(SW))
    buf.append(f"{C_DIM}press any key to close{R}")

    _window_buf(buf, rows)
    return buf


# ---------------------------------------------------------------------------
# Anthropic Pulse modal
# ---------------------------------------------------------------------------
_PULSE_LEVEL_COLOR = {
    "ok":       C_GRN,
    "degraded": C_YEL,
    "bad":      C_RED,
    "error":    C_DIM,
}

_PULSE_COMPONENT_COLOR = {
    "operational":             C_GRN,
    "degraded_performance":    C_YEL,
    "partial_outage":          C_ORN,
    "major_outage":            C_RED,
    "under_maintenance":       C_CYN,
}


def _pulse_age(snap):
    """Return human-readable age of the snapshot."""
    wall_t = snap.get("wall_t", 0) or 0
    if wall_t <= 0:
        return "--"
    age_s = max(0, int(time.time() - wall_t))
    if age_s < 60:
        return f"{age_s}s ago"
    if age_s < SECONDS_1H:
        return f"{age_s // 60}m ago"
    return f"{age_s // SECONDS_1H}h ago"


def render_pulse_modal(cols, rows):
    """Render Anthropic backend stability modal (P key)."""
    snap = pulse.get_pulse_snapshot()
    SW = cols
    buf = []

    buf.append(sep(SW))
    title = "ANTHROPIC PULSE"
    t_pad = max(0, SW - len(title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{title}{R}{BG_BAR}{' ' * t_pad}{R}")
    buf.append(sep(SW))

    level = snap.get("level") or "error"
    color = _PULSE_LEVEL_COLOR.get(level, C_DIM)
    score = snap.get("score")
    verdict = _sanitize(snap.get("verdict") or "AWAITING DATA")
    reason = _sanitize(snap.get("reason") or "")

    if score is None:
        buf.append(f"{C_DIM}{B}STB{R} {mkbar(0, C_DIM)}")
    else:
        buf.append(f"{color}{B}STB{R} {mkbar(float(score), color)}")

    # Verdict line
    buf.append("")
    buf.append(f"{color}{B}>> {verdict} <<{R}")
    if reason:
        buf.append(f"{C_DIM}reason: {reason}{R}")

    buf.append(sep(SW))
    # Details
    indicator = snap.get("indicator")
    if indicator:
        ind_label = pulse.indicator_label(indicator)
        if indicator == "none":
            ind_color = C_GRN
        elif indicator in ("minor", "maintenance"):
            ind_color = C_YEL
        elif indicator in ("major", "critical"):
            ind_color = C_RED
        else:
            # Unknown indicator from a future status schema — stay neutral rather than alarming red
            ind_color = C_DIM
        buf.append(f"{C_DIM}INDICATOR {R} {ind_color}{_sanitize(indicator)}{R} {C_DIM}({_sanitize(ind_label)}){R}")
    else:
        buf.append(f"{C_DIM}INDICATOR {R} {C_DIM}--{R}")

    incidents = snap.get("incidents") or []
    inc_color = C_GRN if not incidents else (C_YEL if len(incidents) < 3 else C_RED)
    buf.append(f"{C_DIM}INCIDENTS {R} {inc_color}{len(incidents)}{R}")

    # Per-model rollup — any model mentioned in any active incident = affected.
    # Silent when no incidents mention models.
    affected = set()
    for inc in incidents:
        for m in (inc.get("affected_models") or []):
            affected.add(m)
    if affected or incidents:
        parts = []
        for m in ("opus", "sonnet", "haiku"):
            if m in affected:
                parts.append(f"{C_RED}{B}{m}{R}")
            else:
                parts.append(f"{C_GRN}{m}{R}")
        buf.append(f"{C_DIM}MODELS    {R} " + f" {C_DIM}/{R} ".join(parts))

    latency = snap.get("latency_ms")
    if latency is None:
        buf.append(f"{C_DIM}LATENCY   {R} {C_RED}timeout{R}")
    else:
        if latency < 300:
            lc = C_GRN
        elif latency < 800:
            lc = C_YEL
        else:
            lc = C_RED
        buf.append(f"{C_DIM}LATENCY   {R} {lc}{int(latency)} ms{R}")

    # p50 / p95 — appears once we have >=3 samples
    p50 = snap.get("latency_p50_ms")
    p95 = snap.get("latency_p95_ms")
    if p50 is not None and p95 is not None:
        buf.append(f"{C_DIM}P50 / P95 {R} {C_WHT}{p50} ms{R} {C_DIM}/{R} {C_WHT}{p95} ms{R}")

    # Raw (instant) score — shown when it diverges from smoothed by > 5 points
    raw_score = snap.get("raw_score")
    if score is not None and raw_score is not None and abs(int(raw_score) - int(score)) > 5:
        buf.append(f"{C_DIM}INSTANT   {R} {C_DIM}{int(raw_score)}% (smoothed: {int(score)}%){R}")

    buf.append(f"{C_DIM}UPDATED   {R} {C_WHT}{_pulse_age(snap)}{R}")

    # Error detail (if any)
    err = snap.get("error")
    if err:
        buf.append(sep(SW))
        buf.append(f"{C_RED}{B}ERROR{R} {C_DIM}{_sanitize(err)}{R}")

    # Active incidents (first 3)
    if incidents:
        buf.append(sep(SW))
        ih = "ACTIVE INCIDENTS"
        ih_pad = max(0, SW - len(ih))
        buf.append(f"{BG_BAR}{C_WHT}{B}{ih}{R}{BG_BAR}{' ' * ih_pad}{R}")
        buf.append(sep(SW))
        for inc in incidents[:3]:
            name = _sanitize(inc.get("name") or "?")[:SW - 8]
            impact = _sanitize(inc.get("impact") or "minor")
            ic = C_RED if impact in ("major", "critical") else C_YEL
            models = inc.get("affected_models") or []
            tag = ""
            if models:
                tag = f" {C_DIM}[{R}{C_ORN}{','.join(models)}{R}{C_DIM}]{R}"
            buf.append(f"{ic}{impact.upper()[:4]:<4}{R} {C_WHT}{name}{R}{tag}")

    # Components
    components = snap.get("components") or []
    if components:
        buf.append(sep(SW))
        ch = "COMPONENTS"
        ch_pad = max(0, SW - len(ch))
        buf.append(f"{BG_BAR}{C_WHT}{B}{ch}{R}{BG_BAR}{' ' * ch_pad}{R}")
        buf.append(sep(SW))
        # Longest possible status label ("partial outage" = 14). Reserve sep + label.
        name_w = max(18, SW - 16)
        for c in components[:10]:
            name = _sanitize(c.get("name") or "?")
            # Strip parenthetical suffixes (e.g. "Claude API (api.anthropic.com)" → "Claude API")
            name = re.sub(r"\s*\([^)]*\)?\s*$", "", name).strip() or name
            cstatus = _sanitize(c.get("status") or "unknown")
            cc = _PULSE_COMPONENT_COLOR.get(cstatus, C_DIM)
            buf.append(f"{C_WHT}{name[:name_w]:<{name_w}}{R} {cc}{cstatus.replace('_', ' ')}{R}")

    buf.append(sep(SW))
    footer = "source: status.claude.com + api.anthropic.com ping"
    if len(footer) > SW:
        footer = "source: status.claude.com + api ping"
    buf.append(f"{C_DIM}{footer[:SW]}{R}")
    buf.append(f"{C_DIM}press any key to close{R}")

    _window_buf(buf, rows)
    return buf


# ---------------------------------------------------------------------------
# Menu modal
# ---------------------------------------------------------------------------
def render_menu(cols, rows):
    SW = cols
    buf = []
    buf.append(sep(SW))
    mn_pad = max(0, SW - 6)  # "≡ MENU" = 6 chars
    buf.append(f"{BG_BAR}{C_WHT}{B}\u2261 MENU{R}{BG_BAR}{' ' * mn_pad}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}]{R}   {C_DIM}Quit{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}r{R}{C_DIM}]{R}   {C_DIM}Refresh{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}s{R}{C_DIM}]{R}   {C_DIM}Session Picker{R}")
    buf.append(sep(SW))
    vp = max(0, SW - 5)
    buf.append(f"{BG_BAR}{C_WHT}{B}VIEWS{R}{BG_BAR}{' ' * vp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}t{R}{C_DIM}]{R}   {C_DIM}Token Stats{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}]{R}   {C_DIM}Agents (fan-out){R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}c{R}{C_DIM}]{R}   {C_DIM}Cost Breakdown{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}p{R}{C_DIM}]{R}   {C_DIM}Anthropic Pulse{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}l{R}{C_DIM}]{R}   {C_DIM}Legend{R}")
    buf.append(sep(SW))
    sp = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}SYSTEM{R}{BG_BAR}{' ' * sp}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}u{R}{C_DIM}]{R}   {C_DIM}Update Manager{R} {C_DIM}a=apply{R}")
    buf.append(sep(SW))
    np = max(0, SW - 10)  # "NAVIGATION" = 10 chars
    buf.append(f"{BG_BAR}{C_WHT}{B}NAVIGATION{R}{BG_BAR}{' ' * np}{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}↑↓ jk{R}{C_DIM}]{R} {C_DIM}Scroll{R}  {C_DIM}[{R}{C_WHT}PgUp/Dn{R}{C_DIM}]{R} {C_DIM}Page{R}  {C_DIM}[{R}{C_WHT}Home/End{R}{C_DIM}]{R} {C_DIM}Jump{R}")
    buf.append(sep(SW))
    buf.append(f"{C_DIM}press any key to close{R}")

    _window_buf(buf, rows)
    return buf


# ---------------------------------------------------------------------------
# Update modal
# ---------------------------------------------------------------------------
_update_result = None  # None=not run, str=output message
_update_lock = threading.Lock()
# Handle to the in-flight self-update worker (set by _apply_update_action).
# Read by the main loop's `q` handler to refuse quitting mid git-pull, which
# would kill the daemon before its post-pull syntax check runs (M-cross-1).
# Written and read only on the main thread; is_alive() is thread-safe.
_update_thread = None
# cleanup() joins an in-flight update worker for at most this long so signals
# (SIGTERM / Ctrl-C) and KeyboardInterrupt — which bypass the `q`-gate — still
# let the post-pull syntax check finish, while staying bounded so a hung pull
# can never wedge the exit path.
_UPDATE_JOIN_TIMEOUT = 2.0


def _join_update_worker(timeout=_UPDATE_JOIN_TIMEOUT):
    """Block up to ``timeout`` seconds for an in-flight self-update worker to
    finish its (atomic pull +) post-pull syntax check. The git pull is atomic,
    but killing the daemon mid-check would skip the integrity verification
    (M-cross-1). Bounded, so any exit path — q, SIGTERM, Ctrl-C — stays prompt
    even if the pull hangs on the network."""
    t = _update_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)

# Update modal git-helper cache — 30s TTL, invalidated on remote_ver change.
# Eliminates per-tick (50ms) synchronous git subprocess spam from render path
# while the update modal is open. See audit P0-1 (24.05.2026).
_update_modal_cache = {
    "commits_ts": 0.0, "commits_ver": None, "commits": [],
    "changelog_ts": 0.0, "changelog_ver": None, "changelog": [],
    "checks_ts": 0.0, "checks": [],
}
_UPDATE_MODAL_TTL = 30.0


def _git_cmd(args, timeout=15):
    """Run git command in repo root, return (returncode, stdout, stderr).
    Uses module-level run_git for consistent env whitelist + mockable patch target."""
    try:
        r = run_git(args, cwd=_REPO_ROOT, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "git not found"
    except subprocess.TimeoutExpired:
        return -2, "", "timeout"


def _update_checks():
    """Return list of warning strings for update safety."""
    warns = []
    rc, out, _ = _git_cmd(["rev-parse", "--abbrev-ref", "HEAD"])
    if rc == 0 and out != "main":
        warns.append(f"Not on main branch (current: {out})")
    rc, out, _ = _git_cmd(["status", "--porcelain", "-uno"])
    if rc == 0 and out:
        warns.append("Uncommitted changes in working tree")
    rc, out, _ = _git_cmd(["rev-list", "--left-right", "--count", "HEAD...origin/main"])
    if rc == 0:
        try:
            ahead, behind = parse_ahead_behind(out)
        except ValueError:
            ahead = behind = 0
        if ahead > 0 and behind > 0:
            warns.append(f"Diverged: {ahead} ahead, {behind} behind origin/main")
    # SEC: pin self-update to the canonical repo (same guard as update.py CLI).
    remote_problem = verify_origin_remote(_REPO_ROOT)
    if remote_problem:
        warns.append(f"Origin check: {remote_problem}")
    return warns


def _get_new_commits(max_lines=10):
    """Return list of oneline commit strings from HEAD to origin/main."""
    rc, out, _ = _git_cmd(["log", "--oneline", f"--max-count={max_lines}", "HEAD..origin/main"])
    if rc != 0 or not out:
        return []
    return out.split("\n")


def _get_remote_changelog_preview(version, max_lines=15):
    """Extract changelog section for a version from origin/main."""
    rc, out, _ = _git_cmd(["show", "origin/main:CHANGELOG.md"])
    if rc != 0:
        return []
    entry = extract_changelog_entry(out, version, max_lines=max_lines)
    if not entry:
        return []
    return entry.split("\n")


def _cached_get_new_commits(remote_ver, max_lines=10):
    """30s TTL cache around _get_new_commits, invalidated on remote_ver change."""
    now = time.monotonic()
    with _update_lock:
        if (_update_modal_cache["commits_ver"] == remote_ver
                and now - _update_modal_cache["commits_ts"] < _UPDATE_MODAL_TTL):
            return _update_modal_cache["commits"]
    # Cache miss — release lock during git call to keep other render-path callers responsive
    commits = _get_new_commits(max_lines=max_lines)
    with _update_lock:
        _update_modal_cache["commits_ts"] = now
        _update_modal_cache["commits_ver"] = remote_ver
        _update_modal_cache["commits"] = commits
    return commits


def _cached_get_remote_changelog_preview(version, max_lines=15):
    """30s TTL cache around _get_remote_changelog_preview, invalidated on version change."""
    now = time.monotonic()
    with _update_lock:
        if (_update_modal_cache["changelog_ver"] == version
                and now - _update_modal_cache["changelog_ts"] < _UPDATE_MODAL_TTL):
            return _update_modal_cache["changelog"]
    cl = _get_remote_changelog_preview(version, max_lines=max_lines)
    with _update_lock:
        _update_modal_cache["changelog_ts"] = now
        _update_modal_cache["changelog_ver"] = version
        _update_modal_cache["changelog"] = cl
    return cl


def _cached_update_checks():
    """30s TTL cache around _update_checks (3 git subprocess calls)."""
    now = time.monotonic()
    with _update_lock:
        if now - _update_modal_cache["checks_ts"] < _UPDATE_MODAL_TTL:
            return _update_modal_cache["checks"]
    warns = _update_checks()
    with _update_lock:
        _update_modal_cache["checks_ts"] = now
        _update_modal_cache["checks"] = warns
    return warns


def _invalidate_update_modal_cache():
    """Drop cached update-modal git results — called after a successful pull
    so the modal immediately reflects post-update state (no stale 'commits ahead')."""
    with _update_lock:
        _update_modal_cache["commits_ts"] = 0.0
        _update_modal_cache["commits_ver"] = None
        _update_modal_cache["commits"] = []
        _update_modal_cache["changelog_ts"] = 0.0
        _update_modal_cache["changelog_ver"] = None
        _update_modal_cache["changelog"] = []
        _update_modal_cache["checks_ts"] = 0.0
        _update_modal_cache["checks"] = []


def _set_update_result(value):
    global _update_result
    with _update_lock:
        _update_result = value


def _get_update_result():
    with _update_lock:
        return _update_result


def _apply_update_worker():
    """Background worker: re-run safety checks, then git pull --ff-only +
    syntax check. Sets _update_result. The checks mirror the update.py CLI
    guards (branch / clean tree / divergence / pinned origin) — the modal
    render is advisory only, so the worker must enforce them itself."""
    try:
        warns = _update_checks()
        if warns:
            _set_update_result(
                "Update blocked: " + "; ".join(_sanitize(w) for w in warns)
            )
            return
        rc, out, err = _git_cmd(["pull", "--ff-only", "origin", "main"], timeout=30)
        if rc == 0:
            # Drop modal git cache so post-pull state (no commits ahead, etc.)
            # is reflected immediately on the next render.
            _invalidate_update_modal_cache()
            # Syntax check via compile() — avoids interpreter version mismatch.
            # Shared with update.py CLI so both paths cover identical file set.
            bad = check_syntax_after_pull(_REPO_ROOT)
            if bad:
                _set_update_result(f"Updated but syntax errors in: {', '.join(bad)}")
            else:
                _set_update_result("Update complete. Restart monitor to apply.")
        else:
            _set_update_result(f"Update failed: {_sanitize(err or out or 'unknown error')}")
    except Exception as e:
        _set_update_result(f"Update error: {_sanitize(str(e))}")


def _apply_update_action():
    """Spawn background thread for update. Non-blocking."""
    global _update_thread
    _set_update_result("Updating...")
    _update_thread = threading.Thread(
        target=_apply_update_worker, name="update-apply", daemon=True
    )
    _update_thread.start()


def render_update_modal(cols, rows):
    """Render the update manager modal."""
    SW = cols
    buf = []
    buf.append(sep(SW))
    up_pad = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}UPDATE{R}{BG_BAR}{' ' * up_pad}{R}")
    buf.append(sep(SW))

    rls = _rls_snapshot()  # atomic coherent read
    rls_s = rls["status"]
    remote_ver = rls.get("remote_ver")

    buf.append(f"{C_GRN}CUR{R} {C_GRN}v{VERSION}{R}")
    if remote_ver:
        buf.append(f"{C_WHT}REM{R} {C_WHT}v{remote_ver}{R}")
    else:
        buf.append(f"{C_DIM}REM{R} {C_DIM}unknown{R}")

    # Last check freshness — show only after worker has actually run.
    # Using status gate (not "t > 0") because time.monotonic() may be small on
    # freshly-started processes where tests set t = monotonic() - 125 < 0.
    if rls_s is not None:
        age_s = max(0, int(time.monotonic() - rls.get("t", 0)))
        if age_s < 60:
            age_str = f"{age_s}s ago"
        elif age_s < SECONDS_1H:
            age_str = f"{age_s // 60}m ago"
        else:
            age_str = f"{age_s // SECONDS_1H}h ago"
        buf.append(f"{C_DIM}Checked {age_str}{R}")

    buf.append(f"{C_CYN}github.com/iM3SK/cc-aio-mon{R}")

    if rls_s == "update" and remote_ver:
        # Show new commits — cached so we don't spam git subprocess every 50ms tick
        commits = _cached_get_new_commits(remote_ver)
        if commits:
            buf.append("")
            buf.append(f"{C_WHT}{B}NEW COMMITS{R}")
            buf.append(sep(SW))
            for c_line in commits:
                buf.append(f"{C_DIM}{_sanitize(c_line)}{R}")

        # Changelog preview — cached
        cl = _cached_get_remote_changelog_preview(remote_ver)
        if cl:
            buf.append("")
            buf.append(f"{C_WHT}{B}CHANGELOG{R}")
            buf.append(sep(SW))
            for c_line in cl:
                buf.append(f"{C_DIM}{_sanitize(c_line)}{R}")

        # Safety warnings — cached (3 git subprocess calls inside)
        warns = _cached_update_checks()
        if warns:
            buf.append("")
            buf.append(f"{C_RED}{B}WARNINGS{R}")
            buf.append(sep(SW))
            for w in [_sanitize(x) for x in warns]:
                buf.append(f"{C_RED}{w}{R}")

        ur = _get_update_result()
        if ur:
            buf.append("")
            if "complete" in ur:
                buf.append(f"{C_GRN}{B}{ur}{R}")
            else:
                buf.append(f"{C_RED}{B}{ur}{R}")
            buf.append(sep(SW))
            buf.append(f"{C_DIM}press any key to close{R}")
        elif warns:
            buf.append(sep(SW))
            buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply (blocked by warnings above){R}")
            buf.append(f"{C_DIM}press any key to close{R}")
        else:
            buf.append(sep(SW))
            buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply{R}")
            buf.append(f"{C_DIM}press any key to close{R}")

    elif rls_s == "ok":
        buf.append(f"{C_GRN}{spin_rls()} Up to date{R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply (no update available){R}")
        buf.append(f"{C_DIM}press any key to close{R}")

    elif rls_s is None:
        buf.append(f"{C_DIM}{spin_rls()} Checking for updates...{R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply (checking...){R}")
        buf.append(f"{C_DIM}press any key to close{R}")

    else:
        buf.append(f"{C_DIM}Could not check for updates.{R}")
        if rls_s == "no_git":
            buf.append(f"{C_DIM}Git is not installed or not on PATH.{R}")
        elif rls_s == "timeout":
            buf.append(f"{C_DIM}Network timeout — check your connection.{R}")
        else:
            buf.append(f"{C_DIM}Unknown error during check.{R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[{R}{C_WHT}a{R}{C_DIM}] apply (check failed){R}")
        buf.append(f"{C_DIM}press any key to close{R}")

    _window_buf(buf, rows)
    return buf


# ---------------------------------------------------------------------------
# Token stats modal
# ---------------------------------------------------------------------------
_PERIOD_LABELS = {"all": "All Time", "7d": "Last 7 Days", "30d": "Last 30 Days"}
_PERIOD_CYCLE = ["all", "7d", "30d"]

_MODEL_ID_RE = re.compile(r"^claude-(opus|sonnet|haiku)-(\d+)-(\d+)")
# Match human-readable display names (e.g. "Opus 4.6 (1M context)") — used by render_picker
_MODEL_LABEL_RE = re.compile(r"(Opus|Sonnet|Haiku)\s+(\d+)\.(\d+)")
_LABEL_FAMILY_CODES = {"Opus": "OP", "Sonnet": "SO", "Haiku": "HA"}


def _model_code_from_label(label):
    """Parse display_name ('Opus 4.6 (1M context)') into ('OP', '4.6') tuple.

    Mirrors _model_code but operates on human-readable labels rather than
    model IDs — used by session picker, which sees display_name (not id).
    """
    mm = _MODEL_LABEL_RE.search(label or "")
    if mm:
        return (_LABEL_FAMILY_CODES[mm.group(1)], f"{mm.group(2)}.{mm.group(3)}")
    return (strip_context_suffix(label or "").strip(), "")


def _model_label(model_id):
    base = _model_base(model_id)
    entry = _MODELS.get(base)
    if entry:
        return entry["name"]
    m = _MODEL_ID_RE.match(base)
    if m:
        fam = m.group(1).capitalize()
        return f"{fam} {m.group(2)}.{m.group(3)}"
    return base or "?"


def _model_code(model_id):
    """Return (short_code, version) tuple for stats display."""
    base = _model_base(model_id)
    entry = _MODELS.get(base)
    if entry:
        return entry["code"]
    m = _MODEL_ID_RE.match(base)
    if m:
        short = {"opus": "OP", "sonnet": "SO", "haiku": "HA"}[m.group(1)]
        return (short, f"{m.group(2)}.{m.group(3)}")
    # Unknown model — sanitize raw input to prevent ANSI injection via transcript
    safe = _sanitize(base[:3]).upper() if base else ""
    return (safe or "?", "")


# Bar colors per model (consistent mapping)
_MODEL_COLORS = [C_CYN, C_GRN, C_YEL, C_ORN, C_RED]


def _total_tokens(m):
    """Total token volume for a model: input + output + cache_read + cache_write."""
    return m.get("input", 0) + m.get("output", 0) + m.get("cache_read", 0) + m.get("cache_write", 0)


_STATS_CACHE_PATH = pathlib.Path.home() / ".claude" / "stats-cache.json"
_STATS_CACHE_MAX_BYTES = 4 * 1024 * 1024  # 4 MiB cap — CC stats cache is tiny in practice


def _read_stats_cache():
    """Read ~/.claude/stats-cache.json. Returns (data_dict, mtime) or (None, 0).

    Validates schema version >= 1 and that top-level is a dict. Size-capped via
    safe_read. No exceptions propagate — missing/invalid cache is silently None.
    """
    try:
        st = _STATS_CACHE_PATH.stat()
    except OSError:
        return None, 0
    raw = safe_read(_STATS_CACHE_PATH, _STATS_CACHE_MAX_BYTES)
    if raw is None:
        return None, 0
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None, 0
    if not isinstance(obj, dict):
        return None, 0
    ver = obj.get("version")
    if not isinstance(ver, int) or ver < 1:
        return None, 0
    return obj, st.st_mtime


_HEATMAP_GLYPHS = " ▁▂▃▄▅▆▇█"  # 9 levels including blank for zero


def _append_lifetime_block(buf, rows, width):
    """Append the LIFETIME ACTIVITY block to buf when the stats cache exists.

    The whole block (incl. DAILY) is always emitted; the modal is scrollable
    (_window_buf), so nothing is dropped to "fit" the terminal height — that
    rows-gating used to make LIFETIME/DAILY unreachable on short terminals.
    Cache miss skips silently. ``rows`` is kept for signature compatibility.
    """
    cache, _ = _read_stats_cache()
    if not cache:
        return

    cd = cache.get("lastComputedDate") or "?"
    la_title = f"LIFETIME  cached {cd}"
    la_pad = max(0, width - len(la_title))

    block = []
    block.append(sep(width))
    block.append(f"{BG_BAR}{C_WHT}{B}{la_title}{R}{BG_BAR}{' ' * la_pad}{R}")
    block.append(sep(width))

    tot_ses = int(_num(cache.get("totalSessions", 0)))
    tot_msg = int(_num(cache.get("totalMessages", 0)))
    tot_tlc = sum(int(_num(d.get("toolCallCount", 0)))
                  for d in (cache.get("dailyActivity") or [])
                  if isinstance(d, dict))
    block.append(
        f"{C_WHT}SES{R} {C_WHT}{tot_ses:,}{R} "
        f"{C_WHT}MSG{R} {C_WHT}{tot_msg:,}{R} "
        f"{C_WHT}TLC{R} {C_WHT}{tot_tlc:,}{R}"
    )

    first = cache.get("firstSessionDate") or ""
    first_short = first[:10] if isinstance(first, str) else ""
    longest = cache.get("longestSession") or {}
    ls_dur = int(_num(longest.get("duration", 0))) if isinstance(longest, dict) else 0
    block.append(
        f"{C_WHT}1ST{R} {C_WHT}{first_short or '--'}{R} "
        f"{C_WHT}LSS{R} {C_WHT}{f_dur(ls_dur)}{R}"
    )

    heat = _render_hour_heatmap(cache.get("hourCounts") or {})
    # Labels align with heatmap glyph positions 0,6,12,18 (after 4-char prefix).
    block.append(f"{C_DIM}HRS{R} {C_DIM}0     6     12    18{R}")
    block.append(f"{C_DIM}ACT{R} {C_CYN}{heat}{R}")

    daily = cache.get("dailyActivity") or []
    items = [d for d in daily if isinstance(d, dict)] if isinstance(daily, list) else []
    if items:
        items.sort(key=lambda d: d.get("date") or "", reverse=True)
        block.append(sep(width))
        block.append(f"{C_DIM}DAILY{R}")
        for d in items[:5]:
            date_s = (d.get("date") or "")[5:]  # MM-DD
            ses = int(_num(d.get("sessionCount", 0)))
            msg = int(_num(d.get("messageCount", 0)))
            tlc = int(_num(d.get("toolCallCount", 0)))
            block.append(
                f"{date_s} {C_DIM}SES{R} {C_WHT}{ses:,}{R} "
                f"{C_DIM}MSG{R} {C_WHT}{msg:,}{R} "
                f"{C_DIM}TLC{R} {C_WHT}{tlc:,}{R}"
            )

    buf.extend(block)


def _render_hour_heatmap(hour_counts):
    """Map dict[str_hour -> count] to a 24-char heatmap string.

    Empty/all-zero input renders as 24 spaces. Each hour quantized to one of 9
    levels by max-normalized fraction. Hours outside 0..23 are ignored.
    """
    counts = [0] * 24
    if isinstance(hour_counts, dict):
        for k, v in hour_counts.items():
            try:
                h = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= h < 24:
                try:
                    counts[h] = max(0, int(v))
                except (TypeError, ValueError):
                    counts[h] = 0
    peak = max(counts)
    if peak <= 0:
        return " " * 24
    last = len(_HEATMAP_GLYPHS) - 1
    out = []
    for c in counts:
        idx = 0 if c <= 0 else max(1, min(last, round(c / peak * last)))
        out.append(_HEATMAP_GLYPHS[idx])
    return "".join(out)


_stats_scan_thread = None
_stats_scan_lock = threading.Lock()


def _stats_refresh_async(period):
    """Kick a background scan_transcript_stats when its cache is stale, so the
    ~0.6s read+parse of all transcripts never blocks the render/input thread
    (mirrors _subagents_refresh_async / _rls_check_worker / the pulse worker).
    render_stats reads the last _usage_cache result and never blocks."""
    global _stats_scan_thread
    cached = _usage_cache.get(period)
    if cached and time.monotonic() - cached["t"] < 30.0:
        return  # fresh enough
    with _stats_scan_lock:
        if _stats_scan_thread is not None and _stats_scan_thread.is_alive():
            return
        t = threading.Thread(target=scan_transcript_stats, args=(period,),
                             name="stats-scan", daemon=True)
        _stats_scan_thread = t
        t.start()


def render_stats(cols, rows, period="all"):
    SW = cols
    buf = []
    buf.append(sep(SW))
    title = f"TOKEN STATS  {_PERIOD_LABELS.get(period, period)}"
    tp = max(0, SW - len(title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{title}{R}{BG_BAR}{' ' * tp}{R}")
    buf.append(sep(SW))

    cached = _usage_cache.get(period)
    if cached is None:
        # First open for this period: one synchronous scan so the modal shows
        # data immediately (and fills the cache). Every later open reads the
        # cache and refreshes it off-thread, so the ~0.6s read never blocks
        # again — that repeated freeze was the Critical finding.
        models, overview = scan_transcript_stats(period)
    else:
        _stats_refresh_async(period)  # refresh stale cache off the render thread
        models, overview = cached["models"], cached["overview"]
    if not models:
        buf.append(f"{C_DIM}No transcript data found in ~/.claude/projects/{R}")
        buf.append(f"{C_DIM}(stats appear after at least one CC session){R}")
        _append_lifetime_block(buf, rows, SW)
        buf.append(sep(SW))
        buf.append(f"{C_DIM}[{R}{C_WHT}1{R}{C_DIM}]all [{R}{C_WHT}2{R}{C_DIM}]7d [{R}{C_WHT}3{R}{C_DIM}]30d{R}")
        buf.append(f"{C_DIM}press any key to close{R}")
        _window_buf(buf, rows)
        return buf

    # -- Overview section --
    n_sessions = overview["sessions"]
    n_days = len(overview["active_days"])
    longest_ms = overview["longest_dur_ms"]
    daily = overview["daily_tokens"]
    current_streak, longest_streak = _calc_streaks(overview["active_days"])

    # Most active day
    most_active = "--"
    if daily:
        top_day = max(daily, key=daily.get)
        most_active = f"{top_day} ({f_tok(daily[top_day])})"

    trunc_tag = (
        f"  {C_YEL}({MAX_TRANSCRIPT_FILES} file limit){R}"
        if overview.get("truncated") else ""
    )
    buf.append(
        f"{C_WHT}SES{R} {C_WHT}{n_sessions}{R} "
        f"{C_WHT}DAY{R} {C_WHT}{n_days}{R} "
        f"{C_WHT}STK{R} {C_WHT}{current_streak}d{R}"
        f"{C_DIM}/{longest_streak}d{R}{trunc_tag}"
    )
    buf.append(f"{C_WHT}LSS{R} {C_WHT}{f_dur(longest_ms)}{R} {C_WHT}TOP{R} {C_WHT}{most_active}{R}")

    # -- Models section --
    total_all = sum(_total_tokens(m) for m in models.values())
    sorted_models = sorted(
        models.items(), key=lambda kv: _total_tokens(kv[1]), reverse=True
    )

    buf.append(sep(SW))
    mp = max(0, SW - 6)
    buf.append(f"{BG_BAR}{C_WHT}{B}MODELS{R}{BG_BAR}{' ' * mp}{R}")
    buf.append(sep(SW))
    for i, (mid, st) in enumerate(sorted_models):
        color = _MODEL_COLORS[i % len(_MODEL_COLORS)]
        code, ver = _model_code(mid)
        total_m = _total_tokens(st)
        pct = total_m / total_all * 100 if total_all else 0
        ver_tag = f" {C_DIM}{ver}{R}" if ver else ""
        buf.append(f"{color}{B}{code}{R}{ver_tag} {mkbar(pct, color)}")
        buf.append(
            f"    {C_DIM}INP:{R} {color}{f_tok(st['input'])}{R}"
            f" {C_DIM}OUT:{R} {color}{f_tok(st['output'])}{R}"
            f" {C_DIM}CLS:{R} {color}{st['calls']:,}{R}"
        )
        if st.get("cache_read", 0) or st.get("cache_write", 0):
            buf.append(
                f"    {C_DIM}CRD:{R} {color}{f_tok(st.get('cache_read', 0))}{R}"
                f" {C_DIM}CWR:{R} {color}{f_tok(st.get('cache_write', 0))}{R}"
            )
        buf.append(sep(SW))

    # Totals
    total_in = sum(m["input"] for m in models.values())
    total_out = sum(m["output"] for m in models.values())
    total_cr = sum(m.get("cache_read", 0) for m in models.values())
    total_cw = sum(m.get("cache_write", 0) for m in models.values())
    total_calls = sum(m["calls"] for m in models.values())
    buf.append(
        f"{C_WHT}{B}ALL{R}"
        f" {C_DIM}INP:{R} {C_WHT}{f_tok(total_in)}{R}"
        f" {C_DIM}OUT:{R} {C_WHT}{f_tok(total_out)}{R}"
        f" {C_DIM}CLS:{R} {C_WHT}{total_calls:,}{R}"
    )
    if total_cr or total_cw:
        buf.append(
            f"    {C_DIM}CRD:{R} {C_WHT}{f_tok(total_cr)}{R}"
            f" {C_DIM}CWR:{R} {C_WHT}{f_tok(total_cw)}{R}"
        )

    _append_lifetime_block(buf, rows, SW)

    buf.append(sep(SW))
    buf.append(f"{C_DIM}[{R}{C_WHT}1{R}{C_DIM}]all [{R}{C_WHT}2{R}{C_DIM}]7d [{R}{C_WHT}3{R}{C_DIM}]30d{R}")
    buf.append(f"{C_DIM}press any key to close{R}")

    _window_buf(buf, rows)
    return buf


# ---------------------------------------------------------------------------
# Agents modal — live subagent / Workflow fan-out for the watched session.
#
# Task subagents and Workflow agents each write their own transcript under
#   <claude_projects>/<proj>/<session>/subagents/agent-*.jsonl   (+ workflows/)
# We derive that dir from the session's transcript_path (reusing the same
# containment hardening as the title/usage scanners), sum each agent's token
# usage, and flag "active" by recent mtime. The scan is lazy — invoked only
# while the modal is open — and TTL-cached, so the default dashboard never
# pays for it. On a cache miss (every TTL while the modal is open) the read
# still runs on the render thread; the file-size + count caps bound that cost.
# ---------------------------------------------------------------------------
_SUBAGENT_SCAN_CAP = 256              # max agent files per scan (DoS guard)
_SUBAGENT_FILE_MAX = 8 * 1024 * 1024  # skip token-sum read above this size
_SUBAGENT_ACTIVE_WINDOW = 30.0        # mtime within N seconds == "active"
_SUBAGENTS_TTL = 2.0
_subagents_cache = {}                 # {dir_str: {"t": monotonic, "data": {...}}}
_SUBAGENTS_CACHE_MAX = 8


def _subagents_dir_for(transcript_path):
    """Derive the subagents/ dir for a session from its transcript path.

    transcript = <root>/<proj>/<session>.jsonl  ->  <root>/<proj>/<session>/subagents/
    Reuses _safe_transcript_path containment (regular file inside the projects
    root, no symlink escapes). Returns the dir Path or None.
    """
    path = _safe_transcript_path(transcript_path)
    if path is None:
        return None
    d = path.parent / path.stem / "subagents"
    # is_safe_dir = lstat + S_ISDIR + Windows reparse-point check. A plain
    # S_ISLNK test misses NTFS junctions, which would let a junction point
    # the subagent scan outside ~/.claude/projects.
    if not is_safe_dir(d):
        return None
    return d


def scan_subagents(transcript_path, ttl=_SUBAGENTS_TTL):
    """Return a fan-out summary for the session's subagents, or None.

    {"total": int, "active": int, "total_tokens": int,
     "agents": [{"id": str, "tokens": int, "tool": str|None, "active": bool,
                 "mtime": float, "too_large": bool}, ...]}

    Lazy + TTL-cached: only invoked while the agents modal is open, and re-reads
    at most every ``ttl`` seconds so an open modal doesn't re-parse N files each
    render tick.
    """
    d = _subagents_dir_for(transcript_path)
    if d is None:
        return None
    key = str(d)
    mono = time.monotonic()
    cached = _subagents_cache.get(key)
    if cached and mono - cached["t"] < ttl:
        return cached["data"]

    now = time.time()
    files = []
    try:
        files.extend(sorted(d.glob("agent-*.jsonl")))
        wf = d / "workflows"
        if is_safe_dir(wf):  # rejects symlinks AND Windows junctions
            files.extend(f for f in wf.glob("**/*.jsonl") if f.name != "journal.jsonl")
    except OSError:
        return None
    files = files[:_SUBAGENT_SCAN_CAP]

    agents = []
    total_tok = 0
    active = 0
    for f in files:
        try:
            lst = f.lstat()
        except OSError:
            continue
        # Defense-in-depth: skip a symlinked or non-regular leaf so a planted
        # symlink can't redirect the read outside the projects root, and a FIFO/
        # special file can't block the (synchronous) scan. Mirrors the
        # _scan_ai_title / _safe_transcript_path hardening for the dir leaf.
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
            continue
        mt = lst.st_mtime
        tok = 0
        last_tool = None
        # Bounded, TOCTOU-safe read (never exceeds the cap even if the file
        # grows between lstat and read). None == unreadable or over cap.
        raw = safe_read(f, _SUBAGENT_FILE_MAX)
        too_large = raw is None
        if raw is not None:
            for line in raw.decode("utf-8", errors="replace").splitlines():
                try:
                    o = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(o, dict):
                    continue  # valid JSON but not an object (e.g. bare array)
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                u = m.get("usage")
                if isinstance(u, dict):
                    tok += (_num(u.get("input_tokens"), 0) + _num(u.get("output_tokens"), 0)
                            + _num(u.get("cache_creation_input_tokens"), 0)
                            + _num(u.get("cache_read_input_tokens"), 0))
                for c in (m.get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        last_tool = c.get("name")
        is_active = (now - mt) < _SUBAGENT_ACTIVE_WINDOW
        if is_active:
            active += 1
        total_tok += tok
        aid = f.stem
        if aid.startswith("agent-"):
            aid = aid[6:]
        agents.append({"id": aid[:12], "tokens": int(tok), "tool": last_tool,
                       "active": is_active, "mtime": mt, "too_large": too_large})

    agents.sort(key=lambda a: a["mtime"], reverse=True)
    # total counts agents we actually materialised (stat/symlink-rejected files
    # drop out), so the header and "+N more" line match the rendered list.
    data = {"total": len(agents), "active": active,
            "total_tokens": total_tok, "agents": agents}

    _subagents_cache[key] = {"t": mono, "data": data}
    if len(_subagents_cache) > _SUBAGENTS_CACHE_MAX:
        oldest = min(_subagents_cache, key=lambda kk: _subagents_cache[kk]["t"])
        if oldest != key:
            _subagents_cache.pop(oldest, None)
    return data


_subagents_scan_thread = None
_subagents_scan_lock = threading.Lock()


def _subagents_refresh_async(transcript_path, d=None):
    """Kick a background scan when the cache is stale and none is in flight.

    Keeps the read+parse of up to 256 agent transcripts OFF the render thread so
    the modal doesn't stutter on large fan-outs (the synchronous scan blocked
    the 20Hz loop for ~0.3s every TTL). The worker writes _subagents_cache;
    render_agents reads the last cached result without ever blocking. Mirrors
    the _rls_check_worker / pulse worker pattern.

    ``d`` is the caller's already-resolved subagents dir; pass it to skip a
    redundant per-tick lstat (render_agents resolves it for its own use).
    """
    global _subagents_scan_thread
    if d is None:
        d = _subagents_dir_for(transcript_path)
    if d is None:
        return
    cached = _subagents_cache.get(str(d))
    if cached and time.monotonic() - cached["t"] < _SUBAGENTS_TTL:
        return  # fresh enough — no scan needed
    with _subagents_scan_lock:
        if _subagents_scan_thread is not None and _subagents_scan_thread.is_alive():
            return
        t = threading.Thread(
            target=scan_subagents, args=(transcript_path,),
            kwargs={"ttl": _SUBAGENTS_TTL}, name="subagents-scan", daemon=True)
        _subagents_scan_thread = t
        t.start()


# Compact tool labels for the agents modal — keeps each per-agent line short and
# consistent with the monitor's 3-4 letter uppercase codes (APR/CHR/CTX...).
# Built-ins map to a curated code; anything else (incl. long mcp__server__method
# names) falls back to the first 4 letters of its method, uppercased.
_TOOL_ABBR = {
    "Read": "READ", "Write": "WRIT", "Edit": "EDIT", "MultiEdit": "MEDT",
    "NotebookEdit": "NBED", "Bash": "BASH", "BashOutput": "BOUT",
    "KillShell": "KILL", "Glob": "GLOB", "Grep": "GREP", "Task": "TASK",
    "Agent": "AGNT", "WebFetch": "WFCH", "WebSearch": "WSCH",
    "TodoWrite": "TODO", "ExitPlanMode": "PLAN", "Skill": "SKIL",
    "AskUserQuestion": "ASK", "SlashCommand": "SLSH",
}


def _tool_abbr(name):
    """Short uppercase code for an agent's last tool. Known built-ins use a
    curated label; an MCP tool (mcp__server__method) uses the first 4 letters of
    its method; anything else its first 4 letters. None -> '--'."""
    if not name:
        return "--"
    if name in _TOOL_ABBR:
        return _TOOL_ABBR[name]
    base = name.split("__")[-1] if name.startswith("mcp__") else name
    return base[:4].upper() or "--"


def render_agents(data, cols, rows, active_only=False):
    """Modal: live subagent / Workflow fan-out for the watched session.

    ``active_only`` hides idle (○) agents so a fresh fan-out isn't buried under
    history — a non-destructive "clear board" (nothing on disk is touched).
    """
    SW = cols
    buf = []
    buf.append(sep(SW))
    title = "AGENTS  fan-out" + ("  [active]" if active_only else "")
    pad = max(0, SW - len(title))
    buf.append(f"{BG_BAR}{C_WHT}{B}{title}{R}{BG_BAR}{' ' * pad}{R}")
    buf.append(sep(SW))

    tpath = (data or {}).get("transcript_path")
    d = _subagents_dir_for(tpath)
    info = None
    if d is not None:
        # Pass the dir we already resolved so the refresh helper doesn't
        # re-lstat it every render tick while the modal is open.
        _subagents_refresh_async(tpath, d=d)  # off-thread scan; render never blocks
        cached = _subagents_cache.get(str(d))
        info = cached["data"] if cached else None
        if info is None:
            # Scan in flight, nothing cached yet (first open this TTL).
            buf.append(f"{C_DIM}Scanning subagents…{R}")
            buf.append(sep(SW))
            buf.append(f"{C_DIM}press any key to close{R}")
            _window_buf(buf, rows)
            return buf
    if not info or not info["total"]:
        buf.append(f"{C_DIM}No subagents for this session.{R}")
        buf.append(f"{C_DIM}(populated when Task or Workflow agents run){R}")
        buf.append(sep(SW))
        buf.append(f"{C_DIM}press any key to close{R}")
        _window_buf(buf, rows)
        return buf

    buf.append(
        f"{C_GRN}{B}{info['active']}{R}{C_DIM} active{R} "
        f"{C_DIM}/{R} {C_WHT}{B}{info['total']}{R}{C_DIM} total{R}    "
        f"{C_DIM}TOK{R} {C_WHT}{f_tok(info['total_tokens'])}{R}"
    )
    buf.append(sep(SW))

    shown = [a for a in info["agents"] if a["active"]] if active_only else info["agents"]
    if active_only and not shown:
        idle = info["total"] - info["active"]
        buf.append(f"{C_DIM}No active agents ({idle} idle hidden).{R}")
    # Emit every agent; _window_buf provides the scrollable window (the old
    # rows-8 cap + "+N more" is gone — the list is now scrollable instead).
    for a in shown:
        dot = f"{C_GRN}●{R}" if a["active"] else f"{C_DIM}○{R}"
        tool = _tool_abbr(a["tool"])
        # Fixed-width columns so the token / tool columns line up regardless of
        # how wide each agent's formatted token count is (pad the plain text
        # before wrapping it in ANSI, or the escape bytes would skew alignment).
        aid = a["id"][:12].ljust(12)
        # A too-large transcript is skipped for token-summing — show "  >cap"
        # instead of a misleading 0 so the count isn't silently undercounted.
        tok_str = f"{'>cap' if a.get('too_large') else f_tok(a['tokens']):>7}"
        line = (f"{dot} {C_DIM}{aid}{R}  "
                f"{C_WHT}{tok_str}{R} {C_DIM}tok{R}   {C_CYN}{tool}{R}")
        buf.append(truncate(line, SW))

    buf.append(sep(SW))
    filt = f"{C_WHT}all{R}{C_DIM}/active" if not active_only else f"{C_DIM}all/{R}{C_WHT}active{R}"
    buf.append(f"{C_DIM}[{R}{C_WHT}f{R}{C_DIM}] {filt}{C_DIM}   ·   press any key to close{R}")
    _window_buf(buf, rows)
    return buf


# ---------------------------------------------------------------------------
# Session picker
# ---------------------------------------------------------------------------
def _picker_order(sessions):
    """Single source of truth for picker ordering: active sessions first, then
    stale, capped at 9 (the keyboard 1-9 limit). render_picker and main()'s
    digit-selection MUST share this so position N on screen maps to the Nth
    session a keypress selects — two hand-synced copies risked an index->sid
    desync where [3] picked a different session than the one shown at slot 3."""
    return sorted(sessions, key=lambda s: s["stale"])[:9]


def render_picker(sessions, cols, rows):
    W = cols
    buf = []
    buf.append(sep(W))
    hp = max(0, W - len(f"CC AIO MON {VERSION}"))
    buf.append(f"{BG_BAR}{C_WHT}{B}CC AIO MON {VERSION}{R}{BG_BAR}{' ' * hp}{R}")
    buf.append(sep(W))

    if not sessions:
        buf.append(f"{C_DIM}Waiting for Claude Code session...{R}")
        buf.append(f"{C_DIM}Start a session, then come back here.{R}")
    else:
        buf.append(f"{C_WHT}{B}SESSIONS{R}")
        buf.append(sep(W))
        # Sort: active first, then stale. Limit to 9 (keyboard limit).
        shown = _picker_order(sessions)
        for i, s in enumerate(shown):
            tag = f"{C_RED}stale{R}" if s["stale"] else f"{C_GRN}live{R}"
            nm = s["session_name"] or s.get("ai_title") or s["id"][:8]
            # Short model: "Opus 4.6 (1M context)" → "OP 4.6" via single source of truth
            code, ver = _model_code_from_label(s["model"])
            model_short = f"{code} {ver}".strip() if ver else code
            line = f"{C_WHT}[{i + 1}]{R} {B}{nm}{R} {C_DIM}{model_short}{R} {tag}"
            buf.append(truncate(line, W))
        if len(sessions) > 9:
            buf.append(f"{C_DIM}+{len(sessions) - 9} more{R}")

    buf.append(sep(W))
    buf.append(f"{C_DIM}[{R}{C_WHT}1-9{R}{C_DIM}] select{R}")
    buf.append(f"{C_DIM}[{R}{C_WHT}q{R}{C_DIM}] quit{R}")

    _window_buf(buf, rows)
    return buf


# ---------------------------------------------------------------------------
# Screen flush
# ---------------------------------------------------------------------------
def flush(buf, cols):
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
def _install_crash_logger():
    """Write uncaught exceptions to $TMPDIR/claude-aio-monitor/monitor-crash.log.

    Alt screen buffer (\\033[?1049h) captures any Python traceback and the atexit
    cleanup (\\033[?1049l) wipes it on exit — so without this hook, a crash looks
    to the user like 'monitor just quit silently'. The crash log survives outside
    the alt buffer for post-mortem diagnosis.
    """

    def excepthook(exc_type, exc_value, tb):
        try:
            if ensure_data_dir(DATA_DIR):
                log_path = DATA_DIR / "monitor-crash.log"
                # Always rotate so two crashes in quick succession don't
                # silently overwrite each other (open("w") below truncates).
                # Previous crash → .log.1, current crash → .log.
                rotate_crash_log(log_path, always=True)
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"monitor v{VERSION} crashed at {time.ctime()}\n")
                    f.write(f"platform: {sys.platform}, python: {sys.version}\n")
                    f.write(f"encoding: stdout={sys.stdout.encoding}, fs={sys.getfilesystemencoding()}\n")
                    f.write("\n---\n")
                    traceback.print_exception(exc_type, exc_value, tb, file=f)
        except Exception:
            pass  # never break exit on diag failure
        # Defer to default handler so traceback also goes to stderr (post-cleanup)
        sys.__excepthook__(exc_type, exc_value, tb)

    sys.excepthook = excepthook


# Module-level handle for the singleton lock. MUST stay alive for the process
# lifetime — Python would otherwise GC the file object and release the lock.
_SINGLETON_LOCK_HANDLE = None


def main():
    _install_crash_logger()

    # SIGPIPE: silent exit when --list output is piped to head/less on Unix
    # (matches statusline.py + update.py for consistency).
    # NOTE: `signal` is imported at module level (line 23); do NOT add a local
    # `import signal` here — that turns `signal` into a function-scope local
    # and breaks `signal.signal(SIGTERM, ...)` later in main() on Windows.
    if sys.platform != "win32" and hasattr(signal, "SIGPIPE"):
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Claude AIO Monitor")
    parser.add_argument("--session", help="Session ID")
    parser.add_argument("--list", action="store_true", help="List sessions")
    parser.add_argument("--refresh", type=int, default=500, help="Refresh ms")
    args = parser.parse_args()

    args.refresh = max(100, min(60000, args.refresh))

    ensure_utf8_stdout()
    # NEW-003: --list emits diacritics from session_name / ai_title and
    # needs Windows console CP 65001 even though it skips the full TUI
    # _setup_term. Restore is handled by atexit cleanup only when set
    # for the interactive path; for the one-shot --list we deliberately
    # leave the console in UTF-8 mode since the process exits right
    # after the listing — no chance for follow-up commands to be
    # affected within this process.
    _set_console_utf8()

    if args.list:
        for s in list_sessions():
            tag = "(stale)" if s["stale"] else "(live)"
            nm = s["session_name"] or s.get("ai_title") or "--"
            print(f"  {s['id'][:16]}  {s['model']:>8}  {nm}  {s['cwd']}  {tag}")
        return

    # Singleton lock — interactive monitor only. Two concurrent dashboards
    # would race on snapshot polling and corrupt the crash log; --list is
    # exempt because it is a one-shot non-interactive read.
    global _SINGLETON_LOCK_HANDLE, _modal_scroll
    if ensure_data_dir(DATA_DIR):
        _SINGLETON_LOCK_HANDLE = acquire_singleton_lock(DATA_DIR / "monitor.lock")
        if _SINGLETON_LOCK_HANDLE is None:
            sys.exit(
                "Error: another monitor.py instance is already running.\n"
                f"Lock file: {DATA_DIR / 'monitor.lock'} (inspect for PID)\n"
                "Close the other instance, or delete the lock file if it is stale."
            )
    else:
        # FILE-IPC contract: the interactive monitor must hold the singleton
        # lock — running unlocked would race a second instance on snapshot
        # polling and corrupt the crash log. No usable data dir also means no
        # snapshots to render, so fail fast instead of degrading silently.
        sys.exit(
            f"Error: data directory unusable: {DATA_DIR}\n"
            "Check permissions/ownership — the monitor needs it for IPC "
            "snapshots and its singleton lock."
        )

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
    sys.stdout.write(ALT_ON + ALT_SCROLL_ON + HIDE_CUR + CLR)
    sys.stdout.flush()

    def cleanup(*_args):
        # Give an in-flight self-update worker a bounded moment to finish its
        # post-pull syntax check before teardown (M-cross-1). The q-gate covers
        # interactive 'q'; this backstops signals and KeyboardInterrupt, which
        # bypass it.
        _join_update_worker()
        _restore_term()
        sys.stdout.write(SHOW_CUR + ALT_SCROLL_OFF + ALT_OFF)
        sys.stdout.flush()

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    sid = args.session
    if sid and not _SID_RE.match(sid):
        # sid failed regex — could contain ANSI escapes / control chars.
        # Sanitize + length-cap before echoing to terminal.
        print(f"Invalid session ID: {_sanitize(sid)[:64]}")
        return
    show_legend = False
    show_menu = False
    show_cost = False
    show_stats = None  # None=off, "all"/"7d"/"30d"=active period
    force_picker = False
    show_update = False
    show_pulse = False
    show_agents = False
    agents_active_only = False
    _modal_scroll = 0
    prev_modal_sig = None  # resets scroll offset when the active modal changes
    # Opt-out: CC_AIO_MON_NO_PULSE=1 disables the background Anthropic Pulse worker.
    # Mirrors CC_AIO_MON_NO_UPDATE_CHECK=1 pattern for the release checker.
    if os.environ.get("CC_AIO_MON_NO_PULSE") != "1":
        pulse.start_pulse_worker()
    _set_update_result(None)
    _render_errors = 0
    last_mt = 0
    last_seen = 0  # monotonic timestamp of last successful data load
    last_data = None
    last_size = (0, 0)
    last_hist_mt = 0
    last_hist = []
    data_interval = args.refresh / 1000
    tick = 0.05  # 50ms tick for responsive resize
    last_data_load = 0.0  # monotonic timestamp of last data file read

    try:
        while True:
            cols, rows = shutil.get_terminal_size((80, 24))
            size_changed = (cols, rows) != last_size
            last_size = (cols, rows)
            now_mono = time.monotonic()
            since_data = now_mono - last_data_load

            # Always poll keyboard
            k = poll_key()
            if k == "q":
                if _update_thread is not None and _update_thread.is_alive():
                    # Self-update worker is mid git-pull / post-pull syntax
                    # check: refuse to quit so we don't kill the daemon and
                    # leave the repo fast-forwarded but never integrity-checked
                    # (M-cross-1). Falls through to a normal render tick; quit
                    # works again once the worker finishes (a few seconds).
                    pass
                else:
                    break
            # ── Scroll the open modal (arrows / Page / j / k) — must precede
            #    the modal close handlers so a scroll key doesn't dismiss it ──
            elif k in _SCROLL_KEYS and (
                show_menu or show_cost or show_legend or show_agents
                or show_pulse or show_update or show_stats is not None
            ):
                _modal_scroll = _apply_scroll(_modal_scroll, k, rows)
                # Coalesce the rest of a wheel/key burst into THIS frame.
                # poll_key otherwise yields one sequence per 50ms loop, so a
                # fast wheel spin (3-15 notches queued, often arriving split on
                # SSH/tmux) scrolled one line per tick and felt stuck. Drain
                # within a short grace window, briefly waiting for in-transit
                # bytes instead of bailing on the first None.
                drain_deadline = time.monotonic() + 0.05
                drained = 0
                while drained < 512 and time.monotonic() < drain_deadline:
                    k2 = poll_key()
                    if k2 in _SCROLL_KEYS:
                        _modal_scroll = _apply_scroll(_modal_scroll, k2, rows)
                        drained += 1
                        drain_deadline = time.monotonic() + 0.02  # extend per notch
                    elif k2 is None:
                        time.sleep(0.001)  # let a split sequence finish arriving
                    else:
                        _key_pushback[0] = k2  # real non-scroll key — next poll_key returns it
                        break
            # ── Modal-specific handlers first (priority) ──
            elif show_update and k == "a" and _get_update_result() is None:
                _apply_update_action()
            elif show_update and k is not None:
                show_update = False
                _set_update_result(None)
            elif show_stats is not None and k in ("1", "2", "3"):
                show_stats = _PERIOD_CYCLE[int(k) - 1]
            elif show_stats is not None and k is not None:
                show_stats = None
            elif show_menu and k is not None:
                # Menu modal: dispatch key or close
                if k == "r":
                    last_mt = 0
                    last_seen = time.monotonic()
                elif k == "s":
                    sid = None
                    force_picker = True
                    last_data = None
                    last_mt = 0
                    last_seen = 0
                    last_hist_mt = 0
                    last_hist = []
                elif k == "l":
                    show_legend = True
                elif k == "t":
                    show_stats = "all"
                elif k == "u":
                    show_update = True
                elif k == "c":
                    show_cost = True
                elif k == "p":
                    show_pulse = True
                elif k == "a":
                    show_agents = True
                show_menu = False
            elif show_cost and k is not None:
                show_cost = False
            elif show_pulse and k is not None:
                show_pulse = False
            elif show_agents and k == "f":
                # In-modal filter toggle: hide idle agents ("clear board").
                agents_active_only = not agents_active_only
            elif show_agents and k is not None:
                show_agents = False
            elif show_legend and k is not None:
                show_legend = False
            # ── Global handlers ──
            elif k == "r":
                last_mt = 0
                last_seen = time.monotonic()
            elif k == "s":
                sid = None
                force_picker = True
                last_data = None
                last_mt = 0
                last_seen = 0
                last_hist_mt = 0
                last_hist = []
            elif k == "m":
                show_menu = not show_menu
                show_legend = False
                show_stats = None
                show_update = False
                _set_update_result(None)
            elif k == "l":
                show_legend = not show_legend
                show_menu = False
                show_stats = None
                show_update = False
                _set_update_result(None)
            elif k == "t":
                if show_stats is not None:
                    show_stats = None
                else:
                    show_stats = "all"
                    show_legend = False
                    show_update = False
                    _set_update_result(None)
            elif k == "c":
                show_cost = not show_cost
                show_menu = False
                show_legend = False
                show_stats = None
                show_update = False
                _set_update_result(None)
            elif k == "u":
                show_update = not show_update
                if not show_update:
                    _set_update_result(None)
                show_legend = False
                show_stats = None
                show_pulse = False
            elif k == "p":
                show_pulse = not show_pulse
                show_menu = False
                show_legend = False
                show_stats = None
                show_cost = False
                show_update = False
                _set_update_result(None)
            elif k == "a":
                show_agents = not show_agents
                show_menu = False
                show_legend = False
                show_stats = None
                show_cost = False
                show_pulse = False
                show_update = False
                _set_update_result(None)

            # Reset scroll whenever the active modal changes (open / switch /
            # close) so a new modal always starts at the top.
            modal_sig = (show_menu, show_cost, show_legend, show_agents,
                         show_pulse, show_update, show_stats)
            if modal_sig != prev_modal_sig:
                _modal_scroll = 0
                prev_modal_sig = modal_sig

            # Render every tick when we have data (for spinner), reload data on interval
            # An open modal must always re-render (scroll responsiveness) even
            # when there is no live session driving the gate.
            any_modal_open = (show_menu or show_cost or show_legend or show_agents
                              or show_pulse or show_update or show_stats is not None)
            need_render = (size_changed or last_data is not None
                           or since_data >= data_interval or any_modal_open)
            if not need_render:
                time.sleep(tick)
                continue

            # Update modal
            if show_update:
                try:
                    flush(render_update_modal(cols, rows), cols)
                except (TypeError, ValueError, KeyError, OSError):
                    pass
                time.sleep(tick)
                continue

            # Pulse modal (no session required)
            if show_pulse:
                try:
                    flush(render_pulse_modal(cols, rows), cols)
                except (TypeError, ValueError, KeyError, OSError):
                    pass
                time.sleep(tick)
                continue

            # Stats modal can render without a session (global data)
            if show_stats is not None:
                try:
                    flush(render_stats(cols, rows, show_stats), cols)
                except (TypeError, ValueError, KeyError, OSError):
                    pass
                time.sleep(tick)
                continue

            # Auto-detect / pick session — cached so the 20Hz picker loop
            # doesn't rescan DATA_DIR every tick (audit P1-3)
            if sid is None:
                sessions = cached_list_sessions()
                active = [s for s in sessions if not s["stale"]]
                if len(active) == 1 and len(sessions) == 1 and not force_picker:
                    sid = active[0]["id"]
                elif not sessions:
                    flush(render_picker([], cols, rows), cols)
                    time.sleep(tick)
                    continue
                else:
                    sorted_s = _picker_order(sessions)
                    flush(render_picker(sessions, cols, rows), cols)
                    if k and k.isdigit():
                        idx = int(k) - 1
                        if 0 <= idx < len(sorted_s):
                            sid = sorted_s[idx]["id"]
                            force_picker = False
                            last_seen = time.monotonic()
                    time.sleep(tick)
                    continue

            # Load state (only on data interval, not resize)
            if since_data >= data_interval:
                last_data_load = now_mono
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
            # A-P2-1: release-check trigger lives in the event loop, not in
            # render_frame. _rls_maybe_check is rate-limited internally
            # (_RLS_TTL) so per-tick invocation here is cheap.
            _rls_maybe_check()
            try:
                flush(
                    render_frame(
                        last_data, last_hist, cols, rows,
                        show_legend, show_menu, show_cost, stale=is_stale,
                        show_agents=show_agents, agents_active_only=agents_active_only,
                    ),
                    cols,
                )
            except (TypeError, ValueError, KeyError, AttributeError, ZeroDivisionError, OverflowError, OSError) as e:
                _render_errors += 1
                if _render_errors <= 3:
                    sys.stderr.write(f"render error #{_render_errors}: {e}\n")

            time.sleep(tick)

    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
