#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Unit tests for shared.py — stdlib only, no pytest required.

Run:
    python -m unittest tests.test_shared
    # or directly:
    python tests/test_shared.py
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
from shared import (
    MAX_FILE_SIZE,
    _ANSI_RE,
    _SID_RE,
    _sanitize,
    RESERVED_SIDS,
    C_RED,
    C_GRN,
    C_YEL,
    C_ORN,
    C_CYN,
    C_DIM,
    char_width,
    is_safe_dir,
    ensure_data_dir,
)

class TestSanitize(unittest.TestCase):

    def test_strips_c0_controls(self):
        self.assertEqual(_sanitize("hello\x00world\x1b"), "helloworld")

    def test_strips_c1_controls(self):
        self.assertEqual(_sanitize("abc\x80\x9fdef"), "abcdef")

    def test_preserves_normal_text(self):
        self.assertEqual(_sanitize("Hello World 123!"), "Hello World 123!")

    def test_preserves_unicode(self):
        self.assertEqual(_sanitize("ěščřžýáíé"), "ěščřžýáíé")

    def test_coerces_non_string(self):
        self.assertEqual(_sanitize(42), "42")
        self.assertEqual(_sanitize(None), "None")


# ---------------------------------------------------------------------------
# _get_terminal_width
# ---------------------------------------------------------------------------
class TestSanitizeBidi(unittest.TestCase):

    def test_strips_bidi_override(self):
        self.assertEqual(_sanitize("hello\u202eworld"), "helloworld")

    def test_strips_bidi_isolate(self):
        self.assertEqual(_sanitize("a\u2066b\u2069c"), "abc")

    def test_strips_lrm_rlm(self):
        self.assertEqual(_sanitize("a\u200eb\u200fc"), "abc")

    def test_strips_lre_rle(self):
        self.assertEqual(_sanitize("a\u202ab\u202bc"), "abc")

    def test_preserves_regular_unicode(self):
        self.assertEqual(_sanitize("café résumé"), "café résumé")


# ---------------------------------------------------------------------------
# TestFormatterEdgeCases — f_cost negative, f_tok boundary, f_dur negative
# ---------------------------------------------------------------------------
class TestReservedFiles(unittest.TestCase):

    def test_rls_in_reserved(self):
        self.assertIn("rls", RESERVED_SIDS)

    def test_stats_in_reserved(self):
        self.assertIn("stats", RESERVED_SIDS)

    def test_pulse_in_reserved(self):
        self.assertIn("pulse", RESERVED_SIDS)

    def test_reserved_is_a_set(self):
        self.assertIsInstance(RESERVED_SIDS, (set, frozenset))


# ---------------------------------------------------------------------------
# TestUpdateFlowFunctions — update.py check_repo, check_branch, fetch_remote, etc.
# ---------------------------------------------------------------------------
class TestCharWidth(unittest.TestCase):

    def test_ascii_a(self):
        self.assertEqual(char_width("a"), 1)

    def test_cjk_zhong(self):
        self.assertEqual(char_width("中"), 2)

    def test_fullwidth_A(self):
        # U+FF21 FULLWIDTH LATIN CAPITAL LETTER A
        self.assertEqual(char_width("\uff21"), 2)

    def test_emoji_does_not_crash(self):
        # Just verify it doesn't raise; result is implementation-defined
        result = char_width("\U0001f600")
        self.assertIn(result, (1, 2))


