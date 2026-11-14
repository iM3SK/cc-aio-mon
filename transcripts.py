#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Transcript-derived data for the monitor — no rendering.

Reads Claude Code's session transcripts (~/.claude/projects/<project>/<sid>.jsonl)
and what belongs with them:

    - transcript usage scanner   — scan_transcript_stats (token stats modal)
    - desktop app sessions       — list_desktop_sessions, load_desktop_state
    - session cost breakdown     — _aggregate_session_cost
    - subagent fan-out           — scan_subagents
    - title / effort / permission mode from the transcript head and tail
    - model metadata and pricing — _MODELS, _get_pricing, _usage_cost
    - Claude Code session registry — live_session_ids

monitor.py reaches everything here as `transcripts.<name>`. Stdlib only.
"""

import os
import pathlib
import re
import stat
import sys
import time
from collections import OrderedDict
from datetime import datetime
from shared import (_env_float, _num, _sanitize, _SID_RE, as_dict, claude_projects_dir,
                    HISTORY_RATE_SAMPLES, is_safe_dir, json_object, MIN_EPOCH,
                    nonnegative_number, RESERVED_SIDS, safe_read, SECONDS_1D, SECONDS_7D,
                    TRANSCRIPT_MAX_BYTES)


# ---------------------------------------------------------------------------
# Transcript usage scanner — reads ~/.claude/projects/**/*.jsonl
# ---------------------------------------------------------------------------
_CLAUDE_DIR = claude_projects_dir()  # honours CLAUDE_CONFIG_DIR, like claude_json_path()
MAX_TRANSCRIPT_FILES = 1000  # hard cap on scan to prevent DoS via oversized projects dir
_usage_cache = {}


def _parse_ts(ts_str):
    """Parse ISO timestamp to epoch, 3.8 compatible. Returns 0 on failure."""
    if not isinstance(ts_str, str):  # F-24 / CORRECTNESS-001: integer or other type -> 0
        return 0
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
        # interpret as UTC (F-26 / CORRECTNESS-006).
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
        # clean has no offset at this point; pin to UTC so the fallback
        # path is consistent with the primary path instead of local-time.
        # If clean somehow already carries an offset, the ValueError below
        # lets the bare parse attempt recover.
        try:
            return datetime.fromisoformat(clean + "+00:00").timestamp()
        except (ValueError, TypeError):
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
    reqs = OrderedDict()  # request key -> (model, inp, out, cr, cw, ts); last record wins
    try:
        with open(jl, "rb") as f:
            try:
                fst = os.fstat(f.fileno())
            except OSError:
                return
            if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
                return
            for line in f:
                obj = json_object(line)
                if obj is None:
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
                key = _usage_key(obj, msg) or f"#{len(reqs)}"
                prev = reqs.get(key)
                reqs[key] = (
                    model,
                    int(_num(u.get("input_tokens", 0))),
                    int(_num(u.get("output_tokens", 0))),
                    int(_num(u.get("cache_read_input_tokens", 0))),
                    int(_num(u.get("cache_creation_input_tokens", 0))),
                    prev[5] if prev and prev[5] > 0 else ts,  # day of the request's first record
                )
                # Known-but-ignored fields (F-18/F-19/F-20 / SD-003/007/008):
                # u.get("diagnostics"), u.get("inference_geo"), u.get("service_tier"),
                # obj.get("context_management") — not needed for stats aggregation.
    except (OSError, UnicodeDecodeError):
        pass  # best-effort: keep what was read before the error

    # One entry per API request: Claude Code repeats a request's usage on every
    # content-block record, so summing records would count it several times.
    for model, inp, out, cr, cw, ts in reqs.values():
        if model not in models:
            models[model] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
        models[model]["input"] += inp
        models[model]["output"] += out
        models[model]["cache_read"] += cr
        models[model]["cache_write"] += cw
        models[model]["calls"] += 1
        if ts > 0:
            # A corrupt/huge ts raises OverflowError (not OSError, so the outer
            # guard wouldn't catch it) or OSError on Windows. Skip just this
            # request's day attribution rather than abort the aggregation.
            try:
                day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                continue
            active_days.add(day)
            daily_tokens[day] = daily_tokens.get(day, 0) + inp + out + cr + cw


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

# ---------------------------------------------------------------------------
# Session freshness and Claude Code's session registry
# ---------------------------------------------------------------------------
STALE_THRESHOLD = 1800  # 30 min — Claude Code emits no events during idle
DEAD_SESSION_TTL = 172800  # 48h — auto-purge dead session files from temp dir


# Claude Code's session registry: <config>/sessions/<pid>.json, one per running
# interactive session, removed on exit (observed with Claude Code 2.1.325; not
# documented). A session listed there with a running process is open, so its
# numbers are current even after STALE_THRESHOLD without a change.
_SESSION_REGISTRY_DIR = _CLAUDE_DIR.parent / "sessions"
_SESSION_REGISTRY_MAX_FILES = 64
_SESSION_REGISTRY_MAX_BYTES = 64 * 1024
_live_cache = {"t": -1.0, "ids": frozenset()}


def _pid_alive(pid):
    """True while process `pid` runs. Never signals: on Windows os.kill(pid, 0)
    would send CTRL_C_EVENT, so the process is queried instead."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                k32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def live_session_ids(ttl=2.0):
    """IDs of the sessions Claude Code has open (registry entry + running
    process). Empty on any problem — callers then keep the mtime rule."""
    now = time.monotonic()
    if now - _live_cache["t"] < ttl:
        return _live_cache["ids"]
    ids = set()
    d = pathlib.Path(_SESSION_REGISTRY_DIR)
    try:
        files = list(d.glob("*.json"))[:_SESSION_REGISTRY_MAX_FILES] if is_safe_dir(d) else []
    except OSError:
        files = []
    for f in files:
        try:
            if not stat.S_ISREG(f.lstat().st_mode):
                continue
        except OSError:
            continue
        raw = safe_read(f, _SESSION_REGISTRY_MAX_BYTES)
        rec = json_object(raw) if raw is not None else None
        if not isinstance(rec, dict):
            continue
        sid = rec.get("sessionId")
        if isinstance(sid, str) and _SID_RE.match(sid) and _pid_alive(rec.get("pid")):
            ids.add(sid)
    _live_cache.update({"t": now, "ids": frozenset(ids)})
    return _live_cache["ids"]


