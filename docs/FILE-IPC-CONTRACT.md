# FILE-IPC CONTRACT: cc-aio-mon v1.12.4

**Status**: Active  
**Version**: 1.12.4 (`SCHEMA_VERSION` = 1)  
**Last Updated**: 2026-05-30  
**Source Truth**: `shared.py`, `statusline.py`, `monitor.py`, `pulse.py`

See also: [ARCHITECTURE.md](ARCHITECTURE.md) for module overview, [RELEASE.md](RELEASE.md) for release process.

## Overview

Two long-running processes communicate solely via the filesystem—no sockets, pipes, or shared memory:

- **`statusline.py`** (Writer): Short-lived invocations per Claude Code event. Reads JSON from stdin, outputs 1-line ANSI status, writes IPC files, exits.
- **`monitor.py`** (Reader & pulse writer): 1 interactive process (singleton lock enforced). Polls IPC files every 500 ms. Runs in-process pulse worker (fetches `status.claude.com` every 30s, writes to `pulse.jsonl`).

**Contract Invariants**:
- No fsync; relies on OS atomic rename (`Path.replace()`)
- All files in single directory: `$TMPDIR/claude-aio-monitor/`
- Session ID validation at entry (regex + reserved list)
- History trimmed at 1 MB (JSONL append-only)
- All I/O is best-effort; OSError silently swallowed (not raised to caller)

---

## Base Directory

### Path Computation

| OS | Formula |
|---|---|
| Unix / macOS | `tempfile.gettempdir() / "claude-aio-monitor"` |
| Windows | `C:\Users\<user>\AppData\Local\Temp\claude-aio-monitor` |
| WSL / MSYS | System `tempfile.gettempdir()` (usually `/tmp`) |

