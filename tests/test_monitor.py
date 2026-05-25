#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Unit tests for monitor.py — stdlib only, no pytest required.

Run:
    python -m unittest tests.test_monitor
    # or directly:
    python tests/test_monitor.py
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os
import pathlib
import re
import subprocess
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
)

import monitor
import monitor as _monitor_mod
from monitor import (
    _fit_buf_height,
    calc_rates,
    f_tok,
    f_cost,
    f_dur,
    f_cd,
    _num,
    _limit_color,
    _reset_color,
    collect_warnings,
    truncate,
    mkbar,
    calc_cross_session_costs,
    _parse_ts,
    _calc_streaks,
    _model_label,
    _total_tokens,
    scan_transcript_stats,
    render_stats,
    render_legend,
    render_frame,
    _CLAUDE_DIR,
    _usage_cache,
    WARN_BRN,
    BRN_MAX,
    CTR_MAX,
    CST_MAX,
    BAR_W,
    _parse_version,
    _rls_cache,
    _rls_blink,
    VERSION,
    _rls_check_worker,
    _rls_maybe_check,
    _RLS_TTL,
    spin_session,
    spin_rls,
    _SPIN_SESSION,
    _SPIN_RLS,
    render_update_modal,
    list_sessions,
    load_state,
    load_history,
    DATA_DIR,
    render_picker,
    cached_cross_session_costs,
    _cost_cache,
    flush,
    SYNC_ON,
    SYNC_OFF,
    render_menu,
    render_cost_breakdown,
    _model_code,
    _cost_thirds,
    _get_pricing,
    _DEFAULT_PRICING,
    _aggregate_session_cost,
    _SESSION_COST_CACHE,
    _SESSION_COST_TTL,
    _SESSION_COST_CACHE_MAX,
)

# Needed for sl_calc_rates identity assertion in TestCalcRates
from statusline import _calc_rates as sl_calc_rates


# ---------------------------------------------------------------------------
# Shared helpers (canonical home: tests/_helpers.py)
# ---------------------------------------------------------------------------
from tests._helpers import (
    _vlen,
    _full_data,
    _strip_ansi,
    _write_session,
    _write_transcript,
    _make_assistant_record,
)


# ---------------------------------------------------------------------------
# _fit_buf_height
# ---------------------------------------------------------------------------
class TestFitBufHeight(unittest.TestCase):

    # -- clip_tail=True (legend / picker) ------------------------------------

    def test_clip_tail_pads_short_buf(self):
        buf = ["a", "b"]
        _fit_buf_height(buf, 10, clip_tail=True)
        self.assertEqual(len(buf), 10)

    def test_clip_tail_removes_empty_lines(self):
        buf = ["a", "", "b", "", "c", "", "", ""]
        _fit_buf_height(buf, 5, clip_tail=True)
        self.assertLessEqual(len(buf), 5)

    def test_clip_tail_clips_bottom_when_too_tall(self):
        buf = [str(i) for i in range(30)]
        _fit_buf_height(buf, 10, clip_tail=True)
        self.assertEqual(len(buf), 10)
        # Header preserved, bottom clipped
        self.assertEqual(buf[0], "0")
        self.assertEqual(buf[-1], "9")

    def test_clip_tail_rows_zero(self):
        buf = ["a", "b", "c"]
        _fit_buf_height(buf, 0, clip_tail=True)
        self.assertEqual(len(buf), 1)

    def test_clip_tail_rows_one(self):
        buf = ["a", "b", "c"]
        _fit_buf_height(buf, 1, clip_tail=True)
        self.assertEqual(len(buf), 1)

    def test_clip_tail_rows_two(self):
        buf = ["x"] * 10
        _fit_buf_height(buf, 2, clip_tail=True)
        self.assertEqual(len(buf), 2)

    # -- clip_tail=False (dashboard) -----------------------------------------

    def test_dashboard_preserves_tail(self):
        footer = ["sep", "[q]qt"]
        body = ["line"] * 20
        buf = body + footer
        _fit_buf_height(buf, 10, clip_tail=False)
        self.assertEqual(len(buf), 10)
        self.assertEqual(buf[-2:], footer)

    def test_dashboard_pads_when_short(self):
        buf = ["a", "b", "footer1", "footer2"]
        _fit_buf_height(buf, 20, clip_tail=False)
        self.assertEqual(len(buf), 20)

    def test_dashboard_removes_empty_lines_from_body(self):
        footer = ["f1", "f2"]
        body = ["a", "", "b", "", "c", ""]
        buf = body + footer
        _fit_buf_height(buf, 8, clip_tail=False)
        self.assertEqual(len(buf), 8)
        self.assertEqual(buf[-2:], footer)

    def test_dashboard_rows_negative(self):
        buf = ["a", "b"]
        _fit_buf_height(buf, -5, clip_tail=False)
        self.assertEqual(len(buf), 1)

    def test_empty_buf(self):
        buf = []
        _fit_buf_height(buf, 10, clip_tail=True)
        self.assertEqual(len(buf), 10)

    def test_buf_exactly_target(self):
        buf = ["x"] * 10
        _fit_buf_height(buf, 10, clip_tail=True)
        self.assertEqual(len(buf), 10)


# ---------------------------------------------------------------------------
# _limit_color / _reset_color (monitor)
# ---------------------------------------------------------------------------
class TestLimitColor(unittest.TestCase):

    def test_low_usage_yellow(self):
        self.assertEqual(_limit_color(20), C_YEL)

    def test_mid_usage_yellow(self):
        self.assertEqual(_limit_color(55), C_YEL)

    def test_high_usage_red(self):
        self.assertEqual(_limit_color(85), C_RED)


class TestResetColor(unittest.TestCase):

    def test_lots_of_time_red(self):
        # Reset in 4h out of 5h window = 80% remaining → red (far from reset)
        self.assertEqual(_reset_color(time.time() + 14400, 18000), C_RED)

    def test_some_time_yellow(self):
        # Reset in 1.5h out of 5h window = 30% remaining → yellow
        self.assertEqual(_reset_color(time.time() + 5400, 18000), C_YEL)

    def test_little_time_green(self):
        # Reset in 15min out of 5h window = 5% remaining → green (close to reset)
        self.assertEqual(_reset_color(time.time() + 900, 18000), C_GRN)

    def test_just_reset_green(self):
        # Reset epoch in the past → just reset
        self.assertEqual(_reset_color(time.time() - 10, 18000), C_GRN)

    def test_no_data_dim(self):
        self.assertEqual(_reset_color(0, 18000), C_DIM)


# ---------------------------------------------------------------------------
# calc_rates
# ---------------------------------------------------------------------------
class TestCalcRates(unittest.TestCase):

    def _entry(self, t, cost, ctx_pct):
        return {"t": t, "cost": {"total_cost_usd": cost}, "context_window": {"used_percentage": ctx_pct}}

    def test_basic(self):
        hist = [self._entry(1_600_000_000, 0.0, 10.0), self._entry(1_600_000_060, 0.06, 20.0)]
        cpm, xpm = calc_rates(hist)
        self.assertAlmostEqual(cpm, 0.06, places=5)
        self.assertAlmostEqual(xpm, 10.0, places=5)

    def test_too_few_entries(self):
        self.assertEqual(calc_rates([]), (None, None))
        self.assertEqual(calc_rates([self._entry(1_600_000_000, 0, 0)]), (None, None))

    def test_dt_too_small(self):
        hist = [self._entry(1_600_000_000, 0, 0), self._entry(1_600_000_005, 1, 10)]
        self.assertEqual(calc_rates(hist), (None, None))

    def test_implausible_timestamp_zero(self):
        hist = [self._entry(0, 0, 0), self._entry(1_600_000_060, 1, 10)]
        self.assertEqual(calc_rates(hist), (None, None))

    def test_implausible_timestamp_pre2020(self):
        # 2019-01-01 = 1546300800
        hist = [self._entry(1_546_300_800, 0, 0), self._entry(1_546_300_860, 1, 10)]
        self.assertEqual(calc_rates(hist), (None, None))

    def test_missing_t_field(self):
        hist = [{"cost": {"total_cost_usd": 0}}, {"t": 1_600_000_060, "cost": {"total_cost_usd": 1}}]
        self.assertEqual(calc_rates(hist), (None, None))

    def test_implausible_t1_pre2020(self):
        """Both endpoints must pass MIN_EPOCH (statusline used to check only t0)."""
        hist = [self._entry(1_600_000_000, 0, 0), self._entry(1_546_300_860, 1, 10)]
        self.assertEqual(calc_rates(hist), (None, None))

    def test_decreasing_cost_brn_none(self):
        hist = [self._entry(1_600_000_000, 1.0, 50.0), self._entry(1_600_000_060, 0.5, 55.0)]
        brn, ctr = calc_rates(hist)
        self.assertIsNone(brn)
        self.assertIsNotNone(ctr)

    def test_decreasing_ctx_ctr_none(self):
        hist = [self._entry(1_600_000_000, 0.0, 60.0), self._entry(1_600_000_060, 0.1, 50.0)]
        brn, ctr = calc_rates(hist)
        self.assertIsNotNone(brn)
        self.assertIsNone(ctr)

    def test_shared_module_identity(self):
        from monitor import calc_rates as m_cr
        self.assertIs(m_cr, shared.calc_rates)
        self.assertIs(sl_calc_rates, shared.calc_rates)


# ---------------------------------------------------------------------------
# _num
# ---------------------------------------------------------------------------
class TestNum(unittest.TestCase):

    def test_float(self):
        self.assertEqual(_num(3.14), 3.14)

    def test_int(self):
        self.assertEqual(_num(42), 42.0)

    def test_string_numeric(self):
        self.assertEqual(_num("7.5"), 7.5)

    def test_none_returns_default(self):
        self.assertEqual(_num(None), 0)
        self.assertEqual(_num(None, 99), 99)

    def test_string_invalid(self):
        self.assertEqual(_num("abc"), 0)
        self.assertEqual(_num("abc", -1), -1)

    def test_list_invalid(self):
        self.assertEqual(_num([1, 2]), 0)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
class TestFormatters(unittest.TestCase):

    def test_f_tok_none(self):
        self.assertEqual(f_tok(None), "--")

    def test_f_tok_zero(self):
        self.assertEqual(f_tok(0), "--")

    def test_f_tok_small(self):
        self.assertEqual(f_tok(500), "500")

    def test_f_tok_kilo(self):
        self.assertEqual(f_tok(1500), "1.5k")

    def test_f_tok_mega(self):
        self.assertEqual(f_tok(1_000_000), "1M")

    def test_f_cost_none(self):
        self.assertEqual(f_cost(None), "--")

    def test_f_cost_zero(self):
        self.assertEqual(f_cost(0), "--")

    def test_f_cost_small(self):
        self.assertIn("$", f_cost(0.001))

    def test_f_cost_normal(self):
        self.assertEqual(f_cost(1.5), "1.50 $")

    def test_f_dur_none(self):
        self.assertEqual(f_dur(None), "--")

    def test_f_dur_seconds(self):
        self.assertEqual(f_dur(45_000), "45s")

    def test_f_dur_minutes(self):
        self.assertEqual(f_dur(90_000), "1m 30s")

    def test_f_dur_hours(self):
        self.assertEqual(f_dur(3_661_000), "1h 01m")

    def test_f_cd_none(self):
        self.assertEqual(f_cd(None), "--")

    def test_f_cd_past(self):
        self.assertEqual(f_cd(1_000_000), "now")

    def test_f_cd_string(self):
        # string epoch that can be coerced — +7260s = 2h 1m → contains "h"
        import time
        result = f_cd(str(int(time.time()) + 7260))
        self.assertIn("h", result)


# ===========================================================================
# STATUSLINE TESTS
# ===========================================================================
# (_vlen and _full_data already imported above from tests._helpers)


# ---------------------------------------------------------------------------
# _sanitize
# ---------------------------------------------------------------------------
class TestFixedRangeConstants(unittest.TestCase):

    def test_brn_max_value(self):
        self.assertEqual(BRN_MAX, 10.0)

    def test_ctr_max_value(self):
        self.assertEqual(CTR_MAX, 10.0)

    def test_cst_max_value(self):
        self.assertEqual(CST_MAX, 1000.0)

    def test_warn_brn_default(self):
        self.assertEqual(WARN_BRN, 3.0)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------
class TestCollectWarnings(unittest.TestCase):

    def test_no_warnings_healthy(self):
        data = _full_data()
        self.assertEqual(collect_warnings(data, 0.01, 0.1), [])

    def test_ctf_under_30min(self):
        data = _full_data()
        data["context_window"]["used_percentage"] = 70
        warns = collect_warnings(data, 0.01, 5.0)  # 30% / 5 = 6 min
        self.assertTrue(any("CTF" in w for w in warns))

    def test_ctf_over_30min_no_warn(self):
        data = _full_data()
        data["context_window"]["used_percentage"] = 10
        warns = collect_warnings(data, 0.01, 0.1)  # 90% / 0.1 = 900 min
        self.assertFalse(any("CTF" in w for w in warns))

    def test_5hl_above_80_no_warn(self):
        data = _full_data()
        data["rate_limits"]["five_hour"]["used_percentage"] = 85
        warns = collect_warnings(data, 0.01, 0.1)
        self.assertFalse(any("5HL" in w for w in warns))

    def test_7dl_above_80_no_warn(self):
        data = _full_data()
        data["rate_limits"]["seven_day"]["used_percentage"] = 90
        warns = collect_warnings(data, 0.01, 0.1)
        self.assertFalse(any("7DL" in w for w in warns))

    def test_brn_above_threshold(self):
        warns = collect_warnings(_full_data(), WARN_BRN + 0.01, 0.1)
        self.assertTrue(any("BRN" in w for w in warns))

    def test_brn_below_threshold(self):
        warns = collect_warnings(_full_data(), 0.01, 0.1)
        self.assertFalse(any("BRN" in w for w in warns))

    def test_multiple_warnings(self):
        data = _full_data()
        data["context_window"]["used_percentage"] = 95
        warns = collect_warnings(data, WARN_BRN + 0.5, 5.0)  # CTF + BRN
        self.assertGreaterEqual(len(warns), 2)


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------
class TestTruncate(unittest.TestCase):

    def test_short_unchanged(self):
        self.assertEqual(truncate("abc", 10), "abc")

    def test_exact_length(self):
        self.assertEqual(_vlen(truncate("abcde", 5)), 5)

    def test_truncates_long(self):
        result = truncate("abcdefghij", 5)
        self.assertEqual(_vlen(result), 5)

    def test_preserves_ansi(self):
        s = f"\033[31mhello world\033[0m"
        result = truncate(s, 5)
        self.assertEqual(_vlen(result), 5)
        self.assertIn("hello", result)

    def test_zero_width(self):
        result = truncate("abc", 0)
        self.assertEqual(_vlen(result), 0)


# ---------------------------------------------------------------------------
# mkbar
# ---------------------------------------------------------------------------
class TestMkbar(unittest.TestCase):

    def test_zero_percent(self):
        result = mkbar(0)
        plain = _ANSI_RE.sub("", result)
        self.assertIn("0.0", plain)
        self.assertIn("[", plain)
        self.assertIn("]", plain)

    def test_100_percent(self):
        result = mkbar(100)
        plain = _ANSI_RE.sub("", result)
        self.assertIn("100.0", plain)

    def test_clamps_negative(self):
        result = mkbar(-10)
        plain = _ANSI_RE.sub("", result)
        self.assertIn("0.0", plain)

    def test_clamps_over_100(self):
        result = mkbar(150)
        plain = _ANSI_RE.sub("", result)
        self.assertIn("100.0", plain)

    def test_green_under_50(self):
        result = mkbar(30)
        self.assertIn(C_GRN, result)

    def test_yellow_50_79(self):
        result = mkbar(60)
        self.assertIn(C_YEL, result)

    def test_red_over_80(self):
        result = mkbar(90)
        self.assertIn(C_RED, result)

    def test_custom_color(self):
        result = mkbar(30, C_ORN)
        self.assertIn(C_ORN, result)

    def test_visual_width(self):
        result = mkbar(50)
        plain = _ANSI_RE.sub("", result)
        # [████░░░]  XX.X %  → brackets + BAR_W + space + percent
        self.assertIn("[", plain)
        self.assertIn("]", plain)