# ---------------------------------------------------------------------------
# Desktop app sessions — transcript-only sessions
# ---------------------------------------------------------------------------
# The Claude desktop app runs Claude Code headless (stream-json), so the
# statusLine command never fires and no snapshot reaches DATA_DIR. The session
# transcript (~/.claude/projects/<project>/<sid>.jsonl) is still written live.
# The helpers below derive a snapshot-shaped dict from it, so render_frame can
# draw the session. Values are estimates: cost from _MODELS pricing, context %
# from the last request's input tokens. The transcript carries no API time, no
# line counts and no rate limits.
#
# Claude Code writes one assistant record per content block and repeats the
# request's usage on each of them, so usage is counted once per request
# (_usage_key).
DESKTOP_SESSION_TTL = DEAD_SESSION_TTL  # list transcripts touched in the last 48 h
# Context-window size for desktop sessions; 0 = the model's window from _MODELS
# (_model_context_window), or 1M when the requested model carries [1m].
_desktop_ctx_env = _env_float("CC_MON_DESKTOP_CTX", 0.0)
# inf / nan / absurd values mean "not set" (int(inf) would crash the import).
DESKTOP_CTX_SIZE = int(_desktop_ctx_env) if 0 < _desktop_ctx_env < 1e12 else 0
# The parse is incremental, so a long session may grow past the 50 MiB cap
# that whole-file readers use. The first catch-up read is capped per poll so a
# large transcript never stalls the render loop.
DESKTOP_TRANSCRIPT_MAX_BYTES = 1024 * 1024 * 1024
_DESKTOP_READ_BUDGET = 16 * 1024 * 1024
_TRANSCRIPT_HEAD_BYTES = 64 * 1024
_TRANSCRIPT_TAIL_BYTES = 256 * 1024
_DESKTOP_SCAN_CAP = 512               # max fresh transcripts whose metadata is read per scan
_DESKTOP_STAT_CAP = 20_000            # max transcript files stat'ed per scan (DoS bound)
_ENTRYPOINT_TAGS = {"claude-desktop": "desk"}
_ENTRYPOINT_TAG_MAX = 8
_desktop_meta_cache = OrderedDict()   # {path_str: (mtime, size, meta)}
_DESKTOP_META_CACHE_MAX = 256
_desktop_sessions_cache = {"t": 0.0, "key": None, "sessions": None}
_desktop_state = OrderedDict()        # {sid: incremental parser state}
_DESKTOP_STATE_MAX = 8


def _origin_tag(entrypoint):
    """Short picker tag for the transcript's entrypoint ('claude-desktop' → 'desk')."""
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        return "?"
    ep = entrypoint.strip()
    return _ENTRYPOINT_TAGS.get(ep) or _sanitize(ep)[:_ENTRYPOINT_TAG_MAX] or "?"


def _open_checked(path, st):
    """Open `path` for binary reading only if it is still the file `st`
    described (same inode/device — TOCTOU guard shared with _scan_ai_title).
    Returns an open file object or None."""
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    try:
        fst = os.fstat(fh.fileno())
    except OSError:
        fh.close()
        return None
    if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
        fh.close()
        return None
    return fh


def _complete_lines(raw, *, drop_first=False):
    """Decode a byte window into whole JSONL lines. A window cut mid-file drops
    its partial first line (drop_first) and always its partial last line."""
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if drop_first and lines:
        lines = lines[1:]
    if lines and not text.endswith("\n"):
        lines = lines[:-1]
    return lines


