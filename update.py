#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""update.py — self-update checker for CC AIO MON.

Usage: python3 update.py           (read-only check)
       python3 update.py --apply   (fetch + pull + verify)

Safe git pull --ff-only with guard rails: detects uncommitted changes,
branch divergence, remote version regression. Cross-platform (Windows: py,
macOS/Linux: python3). Stdlib only.
"""

import argparse
import datetime
import signal
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from shared import (
    VERSION_RE, MAX_FILE_SIZE, _sanitize, run_git as _shared_run_git,
    ensure_utf8_stdout, extract_changelog_entry, PY_FILES, safe_read,
    check_syntax_after_pull, parse_ahead_behind, verify_origin_remote,
    DATA_DIR, ensure_data_dir, acquire_singleton_lock,
)

if sys.platform == "win32":
    import ctypes


# ---------- ANSI colors (Windows VT enable) ----------
def _enable_vt_on_windows():
    """Best-effort VT enable. Returns True when ANSI escapes are safe to emit."""
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        # HANDLE is pointer-sized — without restype, ctypes truncates the
        # return value to c_int on 64-bit Windows and SetConsoleMode gets a
        # corrupted handle (same class of fix as statusline's CONOUT$ branch).
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        h = kernel32.GetStdHandle(-11)
        if h is None or h == ctypes.c_void_p(-1).value:
            return False
        return bool(kernel32.SetConsoleMode(h, 7))
    except Exception:
        return False


def _init_terminal():
    """Set up UTF-8 stdout and VT processing. Called from main() only.
    Returns True when ANSI escapes are safe to emit (gates the color palette
    so a failed VT enable doesn't spray raw escapes into the console)."""
    ensure_utf8_stdout()
    return _enable_vt_on_windows()


# Colors are set lazily after _init_terminal() — defaults for import safety.
# Intentional SSoT exception vs shared.C_* Nord truecolor palette:
# update.py runs before any TUI setup and must remain readable on minimal
# terminals without 24-bit truecolor (e.g. legacy Windows console, error
# recovery scenarios where _enable_vt_on_windows() has not been called yet).
# Basic 16-color ANSI degrades gracefully; shared.C_* would render as garbled
# escape sequences on those terminals. See CONTRIBUTING.md "What to keep in sync".
GRN = YEL = RED = CYN = DIM = R = ""


def ok(msg):   print(f"{GRN}[OK]{R}  {msg}")
def warn(msg): print(f"{YEL}[??]{R}  {msg}")
def err(msg):  print(f"{RED}[!!]{R}  {msg}", file=sys.stderr)
def hdr(msg):  print(f"\n{CYN}== {msg} =={R}")
def note(msg): print(f"     {DIM}{msg}{R}")


REPO_ROOT = Path(__file__).parent.resolve()
MIN_PYTHON = (3, 8)


def run_git(args, capture=True, timeout=30):
    """Run git command in repo root. Returns CompletedProcess."""
    return _shared_run_git(args, cwd=REPO_ROOT, timeout=timeout)


def check_python_version():
    if sys.version_info < MIN_PYTHON:
        err(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
            f"(found {sys.version_info.major}.{sys.version_info.minor})")
        sys.exit(1)


def check_repo():
    r = run_git(["rev-parse", "--is-inside-work-tree"])
    if r.returncode != 0 or r.stdout.strip() != "true":
        err(f"Not a git repository: {REPO_ROOT}")
        sys.exit(1)
    ok(f"Repo: {REPO_ROOT}")


def check_branch():
    r = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = r.stdout.strip()
    if branch == "HEAD":
        err("Detached HEAD — not on any branch")
        sys.exit(1)
    if branch != "main":
        err(f"Not on main branch (current: {branch}) — switch manually first")
        sys.exit(1)
    ok(f"Branch: {branch}")


def check_clean():
    # -uno: ignore untracked files — they don't affect git pull
    r = run_git(["status", "--porcelain", "-uno"])
    if r.stdout.strip():
        err("Working tree has uncommitted changes:")
        print(r.stdout)
        note("Commit or stash your changes before updating:")
        note("  git stash   # (or 'git status' to inspect)")
        sys.exit(1)
    ok("Working tree: clean (tracked files)")


def fetch_remote():
    r = run_git(["fetch", "origin", "main"])
    if r.returncode != 0:
        err("Failed to fetch from origin:")
        print(r.stderr)
        sys.exit(1)
    ok("Fetched from origin")


def get_local_version() -> str:
    source = REPO_ROOT / "shared.py"
    if not source.exists():
        raise RuntimeError("shared.py not found")
    raw = safe_read(source, MAX_FILE_SIZE)
    if raw is None:
        raise RuntimeError(f"shared.py: unreadable or too large (>{MAX_FILE_SIZE} bytes)")
    content = raw.decode("utf-8", errors="replace")
    m = VERSION_RE.search(content)
    if not m:
        raise RuntimeError("VERSION constant not found in shared.py")
    return m.group(1)


def get_remote_version() -> str:
    r = run_git(["show", "origin/main:shared.py"])
    if r.returncode != 0:
        raise RuntimeError("Failed to read remote shared.py")
    m = VERSION_RE.search(r.stdout)
    if not m:
        raise RuntimeError("VERSION constant not found in remote shared.py")
    return m.group(1)


def get_ahead_behind() -> Tuple[int, int]:
    """Return (behind, ahead) relative to origin/main."""
    r = run_git(["rev-list", "--left-right", "--count", "HEAD...origin/main"])
    if r.returncode != 0:
        raise RuntimeError("Failed to compare commits")
    try:
        ahead, behind = parse_ahead_behind(r.stdout)
    except ValueError as e:
        raise RuntimeError(str(e)) from e
    return behind, ahead


def get_new_commits() -> List[str]:
    r = run_git(["log", "--oneline", "HEAD..origin/main"])
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.strip().split("\n") if line]