> **Note**: `$TMPDIR` in file path examples throughout this document resolves per-platform via Python's `tempfile.gettempdir()`:
> - Linux: typically `/tmp`
> - macOS: per-user `/var/folders/<hash>/T/`
> - Windows: `%TEMP%` (e.g. `C:\Users\<user>\AppData\Local\Temp\`)

**Constants** (source: `shared.py`):
- `DATA_DIR_NAME` = `"claude-aio-monitor"`
- `DATA_DIR` = `pathlib.Path(tempfile.gettempdir()) / DATA_DIR_NAME`

### Creator

**Function**: `ensure_data_dir(d)` (`shared.py`)

| Attribute | Value |
|---|---|
| Creates dir | Yes, via `mkdir(mode=0o700, exist_ok=True)` |
| Unix permissions | 0o700 (rwx------); chmod'd even if umask restricts |
| Windows permissions | Inherited from parent (default NTFS ACL) |
| Return value | `True` if created/verified; `False` if symlink/junction/unsafe |
| Idempotent | Yes (mkdir exists_ok=True) |

**Safety Check**: `is_safe_dir(p)` (`shared.py`)

Rejects symlinks and reparse points (Windows junctions). Uses `lstat()` (TOCTOU-resistant):
- Unix: `not S_ISDIR(st_mode) → False`
- Windows: `st_file_attributes & 0x400 (FILE_ATTRIBUTE_REPARSE_POINT) → False`

Callers must call `ensure_data_dir()` once at startup, then all file operations validate the dir via `is_safe_dir()`.

---

## Session ID Constraints

### Regex Pattern

**Source**: `shared.py`

```python
_SID_RE = re.compile(
    r"^(?!(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$)[a-zA-Z0-9_\-]{1,128}$"
)
```

| Constraint | Details |
|---|---|
| Character set | `[a-zA-Z0-9_-]` (alphanumeric, underscore, hyphen) |
| Length | 1–128 chars (inclusive) |
| Windows reserved devices | Case-insensitive negative lookahead: `CON`, `PRN`, `AUX`, `NUL`, `COM0–9`, `LPT0–9` |
| Rationale | On Windows, opening `CON.json` opens the console device, not a file |

### Reserved Session IDs

**Source**: `shared.py`

```python
RESERVED_SIDS = frozenset({"rls", "stats", "pulse"})
```

These are reserved for internal use:
- `rls`: release/update checking state (monitor only)
- `stats`: aggregated cost statistics (monitor only)
- `pulse`: Anthropic status monitor snapshots (pulse.py only)

**Validation**: `statusline.write_shared_state()` and `monitor.load_state()` return early if `sid in RESERVED_SIDS`.

### Invalid SID Handling

| Function | Invalid SID Behavior |
|---|---|
| `statusline.write_shared_state()` | Silently returns (no IPC file written) |
| `monitor.load_state()` | Returns `None` |
| `monitor.list_sessions()` | Skips entry entirely (not included in returned list) |
| `shared.load_history()` | Returns `[]` (empty history) |

---

## File 1: Session Snapshot — `<sid>.json`

### Purpose

Frozen point-in-time view of Claude Code status: model, context window %, cost, rate limits. Monitor polls this every 500 ms in the main event loop to refresh the TUI.

### Full Path

`<DATA_DIR>/<sid>.json` where `<sid>` matches `_SID_RE`.

Example: `$TMPDIR/claude-aio-monitor/default.json`

### Writing (statusline.py)

**Function**: `write_shared_state(data: dict)` (`statusline.py`)

1. Validate session ID via `_SID_RE`
2. Ensure data directory (exit early if `not ensure_data_dir()`)
3. Serialize `data` dict + metadata as single JSON object
4. Write atomically: `NamedTemporaryFile` in same dir → `Path.replace(target)` (atomic rename, no fsync)
5. On disk-full or permission error: silently return (no exception raised)
6. If snapshot write succeeds: append same entry + `"t": time.time()` to history JSONL

**Atomic Write Pattern**:

```python
fd = tempfile.NamedTemporaryFile(
    dir=base, suffix=".tmp", delete=False, mode="w", encoding="utf-8"
)
fd.write(snapshot)
fd.close()
pathlib.Path(fd.name).replace(target)  # atomic on all platforms
```

**Cross-platform atomicity**:
- Unix: `rename()` is atomic
- Windows: `Path.replace()` is atomic (internally uses `ReplaceFileW` if available, fallback to delete + rename)

### Reading (monitor.py)

**Function**: `load_state(sid)` (`monitor.py`)

```python
def load_state(sid):
    if not _SID_RE.match(str(sid)):
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
    # Refuse a snapshot tagged newer than this build understands (schema gate).
    if (isinstance(d, dict) and isinstance(d.get("_schema_version"), int)
            and d["_schema_version"] > SCHEMA_VERSION):
        return None
    return d
```

- Validates `sid` and directory safety
- Bounded read: `safe_read()` caps at `MAX_FILE_SIZE` (1 MB)
- Returns parsed dict or `None` on error
- **No exception raised** on malformed JSON (returns `None`)
- **Schema gate**: a `_schema_version` newer than `shared.SCHEMA_VERSION` degrades to `None`

**Poll Cadence**: The main loop polls `load_state()` for each active session on every data-load interval.

### JSON Schema

**Snapshot Field Map** (merged from Claude Code status protocol + metadata):

| Field | Type | Source | Description |
|---|---|---|---|
| `_schema_version` | int | statusline.write_shared_state() | Contract version (current: 1); added at write time |
| `session_id` | str | Claude Code (stdin) | Session name / UUID passed by CC; validated & defaulted to "default" if invalid |
| `model` | dict | CC | Model metadata: `{display_name, name, ...}` |
| `model.display_name` | str | CC | Human-readable model name, e.g. "Opus 4.7 (1M context)" |
| `model.name` | str | CC | Internal model ID, e.g. "claude-opus-4-1-20250805" |
| `context_window` | dict | CC | `{context_window_size: int, used_percentage: 0–100, ...}` |
| `context_window.context_window_size` | int | CC | Total context tokens available (e.g., 200000) |
| `context_window.used_percentage` | float | CC | 0–100; monitor displays as "CTX NN%" |
| `cost` | dict | CC | `{total_cost_usd: float, ...}` |
| `cost.total_cost_usd` | float | CC | USD spent in this session (cumulative) |
| `rate_limits` | dict | CC | Rate limit buckets: `{five_hour: {...}, seven_day: {...}}` |
| `rate_limits.five_hour` | dict | CC | 5-hour rolling rate limit: `{used_percentage: 0–100, resets_at: epoch}` |
| `rate_limits.five_hour.used_percentage` | float | CC | 0–100 |
| `rate_limits.five_hour.resets_at` | float | CC | Unix epoch (seconds) when limit resets |
| `rate_limits.seven_day` | dict | CC | 7-day rolling rate limit (same schema as five_hour) |
| `rate_limits.seven_day.used_percentage` | float | CC | 0–100 |
| `rate_limits.seven_day.resets_at` | float | CC | Unix epoch (seconds) when limit resets |
| `t` | **not present in snapshot** | — | History only; added when appending to JSONL |

**Forward Compatibility**: Monitor uses `dict.get()` (never KeyError) so unknown fields in future snapshots are silently ignored.

### Schema Version

**Current Value**: 1 (`shared.SCHEMA_VERSION`)

**Semantics**:
- Added to snapshot at write time by statusline: `{..., "_schema_version": 1}`
- `monitor.load_state()` **gates** on it: a snapshot tagged NEWER than the running
  build degrades to `None` (treated as unreadable) rather than risk a misread of
  an incompatible shape
- Missing or older tags default to `0` (pre-v1.10 snapshots) and stay readable
- Bumped when the JSON shape changes incompatibly (e.g., rename field, change nesting)

### File Size & Disk Full

| Scenario | Behavior |
|---|---|
| Snapshot + history write succeeds | Both files updated atomically (snapshot first, history after) |
| Disk full during temp write | `OSError` caught; temp file cleaned up; no main file touched |
| Disk full after replace (post-rename) | Snapshot persists; history append fails silently; snapshot_ok flag still True so history write is attempted |
| File > MAX_FILE_SIZE (1 MB) | Snapshot itself is just one JSON object (rarely > 1 KB); history file is trimmed separately |

---

## File 2: Session History — `<sid>.jsonl`

### Purpose

Append-only sequence of all snapshots. Used by both statusline (for BRN/CTR rate computation) and monitor (for cost trend visualization + RLS decision-making).

### Full Path

`<DATA_DIR>/<sid>.jsonl` where `<sid>` matches `_SID_RE`.

Example: `$TMPDIR/claude-aio-monitor/default.jsonl`

### Writing (statusline.py)

**Function**: `write_shared_state()` (`statusline.py`; the surrounding alignment guard at `statusline.py` aborts the append if the snapshot write failed)

After successful snapshot write:

1. Append one JSON object per line (JSONL format)
2. Each line: `{**snapshot_dict, "_schema_version": SCHEMA_VERSION, "t": time.time()}`
3. No fsync (relies on OS buffering + rename for atomicity)
4. Check file size after write; if `> MAX_FILE_SIZE`, call `_trim_history()`

**Trim Policy** (`statusline.py`):

- **Trigger**: File size > 1 MB (`MAX_FILE_SIZE`)
- **Trim target**: Last 1000 lines (`HISTORY_TRIM_TO`)
- **Atomic rewrite**: Temp file → replace (same pattern as snapshot)
- **Malformed line handling**: Lines that fail `json.loads()` are silently dropped during trim
- **Best-effort**: OSError during trim is caught; original file left alone

### Reading (shared.py + monitor.py)

**Function**: `load_history(sid, n=HISTORY_RATE_SAMPLES, data_dir=None)` (`shared.py`)

Single source of truth for both statusline and monitor. `HISTORY_RATE_SAMPLES`
defaults to 120 — at ~1 statusline event/min this is a ~2-hour rolling window.

```python
def load_history(sid, n=HISTORY_RATE_SAMPLES, data_dir=None):
    """Read last n JSONL history entries for session `sid`.
    
    Returns list of parsed dicts (best-effort — malformed lines skipped).
    Returns [] on invalid SID, unsafe data dir, or read failure.
    """
    dd = data_dir if data_dir is not None else DATA_DIR
    sid_s = str(sid)
    if not _SID_RE.match(sid_s) or sid_s in RESERVED_SIDS:
        return []
    if not is_safe_dir(dd):
        return []
    raw = safe_read(dd / f"{sid_s}.jsonl", HISTORY_READ_MAX)
    if raw is None:
        return []
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out
```

**Bounded Read**: `HISTORY_READ_MAX = MAX_FILE_SIZE * 2 = 2 MB`

- Protects against unbounded file growth (e.g., TOCTOU race where file is resized between stat and read)
- Returns `None` if file exceeds 2 MB

**Encoding**: UTF-8 strict. A `UnicodeDecodeError` at the whole-file decode step makes `load_history` return `[]` rather than substituting replacement chars — corrupt history is treated as empty history so rate calculations see no spurious data.

**Malformed Line Handling**: Lines that fail `json.loads()` are silently skipped (not appended to output).

### JSONL Schema (Per-Line)

Each line is a JSON object with these fields:

| Field | Type | Set by | Description |
|---|---|---|---|
| All snapshot fields... | ... | statusline | See Snapshot schema above |
| `_schema_version` | int | statusline | Contract version (current: 1) |
| `t` | float | statusline | Unix timestamp (seconds); `time.time()` at write |

**Example Line**:
```json
{"session_id":"default","model":{"display_name":"Opus 4.7","name":"claude-opus-4-1-20250805"},"context_window":{"context_window_size":200000,"used_percentage":45.5},"cost":{"total_cost_usd":0.15},"rate_limits":{"five_hour":{"used_percentage":10,"resets_at":1716379200},"seven_day":{"used_percentage":5,"resets_at":1716639600}},"_schema_version":1,"t":1716292800.123}
```

### File Size Management

| Constant | Value | Purpose |
|---|---|---|
| `MAX_FILE_SIZE` | 1 MB (1048576) | Trim trigger threshold |
| `HISTORY_READ_MAX` | 2 MB (2097152) | Bounded read cap (1 MB + 1 MB headroom) |
| `HISTORY_TRIM_TO` | 1000 lines | Target line count after trim |

**Trim Workflow**:
1. Write appends new JSONL line
2. Check `path.stat().st_size > MAX_FILE_SIZE`
3. If yes: read file (max 2 MB), split to lines, keep last 1000, rewrite atomically
4. On read/write error: silently return (file left alone)

---

## File 3: Pulse History — `pulse.jsonl`

### Purpose

Persistent log of Anthropic API stability samples (fetched every 30 sec). Used by monitor to display "API Status" section + trend graphs.

### Full Path

`<DATA_DIR>/pulse.jsonl`

Example: `$TMPDIR/claude-aio-monitor/pulse.jsonl`

### Writing (pulse.py)

**Function**: `_append_log(snap)` (`pulse.py`)

Called by `_refresh_once()` after each fetch + ping cycle.

Records a condensed entry (raw score, not smoothed):

```python
rec = {
    "ts": snap.get("wall_t") or time.time(),      # wall-clock timestamp
    "score": snap.get("raw_score") if ... else snap.get("score"),  # 0–100 or None
    "level": snap.get("level"),      # "ok" | "degraded" | "bad" | "error"
    "indicator": snap.get("indicator"),  # "none" | "maintenance" | "minor" | "major" | "critical"
    "incidents": len(snap.get("incidents") or []),  # incident count
    "latency_ms": snap.get("latency_ms"),  # float | None (HTTPS round-trip)
    "error": snap.get("error"),      # error tag if fetch failed ("HTTP 503", "timeout", etc.)
}
line = json.dumps(rec) + "\n"
```

**Append Pattern** (`pulse.py`):
```python
with open(LOG_PATH, "a", encoding="utf-8") as f:
    f.write(line)
```

No temp file; appends directly. Rotation guard called after every `ROTATE_CHECK_EVERY` writes.

### Retention Policy

**Startup Cleanup** (`pulse.py`):
- Drops entries older than `LOG_AGE_CUTOFF = 24 hours`
- Hard cap: max `LOG_STARTUP_CAP = 2000` records kept
- Called once in `start_pulse_worker()` before launching worker thread

**Runtime Rotation** (`pulse.py`):
- Every `ROTATE_CHECK_EVERY = 100` appends, check file size
- If `file.stat().st_size > LOG_MAX_BYTES (1 MB)`: trim to last `LOG_TRIM_TARGET = 500` lines
- Bounded read (2 MB cap) protects against TOCTOU growth

### JSONL Schema (Per-Line)

| Field | Type | Description |
|---|---|---|
| `ts` | float | Wall-clock Unix timestamp (seconds); `time.time()` |
| `score` | int \| None | 0–100 stability score (raw, not smoothed); None on error |
| `level` | str | "ok" \| "degraded" \| "bad" \| "error" |
| `indicator` | str | "none" \| "maintenance" \| "minor" \| "major" \| "critical" |
| `incidents` | int | Count of active incidents on status page |
| `latency_ms` | float \| None | API ping latency (TLS + HTTP round-trip); None if timeout/error |
| `error` | str \| None | Error tag if fetch failed; None if success |

**Sampling Interval**: ~30 sec (`FETCH_INTERVAL`)

**Example Line**:
```json
{"ts":1716292800.123,"score":95,"level":"ok","indicator":"none","incidents":0,"latency_ms":125.4,"error":null}
```

---

## File 4: Monitor Crash Log — `monitor-crash.log`

### Purpose

Post-mortem diagnosis of unhandled exceptions in the monitor process. Survives outside the alt-screen buffer so users can inspect after a crash.

### Full Path

`<DATA_DIR>/monitor-crash.log`

Example: `$TMPDIR/claude-aio-monitor/monitor-crash.log`

### Writing (monitor.py)

**Function**: `_install_crash_logger()` (`monitor.py`)

Installed once at startup via `sys.excepthook = excepthook`. Captures any uncaught exception. (Snippet below is abbreviated for readability — the real implementation also writes platform / Python version / encoding metadata before the traceback.)

```python
def excepthook(exc_type, exc_value, tb):
    try:
        if ensure_data_dir(DATA_DIR):
            log_path = DATA_DIR / "monitor-crash.log"
            rotate_crash_log(log_path, always=True)  # always preserve prior traceback
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"monitor v{VERSION} crashed at {time.ctime()}\n")
                f.write(f"PID {os.getpid()}\n")
                f.write("\n---\n")
                traceback.print_exception(exc_type, exc_value, tb, file=f)
    except Exception:
        pass  # never break exit on diag failure
    sys.__excepthook__(exc_type, exc_value, tb)
