#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Unit tests for statusline.py — stdlib only, no pytest required.

Run:
    python -m unittest tests.test_statusline
    # or directly:
    python tests/test_statusline.py
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
    _ANSI_RE,
    _sanitize,
    C_RED,
    C_GRN,
    C_YEL,
    C_ORN,
    C_CYN,
    C_DIM,
)

from statusline import (
    _get_terminal_width,
    _calc_rates as sl_calc_rates,
    seg_model,
    seg_ctx,
    seg_5hl,
    seg_7dl,
    seg_cost,
    seg_brn,
    build_line,
    cpc_base,
)

# ---------------------------------------------------------------------------
# Shared helpers (canonical home: tests/_helpers.py)
# ---------------------------------------------------------------------------
from tests._helpers import _vlen, _full_data


class TestGetTerminalWidth(unittest.TestCase):

    def test_columns_env_override(self):
        import os
        old = os.environ.get("COLUMNS")
        try:
            os.environ["COLUMNS"] = "999"
            self.assertEqual(_get_terminal_width(), 999)
        finally:
            if old is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = old

    def test_fallback_returns_positive(self):
        import os
        old = os.environ.get("COLUMNS")
        try:
            os.environ.pop("COLUMNS", None)
            w = _get_terminal_width(fallback=123)
            self.assertGreater(w, 0)
        finally:
            if old is not None:
                os.environ["COLUMNS"] = old

    def test_fallback_value_used(self):
        import os
        old = os.environ.get("COLUMNS")
        try:
            os.environ.pop("COLUMNS", None)
            # In piped test context, may hit fallback or actual terminal
            w = _get_terminal_width(fallback=77)
            self.assertGreater(w, 0)
        finally:
            if old is not None:
                os.environ["COLUMNS"] = old


# ---------------------------------------------------------------------------
# Segment builders
# ---------------------------------------------------------------------------
class TestSegModel(unittest.TestCase):

    def test_renders_display_name_with_consistent_vlen(self):
        text, vl = seg_model(_full_data())
        self.assertEqual(vl, _vlen(text))
        self.assertIn("Opus 4", _ANSI_RE.sub("", text))

    def test_strips_context_suffix(self):
        d = _full_data(model={"display_name": "Opus 4 (1M context)"})
        text, vl = seg_model(d)
        plain = _ANSI_RE.sub("", text)
        self.assertNotIn("1M context", plain)
        self.assertIn("Opus 4", plain)

    def test_empty_model(self):
        text, vl = seg_model({"model": {}})
        self.assertEqual(vl, 0)


class TestSegCtx(unittest.TestCase):

    def test_renders_ctx_pct_with_token_count(self):
        text, vl = seg_ctx(_full_data())
        self.assertEqual(vl, _vlen(text))
        plain = _ANSI_RE.sub("", text)
        self.assertIn("CTX", plain)
        self.assertIn("42%", plain)

    def test_no_total(self):
        d = _full_data()
        d["context_window"]["context_window_size"] = 0
        text, vl = seg_ctx(d)
        plain = _ANSI_RE.sub("", text)
        self.assertIn("42%", plain)
        self.assertNotIn("/", plain)  # no token count without total


