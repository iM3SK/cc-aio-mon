# Promo — CC AIO MON

## AI code reviews

Projekt bol zhodnoteny tromi veducimi AI modelmi (nie nahrada za ludske review, ale dava zakladnu uroven dovery). Kazdy dostal pristup ku kompletnym zdrojovym kodom a robil hlbkovu analyzu architektury, bezpecnosti a kvality kodu.

### Google Gemini Pro

> "Je to vynikajuci kus inzinierstva. Ukazuje to, ze autor velmi dobre rozumie systemovemu programovaniu, I/O operaciam a nevyuziva len slepo existujuce abstrakcie."

> "Majstrovske zvladnutie IPC (Medziprocesovej komunikacie): NamedTemporaryFile + os.replace(). Toto je ucebnickovy pristup pre atomicke zapisy."

> "Keby takto pragmaticky a bez zbytocnych zavislosti vznikala vacsina dnesneho CLI softveru, nas ekosystem by bol ovela cistejsi."

Klucove nalez: architektura, atomic writes, CONOUT$ Windows hack, synchronized output rendering, defenzivne programovanie.

### xAI Grok

> "10/10 pre svoju kategoriu."

> "Kod je extremne cisty, dobre komentovany, konzistentny styl. Ziadne magicke cisla, vsetko konfigurovatelne cez env vars. Perfektna cross-platform podpora."

> "Bezpecnost (security hardened): Session ID validacia regexom, _sanitize() odstranuje vsetky C0/C1 control charaktery, atomic writes, hard limit na velkost suborov. Ziadne subprocess, eval, os.system, ziadne nebezpecne veci."

Klucove nalez: kvalita kodu, bezpecnost, porovnanie s alternativami (vyhra v kazdej kategorii).

### Anthropic Claude Opus 4.6

> "2400 riadkov, stdlib only, 280 testov, CI na dvoch platformach, security hardened. Za 5 dni od v1.0 po v1.6.4."

Klucove nalez: identifikoval bash -c bug v Claude Code statusLine (reported upstream), nasiel duplicitne helpery a typo v testoch — jediny model ktory nasiel realne bugy v kode namiesto len chvaly.

### Pouzitie v postoch (skratena verzia)

**SK/CZ:**

Projekt bol zhodnoteny tromi AI modelmi (Google Gemini Pro, xAI Grok, Anthropic Claude Opus) — nie nahrada za ludske review, ale vsetky potvrdili kvalitu architektury a bezpecnosti. Gemini: "vynikajuci kus inzinierstva". Grok: "10/10 pre svoju kategoriu". Claude Opus identifikoval aj realne bugy ktore ostatne modely prehliadli.

**EN:**

The project was evaluated by three AI models (Google Gemini Pro, xAI Grok, Anthropic Claude Opus) — not a replacement for human review, but all three confirmed architecture and security quality. Gemini: "outstanding piece of engineering." Grok: "10/10 for its category." Claude Opus was the only model that identified actual bugs rather than just praise.

---

## Comparison table

| Tool | Real-time | Official JSON | TUI + statusline | Stdlib only | Multi-session |
|------|-----------|---------------|-------------------|-------------|---------------|
| **cc-aio-mon** | Yes | Yes | Yes | Yes | Yes |
| claude-monitor | No | No (logs) | No | ? | No |
| ccusage | No | No | No | ? | No |
| ccstatusline | Yes | Yes | Statusline only | Yes | No |

---

## SK/CZ — Facebook skupiny (plain text, copy-paste ready)

Ak pouzivate Claude Code CLI, mozno ste si vsimli, ze nemate realny prehlad o tom, kolko vas relacia stoji, ako rychlo sa plni kontextove okno, alebo kedy narazite na API limity.

Pomocou Claude Code (Opus 4.6) som vytvoril open-source terminalovy monitor — CC AIO MON (Python, stdlib only). Bezi priamo v terminali vedla Claude Code a v realnom case ukazuje:

- Context window usage (kolko tokenov zostava)
- API rate limity (5h a 7d) s varovanim pred zablokovanim
- Burn rate v $/min (kolko palite za minutu)
- Cache hit rate (ci funguje prompt caching)
- Session cost + cross-session agregacia (dnes / 7 dni)

Porovnanie s alternativami:

cc-aio-mon ....... real-time, oficialny JSON, TUI + statusline, stdlib only, multi-session
claude-monitor ... nie real-time, scrape logov, bez TUI
ccusage .......... nie real-time, bez TUI
ccstatusline ..... real-time, len statusline, bez multi-session

Technicky: Python 3.8+, stdlib only (ziadne pip balicky), 2400 riadkov, 280 testov, CI na Ubuntu + Windows. Kod som nechal zhodnotit tromi AI modelmi (Gemini Pro, Grok, Claude Opus) — vsetky potvrdili kvalitu architektury a bezpecnosti.

