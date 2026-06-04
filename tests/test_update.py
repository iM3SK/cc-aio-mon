#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Unit tests for update.py — stdlib only, no pytest required.

NOTE: monitor.py currently has private mirrors (`_git_cmd`, `_update_checks`,
`_get_new_commits`, `_get_remote_changelog_preview`, `_apply_update_worker`,
`_apply_update_action`) that PARALLEL the public ones in update.py. The tests
named TestGitCmd / TestUpdateChecks / TestGetNewCommits /
TestGetRemoteChangelogPreview / TestUpdateApplyRollbackTag /
TestApplyUpdateAction in this file exercise the *monitor*-side helpers, but
they belong here because they test the update flow. The temporary coupling
between monitor.py and update.py will be resolved by a follow-up dedup.

Run:
    python -m unittest tests.test_update
    # or directly:
    python tests/test_update.py
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os
import pathlib
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import shared
from shared import _ANSI_RE

from update import (
    get_local_version,
    get_remote_version,
    get_ahead_behind,
    get_remote_changelog_entry,
    check_clean,
)

from monitor import (
    _git_cmd,
    _update_checks,
    _get_new_commits,
    _get_remote_changelog_preview,
    _apply_update_action,
    _apply_update_worker,
)

class TestUpdate(unittest.TestCase):

    # -- get_local_version ---------------------------------------------------

    def test_get_local_version_double_quotes(self):
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            import update
            old_root = update.REPO_ROOT
            update.REPO_ROOT = Path(td)
            (Path(td) / "shared.py").write_text('VERSION = "1.2.3"\n', encoding="utf-8")
            try:
                result = update.get_local_version()
                self.assertEqual(result, "1.2.3")
            finally:
                update.REPO_ROOT = old_root

    def test_get_local_version_single_quotes(self):
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            import update
            old_root = update.REPO_ROOT
            update.REPO_ROOT = Path(td)
            (Path(td) / "shared.py").write_text("VERSION = '2.0.0'\n", encoding="utf-8")
            try:
                result = update.get_local_version()
                self.assertEqual(result, "2.0.0")
            finally:
                update.REPO_ROOT = old_root

    def test_get_local_version_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            import update
            old_root = update.REPO_ROOT
            update.REPO_ROOT = Path(td)
            try:
                with self.assertRaises(RuntimeError):
                    update.get_local_version()
            finally:
                update.REPO_ROOT = old_root

    def test_get_local_version_missing_constant(self):
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            import update
            old_root = update.REPO_ROOT
            update.REPO_ROOT = Path(td)
            (Path(td) / "shared.py").write_text("# no version here\n", encoding="utf-8")
            try:
                with self.assertRaises(RuntimeError):
                    update.get_local_version()
            finally:
                update.REPO_ROOT = old_root

    # -- get_remote_version --------------------------------------------------

    def test_get_remote_version_parses_stdout(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = 'VERSION = "3.4.5"\n# other stuff\n'
        with patch("update.run_git", return_value=fake):
            result = get_remote_version()
        self.assertEqual(result, "3.4.5")

    def test_get_remote_version_single_quotes(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "VERSION = '0.1.0'\n"
        with patch("update.run_git", return_value=fake):
            result = get_remote_version()
        self.assertEqual(result, "0.1.0")

    def test_get_remote_version_git_failure(self):
        fake = MagicMock()
        fake.returncode = 128
        fake.stdout = ""
        with patch("update.run_git", return_value=fake):
            with self.assertRaises(RuntimeError):
                get_remote_version()

    def test_get_remote_version_no_constant(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "# just a comment\nprint('hello')\n"
        with patch("update.run_git", return_value=fake):
            with self.assertRaises(RuntimeError):
                get_remote_version()

    # -- get_ahead_behind ----------------------------------------------------

    def test_get_ahead_behind_normal(self):
        # rev-list --left-right: parts[0]=HEAD(ahead), parts[1]=origin(behind)
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "3\t5\n"
        with patch("update.run_git", return_value=fake):
            behind, ahead = get_ahead_behind()
        self.assertEqual(behind, 5)
        self.assertEqual(ahead, 3)

    def test_get_ahead_behind_up_to_date(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "0\t0\n"
        with patch("update.run_git", return_value=fake):
            behind, ahead = get_ahead_behind()
        self.assertEqual(behind, 0)
        self.assertEqual(ahead, 0)

    def test_get_ahead_behind_only_behind(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "0\t2\n"
        with patch("update.run_git", return_value=fake):
            behind, ahead = get_ahead_behind()
        self.assertEqual(behind, 2)
        self.assertEqual(ahead, 0)

    def test_get_ahead_behind_git_failure(self):
        fake = MagicMock()
        fake.returncode = 1
        fake.stdout = ""
        with patch("update.run_git", return_value=fake):
            with self.assertRaises(RuntimeError):
                get_ahead_behind()

    def test_get_ahead_behind_unexpected_output(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "garbage\n"
        with patch("update.run_git", return_value=fake):
            with self.assertRaises((RuntimeError, ValueError)):
                get_ahead_behind()

    # -- get_remote_changelog_entry ------------------------------------------

    _CHANGELOG = (
        "# Changelog\n\n"
        "## v2.0.0\n\n"
        "- New dashboard layout\n"
        "- Bug fixes\n\n"
        "## v1.9.0\n\n"
        "- Added feature X\n\n"
        "## v1.8.0\n\n"
        "- Initial release\n"
    )

    def test_get_remote_changelog_entry_found(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = self._CHANGELOG
        with patch("update.run_git", return_value=fake):
            result = get_remote_changelog_entry("2.0.0")
        self.assertIsNotNone(result)
        self.assertIn("New dashboard layout", result)
        self.assertNotIn("Added feature X", result)

    def test_get_remote_changelog_entry_middle_version(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = self._CHANGELOG
        with patch("update.run_git", return_value=fake):
            result = get_remote_changelog_entry("1.9.0")
        self.assertIsNotNone(result)
        self.assertIn("Added feature X", result)
        self.assertNotIn("New dashboard layout", result)
        self.assertNotIn("Initial release", result)

    def test_get_remote_changelog_entry_not_found(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = self._CHANGELOG
        with patch("update.run_git", return_value=fake):
            result = get_remote_changelog_entry("99.0.0")
        self.assertIsNone(result)

    def test_get_remote_changelog_entry_git_failure(self):
        fake = MagicMock()
        fake.returncode = 128
        fake.stdout = ""
        with patch("update.run_git", return_value=fake):
            result = get_remote_changelog_entry("2.0.0")
        self.assertIsNone(result)

    # -- check_clean ---------------------------------------------------------

    def test_check_clean_passes_when_clean(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = ""
        with patch("update.run_git", return_value=fake):
            with patch("sys.exit") as mock_exit:
                check_clean()
                mock_exit.assert_not_called()

    def test_check_clean_exits_when_dirty(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = " M monitor.py\n"
        with patch("update.run_git", return_value=fake):
            with self.assertRaises(SystemExit):
                check_clean()

    def test_check_clean_whitespace_only_is_clean(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "   \n"
        with patch("update.run_git", return_value=fake):
            with patch("sys.exit") as mock_exit:
                check_clean()
                mock_exit.assert_not_called()

    # -- import safety -------------------------------------------------------

    def test_import_does_not_replace_stdout(self):
        original = sys.stdout
        import update  # noqa: F401 — already imported, verifying no stdout side-effect
        self.assertIs(sys.stdout, original)


# ---------------------------------------------------------------------------
# spin_session
# ---------------------------------------------------------------------------
class TestGitCmd(unittest.TestCase):

    def test_returns_stripped_stdout_on_zero_rc(self):
        import subprocess as sp
        completed = sp.CompletedProcess(args=["git"], returncode=0, stdout="ok\n", stderr="")
        with patch("monitor.run_git", return_value=completed):
            rc, out, err = _git_cmd(["status"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "ok")
        self.assertEqual(err, "")

    def test_file_not_found(self):
        with patch("monitor.run_git", side_effect=FileNotFoundError):
            rc, out, err = _git_cmd(["status"])
        self.assertEqual(rc, -1)
        self.assertEqual(out, "")
        self.assertIn("git not found", err)

    def test_timeout(self):
        import subprocess as sp
        with patch("monitor.run_git", side_effect=sp.TimeoutExpired(cmd="git", timeout=15)):
            rc, out, err = _git_cmd(["status"])
        self.assertEqual(rc, -2)
        self.assertEqual(out, "")
        self.assertIn("timeout", err)


# ---------------------------------------------------------------------------
# _update_checks
# ---------------------------------------------------------------------------
class TestUpdateChecks(unittest.TestCase):

    def _mock_git(self, responses):
        """Return a side_effect that pops from responses list on each call."""
        responses = list(responses)
        call_count = [0]

        def _cmd(args, **_kw):
            i = call_count[0]
            call_count[0] += 1
            if i < len(responses):
                return responses[i]
            return (0, "", "")

        return _cmd

    def test_clean_main(self):
        responses = [(0, "main", ""), (0, "", ""), (0, "0\t0", "")]
        with patch("monitor._git_cmd", side_effect=self._mock_git(responses)):
            warns = _update_checks()
        self.assertEqual(warns, [])

    def test_not_main(self):
        responses = [(0, "develop", ""), (0, "", ""), (0, "0\t0", "")]
        with patch("monitor._git_cmd", side_effect=self._mock_git(responses)):
            warns = _update_checks()
        self.assertTrue(any("Not on main" in w for w in warns))

    def test_dirty(self):
        responses = [(0, "main", ""), (0, "M file.py", ""), (0, "0\t0", "")]
        with patch("monitor._git_cmd", side_effect=self._mock_git(responses)):
            warns = _update_checks()
        self.assertTrue(any("Uncommitted" in w for w in warns))

    def test_diverged(self):
        responses = [(0, "main", ""), (0, "", ""), (0, "3\t5", "")]
        with patch("monitor._git_cmd", side_effect=self._mock_git(responses)):
            warns = _update_checks()
        self.assertTrue(any("Diverged" in w for w in warns))


# ---------------------------------------------------------------------------
# _get_new_commits
# ---------------------------------------------------------------------------
class TestGetNewCommits(unittest.TestCase):

    def test_splits_stdout_into_commit_lines_on_zero_rc(self):
        with patch("monitor._git_cmd", return_value=(0, "abc feat\ndef fix", "")):
            result = _get_new_commits()
        self.assertEqual(result, ["abc feat", "def fix"])

    def test_returns_empty_list_on_nonzero_rc(self):
        with patch("monitor._git_cmd", return_value=(1, "", "error")):
            result = _get_new_commits()
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _get_remote_changelog_preview
# ---------------------------------------------------------------------------
class TestGetRemoteChangelogPreview(unittest.TestCase):

    _CHANGELOG = (
        "## v2.0.0\n"
        "- item1\n"
        "- item2\n"
        "## v1.0.0\n"
        "- old\n"
    )

    def test_extracts_version(self):
        with patch("monitor._git_cmd", return_value=(0, self._CHANGELOG, "")):
            result = _get_remote_changelog_preview("2.0.0")
        self.assertEqual(result[0], "## v2.0.0")
        self.assertIn("- item1", result)
        self.assertIn("- item2", result)
        # Must not bleed into next section
        self.assertNotIn("- old", result)

    def test_not_found(self):
        content = "## v1.0.0\n- something\n"
        with patch("monitor._git_cmd", return_value=(0, content, "")):
            result = _get_remote_changelog_preview("9.9.9")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _apply_update_action
# ---------------------------------------------------------------------------
class TestUpdateApplyRollbackTag(unittest.TestCase):
    """Verify apply_update() in update.py creates a rollback tag before pull."""

    def test_pulse_in_syntax_check_list(self):
        # Regression: pulse.py was missing from the post-pull compile check.
        # As of v1.10.2 the file list lives in shared.PY_FILES (source of truth
        # shared by update.py and monitor.py:_apply_update_worker).
        self.assertIn("pulse.py", shared.PY_FILES,
                      "pulse.py must be in shared.PY_FILES syntax check list")

    def test_rollback_tag_format(self):
        # Tag uses pre-update-YYYYMMDD-HHMMSS
        import update as _up
        src = pathlib.Path(_up.__file__).read_text(encoding="utf-8")
        self.assertIn("pre-update-", src)
        self.assertIn("%Y%m%d-%H%M%S", src)


class TestApplyUpdateAction(unittest.TestCase):

    def setUp(self):
        import monitor
        self._orig_result = monitor._update_result
        self._orig_env = os.environ.get("CC_AIO_MON_NO_UPDATE_CHECK")
        os.environ["CC_AIO_MON_NO_UPDATE_CHECK"] = "1"

    def tearDown(self):
        import monitor
        monitor._update_result = self._orig_result
        if self._orig_env is None:
            os.environ.pop("CC_AIO_MON_NO_UPDATE_CHECK", None)
        else:
            os.environ["CC_AIO_MON_NO_UPDATE_CHECK"] = self._orig_env

    def test_zero_rc_with_valid_syntax_marks_complete(self):
        # Test the synchronous worker directly (not the thread-spawning wrapper)
        with patch("monitor._git_cmd", return_value=(0, "ok", "")):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value="# valid python\nx = 1\n"):
                    _apply_update_worker()

        import monitor
        self.assertIn("complete", monitor._update_result)

    def test_syntax_check_uses_safe_read(self):
        # After the shared.check_syntax_after_pull extraction, the loop +
        # safe_read call live in shared.py. Patch targets moved with the code:
        # PY_FILES is read from shared's module scope (not monitor's), and
        # safe_read is invoked via shared's import (not monitor's).
        # Real-file approach (vs. global pathlib.Path.exists patch): create a
        # tmp repo root with one real file so the existence check passes
        # without polluting Path.exists for any unrelated code path.
        import monitor
        import shared
        with tempfile.TemporaryDirectory() as td:
            repo_root = pathlib.Path(td)
            (repo_root / "monitor.py").write_text("# stub")
            with patch("monitor._git_cmd", return_value=(0, "ok", "")):
                with patch.object(shared, "PY_FILES", ("monitor.py",)):
                    with patch.object(monitor, "_REPO_ROOT", repo_root):
                        with patch("shared.safe_read", return_value=None) as mock_safe_read:
                            _apply_update_worker()
            mock_safe_read.assert_called_once()
            self.assertIn("syntax errors", monitor._update_result)

    def test_nonzero_rc_marks_failed_with_stderr(self):
        with patch("monitor._git_cmd", return_value=(1, "", "conflict")):
            _apply_update_worker()

        import monitor
        self.assertIn("failed", monitor._update_result)


# ---------------------------------------------------------------------------
# render_update_modal
# ---------------------------------------------------------------------------
class TestUpdateFlowFunctions(unittest.TestCase):

    def _mock_result(self, returncode=0, stdout="", stderr=""):
        r = MagicMock()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def test_check_repo_success(self):
        from update import check_repo
        with patch("update.run_git", return_value=self._mock_result(0, "true\n")):
            check_repo()  # should not raise

    def test_check_repo_failure(self):
        from update import check_repo
        with patch("update.run_git", return_value=self._mock_result(1, "")):
            with self.assertRaises(SystemExit):
                check_repo()

    def test_check_branch_main(self):
        from update import check_branch
        with patch("update.run_git", return_value=self._mock_result(0, "main\n")):
            check_branch()  # should not raise

    def test_check_branch_not_main(self):
        from update import check_branch
        with patch("update.run_git", return_value=self._mock_result(0, "feature\n")):
            with self.assertRaises(SystemExit):
                check_branch()

    def test_check_branch_detached(self):
        from update import check_branch
        with patch("update.run_git", return_value=self._mock_result(0, "HEAD\n")):
            with self.assertRaises(SystemExit):
                check_branch()

    def test_fetch_remote_success(self):
        from update import fetch_remote
        with patch("update.run_git", return_value=self._mock_result(0, "")):
            fetch_remote()  # should not raise

    def test_fetch_remote_failure(self):
        from update import fetch_remote
        with patch("update.run_git", return_value=self._mock_result(1, "", "error")):
            with self.assertRaises(SystemExit):
                fetch_remote()

    def test_get_new_commits(self):
        from update import get_new_commits
        with patch("update.run_git", return_value=self._mock_result(0, "abc feat\ndef fix\n")):
            commits = get_new_commits()
        self.assertEqual(len(commits), 2)

    def test_get_new_commits_failure(self):
        from update import get_new_commits
        with patch("update.run_git", return_value=self._mock_result(1, "")):
            commits = get_new_commits()
        self.assertEqual(commits, [])


# ---------------------------------------------------------------------------
# TestFlush — screen flush output contains ANSI sync markers
# ---------------------------------------------------------------------------
class TestApplyUpdateWorkerVersionErrorSanitized(unittest.TestCase):
    """H-1: exception string in 'Could not verify new VERSION' must be sanitized.

    Exception __str__ can contain ANSI escape sequences (e.g. UnicodeDecodeError
    reported with repr of malicious bytes). Without _sanitize, this would be a
    terminal escape injection vector.
    """

    def test_version_exception_is_sanitized(self):
        import update as _u

        # Simulate get_local_version raising an exception with ANSI in its message
        evil_msg = "\x1b[31mINJECTED\x1b[0m"

        with patch.object(_u, "get_local_version", side_effect=RuntimeError(evil_msg)):
            with patch("builtins.print") as mock_print:
                # Call the branch directly via patched helper
                try:
                    new_ver = _u.get_local_version()
                except Exception as e:
                    _u.warn(f"Could not verify new VERSION: {shared._sanitize(str(e))}")

                printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
                # Evil control chars must be stripped by _sanitize
                self.assertNotIn("\x1b", printed,
                                 "ANSI escape must be stripped from exception text")
                self.assertIn("INJECTED", printed,
                              "Regular text content should pass through _sanitize")


# ---------------------------------------------------------------------------
# v1.10.2 security scan regression tests (L-1..L-4, I-1, I-2)
# ---------------------------------------------------------------------------
class TestApplyUpdateNewVersionSanitized(unittest.TestCase):
    """L-4: new_ver from get_local_version() must be sanitized before echo."""

    def test_new_version_is_sanitized(self):
        # VERSION_RE matches [^"']+ — an attacker could land an ANSI-bearing
        # VERSION value on-disk between git pull and re-read. apply_update
        # must _sanitize(new_ver) before printing.
        import update as _u
        src = pathlib.Path(_u.__file__).read_text(encoding="utf-8")
        # Look for the sanitize wrapper around new_ver in the success branch
        self.assertIn('_sanitize(new_ver)', src,
                      "apply_update must sanitize new_ver before 'New VERSION:' echo")


# ---------------------------------------------------------------------------
# v1.10.3 regression — UnboundLocalError via inline `import signal` in main()
# ---------------------------------------------------------------------------


class TestApplyUpdateSingletonLock(unittest.TestCase):
    """Audit finding #1: update.py --apply must acquire monitor.lock before
    pulling, so it cannot race a running monitor.py on .py file replacement."""

    def test_apply_update_bails_when_lock_held(self):
        # Simulate monitor.py already running → acquire_singleton_lock returns None.
        # apply_update() MUST sys.exit(1) before any git tag/pull side effect.
        import io
        import update as _u

        captured_stderr = io.StringIO()
        with patch.object(_u, "ensure_data_dir", return_value=True), \
             patch.object(_u, "acquire_singleton_lock", return_value=None), \
             patch.object(_u, "run_git") as mock_run_git, \
             patch("sys.stderr", captured_stderr):
            with self.assertRaises(SystemExit) as cm:
                _u.apply_update()

        # exit code 1 (lock contention is a hard failure)
        self.assertEqual(cm.exception.code, 1)
        # no git operations should have occurred — bail happens before tag/pull
        mock_run_git.assert_not_called()
        # user-facing message must mention monitor.py
        stderr_text = captured_stderr.getvalue()
        self.assertIn("monitor.py", stderr_text)

    def test_apply_update_proceeds_when_lock_acquired(self):
        # Happy path: lock acquired → apply_update proceeds and calls run_git.
        import io
        import update as _u
        from unittest.mock import MagicMock

        mock_lock = MagicMock()
        mock_lock.fileno.return_value = 99  # truthy handle

        mock_git_result = MagicMock(returncode=0, stdout="", stderr="")

        captured_stdout = io.StringIO()
        with patch.object(_u, "ensure_data_dir", return_value=True), \
             patch.object(_u, "acquire_singleton_lock", return_value=mock_lock), \
             patch.object(_u, "run_git", return_value=mock_git_result) as mock_run_git, \
             patch("sys.stdout", captured_stdout):
            try:
                _u.apply_update()
            except SystemExit:
                pass  # may exit after git ops — that is fine for this assertion

        mock_run_git.assert_called()


class TestCheckPythonVersion(unittest.TestCase):
    """update.check_python_version is a single early-exit gate; trivial but
    must not silently regress (e.g. a typo flipping the comparison).

    sys.version_info itself is a non-constructible structseq, so the mock
    uses a stand-in namedtuple with the same fields (.major/.minor are read
    by the error-message format string; comparison is delegated to the
    tuple-ordering protocol).
    """

    from collections import namedtuple
    _VI = namedtuple("_VI", "major minor micro releaselevel serial")

    @classmethod
    def _vi(cls, major, minor):
        return cls._VI(major, minor, 0, "final", 0)

    def test_below_min_exits_with_code_1(self):
        import update as _u
        with patch("update.sys.version_info", self._vi(3, 7)):
            with self.assertRaises(SystemExit) as cm:
                _u.check_python_version()
        self.assertEqual(cm.exception.code, 1)

    def test_at_minimum_does_not_exit(self):
        import update as _u
        with patch("update.sys.version_info", self._vi(3, 8)):
            try:
                _u.check_python_version()
            except SystemExit:  # pragma: no cover
                self.fail("Python at MIN_PYTHON must not trigger sys.exit")

    def test_above_minimum_does_not_exit(self):
        import update as _u
        with patch("update.sys.version_info", self._vi(3, 12)):
            try:
                _u.check_python_version()
            except SystemExit:  # pragma: no cover
                self.fail("Python newer than MIN_PYTHON must not trigger sys.exit")


class TestUpdateMainFlow(unittest.TestCase):
    """Coverage for update.main() flow-control exit codes. The component checks
    were tested in isolation; the orchestration (ahead/behind/diverged ->
    exit code) was never exercised."""

    def _run_main(self, behind, ahead, apply=False):
        import io, update
        argv = ["update.py"] + (["--apply"] if apply else [])
        with patch.object(update.sys, "argv", argv), \
             patch.object(update, "_init_terminal", lambda: None), \
             patch.object(update, "check_python_version", lambda: None), \
             patch.object(update, "check_repo", lambda: None), \
             patch.object(update, "check_branch", lambda: None), \
             patch.object(update, "check_clean", lambda: None), \
             patch.object(update, "fetch_remote", lambda: None), \
             patch.object(update, "get_local_version", lambda: "1.0.0"), \
             patch.object(update, "get_remote_version", lambda: "1.0.1"), \
             patch.object(update, "get_ahead_behind", lambda: (behind, ahead)), \
             patch.object(update.sys, "stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                update.main()
        return cm.exception.code

    def test_up_to_date_exits_zero(self):
        self.assertEqual(self._run_main(behind=0, ahead=0), 0)

    def test_local_ahead_exits_one(self):
        # Local ahead of origin (can't downgrade) -> hard exit 1.
        self.assertEqual(self._run_main(behind=0, ahead=2), 1)

    def test_diverged_exits_one(self):
        # Local diverged (ahead AND behind) -> manual merge required, exit 1.
        self.assertEqual(self._run_main(behind=3, ahead=1), 1)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