def _transcript_meta(path, st):
    """Picker metadata from the transcript head and tail: entrypoint, title,
    cwd, the most recent assistant model and effort, and the permission mode
    of the latest prompt. Cached per (mtime, size)."""
    key = str(path)
    cached = _desktop_meta_cache.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        _desktop_meta_cache.move_to_end(key)
        return cached[2]
    meta = {"entrypoint": "", "title": "", "cwd": "", "model": "",
            "effort": "", "permission_mode": ""}
    fh = _open_checked(path, st)
    if fh is None:
        return None
    try:
        with fh:
            head = fh.read(_TRANSCRIPT_HEAD_BYTES)
            tail_start = max(len(head), st.st_size - _TRANSCRIPT_TAIL_BYTES)
            tail = b""
            if tail_start < st.st_size:
                fh.seek(tail_start)
                tail = fh.read(_TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return None
    if tail_start == len(head):
        lines = _complete_lines(head + tail)  # head and tail are contiguous
    else:
        lines = _complete_lines(head) + _complete_lines(tail, drop_first=True)
    ai_title = ""
    for line in lines:
        if not line.startswith("{"):
            continue
        rec = json_object(line)
        if rec is None:
            continue
        rtype = rec.get("type")
        if not meta["entrypoint"] and isinstance(rec.get("entrypoint"), str):
            meta["entrypoint"] = rec["entrypoint"]
        if isinstance(rec.get("cwd"), str) and rec["cwd"]:
            meta["cwd"] = _sanitize(rec["cwd"])
        if not rec.get("isSidechain"):
            if rtype == "user" and isinstance(rec.get("permissionMode"), str) and rec["permissionMode"]:
                meta["permission_mode"] = rec["permissionMode"]
            elif rtype == "assistant" and isinstance(rec.get("effort"), str) and rec["effort"]:
                meta["effort"] = rec["effort"]
        if rtype == "custom-title" and isinstance(rec.get("customTitle"), str) and rec["customTitle"].strip():
            meta["title"] = _sanitize(rec["customTitle"].strip())
        elif rtype == "ai-title" and isinstance(rec.get("aiTitle"), str) and rec["aiTitle"].strip():
            ai_title = _sanitize(rec["aiTitle"].strip())
        elif rtype == "assistant" and not rec.get("isSidechain"):
            mid = as_dict(rec.get("message")).get("model")
            if isinstance(mid, str) and mid and not mid.startswith("<"):
                meta["model"] = mid
    meta["title"] = meta["title"] or ai_title
    _desktop_meta_cache[key] = (st.st_mtime, st.st_size, meta)
    while len(_desktop_meta_cache) > _DESKTOP_META_CACHE_MAX:
        _desktop_meta_cache.popitem(last=False)
    return meta


def list_desktop_sessions(exclude=()):
    """Transcript-only sessions touched within DESKTOP_SESSION_TTL.

    Scans ~/.claude/projects/<project>/<sid>.jsonl (subagent transcripts live
    one level deeper and are not sessions). Session IDs in `exclude` — those
    that already have a statusline snapshot — are skipped, so a CLI session
    appears once, in the CLI section. Each entry has the list_sessions() keys
    plus source="desktop", origin (entrypoint tag) and path. Newest first."""
    root = _safe_claude_projects_root()
    if root is None:
        return []
    exclude = set(exclude)
    now = time.time()
    # Pass 1 — stat only (cheap): keep transcripts touched within the TTL, the
    # newest file per sid. The scan cap applies to these fresh files, so old
    # transcripts (Claude Code keeps 30 days) can never crowd out a new one.
    fresh = {}
    stat_count = 0
    try:
        projects = list(root.iterdir())
    except OSError:
        return []
    for proj in projects:
        try:
            pst = proj.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
            continue
        try:
            files = list(proj.glob("*.jsonl"))
        except OSError:
            continue
        for jl in files:
            stat_count += 1
            if stat_count > _DESKTOP_STAT_CAP:
                break
            sid = jl.stem
            if sid in exclude or sid in RESERVED_SIDS or not _SID_RE.match(sid):
                continue
            try:
                st = jl.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
            if now - st.st_mtime > DESKTOP_SESSION_TTL or st.st_size > DESKTOP_TRANSCRIPT_MAX_BYTES:
                continue
            prev = fresh.get(sid)
            if prev is None or prev[1].st_mtime < st.st_mtime:
                fresh[sid] = (jl, st)
    # Pass 2 — read head/tail metadata of the newest _DESKTOP_SCAN_CAP files.
    found = {}
    newest = sorted(fresh.items(), key=lambda kv: kv[1][1].st_mtime, reverse=True)
    for sid, (jl, st) in newest[:_DESKTOP_SCAN_CAP]:
        path = _safe_transcript_path(str(jl))
        if path is None:
            continue
        meta = _transcript_meta(path, st)
        if not meta or not meta["model"]:
            continue  # no assistant reply yet — nothing to show
        age = now - st.st_mtime
        found[sid] = {
            "id": sid, "mtime": st.st_mtime, "age": age,
            "stale": age > STALE_THRESHOLD and sid not in live_session_ids(),
            "model": _sanitize(_model_label(meta["model"])),
            "session_name": meta["title"],
            "ai_title": "",
            "cwd": meta["cwd"],
            "source": "desktop",
            "origin": _origin_tag(meta["entrypoint"]),
            "path": str(path),
        }
    sessions = list(found.values())
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def cached_desktop_sessions(exclude=(), ttl=2.0):
    """list_desktop_sessions() behind a short TTL for the 20 Hz picker loop."""
    now = time.monotonic()
    key = frozenset(exclude)
    c = _desktop_sessions_cache
    if c["sessions"] is not None and c["key"] == key and now - c["t"] < ttl:
        return c["sessions"]
    sessions = list_desktop_sessions(exclude)
    c.update({"t": now, "key": key, "sessions": sessions})
    return sessions


def _new_desktop_state(path, st):
    return {
        "path": str(path), "ident": (st.st_ino, st.st_dev), "offset": 0,
        "msgs": OrderedDict(), "first_t": 0.0, "last_t": 0.0,
        "title": "", "ai_title": "", "cwd": "", "entrypoint": "",
        "requested": "", "effort": "", "permission_mode": "",
        "built_at": -1, "built": None,
    }


def _ingest_desktop_record(state, rec):
    ts = _parse_ts(rec.get("timestamp"))
    if ts >= MIN_EPOCH:
        if not state["first_t"] or ts < state["first_t"]:
            state["first_t"] = ts
        state["last_t"] = max(state["last_t"], ts)
    rtype = rec.get("type")
    if not state["entrypoint"] and isinstance(rec.get("entrypoint"), str):
        state["entrypoint"] = rec["entrypoint"]
    if isinstance(rec.get("cwd"), str) and rec["cwd"]:
        state["cwd"] = rec["cwd"]
    if isinstance(rec.get("requestedModel"), str):
        state["requested"] = rec["requestedModel"]
    if (rtype == "user" and not rec.get("isSidechain")
            and isinstance(rec.get("permissionMode"), str) and rec["permissionMode"]):
        state["permission_mode"] = rec["permissionMode"]
    if rtype == "custom-title" and isinstance(rec.get("customTitle"), str):
        state["title"] = rec["customTitle"].strip()
    elif rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
        state["ai_title"] = rec["aiTitle"].strip()
    elif rtype == "assistant" and not rec.get("isSidechain"):
        if isinstance(rec.get("effort"), str) and rec["effort"]:
            state["effort"] = rec["effort"]
        msg = as_dict(rec.get("message"))
        model = msg.get("model")
        if not isinstance(model, str) or not model or model.startswith("<"):
            return  # synthetic / local placeholder message — no API usage
        u = as_dict(msg.get("usage"))
        mid = _usage_key(rec, msg) or rec.get("uuid")
        if not isinstance(mid, str) or not mid:
            return
        prev = state["msgs"].get(mid)
        cc = as_dict(u.get("cache_creation"))
        stu = as_dict(u.get("server_tool_use"))
        state["msgs"][mid] = {
            "t": prev["t"] if prev else (ts if ts >= MIN_EPOCH else 0.0),
            "model": model,
            "speed": u.get("speed") if isinstance(u.get("speed"), str) else None,
            "in": _num(u.get("input_tokens")),
            "out": _num(u.get("output_tokens")),
            "cr": _num(u.get("cache_read_input_tokens")),
            "cw": _num(u.get("cache_creation_input_tokens")),
            "cw1h": _num(cc.get("ephemeral_1h_input_tokens")),
            "cw5m": _num(cc.get("ephemeral_5m_input_tokens")),
            "wsr": _num(stu.get("web_search_requests")),
            "wfr": _num(stu.get("web_fetch_requests")),
        }


def _desktop_ctx_size(state, model_id):
    if DESKTOP_CTX_SIZE > 0:
        return DESKTOP_CTX_SIZE
    if "[1m]" in state["requested"].lower():
        return 1_000_000
    return _model_context_window(model_id)


def _build_desktop_snapshot(sid, state, mtime):
    """Snapshot-shaped dict + rate history from the parsed transcript state.
    The snapshot also carries _session_cost, the breakdown the cost modal
    shows (same shape as _aggregate_session_cost)."""
    msgs = list(state["msgs"].values())
    if not msgs:
        return None
    last = msgs[-1]
    window = _desktop_ctx_size(state, last["model"])
    sc = {k: 0.0 for k in ("input", "output", "cache_read", "cache_write",
                           "cost_input", "cost_output", "cost_cache_read", "cost_cache_write",
                           "web_search_requests", "web_fetch_requests", "cache_1h", "cache_5m")}
    cost = 0.0
    hist = []
    for m in msgs:
        c = _usage_cost(m["model"], m["speed"], m["in"], m["out"], m["cr"], m["cw"], m["cw1h"])
        cost += sum(c.values())
        for k, v in (("input", m["in"]), ("output", m["out"]), ("cache_read", m["cr"]),
                     ("cache_write", m["cw"]), ("cache_1h", m["cw1h"]), ("cache_5m", m["cw5m"]),
                     ("web_search_requests", m["wsr"]), ("web_fetch_requests", m["wfr"])):
            sc[k] += v
        for k in ("input", "output", "cache_read", "cache_write"):
            sc["cost_" + k] += c[k]
        if m["t"] >= MIN_EPOCH:
            pct = (m["in"] + m["cr"] + m["cw"]) / window * 100
            hist.append({"t": m["t"], "cost": {"total_cost_usd": cost},
                         "context_window": {"used_percentage": pct}})
    session_cost = {k: (int(v) if not k.startswith("cost_") else v) for k, v in sc.items()}
    session_cost["cost_total"] = cost
    ctx_tokens = last["in"] + last["cr"] + last["cw"]
    dur_ms = max(0.0, (state["last_t"] - state["first_t"]) * 1000) if state["first_t"] else 0.0
    data = {
        "session_id": sid,
        "transcript_path": state["path"],
        "cwd": _sanitize(state["cwd"]),
        "session_name": _sanitize(state["title"] or state["ai_title"]),
        "model": {"id": _sanitize(last["model"]),
                  "display_name": _sanitize(_model_label(last["model"]))},
        "context_window": {
            "context_window_size": window,
            "used_percentage": min(100.0, ctx_tokens / window * 100),
            # Same meaning as the statusline fields: the current context.
            "total_input_tokens": int(ctx_tokens),
            "total_output_tokens": int(last["out"]),
            "current_usage": {
                "input_tokens": int(last["in"]),
                "output_tokens": int(last["out"]),
                "cache_read_input_tokens": int(last["cr"]),
                "cache_creation_input_tokens": int(last["cw"]),
            },
        },
        "cost": {"total_cost_usd": cost, "total_duration_ms": dur_ms},
        # Same shape as the statusline field; permission_mode has no statusline
        # counterpart and comes from the latest prompt record.
        "effort": {"level": _sanitize(state["effort"])} if state["effort"] else {},
        # The statusline field's meaning, taken from the last request's speed
        # (None when the transcript does not say).
        "fast_mode": (last["speed"] == "fast") if last["speed"] else None,
        "permission_mode": _sanitize(state["permission_mode"]),
        "_source": "desktop",
        "_origin": _origin_tag(state["entrypoint"]),
        "_mtime": mtime,
        "_session_cost": session_cost,
    }
    return data, hist[-HISTORY_RATE_SAMPLES:]


def load_desktop_state(sid, path):
    """Return (snapshot, history) for a desktop session, or None.

    Parses the transcript incrementally: only bytes appended since the last
    call are read, so polling a multi-megabyte transcript stays cheap. At most
    _DESKTOP_READ_BUDGET bytes are read per call; while a large transcript is
    still being caught up the call returns None. A replaced or truncated file
    restarts the parse."""
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        return None
    safe = _safe_transcript_path(path)
    if safe is None:
        return None
    try:
        st = safe.stat()
    except OSError:
        return None
    if st.st_size > DESKTOP_TRANSCRIPT_MAX_BYTES:
        return None
    state = _desktop_state.get(sid)
    if (state is None or state["path"] != str(safe)
            or state["ident"] != (st.st_ino, st.st_dev) or st.st_size < state["offset"]):
        state = _new_desktop_state(safe, st)
    _desktop_state[sid] = state
    _desktop_state.move_to_end(sid)
    while len(_desktop_state) > _DESKTOP_STATE_MAX:
        _desktop_state.popitem(last=False)
    caught_up = True
    if st.st_size > state["offset"]:
        want = st.st_size - state["offset"]
        caught_up = want <= _DESKTOP_READ_BUDGET
        fh = _open_checked(safe, st)
        if fh is None:
            return None
        try:
            with fh:
                fh.seek(state["offset"])
                raw = fh.read(min(want, _DESKTOP_READ_BUDGET))
        except OSError:
            return None
        end = raw.rfind(b"\n")
        if end < 0 and not caught_up:
            end = len(raw) - 1  # one line longer than the budget: skip it
        if end >= 0:
            chunk = raw[:end + 1]
            state["offset"] += len(chunk)
            for line in chunk.decode("utf-8", errors="replace").split("\n"):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                rec = json_object(line)
                if rec is not None:
                    _ingest_desktop_record(state, rec)
    if not caught_up:
        return None  # still reading a large transcript; the next poll continues
    if state["built_at"] != state["offset"] or state["built"] is None:
        state["built"] = _build_desktop_snapshot(sid, state, st.st_mtime)
        state["built_at"] = state["offset"]
    elif state["built"] is not None:
        state["built"][0]["_mtime"] = st.st_mtime
    return state["built"]


# ---------------------------------------------------------------------------
# Model metadata and pricing
# ---------------------------------------------------------------------------
# Prices per 1M tokens (USD). Sources: official Anthropic pricing page.
# cache_write = 5-minute TTL price (1.25x input). A 1-hour cache write costs
# CACHE_WRITE_1H_MULT x input; _usage_cost applies it to the 1h share that
# usage.cache_creation reports.
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}  # Sonnet-tier fallback
CACHE_WRITE_1H_MULT = 2.0
DEFAULT_CTX_WINDOW = 1_000_000  # every current model except Haiku 4.5; older 200K models set "ctx"