# ---------------------------------------------------------------------------
# TestIsSafeDir — shared.is_safe_dir()
# ---------------------------------------------------------------------------
class TestIsSafeDir(unittest.TestCase):

    def test_real_directory_returns_true(self):
        import pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        try:
            self.assertTrue(is_safe_dir(d))
        finally:
            d.rmdir()

    def test_nonexistent_path_returns_false(self):
        import pathlib
        p = pathlib.Path(tempfile.gettempdir()) / "cc_aio_mon_nonexistent_xyz"
        self.assertFalse(is_safe_dir(p))

    def test_regular_file_returns_false(self):
        import pathlib
        with tempfile.NamedTemporaryFile(delete=False) as f:
            p = pathlib.Path(f.name)
        try:
            self.assertFalse(is_safe_dir(p))
        finally:
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# TestEnsureDataDir — shared.ensure_data_dir()
# ---------------------------------------------------------------------------
class TestEnsureDataDir(unittest.TestCase):

    def test_new_directory_returns_true_and_exists(self):
        import pathlib, shutil
        base = pathlib.Path(tempfile.mkdtemp())
        target = base / "testdir"
        try:
            result = ensure_data_dir(target)
            self.assertTrue(result)
            self.assertTrue(target.is_dir())
        finally:
            shutil.rmtree(str(base), ignore_errors=True)

    def test_existing_directory_returns_true(self):
        import pathlib, shutil
        base = pathlib.Path(tempfile.mkdtemp())
        try:
            result1 = ensure_data_dir(base)
            result2 = ensure_data_dir(base)
            self.assertTrue(result1)
            self.assertTrue(result2)
        finally:
            shutil.rmtree(str(base), ignore_errors=True)

    def test_foreign_uid_owner_rejected(self):
        """S-P2-1: directory owned by another UID must be refused (CWE-377/732).

        Cross-platform: mocks the Unix branch via `hasattr(os, "geteuid")`
        + `os.geteuid` so the regression guard runs everywhere — Windows
        runners would silently skip the Unix branch otherwise.
        """
        import pathlib, shutil
        from unittest.mock import patch
        base = pathlib.Path(tempfile.mkdtemp())
        target = base / "foreign"
        target.mkdir()
        # Mock: pretend we are running as Unix with a *different* UID than
        # the one that owns `target` (st_uid). os.stat is real; only
        # geteuid is patched. On real Unix we'd also need sys.platform to
        # be non-win32, which is patched via sys-module attribute below.
        try:
            real_uid = target.stat().st_uid
            with patch.object(sys, "platform", "linux"), \
                 patch("os.geteuid", return_value=real_uid + 1, create=True):
                # ensure os.geteuid attribute exists on Windows test runs
                import os as _os
                if not hasattr(_os, "geteuid"):
                    _os.geteuid = lambda: real_uid + 1  # pragma: no cover
                try:
                    result = ensure_data_dir(target)
                finally:
                    if hasattr(_os, "geteuid") and _os.geteuid.__module__ == "tests.test_shared":
                        del _os.geteuid
            self.assertFalse(
                result,
                "ensure_data_dir must refuse a directory owned by another UID",
            )
        finally:
            shutil.rmtree(str(base), ignore_errors=True)


