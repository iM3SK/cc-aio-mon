# CC AIO MON — System Requirements Check
# Run this in PowerShell:  .\check-requirements.ps1
# Read-only. Checks system requirements only. No changes are made to your system.

$ErrorActionPreference = 'SilentlyContinue'

function ok($msg)   { Write-Host "[OK]  $msg" -ForegroundColor Green }
function err($msg)  { Write-Host "[!!]  $msg" -ForegroundColor Red }
function hdr($msg)  { Write-Host "`n== $msg ==" -ForegroundColor Cyan }
function note($msg) { Write-Host "     $msg" -ForegroundColor DarkGray }
function blank()    { Write-Host "" }

hdr "CC AIO MON — System Requirements Check"
note "Read-only. No changes are made to your system."

blank

$allOk = $true

# ---------- Python Launcher ----------
$pyVer = & py --version 2>&1
if ($LASTEXITCODE -eq 0) {
    ok "$pyVer  (py launcher)"
} else {
    err "Python Launcher (py) — not found"
    $allOk = $false
}

# ---------- Git ----------
$gitVer = & git --version 2>&1
if ($LASTEXITCODE -eq 0) {
    ok "$gitVer"
} else {
    err "Git — not found"
    $allOk = $false
}

# ---------- Claude Code CLI ----------
$claudeVer = & claude --version 2>&1
if ($LASTEXITCODE -eq 0) {
    ok "Claude Code $claudeVer"
} else {
    err "Claude Code — not found in PATH"
    $allOk = $false
}

blank

if ($allOk) {
    Write-Host "All requirements met." -ForegroundColor Green
    note "Continue with manual setup: docs/setup-windows.md"
} else {
    Write-Host "Some requirements are missing." -ForegroundColor Yellow
    note "Install the missing tools, then re-run this script."
    note "Manual setup guide: docs/setup-windows.md"
}

blank