# Single source of truth for model metadata — keyed by Anthropic model ID.
# Each entry: {"name": "...", "code": ("XX", "V.v"), "pricing": {...} | None}
# Pricing is per-1M-token USD (input / output / cache_read / cache_write).
# Optional: "pricing_fast" (speed="fast"), "pricing_long" + "long_threshold"
# (rates for a request whose prompt exceeds the threshold), "ctx" (context
# window when it is not DEFAULT_CTX_WINDOW).
# Adding a new model = one entry here, not three. Used by _get_pricing (below)
# and _model_code (token-stats modal section).
_MODELS = {
    # Fable 5.2 / Mythos 5.2: $8 / $40, cache read $0.20 (0.025x), 5m write $10.
    # Fast mode $16 / $80; prompt-caching multipliers apply on top of fast-mode
    # rates (Anthropic fast-mode docs), so cache read $0.40, 5m write $20.
    "claude-fable-5-2": {"name": "Fable 5.2", "code": ("FBL", "5.2"),
                         "pricing": {"input": 8.0, "output": 40.0, "cache_read": 0.20, "cache_write": 10.00},
                         "pricing_fast": {"input": 16.0, "output": 80.0, "cache_read": 0.40, "cache_write": 20.00}},
    "claude-mythos-5-2": {"name": "Mythos 5.2", "code": ("MY", "5.2"),
                          "pricing": {"input": 8.0, "output": 40.0, "cache_read": 0.20, "cache_write": 10.00},
                          "pricing_fast": {"input": 16.0, "output": 80.0, "cache_read": 0.40, "cache_write": 20.00}},
    # Fable 5.1: same per-token price as Fable 5, but cache reads are 0.025x ($0.25).
    "claude-fable-5-1": {"name": "Fable 5.1", "code": ("FBL", "5.1"),
                         "pricing": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.50}},
    "claude-mythos-5-1": {"name": "Mythos 5.1", "code": ("MY", "5.1"),
                          "pricing": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.50}},
    "claude-fable-5": {"name": "Fable 5", "code": ("FBL", "5"),
                       "pricing": {"input": 10.0, "output": 50.0, "cache_read": 1.00, "cache_write": 12.50}},
    # Project Glasswing only — same tier as Fable 5. Rare in transcripts.
    "claude-mythos-5": {"name": "Mythos 5", "code": ("MY", "5"),
                        "pricing": {"input": 10.0, "output": 50.0, "cache_read": 1.00, "cache_write": 12.50}},
    # Standard and fast-mode rates: https://platform.claude.com/docs/en/about-claude/pricing
    "claude-opus-5-5": {"name": "Opus 5.5", "code": ("OP", "5.5"),
                        "pricing": {"input": 4.0,  "output": 20.0, "cache_read": 0.20, "cache_write": 5.00},
                        "pricing_fast": {"input": 8.0, "output": 40.0, "cache_read": 0.40, "cache_write": 10.00}},
    "claude-opus-5": {"name": "Opus 5", "code": ("OP", "5"),
                      "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
                      "pricing_fast": {"input": 10.0, "output": 50.0, "cache_read": 1.00, "cache_write": 12.50}},
    "claude-opus-4-8": {"name": "Opus 4.8", "code": ("OP", "4.8"),
                        "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
                        # fast-mode premium (Anthropic pricing page, 2026-06): Opus 4.8 is $10/$50,
                        # distinct from Opus 4.7/4.6 ($30/$150); cache = 0.1x read / 1.25x 5m write.
                        "pricing_fast": {"input": 10.0, "output": 50.0, "cache_read": 1.00, "cache_write": 12.50}},
    "claude-opus-4-7": {"name": "Opus 4.7", "code": ("OP", "4.7"),
                        "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
                        "pricing_fast": {"input": 30.0, "output": 150.0, "cache_read": 3.00, "cache_write": 37.50}},
    "claude-opus-4-6": {"name": "Opus 4.6", "code": ("OP", "4.6"),
                        "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
                        "pricing_fast": {"input": 30.0, "output": 150.0, "cache_read": 3.00, "cache_write": 37.50}},
    "claude-opus-4-5": {"name": "Opus 4.5", "code": ("OP", "4.5"), "ctx": 200_000,
                        "pricing": {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25}},
    "claude-opus-4-1": {"name": "Opus 4.1", "code": ("OP", "4.1"), "ctx": 200_000,
                        "pricing": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75}},
    "claude-sonnet-5-5": {"name": "Sonnet 5.5", "code": ("SO", "5.5"),
                          "pricing": {"input": 2.0,  "output": 10.0, "cache_read": 0.10, "cache_write": 2.50}},
    # Sonnet 5: $2 / $10 with the standard cache multipliers (0.1x read, 1.25x 5m write).
    "claude-sonnet-5": {"name": "Sonnet 5", "code": ("SO", "5"),
                        "pricing": {"input": 2.0,  "output": 10.0, "cache_read": 0.20, "cache_write": 2.50}},
    "claude-sonnet-4-6": {"name": "Sonnet 4.6", "code": ("SO", "4.6"),
                          "pricing": {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}},
    "claude-sonnet-4-5": {"name": "Sonnet 4.5", "code": ("SO", "4.5"), "ctx": 200_000,
                          "pricing": {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}},
    # Haiku 5.5: $0.10 / $0.50 up to a 100K-token prompt, $0.50 / $2.50 above.
    # Standard cache multipliers on both tiers (0.1x read, 1.25x 5m write).
    "claude-haiku-5-5": {"name": "Haiku 5.5", "code": ("HA", "5.5"),
                         "pricing": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
                         "pricing_long": {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625},
                         "long_threshold": 100_000},
    "claude-haiku-4-5": {"name": "Haiku 4.5", "code": ("HA", "4.5"), "ctx": 200_000,
                         "pricing": {"input": 1.0,  "output": 5.0,  "cache_read": 0.10, "cache_write": 1.25}},
    # Retired model — kept for correct pricing of historical transcripts. Real ID
    # is claude-3-5-haiku-20241022; _model_base strips the -YYYYMMDD to this key.
    "claude-3-5-haiku": {"name": "Haiku 3.5", "code": ("HA", "3.5"), "ctx": 200_000,
                         "pricing": {"input": 0.8,  "output": 4.0,  "cache_read": 0.08, "cache_write": 1.00}},
    # Short-ID fallbacks (some transcript entries use abbreviated IDs).
    # Priced at current standard tier to avoid falling back to _DEFAULT_PRICING
    # (Sonnet-tier), which would over-charge Haiku by ~3x.
    "haiku":  {"name": "Haiku",  "code": ("HA", ""), "pricing": {"input": 1.0, "output": 5.0,  "cache_read": 0.10, "cache_write": 1.25}},
    "sonnet": {"name": "Sonnet", "code": ("SO", ""), "pricing": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}},
    "opus":   {"name": "Opus",   "code": ("OP", ""), "pricing": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25}},
    # Fable / Mythos short aliases: priced as the current release (5.2).
    "fable":  {"name": "Fable",  "code": ("FBL", ""), "pricing": {"input": 8.0, "output": 40.0, "cache_read": 0.20, "cache_write": 10.00}},
    "mythos": {"name": "Mythos", "code": ("MY", ""), "pricing": {"input": 8.0, "output": 40.0, "cache_read": 0.20, "cache_write": 10.00}},
}