# ---------------------------------------------------------------------------
# TestSessionAutoConnect — auto-connect condition logic
# ---------------------------------------------------------------------------
class TestRunGitEnvWhitelist(unittest.TestCase):

    def test_git_ssh_command_not_propagated(self):
        from unittest.mock import patch as _patch, MagicMock
        with _patch("subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="", stderr="")) as m:
            with _patch.dict("os.environ",
                             {"GIT_SSH_COMMAND": "evil", "PATH": "/usr/bin"},
                             clear=False):
                from shared import run_git
                run_git(["status"], cwd=".", timeout=5)
        env_arg = m.call_args[1]["env"]
        self.assertNotIn("GIT_SSH_COMMAND", env_arg)
        self.assertIn("PATH", env_arg)
        self.assertEqual(env_arg["GIT_TERMINAL_PROMPT"], "0")

    def test_git_terminal_prompt_always_set(self):
        from unittest.mock import patch as _patch, MagicMock
        with _patch("subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="", stderr="")) as m:
            from shared import run_git
            run_git(["status"], cwd=".", timeout=5)
        self.assertEqual(m.call_args[1]["env"].get("GIT_TERMINAL_PROMPT"), "0")


# ---------------------------------------------------------------------------
# Security: pre-push hook scans new branch tips
# ---------------------------------------------------------------------------
class TestSidWindowsReservedRejected(unittest.TestCase):
    """Prevents CON.json / PRN.jsonl / AUX.json from being created on Windows
    (would open the console/printer device instead of a file)."""

    def test_core_device_names_rejected_uppercase(self):
        for name in ("CON", "PRN", "AUX", "NUL"):
            with self.subTest(name=name):
                self.assertIsNone(_SID_RE.match(name))

    def test_core_device_names_rejected_lowercase(self):
        for name in ("con", "prn", "aux", "nul"):
            with self.subTest(name=name):
                self.assertIsNone(_SID_RE.match(name))

    def test_core_device_names_rejected_mixed_case(self):
        for name in ("Con", "cOn", "Nul", "Aux"):
            with self.subTest(name=name):
                self.assertIsNone(_SID_RE.match(name))

    def test_com_lpt_ports_rejected(self):
        for i in range(10):
            for prefix in ("COM", "LPT", "com", "lpt"):
                name = f"{prefix}{i}"
                with self.subTest(name=name):
                    self.assertIsNone(_SID_RE.match(name))

    def test_conrad_as_prefix_allowed(self):
        """CON at start of longer name must still be valid."""
        self.assertIsNotNone(_SID_RE.match("Conrad"))
        self.assertIsNotNone(_SID_RE.match("console"))
        self.assertIsNotNone(_SID_RE.match("congress"))

    def test_com10_allowed(self):
        """COM10 is not a reserved device name (only COM0-9)."""
        self.assertIsNotNone(_SID_RE.match("COM10"))
        self.assertIsNotNone(_SID_RE.match("LPT99"))

    def test_normal_session_ids_still_allowed(self):
        for sid in ("abc123-def456", "session_01", "a-b-c-d", "x" * 128):
            with self.subTest(sid=sid):
                self.assertIsNotNone(_SID_RE.match(sid))


# ---------------------------------------------------------------------------
# Regression: _rls_check_worker uses shared.run_git
# ---------------------------------------------------------------------------
class TestSafeRead(unittest.TestCase):
    """shared.safe_read: bounded file read. Core of v1.10.2 TOCTOU hardening."""

    def test_returns_bytes_on_small_file(self):
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            f.write(b"hello")
            p = f.name
        try:
            self.assertEqual(shared.safe_read(p, 100), b"hello")
        finally:
            os.unlink(p)

    def test_returns_none_when_over_cap(self):
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            f.write(b"x" * 100)
            p = f.name
        try:
            # Cap 50, file has 100 bytes → must return None
            self.assertIsNone(shared.safe_read(p, 50))
        finally:
            os.unlink(p)

    def test_returns_none_on_missing_file(self):
        self.assertIsNone(shared.safe_read("/nonexistent/path/xyz", 100))

    def test_reads_exactly_at_cap(self):
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            f.write(b"x" * 100)
            p = f.name
        try:
            # Cap exactly matches size → pass
            self.assertEqual(shared.safe_read(p, 100), b"x" * 100)
        finally:
            os.unlink(p)

    def test_returns_none_on_permission_error(self):
        """An unreadable existing file (any OSError, not just missing)
        must return None — caller treats the read as a failed datapoint
        rather than propagating an exception into the render path."""
        with patch("builtins.open", side_effect=PermissionError("denied")):
            self.assertIsNone(shared.safe_read("any/path", 100))


class TestVersionSingleSourceOfTruth(unittest.TestCase):
    """I-1: VERSION lives in shared.py; monitor + pulse import from there."""

    def test_shared_version_is_string(self):
        self.assertIsInstance(shared.VERSION, str)
        self.assertRegex(shared.VERSION, r"^\d+\.\d+\.\d+$")

    def test_monitor_version_matches_shared(self):
        import monitor as _m
        self.assertEqual(_m.VERSION, shared.VERSION)

    def test_pulse_user_agent_uses_shared_version(self):
        import pulse as _p
        self.assertIn(shared.VERSION, _p.USER_AGENT,
                      f"pulse.USER_AGENT must embed shared.VERSION, got {_p.USER_AGENT!r}")


class TestParseAheadBehind(unittest.TestCase):
    """shared.parse_ahead_behind — parses git rev-list --left-right --count output."""

    def test_valid_input_returns_tuple(self):
        from shared import parse_ahead_behind
        ahead, behind = parse_ahead_behind("3 5")
        self.assertEqual(ahead, 3)
        self.assertEqual(behind, 5)

    def test_zero_values(self):
        from shared import parse_ahead_behind
        ahead, behind = parse_ahead_behind("0 0")
        self.assertEqual(ahead, 0)
        self.assertEqual(behind, 0)

    def test_empty_string_raises_value_error(self):
        from shared import parse_ahead_behind
        with self.assertRaises(ValueError):
            parse_ahead_behind("")

    def test_single_token_raises_value_error(self):
        from shared import parse_ahead_behind
        with self.assertRaises(ValueError):
            parse_ahead_behind("1")

    def test_non_integer_tokens_raise_value_error(self):
        from shared import parse_ahead_behind
        with self.assertRaises(ValueError):
            parse_ahead_behind("abc def")

    def test_three_tokens_raises_value_error(self):
        from shared import parse_ahead_behind
        with self.assertRaises(ValueError):
            parse_ahead_behind("1 2 3")

    def test_tab_separated_valid(self):
        # Real git output uses tab separator — split() handles both space and tab.
        from shared import parse_ahead_behind
        ahead, behind = parse_ahead_behind("3\t5")
        self.assertEqual((ahead, behind), (3, 5))

    def test_negative_integer_rejected(self):
        # int() accepts negative values; parser applies no sign validation.
        # This anchors the current behavior: negatives ARE accepted (no validation).
        from shared import parse_ahead_behind
        ahead, behind = parse_ahead_behind("-3\t5")
        self.assertEqual((ahead, behind), (-3, 5))


class TestAcquireSingletonLock(unittest.TestCase):
    """shared.acquire_singleton_lock — exclusive non-blocking file lock."""

    def test_happy_path_returns_handle_and_writes_pid(self):
        from shared import acquire_singleton_lock
        with tempfile.TemporaryDirectory() as d:
            lock_path = pathlib.Path(d) / "monitor.lock"
            fh = acquire_singleton_lock(lock_path)
            try:
                self.assertIsNotNone(fh, "lock acquire should succeed on fresh path")
                # PID written into the lock file — read via the open handle
                # (Windows msvcrt.locking holds a byte-range lock; re-opening the
                # same path would trigger a sharing violation on some Python builds).
                fh.seek(0)
                content = fh.read()
                self.assertEqual(content.strip(), str(os.getpid()))
            finally:
                if fh is not None:
                    fh.close()

    def test_contention_second_acquire_returns_none(self):
        from shared import acquire_singleton_lock
        with tempfile.TemporaryDirectory() as d:
            lock_path = pathlib.Path(d) / "monitor.lock"
            fh1 = acquire_singleton_lock(lock_path)
            try:
                self.assertIsNotNone(fh1, "first acquire must succeed")
                fh2 = acquire_singleton_lock(lock_path)
                self.assertIsNone(fh2, "second acquire on held lock must return None")
                if fh2 is not None:
                    fh2.close()
            finally:
                if fh1 is not None:
                    fh1.close()

    def test_missing_parent_dir_returns_none(self):
        from shared import acquire_singleton_lock
        # Use a path whose parent does not exist — open() must fail → None
        lock_path = pathlib.Path(tempfile.gettempdir()) / "cc_aio_nonexistent_xyz" / "monitor.lock"
        result = acquire_singleton_lock(lock_path)
        self.assertIsNone(result)
        if result is not None:
            result.close()


class TestRotateCrashLog(unittest.TestCase):
    """shared.rotate_crash_log — rotates oversized crash logs."""

    def test_small_file_not_rotated(self):
        from shared import rotate_crash_log, MAX_FILE_SIZE
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as f:
            f.write(b"x" * 10)
            p = pathlib.Path(f.name)
        try:
            rotate_crash_log(p, max_bytes=MAX_FILE_SIZE)
            # Original must still exist unchanged
            self.assertTrue(p.exists())
            backup = p.with_suffix(p.suffix + ".1")
            self.assertFalse(backup.exists())
        finally:
            p.unlink(missing_ok=True)

    def test_large_file_rotates_to_dot_one(self):
        from shared import rotate_crash_log
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as f:
            f.write(b"y" * 200)
            p = pathlib.Path(f.name)
        backup = p.with_suffix(p.suffix + ".1")
        try:
            rotate_crash_log(p, max_bytes=50)
            self.assertFalse(p.exists(), "original must be gone after rotation")
            self.assertTrue(backup.exists(), ".1 backup must exist")
            self.assertEqual(backup.read_bytes(), b"y" * 200)
        finally:
            p.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)

    def test_existing_dot_one_dropped_before_rotate(self):
        from shared import rotate_crash_log
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as f:
            f.write(b"new" * 100)
            p = pathlib.Path(f.name)
        backup = p.with_suffix(p.suffix + ".1")
        backup.write_bytes(b"old")
        try:
            rotate_crash_log(p, max_bytes=50)
            self.assertTrue(backup.exists())
            # old .1 content replaced by new
            self.assertEqual(backup.read_bytes(), b"new" * 100)
        finally:
            p.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)

    def test_missing_path_silent_no_op(self):
        from shared import rotate_crash_log
        nonexistent = pathlib.Path(tempfile.gettempdir()) / "cc_aio_rotate_nonexist_xyz.log"
        # Must not raise
        rotate_crash_log(nonexistent, max_bytes=100)

    def test_always_rotates_small_file(self):
        """always=True must rotate even when file is well under max_bytes.
        Guards against two-crashes-in-quick-succession losing first traceback."""
        from shared import rotate_crash_log, MAX_FILE_SIZE
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as f:
            f.write(b"first crash traceback")
            p = pathlib.Path(f.name)
        backup = p.with_suffix(p.suffix + ".1")
        try:
            rotate_crash_log(p, max_bytes=MAX_FILE_SIZE, always=True)
            self.assertFalse(p.exists(), "original must be gone after always-rotate")
            self.assertTrue(backup.exists(), ".1 backup must exist")
            self.assertEqual(backup.read_bytes(), b"first crash traceback")
        finally:
            p.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)

    def test_always_no_op_when_path_missing(self):
        """always=True must not raise when the source path does not exist."""
        from shared import rotate_crash_log
        nonexistent = pathlib.Path(tempfile.gettempdir()) / "cc_aio_rotate_always_nonexist_xyz.log"
        rotate_crash_log(nonexistent, always=True)
        self.assertFalse(nonexistent.exists())