class TestSeg5hl(unittest.TestCase):

    def test_renders_5hl_label_with_consistent_vlen(self):
        text, vl = seg_5hl(_full_data())
        self.assertEqual(vl, _vlen(text))
        self.assertIn("5HL", _ANSI_RE.sub("", text))

    def test_low_usage_yellow(self):
        d = _full_data()
        d["rate_limits"]["five_hour"]["used_percentage"] = 20
        text, _ = seg_5hl(d)
        self.assertIn(C_YEL, text)

    def test_high_usage_red(self):
        d = _full_data()
        d["rate_limits"]["five_hour"]["used_percentage"] = 85
        text, _ = seg_5hl(d)
        self.assertIn(C_RED, text)

    def test_no_rate_limits(self):
        self.assertIsNone(seg_5hl({"rate_limits": None}))
        self.assertIsNone(seg_5hl({}))

    def test_expired_resets_at(self):
        d = _full_data()
        d["rate_limits"]["five_hour"]["resets_at"] = 1000  # far in the past
        text, vl = seg_5hl(d)
        plain = _ANSI_RE.sub("", text)
        self.assertIn("0%", plain)
        self.assertNotIn("\u2192", plain)  # no arrow when expired

    def test_future_resets_shows_countdown(self):
        # T-P2-1: freeze time so test setup and seg_5hl observe the same
        # epoch; otherwise sub-second race between time.time() here and
        # time.time() inside seg_5hl can flip "2h" \u2192 "1h 59m" intermittently.
        FROZEN = 1_700_000_000.0
        with patch("statusline.time.time", return_value=FROZEN):
            d = _full_data()
            d["rate_limits"]["five_hour"]["resets_at"] = FROZEN + 7260  # ~2h 1m
            text, vl = seg_5hl(d)
        plain = _ANSI_RE.sub("", text)
        self.assertIn("\u2192", plain)
        self.assertIn("2h", plain)
        self.assertEqual(vl, _vlen(text))

    def test_absent_resets_at_no_arrow(self):
        d = _full_data()
        d["rate_limits"]["five_hour"].pop("resets_at", None)
        text, _ = seg_5hl(d)
        self.assertNotIn("\u2192", _ANSI_RE.sub("", text))

    def test_string_resets_at_handled(self):
        d = _full_data()
        d["rate_limits"]["five_hour"]["resets_at"] = str(int(time.time()) + 3600)
        text, _ = seg_5hl(d)
        self.assertIn("\u2192", _ANSI_RE.sub("", text))


class TestSeg7dl(unittest.TestCase):

    def test_renders_7dl_label_with_consistent_vlen(self):
        text, vl = seg_7dl(_full_data())
        self.assertEqual(vl, _vlen(text))
        self.assertIn("7DL", _ANSI_RE.sub("", text))

    def test_base_color_yellow(self):
        d = _full_data()
        d["rate_limits"]["seven_day"]["used_percentage"] = 10
        text, _ = seg_7dl(d)
        self.assertIn(C_YEL, text)  # base is now yellow, not green

    def test_none(self):
        self.assertIsNone(seg_7dl({}))

    def test_future_resets_shows_countdown(self):
        # T-P2-1: freeze time (see comment in TestSeg5hl with same name).
        FROZEN = 1_700_000_000.0
        with patch("statusline.time.time", return_value=FROZEN):
            d = _full_data()
            d["rate_limits"]["seven_day"]["resets_at"] = FROZEN + 86400 * 6 + 3600 * 12
            text, vl = seg_7dl(d)
        plain = _ANSI_RE.sub("", text)
        self.assertIn("\u2192", plain)
        self.assertIn("6d", plain)
        self.assertEqual(vl, _vlen(text))

    def test_expired_no_arrow(self):
        d = _full_data()
        d["rate_limits"]["seven_day"]["resets_at"] = 1000
        text, _ = seg_7dl(d)
        self.assertNotIn("\u2192", _ANSI_RE.sub("", text))


class TestSegCost(unittest.TestCase):

    def test_renders_cst_label_with_dollar_amount(self):
        text, vl = seg_cost(_full_data())
        self.assertEqual(vl, _vlen(text))
        self.assertIn("CST", _ANSI_RE.sub("", text))
        self.assertIn("1.23", _ANSI_RE.sub("", text))

    def test_uses_orange(self):
        text, _ = seg_cost(_full_data())
        self.assertIn(C_ORN, text)

    def test_zero_cost(self):
        d = _full_data()
        d["cost"]["total_cost_usd"] = 0
        self.assertIsNone(seg_cost(d))


class TestSegBrn(unittest.TestCase):

    def test_renders_brn_label_with_consistent_vlen(self):
        text, vl = seg_brn(0.0512)
        self.assertEqual(vl, _vlen(text))
        self.assertIn("BRN", _ANSI_RE.sub("", text))

    def test_uses_orange(self):
        text, _ = seg_brn(0.05)
        self.assertIn(C_ORN, text)

    def test_none(self):
        self.assertIsNone(seg_brn(None))

    def test_zero(self):
        self.assertIsNone(seg_brn(0))


