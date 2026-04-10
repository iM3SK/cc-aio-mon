#!/usr/bin/env python3
# CC AIO MON — Self-Update Script
# Usage: python3 update.py           (read-only check)
#        python3 update.py --apply   (fetch + pull + verify)
# Cross-platform: Windows (py), macOS/Linux (python3).
# Stdlib only.

import argparse
import re
import subprocess
import sys
from pathlib import Path


# Force UTF-8 on Windows (cp1250/cp1252 can't handle em-dash, box chars)
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.flush()
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8",
                      errors="replace", closefd=False)


# ---------- ANSI colors (Windows VT enable) ----------
def _enable_vt_on_windows():
    if sys.platform == "win32":
        try:
            import ctypes
            h = ctypes.windll.kernel32.GetStdHandle(-11)
            ctypes.windll.kernel32.SetConsoleMode(h, 7)
        except Exception:
            pass

_enable_vt_on_windows()

if sys.stdout.isatty():
    GRN = "\033[32m"; YEL = "\033[33m"; RED = "\033[31m"
    CYN = "\033[36m"; DIM = "\033[2m"; R = "\033[0m"
else:
    GRN = YEL = RED = CYN = DIM = R = ""


def ok(msg):   print(f"{GRN}[OK]{R}  {msg}")
def warn(msg): print(f"{YEL}[??]{R}  {msg}")
def err(msg):  print(f"{RED}[!!]{R}  {msg}", file=sys.stderr)
def hdr(msg):  print(f"\n{CYN}== {msg} =={R}")
def note(msg): print(f"     {DIM}{msg}{R}")


REPO_ROOT = Path(__file__).parent.resolve()
MIN_PYTHON = (3, 8)


def run_git(args, capture=True):
    """Run git command in repo root. Returns CompletedProcess."""
    return subprocess.run(
        ["git"] + args,
        cwd=REPO_ROOT,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


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
    r = run_git(["status", "--porcelain"])
    if r.stdout.strip():
        err("Working tree is not clean:")
        print(r.stdout)
        note("Commit or stash your changes before updating:")
        note("  git stash   # (or 'git status' to inspect)")
        sys.exit(1)
    ok("Working tree: clean")


def fetch_remote():
    r = run_git(["fetch", "origin", "main"])
    if r.returncode != 0:
        err("Failed to fetch from origin:")
        print(r.stderr)
        sys.exit(1)
    ok("Fetched from origin")


def get_local_version():
    monitor = REPO_ROOT / "monitor.py"
    if not monitor.exists():
        raise RuntimeError("monitor.py not found")
    content = monitor.read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    if not m:
        raise RuntimeError("VERSION constant not found in monitor.py")
    return m.group(1)


def get_remote_version():
    r = run_git(["show", "origin/main:monitor.py"])
    if r.returncode != 0:
        raise RuntimeError("Failed to read remote monitor.py")
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', r.stdout, re.MULTILINE)
    if not m:
        raise RuntimeError("VERSION constant not found in remote monitor.py")
    return m.group(1)


def get_ahead_behind():
    """Return (behind, ahead) relative to origin/main."""
    r = run_git(["rev-list", "--left-right", "--count", "HEAD...origin/main"])
    if r.returncode != 0:
        raise RuntimeError("Failed to compare commits")
    parts = r.stdout.strip().split()
    if len(parts) != 2:
        raise RuntimeError(f"Unexpected rev-list output: {r.stdout}")
    ahead, behind = int(parts[0]), int(parts[1])
    return behind, ahead


def get_new_commits():
    r = run_git(["log", "--oneline", "HEAD..origin/main"])
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.strip().split("\n") if line]


def get_remote_changelog_entry(version):
    r = run_git(["show", "origin/main:CHANGELOG.md"])
    if r.returncode != 0:
        return None
    pattern = rf"## v{re.escape(version)}\b.*?(?=\n## v|\Z)"
    m = re.search(pattern, r.stdout, re.DOTALL)
    return m.group(0).strip() if m else None


def apply_update():
    hdr("Applying update")
    r = run_git(["pull", "--ff-only", "origin", "main"], capture=False)
    if r.returncode != 0:
        err("git pull failed")
        sys.exit(1)
    ok("Pulled latest changes")

    try:
        new_ver = get_local_version()
        ok(f"New VERSION: {new_ver}")
    except Exception as e:
        warn(f"Could not verify new VERSION: {e}")

    print()
    print(f"{GRN}Update complete.{R}")
    note("Restart Claude Code to pick up the new statusline.py")
    note("Recommended: re-run check-requirements to verify dependencies:")
    note("  macOS/Linux: bash check-requirements.sh")
    note("  Windows:     .\\check-requirements.ps1")


def main():
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