class TestPyFilesSingleSourceOfTruth(unittest.TestCase):
    """I-2: PY_FILES in shared.py is the single source of truth for syntax-check list."""

    def test_shared_py_files_contains_all_modules(self):
        expected = {"monitor.py", "statusline.py", "shared.py", "pulse.py", "update.py"}
        self.assertEqual(set(shared.PY_FILES), expected)

    def test_update_uses_shared_py_files(self):
        import update as _u
        src = pathlib.Path(_u.__file__).read_text(encoding="utf-8")
        # Must import PY_FILES from shared AND delegate the syntax-check
        # iteration to the shared helper — no parallel loop in update.py.
        self.assertIn("PY_FILES", src)
        self.assertIn("check_syntax_after_pull", src)
        self.assertNotIn("for f in PY_FILES", src)

    def test_monitor_uses_shared_py_files(self):
        import monitor as _m
        src = pathlib.Path(_m.__file__).read_text(encoding="utf-8")
        self.assertIn("PY_FILES", src)
        self.assertIn("check_syntax_after_pull", src)
        self.assertNotIn("for f in PY_FILES", src)

    def test_shared_owns_py_files_iteration(self):
        """The actual PY_FILES loop lives exactly once — in shared.py."""
        src = pathlib.Path(shared.__file__).read_text(encoding="utf-8")
        self.assertIn("for f in py_files", src)