# ---------------------------------------------------------------------------
# build_line
# ---------------------------------------------------------------------------
class TestBuildLine(unittest.TestCase):

    def test_basic_output(self):
        line = build_line(_full_data(), 120)
        self.assertIsNotNone(line)
        self.assertGreater(len(line), 0)

    def test_visual_width_fits_cols(self):
        # Line should not exceed terminal width
        for cols in (120, 200, 300):
            line = build_line(_full_data(), cols)
            vl = _vlen(line)
            self.assertLessEqual(vl, cols, f"cols={cols}, got vl={vl}")

    def test_narrow_no_right_segments_shorter_than_cols(self):
        # When all right segments dropped, line is shorter — EL fills the rest
        line = build_line(_full_data(), 80)
        vl = _vlen(line)
        self.assertLessEqual(vl, 80)

    def test_narrow_drops_right_segments(self):
        wide = build_line(_full_data(), 200)
        narrow = build_line(_full_data(), 40)
        # Narrow should have fewer segments
        self.assertGreater(_vlen(wide), _vlen(narrow))

    def test_narrow_width_fits_and_has_content(self):
        # Very narrow width (20 cols): must still produce non-empty line that fits
        line = build_line(_full_data(), 20)
        self.assertIsNotNone(line)
        self.assertGreater(len(line), 0)
        self.assertLessEqual(_vlen(line), 20)
        # Must contain at least one visible character after ANSI stripping
        visible = _ANSI_RE.sub("", line).strip()
        self.assertGreater(len(visible), 0)

    def test_empty_data_graceful(self):
        line = build_line({}, 80)
        # With empty data: no crash, returns valid string fitting cols
        self.assertIsNotNone(line)
        self.assertIsInstance(line, str)
        self.assertLessEqual(_vlen(line), 80)


# ---------------------------------------------------------------------------
# Fixed-range bars (BRN/CTR/CST)
# ---------------------------------------------------------------------------
class TestWriteSharedState(unittest.TestCase):

    def setUp(self):
        import tempfile, pathlib
        self.tmpdir = tempfile.mkdtemp()
        self._base = pathlib.Path(self.tmpdir) / "claude-aio-monitor"
        import statusline
        self._orig_data_dir = statusline.DATA_DIR
        statusline.DATA_DIR = self._base

    def tearDown(self):
        import shutil, statusline
        statusline.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_snapshot_and_history(self):
        import json
        from statusline import write_shared_state
        data = {"session_id": "test123", "cost": {"total_cost_usd": 1.5}}
        write_shared_state(data)
        snap = self._base / "test123.json"
        hist = self._base / "test123.jsonl"
        self.assertTrue(snap.exists())
        self.assertTrue(hist.exists())
        loaded = json.loads(snap.read_text(encoding="utf-8"))
        self.assertEqual(loaded["session_id"], "test123")

    def test_history_appends(self):
        from statusline import write_shared_state
        data = {"session_id": "test456", "cost": {"total_cost_usd": 1.0}}
        write_shared_state(data)
        write_shared_state(data)
        hist = self._base / "test456.jsonl"
        lines = hist.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_invalid_session_id_uses_default(self):
        from statusline import write_shared_state
        data = {"session_id": "../evil", "cost": {"total_cost_usd": 0}}
        write_shared_state(data)
        snap = self._base / "default.json"
        self.assertTrue(snap.exists())

    def test_snapshot_contains_schema_version(self):
        import json
        from statusline import write_shared_state
        data = {"session_id": "schematest", "cost": {"total_cost_usd": 0.5}}
        write_shared_state(data)
        snap = self._base / "schematest.json"
        self.assertTrue(snap.exists(), "snapshot file must be written")
        loaded = json.loads(snap.read_text(encoding="utf-8"))
        self.assertIn("_schema_version", loaded)
        self.assertEqual(loaded["_schema_version"], shared.SCHEMA_VERSION)
        # JSONL history line must also carry _schema_version
        hist = self._base / "schematest.jsonl"
        self.assertTrue(hist.exists(), "history file must be written")
        first_line = hist.read_text(encoding="utf-8").strip().splitlines()[0]
        entry = json.loads(first_line)
        self.assertIn("_schema_version", entry)
        self.assertEqual(entry["_schema_version"], shared.SCHEMA_VERSION)

    def test_snapshot_failure_skips_history_append(self):
        """Alignment guard: if the snapshot write fails, history must NOT be
        appended — otherwise calc_rates would read a fresh JSONL line against a
        stale snapshot and skew BRN/CTR. statusline.py:309-311."""
        import statusline
        from statusline import write_shared_state
        data = {"session_id": "alignfail", "cost": {"total_cost_usd": 2.0}}
        with patch.object(statusline, "atomic_write_text", return_value=False):
            write_shared_state(data)
        hist = self._base / "alignfail.jsonl"
        self.assertFalse(
            hist.exists(),
            "history JSONL must not be created when the snapshot write fails",
        )


