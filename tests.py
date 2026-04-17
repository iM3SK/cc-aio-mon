#!/usr/bin/env python3
"""Unit tests for CC AIO MON — stdlib only, no pytest required.

Run:
    python tests.py
"""

import os
import pathlib
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import shared
from update import (
    get_local_version,
    get_remote_version,
    get_ahead_behind,
    get_remote_changelog_entry,
    check_clean,
)

# Import target functions directly
from monitor import (
    _fit_buf_height, calc_rates, f_tok, f_cost, f_dur, f_cd, _num,
    _limit_color, _reset_color, collect_warnings,
    truncate, mkbar,
    calc_cross_session_costs,
    _parse_ts, _calc_streaks, _model_label, _total_tokens,
    scan_transcript_stats, render_stats, render_legend, render_frame,
    _CLAUDE_DIR, _usage_cache,
    WARN_BRN, BRN_MAX, CTR_MAX, CST_MAX,
    BAR_W,
    _parse_version, _rls_cache, _rls_blink, VERSION,
    _rls_check_worker, _rls_maybe_check, _RLS_TTL,
    spin_session, spin_rls, _SPIN_SESSION, _SPIN_RLS,
    _git_cmd, _update_checks, _get_new_commits,
    _get_remote_changelog_preview, _apply_update_action, _apply_update_worker,
    render_update_modal,
    _RESERVED_FILES, list_sessions, load_state, load_history, DATA_DIR,
    render_picker,
    cached_cross_session_costs, _cost_cache,
    flush, SYNC_ON, SYNC_OFF,
    render_menu, render_cost_breakdown,
    _model_code, _cost_thirds, _get_pricing, _DEFAULT_PRICING,
    _aggregate_session_cost, _SESSION_COST_CACHE, _SESSION_COST_TTL,
)
from shared import (
    MAX_FILE_SIZE, _ANSI_RE, _sanitize,
    C_RED, C_GRN, C_YEL, C_ORN, C_CYN, C_DIM,
    char_width, is_safe_dir, ensure_data_dir,
)

from statusline import (
    _get_terminal_width,
    _calc_rates as sl_calc_rates,
    seg_model,
    seg_ctx,
    seg_5hl,
    seg_7dl,
    seg_cost,
    seg_chr,
    seg_brn,
    seg_apr,
    build_line,
    cpc_base,
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

    def test_dashboard_rows_invalid_string(self):
        buf = ["a", "b"]
        _fit_buf_height(buf, "bad", clip_tail=False)
        self.assertIsInstance(buf, list)

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

def _vlen(text):
    """Strip ANSI escapes and return visible length."""
    return len(_ANSI_RE.sub("", text))


def _full_data(**overrides):
    """Minimal realistic data dict for statusline segments."""
    d = {
        "model": {"display_name": "Opus 4"},
        "context_window": {
            "used_percentage": 42,
            "context_window_size": 200000,
            "current_usage": {
                "cache_read_input_tokens": 8000,
                "cache_creation_input_tokens": 2000,
            },
        },
        "cost": {
            "total_cost_usd": 1.23,
            "total_duration_ms": 120000,
            "total_api_duration_ms": 90000,
            "total_lines_added": 150,
            "total_lines_removed": 30,
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 25},
            "seven_day": {"used_percentage": 10},
        },
    }
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# _sanitize
# ---------------------------------------------------------------------------
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

    def test_basic(self):
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

    def test_basic(self):
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

    def test_basic(self):
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
        self.assertIn("0%", _ANSI_RE.sub("", text))


class TestSeg7dl(unittest.TestCase):

    def test_basic(self):
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


class TestSegCost(unittest.TestCase):

    def test_basic(self):
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