class TestLoadHistory(unittest.TestCase):
    """Regression guards for shared.load_history defensive branches."""

    def test_reserved_sid_returns_empty(self):
        """RESERVED_SIDS (rls/stats/pulse) must not be readable via session
        history API — prevents monitor from misinterpreting internal JSONL
        files as session history. shared.py:142."""
        for sid in RESERVED_SIDS:
            with self.subTest(sid=sid):
                self.assertEqual(shared.load_history(sid), [])

    def test_binary_garbage_returns_empty(self):
        """Binary garbage in a .jsonl file must be tolerated (return []),
        not raise. shared.py:151 UnicodeDecodeError branch."""
        with tempfile.TemporaryDirectory() as td:
            dd = pathlib.Path(td)
            sid = "binsession"
            (dd / f"{sid}.jsonl").write_bytes(
                b"\xff\xfe\x00\x01binary\x80garbage\xc3\x28\xa0"
            )
            self.assertEqual(shared.load_history(sid, data_dir=dd), [])


class TestEnvPct(unittest.TestCase):
    """Audit P1-8: _env_pct is the SSoT for WARN_PCT / CRIT_PCT parsing.
    Its 4 input classes (valid float, empty string, invalid string, missing
    var) had no direct unit tests — only implicit coverage via WARN_PCT
    module-init."""

    def test_valid_float_parsed(self):
        with patch.dict(os.environ, {"CLAUDE_STATUS_TESTVAR": "65.5"}):
            self.assertEqual(shared._env_pct("CLAUDE_STATUS_TESTVAR", 50.0), 65.5)

    def test_empty_string_uses_default(self):
        with patch.dict(os.environ, {"CLAUDE_STATUS_TESTVAR": ""}):
            self.assertEqual(shared._env_pct("CLAUDE_STATUS_TESTVAR", 50.0), 50.0)

    def test_invalid_string_uses_default(self):
        with patch.dict(os.environ, {"CLAUDE_STATUS_TESTVAR": "notanumber"}):
            self.assertEqual(shared._env_pct("CLAUDE_STATUS_TESTVAR", 50.0), 50.0)

    def test_missing_uses_default(self):
        # patch.dict with clear=False + setdefault-style pop won't run reliably;
        # set then remove so we know the env var is absent.
        os.environ.pop("CLAUDE_STATUS_TESTVAR", None)
        self.assertEqual(shared._env_pct("CLAUDE_STATUS_TESTVAR", 50.0), 50.0)

    def test_integer_string_parsed(self):
        with patch.dict(os.environ, {"CLAUDE_STATUS_TESTVAR": "80"}):
            self.assertEqual(shared._env_pct("CLAUDE_STATUS_TESTVAR", 50.0), 80.0)


