# CC AIO MON — System Requirements Check
# Run this in PowerShell:  .\check-requirements.ps1
# Read-only. Checks system requirements only. No changes are made to your system.
# Exit code: 0 when Python 3.8+ and Git are present, 1 otherwise.

function ok($msg)   { Write-Host "[OK]  $msg" -ForegroundColor Green }
function err($msg)  { Write-Host "[!!]  $msg" -ForegroundColor Red }
function warn($msg) { Write-Host "[--]  $msg" -ForegroundColor Yellow }
function hdr($msg)  { Write-Host "`n== $msg ==" -ForegroundColor Cyan }
function note($msg) { Write-Host "     $msg" -ForegroundColor DarkGray }
function blank()    { Write-Host "" }

# Run a command only when it exists; return its first output line or $null.
function Get-ToolVersion($name, [string[]]$arguments) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { return $null }
    $out = & $name @arguments 2>&1
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($out | Select-Object -First 1 | Out-String).Trim()
}

hdr "CC AIO MON — System Requirements Check"
note "Read-only. No changes are made to your system."

blank

$allOk = $true

# ---------- Python Launcher (3.8+) ----------
$pyVer = Get-ToolVersion 'py' @('--version')
if (-not $pyVer) {
    err "Python Launcher (py) — not found"
    $allOk = $false
} elseif ($pyVer -match 'Python (\d+)\.(\d+)' -and ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 8))) {
    ok "$pyVer  (py launcher)"
} else {
    err "$pyVer — Python 3.8 or newer is required"
    $allOk = $false
}

# ---------- Git ----------
$gitVer = Get-ToolVersion 'git' @('--version')
if ($gitVer) {
    ok "$gitVer"
} else {
    err "Git — not found"
    $allOk = $false
}

# ---------- Claude Code CLI (statusline only) ----------
$claudeVer = Get-ToolVersion 'claude' @('--version')
if ($claudeVer) {
    ok "Claude Code $claudeVer"
} else {
    warn "Claude Code CLI — not found in PATH"
    note "Needed for the statusline. Claude desktop app sessions are shown without it."
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
if (-not $allOk) { exit 1 }