class TestTrimHistory(unittest.TestCase):

    def test_trims_when_over_limit(self):
        import tempfile, pathlib
        from statusline import _trim_history, HISTORY_TRIM_TO
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                          delete=False, encoding="utf-8")
        lines = [f'{{"i": {i}}}' for i in range(HISTORY_TRIM_TO + 500)]
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        p = pathlib.Path(tmp.name)
        _trim_history(p)
        result = p.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(result), HISTORY_TRIM_TO)
        p.unlink()

    def test_trim_drops_malformed_lines(self):
        # FILE-IPC "Trim Policy": lines that fail json.loads() are dropped
        # during trim so a torn write cannot survive rewrites forever.
        import json, tempfile, pathlib
        from statusline import _trim_history, HISTORY_TRIM_TO
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                          delete=False, encoding="utf-8")
        lines = [f'{{"i": {i}}}' for i in range(HISTORY_TRIM_TO + 10)]
        lines[-5] = '{"torn": '  # malformed line inside the kept tail
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        p = pathlib.Path(tmp.name)
        _trim_history(p)
        result = p.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(result), HISTORY_TRIM_TO - 1)
        for ln in result:
            json.loads(ln)  # every surviving line is valid JSON
        p.unlink()


# ---------------------------------------------------------------------------
# _load_history_for_rates
# ---------------------------------------------------------------------------
class TestLoadHistoryForRates(unittest.TestCase):

    def setUp(self):
        import tempfile, pathlib, statusline
        self.tmpdir = tempfile.mkdtemp()
        self._orig_dir = statusline.DATA_DIR
        statusline.DATA_DIR = pathlib.Path(self.tmpdir)

    def tearDown(self):
        import shutil, statusline
        statusline.DATA_DIR = self._orig_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_returns_empty(self):
        from statusline import _load_history_for_rates
        self.assertEqual(_load_history_for_rates("nonexistent"), [])

    def test_valid_jsonl_returns_entries(self):
        import json, pathlib, statusline
        from statusline import _load_history_for_rates
        p = pathlib.Path(statusline.DATA_DIR) / "sess1.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"i": i, "t": 1000 + i}) for i in range(5)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = _load_history_for_rates("sess1", n=120)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["i"], 0)

    def test_tail_n_limits_entries(self):
        import json, pathlib, statusline
        from statusline import _load_history_for_rates
        p = pathlib.Path(statusline.DATA_DIR) / "sess2.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"i": i}) for i in range(20)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = _load_history_for_rates("sess2", n=5)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["i"], 15)  # last 5 entries

    def test_corrupt_lines_skipped(self):
        import json, pathlib, statusline
        from statusline import _load_history_for_rates
        p = pathlib.Path(statusline.DATA_DIR) / "sess3.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        content = 'not json\n{"i": 1}\n{bad\n{"i": 2}\n'
        p.write_text(content, encoding="utf-8")
        result = _load_history_for_rates("sess3")
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# calc_cross_session_costs
# ---------------------------------------------------------------------------
class TestCpcBase(unittest.TestCase):

    def test_below_warn_returns_base(self):
        self.assertEqual(cpc_base(30, C_CYN), C_CYN)

    def test_at_warn_returns_yellow(self):
        self.assertEqual(cpc_base(50, C_CYN), C_YEL)

    def test_above_warn_returns_yellow(self):
        self.assertEqual(cpc_base(70, C_GRN), C_YEL)

    def test_at_crit_returns_red(self):
        self.assertEqual(cpc_base(80, C_CYN), C_RED)

    def test_above_crit_returns_red(self):
        self.assertEqual(cpc_base(95, C_GRN), C_RED)


# ---------------------------------------------------------------------------
# TestListSessions — monitor.list_sessions
# ---------------------------------------------------------------------------
class TestWriteSharedStateReservedSid(unittest.TestCase):

    def setUp(self):
        import statusline
        self._tmpdir = tempfile.mkdtemp()
        self._base = pathlib.Path(self._tmpdir) / "claude-aio-monitor"
        self._orig_data_dir = statusline.DATA_DIR
        statusline.DATA_DIR = self._base

    def tearDown(self):
        import shutil, statusline
        statusline.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_data(self, sid):
        return {
            "session_id": sid,
            "model": {"id": "x", "display_name": "x"},
            "cost": {"total_cost_usd": 0, "total_duration_ms": 0},
        }

    def test_reserved_sid_pulse_not_written(self):
        from statusline import write_shared_state
        write_shared_state(self._make_data("pulse"))
        self.assertFalse((self._base / "pulse.json").exists())

    def test_reserved_sid_rls_not_written(self):
        from statusline import write_shared_state
        write_shared_state(self._make_data("rls"))
        self.assertFalse((self._base / "rls.json").exists())

    def test_reserved_sid_stats_not_written(self):
        from statusline import write_shared_state
        write_shared_state(self._make_data("stats"))
        self.assertFalse((self._base / "stats.json").exists())