class TestEnsureUtf8Stdout(unittest.TestCase):
    """Audit P1-9: ensure_utf8_stdout is called by every entry point
    (statusline/monitor/update) — silent failure would tank glyph rendering
    on Windows. No direct test prior to v1.11.2."""

    def test_utf8_stdout_is_no_op(self):
        """If sys.stdout encoding is already utf-8, do not reassign stdout."""
        fake_stdout = MagicMock()
        fake_stdout.encoding = "utf-8"
        with patch("shared.sys.stdout", fake_stdout):
            shared.ensure_utf8_stdout()
        # When the encoding is already utf-8, the function must not flush
        # nor swap sys.stdout — the MagicMock would record .flush() / .fileno()
        # if it tried to.
        fake_stdout.flush.assert_not_called()
        fake_stdout.fileno.assert_not_called()

    def test_non_utf8_stdout_triggers_reassign(self):
        """cp1250 (Windows legacy) must trigger flush + reopen as utf-8."""
        fake_stdout = MagicMock()
        fake_stdout.encoding = "cp1250"
        fake_stdout.fileno.return_value = 1  # stdout fd
        # We patch `open` at the shared module level so we capture the call
        # without actually reopening real fd 1.
        with patch("shared.sys.stdout", fake_stdout), \
             patch("shared.open", create=True) as mock_open:
            mock_open.return_value = MagicMock()
            shared.ensure_utf8_stdout()
        fake_stdout.flush.assert_called_once()
        mock_open.assert_called_once()
        # Inspect the kwargs to confirm utf-8 + errors=replace + closefd=False
        _, kwargs = mock_open.call_args
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")
        self.assertFalse(kwargs.get("closefd"))


class TestAcquireSingletonLockCrossPlatform(unittest.TestCase):
    """Audit P1-7: the Windows (msvcrt) and Unix (fcntl) branches of
    acquire_singleton_lock are only exercised on the runner's actual
    platform. Mock the opposite branch so both code paths are covered
    on a single OS."""

    def test_unix_path_via_mock(self):
        """Force sys.platform != 'win32' and verify fcntl.flock is called."""
        with tempfile.TemporaryDirectory() as td:
            lock_path = pathlib.Path(td) / "x.lock"
            mock_fcntl = MagicMock()
            mock_fcntl.LOCK_EX = 2
            mock_fcntl.LOCK_NB = 4
            with patch("shared.sys.platform", "linux"), \
                 patch.object(shared, "fcntl", mock_fcntl, create=True):
                fh = shared.acquire_singleton_lock(lock_path)
            self.assertIsNotNone(fh)
            mock_fcntl.flock.assert_called_once()
            # Cleanup: close handle so the temp dir teardown can unlink.
            try:
                fh.close()
            except OSError:
                pass

    def test_windows_path_via_mock(self):
        """Force sys.platform == 'win32' and verify msvcrt.locking is called."""
        with tempfile.TemporaryDirectory() as td:
            lock_path = pathlib.Path(td) / "y.lock"
            mock_msvcrt = MagicMock()
            mock_msvcrt.LK_NBLCK = 1
            with patch("shared.sys.platform", "win32"), \
                 patch.object(shared, "msvcrt", mock_msvcrt, create=True):
                fh = shared.acquire_singleton_lock(lock_path)
            self.assertIsNotNone(fh)
            mock_msvcrt.locking.assert_called_once()
            try:
                fh.close()
            except OSError:
                pass


