#!/usr/bin/env python3
"""Unit tests for CC AIO MON — stdlib only, no pytest required.

Run:
    python tests.py
"""

import re
import sys
import time
import unittest

import rates

# Import target functions directly
from monitor import (
    _fit_buf_height, calc_rates, f_tok, f_cost, f_dur, f_cd, _num,
    _limit_color, _reset_color, collect_warnings,
    WARN_BRN, BRN_MAX, CTR_MAX, CST_MAX,
    C_RED as M_RED, C_YEL as M_YEL, C_GRN as M_GRN, C_DIM as M_DIM,
    C_ORN as M_ORN,
)

from statusline import (
    _ANSI_RE,
    BG_BAR,
    R,
    RB,
    EL,
    C_GRN,
    C_YEL,
    C_RED,
    C_ORN,
    C_DIM,
    _get_terminal_width,
    _sanitize,
    _num as sl_num,
    _calc_rates as sl_calc_rates,
    f_dur as sl_f_dur,
    f_tok as sl_f_tok,
    f_cost as sl_f_cost,
    seg_model,
    seg_ctx,
    seg_5hl,
    seg_7dl,
    seg_cost,
    seg_dur,
    seg_chr,
    seg_brn,
    seg_ctr,
    seg_apr,
    seg_ctf,
    seg_now,
    build_line,
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
        self.assertEqual(buf[-1], "29")

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

    def test_lots_of_time_green(self):
        # Reset in 4h out of 5h window = 80% remaining → green
        self.assertEqual(_reset_color(time.time() + 14400, 18000), M_GRN)

    def test_some_time_yellow(self):
        # Reset in 1.5h out of 5h window = 30% remaining → yellow
        self.assertEqual(_reset_color(time.time() + 5400, 18000), M_YEL)

    def test_little_time_red(self):
        # Reset in 15min out of 5h window = 5% remaining → red
        self.assertEqual(_reset_color(time.time() + 900, 18000), M_RED)

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
        self.assertIs(m_cr, rates.calc_rates)
        self.assertIs(sl_calc_rates, rates.calc_rates)


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


class TestSegDur(unittest.TestCase):

    def test_basic(self):
        text, vl = seg_dur(_full_data())
        self.assertEqual(vl, _vlen(text))
        self.assertIn("DUR", _ANSI_RE.sub("", text))
        self.assertIn("2m", _ANSI_RE.sub("", text))

    def test_uses_dim(self):
        text, _ = seg_dur(_full_data())
        self.assertIn(C_DIM, text)

    def test_zero_duration(self):
        d = _full_data()
        d["cost"]["total_duration_ms"] = 0
        self.assertIsNone(seg_dur(d))


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


class TestSegCtr(unittest.TestCase):

    def test_basic(self):
        text, vl = seg_ctr(1.5)
        self.assertEqual(vl, _vlen(text))
        self.assertIn("CTR", _ANSI_RE.sub("", text))

    def test_none(self):
        self.assertIsNone(seg_ctr(None))


class TestSegCtf(unittest.TestCase):

    def test_basic(self):
        d = _full_data()
        text, vl = seg_ctf(2.0, d)
        self.assertEqual(vl, _vlen(text))
        self.assertIn("CTF", _ANSI_RE.sub("", text))

    def test_none_ctr(self):
        self.assertIsNone(seg_ctf(None, _full_data()))

    def test_full_context(self):
        d = _full_data()
        d["context_window"]["used_percentage"] = 100
        self.assertIsNone(seg_ctf(2.0, d))



class TestSegNow(unittest.TestCase):

    def test_returns_time(self):
        text, vl = seg_now()
        self.assertEqual(vl, _vlen(text))
        plain = _ANSI_RE.sub("", text)
        self.assertIn("NOW", plain)
        self.assertRegex(plain, r"\d{2}:\d{2}:\d{2}")

    def test_uses_dim(self):
        text, _ = seg_now()
        self.assertIn(C_DIM, text)


# ---------------------------------------------------------------------------
# build_line
# ---------------------------------------------------------------------------
class TestBuildLine(unittest.TestCase):

    def test_basic_output(self):
        line = build_line(_full_data(), 120)
        self.assertIsNotNone(line)
        self.assertGreater(len(line), 0)

    def test_visual_width_matches_cols_wide(self):
        # When right segments fit, spacer pads to exact cols
        for cols in (120, 200, 300):
            line = build_line(_full_data(), cols)
            vl = _vlen(line)
            self.assertEqual(vl, cols, f"cols={cols}, got vl={vl}")

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
# RB — bar background persistence
# ---------------------------------------------------------------------------
class TestBarBackgroundPersistence(unittest.TestCase):
    """Verify that BG_BAR is never killed inside the bar line.

    The fix: segments and SEP use RB (reset + re-apply bg) instead of R
    (full reset). Every bare R inside the bar would create a gap in the
    background color. Only the final R after EL is allowed.
    """

    def test_no_bare_reset_inside_line(self):
        line = build_line(_full_data(), 200)
        full = f"{BG_BAR}{line}{EL}{R}"
        # Pattern: \033[0m NOT followed by \033[48;2;46;52;64m
        bare_reset = re.compile(r"\033\[0m(?!\033\[48;2;46;52;64m)")
        matches = bare_reset.findall(full)
        # Exactly 1 bare reset: the final R after EL
        self.assertEqual(len(matches), 1,
                         f"Expected 1 bare reset (final), found {len(matches)}")

    def test_rb_count_matches_segments(self):
        line = build_line(_full_data(), 200)
        rb_pattern = re.compile(r"\033\[0m\033\[48;2;46;52;64m")
        rb_count = len(rb_pattern.findall(line))
        # At least a few RB resets (segments + separators)
        self.assertGreater(rb_count, 5)

    def test_el_fires_with_bg_active(self):
        """After all segments, the last escape before EL should be BG_BAR (via RB)."""
        line = build_line(_full_data(), 200)
        full = f"{BG_BAR}{line}{EL}"
        # Find the last \033[...m before EL (\033[K)
        # The line ends with RB (from last segment), then spacer (spaces), then EL
        # BG_BAR should be active (last color set contains 48;2;46;52;64)
        idx_el = full.rfind("\033[K")
        before_el = full[:idx_el]
        # Find last SGR sequence before EL
        last_sgr = list(re.finditer(r"\033\[[0-9;]*m", before_el))
        self.assertTrue(len(last_sgr) > 0)
        last_escape = last_sgr[-1].group()
        self.assertIn("48;2;46;52;64", last_escape,
                       "BG_BAR must be active when EL fires")


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

    def test_5hl_above_80(self):
        data = _full_data()
        data["rate_limits"]["five_hour"]["used_percentage"] = 85
        warns = collect_warnings(data, 0.01, 0.1)
        self.assertTrue(any("5HL" in w for w in warns))

    def test_7dl_above_80(self):
        data = _full_data()
        data["rate_limits"]["seven_day"]["used_percentage"] = 90
        warns = collect_warnings(data, 0.01, 0.1)
        self.assertTrue(any("7DL" in w for w in warns))

    def test_brn_above_threshold(self):
        warns = collect_warnings(_full_data(), WARN_BRN + 0.01, 0.1)
        self.assertTrue(any("BRN" in w for w in warns))

    def test_brn_below_threshold(self):
        warns = collect_warnings(_full_data(), 0.01, 0.1)
        self.assertFalse(any("BRN" in w for w in warns))

    def test_expired_reset_no_warn(self):
        data = _full_data()
        data["rate_limits"]["five_hour"]["used_percentage"] = 90
        data["rate_limits"]["five_hour"]["resets_at"] = 1000  # far past
        warns = collect_warnings(data, 0.01, 0.1)
        self.assertFalse(any("5HL" in w for w in warns))

    def test_multiple_warnings(self):
        data = _full_data()
        data["context_window"]["used_percentage"] = 95
        data["rate_limits"]["five_hour"]["used_percentage"] = 85
        warns = collect_warnings(data, WARN_BRN + 0.5, 5.0)  # CTF + 5HL + BRN
        self.assertGreaterEqual(len(warns), 3)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