```

### Rotation Policy

**Function**: `rotate_crash_log(path, max_bytes=MAX_FILE_SIZE, always=False)` (`shared.py`)

Two rotation modes, selected by the caller:

- `always=False` (default): size-gated — rotate only when `path.stat().st_size > max_bytes`. Suitable for callers that only care about bounding disk growth.
- `always=True` (used by `_install_crash_logger` since v1.12.2): rotate on every call regardless of size. Used by the crash logger so that two crashes in quick succession do not silently overwrite each other.

Both modes:
1. `path.replace(path.with_suffix(path.suffix + ".1"))` (atomic rename)
2. Drop any pre-existing `.log.1` to keep only 2 entries on disk
3. Best-effort: any OSError silently swallowed

| Scenario (v1.12.2+, monitor crash logger) | Result |
|---|---|
| 1st crash | `monitor-crash.log` created |
| 2nd crash (any size) | Previous traceback preserved as `monitor-crash.log.1`; new crash written to fresh `monitor-crash.log` |
| 3rd crash | Drop old `.log.1`, rename current → `.log.1`, create fresh `monitor-crash.log` |

The crash log retains **the two most recent crashes** (current + `.log.1`). Rotation is now both a disk-growth guard (default behavior) and a diagnostic preservation guarantee (when called with `always=True`).

### Content Format

```
monitor v1.12.0 crashed at Wed May 22 14:30:45 2026
PID 12345