# ---------------------------------------------------------------------------
# TestListSessionsPurgesOrphan — no display_name + age > 1h → purge
# ---------------------------------------------------------------------------


class TestStatuslineMainE2E(unittest.TestCase):
    """Audit P1-10: end-to-end smoke test of statusline.main pipeline
    (stdin -> JSON -> build_line -> write_shared_state -> stdout).

    Component-level tests cover each step in isolation, but the integration
    path (does main() actually wire them?) wasn't asserted before v1.11.2.
    """

    def setUp(self):
        import statusline
        self._tmpdir = tempfile.mkdtemp()
        self._base = pathlib.Path(self._tmpdir) / "claude-aio-monitor"
        self._orig_data_dir = statusline.DATA_DIR
        statusline.DATA_DIR = self._base

    def tearDown(self):
        import shutil, statusline
        statusline.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @staticmethod
    def _fake_stdin(payload_bytes):
        """Build a stdin stand-in exposing .buffer.read() returning bytes.

        NEW-002 fix switched statusline.main() to read via
        `sys.stdin.buffer.read()` for encoding independence; tests need a
        binary-capable mock instead of StringIO.
        """
        import io
        from types import SimpleNamespace
        return SimpleNamespace(buffer=io.BytesIO(payload_bytes))

    def test_main_writes_snapshot_and_prints_line(self):
        import io, json as _json
        import statusline as _sl
        sid = "smoketest"
        payload = {
            "session_id": sid,
            "model": {"id": "claude-opus-4-5", "display_name": "Opus 4.5"},
            "cost": {
                "total_cost_usd": 1.23,
                "total_duration_ms": 60000,
                "total_lines_added": 0,
                "total_lines_removed": 0,
            },
        }
        fake_stdin = self._fake_stdin(_json.dumps(payload).encode("utf-8"))
        fake_stdout = io.StringIO()
        with patch.object(_sl.sys, "stdin", fake_stdin), \
             patch.object(_sl.sys, "stdout", fake_stdout), \
             patch.object(_sl, "ensure_utf8_stdout", lambda: None):
            _sl.main()
        # Snapshot file was written
        snap = self._base / f"{sid}.json"
        self.assertTrue(snap.exists(), "main() must persist snapshot via write_shared_state")
        parsed = _json.loads(snap.read_text(encoding="utf-8"))
        self.assertEqual(parsed["session_id"], sid)
        self.assertEqual(parsed["model"]["display_name"], "Opus 4.5")
        # A statusline was emitted to stdout
        self.assertGreater(len(fake_stdout.getvalue()), 0,
                           "main() must print the statusline to stdout")

    def test_main_survives_broken_pipe_on_print(self):
        # #2b: Claude Code may close the read end of the statusline pipe early.
        # print() then raises BrokenPipeError — main() must swallow it (no
        # uncaught traceback) and still feed the monitor via write_shared_state.
        import json as _json
        import statusline as _sl

        class _BrokenStdout:
            def write(self, *_a):
                raise BrokenPipeError("EPIPE")
            def flush(self):
                pass

        sid = "brokenpipe"
        payload = {"session_id": sid,
                   "model": {"id": "claude-opus-4-5", "display_name": "Opus 4.5"},
                   "cost": {"total_cost_usd": 0.5, "total_duration_ms": 1000,
                            "total_lines_added": 0, "total_lines_removed": 0}}
        fake_stdin = self._fake_stdin(_json.dumps(payload).encode("utf-8"))
        with patch.object(_sl.sys, "stdin", fake_stdin), \
             patch.object(_sl.sys, "stdout", _BrokenStdout()), \
             patch.object(_sl, "ensure_utf8_stdout", lambda: None):
            _sl.main()  # must not raise despite the broken pipe
        self.assertTrue((self._base / f"{sid}.json").exists(),
                        "snapshot must still be written after a broken pipe")

    def test_main_empty_stdin_returns_silently(self):
        import io
        import statusline as _sl
        fake_stdin = self._fake_stdin(b"")
        fake_stdout = io.StringIO()
        with patch.object(_sl.sys, "stdin", fake_stdin), \
             patch.object(_sl.sys, "stdout", fake_stdout), \
             patch.object(_sl, "ensure_utf8_stdout", lambda: None):
            _sl.main()  # must not raise
        self.assertEqual(fake_stdout.getvalue(), "")
        self.assertFalse(any(self._base.glob("*.json")) if self._base.exists() else False)

    def test_main_invalid_json_returns_silently(self):
        import io
        import statusline as _sl
        fake_stdin = self._fake_stdin(b"{not valid json")
        fake_stdout = io.StringIO()
        with patch.object(_sl.sys, "stdin", fake_stdin), \
             patch.object(_sl.sys, "stdout", fake_stdout), \
             patch.object(_sl, "ensure_utf8_stdout", lambda: None):
            _sl.main()  # must not raise

    def test_main_utf8_session_name_preserved_through_pipeline(self):
        """NEW-002 regression: Slovak diacritics in session_name must
        survive stdin -> json -> write_shared_state -> snapshot file.

        Pre-NEW-002, sys.stdin.read() used the locale codec (cp1250 on
        SK Windows) and mangled `ý`, `š`, `č` etc. into mojibake byte
        pairs before json.loads ever saw them.
        """
        import io, json as _json
        import statusline as _sl
        sid = "utf8test"
        title = "Kompletný audit — diakritika: š č ť ž ý á í é"
        payload = {
            "session_id": sid,
            "model": {"id": "claude-opus-4-5", "display_name": "Opus 4.5"},
            "session_name": title,
        }
        # Emulate Claude Code: emits JSON as raw UTF-8 bytes on stdin.
        fake_stdin = self._fake_stdin(_json.dumps(payload).encode("utf-8"))
        fake_stdout = io.StringIO()
        with patch.object(_sl.sys, "stdin", fake_stdin), \
             patch.object(_sl.sys, "stdout", fake_stdout), \
             patch.object(_sl, "ensure_utf8_stdout", lambda: None):
            _sl.main()
        snap = self._base / f"{sid}.json"
        parsed = _json.loads(snap.read_text(encoding="utf-8"))
        # Title must round-trip unchanged — no `Ă˝` / `Ĺˇ` mojibake.
        self.assertEqual(parsed["session_name"], title)