Ziadny pip install, ziadny build step. Setup trva 2 minuty:

1. git clone https://github.com/iM3SK/cc-aio-mon.git ~/.cc-aio-mon
2. Pridajte do ~/.claude/settings.json:
   {"statusLine":{"type":"command","command":"bash -c 'python3 ~/.cc-aio-mon/statusline.py'"}}
3. python3 ~/.cc-aio-mon/monitor.py

Aktualne hladam testerov napriec platformami — najma macOS (Terminal.app, iTerm2) a Linux (rozne terminalove emulatory). Ak narazite na problem s farbami, rozlozenim alebo klavesnicou, otvorte issue na GitHube.

https://github.com/iM3SK/cc-aio-mon

Screenshoty su v README. Otazky rad zodpoviem v komentaroch.

---

## SK/CZ — LinkedIn (markdown OK)

Ak pouzivate Claude Code CLI, mozno ste si vsimli, ze nemate realny prehlad o tom, kolko vas relacia stoji, ako rychlo sa plni kontextove okno, alebo kedy narazite na API limity.

Pomocou Claude Code (Opus 4.6) som vytvoril open-source terminalovy monitor — CC AIO MON (Python, stdlib only). Bezi priamo v terminali vedla Claude Code a v realnom case ukazuje:

- Context window usage (kolko tokenov zostava)
- API rate limity (5h a 7d) s varovanim pred zablokovanim
- Burn rate v $/min (kolko palite za minutu)
- Cache hit rate (ci funguje prompt caching)
- Session cost + cross-session agregacia (dnes / 7 dni)

Technicky: Python 3.8+, stdlib only (ziadne pip balicky), 2400 riadkov, 280 testov, CI na Ubuntu + Windows. Kod som nechal zhodnotit tromi AI modelmi (Gemini Pro, Grok, Claude Opus) — vsetky potvrdili kvalitu architektury a bezpecnosti. Ziadny pip install, ziadny build step. Clone repo, pridajte jeden JSON blok do settings.json, hotovo.

Aktualne hladam testerov napriec platformami — najma macOS (Terminal.app, iTerm2) a Linux (rozne terminalove emulatory). Ak narazite na problem s farbami, rozlozenim alebo klavesnicou, otvorte issue na GitHube.

https://github.com/iM3SK/cc-aio-mon

Screenshoty su v README. Otazky rad zodpoviem v komentaroch.

---

## EN — Reddit r/Python (Showcase Weekend)

**Title:** CC AIO MON — real-time terminal monitor for Claude Code CLI (stdlib only, 2400 LOC, stdlib only)

Using Claude Code (Opus 4.6), I built a TUI dashboard that shows what Claude Code is doing under the hood — context window usage, API rate limits, session costs, burn rate ($/min), cache performance. All in one terminal screen.

**How it works:**

Claude Code pipes JSON telemetry to `statusline.py` via stdin. The script writes atomic snapshots + JSONL history to temp files. A separate `monitor.py` polls those files and renders a fullscreen dashboard. Three Python files, stdlib only.

```
Claude Code -> stdin JSON -> statusline.py -> $TMPDIR -> monitor.py -> TUI
```

**Why I built it:**

Other monitors scrape log files or estimate costs from token counts. CC AIO MON reads the official Claude Code statusLine JSON protocol — the same data Claude Code uses internally. No estimation, no guessing.

| Tool | Real-time | Official JSON | TUI + statusline | Stdlib only | Multi-session |
|------|-----------|---------------|-------------------|-------------|---------------|
| **cc-aio-mon** | Yes | Yes | Yes | Yes | Yes |
| claude-monitor | No | No (logs) | No | ? | No |
| ccusage | No | No | No | ? | No |
| ccstatusline | Yes | Yes | Statusline only | Yes | No |

**Tech details:**

- Python 3.8+, stdlib only — no pip, no venv, no build step
- 2400 lines across 4 files, 280 unit tests
- Atomic writes (NamedTemporaryFile + os.replace) for IPC
- Synchronized Output (DEC private mode 2026h) for flicker-free rendering
- ANSI 24-bit color with Nord palette
- Cross-platform: Windows (py launcher + ctypes/msvcrt), macOS, Linux
- CI: GitHub Actions — tests on Ubuntu 3.8/3.12 + Windows 3.12, Bandit security scan, CodeQL, OSSF Scorecard
- Code evaluated by three AI models (Gemini Pro, Grok, Claude Opus) — all confirmed architecture and security quality

**Looking for testers:**

The main gap is cross-platform terminal testing. I've tested on Windows Terminal + bash, but I need people to try it on:

- macOS Terminal.app, iTerm2, Kitty, Alacritty
- Linux — GNOME Terminal, Konsole, xfce4-terminal, tmux, screen
- Windows — different terminal emulators, PowerShell vs bash