def _model_base(model_id):
    """Normalize a model ID for _MODELS lookup: strip the [..] context-tier
    suffix and a trailing -YYYYMMDD date snapshot. Statusline sends bare IDs
    (claude-opus-4-8), transcripts send dated ones (claude-haiku-4-5-20251001) —
    both must resolve to the same key."""
    base = (model_id if isinstance(model_id, str) else "").split("[")[0]
    return re.sub(r"-\d{8}$", "", base)


def _get_pricing(model_id, speed=None, prompt_tokens=None):
    """Get pricing for model. speed="fast" selects fast-mode rates when the
    model defines them; a prompt (input + cache read + cache write tokens)
    above the model's long_threshold selects pricing_long; otherwise standard.
    Falls back to _DEFAULT_PRICING."""
    entry = _MODELS.get(_model_base(model_id))
    if entry:
        if speed == "fast" and entry.get("pricing_fast"):
            return entry["pricing_fast"]
        if (entry.get("pricing_long") and prompt_tokens is not None
                and prompt_tokens > entry.get("long_threshold", float("inf"))):
            return entry["pricing_long"]
        if entry.get("pricing"):
            return entry["pricing"]
    return _DEFAULT_PRICING


def _model_context_window(model_id):
    """Context window in tokens: DEFAULT_CTX_WINDOW unless _MODELS says otherwise."""
    entry = _MODELS.get(_model_base(model_id)) or {}
    return int(entry.get("ctx") or DEFAULT_CTX_WINDOW)


