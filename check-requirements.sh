#!/usr/bin/env bash
# CC AIO MON — System Requirements Check
# Run this in bash:  bash check-requirements.sh
# Read-only. Checks system requirements only. No changes are made to your system.
# Exit code: 0 when Python 3.8+ and Git are present, 1 otherwise.

# ---------- Colors ----------
if [ -t 1 ]; then
    GRN=$'\033[32m'
    RED=$'\033[31m'
    CYN=$'\033[36m'
    DIM=$'\033[2m'
    R=$'\033[0m'
else
    GRN=''; RED=''; CYN=''; DIM=''; R=''
fi

ok()    { printf "%s[OK]%s  %s\n" "$GRN" "$R" "$1"; }
err()   { printf "%s[!!]%s  %s\n" "$RED" "$R" "$1"; }
hdr()   { printf "\n%s== %s ==%s\n" "$CYN" "$1" "$R"; }
note()  { printf "     %s%s%s\n" "$DIM" "$1" "$R"; }
blank() { printf "\n"; }

hdr "CC AIO MON — System Requirements Check"
note "Read-only. No changes are made to your system."

blank

all_ok=1

# ---------- Python (3.8+) ----------
PY_CMD=""
if command -v python3 >/dev/null 2>&1; then
    PY_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PY_CMD="python"
fi
if [ -z "$PY_CMD" ]; then
    err "Python — not found (tried python3 and python)"
    all_ok=0
elif "$PY_CMD" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    ok "$("$PY_CMD" --version 2>&1)  ($PY_CMD)"
else
    err "$("$PY_CMD" --version 2>&1) — Python 3.8 or newer is required"
    all_ok=0
fi

# ---------- Git ----------
if command -v git >/dev/null 2>&1; then
    ok "$(git --version 2>&1)"
else
    err "Git — not found"
    all_ok=0
fi

# ---------- Claude Code CLI (statusline only) ----------
if command -v claude >/dev/null 2>&1; then
    ok "Claude Code $(claude --version 2>&1)"
else
    printf "[--]  Claude Code CLI — not found in PATH\n"
    note "Needed for the statusline. Claude desktop app sessions are shown without it."
fi

blank

if [ $all_ok -eq 1 ]; then
    printf "%sAll requirements met.%s\n" "$GRN" "$R"
    note "Your Python command: $PY_CMD"
    note "macOS: continue with manual setup in docs/setup-macos.md"
    note "Linux: continue with manual setup in docs/setup-linux.md"
else
    printf "%sSome requirements are missing.%s\n" "$RED" "$R"
    if [ -n "$PY_CMD" ]; then
        note "Your Python command: $PY_CMD"
    fi
    note "Install the missing tools, then re-run this script."
    note "Manual setup guide: docs/setup-macos.md or docs/setup-linux.md"
fi

blank
[ $all_ok -eq 1 ] || exit 1
