#!/usr/bin/env bash
# CC AIO MON — System Check
# Read-only diagnostic. No changes made to your system.
# Usage: bash install.sh

REPO_URL="https://github.com/iM3SK/cc-aio-mon.git"
CLONE_DIR="$HOME/.cc-aio-mon"
SETTINGS="$HOME/.claude/settings.json"

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'
CYN='\033[0;36m'; WHT='\033[1;37m'; DIM='\033[2m'; RST='\033[0m'

ok()   { printf "${GRN}[OK]${RST}  %s\n" "$1"; }
miss() { printf "${YEL}[--]${RST}  %s\n" "$1"; }
warn() { printf "${YEL}[??]${RST}  %s\n" "$1"; }
err()  { printf "${RED}[!!]${RST}  %s\n" "$1"; }
hdr()  { printf "\n${CYN}== %s ==${RST}\n" "$1"; }
cmd()  { printf "${YEL}     %s${RST}\n" "$1"; }
note() { printf "${DIM}     %s${RST}\n" "$1"; }
stp()  { printf "${WHT}  %s. %s${RST}\n" "$1" "$2"; }

hdr "CC AIO MON — System Check"
note "Read-only. No changes made to your system."
echo ""

# ---------- Python ----------
PY_OK=0
PY_CMD=""
if command -v python3 &>/dev/null; then
    ok "$(python3 --version 2>&1)"
    PY_OK=1; PY_CMD="python3"
elif command -v python &>/dev/null; then
    PY_VER=$(python --version 2>&1)
    ok "$PY_VER  (via 'python')"
    PY_OK=1; PY_CMD="python"
else
    err "Python — not found"
fi

# ---------- Git ----------
GIT_OK=0
if command -v git &>/dev/null; then
    ok "$(git --version 2>&1)"
    GIT_OK=1
else
    err "Git — not found"
fi

# ---------- Claude Code ----------
if command -v claude &>/dev/null; then
    ok "Claude Code $(claude --version 2>/dev/null)"
else
    warn "Claude Code — not found in PATH (may still be installed)"
fi

# ---------- Repo ----------
REPO_OK=0
if [ -f "$CLONE_DIR/statusline.py" ]; then
    ok "Repo — found at $CLONE_DIR"
    REPO_OK=1
    LAST_COMMIT=$(git -C "$CLONE_DIR" log -1 --format="%ci %s" 2>/dev/null || true)
    [ -n "$LAST_COMMIT" ] && note "Last commit: $LAST_COMMIT"
    note "To update:   git -C \"$CLONE_DIR\" pull"
else
    miss "Repo — not found at $CLONE_DIR"
fi

# ---------- settings.json ----------
SETTINGS_OK=0
STATUSLINE_OK=0
STATUSLINE_CMD=""

if [ -f "$SETTINGS" ]; then
    ok "settings.json — found"
    SETTINGS_OK=1
    if [ $PY_OK -eq 1 ]; then
        STATUSLINE_CMD=$($PY_CMD -c "
import json, pathlib, sys
try:
    cfg = json.loads(pathlib.Path('$SETTINGS').read_text())
    print(cfg.get('statusLine', {}).get('command', ''))
except Exception:
    print('')
" 2>/dev/null)
        if echo "$STATUSLINE_CMD" | grep -q "statusline.py"; then
            ok "statusLine — configured"
            note "command: $STATUSLINE_CMD"
            STATUSLINE_OK=1
        elif [ -n "$STATUSLINE_CMD" ]; then
            warn "statusLine — set, but not pointing to statusline.py"
            note "current:  $STATUSLINE_CMD"
        else
            miss "statusLine — not configured"
        fi
    else
        miss "statusLine — cannot check (Python not available)"
    fi
else
    miss "settings.json — not found"
    miss "statusLine   — not configured"
fi

# ---------- Steps ----------
hdr "Steps for your system"
echo ""

N=1
IS_MAC=0
[[ "$OSTYPE" == "darwin"* ]] && IS_MAC=1

if [ $PY_OK -eq 0 ]; then
    stp $N "Install Python 3.8+:"
    N=$((N+1))
    if [ $IS_MAC -eq 1 ]; then
        cmd "brew install python"
        note "or download from https://www.python.org/downloads/"
    else
        cmd "sudo apt install python3   # Debian/Ubuntu"
        cmd "sudo dnf install python3   # Fedora/RHEL"
        cmd "sudo pacman -S python      # Arch"
    fi
    note "Then re-run this script."
    echo ""
fi

if [ $GIT_OK -eq 0 ]; then
    stp $N "Install Git:"
    N=$((N+1))
    if [ $IS_MAC -eq 1 ]; then
        cmd "xcode-select --install"
        note "or: brew install git"
    else
        cmd "sudo apt install git   # Debian/Ubuntu"
        cmd "sudo dnf install git   # Fedora/RHEL"
    fi
    note "Then re-run this script."
    echo ""
fi

if [ $REPO_OK -eq 0 ]; then
    stp $N "Clone the repo:"
    N=$((N+1))
    cmd "git clone $REPO_URL $CLONE_DIR"
    echo ""
fi

if [ $STATUSLINE_OK -eq 0 ]; then
    stp $N "Configure settings.json:"
    N=$((N+1))
    note "File: $SETTINGS"
    echo ""
    if [ $SETTINGS_OK -eq 0 ]; then
        note "Create the file and paste this as the full content:"
    else
        note "Add the statusLine key. Do NOT overwrite the entire file."
    fi
    echo ""
    printf "${YEL}     {\n"
    printf "       \"statusLine\": {\n"
    printf "         \"type\": \"command\",\n"
    printf "         \"command\": \"%s %s/statusline.py\"\n" "$PY_CMD" "$CLONE_DIR"
    printf "       }\n"
    printf "     }${RST}\n"
    echo ""
fi

stp $N "Test statusline — paste into terminal:"
N=$((N+1))
cmd "echo '{\"context_window\":{\"used_percentage\":42}}' | $PY_CMD \"$CLONE_DIR/statusline.py\""
echo ""

stp $N "Launch dashboard:"
cmd "$PY_CMD \"$CLONE_DIR/monitor.py\""
echo ""

if [ $PY_OK -eq 1 ] && [ $GIT_OK -eq 1 ] && [ $REPO_OK -eq 1 ] && [ $STATUSLINE_OK -eq 1 ]; then
    printf "${GRN}  All checks passed.${RST}\n"
    echo ""
fi

note "Docs: https://github.com/iM3SK/cc-aio-mon/tree/main/docs"
echo ""