class TestSegChr(unittest.TestCase):

    def test_basic(self):
        text, vl = seg_chr(_full_data())
        self.assertEqual(vl, _vlen(text))
        self.assertIn("CHR", _ANSI_RE.sub("", text))

    def test_label_uses_green(self):
        text, _ = seg_chr(_full_data())
        self.assertIn(C_GRN, text)

    def test_no_cache_data(self):
        d = _full_data()
        d["context_window"]["current_usage"] = {}
        self.assertIsNone(seg_chr(d))

    def test_zero_cache(self):
        d = _full_data()
        d["context_window"]["current_usage"] = {
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        self.assertIsNone(seg_chr(d))


class TestSegApr(unittest.TestCase):

    def test_basic(self):
        text, vl = seg_apr(_full_data())
        self.assertEqual(vl, _vlen(text))
        self.assertIn("APR", _ANSI_RE.sub("", text))
        self.assertIn("75.0%", _ANSI_RE.sub("", text))  # 90000/120000

    def test_zero_duration(self):
        d = _full_data()
        d["cost"]["total_duration_ms"] = 0
        self.assertIsNone(seg_apr(d))


class TestSegBrn(unittest.TestCase):

    def test_basic(self):
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

    def test_minimum_spacer(self):
        # Even at very narrow width, spacer >= 1
        line = build_line(_full_data(), 20)
        self.assertIsNotNone(line)

    def test_empty_data(self):
        line = build_line({}, 80)
        # Should still produce something (empty model at minimum)
        self.assertIsNotNone(line)


# ---------------------------------------------------------------------------
# Fixed-range bars (BRN/CTR/CST)
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

    def _write_session(self, project, sid, lines, subagent=False):
        import pathlib
        if subagent:
            d = pathlib.Path(self.tmpdir) / project / sid / "subagents"
        else:
            d = pathlib.Path(self.tmpdir) / project
        d.mkdir(parents=True, exist_ok=True)
        fn = f"agent-{sid}.jsonl" if subagent else f"{sid}.jsonl"
        (d / fn).write_text("\n".join(lines) + "\n", encoding="utf-8")

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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", main_lines)
        self._write_session("proj1", "sess1", sub_lines, subagent=True)
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
        self._write_session("proj1", "sess1", lines)
        models, ov = scan_transcript_stats("all", ttl=0)
        self.assertEqual(models["claude-opus-4-6"]["calls"], 1)

    def test_ttl_cache(self):
        import json, monitor
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 10, "output_tokens": 20}},
        })]
        self._write_session("proj1", "sess1", lines)
        # First call populates cache
        m1, _ = scan_transcript_stats("all", ttl=60)
        self.assertEqual(m1["claude-opus-4-6"]["calls"], 1)
        # Write more data
        lines.append(json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:01:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 10, "output_tokens": 20}},
        }))
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        self._write_session("proj1", "sess1", lines)
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
        import json, pathlib
        d = pathlib.Path(self.tmpdir) / "proj1"
        d.mkdir(parents=True)
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 200}},
        })]
        (d / "sess1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        buf = render_stats(80, 40, "all")
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("OP", plain)
        self.assertIn("4.6", plain)
        self.assertIn("100.0", plain)  # 100% single model
        self.assertIn("ALL", plain)

    def test_shows_overview_metrics(self):
        import json, pathlib
        d = pathlib.Path(self.tmpdir) / "proj1"
        d.mkdir(parents=True)
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-04-12T10:00:00Z",
            "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 200}},
        })]
        (d / "sess1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
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
        import json, pathlib
        d = pathlib.Path(self.tmpdir) / "proj1"
        d.mkdir(parents=True)
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
        (d / "sess1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

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

    def _make_record(self, model, input_tokens=0, output_tokens=0,
                     cache_read=0, cache_write=0):
        import json as _json
        return _json.dumps({
            "type": "assistant",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                },
            },
        })

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
                self._make_record("claude-opus-4-6",
                                  input_tokens=1000, output_tokens=500) + "\n"
                + self._make_record("claude-sonnet-4-6",
                                    input_tokens=2000, output_tokens=300,
                                    cache_read=4000, cache_write=1000) + "\n",
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
            self._make_record("claude-sonnet-4-6",
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
                self._make_record("claude-sonnet-4-6",
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
                self._make_record("claude-sonnet-4-6",
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
                self._make_record("claude-sonnet-4-6",
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


# ===========================================================================
# RLS BACKGROUND CHECK TESTS
# ===========================================================================

import monitor as _monitor_mod
import subprocess


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
        with patch("monitor.subprocess.run", side_effect=self._make_run(fetch_rc=1)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "error")
        self.assertIsNone(_rls_cache["remote_ver"])

    # ------------------------------------------------------------------
    # b. git show fails → status "error"
    # ------------------------------------------------------------------
    def test_show_fail_sets_error(self):
        with patch("monitor.subprocess.run", side_effect=self._make_run(fetch_rc=0, show_rc=1)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "error")
        self.assertIsNone(_rls_cache["remote_ver"])

    # ------------------------------------------------------------------
    # c. VERSION regex not found in remote output → status "error"
    # ------------------------------------------------------------------
    def test_version_regex_not_found_sets_error(self):
        stdout_no_version = "# some python file\nfoo = 'bar'\n"
        with patch("monitor.subprocess.run",
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
        with patch("monitor.subprocess.run", side_effect=self._make_run(show_stdout=stdout)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "update")
        self.assertEqual(_rls_cache["remote_ver"], remote_ver)

    # ------------------------------------------------------------------
    # e. Remote version == local → status "ok"
    # ------------------------------------------------------------------
    def test_remote_same_sets_ok(self):
        stdout = f'VERSION = "{VERSION}"\n'
        with patch("monitor.subprocess.run", side_effect=self._make_run(show_stdout=stdout)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "ok")
        self.assertEqual(_rls_cache["remote_ver"], VERSION)

    # ------------------------------------------------------------------
    # f. FileNotFoundError (no git binary) → status "no_git"
    # ------------------------------------------------------------------
    def test_no_git_sets_no_git(self):
        with patch("monitor.subprocess.run", side_effect=FileNotFoundError):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "no_git")
        self.assertIsNone(_rls_cache["remote_ver"])

    # ------------------------------------------------------------------
    # g. TimeoutExpired → status "timeout"
    # ------------------------------------------------------------------
    def test_timeout_sets_timeout(self):
        with patch("monitor.subprocess.run",
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
class TestUpdate(unittest.TestCase):

    # -- get_local_version ---------------------------------------------------

    def test_get_local_version_double_quotes(self):
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            import update
            old_root = update.REPO_ROOT
            update.REPO_ROOT = Path(td)
            (Path(td) / "monitor.py").write_text('VERSION = "1.2.3"\n', encoding="utf-8")
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
            (Path(td) / "monitor.py").write_text("VERSION = '2.0.0'\n", encoding="utf-8")
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
            (Path(td) / "monitor.py").write_text("# no version here\n", encoding="utf-8")
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
class TestGitCmd(unittest.TestCase):

    def test_success(self):
        import subprocess as sp
        completed = sp.CompletedProcess(args=["git"], returncode=0, stdout="ok\n", stderr="")
        with patch("monitor.subprocess.run", return_value=completed):
            rc, out, err = _git_cmd(["status"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "ok")
        self.assertEqual(err, "")

    def test_file_not_found(self):
        with patch("monitor.subprocess.run", side_effect=FileNotFoundError):
            rc, out, err = _git_cmd(["status"])
        self.assertEqual(rc, -1)
        self.assertEqual(out, "")
        self.assertIn("git not found", err)

    def test_timeout(self):
        import subprocess as sp
        with patch("monitor.subprocess.run", side_effect=sp.TimeoutExpired(cmd="git", timeout=15)):
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

    def test_success(self):
        with patch("monitor._git_cmd", return_value=(0, "abc feat\ndef fix", "")):
            result = _get_new_commits()
        self.assertEqual(result, ["abc feat", "def fix"])

    def test_failure(self):
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
        import update as _up
        src = pathlib.Path(_up.__file__).read_text(encoding="utf-8")
        self.assertIn('"pulse.py"', src, "pulse.py must be in py_files syntax check list")

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

    def test_success(self):
        # Test the synchronous worker directly (not the thread-spawning wrapper)
        with patch("monitor._git_cmd", return_value=(0, "ok", "")):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value="# valid python\nx = 1\n"):
                    _apply_update_worker()

        import monitor
        self.assertIn("complete", monitor._update_result)

    def test_failure(self):
        with patch("monitor._git_cmd", return_value=(1, "", "conflict")):
            _apply_update_worker()

        import monitor
        self.assertIn("failed", monitor._update_result)


# ---------------------------------------------------------------------------
# render_update_modal
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
# TestSegAprClamp — seg_apr clamps >100%
# ---------------------------------------------------------------------------
class TestSegAprClamp(unittest.TestCase):

    def test_clamp_over_100(self):
        data = {"cost": {"total_duration_ms": 100, "total_api_duration_ms": 200}}
        result = seg_apr(data)
        self.assertIsNotNone(result)
        text, vl = result
        self.assertIn("100.0%", _ANSI_RE.sub("", text))

    def test_visible_length_consistent(self):
        data = {"cost": {"total_duration_ms": 100, "total_api_duration_ms": 200}}
        text, vl = seg_apr(data)
        self.assertEqual(vl, len(_ANSI_RE.sub("", text)))


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
# TestReservedFiles — _RESERVED_FILES contains expected sentinel names
# ---------------------------------------------------------------------------
class TestReservedFiles(unittest.TestCase):

    def test_rls_in_reserved(self):
        self.assertIn("rls", _RESERVED_FILES)

    def test_stats_in_reserved(self):
        self.assertIn("stats", _RESERVED_FILES)

    def test_pulse_in_reserved(self):
        self.assertIn("pulse", _RESERVED_FILES)

    def test_reserved_is_a_set(self):
        self.assertIsInstance(_RESERVED_FILES, (set, frozenset))


# ---------------------------------------------------------------------------
# TestUpdateFlowFunctions — update.py check_repo, check_branch, fetch_remote, etc.
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


# ---------------------------------------------------------------------------
# TestSessionAutoConnect — auto-connect condition logic
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


class TestPulseScore(unittest.TestCase):

    def test_score_all_green(self):
        r = pulse.compute_score({
            "indicator": "none",
            "incidents": [],
            "components": [],
            "latency_ms": 120.0,
            "error": None,
        })
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["level"], "ok")
        self.assertEqual(r["verdict"], "SAFE TO CODE")

    def test_score_critical_indicator(self):
        r = pulse.compute_score({
            "indicator": "critical",
            "incidents": [{"name": "outage", "impact": "critical"}],
            "components": [],
            "latency_ms": None,
            "error": None,
        })
        self.assertEqual(r["level"], "bad")
        self.assertEqual(r["verdict"], "NOT SAFE TO CODE")
        self.assertLess(r["score"], 50)

    def test_score_degraded_band(self):
        r = pulse.compute_score({
            "indicator": "minor",
            "incidents": [{"name": "x", "impact": "minor"}],
            "components": [],
            "latency_ms": 900.0,
            "error": None,
        })
        self.assertEqual(r["level"], "degraded")
        self.assertIn("DEGRADED", r["verdict"])
        self.assertGreaterEqual(r["score"], 50)
        self.assertLess(r["score"], 80)

    def test_score_error_passthrough(self):
        r = pulse.compute_score({
            "indicator": None,
            "incidents": [],
            "components": [],
            "latency_ms": None,
            "error": "HTTP 503",
        })
        self.assertIsNone(r["score"])
        self.assertEqual(r["level"], "error")
        self.assertEqual(r["verdict"], "PULSE ERROR")
        self.assertEqual(r["reason"], "HTTP 503")

    def test_latency_score_buckets(self):
        self.assertEqual(pulse._latency_score(None), 0)
        self.assertEqual(pulse._latency_score(100), 100)
        self.assertEqual(pulse._latency_score(500), 70)
        self.assertEqual(pulse._latency_score(1500), 40)
        self.assertEqual(pulse._latency_score(5000), 10)

    def test_latency_score_exact_boundaries(self):
        # Boundary is exclusive on the upper end: 300 is NOT in the <300 bucket
        self.assertEqual(pulse._latency_score(299.9), 100)
        self.assertEqual(pulse._latency_score(300), 70)
        self.assertEqual(pulse._latency_score(799.9), 70)
        self.assertEqual(pulse._latency_score(800), 40)
        self.assertEqual(pulse._latency_score(1999.9), 40)
        self.assertEqual(pulse._latency_score(2000), 10)

    def test_score_verdict_boundary_80(self):
        # Exactly 80 → "ok" / SAFE; 79 → "degraded"
        self.assertEqual(pulse._score_to_verdict(80), ("SAFE TO CODE", "ok"))
        self.assertEqual(pulse._score_to_verdict(79)[1], "degraded")

    def test_score_verdict_boundary_50(self):
        # Exactly 50 → "degraded"; 49 → "bad"
        self.assertEqual(pulse._score_to_verdict(50)[1], "degraded")
        self.assertEqual(pulse._score_to_verdict(49)[1], "bad")

    def test_score_clamped(self):
        # Many incidents — incident subscore floored at 0
        r = pulse.compute_score({
            "indicator": "none",
            "incidents": [{"name": f"i{i}", "impact": "critical"} for i in range(50)],
            "components": [],
            "latency_ms": 100.0,
            "error": None,
        })
        self.assertGreaterEqual(r["score"], 0)
        self.assertLessEqual(r["score"], 100)


class TestPulseExtract(unittest.TestCase):

    def test_extract_valid(self):
        summary = {
            "status": {"indicator": "minor", "description": "partial"},
            "components": [
                {"name": "API", "status": "operational", "group": False},
                {"name": "Group", "status": "operational", "group": True},  # skip rollup
                {"name": "Claude.ai", "status": "degraded_performance"},
            ],
            "incidents": [{"name": "latency spike", "impact": "minor"}],
        }
        ind, comps, incs = pulse._extract(summary)
        self.assertEqual(ind, "minor")
        self.assertEqual(len(comps), 2)
        self.assertEqual(comps[0]["name"], "API")
        self.assertEqual(len(incs), 1)
        self.assertEqual(incs[0]["impact"], "minor")

    def test_extract_missing_keys(self):
        ind, comps, incs = pulse._extract({})
        self.assertIsNone(ind)
        self.assertEqual(comps, [])
        self.assertEqual(incs, [])

    def test_extract_bad_type(self):
        with self.assertRaises(KeyError):
            pulse._extract("not a dict")


class TestPulseSnapshot(unittest.TestCase):

    def test_snapshot_is_copy(self):
        s1 = pulse.get_pulse_snapshot()
        s1["score"] = 999
        s2 = pulse.get_pulse_snapshot()
        self.assertNotEqual(s2.get("score"), 999)

    def test_snapshot_has_schema(self):
        s = pulse.get_pulse_snapshot()
        for key in ("t", "wall_t", "score", "raw_score", "verdict", "level", "reason",
                    "indicator", "incidents", "components",
                    "latency_ms", "latency_p50_ms", "latency_p95_ms", "error"):
            self.assertIn(key, s)


class TestPulseModal(unittest.TestCase):

    def test_render_empty_snapshot(self):
        # Default snapshot — "AWAITING DATA"
        buf = render_pulse_modal(80, 30)
        plain = _ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("ANTHROPIC PULSE", plain)
        self.assertIn("STB", plain)

    def test_render_ok_snapshot(self):
        with pulse._snapshot_lock:
            pulse._snapshot.update({
                "t": time.monotonic(),
                "wall_t": time.time(),
                "score": 95,
                "verdict": "SAFE TO CODE",
                "level": "ok",
                "reason": "all systems nominal",
                "indicator": "none",
                "incidents": [],
                "components": [{"name": "API", "status": "operational"}],
                "latency_ms": 150.0,
                "error": None,
            })
        try:
            buf = render_pulse_modal(80, 40)
            plain = _ANSI_RE.sub("", "\n".join(buf))
            self.assertIn("SAFE TO CODE", plain)
            self.assertIn("API", plain)
            self.assertIn("150 ms", plain)
        finally:
            with pulse._snapshot_lock:
                pulse._snapshot.update({
                    "score": None, "verdict": "AWAITING DATA",
                    "level": "error", "reason": "no data yet",
                    "indicator": None, "incidents": [], "components": [],
                    "latency_ms": None, "error": None,
                })

    def test_render_error_snapshot(self):
        with pulse._snapshot_lock:
            pulse._snapshot.update({
                "t": time.monotonic(),
                "wall_t": time.time(),
                "score": None,
                "verdict": "PULSE ERROR",
                "level": "error",
                "reason": "HTTP 503",
                "indicator": None,
                "incidents": [],
                "components": [],
                "latency_ms": None,
                "error": "HTTP 503",
            })
        try:
            buf = render_pulse_modal(80, 30)
            plain = _ANSI_RE.sub("", "\n".join(buf))
            self.assertIn("PULSE ERROR", plain)
            self.assertIn("HTTP 503", plain)
        finally:
            with pulse._snapshot_lock:
                pulse._snapshot.update({
                    "score": None, "verdict": "AWAITING DATA",
                    "level": "error", "reason": "no data yet",
                    "error": None,
                })


class TestPulseNetwork(unittest.TestCase):
    """Mocked tests for _fetch_summary + _ping_api error taxonomy.

    Covers every branch advertised in CHANGELOG v1.9.0 error taxonomy:
    HTTPError, URLError(timeout), URLError(gaierror), URLError(other),
    socket.timeout, oversized response, JSONDecodeError.
    """

    # --- _fetch_summary --------------------------------------------------
    def _mock_response(self, body_bytes):
        """Build a context-manager object that mimics urlopen's return."""
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        m.read = MagicMock(return_value=body_bytes)
        m.close = MagicMock(return_value=None)
        return m

    def test_fetch_summary_success(self):
        payload = json.dumps({
            "status": {"indicator": "none"},
            "components": [{"name": "API", "status": "operational"}],
            "incidents": [],
        }).encode("utf-8")
        with patch("pulse.urllib.request.urlopen",
                   return_value=self._mock_response(payload)):
            data, err = pulse._fetch_summary()
        self.assertIsNone(err)
        self.assertIsNotNone(data)
        self.assertEqual(data["status"]["indicator"], "none")

    def test_fetch_summary_http_error(self):
        import urllib.error as ue
        err = ue.HTTPError(pulse.SUMMARY_URL, 503, "Service Unavailable", {}, None)
        with patch("pulse.urllib.request.urlopen", side_effect=err):
            data, tag = pulse._fetch_summary()
        self.assertIsNone(data)
        self.assertEqual(tag, "HTTP 503")

    def test_fetch_summary_http_404(self):
        import urllib.error as ue
        err = ue.HTTPError(pulse.SUMMARY_URL, 404, "Not Found", {}, None)
        with patch("pulse.urllib.request.urlopen", side_effect=err):
            _, tag = pulse._fetch_summary()
        self.assertEqual(tag, "HTTP 404")

    def test_fetch_summary_url_timeout(self):
        import urllib.error as ue
        import socket as sk
        err = ue.URLError(sk.timeout("timed out"))
        with patch("pulse.urllib.request.urlopen", side_effect=err):
            _, tag = pulse._fetch_summary()
        self.assertEqual(tag, "timeout")

    def test_fetch_summary_url_dns(self):
        import urllib.error as ue
        import socket as sk
        err = ue.URLError(sk.gaierror(8, "Name or service not known"))
        with patch("pulse.urllib.request.urlopen", side_effect=err):
            _, tag = pulse._fetch_summary()
        self.assertEqual(tag, "DNS fail")

    def test_fetch_summary_url_other(self):
        import urllib.error as ue
        err = ue.URLError(ConnectionRefusedError(111, "refused"))
        with patch("pulse.urllib.request.urlopen", side_effect=err):
            _, tag = pulse._fetch_summary()
        self.assertTrue(tag.startswith("net: "), f"got {tag!r}")

    def test_fetch_summary_direct_socket_timeout(self):
        import socket as sk
        with patch("pulse.urllib.request.urlopen", side_effect=sk.timeout("slow")):
            _, tag = pulse._fetch_summary()
        self.assertEqual(tag, "timeout")

    def test_fetch_summary_oversized_response(self):
        huge = b"x" * (pulse.MAX_RESPONSE_BYTES + 2)
        with patch("pulse.urllib.request.urlopen",
                   return_value=self._mock_response(huge)):
            data, tag = pulse._fetch_summary()
        self.assertIsNone(data)
        self.assertEqual(tag, "response too large")

    def test_fetch_summary_json_decode_error(self):
        with patch("pulse.urllib.request.urlopen",
                   return_value=self._mock_response(b"not json at all {broken")):
            data, tag = pulse._fetch_summary()
        self.assertIsNone(data)
        self.assertTrue(tag.startswith("parse: "), f"got {tag!r}")

    def test_fetch_summary_os_error(self):
        with patch("pulse.urllib.request.urlopen",
                   side_effect=OSError(5, "I/O error")):
            _, tag = pulse._fetch_summary()
        self.assertEqual(tag, "OSError")

    # --- _ping_api -------------------------------------------------------
    def test_ping_api_success(self):
        with patch("pulse.urllib.request.urlopen",
                   return_value=self._mock_response(b"{}")):
            lat = pulse._ping_api()
        self.assertIsNotNone(lat)
        self.assertGreaterEqual(lat, 0)

    def test_ping_api_http_401_is_alive(self):
        # Any HTTP status = endpoint alive — should return latency, not None
        import urllib.error as ue
        err = ue.HTTPError(pulse.PROBE_URL, 401, "Unauthorized", {}, None)
        with patch("pulse.urllib.request.urlopen", side_effect=err):
            lat = pulse._ping_api()
        self.assertIsNotNone(lat)

    def test_ping_api_http_405_is_alive(self):
        import urllib.error as ue
        err = ue.HTTPError(pulse.PROBE_URL, 405, "Method Not Allowed", {}, None)
        with patch("pulse.urllib.request.urlopen", side_effect=err):
            lat = pulse._ping_api()
        self.assertIsNotNone(lat)

    def test_ping_api_timeout(self):
        import socket as sk
        with patch("pulse.urllib.request.urlopen", side_effect=sk.timeout()):
            self.assertIsNone(pulse._ping_api())

    def test_ping_api_dns_fail(self):
        import urllib.error as ue
        import socket as sk
        err = ue.URLError(sk.gaierror(8, "no dns"))
        with patch("pulse.urllib.request.urlopen", side_effect=err):
            self.assertIsNone(pulse._ping_api())

    def test_ping_api_os_error(self):
        with patch("pulse.urllib.request.urlopen", side_effect=OSError(111, "refused")):
            self.assertIsNone(pulse._ping_api())

    # --- _refresh_once end-to-end ---------------------------------------
    def test_refresh_once_success_path(self):
        """Full pipeline: fetch + ping + score + snapshot update + log append."""
        pulse._reset_history()
        payload = json.dumps({
            "status": {"indicator": "none"},
            "components": [{"name": "API", "status": "operational"}],
            "incidents": [],
        }).encode("utf-8")
        with patch("pulse.urllib.request.urlopen",
                   return_value=self._mock_response(payload)), \
             patch("pulse._append_log"):  # skip disk I/O
            pulse._refresh_once()
        snap = pulse.get_pulse_snapshot()
        self.assertEqual(snap["indicator"], "none")
        self.assertEqual(snap["level"], "ok")
        self.assertEqual(snap["verdict"], "SAFE TO CODE")
        self.assertIsNotNone(snap["score"])

    def test_refresh_once_fetch_error_preserves_error_tag(self):
        """When fetch fails, error tag propagates into snapshot.error."""
        import urllib.error as ue
        err = ue.HTTPError(pulse.SUMMARY_URL, 503, "X", {}, None)
        with patch("pulse.urllib.request.urlopen", side_effect=err), \
             patch("pulse._append_log"):
            pulse._refresh_once()
        snap = pulse.get_pulse_snapshot()
        self.assertEqual(snap["error"], "HTTP 503")
        self.assertEqual(snap["level"], "error")

    def test_refresh_once_malformed_incident_updates_no_crash(self):
        """Regression: incident_updates[0] not being a dict must NOT crash _extract."""
        payload = json.dumps({
            "status": {"indicator": "minor"},
            "components": [],
            "incidents": [{
                "name": "test",
                "impact": "minor",
                "incident_updates": ["not a dict"],  # malformed
            }],
        }).encode("utf-8")
        with patch("pulse.urllib.request.urlopen",
                   return_value=self._mock_response(payload)), \
             patch("pulse._append_log"):
            try:
                pulse._refresh_once()
            except AttributeError:
                self.fail("_extract must defend against malformed incident_updates")
        snap = pulse.get_pulse_snapshot()
        # Should have processed the incident (not crashed into error state)
        self.assertEqual(len(snap["incidents"]), 1)


class TestPulseModelTagging(unittest.TestCase):
    """Tier 4b — detect affected model names in incident titles."""

    def test_extract_preserves_model_tags(self):
        summary = {
            "status": {"indicator": "minor"},
            "components": [],
            "incidents": [
                {"name": "Opus elevated errors", "impact": "major"},
                {"name": "Unrelated maintenance", "impact": "minor"},
            ],
        }
        _, _, incs = pulse._extract(summary)
        self.assertEqual(incs[0]["affected_models"], ["opus"])
        self.assertEqual(incs[1]["affected_models"], [])

    def test_tag_models_prefers_components_array(self):
        """incidents[].components[] takes priority over title regex."""
        inc = {
            "name": "General API degradation",  # no model name in title
            "impact": "minor",
            "components": [{"name": "Claude 3.5 Sonnet"}],
        }
        result = pulse._tag_models_from_incident(inc)
        self.assertEqual(result, ["sonnet"])

    def test_tag_models_components_overrides_title(self):
        """When components present, title model keywords are ignored."""
        inc = {
            "name": "Opus elevated errors",
            "impact": "major",
            "components": [{"name": "Claude Haiku infrastructure"}],
        }
        result = pulse._tag_models_from_incident(inc)
        # components win — should be haiku only, not opus
        self.assertEqual(result, ["haiku"])

    def test_tag_models_fallback_to_title_when_no_components(self):
        """Regex fallback fires when components array is absent."""
        inc = {
            "name": "Sonnet latency spike",
            "impact": "minor",
        }
        result = pulse._tag_models_from_incident(inc)
        self.assertEqual(result, ["sonnet"])

    def test_tag_models_empty_components_uses_fallback(self):
        """Empty components list triggers regex fallback."""
        inc = {
            "name": "Haiku elevated errors",
            "impact": "minor",
            "components": [],
        }
        result = pulse._tag_models_from_incident(inc)
        self.assertEqual(result, ["haiku"])

    def test_extract_uses_components_array_for_tagging(self):
        """_extract propagates components-array tagging through to incidents list."""
        summary = {
            "status": {"indicator": "minor"},
            "components": [],
            "incidents": [
                {
                    "name": "General degradation",
                    "impact": "major",
                    "components": [{"name": "Claude Sonnet API"}],
                },
            ],
        }
        _, _, incs = pulse._extract(summary)
        self.assertEqual(incs[0]["affected_models"], ["sonnet"])


class TestPulseSmoothing(unittest.TestCase):
    """Tests for Tier 2 — rolling median + latency percentiles."""

    def setUp(self):
        pulse._reset_history()

    def tearDown(self):
        pulse._reset_history()

    # --- _score_to_verdict (pure helper) --------------------------------
    def test_score_to_verdict_thresholds(self):
        self.assertEqual(pulse._score_to_verdict(100), ("SAFE TO CODE", "ok"))
        self.assertEqual(pulse._score_to_verdict(80), ("SAFE TO CODE", "ok"))
        self.assertEqual(pulse._score_to_verdict(79)[1], "degraded")
        self.assertEqual(pulse._score_to_verdict(50)[1], "degraded")
        self.assertEqual(pulse._score_to_verdict(49)[1], "bad")
        self.assertEqual(pulse._score_to_verdict(0)[1], "bad")
        self.assertEqual(pulse._score_to_verdict(None), ("AWAITING DATA", "error"))

    # --- _smoothed_score -------------------------------------------------
    def test_smoothed_below_min_samples_returns_raw(self):
        # With <3 samples, just pass through
        self.assertEqual(pulse._smoothed_score(90), 90)
        self.assertEqual(pulse._smoothed_score(80), 80)
        # Third sample — now smoothing kicks in

    def test_smoothed_median_of_window(self):
        pulse._reset_history()
        for s in [90, 90, 90]:
            pulse._smoothed_score(s)
        # All 90 → median 90
        self.assertEqual(pulse._smoothed_score(90), 90)

    def test_smoothed_absorbs_outlier(self):
        pulse._reset_history()
        # Steady 90, then one bad sample — median should stay high
        for s in [90, 90, 90, 90]:
            pulse._smoothed_score(s)
        # Outlier
        result = pulse._smoothed_score(20)
        # Window is [90, 90, 90, 90, 20] → median 90
        self.assertEqual(result, 90)

    def test_smoothed_trends_on_sustained_drop(self):
        pulse._reset_history()
        for s in [90, 90, 90]:
            pulse._smoothed_score(s)
        # Sustained low values — smoothing must eventually track
        for s in [20, 20, 20]:
            last = pulse._smoothed_score(s)
        # Window: [90, 20, 20, 20, 20] → median 20
        self.assertEqual(last, 20)

    def test_smoothed_none_not_recorded(self):
        pulse._reset_history()
        self.assertIsNone(pulse._smoothed_score(None))
        # History still empty
        with pulse._history_lock:
            self.assertEqual(len(pulse._score_history), 0)

    def test_smoothed_history_bounded(self):
        pulse._reset_history()
        for s in range(100):
            pulse._smoothed_score(50)
        with pulse._history_lock:
            self.assertLessEqual(len(pulse._score_history), pulse.SCORE_HISTORY_LEN)

    # --- _latency_percentiles --------------------------------------------
    def test_percentiles_empty(self):
        self.assertEqual(pulse._latency_percentiles(), (None, None))

    def test_percentiles_below_min_samples(self):
        pulse._record_latency(100)
        pulse._record_latency(200)
        self.assertEqual(pulse._latency_percentiles(), (None, None))

    def test_percentiles_basic(self):
        for v in [100, 200, 300, 400, 500]:
            pulse._record_latency(v)
        p50, p95 = pulse._latency_percentiles()
        self.assertEqual(p50, 300)
        self.assertIsNotNone(p95)
        self.assertGreaterEqual(p95, p50)

    def test_percentiles_skips_none(self):
        for v in [100, None, 200, None, 300]:
            pulse._record_latency(v)
        p50, _ = pulse._latency_percentiles()
        self.assertEqual(p50, 200)  # median of [100, 200, 300]

    def test_percentiles_history_bounded(self):
        for v in range(200):
            pulse._record_latency(v)
        with pulse._history_lock:
            self.assertLessEqual(len(pulse._latency_history), pulse.LATENCY_HISTORY_LEN)


class TestPulseLog(unittest.TestCase):
    """Tests for pulse.jsonl persistence + hybrid cleanup."""

    def setUp(self):
        # Redirect LOG_PATH + DATA_DIR into a temp dir to avoid touching real state
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data_dir = pulse.DATA_DIR
        self._orig_log_path = pulse.LOG_PATH
        pulse.DATA_DIR = __import__("pathlib").Path(self._tmp.name)
        pulse.LOG_PATH = pulse.DATA_DIR / "pulse.jsonl"
        # Reset write counter
        pulse._write_counter = 0

    def tearDown(self):
        pulse.DATA_DIR = self._orig_data_dir
        pulse.LOG_PATH = self._orig_log_path
        self._tmp.cleanup()

    def _write_record(self, ts, score=50, level="degraded"):
        """Directly append one record with specific ts (bypasses _append_log)."""
        rec = {"ts": ts, "score": score, "level": level,
               "indicator": None, "incidents": 0,
               "latency_ms": 100, "error": None}
        with open(pulse.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    # --- append ---------------------------------------------------------
    def test_append_creates_file(self):
        snap = {"wall_t": time.time(), "score": 95, "level": "ok",
                "indicator": "none", "incidents": [], "latency_ms": 120,
                "error": None}
        pulse._append_log(snap)
        self.assertTrue(pulse.LOG_PATH.exists())
        with open(pulse.LOG_PATH, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["score"], 95)
        self.assertEqual(rec["incidents"], 0)  # list len, not the list

    def test_append_is_line_delimited(self):
        for i in range(5):
            pulse._append_log({"wall_t": time.time(), "score": i, "level": "ok",
                                "indicator": None, "incidents": [],
                                "latency_ms": 100, "error": None})
        with open(pulse.LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 5)

    def test_append_survives_bad_snap(self):
        # Non-serializable object must not raise; file must not be created
        class Bad:
            pass
        try:
            pulse._append_log({"wall_t": time.time(), "score": Bad(),
                               "level": "ok", "indicator": None,
                               "incidents": [], "latency_ms": 100, "error": None})
        except Exception as e:
            self.fail(f"_append_log raised unexpectedly: {e}")
        self.assertFalse(pulse.LOG_PATH.exists(),
                         "LOG_PATH must not be created when serialization fails")

    # --- startup cleanup ------------------------------------------------
    def test_cleanup_drops_old_entries(self):
        now = time.time()
        self._write_record(now - 48 * 3600)       # 48h ago — drop
        self._write_record(now - 25 * 3600)       # 25h ago — drop
        self._write_record(now - 1 * 3600)        # 1h ago — keep
        self._write_record(now - 60)              # 1min ago — keep
        pulse.cleanup_log_startup()
        with open(pulse.LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)

    def test_cleanup_caps_record_count(self):
        now = time.time()
        # Write 2500 fresh records
        with open(pulse.LOG_PATH, "a", encoding="utf-8") as f:
            for i in range(2500):
                rec = {"ts": now - i, "score": 80, "level": "ok",
                       "indicator": None, "incidents": 0,
                       "latency_ms": 100, "error": None}
                f.write(json.dumps(rec) + "\n")
        pulse.cleanup_log_startup()
        with open(pulse.LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), pulse.LOG_STARTUP_CAP)

    def test_cleanup_missing_file_noop(self):
        # No file exists — must not raise
        pulse.cleanup_log_startup()
        self.assertFalse(pulse.LOG_PATH.exists())

    def test_cleanup_malformed_lines_skipped(self):
        now = time.time()
        with open(pulse.LOG_PATH, "a", encoding="utf-8") as f:
            f.write("not json\n")
            f.write(json.dumps({"ts": now, "score": 90}) + "\n")
            f.write("{broken\n")
        pulse.cleanup_log_startup()
        with open(pulse.LOG_PATH, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 1)

    # --- runtime rotation -----------------------------------------------
    def test_rotate_trims_when_over_max(self):
        # Write > LOG_MAX_BYTES worth of records
        big = {"ts": time.time(), "score": 50, "level": "ok",
               "indicator": "none", "incidents": 0,
               "latency_ms": 100,
               "error": "x" * 200}  # fat records to reach 1MB faster
        with open(pulse.LOG_PATH, "a", encoding="utf-8") as f:
            line = json.dumps(big) + "\n"
            # Exceed 1 MB
            for _ in range(pulse.LOG_MAX_BYTES // len(line) + 100):
                f.write(line)
        self.assertGreater(pulse.LOG_PATH.stat().st_size, pulse.LOG_MAX_BYTES)
        # Trigger rotation via counter
        pulse._write_counter = pulse.ROTATE_CHECK_EVERY - 1
        pulse._maybe_rotate_log()
        with open(pulse.LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), pulse.LOG_TRIM_TARGET)
        self.assertLess(pulse.LOG_PATH.stat().st_size, pulse.LOG_MAX_BYTES)

    def test_rotate_only_every_n_writes(self):
        # Under size — rotate must not fire
        self._write_record(time.time())
        size_before = pulse.LOG_PATH.stat().st_size
        for _ in range(pulse.ROTATE_CHECK_EVERY - 1):
            pulse._maybe_rotate_log()
        # Still one record
        with open(pulse.LOG_PATH, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 1)
        self.assertEqual(pulse.LOG_PATH.stat().st_size, size_before)

    def test_rotate_noop_under_max_size(self):
        self._write_record(time.time())
        # Force the counter to threshold
        pulse._write_counter = pulse.ROTATE_CHECK_EVERY - 1
        pulse._maybe_rotate_log()
        with open(pulse.LOG_PATH, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_rotate_noop_at_exact_max_size(self):
        # Exactly LOG_MAX_BYTES must NOT trigger rotation (uses <= guard)
        pad_line = "x" * (pulse.LOG_MAX_BYTES - 2) + "\n"
        with open(pulse.LOG_PATH, "w", encoding="utf-8") as f:
            f.write(pad_line)
        size = pulse.LOG_PATH.stat().st_size
        # Close enough to threshold — adjust to land exactly on it
        if size < pulse.LOG_MAX_BYTES:
            with open(pulse.LOG_PATH, "a", encoding="utf-8") as f:
                f.write("y" * (pulse.LOG_MAX_BYTES - size))
        self.assertEqual(pulse.LOG_PATH.stat().st_size, pulse.LOG_MAX_BYTES)
        pulse._write_counter = pulse.ROTATE_CHECK_EVERY - 1
        pulse._maybe_rotate_log()
        # Still at threshold, not trimmed
        self.assertEqual(pulse.LOG_PATH.stat().st_size, pulse.LOG_MAX_BYTES)


# ---------------------------------------------------------------------------
# TestPulseSummaryURL — regression guard for SUMMARY_URL
# ---------------------------------------------------------------------------
class TestPulseSummaryURL(unittest.TestCase):

    def test_url_is_status_claude_com(self):
        self.assertEqual(pulse.SUMMARY_URL, "https://status.claude.com/api/v2/summary.json")


# ---------------------------------------------------------------------------
# TestTagModelsFromIncident — edge cases for _tag_models_from_incident
# ---------------------------------------------------------------------------
class TestTagModelsFromIncidentEdgeCases(unittest.TestCase):

    def test_components_non_dict_items_ignored(self):
        inc = {"name": "x", "components": [42, "str", None, {"name": "Claude Opus"}]}
        result = pulse._tag_models_from_incident(inc)
        self.assertEqual(result, ["opus"])

    def test_components_name_none_falls_back_to_title(self):
        inc = {"name": "opus issue", "components": [{"name": None}]}
        result = pulse._tag_models_from_incident(inc)
        self.assertEqual(result, ["opus"])

    def test_components_multi_family_in_one_name(self):
        inc = {"name": "x", "components": [{"name": "Claude Opus and Sonnet API"}]}
        result = pulse._tag_models_from_incident(inc)
        self.assertEqual(result, ["opus", "sonnet"])


# ---------------------------------------------------------------------------
# TestWriteSharedStateReservedSid — reserved SIDs must never create files
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
        proj_dir = pathlib.Path(self._tmpdir) / "proj1"
        proj_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "assistant",
            "timestamp": "2026-04-17T10:00:00Z",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"cache_read_input_tokens": 500},
            },
        }
        (proj_dir / "sess_cache_only.jsonl").write_text(
            _json.dumps(record) + "\n", encoding="utf-8"
        )
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


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
