# CC AIO MON — System Check
# Read-only diagnostic. No changes made to your system.
# Usage: .\install.ps1

$ErrorActionPreference = 'SilentlyContinue'

$REPO_URL   = "https://github.com/iM3SK/cc-aio-mon.git"
$CLONE_DIR  = "$env:USERPROFILE\.cc-aio-mon"
$SETTINGS   = "$env:USERPROFILE\.claude\settings.json"
$CLONE_UNIX = $CLONE_DIR -replace '\\', '/'

function ok($msg)   { Write-Host "[OK]  $msg" -ForegroundColor Green }
function miss($msg) { Write-Host "[--]  $msg" -ForegroundColor Yellow }
function warn($msg) { Write-Host "[??]  $msg" -ForegroundColor Yellow }
function err($msg)  { Write-Host "[!!]  $msg" -ForegroundColor Red }
function hdr($msg)  { Write-Host "`n== $msg ==" -ForegroundColor Cyan }
function cmd($msg)  { Write-Host "     $msg" -ForegroundColor Yellow }
function note($msg) { Write-Host "     $msg" -ForegroundColor DarkGray }
function blank()    { Write-Host "" }

hdr "CC AIO MON — System Check"
note "Read-only. No changes made to your system."

blank

# ---------- Python ----------
$pyOk = $false
$pyVer = & py --version 2>&1
if ($LASTEXITCODE -eq 0) {
    ok "$pyVer  (py launcher)"
    $pyOk = $true
} else {
    err "Python Launcher (py) — not found"
}

# ---------- Git ----------
$gitOk = $false
$gitVer = & git --version 2>&1
if ($LASTEXITCODE -eq 0) {
    ok "$gitVer"
    $gitOk = $true
} else {
    err "Git — not found"
}

# ---------- Claude Code ----------
$claudeVer = & claude --version 2>&1
if ($LASTEXITCODE -eq 0) {
    ok "Claude Code $claudeVer"
} else {
    warn "Claude Code — not found in PATH (may still be installed)"
}

# ---------- Repo ----------
$repoOk = Test-Path "$CLONE_DIR\statusline.py"
if ($repoOk) {
    ok "Repo — found at $CLONE_DIR"
    $lastCommit = & git -C $CLONE_DIR log -1 --format="%ci %s" 2>$null
    if ($lastCommit) { note "Last commit: $lastCommit" }
    note "To update: git -C `"$CLONE_DIR`" pull"
} else {
    miss "Repo — not found at $CLONE_DIR"
}

# ---------- settings.json ----------
$settingsExists = Test-Path $SETTINGS
$statuslineOk   = $false
$statuslineCmd  = ""

if ($settingsExists) {
    ok "settings.json — found"
    try {
        $json = Get-Content $SETTINGS -Raw | ConvertFrom-Json
        $statuslineCmd = $json.statusLine.command
        if ($statuslineCmd -and ($statuslineCmd -like "*statusline.py*")) {
            ok "statusLine — configured"
            note "command: $statuslineCmd"
            $statuslineOk = $true
        } elseif ($statuslineCmd) {
            warn "statusLine — set, but not pointing to statusline.py"
            note "current:  $statuslineCmd"
        } else {
            miss "statusLine — not configured"
        }
    } catch {
        err "settings.json — exists but is invalid JSON"
    }
} else {
    miss "settings.json — not found"
    miss "statusLine   — not configured"
}

# ---------- Steps ----------
hdr "Steps for your system"
blank

$n = 1

if (-not $pyOk) {
    Write-Host "  $n. Install Python (includes py launcher):" -ForegroundColor White
    $n++
    cmd "winget install Python.Python.3.12"
    note "or download from https://www.python.org/downloads/"
    note "Check 'Install Python Launcher' during setup."
    note "Then reopen this terminal and re-run this script."
    blank
}

if (-not $gitOk) {
    Write-Host "  $n. Install Git:" -ForegroundColor White
    $n++
    cmd "winget install Git.Git"
    note "Then reopen this terminal and re-run this script."
    blank
}

if (-not $repoOk) {
    Write-Host "  $n. Clone the repo:" -ForegroundColor White
    $n++
    cmd "git clone $REPO_URL `"$CLONE_DIR`""
    blank
}

if (-not $statuslineOk) {
    Write-Host "  $n. Configure settings.json:" -ForegroundColor White
    $n++
    note "File: $SETTINGS"
    blank
    if (-not $settingsExists) {
        note "Create the file and paste this as the full content:"
    } else {
        note "Add the statusLine key. Do NOT overwrite the entire file."
    }
    blank
    $sl = '  "statusLine": {'
    $ty = '    "type": "command",'
    $co = "    `"command`": `"py \`"$CLONE_UNIX/statusline.py\`"`""
    $cl = '  }'
    Write-Host "     {" -ForegroundColor Yellow
    Write-Host "     $sl" -ForegroundColor Yellow
    Write-Host "     $ty" -ForegroundColor Yellow
    Write-Host "     $co" -ForegroundColor Yellow
    Write-Host "     $cl" -ForegroundColor Yellow
    Write-Host "     }" -ForegroundColor Yellow
    blank
}

Write-Host "  $n. Test statusline — paste into terminal:" -ForegroundColor White
$n++
cmd "echo '{`"context_window`":{`"used_percentage`":42}}' | py `"$CLONE_DIR\statusline.py`""
blank

Write-Host "  $n. Launch dashboard — open a new Windows Terminal window first:" -ForegroundColor White
cmd "py `"$CLONE_DIR\monitor.py`""
blank

if ($pyOk -and $gitOk -and $repoOk -and $statuslineOk) {
    Write-Host "  All checks passed." -ForegroundColor Green
    blank
}

note "Docs: https://github.com/iM3SK/cc-aio-mon/tree/main/docs"
blank
