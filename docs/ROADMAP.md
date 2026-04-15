# Roadmap — Planned Features

> **Status:** Draft — nothing committed yet. Pick, discuss, implement.

## Priority Order

### 1. Multi-session dashboard view (`v`)

Split view of all active sessions — key metrics side by side, no switching needed.

```
──────────────────────────────────────────────
 MULTI-SESSION VIEW
──────────────────────────────────────────────
 #  Session          Model        CTX   CST    BRN     State
 1  cc-aio-mon       Opus 4.6      4%  $1.49  0.12    ACTIVE
 2  google-workspace Opus 4.6     23%  $3.21  0.08    ACTIVE
 3  obsidian-vault   Haiku 4.5    67%  $0.14  0.02    stale 5m
 4  web-scraper      Sonnet 4.6   12%  $0.88  0.05    stale 32m
──────────────────────────────────────────────
  Total today:   $8.42
  Total 7-day:  $34.17
  Active: 2    Stale: 2
──────────────────────────────────────────────
[1-9] switch  [v] close
```

- **Data source:** `list_sessions()` + `load_state()` — already implemented
- **Complexity:** Low — pure layout, no new data needed
- **Value:** High — overview of all sessions without switching

### 2. Sparkline history (BRN / CTX / CST)

Mini trend graphs of last ~30 values inline in the dashboard. Uses braille/block characters.

```
BRN [█████████████████░░░░░░░░]  0.12 $/min
    ▁▂▃▃▅▇█▇▅▃▂▁▁▂▃▅▇▆▅▃▂▁▁▂▃

CTX [████░░░░░░░░░░░░░░░░░░░░░]   4.0 %
    ▁▁▁▁▂▂▃▅▇█▁▁▂▃▃▄▅▆▇█▁▁▂▃▃
    ↑ compaction               ↑ compaction
```

- **Data source:** `{sid}.jsonl` history — already written by statusline.py
- **Complexity:** Low — stdlib, render only
- **Value:** High — instant trend visibility, compaction events obvious

### 3. Rate limit forecast

Predict when you'll hit 5HL/7DL based on current burn rate. Inline in dashboard.

```
5HL [██████████░░░░░░░░░░░░░░░]  41.0 %
    reset in: 3h 12m
    ⚠ at current rate: hits 100% in ~2h 05m
```

- **Data source:** `used_percentage` + `resets_at` + `BRN` — all available
- **Complexity:** Low — simple extrapolation
- **Value:** Medium — plan your work around rate limits

### ~~4. Cost breakdown modal (`c`)~~ — Done (v1.8.0)

Per-session cost breakdown — token split, cache savings, cost-per-minute timeline.

```
──────────────────────────────────────────────
 SESSION COST BREAKDOWN
──────────────────────────────────────────────
  Duration:     13m 42s
  Total Cost:   $1.49

  Input tokens:      1,948   ($0.02)
  Output tokens:    10,895   ($0.82)
  Cache read:       40.9k   ($0.03)  ← saved
  Cache write:         736   ($0.03)

  Cache savings:    ~$0.58  (28% of total)
  ──────────────────────────────────────────
  Cost timeline (per minute):
  m1  ▁ $0.02
  m2  ▃ $0.11
  m3  ▅ $0.18
  m4  █ $0.31    ← peak (agent burst)
  m5  ▃ $0.14
──────────────────────────────────────────────
[c] close
```

- **Data source:** `context_window.current_usage` + `cost.*` — partially available. Per-minute breakdown needs JSONL history aggregation.
- **Complexity:** Medium — cost math, new modal
- **Value:** High — understand where money goes, motivate cache optimization

### 5. Session activity heatmap (`a`)

Horizontal heatmap of last 60 minutes. Each char = 1 minute.

```
──────────────────────────────────────────────
 SESSION ACTIVITY  (last 60 min)
──────────────────────────────────────────────
  API  ░░▓▓██░░░░▓▓▓███░░░░░░▓▓██▓▓░░░▓▓██
  CTX  ▁▂▃▄▅▆▇█▁▂▃▄▅▆▇█▁▂▃▄▅▆▇█▁▂▃▄▅▆▇█
       ╰─ idle ─╯       ╰ idle╯
  Active:  42m / 60m  (70%)
──────────────────────────────────────────────
[a] close
```

- **Data source:** JSONL timestamps
- **Complexity:** Low — timestamp bucketing, render
- **Value:** Medium — usage pattern awareness

### 6. Efficiency score

Aggregated efficiency indicator — combines CHR, APR, cost-per-output-token.

```
  EFF  [████████████████████░░░░░]  82 %
       CHR 98% ✓  APR 46% ✓  $/out 0.04¢/tok
```

- **Data source:** Existing metrics
- **Complexity:** Low — formula, single bar
- **Value:** Low — nice-to-have, not actionable

---

## Notes

- All features are stdlib-only (no pip dependencies)
- All data sources already exist — no new IPC needed
- Keyboard shortcuts tentative — may change during implementation
- Sparklines and forecast could be dashboard-inline (no modal needed)