def get_remote_changelog_entry(version: str) -> Optional[str]:
    r = run_git(["show", "origin/main:CHANGELOG.md"])
    if r.returncode != 0:
        return None
    entry = extract_changelog_entry(r.stdout, version)
    return entry if entry else None


# Mutual exclusion with monitor.py: the interactive monitor holds a singleton
# OS-level lock at ``DATA_DIR / "monitor.lock"`` for its entire TUI lifetime
# (see monitor.main() and shared.acquire_singleton_lock). `update.py --apply`
# rewrites the same .py files monitor imports — if both ran concurrently, git
# pull could replace a module on disk while monitor's threads still hold open
# references, producing partial reads or stale imports. apply_update() must
# therefore acquire the SAME lock before any tag/pull operation; if monitor is
# running it bails out with a friendly message instead of racing it.
def apply_update():
    hdr("Applying update")

    # Singleton lock — fail fast if monitor.py is running. Lock handle stays in
    # function scope; OS releases it when apply_update() returns or sys.exit().
    if ensure_data_dir(DATA_DIR):
        _lock_handle = acquire_singleton_lock(DATA_DIR / "monitor.lock")
        if _lock_handle is None:
            err("Close monitor.py first — another instance is running")
            note(f"Lock file: {DATA_DIR / 'monitor.lock'} (inspect for PID)")
            sys.exit(1)
    else:
        # FILE-IPC contract: --apply replaces the runtime files and must hold
        # the singleton lock — racing a live monitor.py on file replacement
        # can leave a half-updated install. Fail fast instead of best-effort.
        err("Data directory unusable — cannot take the singleton lock.")
        note(f"Data dir: {DATA_DIR} (check permissions/ownership)")
        sys.exit(1)

    # Rollback point — created before pull so user can revert via `git reset --hard <tag>`
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rollback_tag = f"pre-update-{ts}"
    tr = run_git(["tag", rollback_tag])
    if tr.returncode == 0:
        ok(f"Rollback tag: {rollback_tag}")
        note(f"  Recover with: git reset --hard {rollback_tag}")
    else:
        warn(f"Could not create rollback tag (continuing): {_sanitize(tr.stderr.strip())}")

    r = run_git(["pull", "--ff-only", "origin", "main"])
    if r.returncode != 0:
        err(f"git pull failed: {_sanitize(r.stderr or r.stdout or 'unknown error')}")
        note(f"Revert with: git reset --hard {rollback_tag}")
        sys.exit(1)
    if r.stdout.strip():
        for line in r.stdout.strip().split("\n"):
            note(_sanitize(line))
    ok("Pulled latest changes")

    try:
        new_ver = get_local_version()
        # VERSION_RE matches [^"']+ — on-disk shared.py could carry ANSI;
        # sanitize before echoing to terminal.
        ok(f"New VERSION: {_sanitize(new_ver)}")
    except Exception as e:
        warn(f"Could not verify new VERSION: {_sanitize(str(e))}")

    # Syntax check — catch broken updates before user runs monitor.
    # File list + check logic come from shared (single source of truth across
    # update.py CLI and monitor.py TUI worker).
    bad = check_syntax_after_pull(REPO_ROOT)
    if bad:
        warn(f"Syntax errors in: {', '.join(bad)} — update may be broken")
        note(f"Revert with: git reset --hard {rollback_tag}")
    else:
        ok(f"Syntax check passed ({len(PY_FILES)} files)")

    print()
    print(f"{GRN}Update complete.{R}")
    note("Restart Claude Code to pick up the new statusline.py")
    note("Recommended: re-run check-requirements to verify dependencies:")
    note("  macOS/Linux: bash check-requirements.sh")
    note("  Windows:     .\\check-requirements.ps1")