---
Traceback (most recent call last):
  File "monitor.py", line 2500, in main
    ... (full traceback)
...
```

### Reading

Monitor does not read this file; it is for manual post-mortem inspection by the user. Check lock file path in error message points to crash log location.

---

## File 5: Monitor Singleton Lock — `monitor.lock`

### Purpose

Enforce mutual exclusion across two writers: at most one interactive `monitor.py` process at a time, AND `update.py --apply` cannot run while monitor.py is live (it acquires the same lock). `monitor.py --list` mode is exempt (read-only, no lock required).

### Full Path

`<DATA_DIR>/monitor.lock`

Example: `$TMPDIR/claude-aio-monitor/monitor.lock`

### New in v1.12.0

Lock file mechanism introduced to prevent multiple monitors from corrupting the TUI or polling state simultaneously.

### Acquisition

**Function**: `acquire_singleton_lock(lock_path)` (`shared.py`)

```python
def acquire_singleton_lock(lock_path):
    """Try to acquire an exclusive non-blocking file lock.
    
    Returns open file handle on success (caller MUST keep reference).
    Returns None if another process already holds the lock.
    Cross-platform: msvcrt on Windows, fcntl elsewhere.
    Best-effort PID write for human inspection.
    """
    try:
        fh = open(lock_path, "a+")
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt
            # Ensure byte 0 exists for msvcrt.locking() to grab
            try:
                fh.seek(0, 2)  # end of file
                if fh.tell() == 0:
                    fh.write("\0")
                    fh.flush()
                fh.seek(0)
            except OSError:
                pass
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        try:
            fh.close()
        except OSError:
            pass
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass  # lock held; PID write is best-effort
    return fh
