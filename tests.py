#!/usr/bin/env python3
"""Unit tests for CC AIO MON — stdlib only, no pytest required.

Run:
    python tests.py
"""

import os
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
    truncate, vlen, mkbar,
    calc_cross_session_costs,
    _parse_ts, _calc_streaks, _model_label,
    scan_transcript_stats, render_stats, render_legend, render_frame,
    _CLAUDE_DIR, _usage_cache,
    WARN_BRN, BRN_MAX, CTR_MAX, CST_MAX,
    BAR_W,
    _parse_version, _rls_cache, _rls_blink, VERSION,
    _rls_check_worker, _rls_maybe_check, _RLS_TTL,
    spin_session, spin_rls, _SPIN_SESSION, _SPIN_RLS,
    _git_cmd, _update_checks, _get_new_commits,
    _get_remote_changelog_preview, _apply_update_action,
    render_update_modal,
    _RESERVED_FILES, list_sessions, load_state, load_history, DATA_DIR,
    render_picker,
)
from shared import (
    MAX_FILE_SIZE, _ANSI_RE, _ANSI_RE as M_ANSI_RE,
    _sanitize,
    C_RED, C_GRN, C_YEL, C_ORN, C_CYN, C_WHT, C_DIM,
)
# M_* aliases for backward compat with existing test assertions
M_RED = C_RED; M_YEL = C_YEL; M_GRN = C_GRN; M_DIM = C_DIM; M_ORN = C_ORN

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
        self.assertEqual(_limit_color(20), M_YEL)

    def test_mid_usage_yellow(self):
        self.assertEqual(_limit_color(55), M_YEL)

    def test_high_usage_red(self):
        self.assertEqual(_limit_color(85), M_RED)


class TestResetColor(unittest.TestCase):

    def test_lots_of_time_red(self):
        # Reset in 4h out of 5h window = 80% remaining → red (far from reset)
        self.assertEqual(_reset_color(time.time() + 14400, 18000), M_RED)

    def test_some_time_yellow(self):
        # Reset in 1.5h out of 5h window = 30% remaining → yellow
        self.assertEqual(_reset_color(time.time() + 5400, 18000), M_YEL)

    def test_little_time_green(self):
        # Reset in 15min out of 5h window = 5% remaining → green (close to reset)
        self.assertEqual(_reset_color(time.time() + 900, 18000), M_GRN)

    def test_just_reset_green(self):
        # Reset epoch in the past → just reset
        self.assertEqual(_reset_color(time.time() - 10, 18000), M_GRN)

    def test_no_data_dim(self):
        self.assertEqual(_reset_color(0, 18000), M_DIM)


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

    def test_brn_max_positive(self):
        self.assertGreater(BRN_MAX, 0)

    def test_ctr_max_positive(self):
        self.assertGreater(CTR_MAX, 0)

    def test_cst_max_positive(self):
        self.assertGreater(CST_MAX, 0)


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
# truncate / vlen
# ---------------------------------------------------------------------------
class TestVlen(unittest.TestCase):

    def test_plain_text(self):
        self.assertEqual(vlen("hello"), 5)

    def test_ansi_ignored(self):
        s = f"\033[31mred\033[0m"
        self.assertEqual(vlen(s), 3)

    def test_empty(self):
        self.assertEqual(vlen(""), 0)


class TestTruncate(unittest.TestCase):

    def test_short_unchanged(self):
        self.assertEqual(truncate("abc", 10), "abc")

    def test_exact_length(self):
        self.assertEqual(vlen(truncate("abcde", 5)), 5)

    def test_truncates_long(self):
        result = truncate("abcdefghij", 5)
        self.assertEqual(vlen(result), 5)

    def test_preserves_ansi(self):
        s = f"\033[31mhello world\033[0m"
        result = truncate(s, 5)
        self.assertEqual(vlen(result), 5)
        self.assertIn("hello", result)

    def test_zero_width(self):
        result = truncate("abc", 0)
        self.assertEqual(vlen(result), 0)