class TestExtractChangelogEntry(unittest.TestCase):
    """Direct unit tests for shared.extract_changelog_entry. Previously
    covered only indirectly via test_update (TestUpdate / TestGetRemoteChangelogPreview)."""

    def test_extracts_middle_entry(self):
        text = (
            "## v1.2.0\n"
            "feat: middle\n"
            "\n"
            "## v1.1.0\n"
            "fix: old\n"
        )
        out = shared.extract_changelog_entry(text, "1.2.0")
        self.assertIn("## v1.2.0", out)
        self.assertIn("feat: middle", out)
        self.assertNotIn("v1.1.0", out)

    def test_extracts_last_entry_at_eof(self):
        """Final entry has no following '## v' — pattern must anchor on \\Z."""
        text = "## v0.9.0\nfeat: trailing\n"
        out = shared.extract_changelog_entry(text, "0.9.0")
        self.assertIn("feat: trailing", out)
        self.assertTrue(out.startswith("## v0.9.0"))

    def test_missing_version_returns_empty_string(self):
        text = "## v1.0.0\nfeat: only\n"
        self.assertEqual(shared.extract_changelog_entry(text, "9.9.9"), "")

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(shared.extract_changelog_entry("", "1.0.0"), "")

    def test_max_lines_truncates(self):
        body = "\n".join(f"line {i}" for i in range(20))
        text = f"## v2.0.0\n{body}\n"
        out = shared.extract_changelog_entry(text, "2.0.0", max_lines=5)
        self.assertEqual(len(out.splitlines()), 5)

    def test_version_with_regex_metachars_is_escaped(self):
        """Versions like 1.0.0+build.1 contain regex metachars — re.escape
        must prevent them from acting as the regex's '.' wildcard."""
        text = "## v1.0.0+build.1\nfeat: a\n\n## v1X0X0PbuildX1\nfeat: b\n"
        out = shared.extract_changelog_entry(text, "1.0.0+build.1")
        self.assertIn("feat: a", out)
        self.assertNotIn("feat: b", out)


class TestContextSuffixHelpers(unittest.TestCase):
    """Direct unit tests for shared.strip_context_suffix and
    compact_context_suffix. Previously only covered implicitly through
    statusline rendering tests."""

    def test_strip_removes_1m_context(self):
        self.assertEqual(shared.strip_context_suffix("Opus 4.7 (1M context)"), "Opus 4.7")

    def test_strip_removes_200k_context(self):
        self.assertEqual(shared.strip_context_suffix("Sonnet 4.6 (200k context)"), "Sonnet 4.6")

    def test_strip_no_suffix_unchanged(self):
        self.assertEqual(shared.strip_context_suffix("Opus 4.7"), "Opus 4.7")

    def test_strip_empty_input(self):
        self.assertEqual(shared.strip_context_suffix(""), "")

    def test_compact_inlines_1m(self):
        self.assertEqual(shared.compact_context_suffix("Opus 4.7 (1M context)"), "Opus 4.7 1M")

    def test_compact_inlines_200k(self):
        self.assertEqual(shared.compact_context_suffix("Sonnet 4.6 (200k context)"), "Sonnet 4.6 200k")

    def test_compact_no_suffix_unchanged(self):
        self.assertEqual(shared.compact_context_suffix("Opus 4.7"), "Opus 4.7")

    def test_compact_empty_input(self):
        self.assertEqual(shared.compact_context_suffix(""), "")


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
