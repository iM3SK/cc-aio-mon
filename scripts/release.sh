#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# release.sh — release pre-flight for cc-aio-mon.
#
# Validates that the working tree is ready to cut version X.Y.Z and runs the
# full gate (version bump check, CHANGELOG entry, compile check, test suite),
# then prints the exact PR-based push sequence from docs/RELEASE.md Section 5.
#
# It deliberately does NOT push, tag, or merge: `main` is branch-protected
# (enforce_admins), so a release lands via PR. This script eliminates the
# slip risk of a manual checklist (forgotten VERSION bump, missing CHANGELOG
# entry, failing tests) without performing any irreversible action.
#
# Usage:  scripts/release.sh X.Y.Z
#
# Maintainer tool (Linux/macOS bash). Not shipped to end users, not part of the
# five runtime modules in shared.PY_FILES.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ok()   { printf '[OK]  %s\n' "$1"; }
errln(){ printf '[!!]  %s\n' "$1" >&2; }

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    errln "Usage: scripts/release.sh X.Y.Z"
    exit 2
fi
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    errln "Version must be X.Y.Z (got: $VERSION)"
    exit 2
fi

PY="$(command -v python3 || command -v py || true)"
if [ -z "$PY" ]; then
    errln "python3 / py not found on PATH"
    exit 1
fi

# Escape dots for use in an ERE.
VERSION_RE="${VERSION//./\\.}"
fail=0

# 1) shared.VERSION must already be bumped to match the requested release.
#    Tolerate an import failure (e.g. a syntax error in shared.py) under `set -e`
#    so the fail-accumulator keeps going and the compile gate (step 3) reports
#    the precise error location instead of a bare traceback aborting the script.
SHARED_VER="$("$PY" -c "import shared; print(shared.VERSION)" 2>/dev/null || true)"
if [ "$SHARED_VER" = "$VERSION" ]; then
    ok "shared.VERSION == $VERSION"
elif [ -z "$SHARED_VER" ]; then
    errln "could not read shared.VERSION (shared.py import failed — see compile check below)"
    fail=1
else
    errln "shared.VERSION is $SHARED_VER, expected $VERSION — bump shared.py first"
    fail=1
fi

# 2) CHANGELOG must carry a matching entry (## vX.Y.Z ...).
if grep -Eq "^## v${VERSION_RE}([^0-9]|$)" CHANGELOG.md; then
    ok "CHANGELOG.md has a '## v$VERSION' entry"
else
    errln "CHANGELOG.md is missing a '## v$VERSION' entry"
    fail=1
fi

# 3) Compile gate — same file list as CI and the post-update syntax check.
if "$PY" -c "import py_compile, shared; [py_compile.compile(f, doraise=True) for f in shared.PY_FILES]"; then
    ok "Syntax check passed (PY_FILES)"
else
    errln "Syntax check failed"
    fail=1
fi

# 4) Full test suite must pass.
if "$PY" tests.py >/dev/null 2>&1; then
    ok "Test suite passed"
else
    errln "Test suite failed — run '$PY tests.py' to see details"
    fail=1
fi

if [ "$fail" -ne 0 ]; then
    errln "Pre-flight failed — fix the above before releasing."
    exit 1
fi

cat <<EOF

All pre-flight checks passed for v$VERSION.

main is branch-protected — land the release via PR (docs/RELEASE.md Section 5):

  git switch -c release/v$VERSION
  git add CHANGELOG.md shared.py
  git commit -m "chore(release): bump to v$VERSION"
  git push origin release/v$VERSION
  gh pr create --fill --base main
  gh pr merge --squash --delete-branch
  git switch main && git pull --ff-only origin main
  git tag v$VERSION && git push origin v$VERSION

Then complete the Section 8 post-release checks.
EOF
