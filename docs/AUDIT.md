# CC AIO MON — Full Codebase Audit & Bug Plan

> Dátum: 2026-04-15 | Branch: `test/expand-coverage` | Verzia: 1.8.4
> Zdroje: 11 audit agentov v 2 kolách (6 prvý audit + 5 re-audit po fixoch)
> Stav: **ARCHIVED.** Všetky nálezy H1-H9, M1-M13, C1-C3 fixnuté v 1.8.4. Line references sú stale (kód sa zmenil). Tento dokument slúži ako historický záznam auditu, nie ako živý tracker. Pre aktuálny stav pozri CHANGELOG.md v1.8.4.

---

## Zhrnutie

| Súbor | Critical | High | Medium | Low |
|-------|----------|------|--------|-----|
| monitor.py | 0 | 3 | 8 | 6 |
| statusline.py | 0 | 2 | 6 | 6 |
| shared.py | 0 | 0 | 2 | 4 |
| update.py | 0 | 2 | 0 | 4 |
| tests.py (gaps) | 3 | 5 | 7 | 6 |
| projekt/config | 0 | 0 | 5 | 6 |
| **Spolu** | **3** | **12** | **28** | **32** |

---

## 1. CRITICAL — Chýbajúce testy (skryté riziká)

### C1. `_get_pricing()` — žiadne testy
- **Súbor:** `monitor.py:1081`
- **Problém:** Stripuje `[1m]` suffix z model ID pre lookup cien. Ak sa zmení formát, cost breakdown bude tichý fail.
- **Akcia:** Napísať testy pre known model IDs, suffix stripping, fallback na default pricing.

### C2. `_cost_thirds()` — žiadne testy
- **Súbor:** `monitor.py:1087`
- **Problém:** Bucketing cost-per-time z JSONL histórie. Netriviálna logika bucket boundaries, edge case `span < 1`.
- **Akcia:** Testy pre empty history, single entry, multi-bucket, edge boundaries.

### C3. `_rls_check_worker()` — rls.json zápis netestovaný
- **Súbor:** `monitor.py:510-519`
- **Problém:** Finally blok píše rls.json do DATA_DIR. Ak zlyhá alebo zapíše malformed JSON, statusline.py číta stale/broken data.
- **Akcia:** Test s mock DATA_DIR, overenie JSON validity po zápise.

---

## 2. HIGH — Reálne bugy

### H1. ✅ `_rls_cache["t"]` inicializácia na `0.0` (UŽ FIXNUTÉ)
- **Súbor:** `monitor.py:453`
- **Problém:** Na čerstvo nabootovanom PC (uptime < 1h) TTL check `time.monotonic() - 0.0 < 3600` → True, release check sa nikdy nespustí.
- **Fix:** Zmenené na `-_RLS_TTL`. ✅

### H2. Lock deadlock ak `Thread.start()` zlyhá
- **Súbor:** `monitor.py:529-533`
- **Problém:** `_rls_lock.acquire()` v `_rls_maybe_check()`, potom `Thread(target=...).start()`. Ak `.start()` hodí výnimku, lock ostane held navždy — žiadne ďalšie release checky.
- **Fix:** Wrappnuť `t.start()` do try/except s `_rls_lock.release()` vo fallbacku.

### H3. `_apply_update_action()` blokuje main thread
- **Súbor:** `monitor.py:1649`
- **Problém:** Git pull + py_compile na main threade. UI zamrzne na 30+ sekúnd.
- **Fix:** Spustiť v daemon threade ako `_rls_check_worker`.

### H4. `_rls_fetching` bez synchronizácie
- **Súbor:** `monitor.py:455`
- **Problém:** Set na True (main thread) a False (worker thread) bez locku. Mŕtvy kód — nikde sa nečíta pre guard.
- **Fix:** Odstrániť alebo integrovať do lock-protected bloku.

### H5. Nadmerné čítanie histórie (10 MB)
- **Súbor:** `statusline.py:301`
- **Problém:** `fh.read(MAX_FILE_SIZE * 10 + 1)` = 10 MB, ale JSONL je trimovaný na 1 MB. Zbytočná pamäťová záťaž.
- **Fix:** Zmeniť limit na `MAX_FILE_SIZE + 1`, alebo tail-read posledných ~50 KB.

### H6. Double `CloseHandle` na Windows
- **Súbor:** `statusline.py:84-92`
- **Problém:** Ak `GetConsoleScreenBufferInfo` uspeje ale `w <= 0`, `CloseHandle(h)` sa volá 2x. Undefined behavior — môže zavrieť nesúvisiaci handle.
- **Fix:** Jeden `CloseHandle` vo finally bloku.

### H7. `sys.stdout` replacement leaks wrapper (update.py + statusline.py + monitor.py)
- **Súbory:** `update.py:33`, `statusline.py:261`, `monitor.py:1577`
- **Problém:** Nový `open()` na stdout fd, starý wrapper nie je closed. Minor memory leak.
- **Fix:** `sys.stdout.flush()` pred reassignment, akceptovateľné pre CLI tool — low priority.