```

### Platform-Specific Behavior

| OS | Mechanism | Semantics |
|---|---|---|
| Unix (Linux, macOS) | `fcntl.flock(fh.fileno(), LOCK_EX \| LOCK_NB)` | Exclusive non-blocking lock; OS releases on process exit (including SIGKILL) |
| Windows | `msvcrt.locking(fh.fileno(), LK_NBLCK, 1)` | Locks byte 0 (placeholder written first if file empty); released on handle close or process exit |

**Idempotent Release**: OS automatically releases lock when process exits, even on forceful termination (kill -9). No explicit unlock needed.

### Usage in monitor.py

**Source**: `monitor.py`

```python
if ensure_data_dir(DATA_DIR):
    _SINGLETON_LOCK_HANDLE = acquire_singleton_lock(DATA_DIR / "monitor.lock")
    if _SINGLETON_LOCK_HANDLE is None:
        sys.exit(
            "Error: another monitor.py instance is already running.\n"
            f"Lock file: {DATA_DIR / 'monitor.lock'} (inspect for PID)\n"
            "Close the other instance, or delete the lock file if it is stale."
        )
```

- Called once at startup (before main loop)
- Stored in module-level variable to keep handle alive for process lifetime
- If lock acquisition fails: print error + exit(1)

### PID Write for Human Inspection

After acquiring lock, `acquire_singleton_lock()` writes the current PID to the file:

```python
fh.seek(0)
fh.truncate()
fh.write(str(os.getpid()))
fh.flush()
```

User can inspect: `cat $TMPDIR/claude-aio-monitor/monitor.lock` to see which process ID holds the lock.

### Lock Semantics for `--list` Mode

`--list` is a read-only operation (prints sessions, exits). Bypasses lock acquisition:

```python
if ensure_data_dir(DATA_DIR):
    sessions = list_sessions()  # read-only; no lock