def _usage_key(rec, msg):
    """Identity of the API request behind an assistant record. Claude Code
    writes one record per content block and repeats the request's usage on
    each, so usage must be counted once per key. None = no identity (count
    the record on its own)."""
    for k in (msg.get("id"), rec.get("requestId")):
        if isinstance(k, str) and k:
            return k
    return None


def _usage_cost(model_id, speed, inp, out, cr, cw, cw_1h=0):
    """USD cost of one request, split as {input, output, cache_read,
    cache_write}. cw is every cache-creation token; cw_1h is the part written
    with the 1-hour TTL (from usage.cache_creation), priced at
    CACHE_WRITE_1H_MULT x input instead of the 5-minute write rate."""
    p = _get_pricing(model_id, speed, prompt_tokens=inp + cr + cw)
    cw_1h = max(0.0, min(cw_1h, cw))
    return {
        "input": inp * p["input"] / 1_000_000,
        "output": out * p["output"] / 1_000_000,
        "cache_read": cr * p["cache_read"] / 1_000_000,
        "cache_write": ((cw - cw_1h) * p["cache_write"]
                        + cw_1h * p["input"] * CACHE_WRITE_1H_MULT) / 1_000_000,
    }


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


_AI_TITLE_CACHE = OrderedDict()  # LRU: {sid: (ts, mtime, meta_or_None)}
_AI_TITLE_CACHE_MAX = 64
_AI_TITLE_TTL = 30.0


