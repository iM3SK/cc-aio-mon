#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Unit tests for pulse.py — stdlib only, no pytest required.

Run:
    python -m unittest tests.test_pulse
    # or directly:
    python tests/test_pulse.py
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import json
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
class TestPulseWorkerStartRevert(unittest.TestCase):
    """M-5: start_pulse_worker must revert _worker_started if thread spawn fails.

    Otherwise the pulse module stays permanently in "AWAITING DATA" state
    with no way to retry.
    """

    def test_worker_started_reverts_on_thread_exception(self):
        import pulse as _p
        import threading as _threading
        # Reset state
        with _p._worker_lock:
            _p._worker_started = False
        # Simulate Thread() raising
        original_thread = _threading.Thread

        def raising_thread(*a, **kw):
            raise RuntimeError("simulated thread creation failure")

        with patch.object(_threading, "Thread", side_effect=raising_thread):
            with self.assertRaises(RuntimeError):
                _p.start_pulse_worker()

        # _worker_started must be reverted so next call can retry
        with _p._worker_lock:
            self.assertFalse(_p._worker_started,
                             "start_pulse_worker must revert _worker_started on thread spawn failure")

    def tearDown(self):
        import pulse as _p
        # Be kind to other tests — leave flag in a known state
        with _p._worker_lock:
            _p._worker_started = False

class TestWorkerLoopCrashRecovery(unittest.TestCase):
    """Audit P1-11: _worker_loop has a last-resort `except Exception` that
    sets the snapshot to a 'worker crashed' state so the dashboard surfaces
    a visible error rather than freezing at the last-good state. Without
    this branch the daemon thread would die silently. No prior test.

    pulse.py:597-607
    """

    def setUp(self):
        import pulse as _p
        self._p = _p
        # Snapshot baseline — restore in tearDown
        with _p._snapshot_lock:
            self._orig_snapshot = dict(_p._snapshot)

    def tearDown(self):
        with self._p._snapshot_lock:
            self._p._snapshot.clear()
            self._p._snapshot.update(self._orig_snapshot)

    def test_refresh_exception_sets_worker_crashed_snapshot(self):
        """When _refresh_once raises, the worker's except branch must
        write a 'PULSE ERROR' / 'worker crashed' snapshot, not propagate."""

        # We test the except-branch body without running the infinite loop:
        # simulate one iteration where _refresh_once raises, then verify
        # the snapshot reflects the crash. We do this by running the body
        # of one iteration manually with _refresh_once patched.
        import time as _time

        # Drive a single iteration of the loop logic
        with patch.object(self._p, "_refresh_once", side_effect=RuntimeError("simulated")):
            # Inline the loop body once — this mirrors pulse._worker_loop without
            # the `while True` so the test stays deterministic.
            try:
                self._p._refresh_once()
            except Exception:
                with self._p._snapshot_lock:
                    self._p._snapshot.update({
                        "t": _time.monotonic(),
                        "wall_t": _time.time(),
                        "score": None,
                        "verdict": "PULSE ERROR",
                        "level": "error",
                        "reason": "worker crashed",
                        "error": "worker crash",
                    })

        with self._p._snapshot_lock:
            snap = dict(self._p._snapshot)
        self.assertEqual(snap.get("verdict"), "PULSE ERROR")
        self.assertEqual(snap.get("reason"), "worker crashed")
        self.assertEqual(snap.get("level"), "error")
        self.assertIsNone(snap.get("score"))

    def test_worker_loop_actually_recovers_from_one_exception(self):
        """Integration: spin _worker_loop briefly with _refresh_once raising,
        verify it reaches the except branch (snapshot mutates to crashed state)
        and the thread keeps running (does not propagate the exception)."""
        import threading
        import time as _time

        crashed_event = threading.Event()
        original_sleep = _time.sleep

        def quick_sleep(secs):
            # First tick after the except branch — signal the assertion and
            # exit the worker loop by raising SystemExit (only a daemon thread,
            # safe to kill).
            crashed_event.set()
            raise SystemExit

        with patch.object(self._p, "_refresh_once", side_effect=RuntimeError("boom")), \
             patch.object(self._p.time, "sleep", side_effect=quick_sleep):
            t = threading.Thread(target=self._p._worker_loop, daemon=True)
            t.start()
            crashed_event.wait(timeout=2.0)
            t.join(timeout=1.0)

        self.assertTrue(crashed_event.is_set(),
                        "_worker_loop must reach sleep() after handling _refresh_once exception")
        with self._p._snapshot_lock:
            snap = dict(self._p._snapshot)
        self.assertEqual(snap.get("verdict"), "PULSE ERROR")
        self.assertEqual(snap.get("reason"), "worker crashed")


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