```

Multiple `--list` invocations can run concurrently with monitor or each other.

---

## Atomicity Guarantees

### Snapshot (`<sid>.json`)

| Aspect | Guarantee | Cross-Platform Notes |
|---|---|---|
| Write | Atomic via temp file + `Path.replace()` | Windows: uses `ReplaceFileW` if available; Unix: atomic `rename()` |
| Concurrent reads | Safe during replace (OS handles) | Reader sees old or new, never torn file |
| fsync | **Not used** | Relies on OS buffering + FS guarantees |
| Disk full | Temp file cleaned up; original file untouched | Exception caught; no partial file |

### History JSONL (`<sid>.jsonl`)

| Aspect | Guarantee | Notes |
|---|---|---|
| Append | Non-atomic write to existing file | Concurrent appends may interleave; reader sees any subset of complete lines |
| Trim | Atomic via temp file + replace | Same pattern as snapshot |
| Concurrent reads | Safe; reader may skip partially-written line (ends mid-JSON) | `splitlines()` stops at `\n`; incomplete line dropped by `json.loads()` |
| Line boundaries | Caller ensures each `write(entry + "\n")` | `\n` delimiter is critical for splitter |

### Pulse Log (`pulse.jsonl`)

| Aspect | Guarantee | Notes |
|---|---|---|
| Append | Non-atomic append; direct write | Same as history; any subset of lines visible |
| Rotation | Atomic trim via temp file + replace | Runtime guard every 100 appends |
| Startup cleanup | Atomic rewrite | Called once before worker thread starts |

### Monitor Crash Log (`monitor-crash.log`)

| Aspect | Guarantee | Notes |
|---|---|---|
| Rotate | Atomic rename to `.log.1` | Pre-write check; best-effort on OSError |
| Write | Non-atomic (direct write) | `open(mode="w")` truncates; exception hook never crashes |

### Singleton Lock (`monitor.lock`)

| Aspect | Guarantee | Notes |
|---|---|---|
| Acquire | Atomic on both platforms | fcntl/msvcrt guarantee exclusive hold |
| Release | Automatic on process exit | Includes SIGKILL; no explicit unlock needed |
| PID write | Best-effort | Failure to write PID does not invalidate lock |

---

## Schema Version Evolution

### Current State

- **`SCHEMA_VERSION`** = 1 (`shared.py`)
- **Version in file**: `_schema_version: 1` added to every snapshot & history entry by statusline.py

### Backward Compatibility

Monitor reads via `dict.get("_schema_version")` → defaults to `None` if absent:
- Pre-v1.10 snapshots (no `_schema_version` field) are tolerated
- Unknown fields in snapshots are silently ignored (defensive dict.get)
- Unknown future `_schema_version` values are not yet gated (future-proofing only)

### Forward Compatibility (Future)

When a breaking JSON shape change is needed:
1. Bump `SCHEMA_VERSION` to 2 in `shared.py`
2. Statusline writes `"_schema_version": 2` to new snapshots
3. Monitor adds a version check in `load_state()`:
   ```python
   ver = d.get("_schema_version", 0)
   if ver > CURRENT_SCHEMA_VERSION:
       raise ValueError("snapshot too new for this monitor version")
   ```
4. Document migration path in CHANGELOG

### Deprecation Timeline

No deprecated fields yet. When a field is retired:
1. Keep field in snapshot for 1–2 releases (backward compat)
2. Monitor ignores the field (already using dict.get)
3. Statusline stops writing it (or writes `null`)
4. Remove from schema doc

---

## Cross-Platform Notes

### Windows Considerations

| Issue | Mitigation |
|---|---|
| Reserved device names (CON, PRN, NUL, etc.) | Rejected by `_SID_RE` negative lookahead |
| Path separator (`\` vs `/`) | `pathlib.Path` normalizes automatically |
| Junctions / reparse points | Rejected by `is_safe_dir()` (checks `FILE_ATTRIBUTE_REPARSE_POINT`) |
| Temp dir location | `%LOCALAPPDATA%\Temp\` (typically `C:\Users\<user>\AppData\Local\Temp`) |
| File locking | `msvcrt.locking()` with LK_NBLCK (non-blocking) |
| fsync absence | Relies on NTFS buffering + atomic ReplaceFileW |

### Unix / Linux / macOS

| Issue | Mitigation |
|---|---|
| Permissions | 0o700 (rwx------) enforced on dir; chmod'd even if umask restricts |
| Symlinks | Rejected by `is_safe_dir()` via `lstat()` (TOCTOU-resistant) |
| Temp dir | Usually `/tmp` or environment-specific via `tempfile.gettempdir()` |
| File locking | `fcntl.flock()` with LOCK_EX \| LOCK_NB (exclusive non-blocking) |
| Atomicity | `rename()` system call is atomic (OS guarantee) |

### WSL / MSYS

- `tempfile.gettempdir()` returns system temp (usually `/tmp`)
- Symlink + permission handling follows Unix path
- File locking via fcntl (same as Linux)

---

## Summary: File Manifest

| File | Purpose | Writer(s) | Reader(s) | Atomicity | Lifetime |
|---|---|---|---|---|---|
| `<sid>.json` | Session snapshot | statusline | monitor | Atomic replace | Until session expires (~1h idle in monitor) |
| `<sid>.jsonl` | Session history | statusline | statusline (rates), monitor (trends) | Atomic trim; non-atomic append | Trimmed to 1000 lines @ 1 MB |
| `pulse.jsonl` | API stability log | pulse.py (worker) | monitor (display), external tools | Atomic trim; non-atomic append | Trimmed to 500 lines @ 1 MB; startup cleanup drops >24h entries |
| `monitor-crash.log` | Crash traceback | monitor (excepthook) | User (post-mortem) | None (diagnostic only) | Rotated to `.log.1` on every crash (v1.12.2+); size guard still applies for non-crash callers |
| `monitor.lock` | Singleton lock | monitor | monitor (check at startup) | Atomic fcntl/msvcrt | Process lifetime; auto-released on exit |

---

## Error Cases & Silent Failures

All IPC is best-effort. No exceptions are raised to the user—errors are logged or silently swallowed:

| Operation | Error Behavior |
|---|---|
| `write_shared_state()` on disk full | Snapshot temp cleanup; history append skipped; function returns silently |
| `load_state()` on malformed JSON | Returns `None` (no exception) |
| `load_history()` on partial line | Line skipped; rest of history returned |
| Trim on OSError | Original file left alone; function returns silently |
| Lock acquisition on occupied lock | Print error msg; `sys.exit(1)` |
| Crash log write on OSError | Exception hook swallows error; still prints to stderr |

---

## Glossary

| Term | Definition |
|---|---|
| **Atomic write** | All-or-nothing: file replace via NamedTemporaryFile + Path.replace(); no partial state visible |
| **Best-effort** | Operation may silently fail (OSError caught); no exception raised |
| **JSONL** | JSON Lines format: one JSON object per line, newline-delimited |
| **TOCTOU** | Time-of-check to time-of-use race condition; mitigated by bounded reads + lstat |
| **Reparse point** | Windows junction or symlink (detected via `FILE_ATTRIBUTE_REPARSE_POINT` flag) |
| **Sentinel** | Placeholder byte (written to lock file to give msvcrt.locking() something to grab) |

---

## Index

**Files**
- [Snapshot (`<sid>.json`)](#file-1-session-snapshot--sidjson)
- [History (`<sid>.jsonl`)](#file-2-session-history--sidjsonl)
- [Pulse Log (`pulse.jsonl`)](#file-3-pulse-history--pulsejsonl)
- [Crash Log (`monitor-crash.log`)](#file-4-monitor-crash-log--monitor-crashlog)
- [Lock (`monitor.lock`)](#file-5-monitor-singleton-lock--monitorlock)

**Concepts**
- [Base Directory](#base-directory)
- [Session ID Constraints](#session-id-constraints)
- [Atomicity Guarantees](#atomicity-guarantees)
- [Cross-Platform Notes](#cross-platform-notes)
- [Schema Version Evolution](#schema-version-evolution)
- [Error Handling](#error-cases--silent-failures)

**Functions** (with source)
- `ensure_data_dir()` – `shared.py`
- `is_safe_dir()` – `shared.py`
- `safe_read()` – `shared.py`
- `load_history()` – `shared.py`
- `load_state()` – `monitor.py`
- `write_shared_state()` – `statusline.py`
- `acquire_singleton_lock()` – `shared.py`
- `rotate_crash_log()` – `shared.py`

---

**Document Version**: 1.12.0  
**Last Verified**: 2026-05-22  
**Status**: Production