### H8. `_model_label()` nestrippuje `[1m]` suffix
- **Súbor:** `monitor.py:1428`
- **Problém:** `_get_pricing()` stripuje `[...]` suffix, ale `_model_label()` nie. Model ID `claude-opus-4-6[1m]` sa zobrazí ako raw string namiesto "Opus 4.6".
- **Fix:** Pridať `model_id.split("[")[0]` pred dict lookup.

### H9. update.py syntax check uses running interpreter
- **Súbor:** `update.py:190`
- **Problém:** Po `git pull`, `py_compile` beží s aktuálnym Python — ak nový kód vyžaduje novšiu verziu, false failure.
- **Fix:** Použiť `compile()` s raw source textom namiesto `py_compile`.

---

## 3. MEDIUM — Robustnosť a edge cases

### M1. Floating-point drift v `since_data`
- **Súbor:** `monitor.py:1642`
- **Problém:** `since_data += tick` akumuluje float drift cez dlhé sessions.
- **Fix:** Použiť `time.monotonic()` na tracking posledného data load.

### M2. NamedTemporaryFile leak pri write failure
- **Súbory:** `statusline.py:347-355`, `statusline.py:378-384`
- **Problém:** Ak `fd.write()` hodí výnimku (disk full), temp file sa nikdy nezmaže.
- **Fix:** try/finally s `fd.close()` + `os.unlink(fd.name)`.

### M3. UTF-8 encoding check nechytí `utf_8`
- **Súbory:** `statusline.py:259`, `update.py:31`, `monitor.py:1577`
- **Problém:** `.lower().replace("-", "")` nechytí Python normalized form `utf_8`.
- **Fix:** Použiť `codecs.lookup(sys.stdout.encoding).name == "utf-8"`.

### M4. `session_id: null` v JSON → filename `None.json`
- **Súbory:** `statusline.py:271`, `statusline.py:316`
- **Problém:** `data.get("session_id", "default")` vracia `None` pre explicit null → `str(None)` = `"None"`.
- **Fix:** `data.get("session_id") or "default"`.

### M5. Nekonzistentný symlink check
- **Súbor:** `monitor.py:662` vs `load_state`/`load_history`
- **Problém:** `list_sessions()` rejectuje symlinked DATA_DIR, ale load funkcie nie.
- **Fix:** Centralizovať symlink check.

### M6. Silent truncation pri 1000 súboroch
- **Súbor:** `monitor.py:93-95`
- **Problém:** Scanner breaks po 1000 `.jsonl` súboroch. Power users s veľa projektami majú neúplné stats bez varovania.
- **Fix:** Zobraziť warning keď sa limit dosiahne.

### M7. `_ANSI_RE` nechytí všetky ANSI sekvencie
- **Súbor:** `shared.py:10`
- **Problém:** Nechytí OSC, `?` parameter bytes (`\033[?25h`).
- **Fix:** Rozšíriť regex: `\033(?:\[[0-9;?]*[a-zA-Z~]|\][^\x07]*\x07)`.

### M8. SIGTERM cleanup volá `cleanup()` 2x
- **Súbor:** `monitor.py:1613`
- **Problém:** Lambda volá `cleanup()` + `sys.exit(0)`, ale `atexit` tiež volá `cleanup()`.
- **Fix:** SIGTERM handler len `sys.exit(0)`, nechať `atexit` handle cleanup.

### M9. Rate limit `resets_at` — stale zobrazenie po skončení session
- **Súbor:** `monitor.py:928-940`
- **Problém:** Keď session skončí, 0% sa zobrazuje donekonečna.
- **Fix:** Indikátor že dáta sú z minulého okna.

### M10. Duplicitný `SECURITY.md`
- **Súbory:** `SECURITY.md` (root) vs `.github/SECURITY.md`
- **Problém:** Rozdielny obsah (72h vs 7 dní SLA).
- **Fix:** Odstrániť root kópiu, nechať `.github/` verziu.

### M11. `PROMO.md` tracked napriek `.gitignore`
- **Súbor:** `.gitignore:24` vs git tracking
- **Problém:** `.gitignore` neplatí pre already-tracked files.
- **Fix:** `git rm --cached PROMO.md`.

### M12. `_model_label()` vs `_get_pricing()` inconsistencia
- **Súbor:** `monitor.py:1428` vs `monitor.py:1083`
- **Problém:** Pricing stripuje `[...]`, label nie. (Viď H8)

### M13. Dead code — `k == "q"` v menu handler
- **Súbor:** `monitor.py:1661`
- **Problém:** Nedosiahnuteľný branch — `q` je zachytený skôr na riadku 1646.
- **Fix:** Odstrániť mŕtvy kód.

---

## 4. LOW — Kozmetika a edge cases

