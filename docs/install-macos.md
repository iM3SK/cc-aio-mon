# Install — macOS

> macOS is not included in CI. Tested manually on macOS 13+ with Python 3.12. Report issues if something breaks.

## Requirements

- **Python 3.8+** — `python3 --version` to check
- **Claude Code CLI** with statusline support
- **Truecolor terminal** — Terminal.app (macOS 10.12+), iTerm2, Kitty, or Alacritty
- **Git** — included with Xcode Command Line Tools

## Step 1 — Verify Python

```bash
python3 --version
```

If missing:

```bash
# Homebrew
brew install python

# or download from python.org
open https://www.python.org/downloads/
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
    "command": "python3 /Users/yourname/.cc-aio-mon/statusline.py"
  }
}
```

Replace `/Users/yourname` with your actual home path (`echo $HOME`).

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

Optional alias — add to `~/.zshrc` or `~/.bash_profile`:

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

**Statusline not appearing**
- Verify the path in `statusLine.command`.
- Ensure Claude Code reloaded the settings (restart Claude Code).

**Raw escape codes visible**
- Terminal.app supports truecolor since macOS 10.12.
- Test: `python3 -c "print('\033[32mGREEN\033[0m')"`

**`python3` not found after brew install**
- Run `brew link python` or use the full path: `/opt/homebrew/bin/python3`.