def _scan_ai_title(transcript_path):
    """Return the session's sanitized title from its transcript, or None.

    A rename (`custom-title`) wins over the generated `ai-title`; the last
    non-empty record of a kind wins.
    """
    return (_scan_transcript_meta(transcript_path) or {}).get("title") or None


def _scan_transcript_meta(transcript_path):
    """Head/tail metadata of the session's transcript (_transcript_meta), or None.

    Only the head and tail of the file are read, so this stays fast on
    multi-megabyte transcripts called per render tick. Path containment via
    _safe_transcript_path.
    """
    path = _safe_transcript_path(transcript_path)
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > DESKTOP_TRANSCRIPT_MAX_BYTES:
        return None

    sid = path.stem
    if not _SID_RE.match(sid):
        return None

    now = time.time()
    cached = _AI_TITLE_CACHE.get(sid)
    if cached and cached[1] == st.st_mtime and (now - cached[0]) < _AI_TITLE_TTL:
        _AI_TITLE_CACHE.move_to_end(sid)
        return cached[2]

    # Head + tail read with the TOCTOU identity check (_transcript_meta):
    # Claude Code repeats ai-title records through the session (the first one
    # can sit hundreds of KiB in), and a rename writes custom-title, which wins.
    meta = _transcript_meta(path, st)

    while len(_AI_TITLE_CACHE) >= _AI_TITLE_CACHE_MAX:
        _AI_TITLE_CACHE.popitem(last=False)
    _AI_TITLE_CACHE[sid] = (now, st.st_mtime, meta)
    return meta