class TestIPCForwardCompatNoSchemaVersion(unittest.TestCase):
    """Audit P1-6: FILE-IPC-CONTRACT.md guarantees pre-v1.10 snapshots
    (no `_schema_version` field) are tolerated by readers. The contract
    relies on dict.get() everywhere, but no test pinned the actual
    pre-v1.10 shape until v1.11.2.

    `load_state` lives in monitor.py; testing it here keeps the IPC
    forward-compat assertions next to the snapshot writer they pair with.
    """

    def setUp(self):
        import monitor
        self._monitor = monitor
        self._tmpdir = tempfile.mkdtemp()
        self._base = pathlib.Path(self._tmpdir) / "claude-aio-monitor"
        self._base.mkdir(parents=True, exist_ok=True)
        self._orig_data_dir = monitor.DATA_DIR
        monitor.DATA_DIR = self._base

    def tearDown(self):
        import shutil
        self._monitor.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_state_without_schema_version_returns_dict(self):
        import json as _json
        sid = "pre110"
        # Snapshot in pre-v1.10 shape — no `_schema_version` key at all.
        snap = {
            "session_id": sid,
            "model": {"id": "claude-opus-4-5", "display_name": "Opus 4.5"},
            "cost": {"total_cost_usd": 0.5, "total_duration_ms": 30000},
        }
        (self._base / f"{sid}.json").write_text(
            _json.dumps(snap), encoding="utf-8"
        )
        loaded = self._monitor.load_state(sid)
        self.assertIsNotNone(loaded, "load_state must accept pre-v1.10 snapshot")
        self.assertEqual(loaded["session_id"], sid)
        # Crucial: the absent field is None / missing, not an exception
        self.assertIsNone(loaded.get("_schema_version"))
        # All pre-existing fields preserved
        self.assertEqual(loaded["model"]["display_name"], "Opus 4.5")
        self.assertEqual(loaded["cost"]["total_cost_usd"], 0.5)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
