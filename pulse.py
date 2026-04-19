#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Anthropic Pulse — backend stability monitor.

Fetches status.claude.com summary.json + pings api.anthropic.com.
Computes a 0-100 stability score and verdict. Stdlib only.

Public API:
    start_pulse_worker()     — launch daemon thread (idempotent)
    get_pulse_snapshot()     — return latest snapshot dict (thread-safe read)
    compute_score(raw)       — pure scoring fn (testable)

Snapshot schema (keys may be None during warm-up or on errors):
    t               float   monotonic time of last fetch
    wall_t          float   wall time of last fetch
    score           int     0-100 display score (smoothed); None on error
    raw_score       int     0-100 raw score from current fetch; None on error
    verdict         str     human-readable verdict line
    level           str     "ok" | "degraded" | "bad" | "error"
    reason          str     short reason string
    indicator       str     none|minor|major|critical|maintenance; None on error
    incidents       list    [{"name", "impact", "affected_models": [..]}]
    components      list    [{"name", "status"}]
    latency_ms      float   last HTTPS probe round-trip; None on timeout
    latency_p50_ms  int     median latency over recent window; None if <3 samples
    latency_p95_ms  int     p95 latency over recent window; None if <3 samples
    error           str     short error tag for UI (HTTP 503 / timeout / ...)