# ---------------------------------------------------------------------------
# IPC — write_shared_state / _trim_history
# ---------------------------------------------------------------------------
class TestCalcCrossSessionCosts(unittest.TestCase):

    def setUp(self):
        import tempfile, pathlib
        import monitor
        self.tmpdir = tempfile.mkdtemp()
        self._orig = monitor.DATA_DIR
        monitor.DATA_DIR = pathlib.Path(self.tmpdir)

    def tearDown(self):
        import shutil, monitor
        monitor.DATA_DIR = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_dir(self):
        today, week = calc_cross_session_costs()
        self.assertEqual(today, 0.0)
        self.assertEqual(week, 0.0)

    def test_single_session(self):
        import pathlib, json, time
        from datetime import datetime
        now = time.time()
        today_start = datetime.combine(datetime.today().date(),
                                       datetime.min.time()).timestamp()
        entries = [
            json.dumps({"t": today_start - 100, "cost": {"total_cost_usd": 0.0}}),
            json.dumps({"t": now, "cost": {"total_cost_usd": 5.0}}),
        ]
        jl = pathlib.Path(self.tmpdir) / "sess1.jsonl"
        jl.write_text("\n".join(entries) + "\n", encoding="utf-8")
        today, week = calc_cross_session_costs()
        self.assertAlmostEqual(today, 5.0, places=2)
        self.assertGreater(week, 0.0)

    def test_invalid_sid_skipped(self):
        import pathlib
        jl = pathlib.Path(self.tmpdir) / "..evil.jsonl"
        jl.write_text('{"t": 1, "cost": {"total_cost_usd": 999}}\n', encoding="utf-8")
        today, week = calc_cross_session_costs()
        self.assertEqual(today, 0.0)


# ---------------------------------------------------------------------------
# cached_cross_session_costs
# ---------------------------------------------------------------------------
class TestCachedCrossSessionCosts(unittest.TestCase):

    def setUp(self):
        import tempfile, pathlib, monitor
        self.tmpdir = tempfile.mkdtemp()
        self._orig = monitor.DATA_DIR
        self._orig_cache = _cost_cache.copy()
        monitor.DATA_DIR = pathlib.Path(self.tmpdir)
        _cost_cache.update({"t": 0, "today": 0.0, "week": 0.0})

    def tearDown(self):
        import shutil, monitor
        monitor.DATA_DIR = self._orig
        _cost_cache.update(self._orig_cache)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ttl_returns_cached(self):
        import time
        _cost_cache.update({"t": time.monotonic(), "today": 42.0, "week": 99.0})
        today, week = cached_cross_session_costs(ttl=60)
        self.assertEqual(today, 42.0)
        self.assertEqual(week, 99.0)

    def test_ttl_expired_refreshes(self):
        _cost_cache.update({"t": 0, "today": 42.0, "week": 99.0})
        today, week = cached_cross_session_costs(ttl=0)
        # Empty dir → 0.0
        self.assertEqual(today, 0.0)
        self.assertEqual(week, 0.0)


# ---------------------------------------------------------------------------
# _parse_ts
# ---------------------------------------------------------------------------
class TestParseTs(unittest.TestCase):

    def test_iso_basic(self):
        ts = _parse_ts("2026-04-12T17:23:27.144")
        self.assertGreater(ts, 0)

    def test_iso_z_suffix(self):
        ts = _parse_ts("2026-04-12T17:23:27.144Z")
        self.assertGreater(ts, 0)

    def test_iso_positive_offset(self):
        ts = _parse_ts("2026-04-12T17:23:27.144+05:30")
        self.assertGreater(ts, 0)

    def test_iso_negative_offset(self):
        ts = _parse_ts("2026-04-12T17:23:27.144-05:00")
        self.assertGreater(ts, 0)

    def test_empty_string(self):
        self.assertEqual(_parse_ts(""), 0)

    def test_none(self):
        self.assertEqual(_parse_ts(None), 0)

    def test_malformed(self):
        self.assertEqual(_parse_ts("not-a-date"), 0)

    def test_consistent_across_formats(self):
        """Z and no-tz should produce the same result (both naive local)."""
        a = _parse_ts("2026-04-12T17:23:27")
        b = _parse_ts("2026-04-12T17:23:27Z")
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# _calc_streaks
# ---------------------------------------------------------------------------
class TestCalcStreaks(unittest.TestCase):

    def test_empty_returns_0_0(self):
        self.assertEqual(_calc_streaks(set()), (0, 0))

    def test_single_day_today(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        current, longest = _calc_streaks({today})
        self.assertEqual(current, 1)
        self.assertEqual(longest, 1)

    def test_single_day_past(self):
        current, longest = _calc_streaks({"2020-01-01"})
        self.assertEqual(current, 0)
        self.assertEqual(longest, 1)

    def test_consecutive_3_days_ending_today(self):
        from datetime import datetime, timedelta
        today = datetime.now().date()
        days = {(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)}
        current, longest = _calc_streaks(days)
        self.assertEqual(current, 3)
        self.assertEqual(longest, 3)

    def test_gap_breaks_streak(self):
        from datetime import datetime, timedelta
        today = datetime.now().date()
        # today + 3 days ago (gap at yesterday)
        days = {
            today.strftime("%Y-%m-%d"),
            (today - timedelta(days=3)).strftime("%Y-%m-%d"),
        }
        current, longest = _calc_streaks(days)
        self.assertEqual(current, 1)
        self.assertEqual(longest, 1)

    def test_longest_in_past_not_current(self):
        from datetime import datetime, timedelta
        today = datetime.now().date()
        # 5-day streak in past, 1-day current
        past = {(today - timedelta(days=20 + i)).strftime("%Y-%m-%d") for i in range(5)}
        current_day = {today.strftime("%Y-%m-%d")}
        current, longest = _calc_streaks(past | current_day)
        self.assertEqual(current, 1)
        self.assertEqual(longest, 5)


# ---------------------------------------------------------------------------
# _model_label
# ---------------------------------------------------------------------------
class TestModelLabel(unittest.TestCase):

    def test_known_opus(self):
        self.assertEqual(_model_label("claude-opus-4-6"), "Opus 4.6")

    def test_known_opus_47(self):
        self.assertEqual(_model_label("claude-opus-4-7"), "Opus 4.7")

    def test_known_sonnet(self):
        self.assertEqual(_model_label("claude-sonnet-4-6"), "Sonnet 4.6")

    def test_known_haiku(self):
        self.assertEqual(_model_label("claude-haiku-4-5-20251001"), "Haiku 4.5")

    def test_short_haiku(self):
        self.assertEqual(_model_label("haiku"), "Haiku")

    def test_short_sonnet(self):
        self.assertEqual(_model_label("sonnet"), "Sonnet")

    def test_short_opus(self):
        self.assertEqual(_model_label("opus"), "Opus")

    def test_unknown_passthrough(self):
        self.assertEqual(_model_label("claude-future-99"), "claude-future-99")

    def test_dynamic_regex_opus(self):
        self.assertEqual(_model_label("claude-opus-99-9"), "Opus 99.9")

    def test_dynamic_regex_sonnet(self):
        self.assertEqual(_model_label("claude-sonnet-5-0"), "Sonnet 5.0")

    def test_dynamic_regex_haiku(self):
        self.assertEqual(_model_label("claude-haiku-5-1"), "Haiku 5.1")

    def test_empty_returns_question(self):
        self.assertEqual(_model_label(""), "?")

    def test_bracket_suffix_stripped(self):
        self.assertEqual(_model_label("claude-opus-4-7[1m]"), "Opus 4.7")


class TestEnvFloat(unittest.TestCase):

    def test_valid_float(self):
        from monitor import _env_float
        os.environ["_TEST_ENV_FLOAT"] = "5.0"
        try:
            self.assertEqual(_env_float("_TEST_ENV_FLOAT", 10.0), 5.0)
        finally:
            os.environ.pop("_TEST_ENV_FLOAT", None)

    def test_missing_uses_default(self):
        from monitor import _env_float
        os.environ.pop("_TEST_ENV_FLOAT_MISSING", None)
        self.assertEqual(_env_float("_TEST_ENV_FLOAT_MISSING", 7.5), 7.5)

    def test_invalid_uses_default(self):
        from monitor import _env_float
        os.environ["_TEST_ENV_FLOAT_BAD"] = "notanumber"
        try:
            self.assertEqual(_env_float("_TEST_ENV_FLOAT_BAD", 2.0), 2.0)
        finally:
            os.environ.pop("_TEST_ENV_FLOAT_BAD", None)


# ---------------------------------------------------------------------------
# scan_transcript_stats
# ---------------------------------------------------------------------------
class TestScanTranscriptStats(unittest.TestCase):

    def setUp(self):
        import tempfile, pathlib, monitor
        self.tmpdir = tempfile.mkdtemp()
        self._orig = monitor._CLAUDE_DIR
        self._orig_cache = monitor._usage_cache.copy()
        monitor._CLAUDE_DIR = pathlib.Path(self.tmpdir)
        monitor._usage_cache.clear()

    def tearDown(self):
        import shutil, monitor
        monitor._CLAUDE_DIR = self._orig
        monitor._usage_cache.clear()
        monitor._usage_cache.update(self._orig_cache)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_dir(self):
        models, ov = scan_transcript_stats("all", ttl=0)
        self.assertEqual(models, {})
        self.assertEqual(ov["sessions"], 0)

    def test_single_session(self):
        import json
        lines = [
            json.dumps({"type": "user", "timestamp": "2026-04-12T10:00:00Z"}),
            json.dumps({
                "type": "assistant",
                "timestamp": "2026-04-12T10:05:00Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {"input_tokens": 100, "output_tokens": 200},
                },
            }),
            json.dumps({
                "type": "assistant",
                "timestamp": "2026-04-12T10:10:00Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {"input_tokens": 50, "output_tokens": 80},
                },
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        models, ov = scan_transcript_stats("all", ttl=0)
        self.assertEqual(models["claude-opus-4-6"]["input"], 150)
        self.assertEqual(models["claude-opus-4-6"]["output"], 280)
        self.assertEqual(models["claude-opus-4-6"]["calls"], 2)
        self.assertEqual(ov["sessions"], 1)
        self.assertIn("2026-04-12", ov["active_days"])

    def test_subagent_excluded_from_session_count(self):
        import json
        main_lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 10, "output_tokens": 20}},
        })]
        sub_lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:01:00Z",
            "message": {"model": "claude-haiku-4-5-20251001",
                        "usage": {"input_tokens": 5, "output_tokens": 10}},
        })]
        _write_session(self.tmpdir, "proj1", "sess1", main_lines)
        _write_session(self.tmpdir, "proj1", "sess1", sub_lines, subagent=True)
        models, ov = scan_transcript_stats("all", ttl=0)
        # Session count: only main session, not subagent
        self.assertEqual(ov["sessions"], 1)
        # But tokens from subagent are included
        self.assertIn("claude-haiku-4-5-20251001", models)

    def test_malformed_lines_skipped(self):
        import json
        lines = [
            "not json at all",
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 10, "output_tokens": 20}},
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        models, ov = scan_transcript_stats("all", ttl=0)
        self.assertEqual(models["claude-opus-4-6"]["calls"], 1)

    def test_ttl_cache(self):
        import json, monitor
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 10, "output_tokens": 20}},
        })]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        # First call populates cache
        m1, _ = scan_transcript_stats("all", ttl=60)
        self.assertEqual(m1["claude-opus-4-6"]["calls"], 1)
        # Write more data
        lines.append(json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:01:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 10, "output_tokens": 20}},
        }))
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        # Cached — should still show 1 call
        m2, _ = scan_transcript_stats("all", ttl=60)
        self.assertEqual(m2["claude-opus-4-6"]["calls"], 1)
        # Force refresh
        m3, _ = scan_transcript_stats("all", ttl=0)
        self.assertEqual(m3["claude-opus-4-6"]["calls"], 2)

    def test_daily_tokens(self):
        import json
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 200}},
        })]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        _, ov = scan_transcript_stats("all", ttl=0)
        self.assertEqual(ov["daily_tokens"].get("2026-04-12"), 300)

    def test_daily_tokens_includes_cache(self):
        import json
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 200,
                            "cache_read_input_tokens": 5000,
                            "cache_creation_input_tokens": 300,
                        }},
        })]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        _, ov = scan_transcript_stats("all", ttl=0)
        self.assertEqual(ov["daily_tokens"].get("2026-04-12"), 5600)

    def test_longest_session_duration(self):
        import json
        lines = [
            json.dumps({"type": "user", "timestamp": "2026-04-12T10:00:00Z"}),
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T11:00:00Z",
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 10, "output_tokens": 20}},
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        _, ov = scan_transcript_stats("all", ttl=0)
        # 1 hour = 3600000 ms
        self.assertAlmostEqual(ov["longest_dur_ms"], 3600000, delta=1000)

    def test_period_7d_filters_old_entries(self):
        import json, time
        from datetime import datetime
        now = time.time()
        old_ts = "2025-01-01T10:00:00Z"  # well outside 7d window
        new_ts = datetime.fromtimestamp(now - 3600).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            json.dumps({
                "type": "assistant", "timestamp": old_ts,
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 100, "output_tokens": 200}},
            }),
            json.dumps({
                "type": "assistant", "timestamp": new_ts,
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 50, "output_tokens": 80}},
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        models_all, _ = scan_transcript_stats("all", ttl=0)
        models_7d, _ = scan_transcript_stats("7d", ttl=0)
        self.assertEqual(models_all["claude-opus-4-6"]["input"], 150)
        self.assertEqual(models_7d["claude-opus-4-6"]["input"], 50)

    def test_period_30d_filters_old_entries(self):
        import json, time
        from datetime import datetime
        now = time.time()
        old_ts = "2025-01-01T10:00:00Z"  # well outside 30d window
        new_ts = datetime.fromtimestamp(now - 3600).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            json.dumps({
                "type": "assistant", "timestamp": old_ts,
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 100, "output_tokens": 200}},
            }),
            json.dumps({
                "type": "assistant", "timestamp": new_ts,
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 50, "output_tokens": 80}},
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        models_all, _ = scan_transcript_stats("all", ttl=0)
        models_30d, _ = scan_transcript_stats("30d", ttl=0)
        self.assertEqual(models_all["claude-opus-4-6"]["input"], 150)
        self.assertEqual(models_30d["claude-opus-4-6"]["input"], 50)

    def test_synthetic_model_filtered(self):
        import json
        lines = [
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 100, "output_tokens": 200}},
            }),
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:01:00Z",
                "message": {"model": "<synthetic>",
                            "usage": {"input_tokens": 0, "output_tokens": 0}},
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        models, _ = scan_transcript_stats("all", ttl=0)
        self.assertNotIn("<synthetic>", models)
        self.assertEqual(len(models), 1)
        self.assertEqual(models["claude-opus-4-6"]["calls"], 1)

    def test_multiple_models(self):
        import json
        lines = [
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
                "message": {"model": "claude-opus-4-6",
                            "usage": {"input_tokens": 100, "output_tokens": 200}},
            }),
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:01:00Z",
                "message": {"model": "claude-haiku-4-5-20251001",
                            "usage": {"input_tokens": 50, "output_tokens": 80}},
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        models, _ = scan_transcript_stats("all", ttl=0)
        self.assertEqual(len(models), 2)
        self.assertIn("claude-opus-4-6", models)
        self.assertIn("claude-haiku-4-5-20251001", models)

    def test_cache_tokens_summed(self):
        import json
        lines = [
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 500,
                        "cache_creation_input_tokens": 200,
                    },
                },
            }),
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:01:00Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 300,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        models, _ = scan_transcript_stats("all", ttl=0)
        m = models["claude-opus-4-6"]
        self.assertEqual(m["cache_read"], 800)
        self.assertEqual(m["cache_write"], 200)
        self.assertEqual(m["input"], 120)
        self.assertEqual(m["output"], 60)
        self.assertEqual(m["calls"], 2)

    def test_cache_tokens_absent_defaults_zero(self):
        import json
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        })]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        models, _ = scan_transcript_stats("all", ttl=0)
        m = models["claude-opus-4-6"]
        self.assertEqual(m.get("cache_read", 0), 0)
        self.assertEqual(m.get("cache_write", 0), 0)