def main():
    global GRN, YEL, RED, CYN, DIM, R
    # SIGPIPE: silent exit when piped to head/less on Unix (no BrokenPipeError traceback)
    if hasattr(signal, "SIGPIPE"):
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except (AttributeError, ValueError):
            pass
    vt_ok = _init_terminal()
    if sys.stdout.isatty() and vt_ok:
        GRN = "\033[32m"; YEL = "\033[33m"; RED = "\033[31m"
        CYN = "\033[36m"; DIM = "\033[2m"; R = "\033[0m"

    parser = argparse.ArgumentParser(
        description="CC AIO MON self-update. Read-only by default.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the update (git pull --ff-only). Without this flag, shows status only.",
    )
    args = parser.parse_args()

    hdr("CC AIO MON — Update Check")
    note("Apply mode: will run 'git pull --ff-only'" if args.apply
         else "Read-only until confirmed.")

    check_python_version()

    hdr("Repository check")
    check_repo()
    check_branch()
    check_clean()

    hdr("Remote check")
    # SEC: pin self-update to the canonical repo — a rewritten origin must not
    # be able to feed the updater foreign code.
    remote_problem = verify_origin_remote(REPO_ROOT)
    if remote_problem:
        err(f"Origin check failed: {remote_problem}")
        note("Self-update pulls only from the canonical repo. "
             "Forks: set CC_AIO_MON_REMOTE to your remote URL.")
        sys.exit(1)
    ok("Origin: canonical remote verified")
    fetch_remote()

    hdr("Version comparison")
    local_ver = get_local_version()
    remote_ver = get_remote_version()
    behind, ahead = get_ahead_behind()

    print(f"     Current:  {local_ver}")
    print(f"     Latest:   {remote_ver}")

    if ahead > 0 and behind == 0:
        warn(f"Local is {ahead} commit(s) ahead of origin/main — cannot downgrade")
        sys.exit(1)
    if behind == 0 and ahead == 0:
        print()
        ok("Already up to date.")
        sys.exit(0)
    if behind > 0 and ahead > 0:
        err(f"Local has diverged from origin/main ({ahead} ahead, {behind} behind) "
            f"— manual merge required")
        sys.exit(1)

    print(f"     Behind:   {behind} commit(s)")

    hdr("New commits")
    for line in get_new_commits():
        print(f"     {line}")

    cl_entry = get_remote_changelog_entry(remote_ver)
    if cl_entry:
        hdr(f"CHANGELOG preview (v{remote_ver})")
        for line in cl_entry.split("\n")[:20]:
            print(f"     {line}")
        note("(see CHANGELOG.md for full details)")

    if not args.apply:
        print()
        note("To apply: python3 update.py --apply  (or 'py update.py --apply' on Windows)")
        sys.exit(0)

    apply_update()


if __name__ == "__main__":
    main()
