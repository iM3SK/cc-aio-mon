# Setup — Windows

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

Open `%USERPROFILE%\.claude\settings.json` (create the file if it doesn't exist). Add the following block — replace `<your-username>` with your actual Windows username:

```json
{
  "statusLine": {
    "type": "command",
    "command": "py \"C:/Users/<your-username>/.cc-aio-mon/statusline.py\""
  }
}
```

**If `settings.json` already has other settings**, add only the `statusLine` key — do not overwrite the file. The file must remain valid JSON.

**Tip:** to print the exact command value for your machine, run:

```powershell
$p = "$env:USERPROFILE\.cc-aio-mon\statusline.py" -replace '\\', '/'
Write-Host "py `"$p`""
```

## Step 4 — Launch the dashboard

Open a **new Windows Terminal window** (required for ANSI support):

```powershell
py "$env:USERPROFILE\.cc-aio-mon\monitor.py"
```

Optional alias — add to `$PROFILE` (PowerShell profile):

```powershell
function mon { py "$env:USERPROFILE\.cc-aio-mon\monitor.py" @args }
```

## Requirements check (optional)

[check-requirements.ps1](../check-requirements.ps1) is an optional read-only script that verifies your system has Python, Git, and Claude Code CLI installed. It makes no changes to your system.

Run from the repo directory:

```powershell
cd "$env:USERPROFILE\.cc-aio-mon"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\check-requirements.ps1
```

If all checks pass, continue with the manual setup above. If something is missing, install it and re-run the script.

## Troubleshooting

**`py` not found**
- Reinstall Python from [python.org](https://www.python.org/downloads/). Check "Install Python Launcher" during setup.
- After install, reopen the terminal.

**Monitor shows "Waiting for Claude Code session..."**
- Verify `statusLine.command` path in `%USERPROFILE%\.claude\settings.json`.
- Check that temp files appear after a Claude Code event: `%TEMP%\claude-aio-monitor\`
- Test statusline directly (run from any directory): `'{"context_window": {"used_percentage": 42}}' | py "$env:USERPROFILE\.cc-aio-mon\statusline.py"`

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