# ---------------------------------------------------------------------------
# render_stats
# ---------------------------------------------------------------------------
class TestRenderStats(unittest.TestCase):

    def setUp(self):
        import tempfile, pathlib, monitor
        self.tmpdir = tempfile.mkdtemp()
        self._orig = monitor._CLAUDE_DIR
        self._orig_cache = monitor._usage_cache.copy()
        monitor._CLAUDE_DIR = pathlib.Path(self.tmpdir)
        monitor._usage_cache.clear()

    def tearDown(self):
        import shutil, monitor
        monitor._CLAUDE_DIR = self._orig
        monitor._usage_cache.clear()
        monitor._usage_cache.update(self._orig_cache)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_data_shows_placeholder(self):
        buf = render_stats(80, 24, "all")
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("No transcript data", plain)

    def test_with_data_shows_model(self):
        import json
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 200}},
        })]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        buf = render_stats(80, 40, "all")
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("OP", plain)
        self.assertIn("4.6", plain)
        self.assertIn("100.0", plain)  # 100% single model
        self.assertIn("ALL", plain)

    def test_shows_overview_metrics(self):
        import json
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 200}},
        })]
        _write_session(self.tmpdir, "proj1", "sess1", lines)
        buf = render_stats(80, 40, "all")
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("SES", plain)
        self.assertIn("DAY", plain)
        self.assertIn("STK", plain)
        self.assertIn("LSS", plain)

    def test_period_labels(self):
        buf_all = render_stats(80, 24, "all")
        buf_7d = render_stats(80, 24, "7d")
        buf_30d = render_stats(80, 24, "30d")
        self.assertIn("All Time", _ANSI_RE.sub("", "\n".join(buf_all)))
        self.assertIn("Last 7 Days", _ANSI_RE.sub("", "\n".join(buf_7d)))
        self.assertIn("Last 30 Days", _ANSI_RE.sub("", "\n".join(buf_30d)))

    def test_footer_has_keys(self):
        buf = render_stats(80, 24, "all")
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("1", plain)
        self.assertIn("2", plain)
        self.assertIn("3", plain)
        self.assertIn("close", plain)

    def test_render_stats_bar_includes_cache_tokens(self):
        import json
        lines = [
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
                "message": {"model": "claude-opus-4-6",
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 200,
                                "cache_read_input_tokens": 10000,
                            }},
            }),
            json.dumps({
                "type": "assistant", "timestamp": "2026-04-12T10:01:00Z",
                "message": {"model": "claude-haiku-4-5-20251001",
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 200,
                            }},
            }),
        ]
        _write_session(self.tmpdir, "proj1", "sess1", lines)

        # Verify _total_tokens directly
        import monitor
        monitor._usage_cache.clear()
        models, _ = scan_transcript_stats("all", ttl=0)
        a = models["claude-opus-4-6"]
        b = models["claude-haiku-4-5-20251001"]
        self.assertEqual(_total_tokens(a), 10300)
        self.assertEqual(_total_tokens(b), 300)

        total_all = _total_tokens(a) + _total_tokens(b)
        pct_a = _total_tokens(a) / total_all * 100
        pct_b = _total_tokens(b) / total_all * 100
        self.assertGreater(pct_a, 97.0)
        self.assertLess(pct_b, 3.0)


# ---------------------------------------------------------------------------
# render_legend
# ---------------------------------------------------------------------------
class TestRenderLegend(unittest.TestCase):

    def test_returns_buffer(self):
        buf = render_legend(80, 24)
        self.assertIsInstance(buf, list)
        self.assertEqual(len(buf), 24)

    def test_contains_all_metrics(self):
        buf = render_legend(80, 60)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        for label in ["APR", "CHR", "CTX", "5HL", "7DL", "BRN", "CTR", "CST",
                       "TDY", "WEK", "LNS", "NOW", "UPD", "RLS"]:
            self.assertIn(label, plain)

    def test_contains_usage_stats_section(self):
        buf = render_legend(80, 60)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        for label in ["SES", "DAY", "STK", "LSS", "TOP"]:
            self.assertIn(label, plain)

    def test_contains_keys(self):
        buf = render_legend(80, 60)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        for key in ["q", "r", "s", "u", "l", "1-9"]:
            self.assertIn(key, plain)


# ---------------------------------------------------------------------------
# render_menu
# ---------------------------------------------------------------------------
class TestRenderMenu(unittest.TestCase):

    def test_returns_buffer(self):
        buf = render_menu(80, 30)
        self.assertIsInstance(buf, list)
        self.assertGreater(len(buf), 0)

    def test_contains_all_keys(self):
        buf = render_menu(80, 30)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        for key in ["q", "r", "s", "t", "c", "p", "l", "u"]:
            self.assertIn(key, plain)

    def test_contains_sections(self):
        buf = render_menu(80, 30)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("VIEWS", plain)
        self.assertIn("SYSTEM", plain)

    def test_show_menu_flag(self):
        buf = render_frame(_full_data(), [], 80, 35, show_menu=True)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("MENU", plain)
        self.assertIn("Quit", plain)

    def test_menu_contains_cost(self):
        buf = render_menu(80, 30)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("c", plain)
        self.assertIn("Cost Breakdown", plain)