def _aggregate_session_cost(data):
    """Walk the current session's transcript JSONL, sum per-category tokens
    once per API request (_usage_key), apply pricing per-request model.
    Returns dict {input, output, cache_read, cache_write, cost_total,
                  cost_input, cost_output, cost_cache_read, cost_cache_write}
    or None if transcript unreachable. A desktop app snapshot already carries
    this breakdown (_session_cost) from its incremental parse.

    F-06 / CACHE-06: all failure paths for a valid sid write a short-lived
    sentinel (now, None) to _SESSION_COST_CACHE so the next render-frame call
    within TTL returns cached None immediately instead of re-scanning.  The
    cache-hit branch already returns cached[1] (which may be None) correctly.
    """
    if data.get("_source") == "desktop" and isinstance(data.get("_session_cost"), dict):
        return data["_session_cost"]
    sid = data.get("session_id")
    sid = sid.strip() if isinstance(sid, str) else ""
    if not sid or not _SID_RE.match(sid):
        # Invalid sid — cannot key the cache; plain return.
        return None

    now = time.time()
    cached = _SESSION_COST_CACHE.get(sid)
    if cached and (now - cached[0]) < _SESSION_COST_TTL:
        _SESSION_COST_CACHE.move_to_end(sid)  # LRU touch
        return cached[1]  # may be None if a sentinel was stored

    def _cache_sentinel():
        """Store (now, None) sentinel so repeated failure calls are cheap."""
        _SESSION_COST_CACHE[sid] = (now, None)
        _SESSION_COST_CACHE.move_to_end(sid)
        while len(_SESSION_COST_CACHE) > _SESSION_COST_CACHE_MAX:
            _SESSION_COST_CACHE.popitem(last=False)

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
        _cache_sentinel()
        return None

    try:
        st = path.stat()
    except OSError:
        _cache_sentinel()
        return None
    if st.st_size > TRANSCRIPT_MAX_BYTES:
        _cache_sentinel()
        return None

    has_usage = False
    reqs = OrderedDict()  # request key -> usage of that request (last record wins)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # S-P2-2 (CWE-367): TOCTOU guard — same fstat identity check as
            # _scan_ai_title: if the inode/device pair diverged between the
            # stat above and this open, the file was swapped underneath us.
            try:
                fst = os.fstat(f.fileno())
            except OSError:
                _cache_sentinel()
                return None
            if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
                _cache_sentinel()
                return None
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                rec = json_object(line)
                if rec is None or rec.get("type") != "assistant":
                    continue
                # Same crash class as the stats-modal aggregator: a string
                # "message" would turn the .get() calls into AttributeError.
                msg = rec.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage") or {}
                if not isinstance(u, dict):
                    u = {}
                if any(nonnegative_number(u.get(key)) is not None for key in (
                    "input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )):
                    has_usage = True
                mid = msg.get("model") or ""
                if not isinstance(mid, str):
                    mid = ""
                if mid.startswith("<"):
                    continue  # <synthetic> placeholder — no API usage
                stu = as_dict(u.get("server_tool_use"))
                cc = as_dict(u.get("cache_creation"))
                key = _usage_key(rec, msg) or f"#{len(reqs)}"
                reqs[key] = (
                    mid, u.get("speed"),
                    _num(u.get("input_tokens", 0)), _num(u.get("output_tokens", 0)),
                    _num(u.get("cache_read_input_tokens", 0)),
                    _num(u.get("cache_creation_input_tokens", 0)),
                    int(_num(cc.get("ephemeral_1h_input_tokens", 0))),
                    int(_num(cc.get("ephemeral_5m_input_tokens", 0))),
                    int(_num(stu.get("web_search_requests", 0))),
                    int(_num(stu.get("web_fetch_requests", 0))),
                )
                # Known-but-ignored fields (F-18/F-19/F-20 / SD-003/007/008):
                # u.get("diagnostics"), u.get("inference_geo"), u.get("service_tier"),
                # rec.get("context_management") — not needed for cost/token aggregation.
    except OSError:
        _cache_sentinel()
        return None

    if not has_usage:
        _cache_sentinel()
        return None

    inp = out = cr = cw = 0.0
    ci = co = ccr = ccw = 0.0
    wsr = wfr = c1h = c5m = 0
    for mid, speed, in_inc, out_inc, cr_inc, cw_inc, h1, m5, ws, wf in reqs.values():
        inp += in_inc; out += out_inc; cr += cr_inc; cw += cw_inc
        cost = _usage_cost(mid, speed, in_inc, out_inc, cr_inc, cw_inc, h1)
        ci += cost["input"]; co += cost["output"]
        ccr += cost["cache_read"]; ccw += cost["cache_write"]
        c1h += h1; c5m += m5; wsr += ws; wfr += wf

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

_MODEL_ID_RE = re.compile(
    r"^claude-(opus|sonnet|haiku|mythos|fable)-(\d+)(?:-(\d+))?$"
)


def _model_label(model_id):
    base = _model_base(model_id)
    entry = _MODELS.get(base)
    if entry:
        return entry["name"]
    m = _MODEL_ID_RE.match(base)
    if m:
        fam = m.group(1).capitalize()
        version = m.group(2)
        if m.group(3):
            version += f".{m.group(3)}"
        return f"{fam} {version}"
    if base == "fable":
        return "Fable"
    return base or "?"


# ---------------------------------------------------------------------------
# Subagent fan-out — live subagent / Workflow agents of the watched session.
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
        attr_label = None  # first attributionAgent value found in this file (F-08)
        # Bounded, TOCTOU-safe read (never exceeds the cap even if the file
        # grows between lstat and read). None == unreadable or over cap.
        raw = safe_read(f, _SUBAGENT_FILE_MAX)
        too_large = raw is None
        seen = set()  # request keys already counted (usage repeats per content block)
        if raw is not None:
            for line in raw.decode("utf-8", errors="replace").splitlines():
                o = json_object(line)
                if not isinstance(o, dict):
                    continue  # valid JSON but not an object (e.g. bare array)
                # Extract attributionAgent label from first matching record (F-08)
                if attr_label is None:
                    al = o.get("attributionAgent")
                    if isinstance(al, str) and al.strip():
                        attr_label = _sanitize(al.strip())
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                u = m.get("usage")
                ukey = _usage_key(o, m)  # not `key`: that names the cache entry
                if isinstance(u, dict) and (ukey is None or ukey not in seen):
                    if ukey is not None:
                        seen.add(ukey)
                    tok += (_num(u.get("input_tokens"), 0) + _num(u.get("output_tokens"), 0)
                            + _num(u.get("cache_creation_input_tokens"), 0)
                            + _num(u.get("cache_read_input_tokens"), 0))
                content = m.get("content")
                for c in (content if isinstance(content, list) else []):
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        last_tool = c.get("name")
        is_active = (now - mt) < _SUBAGENT_ACTIVE_WINDOW
        if is_active:
            active += 1
        total_tok += tok
        aid = f.stem
        if aid.startswith("agent-"):
            aid = aid[6:]
        agents.append({"id": _sanitize(aid[:12]), "tokens": int(tok), "tool": last_tool,
                       "active": is_active, "mtime": mt, "too_large": too_large,
                       "label": attr_label})

    agents.sort(key=lambda a: a["mtime"], reverse=True)
    # total counts agents we actually materialised (stat/symlink-rejected files
    # drop out), so the header and "+N more" line match the rendered list.
    data = {"total": len(agents), "active": active,
            "total_tokens": total_tok, "agents": agents}

    _subagents_cache[key] = {"t": mono, "data": data}
    if len(_subagents_cache) > _SUBAGENTS_CACHE_MAX:
        oldest = min((_k for _k in _subagents_cache if _k != key),
                     key=lambda kk: _subagents_cache[kk]["t"], default=None)
        if oldest is not None:
            evicted = _subagents_cache.pop(oldest, None)
            # Tombstone (F-07 / CACHE-09): keep last-known data for render_agents
            # so re-opening the modal doesn't flash "Scanning…".  Only re-add when
            # it fits within the cap; if we're still over the limit after pop the
            # tombstone is silently dropped to enforce the hard size bound.
            if (evicted is not None and evicted.get("data") is not None
                    and not evicted.get("_tombstone")
                    and len(_subagents_cache) < _SUBAGENTS_CACHE_MAX):
                _subagents_cache[oldest] = {
                    "t": evicted["t"], "data": evicted["data"], "_tombstone": True
                }
    return data