# ---------------------------------------------------------------------------
# mkbar
# ---------------------------------------------------------------------------
class TestMkbar(unittest.TestCase):

    def test_zero_percent(self):
        result = mkbar(0)
        plain = M_ANSI_RE.sub("", result)
        self.assertIn("0.0", plain)
        self.assertIn("[", plain)
        self.assertIn("]", plain)

    def test_100_percent(self):
        result = mkbar(100)
        plain = M_ANSI_RE.sub("", result)
        self.assertIn("100.0", plain)

    def test_clamps_negative(self):
        result = mkbar(-10)
        plain = M_ANSI_RE.sub("", result)
        self.assertIn("0.0", plain)

    def test_clamps_over_100(self):
        result = mkbar(150)
        plain = M_ANSI_RE.sub("", result)
        self.assertIn("100.0", plain)

    def test_green_under_50(self):
        result = mkbar(30)
        self.assertIn(M_GRN, result)

    def test_yellow_50_79(self):
        result = mkbar(60)
        self.assertIn(M_YEL, result)

    def test_red_over_80(self):
        result = mkbar(90)
        self.assertIn(M_RED, result)

    def test_custom_color(self):
        result = mkbar(30, M_ORN)
        self.assertIn(M_ORN, result)

    def test_visual_width(self):
        result = mkbar(50)
        plain = M_ANSI_RE.sub("", result)
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
        # Patch tempfile.gettempdir in statusline module
        import statusline
        self._orig_gettempdir = statusline.tempfile.gettempdir
        statusline.tempfile.gettempdir = lambda: self.tmpdir
        statusline._DATA_DIR = self._base

    def tearDown(self):
        import shutil, statusline
        statusline.tempfile.gettempdir = self._orig_gettempdir
        statusline._DATA_DIR = statusline.pathlib.Path(
            self._orig_gettempdir()) / "claude-aio-monitor"
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

    def test_known_sonnet(self):
        self.assertEqual(_model_label("claude-sonnet-4-6"), "Sonnet 4.6")

    def test_known_haiku(self):
        self.assertEqual(_model_label("claude-haiku-4-5-20251001"), "Haiku 4.5")

    def test_unknown_passthrough(self):
        self.assertEqual(_model_label("claude-future-99"), "claude-future-99")


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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Opus 4.6", plain)
        self.assertIn("100.0", plain)  # 100% single model
        self.assertIn("Total", plain)

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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("SES", plain)
        self.assertIn("DAY", plain)
        self.assertIn("STK", plain)
        self.assertIn("LSS", plain)

    def test_period_labels(self):
        buf_all = render_stats(80, 24, "all")
        buf_7d = render_stats(80, 24, "7d")
        buf_30d = render_stats(80, 24, "30d")
        self.assertIn("All Time", M_ANSI_RE.sub("", "\n".join(buf_all)))
        self.assertIn("Last 7 Days", M_ANSI_RE.sub("", "\n".join(buf_7d)))
        self.assertIn("Last 30 Days", M_ANSI_RE.sub("", "\n".join(buf_30d)))

    def test_footer_has_keys(self):
        buf = render_stats(80, 24, "all")
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("1", plain)
        self.assertIn("2", plain)
        self.assertIn("3", plain)
        self.assertIn("close", plain)


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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        for label in ["APR", "CHR", "CTX", "5HL", "7DL", "BRN", "CTR", "CST",
                       "TDY", "WEK", "LNS", "NOW", "UPD", "RLS"]:
            self.assertIn(label, plain)

    def test_contains_usage_stats_section(self):
        buf = render_legend(80, 60)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        for label in ["SES", "DAY", "STK", "LSS", "TOP"]:
            self.assertIn(label, plain)

    def test_contains_keys(self):
        buf = render_legend(80, 60)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        for key in ["q", "r", "s", "u", "l", "1-9"]:
            self.assertIn(key, plain)


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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Inactive", plain)

    def test_legend_mode(self):
        buf = render_frame(_full_data(), [], 80, 50, show_legend=True)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("LEGEND", plain)

    def test_footer_has_keys(self):
        buf = render_frame(_full_data(), [], 80, 35)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("[t]tk", plain)
        self.assertIn("[u]up", plain)


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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("RLS", plain)
        self.assertIn("Up to date", plain)

    def test_rls_update_available(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "update", "remote_ver": "9.9.9"})
        monitor._rls_blink_on = True
        monitor._rls_blink_last = time.monotonic()
        buf = render_frame(_full_data(), [], 80, 35)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("RLS", plain)
        self.assertIn("v9.9.9 available", plain)

    def test_rls_error_silent(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": "error", "remote_ver": None})
        buf = render_frame(_full_data(), [], 80, 35)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertNotIn("RLS", plain)

    def test_rls_checking(self):
        import monitor
        monitor._rls_cache.update({"t": time.monotonic(), "status": None, "remote_ver": None})
        buf = render_frame(_full_data(), [], 80, 35)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("RLS", plain)
        self.assertIn("Checking", plain)

    def test_legend_contains_rls(self):
        buf = render_legend(80, 60)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
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
        self._orig_fetching = _monitor_mod._rls_fetching
        # Start each test as if fetching is in progress (worker sets it False in finally)
        _monitor_mod._rls_fetching = True

    def tearDown(self):
        _rls_cache.update(self._orig_cache)
        _monitor_mod._rls_fetching = self._orig_fetching

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
        self.assertFalse(_monitor_mod._rls_fetching)

    # ------------------------------------------------------------------
    # b. git show fails → status "error"
    # ------------------------------------------------------------------
    def test_show_fail_sets_error(self):
        with patch("monitor.subprocess.run", side_effect=self._make_run(fetch_rc=0, show_rc=1)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "error")
        self.assertIsNone(_rls_cache["remote_ver"])
        self.assertFalse(_monitor_mod._rls_fetching)

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
        self.assertFalse(_monitor_mod._rls_fetching)

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
        self.assertFalse(_monitor_mod._rls_fetching)

    # ------------------------------------------------------------------
    # e. Remote version == local → status "ok"
    # ------------------------------------------------------------------
    def test_remote_same_sets_ok(self):
        stdout = f'VERSION = "{VERSION}"\n'
        with patch("monitor.subprocess.run", side_effect=self._make_run(show_stdout=stdout)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "ok")
        self.assertEqual(_rls_cache["remote_ver"], VERSION)
        self.assertFalse(_monitor_mod._rls_fetching)

    # ------------------------------------------------------------------
    # f. FileNotFoundError (no git binary) → status "no_git"
    # ------------------------------------------------------------------
    def test_no_git_sets_no_git(self):
        with patch("monitor.subprocess.run", side_effect=FileNotFoundError):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "no_git")
        self.assertIsNone(_rls_cache["remote_ver"])
        self.assertFalse(_monitor_mod._rls_fetching)

    # ------------------------------------------------------------------
    # g. TimeoutExpired → status "timeout"
    # ------------------------------------------------------------------
    def test_timeout_sets_timeout(self):
        with patch("monitor.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="git", timeout=15)):
            _rls_check_worker()
        self.assertEqual(_rls_cache["status"], "timeout")
        self.assertIsNone(_rls_cache["remote_ver"])
        self.assertFalse(_monitor_mod._rls_fetching)


class TestRlsMaybeCheck(unittest.TestCase):
    """Tests for _rls_maybe_check() — verifies thread spawning decisions."""

    def setUp(self):
        self._orig_cache = dict(_rls_cache)
        self._orig_fetching = _monitor_mod._rls_fetching
        _monitor_mod._rls_fetching = False
        # Expire the cache so TTL check would normally pass
        # Note: t=0.0 may NOT be expired on fresh CI runners where monotonic() < _RLS_TTL
        _rls_cache.update({"t": time.monotonic() - _RLS_TTL - 1, "status": None, "remote_ver": None})
        # Remove the env var if set
        self._env_var_was_set = "CC_AIO_MON_NO_UPDATE_CHECK" in os.environ
        os.environ.pop("CC_AIO_MON_NO_UPDATE_CHECK", None)

    def tearDown(self):
        _rls_cache.update(self._orig_cache)
        _monitor_mod._rls_fetching = self._orig_fetching
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
        self.assertFalse(_monitor_mod._rls_fetching)

    # ------------------------------------------------------------------
    # b. _rls_fetching=True → no thread spawned
    # ------------------------------------------------------------------
    def test_already_fetching_skips(self):
        _monitor_mod._rls_fetching = True
        with patch("monitor.threading.Thread") as mock_thread:
            _rls_maybe_check()
        mock_thread.assert_not_called()
        # Still True — we didn't change it
        self.assertTrue(_monitor_mod._rls_fetching)

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
        _monitor_mod._rls_fetching = False
        _rls_cache.update({"t": time.monotonic() - _RLS_TTL - 1, "status": None, "remote_ver": None})
        mock_thread_instance = MagicMock()
        with patch("monitor.threading.Thread", return_value=mock_thread_instance) as mock_thread_cls:
            _rls_maybe_check()
        mock_thread_cls.assert_called_once_with(
            target=_rls_check_worker, daemon=True
        )
        mock_thread_instance.start.assert_called_once()
        # _rls_fetching must be True after spawning
        self.assertTrue(_monitor_mod._rls_fetching)


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
        import subprocess as sp
        pull_ok = sp.CompletedProcess(args=["git"], returncode=0, stdout="ok\n", stderr="")
        compile_ok = sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        def _fake_subprocess_run(cmd, **kw):
            # py_compile calls come through subprocess.run directly
            return compile_ok

        with patch("monitor._git_cmd", return_value=(0, "ok", "")):
            with patch("monitor.subprocess.run", return_value=compile_ok):
                _apply_update_action()

        import monitor
        self.assertIn("complete", monitor._update_result)

    def test_failure(self):
        with patch("monitor._git_cmd", return_value=(1, "", "conflict")):
            _apply_update_action()

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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn("Up to date", plain)

    def test_update_available(self):
        import monitor
        remote_ver = "9.9.9"
        monitor._rls_cache.update({"t": time.monotonic(), "status": "update", "remote_ver": remote_ver})
        monitor._update_result = None
        with patch("monitor._get_new_commits", return_value=["abc new feature"]):
            with patch("monitor._update_checks", return_value=[]):
                with patch("monitor._get_remote_changelog_preview", return_value=[]):
                    buf = render_update_modal(80, 40)
        plain = M_ANSI_RE.sub("", "\n".join(buf))
        self.assertIn(remote_ver, plain)


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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
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
        plain = M_ANSI_RE.sub("", "\n".join(buf))
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

    def test_reserved_is_a_set(self):
        self.assertIsInstance(_RESERVED_FILES, (set, frozenset))


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