# ---------------------------------------------------------------------------
# render_cost_breakdown
# ---------------------------------------------------------------------------
class TestRenderCostBreakdown(unittest.TestCase):

    def test_returns_buffer(self):
        buf = render_cost_breakdown(_full_data(), [], 80, 35)
        self.assertIsInstance(buf, list)
        self.assertGreater(len(buf), 0)

    def test_contains_cost_info(self):
        buf = render_cost_breakdown(_full_data(), [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("COST BREAKDOWN", plain)
        self.assertIn("CST", plain)

    def test_contains_token_breakdown(self):
        buf = render_cost_breakdown(_full_data(), [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("INP", plain)
        self.assertIn("OUT", plain)
        self.assertIn("CRD", plain)
        self.assertIn("CWR", plain)

    def test_cache_savings_shown(self):
        data = _full_data()
        data["context_window"]["current_usage"] = {
            "input_tokens": 100,
            "output_tokens": 500,
            "cache_read_input_tokens": 50000,
            "cache_creation_input_tokens": 1000,
        }
        buf = render_cost_breakdown(data, [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("SAV", plain)

    def test_with_history_shows_timeline(self):
        import time
        now = time.time()
        hist = [
            {"t": now - 300, "cost": {"total_cost_usd": 0.0}},
            {"t": now - 200, "cost": {"total_cost_usd": 0.5}},
            {"t": now - 100, "cost": {"total_cost_usd": 1.2}},
            {"t": now, "cost": {"total_cost_usd": 2.0}},
        ]
        buf = render_cost_breakdown(_full_data(), hist, 80, 40)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("BURN RATE OVER TIME", plain)

    def test_show_cost_flag(self):
        buf = render_frame(_full_data(), [], 80, 35, show_cost=True)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("COST BREAKDOWN", plain)

    def test_render_cost_breakdown_relabels_last_request(self):
        buf = render_cost_breakdown(_full_data(), [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("LAST REQUEST (est.)", plain)
        self.assertNotIn("TOKEN COSTS", plain)


# ---------------------------------------------------------------------------
# _aggregate_session_cost
# ---------------------------------------------------------------------------
class TestAggregateSessionCost(unittest.TestCase):

    def setUp(self):
        _SESSION_COST_CACHE.clear()

    def test_aggregate_session_cost_via_transcript_path(self):
        import tempfile, shutil, pathlib as _pathlib
        import monitor as _monitor
        tmpdir = tempfile.mkdtemp()
        try:
            proj_dir = _pathlib.Path(tmpdir) / "proj1"
            proj_dir.mkdir()
            jl = proj_dir / "abcd1234.jsonl"
            jl.write_text(
                _make_assistant_record("claude-opus-4-6",
                                       input_tokens=1000, output_tokens=500) + "\n"
                + _make_assistant_record("claude-sonnet-4-6",
                                         input_tokens=2000, output_tokens=300,
                                         cache_read_input_tokens=4000,
                                         cache_creation_input_tokens=1000) + "\n",
                encoding="utf-8",
            )
            fake_projects = _pathlib.Path(tmpdir).resolve()
            with patch.object(_monitor, "CLAUDE_PROJECTS_DIR", fake_projects):
                _SESSION_COST_CACHE.clear()
                data = {"session_id": "abcd1234", "transcript_path": str(jl)}
                result = _aggregate_session_cost(data)
            self.assertIsNotNone(result)
            self.assertEqual(result["input"], 3000)
            self.assertEqual(result["output"], 800)
            self.assertEqual(result["cache_read"], 4000)
            self.assertEqual(result["cache_write"], 1000)
            # Opus 4.6: 1000*5/1M + 500*25/1M = 0.005 + 0.0125 = 0.0175
            # Sonnet 4.6: 2000*3/1M + 300*15/1M + 4000*0.3/1M + 1000*3.75/1M
            #           = 0.006 + 0.0045 + 0.0012 + 0.00375 = 0.01545
            expected = 0.0175 + 0.01545
            self.assertAlmostEqual(result["cost_total"], expected, places=6)
        finally:
            shutil.rmtree(tmpdir)

    def test_aggregate_session_cost_fallback_glob(self):
        import tempfile, pathlib as _pathlib, shutil
        import monitor as _monitor
        tmpdir = tempfile.mkdtemp()
        # Structure: fake_projects/proj1/abcd5678.jsonl
        proj_dir = _pathlib.Path(tmpdir) / "proj1"
        proj_dir.mkdir(parents=True)
        jl = proj_dir / "abcd5678.jsonl"
        jl.write_text(
            _make_assistant_record("claude-sonnet-4-6",
                                   input_tokens=500, output_tokens=200) + "\n",
            encoding="utf-8",
        )
        fake_projects = _pathlib.Path(tmpdir).resolve()
        with patch.object(_monitor, "CLAUDE_PROJECTS_DIR", fake_projects):
            _SESSION_COST_CACHE.clear()
            data = {"session_id": "abcd5678"}
            result = _aggregate_session_cost(data)
        self.assertIsNotNone(result)
        self.assertEqual(result["input"], 500)
        self.assertEqual(result["output"], 200)
        shutil.rmtree(tmpdir)

    def test_aggregate_session_cost_invalid_sid(self):
        data = {"session_id": "../bad/path"}
        result = _aggregate_session_cost(data)
        self.assertIsNone(result)

    def test_aggregate_session_cost_missing_file(self):
        data = {
            "session_id": "abcd9999",
            "transcript_path": "/nonexistent/path/x.jsonl",
        }
        with patch("pathlib.Path.home", return_value=pathlib.Path("/nonexistent")):
            result = _aggregate_session_cost(data)
        self.assertIsNone(result)

    def test_aggregate_session_cost_cache_ttl(self):
        import tempfile, shutil, pathlib as _pathlib
        import monitor as _monitor
        tmpdir = tempfile.mkdtemp()
        try:
            proj_dir = _pathlib.Path(tmpdir) / "proj1"
            proj_dir.mkdir()
            jl = proj_dir / "cachesid1.jsonl"
            jl.write_text(
                _make_assistant_record("claude-sonnet-4-6",
                                       input_tokens=100, output_tokens=50) + "\n",
                encoding="utf-8",
            )
            fake_projects = _pathlib.Path(tmpdir).resolve()
            with patch.object(_monitor, "CLAUDE_PROJECTS_DIR", fake_projects):
                _SESSION_COST_CACHE.clear()
                data = {"session_id": "cachesid1", "transcript_path": str(jl)}
                r1 = _aggregate_session_cost(data)
                self.assertIsNotNone(r1)
                # Second call within TTL — should return cached (same dict object)
                r2 = _aggregate_session_cost(data)
                self.assertIs(r1, r2)
        finally:
            shutil.rmtree(tmpdir)

    def test_render_cost_breakdown_shows_session_breakdown(self):
        import tempfile, shutil, pathlib as _pathlib
        import monitor as _monitor
        tmpdir = tempfile.mkdtemp()
        try:
            proj_dir = _pathlib.Path(tmpdir) / "proj1"
            proj_dir.mkdir()
            jl = proj_dir / "rendersid1.jsonl"
            jl.write_text(
                _make_assistant_record("claude-sonnet-4-6",
                                       input_tokens=1000, output_tokens=400) + "\n",
                encoding="utf-8",
            )
            fake_projects = _pathlib.Path(tmpdir).resolve()
            with patch.object(_monitor, "CLAUDE_PROJECTS_DIR", fake_projects):
                _SESSION_COST_CACHE.clear()
                data = _full_data()
                data["session_id"] = "rendersid1"
                data["transcript_path"] = str(jl)
                buf = render_cost_breakdown(data, [], 80, 50)
            plain = _ANSI_RE.sub("", "\n".join(buf))
            self.assertIn("SESSION BREAKDOWN (est.)", plain)
        finally:
            shutil.rmtree(tmpdir)

    def test_render_cost_breakdown_reconciliation_delta_warn(self):
        import tempfile, shutil, pathlib as _pathlib
        import monitor as _monitor
        tmpdir = tempfile.mkdtemp()
        try:
            proj_dir = _pathlib.Path(tmpdir) / "proj1"
            proj_dir.mkdir()
            jl = proj_dir / "deltasid1.jsonl"
            # 80000 input tokens at Sonnet 3$/M = 0.24, 16000 output at 15$/M = 0.24 → 0.48 est
            # We'll push CST low enough to trigger >15% diff
            jl.write_text(
                _make_assistant_record("claude-sonnet-4-6",
                                       input_tokens=80000, output_tokens=16000) + "\n",
                encoding="utf-8",
            )
            fake_projects = _pathlib.Path(tmpdir).resolve()
            with patch.object(_monitor, "CLAUDE_PROJECTS_DIR", fake_projects):
                _SESSION_COST_CACHE.clear()
                data = _full_data()
                data["session_id"] = "deltasid1"
                data["transcript_path"] = str(jl)
                # est = 0.48, set reported CST to 0.20 → delta 140% → warn
                data["cost"] = {"total_cost_usd": 0.20, "total_duration_ms": 120000}
                buf = render_cost_breakdown(data, [], 80, 60)
            plain = _ANSI_RE.sub("", "\n".join(buf))
            self.assertIn("delta", plain)
        finally:
            shutil.rmtree(tmpdir)


# ---------------------------------------------------------------------------
# render_frame
# ---------------------------------------------------------------------------
class TestRenderFrame(unittest.TestCase):

    def test_basic_render(self):
        buf = render_frame(_full_data(), [], 80, 30)
        self.assertIsInstance(buf, list)
        self.assertEqual(len(buf), 30)

    def test_stale_shows_inactive(self):
        buf = render_frame(_full_data(), [], 80, 30, stale=True)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Inactive", plain)

    def test_legend_mode(self):
        buf = render_frame(_full_data(), [], 80, 50, show_legend=True)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("LEGEND", plain)

    def test_footer_has_keys(self):
        buf = render_frame(_full_data(), [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("[m]", plain)
        self.assertIn("menu", plain)


# ---------------------------------------------------------------------------
# _parse_version / RLS
# ---------------------------------------------------------------------------
class TestParseVersion(unittest.TestCase):

    def test_simple(self):
        self.assertEqual(_parse_version("1.7.1"), (1, 7, 1))

    def test_comparison_minor(self):
        self.assertGreater(_parse_version("1.7.2"), _parse_version("1.7.1"))

    def test_comparison_major(self):
        self.assertGreater(_parse_version("2.0.0"), _parse_version("1.99.99"))

    def test_comparison_ten(self):
        # String compare would fail this: "1.10.0" < "1.9.0"
        self.assertGreater(_parse_version("1.10.0"), _parse_version("1.9.0"))

    def test_pre_release_suffix(self):
        # "1.7.2-beta" should parse as (1, 7, 2)
        self.assertEqual(_parse_version("1.7.2-beta"), (1, 7, 2))

    def test_equal(self):
        self.assertEqual(_parse_version("1.7.1"), _parse_version("1.7.1"))

    def test_current_version_parses(self):
        t = _parse_version(VERSION)
        self.assertIsInstance(t, tuple)
        self.assertGreaterEqual(len(t), 3)


class TestRlsBlink(unittest.TestCase):

    def test_returns_bool(self):
        self.assertIsInstance(_rls_blink(), bool)

    def test_toggles(self):
        # Force blink state by manipulating internals
        import monitor
        monitor._rls_blink_last = 0.0  # expired
        monitor._rls_blink_on = True
        result1 = _rls_blink()
        monitor._rls_blink_last = 0.0  # expired again
        result2 = _rls_blink()
        # Should have toggled
        self.assertNotEqual(result1, result2)


class TestRlsCache(unittest.TestCase):

    def test_initial_state(self):
        # status starts as None (not yet checked)
        self.assertIn("status", _rls_cache)
        self.assertIn("t", _rls_cache)

    def test_cache_has_remote_ver(self):
        self.assertIn("remote_ver", _rls_cache)


class TestRlsInDashboard(unittest.TestCase):

    def setUp(self):
        import monitor
        self._old_cache = monitor._rls_cache.copy()
        self._old_blink = monitor._rls_blink_on
        self._old_env = os.environ.get("CC_AIO_MON_NO_UPDATE_CHECK")
        os.environ["CC_AIO_MON_NO_UPDATE_CHECK"] = "1"

    def tearDown(self):
        import monitor
        monitor._rls_cache.update(self._old_cache)
        monitor._rls_blink_on = self._old_blink
        if self._old_env is None:
            os.environ.pop("CC_AIO_MON_NO_UPDATE_CHECK", None)
        else:
            os.environ["CC_AIO_MON_NO_UPDATE_CHECK"] = self._old_env

    def test_rls_up_to_date(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "ok", "remote_ver": "1.8.0"})
        buf = render_frame(_full_data(), [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("RLS", plain)
        self.assertIn("Up to date", plain)

    def test_rls_update_available(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "update", "remote_ver": "9.9.9"})
        monitor._rls_blink_on = True
        monitor._rls_blink_last = time.monotonic()
        buf = render_frame(_full_data(), [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("RLS", plain)
        self.assertIn("v9.9.9 available", plain)

    def test_rls_error_silent(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "error", "remote_ver": None})
        buf = render_frame(_full_data(), [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertNotIn("RLS", plain)

    def test_rls_checking(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": None, "remote_ver": None})
        buf = render_frame(_full_data(), [], 80, 35)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("RLS", plain)
        self.assertIn("Checking", plain)

    def test_legend_contains_rls(self):
        buf = render_legend(80, 60)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("RLS", plain)


class TestRlsCheckWorker(unittest.TestCase):
    """Tests for _rls_check_worker() — mocks subprocess.run, no real git."""

    def setUp(self):
        self._orig_cache = dict(_rls_cache)
        # Acquire lock so worker can release it (mirrors _rls_maybe_check behavior)
        _monitor_mod._rls_lock.acquire(blocking=False)

    def tearDown(self):
        _rls_cache.update(self._orig_cache)
        # Ensure lock is released for next test
        try:
            _monitor_mod._rls_lock.release()
        except RuntimeError:
            pass

    def _make_run(self, fetch_rc=0, show_rc=0, show_stdout=""):
        """Return a side_effect callable for subprocess.run."""
        call_count = [0]

        def _run(cmd, **_kw):
            r = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:          # git fetch
                r.returncode = fetch_rc
                r.stdout = ""
            else:                           # git show
                r.returncode = show_rc
                r.stdout = show_stdout
            return r

        return _run

    # ------------------------------------------------------------------
    # a. git fetch fails → status "error"
    # ------------------------------------------------------------------
    def test_fetch_fail_sets_error(self):
        with patch("monitor.run_git", side_effect=self._make_run(fetch_rc=1)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "error")
        self.assertIsNone(_rls_cache["remote_ver"])

    # ------------------------------------------------------------------
    # b. git show fails → status "error"
    # ------------------------------------------------------------------
    def test_show_fail_sets_error(self):
        with patch("monitor.run_git", side_effect=self._make_run(fetch_rc=0, show_rc=1)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "error")
        self.assertIsNone(_rls_cache["remote_ver"])

    # ------------------------------------------------------------------
    # c. VERSION regex not found in remote output → status "error"
    # ------------------------------------------------------------------
    def test_version_regex_not_found_sets_error(self):
        stdout_no_version = "# some python file\nfoo = 'bar'\n"
        with patch("monitor.run_git",
                   side_effect=self._make_run(show_stdout=stdout_no_version)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "error")
        self.assertIsNone(_rls_cache["remote_ver"])

    # ------------------------------------------------------------------
    # d. Remote version > local → status "update", remote_ver set
    # ------------------------------------------------------------------
    def test_remote_newer_sets_update(self):
        local_parts = [int(p) for p in VERSION.split(".")]
        # Bump the last component to guarantee remote > local
        local_parts[-1] += 1
        remote_ver = ".".join(str(p) for p in local_parts)
        stdout = f'VERSION = "{remote_ver}"\n'
        with patch("monitor.run_git", side_effect=self._make_run(show_stdout=stdout)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "update")
        self.assertEqual(_rls_cache["remote_ver"], remote_ver)

    # ------------------------------------------------------------------
    # e. Remote version == local → status "ok"
    # ------------------------------------------------------------------
    def test_remote_same_sets_ok(self):
        stdout = f'VERSION = "{VERSION}"\n'
        with patch("monitor.run_git", side_effect=self._make_run(show_stdout=stdout)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "ok")
        self.assertEqual(_rls_cache["remote_ver"], VERSION)

    # ------------------------------------------------------------------
    # f. FileNotFoundError (no git binary) → status "no_git"
    # ------------------------------------------------------------------
    def test_no_git_sets_no_git(self):
        with patch("monitor.run_git", side_effect=FileNotFoundError):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "no_git")
        self.assertIsNone(_rls_cache["remote_ver"])

    # ------------------------------------------------------------------
    # g. TimeoutExpired → status "timeout"
    # ------------------------------------------------------------------
    def test_timeout_sets_timeout(self):
        with patch("monitor.run_git",
                   side_effect=subprocess.TimeoutExpired(cmd="git", timeout=15)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "timeout")
        self.assertIsNone(_rls_cache["remote_ver"])


class TestRlsMaybeCheck(unittest.TestCase):
    """Tests for _rls_maybe_check() — verifies thread spawning decisions."""

    def setUp(self):
        self._orig_cache = dict(_rls_cache)
        # Ensure lock is free
        try:
            _monitor_mod._rls_lock.release()
        except RuntimeError:
            pass
        # Expire the cache so TTL check would normally pass
        _rls_cache.update({"t": time.monotonic() - _RLS_TTL - 1, "status": None, "remote_ver": None})
        # Remove the env var if set
        self._env_var_was_set = "CC_AIO_MON_NO_UPDATE_CHECK" in os.environ
        os.environ.pop("CC_AIO_MON_NO_UPDATE_CHECK", None)

    def tearDown(self):
        _rls_cache.update(self._orig_cache)
        try:
            _monitor_mod._rls_lock.release()
        except RuntimeError:
            pass
        if self._env_var_was_set:
            os.environ["CC_AIO_MON_NO_UPDATE_CHECK"] = "1"
        else:
            os.environ.pop("CC_AIO_MON_NO_UPDATE_CHECK", None)

    # ------------------------------------------------------------------
    # a. NO_UPDATE_CHECK=1 → no thread spawned
    # ------------------------------------------------------------------
    def test_no_update_check_env_skips(self):
        os.environ["CC_AIO_MON_NO_UPDATE_CHECK"] = "1"
        with patch("monitor.threading.Thread") as mock_thread:
            _rls_maybe_check()
        mock_thread.assert_not_called()

    # ------------------------------------------------------------------
    # b. Lock already held → no thread spawned
    # ------------------------------------------------------------------
    def test_already_fetching_skips(self):
        # Acquire lock to simulate in-progress fetch
        _monitor_mod._rls_lock.acquire(blocking=False)
        with patch("monitor.threading.Thread") as mock_thread:
            _rls_maybe_check()
        mock_thread.assert_not_called()

    # ------------------------------------------------------------------
    # c. Cache TTL not expired → no thread spawned
    # ------------------------------------------------------------------
    def test_ttl_not_expired_skips(self):
        _rls_cache.update({"t": time.monotonic(), "status": "ok", "remote_ver": VERSION})
        with patch("monitor.threading.Thread") as mock_thread:
            _rls_maybe_check()
        mock_thread.assert_not_called()

    # ------------------------------------------------------------------
    # d. TTL expired, not fetching → thread spawned and started
    # ------------------------------------------------------------------
    def test_ttl_expired_spawns_thread(self):
        # Force cache TTL to be expired (monotonic=0 may be within TTL on fresh CI runners)
        _rls_cache.update({"t": time.monotonic() - _RLS_TTL - 1, "status": None, "remote_ver": None})
        mock_thread_instance = MagicMock()
        with patch("monitor.threading.Thread", return_value=mock_thread_instance) as mock_thread_cls:
            _rls_maybe_check()
        mock_thread_cls.assert_called_once_with(
            target=_rls_check_worker, daemon=True
        )
        mock_thread_instance.start.assert_called_once()


# ---------------------------------------------------------------------------
# TestUpdate — update.py
# ---------------------------------------------------------------------------
class TestSpinSession(unittest.TestCase):

    def setUp(self):
        import monitor
        self._orig_idx = monitor._spin_session_idx
        self._orig_last = monitor._spin_session_last

    def tearDown(self):
        import monitor
        monitor._spin_session_idx = self._orig_idx
        monitor._spin_session_last = self._orig_last

    def test_returns_valid_char(self):
        result = spin_session()
        self.assertIn(result, _SPIN_SESSION)

    def test_advances(self):
        import monitor
        monitor._spin_session_last = 0.0
        monitor._spin_session_idx = 0
        first = spin_session()
        idx_after_first = monitor._spin_session_idx
        monitor._spin_session_last = 0.0
        spin_session()
        idx_after_second = monitor._spin_session_idx
        self.assertGreater(idx_after_second, idx_after_first)


# ---------------------------------------------------------------------------
# spin_rls
# ---------------------------------------------------------------------------
class TestSpinRls(unittest.TestCase):

    def setUp(self):
        import monitor
        self._orig_idx = monitor._spin_rls_idx
        self._orig_last = monitor._spin_rls_last

    def tearDown(self):
        import monitor
        monitor._spin_rls_idx = self._orig_idx
        monitor._spin_rls_last = self._orig_last

    def test_returns_valid_char(self):
        result = spin_rls()
        self.assertIn(result, _SPIN_RLS)


# ---------------------------------------------------------------------------
# _git_cmd
# ---------------------------------------------------------------------------
class TestRenderUpdateModal(unittest.TestCase):

    def setUp(self):
        import monitor
        self._orig_cache = monitor._rls_cache.copy()
        self._orig_result = monitor._update_result
        self._orig_env = os.environ.get("CC_AIO_MON_NO_UPDATE_CHECK")
        os.environ["CC_AIO_MON_NO_UPDATE_CHECK"] = "1"

    def tearDown(self):
        import monitor
        monitor._rls_cache.update(self._orig_cache)
        monitor._update_result = self._orig_result
        if self._orig_env is None:
            os.environ.pop("CC_AIO_MON_NO_UPDATE_CHECK", None)
        else:
            os.environ["CC_AIO_MON_NO_UPDATE_CHECK"] = self._orig_env

    def test_up_to_date(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "ok", "remote_ver": VERSION})
        buf = render_update_modal(80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Up to date", plain)

    def test_checked_timestamp_shown(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic() - 125, "status": "ok", "remote_ver": VERSION})
        buf = render_update_modal(80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Checked", plain)
        self.assertRegex(plain, r"Checked \d+m ago")

    def test_repo_url_present(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "ok", "remote_ver": VERSION})
        buf = render_update_modal(80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("github.com/iM3SK/cc-aio-mon", plain)

    def test_update_available(self):
        import monitor
        remote_ver = "9.9.9"
        monitor._rls_cache.update({"t": time.monotonic(), "status": "update", "remote_ver": remote_ver})
        monitor._update_result = None
        with patch("monitor._get_new_commits", return_value=["abc new feature"]):
            with patch("monitor._update_checks", return_value=[]):
                with patch("monitor._get_remote_changelog_preview", return_value=[]):
                    buf = render_update_modal(80, 40)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn(remote_ver, plain)

    def test_checking_state(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": None, "remote_ver": None})
        buf = render_update_modal(80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Checking", plain)

    def test_no_git_state(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "no_git", "remote_ver": None})
        buf = render_update_modal(80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Git is not installed", plain)

    def test_timeout_state(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "timeout", "remote_ver": None})
        buf = render_update_modal(80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("timeout", plain.lower())

    def test_unknown_error_state(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "error", "remote_ver": None})
        buf = render_update_modal(80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Unknown error", plain)


import pathlib
import json


# ---------------------------------------------------------------------------
# TestCpcBase — statusline.cpc_base
# ---------------------------------------------------------------------------
class TestListSessions(unittest.TestCase):

    def setUp(self):
        import monitor
        self._orig = monitor.DATA_DIR
        self._tmp = tempfile.mkdtemp()
        monitor.DATA_DIR = pathlib.Path(self._tmp)

    def tearDown(self):
        import shutil, monitor
        monitor.DATA_DIR = self._orig
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_empty_dir_returns_empty_list(self):
        result = list_sessions()
        self.assertEqual(result, [])

    def test_valid_session_found(self):
        sid = "validSession123"
        p = pathlib.Path(self._tmp) / f"{sid}.json"
        p.write_text(json.dumps({"model": {"display_name": "Opus"}, "session_name": "", "cwd": ""}), encoding="utf-8")
        result = list_sessions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], sid)

    def test_rls_json_skipped(self):
        p = pathlib.Path(self._tmp) / "rls.json"
        p.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
        result = list_sessions()
        self.assertEqual(result, [])

    def test_stats_json_skipped(self):
        p = pathlib.Path(self._tmp) / "stats.json"
        p.write_text(json.dumps({"data": 1}), encoding="utf-8")
        result = list_sessions()
        self.assertEqual(result, [])

    def test_invalid_sid_skipped(self):
        # Dots in stem make SID invalid per _SID_RE
        p = pathlib.Path(self._tmp) / "..evil.json"
        p.write_text(json.dumps({"model": {}}), encoding="utf-8")
        result = list_sessions()
        self.assertEqual(result, [])

    def test_oversized_file_skipped(self):
        import monitor
        sid = "bigSession"
        p = pathlib.Path(self._tmp) / f"{sid}.json"
        # Write content slightly over MAX_FILE_SIZE
        p.write_bytes(b"x" * (MAX_FILE_SIZE + 1))
        result = list_sessions()
        self.assertEqual(result, [])

    def test_stale_tmp_files_cleaned_up(self):
        tmp_file = pathlib.Path(self._tmp) / "orphan.tmp"
        tmp_file.write_text("garbage", encoding="utf-8")
        # Force mtime to be old (> 60s ago)
        old_time = time.time() - 120
        os.utime(str(tmp_file), (old_time, old_time))
        list_sessions()
        self.assertFalse(tmp_file.exists())

    def test_recent_tmp_files_not_cleaned_up(self):
        tmp_file = pathlib.Path(self._tmp) / "recent.tmp"
        tmp_file.write_text("garbage", encoding="utf-8")
        # mtime is now — should NOT be cleaned
        list_sessions()
        self.assertTrue(tmp_file.exists())

    def test_dead_session_purged_after_48h(self):
        import monitor
        sid = "oldSession"
        p = pathlib.Path(self._tmp) / f"{sid}.json"
        h = pathlib.Path(self._tmp) / f"{sid}.jsonl"
        p.write_text('{"model": {}}', encoding="utf-8")
        h.write_text('{"t": 1}\n', encoding="utf-8")
        # Set mtime to 49h ago
        old_time = time.time() - 176400
        os.utime(p, (old_time, old_time))
        list_sessions()
        self.assertFalse(p.exists())
        self.assertFalse(h.exists())

    def test_recent_session_not_purged(self):
        import monitor
        sid = "recentSession"
        p = pathlib.Path(self._tmp) / f"{sid}.json"
        p.write_text('{"model": {"display_name": "Opus"}, "session_name": "", "cwd": ""}', encoding="utf-8")
        result = list_sessions()
        self.assertTrue(p.exists())
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# TestLoadState — monitor.load_state
# ---------------------------------------------------------------------------
class TestLoadState(unittest.TestCase):

    def setUp(self):
        import monitor
        self._orig = monitor.DATA_DIR
        self._tmp = tempfile.mkdtemp()
        monitor.DATA_DIR = pathlib.Path(self._tmp)

    def tearDown(self):
        import shutil, monitor
        monitor.DATA_DIR = self._orig
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_valid_json_returns_dict(self):
        sid = "sess001"
        p = pathlib.Path(self._tmp) / f"{sid}.json"
        payload = {"session_id": sid, "cost": {"total_cost_usd": 1.5}}
        p.write_text(json.dumps(payload), encoding="utf-8")
        result = load_state(sid)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["session_id"], sid)

    def test_invalid_sid_returns_none(self):
        result = load_state("../evil")
        self.assertIsNone(result)

    def test_missing_file_returns_none(self):
        result = load_state("nonExistentSid")
        self.assertIsNone(result)

    def test_oversized_file_returns_none(self):
        sid = "bigSess"
        p = pathlib.Path(self._tmp) / f"{sid}.json"
        p.write_bytes(b"{}" + b" " * MAX_FILE_SIZE)
        result = load_state(sid)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestLoadHistory — monitor.load_history
# ---------------------------------------------------------------------------
class TestLoadHistory(unittest.TestCase):

    def setUp(self):
        import monitor
        self._orig = monitor.DATA_DIR
        self._tmp = tempfile.mkdtemp()
        monitor.DATA_DIR = pathlib.Path(self._tmp)

    def tearDown(self):
        import shutil, monitor
        monitor.DATA_DIR = self._orig
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_valid_jsonl_returns_list(self):
        sid = "histSess"
        p = pathlib.Path(self._tmp) / f"{sid}.jsonl"
        lines = [json.dumps({"t": 1_600_000_000 + i, "v": i}) for i in range(5)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = load_history(sid)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["v"], 0)

    def test_invalid_sid_returns_empty(self):
        result = load_history("../../etc/passwd")
        self.assertEqual(result, [])

    def test_missing_file_returns_empty(self):
        result = load_history("noSuchSession")
        self.assertEqual(result, [])

    def test_corrupt_json_lines_skipped(self):
        sid = "partialSess"
        p = pathlib.Path(self._tmp) / f"{sid}.jsonl"
        lines = [
            "not valid json",
            json.dumps({"t": 1_600_000_000, "ok": True}),
            "{broken",
            json.dumps({"t": 1_600_000_060, "ok": True}),
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = load_history(sid)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r.get("ok") for r in result))


# ---------------------------------------------------------------------------
# TestRenderPicker — monitor.render_picker
# ---------------------------------------------------------------------------
class TestRenderPicker(unittest.TestCase):

    def test_empty_sessions_shows_waiting_message(self):
        buf = render_picker([], 80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Waiting", plain)

    def test_non_empty_sessions_shows_session_list(self):
        sessions = [
            {
                "id": "sess001",
                "session_name": "MyProject",
                "model": "Opus 4",
                "cwd": "/home/user",
                "stale": False,
            }
        ]
        buf = render_picker(sessions, 80, 24)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("MyProject", plain)

    def test_returns_buffer_of_correct_length(self):
        buf = render_picker([], 80, 24)
        self.assertEqual(len(buf), 24)

    def test_stale_session_shows_stale_label(self):
        sessions = [
            {
                "id": "staleSess",
                "session_name": "",
                "model": "Haiku",
                "cwd": "/tmp",
                "stale": True,
            }
        ]
        buf = render_picker(sessions, 80, 30)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("stale", plain)

    def test_live_session_shows_live_label(self):
        sessions = [
            {
                "id": "liveSess",
                "session_name": "Active",
                "model": "Sonnet",
                "cwd": "/workspace",
                "stale": False,
            }
        ]
        buf = render_picker(sessions, 80, 30)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("live", plain)


# ---------------------------------------------------------------------------
# TestCollectWarningsCTFMin — CTF <1m fix (never shows <0m)
# ---------------------------------------------------------------------------
class TestCollectWarningsCTFMin(unittest.TestCase):

    def test_ctf_shows_1m_minimum(self):
        # xpm so large that eta_mins rounds to 0 — must clamp to 1
        data = {"context_window": {"used_percentage": 99.99}}
        warns = collect_warnings(data, None, 50.0)
        self.assertTrue(any("CTF" in w for w in warns))
        ctf = [w for w in warns if "CTF" in w][0]
        self.assertIn("<1m", ctf)
        self.assertNotIn("<0m", ctf)

    def test_ctf_shows_actual_minutes_when_above_1(self):
        # 100 - 70 = 30% remaining; xpm = 5 → eta = 6 min
        data = {"context_window": {"used_percentage": 70}}
        warns = collect_warnings(data, None, 5.0)
        ctf = [w for w in warns if "CTF" in w]
        self.assertTrue(ctf)
        self.assertIn("<6m", ctf[0])


# ---------------------------------------------------------------------------
# TestSanitizeBidi — bidi control character stripping
# ---------------------------------------------------------------------------
class TestFormatterEdgeCases(unittest.TestCase):

    def test_f_cost_negative(self):
        self.assertEqual(f_cost(-1.0), "--")

    def test_f_tok_99999_is_one_decimal_k(self):
        # 99999 < 100_000, so uses 1-decimal format: "100.0k"
        self.assertEqual(f_tok(99999), "100.0k")

    def test_f_tok_100k_boundary(self):
        # 100000 >= 100_000, so uses 0-decimal format: "100k"
        self.assertEqual(f_tok(100000), "100k")

    def test_f_tok_very_large(self):
        self.assertEqual(f_tok(1_000_000_000), "1000M")

    def test_f_dur_negative(self):
        self.assertEqual(f_dur(-1000), "--")

    def test_f_dur_zero(self):
        self.assertEqual(f_dur(0), "--")

    def test_f_cost_very_small_positive(self):
        result = f_cost(0.0001)
        self.assertIn("$", result)
        self.assertIn("0.0001", result)


# ---------------------------------------------------------------------------
# TestReservedFiles — RESERVED_SIDS contains expected sentinel names
# ---------------------------------------------------------------------------
class TestFlush(unittest.TestCase):

    def test_contains_sync_markers(self):
        from io import StringIO
        buf = ["line1", "line2"]
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            flush(buf, cols=80)
            output = mock_out.getvalue()
        self.assertIn(SYNC_ON, output)
        self.assertIn(SYNC_OFF, output)

    def test_correct_line_count(self):
        from io import StringIO
        buf = ["a", "b", "c"]
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            flush(buf, cols=80)
            output = mock_out.getvalue()
        # Should have exactly len(buf)-1 newlines between lines
        newline_count = output.count("\n")
        self.assertEqual(newline_count, len(buf) - 1)

    def test_empty_buf(self):
        from io import StringIO
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            flush([], cols=80)
            output = mock_out.getvalue()
        self.assertIn(SYNC_ON, output)
        self.assertIn(SYNC_OFF, output)


# ---------------------------------------------------------------------------
# TestModelCode — _model_code()
# ---------------------------------------------------------------------------
class TestModelCode(unittest.TestCase):

    def test_known_opus_47(self):
        self.assertEqual(_model_code("claude-opus-4-7"), ("OP", "4.7"))

    def test_known_opus(self):
        self.assertEqual(_model_code("claude-opus-4-6"), ("OP", "4.6"))

    def test_known_haiku(self):
        self.assertEqual(_model_code("claude-haiku-4-5-20251001"), ("HA", "4.5"))

    def test_known_sonnet(self):
        self.assertEqual(_model_code("claude-sonnet-4-6"), ("SO", "4.6"))

    def test_short_opus(self):
        self.assertEqual(_model_code("opus"), ("OP", ""))

    def test_unknown_foo_bar(self):
        # base[:3].upper() → "FOO", version → ""
        self.assertEqual(_model_code("foo-bar"), ("FOO", ""))

    def test_with_1m_suffix(self):
        # "[1m]" stripped before lookup
        self.assertEqual(_model_code("claude-opus-4-6[1m]"), ("OP", "4.6"))

    def test_dynamic_regex_fallback(self):
        self.assertEqual(_model_code("claude-opus-99-9"), ("OP", "99.9"))

    def test_dynamic_regex_sonnet(self):
        self.assertEqual(_model_code("claude-sonnet-5-0"), ("SO", "5.0"))

    def test_dynamic_regex_haiku(self):
        self.assertEqual(_model_code("claude-haiku-5-1"), ("HA", "5.1"))


# ---------------------------------------------------------------------------
# TestCostThirds — _cost_thirds()
# ---------------------------------------------------------------------------
class TestCostThirds(unittest.TestCase):

    def _entry(self, t, cost):
        return {"t": t, "cost": {"total_cost_usd": cost}}

    def test_empty_history(self):
        self.assertEqual(_cost_thirds([]), [])

    def test_one_entry(self):
        self.assertEqual(_cost_thirds([self._entry(1_600_000_000, 0.1)]), [])

    def test_span_under_30s(self):
        hist = [self._entry(1_600_000_000, 0.0), self._entry(1_600_000_020, 1.0)]
        self.assertEqual(_cost_thirds(hist), [])

    def test_valid_history_returns_3_tuples(self):
        now = 1_600_000_000
        hist = [
            self._entry(now, 0.0),
            self._entry(now + 20, 0.3),
            self._entry(now + 40, 0.6),
            self._entry(now + 60, 1.0),
        ]
        result = _cost_thirds(hist)
        self.assertEqual(len(result), 3)

    def test_labels_are_early_mid_late(self):
        now = 1_600_000_000
        hist = [
            self._entry(now, 0.0),
            self._entry(now + 20, 0.3),
            self._entry(now + 40, 0.6),
            self._entry(now + 60, 1.0),
        ]
        result = _cost_thirds(hist)
        labels = [r[0] for r in result]
        self.assertEqual(labels, ["early", "mid", "late"])

    def test_costs_non_negative(self):
        now = 1_600_000_000
        hist = [
            self._entry(now, 0.0),
            self._entry(now + 20, 0.5),
            self._entry(now + 40, 0.8),
            self._entry(now + 60, 1.2),
        ]
        result = _cost_thirds(hist)
        for label, cost, rate in result:
            self.assertGreaterEqual(cost, 0.0)

    def test_rate_equals_cost_over_third_minutes(self):
        now = 1_600_000_000
        # 3 equal thirds of 60s each (total span = 180s, third = 60s = 1min)
        hist = [
            self._entry(now, 0.0),
            self._entry(now + 60, 0.6),
            self._entry(now + 120, 1.2),
            self._entry(now + 180, 1.8),
        ]
        result = _cost_thirds(hist)
        for label, cost, rate in result:
            third_minutes = 60 / 60  # 60s / 60 = 1 min
            expected_rate = cost / third_minutes if cost > 0 else 0.0
            self.assertAlmostEqual(rate, expected_rate, places=5)


# ---------------------------------------------------------------------------
# TestGetPricing — _get_pricing()
# ---------------------------------------------------------------------------
class TestGetPricing(unittest.TestCase):

    def test_known_opus(self):
        p = _get_pricing("claude-opus-4-6")
        self.assertIn("input", p)
        self.assertIn("output", p)
        self.assertIn("cache_read", p)
        self.assertIn("cache_write", p)
        self.assertEqual(p["input"], 5.0)
        self.assertEqual(p["output"], 25.0)

    def test_known_opus_47(self):
        p = _get_pricing("claude-opus-4-7")
        self.assertEqual(p["input"], 5.0)
        self.assertEqual(p["output"], 25.0)

    def test_with_1m_suffix(self):
        p_base = _get_pricing("claude-opus-4-6")
        p_suffix = _get_pricing("claude-opus-4-6[1m]")
        self.assertEqual(p_base, p_suffix)

    def test_unknown_model_returns_default(self):
        p = _get_pricing("claude-future-99")
        self.assertEqual(p, _DEFAULT_PRICING)

    def test_empty_string_returns_default(self):
        p = _get_pricing("")
        self.assertEqual(p, _DEFAULT_PRICING)


# ---------------------------------------------------------------------------
# TestCharWidth — shared.char_width()
# ---------------------------------------------------------------------------
class TestSessionAutoConnect(unittest.TestCase):
    """Test the auto-connect condition: connect only when exactly 1 session exists."""

    def _auto_connect(self, sessions):
        """Replicate the condition used in main(): connect iff len(sessions)==1."""
        active = [s for s in sessions if not s["stale"]]
        return len(active) == 1 and len(sessions) == 1

    def test_one_active_zero_stale_auto_connects(self):
        sessions = [{"id": "s1", "stale": False}]
        self.assertTrue(self._auto_connect(sessions))

    def test_one_active_one_stale_no_auto_connect(self):
        sessions = [{"id": "s1", "stale": False}, {"id": "s2", "stale": True}]
        self.assertFalse(self._auto_connect(sessions))

    def test_zero_sessions_no_auto_connect(self):
        self.assertFalse(self._auto_connect([]))

    def test_two_active_no_auto_connect(self):
        sessions = [{"id": "s1", "stale": False}, {"id": "s2", "stale": False}]
        self.assertFalse(self._auto_connect(sessions))


# ---------------------------------------------------------------------------
# TestRenderPickerLimit — render_picker with >9 sessions
# ---------------------------------------------------------------------------
class TestRenderPickerLimit(unittest.TestCase):

    def _make_sessions(self, n):
        return [
            {
                "id": f"sess{i:03d}",
                "session_name": f"Session {i}",
                "model": "Sonnet",
                "cwd": "/tmp",
                "stale": False,
            }
            for i in range(1, n + 1)
        ]

    def test_only_9_shown(self):
        sessions = self._make_sessions(15)
        buf = render_picker(sessions, 80, 40)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        for i in range(1, 10):
            self.assertIn(f"[{i}]", plain)
        self.assertNotIn("[10]", plain)

    def test_more_indicator_shown(self):
        sessions = self._make_sessions(15)
        buf = render_picker(sessions, 80, 40)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("+6 more", plain)


# ---------------------------------------------------------------------------
# Pulse — scoring + extraction
# ---------------------------------------------------------------------------
import pulse
from monitor import render_pulse_modal


class TestListSessionsPurgesOrphan(unittest.TestCase):

    def setUp(self):
        import monitor
        self._orig = monitor.DATA_DIR
        self._tmp = tempfile.mkdtemp()
        monitor.DATA_DIR = pathlib.Path(self._tmp)

    def tearDown(self):
        import shutil, monitor
        monitor.DATA_DIR = self._orig
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_purges_orphan_without_display_name_after_1h(self):
        import monitor
        sid = "orphanSession1"
        p = pathlib.Path(self._tmp) / f"{sid}.json"
        h = pathlib.Path(self._tmp) / f"{sid}.jsonl"
        p.write_text('{"model": {}}', encoding="utf-8")
        h.write_text('{"t": 1}\n', encoding="utf-8")
        old_time = time.time() - 3700
        os.utime(str(p), (old_time, old_time))
        result = list_sessions()
        self.assertFalse(p.exists())
        self.assertFalse(h.exists())
        self.assertNotIn(sid, [s["id"] for s in result])


# ---------------------------------------------------------------------------
# TestScanTranscriptCacheOnly — cache_read with no input/output tokens
# ---------------------------------------------------------------------------
class TestScanTranscriptCacheOnly(unittest.TestCase):

    def setUp(self):
        import monitor
        self._tmpdir = tempfile.mkdtemp()
        self._orig = monitor._CLAUDE_DIR
        self._orig_cache = monitor._usage_cache.copy()
        monitor._CLAUDE_DIR = pathlib.Path(self._tmpdir)
        monitor._usage_cache.clear()

    def tearDown(self):
        import shutil, monitor
        monitor._CLAUDE_DIR = self._orig
        monitor._usage_cache.clear()
        monitor._usage_cache.update(self._orig_cache)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_cache_read_only_no_input_no_output(self):
        import json as _json
        record = {
            "type": "assistant",
            "timestamp": "2026-04-17T10:00:00Z",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"cache_read_input_tokens": 500},
            },
        }
        _write_session(self._tmpdir, "proj1", "sess_cache_only", [_json.dumps(record)])
        models, _ = scan_transcript_stats("all", ttl=0)
        agg = models.get("claude-sonnet-4-6", {})
        self.assertEqual(agg.get("cache_read"), 500)
        self.assertEqual(agg.get("input"), 0)
        self.assertEqual(agg.get("output"), 0)
        self.assertEqual(agg.get("calls"), 1)


# ---------------------------------------------------------------------------
# Security: transcript_path containment (FIX 1)
# ---------------------------------------------------------------------------
class TestAggregateSessionCostSecurity(unittest.TestCase):

    def setUp(self):
        _SESSION_COST_CACHE.clear()

    def test_transcript_path_traversal_rejected(self):
        import monitor as _monitor
        # Path outside ~/.claude/projects/ — must return None
        with patch.object(_monitor, "CLAUDE_PROJECTS_DIR",
                          pathlib.Path("/nonexistent/projects").resolve()):
            data = {"session_id": "abcd1234", "transcript_path": "/etc/passwd"}
            result = _aggregate_session_cost(data)
        self.assertIsNone(result)

    def test_transcript_path_symlink_rejected(self):
        import tempfile, shutil
        import monitor as _monitor
        with tempfile.TemporaryDirectory() as td:
            real = pathlib.Path(td) / "real.jsonl"
            real.write_text(
                '{"type":"assistant","message":{"model":"x","usage":{"input_tokens":1}}}\n'
            )
            link = pathlib.Path(td) / "link.jsonl"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            fake_projects = pathlib.Path(td).resolve()
            with patch.object(_monitor, "CLAUDE_PROJECTS_DIR", fake_projects):
                _SESSION_COST_CACHE.clear()
                data = {"session_id": "abcd1234", "transcript_path": str(link)}
                result = _aggregate_session_cost(data)
        self.assertIsNone(result)

    def test_claude_projects_root_symlink_rejected(self):
        import monitor as _monitor
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            real_root = base / "real-projects"
            real_root.mkdir()
            proj = real_root / "proj"
            proj.mkdir()
            sid = "rootsymlinksid"
            jl = proj / f"{sid}.jsonl"
            jl.write_text(
                '{"type":"assistant","message":{"model":"x","usage":{"input_tokens":1}}}\n',
                encoding="utf-8",
            )
            link_root = base / "projects-link"
            try:
                link_root.symlink_to(real_root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            with patch.object(_monitor, "CLAUDE_PROJECTS_DIR", link_root):
                data = {"session_id": sid, "transcript_path": str(jl)}
                result = _aggregate_session_cost(data)
        self.assertIsNone(result)

    def test_transcript_path_non_string_rejected(self):
        import monitor as _monitor
        for bad in (None, 42, ["/tmp/x"], {"path": "/tmp/x"}):
            _SESSION_COST_CACHE.clear()
            data = {"session_id": "abcd1234", "transcript_path": bad}
            result = _aggregate_session_cost(data)
            self.assertIsNone(result, f"expected None for transcript_path={bad!r}")


# ---------------------------------------------------------------------------
# Security: _model_code sanitizes unknown input (FIX 2)
# ---------------------------------------------------------------------------
class TestModelCodeSanitization(unittest.TestCase):

    def test_unknown_model_control_chars_stripped(self):
        code, ver = _model_code("claude-\x1b[31mhack\x1b[0m")
        self.assertNotIn("\x1b", code)
        self.assertNotIn("\x1b", ver)

    def test_unknown_model_known_fallback_unchanged(self):
        code, ver = _model_code("claude-opus-4-7")
        self.assertEqual((code, ver), ("OP", "4.7"))


# ---------------------------------------------------------------------------
# Security: run_git env whitelist (FIX 3)
# ---------------------------------------------------------------------------
class TestPrePushHook(unittest.TestCase):

    @staticmethod
    def _hook_path():
        return pathlib.Path(__file__).resolve().parent.parent / ".githooks" / "pre-push"

    def test_new_branch_scans_tip_tree(self):
        hook = self._hook_path()
        if not hook.exists():
            self.skipTest("pre-push hook missing")
        src = hook.read_text(encoding="utf-8")
        self.assertIn('git diff-tree --no-commit-id --name-only -r "$local_sha"', src)
        self.assertIn('git diff-tree --no-commit-id -p -r "$local_sha"', src)
        self.assertNotIn('range="$local_sha"  # new branch', src)


# ---------------------------------------------------------------------------
# Regression: _SID_RE rejects Windows reserved device names (SEC-002 v1.9.1)
# ---------------------------------------------------------------------------
class TestRlsCheckWorkerUsesRunGit(unittest.TestCase):
    """Ensures the RLS background worker goes through monitor.run_git
    (shared env whitelist) and does NOT fall back to inline subprocess.run."""

    def test_worker_invokes_run_git_and_not_subprocess(self):
        """SEC-001 + SEC-010: worker uses run_git exclusively, never raw subprocess.run."""
        from unittest.mock import patch as _patch, MagicMock
        import monitor as _m
        mock_result = MagicMock(returncode=0, stdout='VERSION = "99.0.0"', stderr="")
        try:
            if _m._rls_lock.locked():
                _m._rls_lock.release()
        except RuntimeError:
            pass
        _m._rls_lock.acquire(blocking=False)
        with _patch("monitor.run_git", return_value=mock_result) as mock_rg, \
             _patch("subprocess.run") as mock_sp:
            _m._rls_check_worker()
        # Exactly 2 run_git calls: fetch + show
        self.assertEqual(mock_rg.call_count, 2, "expected 2 run_git calls (fetch + show)")
        self.assertEqual(mock_rg.call_args_list[0].args[0][0], "fetch")
        self.assertEqual(mock_rg.call_args_list[1].args[0][0], "show")
        # Worker must NOT bypass run_git to call subprocess.run directly
        mock_sp.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: _SESSION_COST_CACHE LRU eviction (DEBT-017 v1.9.1)
# ---------------------------------------------------------------------------
class TestSessionCostCacheEviction(unittest.TestCase):
    """Verifies that the OrderedDict cache actually evicts oldest entries beyond cap.
    Simulates the insert pattern used by _aggregate_session_cost without filesystem I/O."""

    def setUp(self):
        _SESSION_COST_CACHE.clear()

    def tearDown(self):
        _SESSION_COST_CACHE.clear()

    def test_cache_evicts_oldest_beyond_cap(self):
        # Insert MAX + 5 distinct sids, simulating the eviction pattern in _aggregate_session_cost
        total = _SESSION_COST_CACHE_MAX + 5
        for i in range(total):
            sid = f"sid{i:04d}"
            _SESSION_COST_CACHE[sid] = (float(i), {"cost_total": float(i)})
            _SESSION_COST_CACHE.move_to_end(sid)
            while len(_SESSION_COST_CACHE) > _SESSION_COST_CACHE_MAX:
                _SESSION_COST_CACHE.popitem(last=False)
        self.assertEqual(len(_SESSION_COST_CACHE), _SESSION_COST_CACHE_MAX)
        # Oldest 5 evicted
        for i in range(5):
            self.assertNotIn(f"sid{i:04d}", _SESSION_COST_CACHE,
                             f"sid{i:04d} should have been evicted")
        # Newest MAX retained
        for i in range(5, total):
            self.assertIn(f"sid{i:04d}", _SESSION_COST_CACHE)

    def test_cache_move_to_end_on_hit_preserves_recent(self):
        # Populate cache to cap
        for i in range(_SESSION_COST_CACHE_MAX):
            _SESSION_COST_CACHE[f"sid{i:04d}"] = (float(i), {})
        # Touch oldest key (simulating cache hit → move_to_end)
        _SESSION_COST_CACHE.move_to_end("sid0000")
        # Insert new sid; eviction should now remove sid0001, not sid0000
        new_sid = "sidNEW"
        _SESSION_COST_CACHE[new_sid] = (999.0, {})
        _SESSION_COST_CACHE.move_to_end(new_sid)
        while len(_SESSION_COST_CACHE) > _SESSION_COST_CACHE_MAX:
            _SESSION_COST_CACHE.popitem(last=False)
        self.assertIn("sid0000", _SESSION_COST_CACHE, "recently touched sid should survive")
        self.assertNotIn("sid0001", _SESSION_COST_CACHE, "untouched oldest should be evicted")
        self.assertIn(new_sid, _SESSION_COST_CACHE)


# ---------------------------------------------------------------------------
# Regression: SIGPIPE handler installed on Unix (PERF-001 v1.9.1)
# ---------------------------------------------------------------------------
class TestSigpipeHandler(unittest.TestCase):
    """Verifies statusline.main() installs SIGPIPE=SIG_DFL on non-Windows."""

    def test_sigpipe_installed_on_unix(self):
        import signal as _signal
        if not hasattr(_signal, "SIGPIPE"):
            self.skipTest("SIGPIPE not available on this platform (Windows)")
        import statusline
        with patch("sys.stdin") as mock_stdin, \
             patch.object(_signal, "signal") as mock_sig:
            mock_stdin.read.return_value = ""  # empty → statusline.main() returns early
            statusline.main()
        # Among all signal.signal calls, at least one must be SIGPIPE -> SIG_DFL
        calls = [(c.args[0], c.args[1]) for c in mock_sig.call_args_list if len(c.args) >= 2]
        self.assertIn((_signal.SIGPIPE, _signal.SIG_DFL), calls,
                      f"SIGPIPE handler missing; got {calls}")


# ---------------------------------------------------------------------------
# Regression: _rls_cache snapshot/write helpers are thread-safe (DEBT-020 v1.9.1)
# ---------------------------------------------------------------------------
class TestRlsCacheHelpers(unittest.TestCase):
    """Verifies _rls_snapshot returns a coherent copy and _rls_write updates all 3 fields."""

    def test_snapshot_returns_copy(self):
        import monitor as _m
        snap1 = _m._rls_snapshot()
        snap1["status"] = "MUTATED"  # mutate local copy
        snap2 = _m._rls_snapshot()
        self.assertNotEqual(snap2["status"], "MUTATED", "_rls_snapshot must return a copy")

    def test_write_sets_all_three_fields(self):
        import monitor as _m
        orig = _m._rls_snapshot()
        try:
            _m._rls_write("update", remote_ver="9.9.9")
            after = _m._rls_snapshot()
            self.assertEqual(after["status"], "update")
            self.assertEqual(after["remote_ver"], "9.9.9")
            self.assertIsNotNone(after["t"])
        finally:
            # restore
            _m._rls_write(orig["status"], remote_ver=orig.get("remote_ver"))


# ---------------------------------------------------------------------------
# v1.10.1 audit regression tests (H-1, M-1, M-5)
# ---------------------------------------------------------------------------
class TestScanTranscriptStatsSafeDir(unittest.TestCase):
    """M-1: scan_transcript_stats must use is_safe_dir (not bare .is_dir) on _CLAUDE_DIR.

    Guards against symlink/junction on ~/.claude/projects pointing to attacker-controlled
    directory being scanned for JSONL files.
    """

    def test_unsafe_dir_returns_empty(self):
        import monitor as _m
        with patch.object(_m, "is_safe_dir", return_value=False):
            models, overview = _m.scan_transcript_stats(period="all")
        self.assertEqual(models, {})
        self.assertEqual(overview["sessions"], 0)

    def test_safe_dir_check_called(self):
        import monitor as _m
        with patch.object(_m, "is_safe_dir", return_value=False) as mock_safe:
            _m.scan_transcript_stats(period="all")
        mock_safe.assert_called()


class TestInvalidSessionIdSanitized(unittest.TestCase):
    """L-2: CLI --session error path must sanitize before echoing to terminal."""

    def test_source_wraps_sid_in_sanitize(self):
        # Running main() with evil stdin requires full TTY mock; simpler to pin
        # the shape of the fix: the 'Invalid session ID' print site must call
        # _sanitize(sid) before interpolating.
        import monitor as _m
        src = pathlib.Path(_m.__file__).read_text(encoding="utf-8")
        # Find the print line and check it sanitizes
        idx = src.find("Invalid session ID:")
        self.assertGreater(idx, -1, "Invalid session ID print site missing")
        snippet = src[idx:idx + 120]
        self.assertIn("_sanitize(sid)", snippet,
                      "'Invalid session ID' print must pass sid through _sanitize")


class TestMainStartupNoUnboundLocal(unittest.TestCase):
    """v1.10.3 — monitor.main() crashed on Windows with UnboundLocalError.

    Root cause: the M-6 SIGPIPE fix had `import signal` inside main() inside
    an `if sys.platform != "win32":` block. Python's scope rule makes `signal`
    a function-local for the entire main(), so `signal.signal(SIGTERM, ...)`
    later in the same function failed on Windows where the `import` branch
    never executed.

    This test runs main() via --list flag (early return, no TUI needed) and
    asserts no UnboundLocalError propagates — the early SIGTERM wiring still
    runs before --list check only after the fix reorder (actually: SIGTERM
    wiring happens AFTER --list returns, so --list doesn't trip the bug).

    Plus source-level guard: no local `import signal` anywhere in main().
    """

    def test_no_inline_signal_import_in_main(self):
        import monitor as _m
        src = pathlib.Path(_m.__file__).read_text(encoding="utf-8")
        # Find main() definition
        idx = src.find("def main():")
        self.assertGreater(idx, -1, "main() function missing")
        # Extract until the next top-level 'def' (or EOF)
        tail = src[idx:]
        next_def = tail.find("\ndef ", 1)
        main_body = tail if next_def == -1 else tail[:next_def]
        # Strip comments — we're guarding against CODE not prose
        code_lines = []
        for line in main_body.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # whole-line comment
            # Strip inline comments (naive but sufficient — no '#' in our strings)
            if "#" in line:
                line = line.split("#", 1)[0].rstrip()
            code_lines.append(line)
        code_only = "\n".join(code_lines)
        self.assertNotIn(
            "import signal",
            code_only,
            "main() must NOT contain a local `import signal` — that shadows "
            "the module-level import and breaks signal.SIGTERM on Windows "
            "(v1.10.3 regression). Use module-level import only."
        )

    def test_main_list_mode_runs_without_unbound_local(self):
        import monitor as _m
        # --list exits early after listing sessions; it runs past the SIGPIPE
        # block that used to contain the bad import. Enough to exercise the
        # scope rule on Windows.
        with patch.object(sys, "argv", ["monitor.py", "--list"]):
            with patch("builtins.print"):
                try:
                    _m.main()
                except UnboundLocalError as e:
                    self.fail(f"UnboundLocalError in main() — scope bug regressed: {e}")
                except SystemExit:
                    pass  # normal for --list or TTY fallback


class TestCrashLoggerInstalled(unittest.TestCase):
    """v1.10.3 — sys.excepthook must write crash to $TMPDIR/claude-aio-monitor/monitor-crash.log.

    Alt-buffer wipes tracebacks on terminal; the crash log is the only
    post-mortem signal for bugs like the v1.10.3 scope regression.
    """

    def test_install_crash_logger_sets_excepthook(self):
        import monitor as _m
        original = sys.excepthook
        try:
            _m._install_crash_logger()
            self.assertIsNot(sys.excepthook, original,
                             "_install_crash_logger must replace sys.excepthook")
        finally:
            sys.excepthook = original

    def test_crash_logger_writes_log_file(self):
        import monitor as _m
        original = sys.excepthook
        log_path = _m.DATA_DIR / "monitor-crash.log"
        if log_path.exists():
            try:
                log_path.unlink()
            except OSError:
                pass
        try:
            _m._install_crash_logger()
            # Trigger via the hook directly
            try:
                raise RuntimeError("test-crash-for-regression")
            except RuntimeError:
                exc_type, exc_value, tb = sys.exc_info()
                # Avoid default handler printing to stderr
                with patch("sys.__excepthook__"):
                    sys.excepthook(exc_type, exc_value, tb)
            self.assertTrue(log_path.exists(), "crash log should be written")
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("test-crash-for-regression", content)
            self.assertIn("platform:", content)
            self.assertIn("encoding:", content)
        finally:
            sys.excepthook = original
            if log_path.exists():
                try:
                    log_path.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# v1.10.5 audit regression tests — SEC-008/009/010 + DEBT-014/018/022
# ---------------------------------------------------------------------------
class TestAuditRegressionV1105(unittest.TestCase):
    """Regression guards for the v1.10.5 audit fixes.

    Each test locks in a specific behavior introduced or restored by the audit
    so future refactors cannot silently undo the fix.
    """

    def test_debt014_signal_imported_at_module_level(self):
        """DEBT-014: statusline/update/monitor import `signal` at module level,
        not inside main() — the v1.10.3 Windows UnboundLocalError was caused
        by an in-function `import signal` shadowing the module-level name."""
        import statusline, update, monitor
        import signal as sig_mod
        # Module-level signal binding must be the real signal module on all three
        self.assertIs(statusline.signal, sig_mod)
        self.assertIs(update.signal, sig_mod)
        self.assertIs(monitor.signal, sig_mod)

    def test_debt014_shared_subprocess_module_level(self):
        """DEBT-014: shared.run_git uses module-level subprocess (not in-function import)."""
        import subprocess
        self.assertIs(shared.subprocess, subprocess)

    def test_debt014_monitor_bisect_traceback_module_level(self):
        """DEBT-014: monitor.py exposes bisect and traceback at module scope."""
        import bisect, traceback
        import monitor
        self.assertIs(monitor.bisect, bisect)
        self.assertIs(monitor.traceback, traceback)

    def test_debt016_monitor_loc_tripwire(self):
        """DEBT-016 / Audit P1-13: monitor.py is the single most likely
        place for the project to outgrow its "5 runtime files" constraint.
        The 24.05.2026 audit set 3500 LOC as the discussion trigger: at or
        above that line count, contributors must open an ADR for the
        monitor.py size strategy (pseudo-namespace classes vs relaxing the
        5-file constraint). See PROJECTS/cc-aio-mon/ROZHODNUTIA.md.

        This is a *trigger*, not a hard ceiling — a failing test here means
        "have the conversation", not "revert your work".
        """
        import monitor
        src = pathlib.Path(monitor.__file__).read_text(encoding="utf-8")
        loc = src.count("\n") + (0 if src.endswith("\n") else 1)
        # Trigger threshold per audit 24.05.2026
        self.assertLess(
            loc, 3500,
            f"monitor.py is {loc} LOC, past the 3500-line audit trigger. "
            f"Open an ADR (see PROJECTS/cc-aio-mon/ROZHODNUTIA.md) before "
            f"adding more, OR raise this trigger after the ADR is filed."
        )

    def test_debt015_monitor_load_history_delegates_to_shared(self):
        """DEBT-015: monitor.load_history is a thin wrapper over shared.load_history.

        Patch monitor's local reference (via `from shared import load_history
        as _shared_load_history`) — patching shared.load_history would not
        intercept the already-bound name inside monitor.
        """
        import monitor
        sentinel = object()
        with patch("monitor._shared_load_history", return_value=sentinel) as m:
            result = monitor.load_history("abc", 7)
            self.assertIs(result, sentinel)
            m.assert_called_once()
            args, kwargs = m.call_args
            self.assertEqual(args[0], "abc")
            self.assertEqual(args[1], 7)
            self.assertEqual(kwargs.get("data_dir"), monitor.DATA_DIR)

    def test_debt015_statusline_delegates_to_shared(self):
        """DEBT-015: statusline._load_history_for_rates is a thin wrapper too."""
        import statusline
        sentinel = object()
        with patch("statusline._shared_load_history", return_value=sentinel) as m:
            result = statusline._load_history_for_rates("xyz", 42)
            self.assertIs(result, sentinel)
            args, kwargs = m.call_args
            self.assertEqual(args[0], "xyz")
            self.assertEqual(args[1], 42)
            self.assertEqual(kwargs.get("data_dir"), statusline.DATA_DIR)

    def test_debt018_max_transcript_files_constant(self):
        """DEBT-018: monitor.MAX_TRANSCRIPT_FILES is the named constant
        (was magic literal 1000 inline before v1.10.5)."""
        import monitor
        self.assertTrue(hasattr(monitor, "MAX_TRANSCRIPT_FILES"))
        self.assertIsInstance(monitor.MAX_TRANSCRIPT_FILES, int)
        self.assertGreaterEqual(monitor.MAX_TRANSCRIPT_FILES, 100)  # sanity

    def test_debt021_models_dict_has_all_known_families(self):
        """DEBT-021: _MODELS dict consolidates name + code + pricing."""
        import monitor
        for known_id in ("claude-opus-4-7", "claude-sonnet-4-6",
                         "claude-haiku-4-5-20251001", "claude-opus-4-1"):
            self.assertIn(known_id, monitor._MODELS, f"missing {known_id}")
            entry = monitor._MODELS[known_id]
            self.assertIn("name", entry)
            self.assertIn("code", entry)
            self.assertIn("pricing", entry)
            # Each code is a 2-tuple (short, version)
            self.assertEqual(len(entry["code"]), 2)

    def test_debt022_model_code_from_label(self):
        """DEBT-022: render_picker uses _model_code_from_label, not an
        inline regex duplicate of _model_code's logic."""
        import monitor
        self.assertEqual(monitor._model_code_from_label("Opus 4.6"), ("OP", "4.6"))
        self.assertEqual(monitor._model_code_from_label("Sonnet 4.5 (1M context)"),
                         ("SO", "4.5"))
        self.assertEqual(monitor._model_code_from_label("Haiku 3.5"), ("HA", "3.5"))
        # Unknown label — fallback path, no crash
        code, ver = monitor._model_code_from_label("Unknown Model")
        self.assertEqual(ver, "")
        # Empty input — returns ("", "")
        self.assertEqual(monitor._model_code_from_label(""), ("", ""))
        self.assertEqual(monitor._model_code_from_label(None), ("", ""))

    def test_sec008_claude_dir_resolved_and_symlink_reject(self):
        """SEC-008: scan_transcript_stats resolves _CLAUDE_DIR and rejects
        symlinked transcript files (mirrors _safe_transcript_path hardening)."""
        import monitor
        # Build a fake _CLAUDE_DIR with one real transcript and one symlink to /etc/passwd-like.
        tmpdir = tempfile.mkdtemp()
        try:
            fake_claude = pathlib.Path(tmpdir) / "projects"
            fake_claude.mkdir()
            # Real transcript (1 assistant message with usage)
            (fake_claude / "real.jsonl").write_text(
                '{"type":"assistant","timestamp":"2026-04-19T10:00:00Z",'
                '"message":{"model":"claude-opus-4-6","usage":{"input_tokens":100}}}\n',
                encoding="utf-8",
            )
            # Symlinked transcript — point at outside-root file
            outside = pathlib.Path(tmpdir) / "outside.jsonl"
            outside.write_text(
                '{"type":"assistant","timestamp":"2026-04-19T10:00:00Z",'
                '"message":{"model":"claude-opus-4-1","usage":{"input_tokens":999999}}}\n',
                encoding="utf-8",
            )
            try:
                (fake_claude / "link.jsonl").symlink_to(outside)
                symlinks_supported = True
            except (OSError, NotImplementedError):
                symlinks_supported = False

            orig = monitor._CLAUDE_DIR
            monitor._CLAUDE_DIR = fake_claude
            monitor._usage_cache.clear()
            try:
                models, _ov = monitor.scan_transcript_stats(period="all", ttl=0.0)
                # The real transcript was scanned
                self.assertIn("claude-opus-4-6", models)
                # The symlink was NOT scanned (Opus 4.1 token count didn't leak in)
                if symlinks_supported:
                    self.assertNotIn("claude-opus-4-1", models)
            finally:
                monitor._CLAUDE_DIR = orig
                monitor._usage_cache.clear()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sec009_update_get_local_version_uses_safe_read(self):
        """SEC-009: get_local_version enforces a size cap via safe_read,
        returning a clear error on oversized shared.py rather than OOM."""
        import update
        # Monkey-patch REPO_ROOT to a tempdir with an oversized shared.py
        tmpdir = tempfile.mkdtemp()
        try:
            mpath = pathlib.Path(tmpdir) / "shared.py"
            # Write slightly over MAX_FILE_SIZE (1 MB) — safe_read rejects it
            mpath.write_bytes(b'VERSION = "9.9.9"\n' + b"x" * (shared.MAX_FILE_SIZE + 10))
            orig = update.REPO_ROOT
            update.REPO_ROOT = pathlib.Path(tmpdir)
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    update.get_local_version()
                # Error message mentions the size cap (not a generic read error)
                self.assertIn("too large", str(ctx.exception).lower())
            finally:
                update.REPO_ROOT = orig
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sec010_pulse_proxy_env_scrubbed(self):
        """SEC-010: pulse.py installs a proxy-scrubbed opener at import time
        so HTTP(S)_PROXY env vars do not silently route Anthropic Pulse
        fetches through an attacker-controlled intermediary.

        `build_opener(ProxyHandler({}))` creates an opener where no
        ProxyHandler remains in `.handlers` (CPython filters handlers that
        register no open-methods — an empty ProxyHandler has none). The
        invariant we lock in: after `import pulse`, `urllib.request._opener`
        is installed AND contains no ProxyHandler — so urlopen never consults
        HTTP(S)_PROXY env vars.
        """
        import pulse  # noqa: F401 — import triggers install_opener
        import urllib.request
        self.assertIsNotNone(
            urllib.request._opener,
            "urllib.request._opener should be set by pulse.py import",
        )
        self.assertFalse(
            any(isinstance(h, urllib.request.ProxyHandler)
                for h in urllib.request._opener.handlers),
            "pulse.py opener must contain NO ProxyHandler (env-proxy scrub)",
        )


class TestScanAiTitle(unittest.TestCase):
    """Coverage for _scan_ai_title — transcript JSONL ai-title extractor."""

    def setUp(self):
        import monitor as _monitor
        self._monitor = _monitor
        self._tmpdir = tempfile.mkdtemp()
        self._proj = pathlib.Path(self._tmpdir) / "proj"
        self._proj.mkdir()
        self._fake_root = pathlib.Path(self._tmpdir).resolve()
        _monitor._AI_TITLE_CACHE.clear()

    def tearDown(self):
        import shutil as _sh
        self._monitor._AI_TITLE_CACHE.clear()
        _sh.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, sid, lines):
        return _write_transcript(self._proj, sid, lines)

    def test_extracts_ai_title(self):
        jl = self._write("session1", [
            json.dumps({"type": "user", "content": "hi"}),
            json.dumps({"type": "ai-title", "aiTitle": "Refactor cost modal"}),
            json.dumps({"type": "assistant", "content": "ok"}),
        ])
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
            result = self._monitor._scan_ai_title(str(jl))
        self.assertEqual(result, "Refactor cost modal")

    def test_last_title_wins(self):
        jl = self._write("session2", [
            json.dumps({"type": "ai-title", "aiTitle": "Old title"}),
            json.dumps({"type": "user", "content": "x"}),
            json.dumps({"type": "ai-title", "aiTitle": "New title"}),
        ])
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
            result = self._monitor._scan_ai_title(str(jl))
        self.assertEqual(result, "New title")

    def test_missing_returns_none(self):
        jl = self._write("session3", [
            json.dumps({"type": "user", "content": "no title here"}),
            json.dumps({"type": "assistant", "content": "ok"}),
        ])
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
            result = self._monitor._scan_ai_title(str(jl))
        self.assertIsNone(result)

    def test_sanitizes_ansi(self):
        jl = self._write("session4", [
            json.dumps({"type": "ai-title", "aiTitle": "Title \x1b[31mred\x1b[0m"}),
        ])
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
            result = self._monitor._scan_ai_title(str(jl))
        self.assertNotIn("\x1b", result or "")
        self.assertIn("Title", result or "")

    def test_invalid_json_lines_skipped(self):
        jl = self._proj / "session5.jsonl"
        jl.write_text(
            "not json\n"
            + json.dumps({"type": "ai-title", "aiTitle": "Survived"}) + "\n"
            + "{broken\n",
            encoding="utf-8",
        )
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
            result = self._monitor._scan_ai_title(str(jl))
        self.assertEqual(result, "Survived")

    def test_oversize_file_returns_none(self):
        jl = self._proj / "session6.jsonl"
        # Title at the head still must be ignored when the transcript exceeds the cap.
        jl.write_text(
            json.dumps({"type": "ai-title", "aiTitle": "Too large"}) + "\n" + ("x" * 64),
            encoding="utf-8",
        )
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root), \
             patch.object(self._monitor, "TRANSCRIPT_MAX_BYTES", 10):
            result = self._monitor._scan_ai_title(str(jl))
        self.assertIsNone(result)

    def test_empty_string_title_ignored(self):
        jl = self._write("session7", [
            json.dumps({"type": "ai-title", "aiTitle": "   "}),
            json.dumps({"type": "ai-title", "aiTitle": ""}),
        ])
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
            result = self._monitor._scan_ai_title(str(jl))
        self.assertIsNone(result)

    def test_path_outside_root_rejected(self):
        # Write outside the fake root, ensure containment check rejects it
        outside = pathlib.Path(self._tmpdir).parent / "evil.jsonl"
        try:
            outside.write_text(
                json.dumps({"type": "ai-title", "aiTitle": "Pwn"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
                result = self._monitor._scan_ai_title(str(outside))
            self.assertIsNone(result)
        finally:
            try:
                outside.unlink()
            except OSError:
                pass

    def test_cache_hit_skips_reparse(self):
        jl = self._write("session8", [
            json.dumps({"type": "ai-title", "aiTitle": "First read"}),
        ])
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
            r1 = self._monitor._scan_ai_title(str(jl))
            # Overwrite contents but DON'T touch mtime — cache should serve old value
            old_mt = jl.stat().st_mtime
            jl.write_text(
                json.dumps({"type": "ai-title", "aiTitle": "Changed"}) + "\n",
                encoding="utf-8",
            )
            os.utime(str(jl), (old_mt, old_mt))
            r2 = self._monitor._scan_ai_title(str(jl))
        self.assertEqual(r1, "First read")
        self.assertEqual(r2, "First read")  # cached

    def test_cache_invalidates_on_mtime_change(self):
        jl = self._write("session9", [
            json.dumps({"type": "ai-title", "aiTitle": "v1"}),
        ])
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", self._fake_root):
            r1 = self._monitor._scan_ai_title(str(jl))
            # Write new content + bump mtime
            jl.write_text(
                json.dumps({"type": "ai-title", "aiTitle": "v2"}) + "\n",
                encoding="utf-8",
            )
            os.utime(str(jl), (time.time() + 10, time.time() + 10))
            r2 = self._monitor._scan_ai_title(str(jl))
        self.assertEqual(r1, "v1")
        self.assertEqual(r2, "v2")

    def test_invalid_input_returns_none(self):
        self.assertIsNone(self._monitor._scan_ai_title(None))
        self.assertIsNone(self._monitor._scan_ai_title(""))
        self.assertIsNone(self._monitor._scan_ai_title(12345))


class TestListSessionsAiTitle(unittest.TestCase):
    """ai_title surfaces into list_sessions output dict."""

    def setUp(self):
        import monitor as _monitor
        self._monitor = _monitor
        self._orig_data = _monitor.DATA_DIR
        self._tmpdir = tempfile.mkdtemp()
        self._proj_root = pathlib.Path(self._tmpdir) / "projects"
        self._proj_root.mkdir()
        self._proj_dir = self._proj_root / "proj"
        self._proj_dir.mkdir()
        self._data_dir = pathlib.Path(self._tmpdir) / "data"
        self._data_dir.mkdir()
        _monitor.DATA_DIR = self._data_dir
        _monitor._AI_TITLE_CACHE.clear()

    def tearDown(self):
        import shutil as _sh
        self._monitor.DATA_DIR = self._orig_data
        self._monitor._AI_TITLE_CACHE.clear()
        _sh.rmtree(self._tmpdir, ignore_errors=True)

    def test_ai_title_surfaces_into_session_dict(self):
        sid = "abc123validsid"
        # Transcript inside fake projects root with ai-title record
        jl = self._proj_dir / f"{sid}.jsonl"
        jl.write_text(
            json.dumps({"type": "ai-title", "aiTitle": "My session goal"}) + "\n",
            encoding="utf-8",
        )
        # State snapshot pointing at transcript
        snap = self._data_dir / f"{sid}.json"
        snap.write_text(
            json.dumps({
                "model": {"display_name": "Opus"},
                "session_name": "",
                "cwd": "",
                "transcript_path": str(jl),
            }),
            encoding="utf-8",
        )
        fake_root = self._proj_root.resolve()
        with patch.object(self._monitor, "CLAUDE_PROJECTS_DIR", fake_root):
            sessions = self._monitor.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["ai_title"], "My session goal")

    def test_missing_transcript_yields_empty_ai_title(self):
        sid = "noTranscriptSid"
        snap = self._data_dir / f"{sid}.json"
        snap.write_text(
            json.dumps({
                "model": {"display_name": "Opus"},
                "session_name": "",
                "cwd": "",
            }),
            encoding="utf-8",
        )
        sessions = self._monitor.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["ai_title"], "")


class TestServerToolUseAggregation(unittest.TestCase):
    """C: server_tool_use + cache 1h/5m split surface in _aggregate_session_cost."""

    # NOTE: helper merged with TestAggregateSessionCost._make_record into
    # tests._helpers._make_assistant_record (callers pass raw JSON keys).
    _USAGE_DEFAULTS = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    def _make_record(self, **usage_overrides):
        return _make_assistant_record(
            "claude-opus-4-7", **{**self._USAGE_DEFAULTS, **usage_overrides}
        )

    def setUp(self):
        from monitor import _SESSION_COST_CACHE as _cache
        self._cache = _cache
        _cache.clear()
        self._tmpdir = tempfile.mkdtemp()
        self._proj_dir = pathlib.Path(self._tmpdir) / "proj"
        self._proj_dir.mkdir()
        self._fake_root = pathlib.Path(self._tmpdir).resolve()

    def tearDown(self):
        import shutil as _sh
        self._cache.clear()
        _sh.rmtree(self._tmpdir, ignore_errors=True)

    def _run(self, sid, lines):
        import monitor as _m
        jl = _write_transcript(self._proj_dir, sid, lines)
        with patch.object(_m, "CLAUDE_PROJECTS_DIR", self._fake_root):
            return _m._aggregate_session_cost(
                {"session_id": sid, "transcript_path": str(jl)}
            )

    def test_counts_web_search_and_fetch(self):
        result = self._run("svrtoolssid", [
            self._make_record(server_tool_use={
                "web_search_requests": 5, "web_fetch_requests": 2,
            }),
            self._make_record(server_tool_use={
                "web_search_requests": 7, "web_fetch_requests": 1,
            }),
        ])
        self.assertEqual(result["web_search_requests"], 12)
        self.assertEqual(result["web_fetch_requests"], 3)

    def test_counts_cache_creation_split(self):
        result = self._run("cachesplitsid", [
            self._make_record(cache_creation={
                "ephemeral_1h_input_tokens": 34117,
                "ephemeral_5m_input_tokens": 0,
            }),
            self._make_record(cache_creation={
                "ephemeral_1h_input_tokens": 1000,
                "ephemeral_5m_input_tokens": 500,
            }),
        ])
        self.assertEqual(result["cache_1h"], 35117)
        self.assertEqual(result["cache_5m"], 500)

    def test_backwards_compat_no_server_tool_use(self):
        # Old transcript records without server_tool_use / cache_creation
        result = self._run("oldsid", [
            self._make_record(input_tokens=100, output_tokens=50),
        ])
        self.assertEqual(result["web_search_requests"], 0)
        self.assertEqual(result["web_fetch_requests"], 0)
        self.assertEqual(result["cache_1h"], 0)
        self.assertEqual(result["cache_5m"], 0)

    def test_non_dict_server_tool_use_ignored(self):
        # Defensive: malformed sub-objects must not crash
        result = self._run("badtypesid", [
            self._make_record(server_tool_use="not-a-dict",
                              cache_creation=12345),
        ])
        self.assertEqual(result["web_search_requests"], 0)
        self.assertEqual(result["cache_1h"], 0)


class TestRenderCostBreakdownServerTool(unittest.TestCase):
    """C: WSR/WFR/TIE/T5M rows render conditionally on non-zero values."""

    def setUp(self):
        from monitor import _SESSION_COST_CACHE as _cache
        self._cache = _cache
        _cache.clear()
        self._tmpdir = tempfile.mkdtemp()
        self._proj_dir = pathlib.Path(self._tmpdir) / "proj"
        self._proj_dir.mkdir()
        self._fake_root = pathlib.Path(self._tmpdir).resolve()

    def tearDown(self):
        import shutil as _sh
        self._cache.clear()
        _sh.rmtree(self._tmpdir, ignore_errors=True)

    def _render(self, sid, usage):
        import monitor as _m
        jl = self._proj_dir / f"{sid}.jsonl"
        jl.write_text(
            json.dumps({"type": "assistant",
                        "message": {"model": "claude-opus-4-7", "usage": usage}}) + "\n",
            encoding="utf-8",
        )
        data = {
            "session_id": sid,
            "transcript_path": str(jl),
            "model": {"id": "claude-opus-4-7", "display_name": "Opus 4.7"},
            "cost": {"total_cost_usd": 0.5, "total_duration_ms": 60000},
            "context_window": {
                "current_usage": {"input_tokens": 100, "output_tokens": 50,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0},
                "total_input_tokens": 100, "total_output_tokens": 50,
            },
        }
        with patch.object(_m, "CLAUDE_PROJECTS_DIR", self._fake_root):
            buf = _m.render_cost_breakdown(data, [], 80, 50)
        return "\n".join(_strip_ansi(buf))

    def test_wsr_wfr_visible_when_nonzero(self):
        plain = self._render("wsrvisible", {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "server_tool_use": {"web_search_requests": 7, "web_fetch_requests": 2},
        })
        self.assertIn("WSR:", plain)
        self.assertIn("WFR:", plain)
        self.assertIn("7", plain)

    def test_wsr_wfr_hidden_when_zero(self):
        plain = self._render("wsrhidden", {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        })
        self.assertNotIn("WSR:", plain)
        self.assertNotIn("WFR:", plain)

    def test_cache_split_visible_when_nonzero(self):
        plain = self._render("cachesplit", {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_creation": {
                "ephemeral_1h_input_tokens": 5000,
                "ephemeral_5m_input_tokens": 200,
            },
        })
        self.assertIn("TIE:", plain)
        self.assertIn("T5M:", plain)


class TestReadStatsCache(unittest.TestCase):
    """Coverage for _read_stats_cache — CC ~/.claude/stats-cache.json reader."""

    def setUp(self):
        import monitor as _monitor
        self._monitor = _monitor
        self._tmpdir = tempfile.mkdtemp()
        self._fake_path = pathlib.Path(self._tmpdir) / "stats-cache.json"
        self._orig_path = _monitor._STATS_CACHE_PATH
        _monitor._STATS_CACHE_PATH = self._fake_path

    def tearDown(self):
        import shutil as _sh
        self._monitor._STATS_CACHE_PATH = self._orig_path
        _sh.rmtree(self._tmpdir, ignore_errors=True)

    def test_missing_file(self):
        data, mt = self._monitor._read_stats_cache()
        self.assertIsNone(data)
        self.assertEqual(mt, 0)

    def test_invalid_json(self):
        self._fake_path.write_text("not json", encoding="utf-8")
        data, mt = self._monitor._read_stats_cache()
        self.assertIsNone(data)
        self.assertEqual(mt, 0)

    def test_missing_version(self):
        self._fake_path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        data, mt = self._monitor._read_stats_cache()
        self.assertIsNone(data)

    def test_invalid_version_type(self):
        self._fake_path.write_text(json.dumps({"version": "3"}), encoding="utf-8")
        data, mt = self._monitor._read_stats_cache()
        self.assertIsNone(data)

    def test_non_dict_root(self):
        self._fake_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        data, mt = self._monitor._read_stats_cache()
        self.assertIsNone(data)

    def test_oversize_rejected(self):
        self._fake_path.write_bytes(b"x" * (self._monitor._STATS_CACHE_MAX_BYTES + 1))
        data, mt = self._monitor._read_stats_cache()
        self.assertIsNone(data)

    def test_valid_cache(self):
        payload = {
            "version": 3,
            "lastComputedDate": "2026-04-24",
            "totalSessions": 31,
            "totalMessages": 8744,
            "hourCounts": {"0": 7, "12": 1},
            "dailyActivity": [{"date": "2026-04-24", "messageCount": 865, "sessionCount": 1, "toolCallCount": 239}],
        }
        self._fake_path.write_text(json.dumps(payload), encoding="utf-8")
        data, mt = self._monitor._read_stats_cache()
        self.assertIsNotNone(data)
        self.assertEqual(data["totalSessions"], 31)
        self.assertGreater(mt, 0)


class TestRenderHourHeatmap(unittest.TestCase):

    def setUp(self):
        import monitor as _monitor
        self._monitor = _monitor

    def test_empty_dict(self):
        result = self._monitor._render_hour_heatmap({})
        self.assertEqual(result, " " * 24)

    def test_all_zero(self):
        result = self._monitor._render_hour_heatmap({"0": 0, "12": 0})
        self.assertEqual(result, " " * 24)

    def test_single_hour(self):
        result = self._monitor._render_hour_heatmap({"5": 100})
        self.assertEqual(len(result), 24)
        self.assertEqual(result[5], "█")  # peak hour gets full block
        # Other hours blank
        self.assertEqual(result[0], " ")

    def test_peak_is_highest_glyph(self):
        result = self._monitor._render_hour_heatmap({"3": 1, "10": 50, "20": 100})
        self.assertEqual(result[20], "█")
        # mid value uses lower glyph
        self.assertNotEqual(result[10], "█")
        self.assertNotEqual(result[10], " ")

    def test_invalid_keys_ignored(self):
        result = self._monitor._render_hour_heatmap({"abc": 5, "24": 10, "-1": 7, "5": 1})
        self.assertEqual(len(result), 24)
        self.assertEqual(result[5], "█")

    def test_negative_values_treated_as_zero(self):
        result = self._monitor._render_hour_heatmap({"5": -10, "6": 10})
        self.assertEqual(result[5], " ")
        self.assertEqual(result[6], "█")

    def test_non_dict_input(self):
        result = self._monitor._render_hour_heatmap(None)
        self.assertEqual(result, " " * 24)


class TestRenderStatsLifetime(unittest.TestCase):
    """Coverage for LIFETIME ACTIVITY block in render_stats."""

    def setUp(self):
        import monitor as _monitor
        self._monitor = _monitor
        self._tmpdir = tempfile.mkdtemp()
        self._fake_path = pathlib.Path(self._tmpdir) / "stats-cache.json"
        self._orig_path = _monitor._STATS_CACHE_PATH
        _monitor._STATS_CACHE_PATH = self._fake_path
        # Force scan_transcript_stats to return empty so layout is deterministic
        # and the LIFETIME block has predictable space budget.
        self._scan_patcher = patch.object(
            _monitor, "scan_transcript_stats",
            return_value=({}, {"sessions": 0, "active_days": set(),
                                "longest_dur_ms": 0, "first_date": None,
                                "daily_tokens": {}, "truncated": False}),
        )
        self._scan_patcher.start()

    def tearDown(self):
        import shutil as _sh
        self._scan_patcher.stop()
        self._monitor._STATS_CACHE_PATH = self._orig_path
        # Clear module-level caches so we don't pollute subsequent tests
        self._monitor._usage_cache.clear()
        _sh.rmtree(self._tmpdir, ignore_errors=True)

    def _write_cache(self, **overrides):
        payload = {
            "version": 3,
            "lastComputedDate": "2026-04-24",
            "totalSessions": 42,
            "totalMessages": 9999,
            "firstSessionDate": "2026-04-20T17:54:18.073Z",
            "longestSession": {"sessionId": "abc", "duration": 80793630, "messageCount": 100},
            "hourCounts": {"5": 10, "14": 25, "20": 50},
            "dailyActivity": [
                {"date": "2026-04-24", "messageCount": 865, "sessionCount": 1, "toolCallCount": 239},
                {"date": "2026-04-23", "messageCount": 928, "sessionCount": 2, "toolCallCount": 365},
                {"date": "2026-04-22", "messageCount": 2018, "sessionCount": 4, "toolCallCount": 814},
            ],
        }
        payload.update(overrides)
        self._fake_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_lifetime_block_renders_when_cache_present(self):
        self._write_cache()
        buf = self._monitor.render_stats(80, 40, period="all")
        plain = "\n".join(_strip_ansi(buf))
        self.assertIn("LIFETIME", plain)
        self.assertIn("cached 2026-04-24", plain)
        self.assertIn("42", plain)        # totalSessions
        self.assertIn("9,999", plain)     # totalMessages
        self.assertIn("HRS", plain)
        self.assertIn("DAILY", plain)
        self.assertIn("04-24", plain)     # date short

    def test_lifetime_block_omitted_on_small_terminal(self):
        self._write_cache()
        buf = self._monitor.render_stats(80, 12, period="all")
        plain = "\n".join(_strip_ansi(buf))
        self.assertNotIn("LIFETIME", plain)

    def test_daily_omitted_on_medium_terminal(self):
        self._write_cache()
        # Empty-models path: pre=5, footer=3, core needs 7, daily needs 7 more.
        # rows=18 gives budget = 10 → core fits, daily doesn't.
        buf = self._monitor.render_stats(80, 18, period="all")
        plain = "\n".join(_strip_ansi(buf))
        self.assertIn("LIFETIME", plain)
        self.assertIn("HRS", plain)
        self.assertNotIn("DAILY", plain)

    def test_lifetime_block_skipped_when_cache_missing(self):
        # No file written
        buf = self._monitor.render_stats(80, 40, period="all")
        plain = "\n".join(_strip_ansi(buf))
        self.assertNotIn("LIFETIME", plain)


# ---------------------------------------------------------------------------
# TestMainSingleton — main() exits with code 1 when lock already held
# ---------------------------------------------------------------------------
class TestMainSingleton(unittest.TestCase):
    """Verify main() calls sys.exit(msg) when acquire_singleton_lock returns None."""

    def _run_main_with_lock_none(self):
        """Patch enough of main() to reach the singleton-lock check and no further."""
        import io
        stderr_capture = io.StringIO()
        with patch("sys.argv", ["monitor"]), \
             patch("monitor.ensure_data_dir", return_value=True), \
             patch("monitor.acquire_singleton_lock", return_value=None), \
             patch("monitor._install_crash_logger"), \
             patch("sys.stderr", stderr_capture):
            import monitor as _m
            with self.assertRaises(SystemExit) as cm:
                _m.main()
        return cm.exception, stderr_capture.getvalue()

    def test_singleton_lock_none_raises_system_exit(self):
        exc, _ = self._run_main_with_lock_none()
        # sys.exit(msg) sets code to the message string (non-zero equivalent)
        self.assertIsNotNone(exc.code)
        # Truthy code — either a non-empty string or integer != 0
        self.assertTrue(exc.code)

    def test_singleton_lock_none_exit_message_contains_already_running(self):
        exc, _ = self._run_main_with_lock_none()
        msg = str(exc.code)
        self.assertIn("already running", msg)

    def test_singleton_lock_none_exit_code_is_1_via_stderr(self):
        """When called via subprocess the process exits with code 1.

        We verify the exit *value* type: sys.exit(string) maps to exit code 1
        in CPython — confirmed by checking that exc.code is a non-empty string
        (not an integer 0).
        """
        exc, _ = self._run_main_with_lock_none()
        # A string exit message always causes Python to print to stderr and exit 1
        self.assertIsInstance(exc.code, str)
        self.assertGreater(len(exc.code), 0)

    def test_main_list_mode_skips_singleton_lock(self):
        """--list exits before acquire_singleton_lock is called (exempt path)."""
        with patch("sys.argv", ["monitor.py", "--list"]), \
             patch("monitor.ensure_data_dir", return_value=True), \
             patch("monitor.ensure_utf8_stdout"), \
             patch("monitor.acquire_singleton_lock") as mock_lock, \
             patch("monitor.list_sessions", return_value=[]):
            import monitor as _m
            _m.main()
        mock_lock.assert_not_called()

if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
