# Project segment (PRJ)

`PRJ` shows at the start of the statusline which project a Claude Code
session is working in:

```
PRJ shop │ Opus 5.5 │ CTX 20% 200k/1M │ 7DL 19% → 5d 11h │ CST $4.12 │ BRN 0.1325 $/min
```

It helps when several sessions run side by side and the terminal title does
not say which is which.

**It is off by default.** Nothing changes until you create the configuration
file described below. The segment only reads files; it writes nothing and
opens no network connection of its own (a `sessions_dir` on a network share is
read like any other folder — keep it local, see Troubleshooting).

---

## Turn it on (one step)

Create an empty JSON object in the Claude Code config directory:

| Shell | Command |
|---|---|
| macOS / Linux | `echo '{}' > ~/.claude/cc-aio-mon-projects.json` |
| Windows PowerShell | `Set-Content -Path "$HOME\.claude\cc-aio-mon-projects.json" -Value '{}'` |
| Windows cmd | `echo {} > "%USERPROFILE%\.claude\cc-aio-mon-projects.json"` |

If you set `CLAUDE_CONFIG_DIR`, the file goes into that directory instead
(`$CLAUDE_CONFIG_DIR/cc-aio-mon-projects.json`), so each account can have its
own.

From the next statusline update on, the segment shows the name of the folder
Claude Code was started in (`workspace.project_dir` from the statusline
input). No restart is needed.

To turn it off again, delete the file.

---

## Name and color your projects

Folder names are often not the names you use (`app-v2`, `repo`). Map folders
to labels and colors:

```json
{
  "color": "white",
  "projects": [
    { "path": "~/work/shop",          "label": "Shop",      "color": "green"  },
    { "path": "~/work/shop/api",      "label": "Shop API",  "color": "teal"   },
    { "path": "C:/Users/me/billing",  "label": "Billing",   "color": "orange" }
  ]
}
```

- **`path`** matches the folder itself and everything below it. When several
  entries match, the longest `path` wins, so `~/work/shop/api` beats
  `~/work/shop` for a session inside `api`.
- The session's current directory is checked first, then the folder Claude
  Code was started in, then `cwd`. A `cd` into another mapped folder switches
  the label.
- Paths are compared as text: symlinks and junctions are not resolved. Write
  the path the way Claude Code reports it in its statusline input (for
  example, on macOS `/tmp` is a symlink to `/private/tmp`). Outside Windows
  the comparison is case-sensitive.
- `~` is expanded. On Windows, case and `/` vs `\` do not matter — but write
  paths with `/` (`C:/Users/me/billing`). In JSON a single `\` must be doubled
  (`C:\\Users\\me\\billing`). A single one is either an error (`PRJ config?`)
  or, before `b`, `f`, `n`, `r` or `t`, silently a control character:
  `"D:\repos\frontend"` loads without an error and never matches.
- A folder that matches no entry still shows its own name. Add
  `"folder_name": false` to show `PRJ` only for mapped folders.

### All keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `color` | color name | `"white"` | Color of every label that has no color of its own |
| `folder_name` | `true` / `false` | `true` | Show the folder name when nothing else matches |
| `projects` | list | `[]` | `{ "path", "label", "color"? }` entries, see above |
| `sessions_dir` | absolute path | none | Folder with per-session labels, see below |

Unknown keys are ignored. An entry without `path` or `label` is skipped.

### Colors

`red`, `orange`, `yellow`, `green`, `teal`, `cyan`, `blue`, `navy`,
`purple`, `white` — the Nord palette the rest of the statusline uses. An
unknown name falls back to the default color. Raw escape codes are not
accepted.

Labels are cut to 24 columns (ending in `…`). Control characters, zero-width
characters and broken UTF-16 halves (an emoji cut in two) are removed; a label
that is empty after that counts as no label.

---

## A different label per session (for tools)

When the folder does not identify the project — for example several sessions
run in one repository and a tool decides what each one works on — a tool can
label each session separately.

1. Set `sessions_dir` to an absolute folder:

   ```json
   { "sessions_dir": "/home/me/.cache/my-tool/statusline" }
   ```

2. For each session, write `<session_id>.json` into that folder:

   ```json
   { "label": "P5 Development", "color": "yellow" }
   ```

   `color` is optional. The file must stay under 4 KB.

The session id is the `session_id` field Claude Code passes to hooks and to
the statusline. Commands Claude Code runs (Bash and PowerShell tools, hooks)
also get it as the
[`CLAUDE_CODE_SESSION_ID`](https://code.claude.com/docs/en/env-vars)
environment variable. `/clear` starts a new session id, so a tool that labels
sessions should label the new one too (a `SessionStart` hook does, see below).

Example — label the current session from inside it (macOS / Linux):

```bash
dir="$HOME/.cache/my-tool/statusline"
mkdir -p "$dir"
[ -n "$CLAUDE_CODE_SESSION_ID" ] &&
  echo '{"label": "Billing", "color": "orange"}' > "$dir/$CLAUDE_CODE_SESSION_ID.json"
```

Example — a `SessionStart` hook that labels each session from a lookup of
your own (`.claude/settings.json`):

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python3 ~/bin/label-session.py" } ] }
    ]
  }
}
```

```python
# ~/bin/label-session.py — reads the hook input, writes the label file
import json, pathlib, sys

event = json.load(sys.stdin)
out = pathlib.Path.home() / ".cache/my-tool/statusline"
out.mkdir(parents=True, exist_ok=True)
label = pathlib.Path(event.get("cwd", "")).name or "unknown"   # your own logic here
(out / f"{event['session_id']}.json").write_text(json.dumps({"label": label}), encoding="utf-8")
```

A per-session file wins over `projects` and over the folder name. When the file
is missing, unreadable, not a regular file or has no `label`, the segment falls
back to them. Remove old files yourself; the statusline never deletes anything.

---

## Which label wins

1. `sessions_dir/<session_id>.json`, when `sessions_dir` is set and the file
   has a `label`
2. the longest `projects[].path` that contains the current directory — else the
   folder Claude Code was started in, else `cwd`
3. the name of the folder Claude Code was started in, unless
   `"folder_name": false`
4. nothing — no `PRJ` segment

---

## Narrow terminals

`PRJ` is the first segment, but not the most important one. When the line does
not fit, segments are dropped in this order: `BRN`, `CST`, `PRJ`, `CCH`, `FBL`,
`7DL`, `5HL`, `CTX`, model. The limits stay visible longer than the project
name.

---

## Troubleshooting

| You see | Cause | Fix |
|---|---|---|
| no `PRJ` at all | the file is not where the statusline looks | check `~/.claude/cc-aio-mon-projects.json`, or the `CLAUDE_CONFIG_DIR` folder |
| `PRJ config?` in red | the file exists but is not a JSON object (`{ … }`), cannot be read, or is over 64 KB | open it in a JSON validator; the whole file must be one object |
| the folder name instead of your label | no `path` contains the session's folder | compare `path` with the folder Claude Code runs in. A relative `path` never matches (use an absolute one or `~`); so does a Windows path with single `\` (see above) or a symlinked folder |
| a per-session label is ignored | `sessions_dir` is relative, the file name is not the exact session id, or the id changed after `/clear` | use an absolute folder; the file is `<session_id>.json` |
| the statusline is slow or freezes | `sessions_dir` is on a network share that does not answer | put `sessions_dir` on a local disk |

Errors in the configuration never stop the statusline: they show
`PRJ config?` instead of a label, and the other segments are computed as usual
(on a narrow terminal the error takes room like a label would).
