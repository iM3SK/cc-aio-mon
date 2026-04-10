# Install — Windows

## Requirements

- **Python 3.8+** with Python Launcher (`py`) — [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.12`
- **Windows Terminal** — required for truecolor ANSI support; [install from Microsoft Store](https://aka.ms/terminal) or `winget install Microsoft.WindowsTerminal`
- **Claude Code CLI** with statusline support
- **Git** — `winget install Git.Git` if missing

> The built-in `python` command on a fresh Windows 11 install opens Microsoft Store, not Python. This guide uses the `py` launcher which is bundled with the official Python installer and always works. Run `py --version` to verify.

## Step 1 — Verify Python

```powershell
py --version
```

If this fails, install Python via winget:

```powershell
winget install Python.Python.3.12
```

Then reopen the terminal and re-run `py --version`.

## Step 2 — Clone

```powershell
git clone https://github.com/iM3SK/cc-aio-mon.git "$env:USERPROFILE\.cc-aio-mon"
```

## Step 3 — Configure statusLine

Open `%USERPROFILE%\.claude\settings.json` (create if it doesn't exist). Add the `statusLine` block:

```json
{
  "statusLine": {
    "type": "command",
    "command": "py \"C:/Users/YourName/.cc-aio-mon/statusline.py\""
  }
}
```

Generate the correct path for your machine:

```powershell
$p = "$env:USERPROFILE\.cc-aio-mon\statusline.py" -replace '\\', '/'
Write-Host "py `"$p`""
```

Paste the output as the `command` value.

**If `settings.json` already has other settings**, add only the `statusLine` key — do not overwrite the file. The file must remain valid JSON.

## Step 4 — Launch the dashboard

Open a **new Windows Terminal window** (required for ANSI support):

```powershell
py "$env:USERPROFILE\.cc-aio-mon\monitor.py"
```

Optional alias — add to `$PROFILE` (PowerShell profile):

```powershell
function mon { py "$env:USERPROFILE\.cc-aio-mon\monitor.py" @args }
```

## System check script

[install.ps1](../install.ps1) checks your system and prints the exact steps and code blocks for your specific setup. **No changes are made automatically.**

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\install.ps1
```

The script detects Python, Git, Claude Code, existing `settings.json`, and current `statusLine` config — then outputs only the steps you actually need, with the correct paths for your machine.

## Troubleshooting

**`py` not found**
- Reinstall Python from [python.org](https://www.python.org/downloads/). Check "Install Python Launcher" during setup.
- After install, reopen the terminal.

**Monitor shows "Waiting for Claude Code session..."**
- Verify `statusLine.command` path in `%USERPROFILE%\.claude\settings.json`.
- Check that temp files appear after a Claude Code event: `%TEMP%\claude-aio-monitor\`
- Test statusline directly: `echo '{"context_window": {"used_percentage": 42}}' | py statusline.py`

**Garbled output or missing colors**
- Use Windows Terminal — cmd.exe and classic PowerShell console do not support truecolor.
- Run `chcp 65001` for UTF-8 encoding.
- Test color support: `py -c "print('\033[32mGREEN\033[0m')"`

**Raw escape codes visible**
- Same fix — use Windows Terminal. The monitor checks `isatty()` and `TERM=dumb` on startup.

**Keyboard not responding**
- The terminal window must have focus (`msvcrt.getch()` requirement).
- Fallback: `Ctrl+C` to exit.

**`settings.json` path not found**
```powershell
New-Item -Path "$env:USERPROFILE\.claude\settings.json" -ItemType File -Force
```

## CI status

CC AIO MON is CI-tested on Windows with **Python 3.12** (Ubuntu tests both 3.8 and 3.12). Python 3.8 on Windows is not CI-tested.
