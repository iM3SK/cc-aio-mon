#!/usr/bin/env bash
# CC AIO MON — System Requirements Check
# Run this in bash:  bash check-requirements.sh
# Read-only. Checks system requirements only. No changes are made to your system.

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

# ---------- Python ----------
PY_CMD=""
if command -v python3 >/dev/null 2>&1; then
    ok "$(python3 --version 2>&1)  (python3)"
    PY_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    ok "$(python --version 2>&1)  (python)"
    PY_CMD="python"
else
    err "Python — not found (tried python3 and python)"
    all_ok=0
fi

# ---------- Git ----------
if command -v git >/dev/null 2>&1; then
    ok "$(git --version 2>&1)"
else
    err "Git — not found"
    all_ok=0
fi

# ---------- Claude Code CLI ----------
if command -v claude >/dev/null 2>&1; then
    ok "Claude Code $(claude --version 2>&1)"
else
    err "Claude Code — not found in PATH"
    all_ok=0
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
