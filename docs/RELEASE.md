# Release Guide — CC AIO MON

Audience: any maintainer doing a release, whether it is your first or you have
not touched this repo in six months. Follow the checklist top to bottom.
Every step is grounded in observable codebase behavior — no invented tooling.

See also: [ARCHITECTURE.md](ARCHITECTURE.md) for module overview, [FILE-IPC-CONTRACT.md](FILE-IPC-CONTRACT.md) for IPC schema bump procedure and self-update integration constraints.

---

## 1. SemVer policy

**MAJOR** — bump when existing installs break on `git pull --ff-only`:

- Removing or renaming a statusLine config path that Claude Code reads.
- Changing the public Python import API (functions/classes that external code
  could import from `shared.py`).
- Removing CLI flags from `update.py` or `statusline.py`.
- Bumping `SCHEMA_VERSION` in `shared.py`. `monitor.load_state()` refuses a
  snapshot tagged newer than the running build (it degrades to `None` = "no
  data" until that monitor self-updates), so a bump causes a transient blind
  spot for already-running older monitors rather than a crash or hard break.
  Treat a bump as MINOR when existing fields stay readable; reserve MAJOR for
  removing or retyping existing fields.

**MINOR** — new features, new env-var knobs, new modals, new segments. Recent
examples: v1.12.0 (singleton lock, crash-log rotation, schema version tag,
`shared.check_syntax_after_pull`, `shared.parse_ahead_behind`); v1.11.0
(AI-generated session titles, lifetime activity panel, server-side tool
counts); v1.10.0 (reset countdown in rate-limit segments); v1.9.0 (Pulse
module).

**PATCH** — bug fixes and security hardening with no user-visible behavior
change. Recent examples: v1.11.1 (transcript-root validation, bounded
syntax-check read in update worker); v1.10.6 (release-check worker reading
from wrong file, null-payload dashboard crash).

> When in doubt, bump higher. Users prefer over-versioning to a surprise
> breakage on `git pull`.

---

## 2. Pre-release checklist

Work through these in order before creating any tag.

> Tip: `scripts/release.sh X.Y.Z` runs the mechanical gate of this checklist
> (VERSION bump match, CHANGELOG entry, compile check, full test suite) and then
> prints the PR push sequence from Section 5. It performs no irreversible action
> — it never pushes, tags, or merges.

- [ ] **Branch is `main`, working tree is clean.**
  ```
  git status --porcelain -uno
  ```
  Must produce no output. Untracked files are fine (`-uno` ignores them,
  matching `update.py:check_clean()`).

- [ ] **Tests pass and count is >= baseline.**
  ```
  py tests.py          # Windows
  python3 tests.py     # macOS / Linux
  ```
  `tests.py` is a thin wrapper that runs `unittest discover tests/`
  (`tests.py:main()`). Current baseline: **678 passing** (v1.13.0). The new
  release's count must be >= this number unless tests were intentionally
  removed (document the removal in CHANGELOG).

- [ ] **CHANGELOG entry drafted** (see Section 3 for exact format).
  Write the entry for the new version at the top of `CHANGELOG.md`, above the
  current `## v1.12.4` block. Do not push yet.

- [ ] **VERSION constant bumped in `shared.py` only.**
  The constant lives at `shared.py`:
  ```python
  VERSION = "1.12.4"
  ```
  Change this string to the new version. Do not touch `monitor.py`,
  `pulse.py`, or `update.py` for the version — all three import from
  `shared.py` (`from shared import VERSION` or similar). `update.py`'s
  `get_local_version()` and `get_remote_version()` both regex-scan
  `shared.py` via `VERSION_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']',
  re.MULTILINE)` (`shared.py`). If you put the version anywhere else,
  the release-check worker silently reports `error`.

- [ ] **Re-run tests after the VERSION and CHANGELOG edits.**
  Confirm the count is still correct and no test imports a hard-coded version
  string that needs updating.

---

## 3. CHANGELOG entry format

### What qualifies as a CHANGELOG entry

`CHANGELOG.md` is for **user-visible application changes only**:

- features
- bug fixes
- security fixes
- behavior changes
- CLI / output / API / protocol changes
- new env vars or release-relevant setup changes

Do **not** add CHANGELOG entries and do **not** create a release for:

- contributor docs (`CONTRIBUTING.md`, `docs/RELEASE.md`, this file)
- workflow / process rule updates
- audit notes
- internal-only repository maintenance
- CI or hook changes that do not affect shipped behavior

**Simple test:** if a user running `py monitor.py` would not notice a
difference, it is not a CHANGELOG entry and not a release trigger.

### Entry format

The established format is: heading, blank line, one or more **bold** subsection
headers, bullet list under each, blank line, trailing test count line.

### Template

```markdown
## vX.Y.Z — YYYY-MM-DD

**<Category — short descriptor phrase>:**
- **Feature name.** One or two sentences in present tense describing what
  the feature does and why. Cite specific file:function when relevant.
  Cross-platform caveats noted inline.
- **Second feature.** Same style.

**<Second category>:**
- Extract/consolidation described as "X is now Y" or "Detect X".

**<Third category — optional>:**
- Structural or documentation changes.

**Tests:** N passing (+M).
```

### Observed conventions (from v1.12.0 and v1.11.1)

- Subsection headers use **bold** (not `###` headings): `**New features —
  operational reliability:**`, `**Refactor — single source of truth:**`,
  `**Repository structure — developer experience:**`, `**Security
  hardening:**`.
- Bullet prose style: "Detect X", "Now does Y", "Extract Z to shared.W".
  Active voice, present tense. Not "Fixed" (that is PATCH language) for MINOR.
- For PATCH releases: subsection headers like `**Bug fixes:**`,
  `**Security hardening:**`, `**Documentation:**` (v1.11.1 pattern).
- Sub-bullets under a feature bullet are indented two spaces and cite the
  specific mechanism: `Cross-platform via \`fcntl.flock\` (Unix) and
  \`msvcrt.locking\` (Windows)`.
- The test count line is always last, bold label, no period after `passing`:
  `**Tests:** 583 passing (+41).`
- The `(+M)` delta is relative to the previous release's stated count.
- If a PATCH release adds no tests, omit the delta: `**Tests:** 542 passing.`
- ISO 8601 date in the heading (`YYYY-MM-DD`).

---

## 4. Verification before tagging

- [ ] **Full test suite — final run.**
  ```
  py tests.py
  ```
  All tests must pass. The `tests/` package has one module per source file
  (`test_statusline.py`, `test_monitor.py`, `test_shared.py`,
  `test_pulse.py`, `test_update.py`). The `tests.py` wrapper runs all of
  them via `unittest discover`.

- [ ] **Syntax check covers all five modules.**
  `shared.PY_FILES` (`shared.py`) lists every file the post-pull syntax
  check verifies:
  ```python
  PY_FILES = ("monitor.py", "statusline.py", "shared.py", "pulse.py", "update.py")
  ```
  Confirm your changes compile cleanly in all five:
  ```
  py -c "import py_compile; [py_compile.compile(f) for f in ['monitor.py','statusline.py','shared.py','pulse.py','update.py']]"
  ```

- [ ] **Self-update dry run (read-only).**
  ```
  py update.py          # Windows — no --apply flag
  python3 update.py     # macOS / Linux
  ```
  This runs `fetch_remote()` → `get_remote_version()` → `get_ahead_behind()`.
  If the new commit is already on `origin/main` (i.e. you pushed the
  CHANGELOG + VERSION bump), `update.py` must report the new version as
  remote and show `behind: 1` (or however many commits). If it still shows
  `Already up to date`, the push has not reached origin yet — do not tag.

- [ ] **Cross-platform (if possible).**
  The README CI badge documents: Ubuntu 3.8 / 3.10 / 3.11 / 3.12, Windows
  3.12, macOS 3.12. If you only have one platform locally, push to a branch
  first and let CI run before merging and tagging.

---

## 5. Tagging and push order

Order matters. The self-update mechanism is commit-driven, not tag-driven
(see Section 6). Follow this sequence exactly:

`main` is protected (`enforce_admins` is on), so the version bump lands via a
PR — direct `git push origin main` is rejected even for the maintainer. The tag
is still applied to the merge commit *after* it is on `main`:

```bash
# 1. Create a release branch and commit (CHANGELOG + shared.py version bump only)
git switch -c release/vX.Y.Z
git add CHANGELOG.md shared.py
git commit -m "chore(release): bump to vX.Y.Z"

# 2. Push the branch and open a PR against main
git push origin release/vX.Y.Z
gh pr create --fill --base main

# 3. Once CI is green (incl. release-smoke), squash-merge — this lands the
#    version bump on main HEAD, which is what self-update reads
gh pr merge --squash --delete-branch

# 4. Sync local main to the merge commit, THEN tag it (after it is on main)
git switch main && git pull --ff-only origin main
git tag vX.Y.Z

# 5. Push the tag separately
git push origin vX.Y.Z
```

**Why this order:** `update.py:get_remote_version()` reads
`origin/main:shared.py` via `git show` — it compares against the remote
branch HEAD, not against tags. Users who run `py update.py` (no `--apply`)
after the PR merges will see the new version as available. The tag must point
at a commit that is already on `main`; tagging before the merge lands would
leave the release-check worker blind to the version bump (it only reads
`origin/main`).

**Signed tags** (`git tag -s vX.Y.Z`) are optional but encouraged for
releases that touch security-sensitive code paths. The tag message should
repeat the first line of the CHANGELOG entry:

```bash
git tag -s vX.Y.Z -m "vX.Y.Z — <one-line summary from CHANGELOG>"
```

**Do not reuse or move tags.** If you tagged the wrong commit, see Section 7.

---

## 6. Self-update integration — what makes a release work

`py update.py --apply` succeeds for an end user only when all of the
following are true after your push:

| What the code checks | Where it reads | Failure mode if wrong |
|---|---|---|
| Remote VERSION string | `git show origin/main:shared.py` → `VERSION_RE` (`shared.py`) | Reports `error` in release indicator; `RuntimeError: VERSION constant not found in remote shared.py` on `--apply` |
| Remote CHANGELOG entry | `git show origin/main:CHANGELOG.md` → `extract_changelog_entry(text, version, max_lines=None)` (`shared.py`) | Update modal shows no changelog preview; not fatal |
| `git pull --ff-only` succeeds | Requires `main` is linear (no force-push, no rebase of published history) | `git pull` exits non-zero; user is left on old version with the rollback tag as recovery point |
| Post-pull syntax check passes | `shared.check_syntax_after_pull(repo_root)` iterates `PY_FILES` (`shared.py`) | Warns user `Syntax errors in: <file>` and shows rollback hint |

**Critical constraint:** `git pull --ff-only` requires that `origin/main` is a
fast-forward ancestor of the user's local `main`. If you ever rebase or
force-push `main` after users have pulled, `git pull --ff-only` will fail with
`fatal: Not possible to fast-forward, aborting` for every existing install.
There is no recovery path short of users manually running `git reset --hard
origin/main` (which discards any local changes). Do not rewrite published
history on `main`.

**The syntax check covers exactly the five files in `PY_FILES`.** If you add
a new `.py` module to the project, add it to `PY_FILES` in `shared.py` so
post-pull checks catch syntax errors in it.

**Blast radius — there is no staged rollout (by design).** Self-update is
trunk-based: every commit that lands on `main` reaches *all* auto-updating
installs at once (the in-monitor release check polls `origin/main` hourly).
There is no `next`/`beta` channel or canary cohort — a single maintainer does
not have the operational capacity to soak releases, and adding channels would
complicate the update path for a marginal cohort. The accepted mitigations are:

- **Pre-merge gate:** `release-smoke.yml` runs the *real* `update.py --apply`
  path from the previous tag on every push to `main`, so a broken self-update
  is caught in CI before users pull it.
- **Per-user rollback:** `update.py --apply` writes a `pre-update-*` tag before
  pulling (see Section 7), so any user can revert in one command.
- **Fast-forward-only safety:** `git pull --ff-only` refuses to apply a
  non-linear update rather than corrupting a working tree.

If the project ever grows beyond a single maintainer or adds higher-risk
self-modifying paths, revisit this with a `next`-branch soak step before the
`main` merge.

---

## 7. Rollback — if a release breaks something after tagging

`update.py --apply` automatically creates a local rollback tag on the user's
machine (`pre-update-YYYYMMDD-HHMMSS`, in `update.py:apply_update()`) before
running `git pull`. Users can recover with:

```bash
git reset --hard pre-update-20260522-143000
```

On the maintainer side, **do not delete the broken tag**. Users who already
pulled may have it in their local repo. Deleting it breaks their reference.
Instead:

1. Fix the issue in a new commit on a branch.
2. Merge it to `main` via PR (direct push is blocked — see Section 5).
3. Create a new patch tag: `git tag vX.Y.Z+1` (e.g. `v1.12.5` if `v1.12.4`
   was broken), push it.
4. Update CHANGELOG with a brief PATCH entry describing the regression and fix.

**Diagnostics for user-reported breakage:** the crash log lives at
`$TMPDIR/claude-aio-monitor/monitor-crash.log`. Since v1.12.2 the previous
traceback is always preserved as `monitor-crash.log.1` (the rotation runs
on every crash via `shared.rotate_crash_log(..., always=True)`; the 1 MB
size guard still applies when callers use the default `always=False`).
Ask users to
share the last 50 lines. The log includes full traceback, platform,
Python version, and encoding details.

---

## 8. Post-release checks

- [ ] **GitHub release page.** Create a GitHub release for the tag via the
  web UI or `gh release create vX.Y.Z`. Paste the new CHANGELOG entry as
  the release body. Verify the Markdown renders correctly (bold subsections,
  bullet nesting, trailing test count line).

- [ ] **`py update.py` from a clean clone.** On at least one platform, clone
  the repo fresh and run `py update.py` (no `--apply`). It must show
  `Already up to date.` — confirming the pushed VERSION matches what
  `get_remote_version()` sees.

- [ ] **Release indicator in monitor.** Start `py monitor.py` after the
  release. The release-check worker (`CC_AIO_MON_NO_UPDATE_CHECK=1` disables
  it) polls `origin/main:shared.py` hourly. Within one poll cycle the update
  indicator should show green `Up to date` if you are running the newly
  released version.

- [ ] **Issues / milestone.** If you maintain a `vX.Y.Z` label on GitHub
  Issues, move any resolved issues to `Closed` and open a `vX.Y.Z+1`
  milestone for the next cycle.
