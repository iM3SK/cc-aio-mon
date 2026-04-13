#!/usr/bin/env python3
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
import platform
import re
import struct
import sys
import tempfile
import time

from shared import (calc_rates as _calc_rates, _num, _sanitize, f_tok, f_cost,
                    _SID_RE, _ANSI_RE, MAX_FILE_SIZE, DATA_DIR_NAME,
                    E, R, B, C_RED, C_GRN, C_YEL, C_ORN, C_CYN, C_WHT, C_DIM)

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

# _SID_RE, _ANSI_RE, MAX_FILE_SIZE, ANSI colors — imported from shared.py


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
# Formatting — single-line, no background (CC notifications share the row)
# ---------------------------------------------------------------------------
_SEP = f" {C_DIM}\u2502{R} "  # │
_SEP_VLEN = 3  # " │ "


# ---------------------------------------------------------------------------
# Segment builders — each returns (text, visible_length) or None
# ---------------------------------------------------------------------------
def seg_model(data):
    name = _sanitize(data.get("model", {}).get("display_name", ""))
    name = name.replace(" (1M context)", "").replace(" (200k)", "")
    text = f"{B}{C_WHT}{name}{R}"
    return text, len(_ANSI_RE.sub("", text))


def seg_ctx(data):
    cw = data.get("context_window", {})
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
    if resets > 0 and resets < time.time():
        pct = 0
    c = cpc_base(pct, C_YEL)
    text = f"{c}{B}5HL{R} {c}{pct}%{R}"
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
    text = f"{c}{B}7DL{R} {c}{pct}%{R}"
    return text, len(_ANSI_RE.sub("", text))


def seg_cost(data):
    usd = _num(data.get("cost", {}).get("total_cost_usd"))
    if usd <= 0:
        return None
    text = f"{C_ORN}CST{R} {C_ORN}{B}{f_cost(usd)}{R}"
    return text, len(_ANSI_RE.sub("", text))


def seg_chr(data):
    usage = data.get("context_window", {}).get("current_usage", {})
    cr = _num(usage.get("cache_read_input_tokens"))
    cw = _num(usage.get("cache_creation_input_tokens"))
    total = cr + cw
    if total <= 0:
        return None
    pct = round(cr / total * 100, 1)
    if pct >= WARN:
        c = C_GRN
    elif pct < (100 - CRIT):
        c = C_RED
    else:
        c = C_YEL
    text = f"{C_GRN}{B}CHR{R} {c}{pct}%{R}"
    return text, len(_ANSI_RE.sub("", text))


def seg_brn(brn):
    if brn is None or brn <= 0.0001:
        return None
    text = f"{C_ORN}BRN{R} {C_ORN}{B}{brn:.4f} $/m{R}"
    return text, len(_ANSI_RE.sub("", text))


def seg_apr(data):
    dur_ms = _num(data.get("cost", {}).get("total_duration_ms"))
    api_ms = _num(data.get("cost", {}).get("total_api_duration_ms"))
    if dur_ms <= 0:
        return None
    pct = min(100.0, round(api_ms / dur_ms * 100, 1))
    c = cpc_base(pct, C_GRN)
    text = f"{C_GRN}{B}APR{R} {c}{pct}%{R}"
    return text, len(_ANSI_RE.sub("", text))


# ---------------------------------------------------------------------------
# Layout assembly — single line (CC notifications share the row on the right)
# ---------------------------------------------------------------------------
def build_line(data, cols, brn=None, ctr=None):
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
        seg_apr(data),
        seg_chr(data),
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

    # Read history BEFORE writing — needed for BRN rate computation
    hist = _load_history_for_rates(sid)
    brn, _ctr = _calc_rates(hist)

    cols = _get_terminal_width(fallback=120)
    line = build_line(data, cols, brn=brn)
    if line:
        print(line)

    # Feed data to TUI monitor
    write_shared_state(data)


# ---------------------------------------------------------------------------
# IPC — shared state for monitor.py
# ---------------------------------------------------------------------------
HISTORY_TRIM_TO = 1000
# MAX_FILE_SIZE imported from shared.py
_DATA_DIR = pathlib.Path(tempfile.gettempdir()) / DATA_DIR_NAME


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
            if _DATA_DIR.is_symlink():
                return  # reject symlinked data directory
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