"""

import json
import os
import pathlib
import re
import socket
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from shared import DATA_DIR, ensure_data_dir, safe_read, VERSION

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUMMARY_URL = "https://status.claude.com/api/v2/summary.json"
PROBE_URL = "https://api.anthropic.com/v1/messages"  # expect 401/405 without auth
HTTP_TIMEOUT = 5.0
PING_TIMEOUT = 4.0  # covers TLS handshake + HTTP round-trip
FETCH_INTERVAL = 30.0  # seconds between background fetches
# VERSION from shared.py
USER_AGENT = f"cc-aio-mon-pulse/{VERSION} (+https://github.com/iM3SK/cc-aio-mon)"
MAX_RESPONSE_BYTES = 512 * 1024  # 512 KB cap on status.json response

# Scrub proxy env vars for urllib.request.urlopen calls. Mirrors the env-scrub
# rationale in shared._GIT_ENV_WHITELIST: a pre-injected HTTP(S)_PROXY should
# not silently route Pulse fetches through an attacker-controlled intermediary.
# install_opener is process-global, but urlopen is only used by pulse.py in this
# codebase (verified by grep).
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)

# Persistence + cleanup
LOG_PATH = DATA_DIR / "pulse.jsonl"
LOG_MAX_BYTES = 1_048_576        # 1 MB — aligned with shared.MAX_FILE_SIZE
LOG_AGE_CUTOFF = 24 * 3600       # startup: drop entries older than 24h
LOG_TRIM_TARGET = 500            # runtime rotation: keep last N lines
LOG_STARTUP_CAP = 2000           # startup: hard cap on record count
ROTATE_CHECK_EVERY = 100         # runtime: check size every N appends

# Rolling window (Tier 2 — smoothing)
SCORE_HISTORY_LEN = 10           # samples kept for median smoothing
SCORE_MEDIAN_WINDOW = 5          # median taken over last N samples
LATENCY_HISTORY_LEN = 60         # ~30 min of latency samples at 30s interval
MIN_SAMPLES_FOR_SMOOTHING = 3    # below this, pass raw score through

# Indicator weights (0-100 scale)
_INDICATOR_SCORE = {
    "none": 100,
    "maintenance": 80,
    "minor": 60,
    "major": 25,
    "critical": 0,
}

# Impact weights for incidents (points deducted from incident subscore)
_IMPACT_DEDUCT = {
    "none": 5,
    "maintenance": 10,
    "minor": 20,
    "major": 40,
    "critical": 60,
}

# Score weights
_W_INDICATOR = 0.50
_W_INCIDENTS = 0.30
_W_LATENCY = 0.20

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_snapshot = {
    "t": 0.0,
    "wall_t": 0.0,
    "score": None,
    "raw_score": None,
    "verdict": "AWAITING DATA",
    "level": "error",
    "reason": "no data yet",
    "indicator": None,
    "incidents": [],
    "components": [],
    "latency_ms": None,
    "latency_p50_ms": None,
    "latency_p95_ms": None,
    "error": None,
}
_snapshot_lock = threading.Lock()
_worker_started = False
_worker_lock = threading.Lock()

# Rolling history for smoothing (Tier 2)
_score_history = deque(maxlen=SCORE_HISTORY_LEN)
_latency_history = deque(maxlen=LATENCY_HISTORY_LEN)
_history_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _latency_score(latency_ms):
    """Map latency to 0-100. None/timeout -> 0."""
    if latency_ms is None:
        return 0
    if latency_ms < 300:
        return 100
    if latency_ms < 800:
        return 70
    if latency_ms < 2000:
        return 40
    return 10


def _indicator_label(indicator):
    m = {
        "none": "operational",
        "maintenance": "maintenance",
        "minor": "minor issues",
        "major": "major outage",
        "critical": "critical outage",
    }
    return m.get(indicator or "", "unknown")


def _score_to_verdict(score):
    """Map numeric score (0-100 or None) to (verdict, level). Pure fn."""
    if score is None:
        return "AWAITING DATA", "error"
    if score >= 80:
        return "SAFE TO CODE", "ok"
    if score >= 50:
        return "DEGRADED -- proceed with caution", "degraded"
    return "NOT SAFE TO CODE", "bad"


def _smoothed_score(raw_score):
    """Append raw_score to history, return median of last SCORE_MEDIAN_WINDOW samples.

    Below MIN_SAMPLES_FOR_SMOOTHING, returns raw_score unchanged (not enough data).
    None input is not recorded and returns None.
    """
    if raw_score is None:
        return None
    with _history_lock:
        _score_history.append(raw_score)
        window = list(_score_history)[-SCORE_MEDIAN_WINDOW:]
    if len(window) < MIN_SAMPLES_FOR_SMOOTHING:
        return raw_score
    return int(round(statistics.median(window)))


def _record_latency(latency_ms):
    """Append latency sample (or None) to history."""
    with _history_lock:
        _latency_history.append(latency_ms)


def _latency_percentiles():
    """Return (p50, p95) over recent successful samples, or (None, None)."""
    with _history_lock:
        samples = [x for x in _latency_history if x is not None]
    if len(samples) < 3:
        return None, None
    samples.sort()
    p50 = statistics.median(samples)
    # p95 index clamped within bounds
    p95_idx = min(len(samples) - 1, max(0, int(round(len(samples) * 0.95)) - 1))
    return int(p50), int(samples[p95_idx])


def _reset_history():
    """Clear rolling history. Test-only helper."""
    with _history_lock:
        _score_history.clear()
        _latency_history.clear()


def compute_score(raw):
    """Pure scoring fn. Input: raw fetch dict. Output: snapshot dict (partial).

    raw = {
        "indicator": str | None,
        "incidents": list[dict],
        "components": list[dict],
        "latency_ms": float | None,
        "error": str | None,
    }
    """
    err = raw.get("error")
    if err:
        return {
            "score": None,
            "verdict": "PULSE ERROR",
            "level": "error",
            "reason": err,
        }

    indicator = raw.get("indicator") or "none"
    incidents = raw.get("incidents") or []
    latency_ms = raw.get("latency_ms")

    ind_score = _INDICATOR_SCORE.get(indicator, 50)

    # Incident subscore: start at 100, deduct per active incident impact.
    inc_score = 100
    for inc in incidents:
        impact = (inc.get("impact") or "minor").lower()
        inc_score -= _IMPACT_DEDUCT.get(impact, 20)
    inc_score = max(0, inc_score)

    lat_score = _latency_score(latency_ms)

    score = int(round(
        ind_score * _W_INDICATOR +
        inc_score * _W_INCIDENTS +
        lat_score * _W_LATENCY
    ))
    score = max(0, min(100, score))

    verdict, level = _score_to_verdict(score)

    # Reason: pick dominant factor
    reasons = []
    if indicator and indicator != "none":
        reasons.append(f"status: {_indicator_label(indicator)}")
    if incidents:
        reasons.append(f"{len(incidents)} active incident(s)")
    if latency_ms is None:
        reasons.append("API ping failed")
    elif latency_ms >= 2000:
        reasons.append(f"high latency {int(latency_ms)}ms")
    elif latency_ms >= 800:
        reasons.append(f"elevated latency {int(latency_ms)}ms")
    reason = "; ".join(reasons) if reasons else "all systems nominal"

    return {
        "score": score,
        "verdict": verdict,
        "level": level,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def _fetch_summary():
    """Fetch status.claude.com summary.json. Returns (data, error_tag)."""
    req = urllib.request.Request(SUMMARY_URL, headers={"User-Agent": USER_AGENT})
    try:
        # SUMMARY_URL is a hardcoded HTTPS constant; proxy env scrubbed via install_opener
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # nosec B310
            # Guard against oversized responses
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return None, "response too large"
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError) as e:
                return None, f"parse: {type(e).__name__}"
            return data, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            return None, "timeout"
        if isinstance(reason, socket.gaierror):
            return None, "DNS fail"
        return None, f"net: {type(reason).__name__}"
    except socket.timeout:
        return None, "timeout"
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}"


def _ping_api():
    """HTTPS probe to api.anthropic.com/v1/messages — returns latency_ms or None.

    Measures TLS handshake + HTTP round-trip (real edge latency, not just TCP).
    Any HTTP status = endpoint responsive (401/405 expected without auth).
    Network/TLS errors = None (real problem).
    """
    req = urllib.request.Request(
        PROBE_URL, method="GET",
        headers={"User-Agent": USER_AGENT},
    )
    start = time.monotonic()
    try:
        # PROBE_URL is a hardcoded HTTPS constant; proxy env scrubbed via install_opener
        with urllib.request.urlopen(req, timeout=PING_TIMEOUT) as resp:  # nosec B310
            resp.read(1)  # drain minimally; context manager guarantees close on exit
    except urllib.error.HTTPError:
        # Any HTTP status = endpoint responded (401/405/429 all fine as liveness signal)
        return (time.monotonic() - start) * 1000.0
    except (urllib.error.URLError, socket.timeout, OSError):
        return None
    return (time.monotonic() - start) * 1000.0


# Regex patterns to detect affected model(s) in incident titles.
# Word-boundary match to avoid false positives (e.g. "opus" in unrelated words).
_MODEL_PATTERNS = {
    "opus":   re.compile(r"\bopus\b",   re.IGNORECASE),
    "sonnet": re.compile(r"\bsonnet\b", re.IGNORECASE),
    "haiku":  re.compile(r"\bhaiku\b",  re.IGNORECASE),
}


def _tag_models_from_incident(inc):
    """Return sorted model tags for an incident.

    Prefers incidents[].components[] array (canonical Statuspage schema).
    Falls back to regex on title + first incident_update body (legacy).
    """
    tags = set()
    for comp in (inc.get("components") or []):
        if isinstance(comp, dict):
            name = (comp.get("name") or "").lower()
            for fam in ("opus", "sonnet", "haiku"):
                if re.search(rf"\b{fam}\b", name):
                    tags.add(fam)
    if tags:
        return sorted(tags)
    # Legacy fallback — regex on title + first update body
    title = (inc.get("name") or "").lower()
    body = ""
    updates = inc.get("incident_updates") or []
    if isinstance(updates, list) and updates and isinstance(updates[0], dict):
        body = (updates[0].get("body") or "").lower()
    impact_override = inc.get("impact_override")
    impact_override_s = str(impact_override).lower() if isinstance(impact_override, (str, int, float)) else ""
    text = f"{title} {impact_override_s} {body}"
    for fam in ("opus", "sonnet", "haiku"):
        if re.search(rf"\b{fam}\b", text):
            tags.add(fam)
    return sorted(tags)


def _extract(summary):
    """Pull indicator + components + incidents from summary.json structure."""
    if not isinstance(summary, dict):
        raise KeyError("summary is not an object")
    status = summary.get("status") or {}
    indicator = status.get("indicator")
    components = []
    for c in summary.get("components") or []:
        if not isinstance(c, dict):
            continue
        # Skip group rollups that have components of their own (avoid double count)
        if c.get("group"):
            continue
        name = str(c.get("name") or "?")[:40]
        cstatus = str(c.get("status") or "unknown")[:24]
        components.append({"name": name, "status": cstatus})
    incidents = []
    for inc in summary.get("incidents") or []:
        if not isinstance(inc, dict):
            continue
        name = str(inc.get("name") or "?")[:80]
        impact = str(inc.get("impact") or "minor")[:16]
        incidents.append({
            "name": name,
            "impact": impact,
            "affected_models": _tag_models_from_incident(inc),
        })
    return indicator, components, incidents


# ---------------------------------------------------------------------------
# Persistence + cleanup (pulse.jsonl in DATA_DIR)
# ---------------------------------------------------------------------------
_write_counter = 0
_log_lock = threading.Lock()


def _atomic_replace_log(lines):
    """Atomically rewrite LOG_PATH with `lines` (iterable of strings with \\n)."""
    if not ensure_data_dir(DATA_DIR):
        return
    fd = None
    try:
        fd = tempfile.NamedTemporaryFile(
            dir=str(DATA_DIR), suffix=".tmp", delete=False,
            mode="w", encoding="utf-8",
        )
        fd.writelines(lines)
        fd.close()
        pathlib.Path(fd.name).replace(LOG_PATH)
    except OSError:
        if fd is not None:
            try:
                fd.close()
            except OSError:
                pass
            try:
                os.unlink(fd.name)
            except OSError:
                pass


def cleanup_log_startup():
    """Startup cleanup: drop entries older than LOG_AGE_CUTOFF and cap record count.

    Idempotent. Safe on missing file / malformed lines / permission errors.
    """
    try:
        if not LOG_PATH.exists():
            return
    except OSError:
        return
    cutoff = time.time() - LOG_AGE_CUTOFF
    # Bounded read — hard ceiling 2× LOG_MAX_BYTES protects against malicious growth
    raw = safe_read(LOG_PATH, LOG_MAX_BYTES * 2)
    if raw is None:
        return
    kept = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # drop malformed
        ts = rec.get("ts", 0)
        if isinstance(ts, (int, float)) and ts >= cutoff:
            kept.append(line + "\n")
    if len(kept) > LOG_STARTUP_CAP:
        kept = kept[-LOG_STARTUP_CAP:]
    with _log_lock:
        _atomic_replace_log(kept)


def _maybe_rotate_log():
    """Runtime guard: every ROTATE_CHECK_EVERY writes, trim if file > LOG_MAX_BYTES."""
    global _write_counter
    _write_counter += 1
    if _write_counter % ROTATE_CHECK_EVERY != 0:
        return
    try:
        size = LOG_PATH.stat().st_size
    except OSError:
        return
    if size <= LOG_MAX_BYTES:
        return
    with _log_lock:
        # Bounded read — hard ceiling 2× LOG_MAX_BYTES, even if file grew between stat and read
        raw = safe_read(LOG_PATH, LOG_MAX_BYTES * 2)
        if raw is None:
            return
        lines = [ln + "\n" for ln in raw.decode("utf-8", errors="replace").splitlines()]
        trimmed = lines[-LOG_TRIM_TARGET:]
        _atomic_replace_log(trimmed)


def _append_log(snap):
    """Append one pulse record to LOG_PATH. Silent on error — persistence is best-effort."""
    if not ensure_data_dir(DATA_DIR):
        return
    # Persist raw_score (truth) — smoothed score is a UI concern only
    rec = {
        "ts": snap.get("wall_t") or time.time(),
        "score": snap.get("raw_score") if snap.get("raw_score") is not None else snap.get("score"),
        "level": snap.get("level"),
        "indicator": snap.get("indicator"),
        "incidents": len(snap.get("incidents") or []),
        "latency_ms": snap.get("latency_ms"),
        "error": snap.get("error"),
    }
    try:
        line = json.dumps(rec) + "\n"
    except (TypeError, ValueError):
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        return
    _maybe_rotate_log()


# ---------------------------------------------------------------------------
# Snapshot orchestration
# ---------------------------------------------------------------------------
def _refresh_once():
    """Perform one fetch + ping cycle and update the snapshot."""
    summary, fetch_err = _fetch_summary()
    latency = _ping_api()

    if fetch_err:
        raw = {
            "indicator": None,
            "incidents": [],
            "components": [],
            "latency_ms": latency,
            "error": fetch_err,
        }
    else:
        try:
            indicator, components, incidents = _extract(summary)
        except (KeyError, TypeError, ValueError) as e:
            raw = {
                "indicator": None,
                "incidents": [],
                "components": [],
                "latency_ms": latency,
                "error": f"parse: {type(e).__name__}",
            }
        else:
            raw = {
                "indicator": indicator,
                "incidents": incidents,
                "components": components,
                "latency_ms": latency,
                "error": None,
            }

    scored = compute_score(raw)
    raw_score = scored["score"]

    # Record latency sample (even if None for error bookkeeping of successful vs failed pings)
    _record_latency(raw["latency_ms"])

    # Apply rolling median smoothing to the score
    smoothed = _smoothed_score(raw_score)

    # Derive display verdict from smoothed score (falls back to raw verdict on error)
    if raw_score is None:
        display_score = None
        verdict = scored["verdict"]
        level = scored["level"]
    else:
        display_score = smoothed
        verdict, level = _score_to_verdict(smoothed)

    p50, p95 = _latency_percentiles()

    new_snap = {
        "t": time.monotonic(),
        "wall_t": time.time(),
        "score": display_score,
        "raw_score": raw_score,
        "verdict": verdict,
        "level": level,
        "reason": scored["reason"],
        "indicator": raw["indicator"],
        "incidents": raw["incidents"],
        "components": raw["components"],
        "latency_ms": raw["latency_ms"],
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "error": raw["error"],
    }
    with _snapshot_lock:
        _snapshot.update(new_snap)
    _append_log(new_snap)


def _worker_loop():
    while True:
        try:
            _refresh_once()
        except Exception:  # last-resort guard — daemon must not die
            with _snapshot_lock:
                _snapshot.update({
                    "t": time.monotonic(),
                    "wall_t": time.time(),
                    "score": None,
                    "verdict": "PULSE ERROR",
                    "level": "error",
                    "reason": "worker crashed",
                    "error": "worker crash",
                })
        time.sleep(FETCH_INTERVAL)


def start_pulse_worker():
    """Start the background fetcher. Idempotent.

    Runs startup cleanup on pulse.jsonl before launching the worker thread.
    """
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    # Startup cleanup — drop stale entries (>24h) and cap record count
    try:
        cleanup_log_startup()
    except Exception:  # pragma: no cover — never block worker start on cleanup
        pass
    try:
        t = threading.Thread(target=_worker_loop, name="pulse-worker", daemon=True)
        t.start()
    except Exception:
        # Thread() or start() failed — revert the started flag so a later call can retry
        with _worker_lock:
            _worker_started = False
        raise


def get_pulse_snapshot():
    """Return a shallow copy of the latest snapshot."""
    with _snapshot_lock:
        return dict(_snapshot)
