#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Claude AIO Monitor — statusline for Claude Code.

Stdlib-only status line script (uses shared.py for helpers).
Reads JSON from stdin (Claude Code status line protocol), outputs single ANSI-colored line.
Segments drop from the right when terminal is narrow.
CC notifications share the status line row — no full-width padding.

Config env vars:
    CLAUDE_STATUS_WARN  — yellow threshold % (default 50)
    CLAUDE_STATUS_CRIT  — red threshold % (default 80)
"""

import json
import os
import pathlib
import signal
import struct
import sys
import time

from shared import (calc_rates as _calc_rates, _num, _sanitize, safe_read, atomic_write_text,
                    f_tok, f_cost, f_cd,
                    ensure_data_dir, ensure_utf8_stdout, load_history as _shared_load_history,
                    lock_file_handle, unlock_file_handle,
                    _SID_RE, _ANSI_RE, MAX_FILE_SIZE, HISTORY_READ_MAX, HISTORY_RATE_SAMPLES,
                    DATA_DIR, RESERVED_SIDS, SCHEMA_VERSION,
                    strip_context_suffix, WARN_PCT, CRIT_PCT,
                    R, B, C_RED, C_YEL, C_ORN, C_CYN, C_WHT, C_DIM)



_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import ctypes
else:
    import fcntl
    import termios


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
            kernel32 = ctypes.windll.kernel32
            # ctypes defaults foreign-function restype to c_int (32-bit). A
            # HANDLE is pointer-sized, so on 64-bit Windows the value returned
            # by CreateFileW would be truncated/sign-extended and the handle
            # corrupted — GetConsoleScreenBufferInfo then fails and this whole
            # branch becomes a no-op. Declare the real types so the handle
            # survives intact.
            kernel32.CreateFileW.restype = ctypes.c_void_p
            kernel32.CreateFileW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ]
            kernel32.GetConsoleScreenBufferInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            _INVALID_HANDLE = ctypes.c_void_p(-1).value
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
            if h not in (None, 0, _INVALID_HANDLE):
                try:
                    csbi = ctypes.create_string_buffer(22)
                    if kernel32.GetConsoleScreenBufferInfo(h, csbi):
                        _, _, _, _, _, left, _, right, _, _, _ = struct.unpack("hhhhHhhhhhh", csbi.raw)
                        w = right - left + 1
                        if w > 0:
                            return w
                finally:
                    kernel32.CloseHandle(h)
        except Exception:
            pass
    else:
        try:
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
    if pct >= CRIT_PCT:
        return C_RED
    if pct >= WARN_PCT:
        return C_YEL
    return base


# ---------------------------------------------------------------------------
# Formatting — single-line, no background (CC notifications share the row)
# ---------------------------------------------------------------------------
_SEP = f" {C_DIM}\u2502{R} "  # │
_SEP_VLEN = 3  # " │ "


# ---------------------------------------------------------------------------
# Segment builders — each returns (text, visible_length) or None
# ---------------------------------------------------------------------------
def seg_model(data):
    name = _sanitize((data.get("model") or {}).get("display_name", ""))
    name = strip_context_suffix(name).replace(" (200k)", "")
    text = f"{B}{C_WHT}{name}{R}"
    return text, len(_ANSI_RE.sub("", text))


def seg_ctx(data):
    cw = data.get("context_window") or {}
    pct = round(_num(cw.get("used_percentage")))
    total = _num(cw.get("context_window_size"), 0)
    used = int(total * pct / 100) if total else 0
    c = cpc_base(pct, C_CYN)
    tok = f" {C_CYN}{f_tok(used)}/{f_tok(int(total))}{R}" if total else ""
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
    now = time.time()
    if resets > 0 and resets < now:
        pct = 0
    c = cpc_base(pct, C_YEL)
    reset_str = f" {c}\u2192 {f_cd(resets)}{R}" if resets > now else ""
    text = f"{c}{B}5HL{R} {c}{pct}%{R}{reset_str}"
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
    now = time.time()
    if resets > 0 and resets < now:
        pct = 0
    c = cpc_base(pct, C_YEL)
    reset_str = f" {c}\u2192 {f_cd(resets)}{R}" if resets > now else ""
    text = f"{c}{B}7DL{R} {c}{pct}%{R}{reset_str}"
    return text, len(_ANSI_RE.sub("", text))


def seg_cost(data):
    usd = _num((data.get("cost") or {}).get("total_cost_usd"))
    if usd <= 0:
        return None
    text = f"{C_ORN}CST{R} {C_ORN}{B}{f_cost(usd)}{R}"
    return text, len(_ANSI_RE.sub("", text))


def seg_brn(brn):
    if brn is None or brn <= 0.0001:
        return None
    text = f"{C_ORN}BRN{R} {C_ORN}{B}{brn:.4f} $/min{R}"
    return text, len(_ANSI_RE.sub("", text))


# ---------------------------------------------------------------------------
# Layout assembly — single line (CC notifications share the row on the right)
# ---------------------------------------------------------------------------
def build_line(data, cols, brn=None):
    """Build single status line. Drops trailing segments when too wide."""
    sv = _SEP_VLEN

    # All segments in priority order — dropped from the end when too wide
    all_segs = [s for s in [
        seg_model(data),
        seg_ctx(data),
        seg_5hl(data),
        seg_7dl(data),
        seg_cost(data),
        seg_brn(brn),
    ] if s is not None]

    # Drop trailing segments until it fits
    while all_segs:
        vlen = sum(s[1] for s in all_segs) + sv * (len(all_segs) - 1)
        if vlen <= cols:
            break
        all_segs.pop()

    if not all_segs:
        return ""
    return _SEP.join(s[0] for s in all_segs)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # SIGPIPE: silent exit when piped to head/less on Unix (no BrokenPipeError traceback)
    if hasattr(signal, "SIGPIPE"):
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except (AttributeError, ValueError):
            pass
    ensure_utf8_stdout()

    try:
        # Read stdin at byte level and decode UTF-8 explicitly. Claude Code
        # emits JSON as UTF-8 bytes; sys.stdin.read() would use the locale
        # encoding (e.g. cp1250 on SK Windows) when PYTHONUTF8=1 is not set
        # in Claude Code's subprocess env, mangling diacritics in
        # `session_name` / `aiTitle` fields before they ever reach
        # write_shared_state (NEW-002 fix, complements NEW-001 which only
        # covered monitor.py console output).
        raw_bytes = sys.stdin.buffer.read(MAX_FILE_SIZE)
        if not raw_bytes.strip():
            return
        raw = raw_bytes.decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError, RecursionError):
        return

    sid = data.get("session_id") or "default"
    if not _SID_RE.match(str(sid)):
        sid = "default"

    # Read history BEFORE writing — needed for BRN rate computation
    hist = _load_history_for_rates(sid)
    brn, _ctr = _calc_rates(hist)

    cols = _get_terminal_width(fallback=120)
    line = build_line(data, cols, brn=brn)
    if line:
        # Claude Code reads this line from the statusline subprocess's stdout.
        # If it closed the pipe early (e.g. the event was superseded), print()
        # raises BrokenPipeError — swallow it so the statusline never dies with
        # an uncaught traceback in CC's logs. The monitor is still fed below.
        try:
            print(line)
        except (BrokenPipeError, OSError):
            pass

    # Feed data to TUI monitor
    write_shared_state(data)


# ---------------------------------------------------------------------------
# IPC — shared state for monitor.py
# ---------------------------------------------------------------------------
HISTORY_TRIM_TO = 1000
# MAX_FILE_SIZE, DATA_DIR imported from shared.py


def _load_history_for_rates(sid, n=HISTORY_RATE_SAMPLES):
    """Read last n history entries for BRN/CTR rate computation. Call BEFORE write_shared_state.

    Thin wrapper around shared.load_history — both modules share the same reader
    (single source of truth since v1.10.5). DATA_DIR is forwarded explicitly so
    test monkey-patching of statusline.DATA_DIR continues to work.
    """
    return _shared_load_history(sid, n, data_dir=DATA_DIR)


def write_shared_state(data: dict):
    sid = str(data.get("session_id") or "default")
    if not _SID_RE.match(sid):
        sid = "default"
    if sid in RESERVED_SIDS:
        return
    if not ensure_data_dir(DATA_DIR):
        return
    base = DATA_DIR

    # Serialize once — same rules for snapshot and history (avoid TypeError mid-write).
    # _schema_version tags the file-IPC contract: monitor.load_state() gates on
    # it (newer-than-known snapshots degrade to None); older/untagged snapshots
    # already on disk after a `git pull` are tolerated (treated as v0 = pre-tag).
    try:
        snapshot = json.dumps({**data, "_schema_version": SCHEMA_VERSION})
        entry = json.dumps({**data, "_schema_version": SCHEMA_VERSION, "t": time.time()})
    except (TypeError, ValueError):
        return

    # Atomic write of current state via unpredictable temp file (shared helper)
    target = base / f"{sid}.json"
    snapshot_ok = atomic_write_text(target, snapshot)

    # History must stay aligned with the latest snapshot (avoid BRN/CTR vs stale JSON)
    if not snapshot_ok:
        return

    # Append to history JSONL + trim if needed. Append and the read→rewrite
    # trim are serialized via a sidecar lock file — without it, a concurrent
    # statusline append landing between the trim's read and its atomic
    # replace would be silently lost. Lock failure degrades to the old
    # unlocked best-effort behaviour.
    hist = base / f"{sid}.jsonl"
    try:
        with open(hist.with_name(hist.name + ".lock"), "a", encoding="utf-8") as lf:
            locked = lock_file_handle(lf)
            try:
                with open(hist, "a", encoding="utf-8") as f:
                    f.write(entry + "\n")
                # Trim based on actual file size
                if hist.stat().st_size > MAX_FILE_SIZE:
                    _trim_history(hist)
            finally:
                if locked:
                    unlock_file_handle(lf)
    except OSError:
        pass


def _trim_history(path: pathlib.Path):
    raw = safe_read(path, HISTORY_READ_MAX)
    if raw is None:
        return
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if len(lines) > HISTORY_TRIM_TO:
        kept = lines[-HISTORY_TRIM_TO:]
        # FILE-IPC "Trim Policy": malformed JSON lines are dropped during the
        # trim so a torn/corrupted line cannot survive rewrites forever.
        valid = []
        for ln in kept:
            try:
                json.loads(ln)
            except (json.JSONDecodeError, ValueError, RecursionError):
                continue
            valid.append(ln)
        trimmed = "\n".join(valid) + "\n" if valid else ""
        atomic_write_text(path, trimmed)


if __name__ == "__main__":
    main()