| # | Súbor:riadok | Popis |
|---|-------------|-------|
| L1 | `monitor.py:300` | `WARN_BRN` env var parsovaný pri module load, nie runtime |
| L2 | `monitor.py:750` | Braille spinner nefunguje na legacy Windows konzolách |
| L3 | `monitor.py:1187` | `max_delta or 0.01` — fragile fallback |
| L4 | `monitor.py:103` | mtime-based skip môže minúť relevantné súbory |
| L5 | `monitor.py:156` | `datetime.fromtimestamp()` implicit local timezone |
| L6 | `shared.py:45` | `f_dur` truncates ms, neround |
| L7 | `shared.py:64` | `f_tok` vizuálny skok pri 100k boundary |
| L8 | `shared.py:13` | `VERSION_RE` akceptuje mismatched quotes |
| L9 | `statusline.py:194` | CHR threshold zdieľa CRIT env var s rate-limit |
| L10 | `statusline.py:264` | `stdin.read()` blokuje ak parent process neuzavrie pipe |
| L11 | `statusline.py:364` | History append nie je atomic (interleave risk) |
| L12 | `update.py:44` | `err()` na stderr, ostatné na stdout — mixed streams |
| L13 | Platform detection | Mix `platform.system()` vs `sys.platform` |
| L14 | `tests.yml` | Python 3.8 testovaný iba na Ubuntu, nie Win/Mac |
| L15 | `monitor.py:1073` | `_MODEL_PRICING` hardcoded — bude driftovať s novými modelmi |

---

## 5. Test Coverage Gaps (prioritizované)

### Žiadne testy vôbec:
| Funkcia | Súbor | Riziko |
|---------|-------|--------|
| `_get_pricing()` | monitor.py:1081 | Cost breakdown wrong |
| `_cost_thirds()` | monitor.py:1087 | Chart data corrupt |
| `poll_key()` / `_setup_term()` / `_restore_term()` | monitor.py:257+ | Platform-specific I/O |
| `main()` — event loop | monitor.py:1568 | Modal state machine |
| `main()` — statusline | statusline.py:257 | Integration flow |
| `sep()` | monitor.py:648 | Used in every render |
| `apply_update()` | update.py:167 | Standalone updater |
| `main()` — update CLI | update.py:209 | Full CLI flow |

### Chýbajúce edge case testy:
| Čo | Kde | Problém |
|----|-----|---------|
| `_limit_color` at exactly 80% | tests.py:156 | Boundary untested |
| `truncate()` s multi-byte Unicode | monitor.py:310 | CJK = 2 columns wide |
| History file > 10MB | statusline.py:296 | Size guard path |
| `_calc_streaks()` yesterday-only | monitor.py:185 | current=0 vs longest=N |
| `calc_rates()` non-dict entries | shared.py:77 | AttributeError path |
| DATA_DIR symlink guard | monitor.py:662 | Security boundary |
| `f_cd` multi-day countdown | monitor.py | `d > 0` path |
| `_file_count > 1000` guard | monitor.py:96 | DoS prevention |

---

## 6. Platform Research — Kľúčové riziká

| Riziko | Severity | Stav v kóde |
|--------|----------|-------------|
| Daemon thread GIL hang pri exit | High | Čiastočne ošetrené (finally blok) |
| `os.replace()` nie je atomic na Windows | Medium | Ošetrené (try-except) |
| `subprocess.run` timeout neukončí child na Win | Medium | Neošetrené |
| `time.monotonic()` 15ms jitter na Win | Low | OK pre TTL, nie ideálne pre blink |
| ANSI 24-bit na Win < 1909 | Low | Graceful fallback |
| stdin `select()` + signal na Unix | Low | Neošetrené |

---

## 7. Navrhovaný plán opráv

### Fáza 1 — Bugy (pred releasom)
1. ~~`_rls_cache["t"]` fix~~ ✅ Hotové
2. H2 — Lock safety v `_rls_maybe_check` (try/except okolo `t.start()`)
3. H6 — Double CloseHandle fix
4. H8 — `_model_label()` suffix strip
5. M4 — session_id null handling
6. M13 — Dead code removal

### Fáza 2 — Robustnosť
7. H3 — `_apply_update_action()` do background threadu
8. H5 — History read limit reduction
9. M1 — `time.monotonic()` namiesto float drift
10. M2 — NamedTemporaryFile try/finally
11. M3 — `codecs.lookup()` pre encoding check
12. M7 — ANSI regex rozšírenie
13. M8 — SIGTERM double cleanup

### Fáza 3 — Testy
14. C1-C3 — Testy pre `_get_pricing`, `_cost_timeline`, rls.json write
15. Edge case testy (boundary, Unicode, symlink)
16. Integration testy pre `statusline.py:main()`

### Fáza 4 — Údržba
17. M10 — SECURITY.md deduplikácia
18. M11 — PROMO.md git tracking fix
19. L13 — Platform detection konzistencia
20. L15 — Model pricing externalizácia

---

*Generované z 6 paralelných auditov. 14 zdrojov konzultovaných pre platform research.*
