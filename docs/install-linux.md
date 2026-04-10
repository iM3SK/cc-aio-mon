# Install — Linux

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
    "command": "python3 /home/yourname/.cc-aio-mon/statusline.py"
  }
}
```

Replace `/home/yourname` with your actual home path (`echo $HOME`).

**If `settings.json` already has other settings**, merge safely instead of overwriting:

```bash
python3 - << 'EOF'
import json, pathlib
p = pathlib.Path.home() / ".claude/settings.json"
p.parent.mkdir(parents=True, exist_ok=True)
cfg = json.loads(p.read_text()) if p.exists() else {}
cfg["statusLine"] = {
    "type": "command",
    "command": f"python3 {pathlib.Path.home()}/.cc-aio-mon/statusline.py"
}
p.write_text(json.dumps(cfg, indent=2))
print("Written:", p)
EOF
```

## Step 4 — Launch the dashboard

```bash
python3 ~/.cc-aio-mon/monitor.py
```

Optional alias — add to `~/.bashrc` or `~/.zshrc`:

```bash
alias mon='python3 ~/.cc-aio-mon/monitor.py'
```

## System check script

[install.sh](../install.sh) checks your system and prints the exact steps and code blocks for your specific setup. **No changes are made automatically.**

```bash
bash install.sh
```

The script detects Python, Git, Claude Code, existing `settings.json`, and current `statusLine` config — then outputs only the steps you actually need, with the correct paths for your machine.

## Troubleshooting

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

## CI status

CC AIO MON is CI-tested on **Ubuntu** with **Python 3.8 and 3.12**.