If colors look wrong, bars are misaligned, or keyboard input doesn't work — open an issue. Setup takes 2 minutes (clone + one JSON block in settings.json).

GitHub: https://github.com/iM3SK/cc-aio-mon

Screenshots in the README. Happy to answer questions about the architecture.

---

## EN — Reddit r/ClaudeAI

**Title:** I built a real-time terminal monitor for Claude Code — track context, costs, rate limits, burn rate

If you use Claude Code CLI, you probably noticed there's no easy way to see how fast you're burning through context, what your session is costing you per minute, or when you're about to hit API rate limits.

I built CC AIO MON using Claude Code (Opus 4.6) to solve that. It reads Claude Code's official statusLine JSON protocol via stdin and gives you:

- One-line ANSI status bar (runs inside Claude Code)
- Fullscreen TUI dashboard (runs in a separate terminal)
- Cross-session cost tracking (today + rolling 7 days)
- Smart warnings when context fills fast, rate limits approach, or burn rate spikes

| Tool | Real-time | Official JSON | TUI + statusline | Stdlib only | Multi-session |
|------|-----------|---------------|-------------------|-------------|---------------|
| **cc-aio-mon** | Yes | Yes | Yes | Yes | Yes |
| claude-monitor | No | No (logs) | No | ? | No |
| ccusage | No | No | No | ? | No |
| ccstatusline | Yes | Yes | Statusline only | Yes | No |

Python 3.8+, stdlib only, stdlib only. Code evaluated by three AI models (Gemini Pro, Grok, Claude Opus) — all confirmed architecture and security quality. Setup is 2 minutes — clone + one JSON block in `~/.claude/settings.json`.

I need testers across macOS and Linux terminals. If something looks broken — colors, layout, keyboard — please open an issue.

https://github.com/iM3SK/cc-aio-mon

---

## EN — LinkedIn

Using Claude Code (Opus 4.6), I built an open-source real-time terminal monitor for Claude Code CLI.

If you work with Claude Code professionally, you know the pain: no visibility into context window usage, API rate limits, or session costs until it's too late.

CC AIO MON reads Claude Code's official statusLine JSON protocol and displays everything in a compact TUI dashboard — burn rate ($/min), context consumption rate (%/min), cache performance, rate limit countdowns, cross-session cost aggregation.

Technical highlights:
- Python 3.8+, stdlib only — zero external dependencies
- 2400 lines, 280 unit tests, CI on Ubuntu + Windows
- Atomic IPC via temp files, security hardened (path traversal prevention, input sanitization, file size limits)
- Nord truecolor palette, flicker-free synchronized output
- Evaluated by three AI models (Gemini Pro, Grok, Claude Opus)

Currently looking for testers across macOS and Linux terminals. The tool is stable on Windows Terminal, but needs real-world validation on iTerm2, Kitty, GNOME Terminal, and others.

Two-minute setup: clone, add one JSON block to settings, run monitor.py.

https://github.com/iM3SK/cc-aio-mon

---

## EN — X / Twitter

Built with Claude Code Opus 4.6: real-time terminal monitor for Claude Code CLI.

Context window, API rate limits, burn rate ($/min), cache performance — all in one TUI dashboard.

Python 3.8+, stdlib only, stdlib only, 2400 LOC, 280 tests. Evaluated by Gemini Pro, Grok & Claude Opus.

Looking for testers across macOS/Linux/Windows terminals.

https://github.com/iM3SK/cc-aio-mon

---

## Kde postovat

| Platforma | Jazyk | Kedy | Poznamka |
|-----------|-------|------|----------|
| FB — SK/CZ dev skupiny | SK | Kedykolvek | Uz si postoval starsu verziu, teraz update |
| FB — AI skupiny SK/CZ | SK | Kedykolvek | Mensia ale zaujata cielovka |
| Reddit r/Python | EN | Vikend (Showcase Saturday/Sunday) | Automod maze pocas tyzdna |
| Reddit r/ClaudeAI | EN | Kedykolvek | Presna cielovka, mensia komunita |
| Reddit r/commandline | EN | Kedykolvek | TUI enthusiasti |
| LinkedIn | EN | Utorok-Stvrtok 8-10h CET | Profesionalny ton |
| X / Twitter | EN | Kedykolvek | Kratky format, pridaj screenshot |
| Hacker News (Show HN) | EN | Utorok-Stvrtok | Vysoka konkurencia ale obrovska expozicia |

## Tipy

- **GIF/video** — natoč 15s screen recording kde dashboard bezi nazivo pocas konverzacie (asciinema alebo OBS)
- **Screenshot** — pridaj ku kazdemu postu, text sam o sebe nepredava TUI tool
- **Odpoved na komentare** — bud aktivny prvu hodinu po poste, algoritmy to odmenuju
- **Cross-post** — nepostuj vsade naraz, rozloz na 2-3 dni
