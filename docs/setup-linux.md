# Setup — Linux

> **Python command:** This guide uses `python3`. If your system only has `python` (no `python3`), replace every `python3` in the commands below with `python`. Run `check-requirements.sh` to see which command is detected on your machine.

## Requirements

- **Python 3.8+** — `python3 --version` to check
- **Claude Code CLI** with statusline support
- **Truecolor terminal** — Kitty, Alacritty, GNOME Terminal, xterm-256color, or any terminal with 24-bit color support
- **Git**

## Step 1 — Verify Python

```bash
python3 --version
```

If missing, install via package manager:

```bash
# Debian / Ubuntu
sudo apt install python3

# Fedora / RHEL
sudo dnf install python3

# Arch
sudo pacman -S python
```

## Step 2 — Clone

```bash
git clone https://github.com/iM3SK/cc-aio-mon.git ~/.cc-aio-mon
```

## Step 3 — Configure statusLine

Add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash -c 'python3 /home/yourname/.cc-aio-mon/statusline.py'"
  }
}
```

Replace `/home/yourname` with your actual home path (`echo $HOME`).

**If `settings.json` already has other settings**, add only the `statusLine` key — do not overwrite the file. The file must remain valid JSON.

## Step 4 — Launch the dashboard

```bash
python3 ~/.cc-aio-mon/monitor.py
```

Optional alias — add to your shell config (`~/.zshrc` for zsh, `~/.bashrc` for bash):

```bash
alias mon='python3 ~/.cc-aio-mon/monitor.py'
```

## Requirements check (optional)

[check-requirements.sh](../check-requirements.sh) is an optional read-only script that verifies your system has Python, Git, and Claude Code CLI installed. It makes no changes to your system.

Run from the repo directory:

```bash
cd ~/.cc-aio-mon
bash check-requirements.sh
```

If all checks pass, continue with the manual setup above. If something is missing, install it and re-run the script.

## Updating

To update to the latest version:

```bash
cd ~/.cc-aio-mon
python3 update.py             # check only
python3 update.py --apply     # check + apply
```

Restart Claude Code after updating. See [README — Updating](../README.md#updating) for full details.

## Troubleshooting

**Statusline not appearing**
- Claude Code's statusLine runs commands in a context where external binaries (`python3`, `python`) do not produce captured output. The command **must** be wrapped in `bash -c '...'`.
- Correct: `"command": "bash -c 'python3 /home/you/.cc-aio-mon/statusline.py'"`
- Wrong: `"command": "python3 /home/you/.cc-aio-mon/statusline.py"`
- If the statusline was working before and stopped, verify the `bash -c` wrapper is still present in `~/.claude/settings.json`.

**Monitor shows "Waiting for Claude Code session..."**
- Check `statusLine.command` in `~/.claude/settings.json`.
- Verify temp files appear after a Claude Code event: `/tmp/claude-aio-monitor/`
- Test: `echo '{"context_window": {"used_percentage": 42}}' | python3 ~/.cc-aio-mon/statusline.py`

**Raw escape codes / no color**
- Check `$TERM`: should be `xterm-256color` or similar.
- Set `COLORTERM=truecolor` if your terminal supports it but doesn't advertise it.
- Test: `python3 -c "print('\033[32mGREEN\033[0m')"`

**Keyboard not responding in dashboard**
- The terminal must support raw keyboard input.
- If running inside tmux, check that `terminal-overrides` passes through correctly.
- Test outside tmux first.

## Outbound network

The Anthropic Pulse worker (`p` in the dashboard) performs unauthenticated HTTPS requests every 30 s to:

- `status.claude.com` — public status JSON
- `api.anthropic.com` — liveness probe (expects 401/405)

No credentials, no user data, no request body is sent. If you are behind a restrictive firewall or prefer zero outbound traffic, disable the worker:

```bash
CC_AIO_MON_NO_PULSE=1 python3 monitor.py
```

## CI status

CC AIO MON is CI-tested on **Ubuntu** with **Python 3.8, 3.10, 3.11, and 3.12**.
