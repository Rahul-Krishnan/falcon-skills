#!/usr/bin/env python3
"""Tests for analyze_results.py --triage deterministic failure classification."""

from __future__ import annotations

import json
import contextlib
import io
import os
import tempfile
import unittest

from analyze_results import analyze, classify_failure, triage
from hone_common import DIMENSION_FLOOR


class TestClassifyFailure(unittest.TestCase):
    """Test the deterministic failure classification logic."""

    def test_all_zero_is_criteria_bug(self):
        result = classify_failure(0.0, [0.0, 0.0, 0.0])
        self.assertEqual(result, "criteria_bug")

    def test_single_zero_with_others_passing_is_variance(self):
        result = classify_failure(0.0, [0.0, 0.8, 0.9])
        self.assertEqual(result, "variance")

    def test_floor_score_with_others_passing_is_variance(self):
        """A deterministic composite bottoms out at DIMENSION_FLOOR, not 0.0.

        Written against an exact 0.0, this band was dead in the documented
        primary mode.
        """
        floor = DIMENSION_FLOOR
        self.assertEqual(classify_failure(floor, [floor, 0.8, 0.9]), "variance")

    def test_all_at_floor_is_criteria_bug(self):
        floor = DIMENSION_FLOOR
        self.assertEqual(classify_failure(floor, [floor] * 3), "criteria_bug")

    def test_float_noise_at_floor_still_counts_as_floor(self):
        """0.05 ** 1 is 0.049999999999999996 before rounding."""
        noisy = 0.05 ** 1.0
        self.assertEqual(classify_failure(noisy, [noisy, 0.9]), "variance")

    def test_low_score_is_real_issue(self):
        result = classify_failure(0.3, [0.3, 0.8, 0.9])
        self.assertEqual(result, "real_issue")

    def test_passing_score(self):
        result = classify_failure(0.8, [0.8, 0.9, 0.7])
        self.assertEqual(result, "pass")

    def test_zero_with_no_high_scores_is_criteria_bug(self):
        """When all scores are below 0.5, uniformly low = criteria_bug."""
        result = classify_failure(0.0, [0.0, 0.3, 0.2])
        self.assertEqual(result, "criteria_bug")

    def test_actionable_threshold_boundary(self):
        """0.8 is the Phase 1 exit gate: at or above it is passing."""
        result = classify_failure(0.8, [0.8, 0.9, 0.85])
        self.assertEqual(result, "pass")

    def test_below_actionable_threshold_is_real_issue(self):
        """A 0.637 test is a real_issue, matching the gate's below-0.8 rule."""
        result = classify_failure(0.637, [0.637, 0.9, 1.0])
        self.assertEqual(result, "real_issue")

    def test_just_below_threshold(self):
        result = classify_failure(0.49, [0.49, 0.6, 0.7])
        self.assertEqual(result, "real_issue")

    def test_all_low_but_nonzero_is_criteria_bug(self):
        """All scores uniformly low (0.1-0.4) = criteria_bug, not real_issue."""
        result = classify_failure(0.1, [0.1, 0.2, 0.3, 0.4])
        self.assertEqual(result, "criteria_bug")

    def test_single_low_score_is_real_issue_not_criteria_bug(self):
        """Single-element list bypasses the all-low check (len > 1 guard)."""
        result = classify_failure(0.2, [0.2])
        self.assertEqual(result, "real_issue")

    def test_all_at_exactly_half_is_real_issue_not_criteria_bug(self):
        """0.5 clears the criteria_bug bar but sits below the actionable gate."""
        result = classify_failure(0.5, [0.5, 0.5, 0.5])
        self.assertEqual(result, "real_issue")

    def test_mixed_low_and_high_is_not_criteria_bug(self):
        """If any score is >= 0.5, the uniform-low check doesn't trigger."""
        result = classify_failure(0.2, [0.2, 0.6, 0.3])
        self.assertEqual(result, "real_issue")


class TestTriageFunction(unittest.TestCase):
    """Test the triage function with mock results.json files."""

    def _write_results(self, results_data, det_scores=None):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "results.json")
        with open(path, "w") as f:
            json.dump(results_data, f)
        if det_scores is not None:
            det_path = os.path.join(tmpdir, "deterministic_scores.json")
            with open(det_path, "w") as f:
                json.dump(det_scores, f)
        return path

    def test_triage_skips_non_object_entries(self):
        path = self._write_results(
            {"results": ["oops", {"test_id": "a", "score": 0.9}]}
        )
        result = triage(path)
        self.assertEqual([c["test_id"] for c in result["classifications"]], ["a"])

    def test_empty_results(self):
        path = self._write_results({"results": []})
        result = triage(path)
        self.assertEqual(result["summary"]["pass"], 0)
        self.assertEqual(len(result["classifications"]), 0)

    def test_all_passing(self):
        path = self._write_results(
            {
                "results": [
                    {"test_id": "TC-001", "score": 0.9},
                    {"test_id": "TC-002", "score": 0.8},
                ]
            }
        )
        result = triage(path)
        self.assertEqual(result["summary"]["pass"], 2)
        self.assertEqual(result["summary"]["real_issue"], 0)

    def test_all_zero_classified_as_criteria_bug(self):
        path = self._write_results(
            {
                "results": [
                    {"test_id": "TC-001", "score": 0.0},
                    {"test_id": "TC-002", "score": 0.0},
                ]
            }
        )
        result = triage(path)
        self.assertEqual(result["summary"]["criteria_bug"], 2)

    def test_single_zero_classified_as_variance(self):
        path = self._write_results(
            {
                "results": [
                    {"test_id": "TC-001", "score": 0.0},
                    {"test_id": "TC-002", "score": 0.8},
                ]
            }
        )
        result = triage(path)
        self.assertEqual(result["summary"]["variance"], 1)
        self.assertEqual(result["summary"]["pass"], 1)

    def test_variance_reachable_on_deterministic_only_run(self):
        """The real shape: no LLM scores, composites from the sibling file.

        The other variance tests hand-write an LLM `score: 0.0`, which a
        deterministic composite can never be.
        """
        path = self._write_results(
            {"results": [{"test_id": "TC-001"}, {"test_id": "TC-002"}]},
            det_scores={
                "per_test": [
                    {"test_id": "TC-001", "composite": DIMENSION_FLOOR},
                    {"test_id": "TC-002", "composite": 0.9},
                ]
            },
        )
        result = triage(path)
        tc1 = next(c for c in result["classifications"] if c["test_id"] == "TC-001")
        self.assertEqual(tc1["score_source"], "deterministic")
        self.assertEqual(tc1["classification"], "variance")
        self.assertEqual(result["summary"]["variance"], 1)
        self.assertEqual(result["summary"]["pass"], 1)

    def test_test_results_alias_is_triaged(self):
        """Reading only `results` reported a zero-test run for a file
        score_execution had just graded, so Phase 2 saw no failures."""
        path = self._write_results(
            {"test_results": [{"test_id": "TC-001", "score": 0.3}]}
        )
        result = triage(path)
        self.assertEqual(len(result["classifications"]), 1)
        self.assertEqual(result["classifications"][0]["test_id"], "TC-001")
        self.assertEqual(result["summary"]["real_issue"], 1)

    def test_deterministic_scores_preferred(self):
        path = self._write_results(
            {
                "results": [
                    {"test_id": "TC-001", "score": 0.3},
                    {"test_id": "TC-002", "score": 0.9},
                ]
            },
            det_scores={
                "per_test": [
                    {"test_id": "TC-001", "composite": 0.85},
                    {"test_id": "TC-002", "composite": 0.92},
                ]
            },
        )
        result = triage(path)
        tc1 = next(c for c in result["classifications"] if c["test_id"] == "TC-001")
        self.assertEqual(tc1["score_source"], "deterministic")
        self.assertAlmostEqual(tc1["score"], 0.85, places=2)
        self.assertEqual(tc1["classification"], "pass")

    def test_score_source_field(self):
        path = self._write_results(
            {
                "results": [
                    {"test_id": "TC-001", "score": 0.7},
                ]
            }
        )
        result = triage(path)
        self.assertEqual(result["classifications"][0]["score_source"], "llm_judge")

    def test_invalid_json_returns_error(self):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "results.json")
        with open(path, "w") as f:
            f.write("not json")
        result = triage(path)
        self.assertIn("error", result)


class TestAnalyzeOutput(unittest.TestCase):
    """The human-readable report must not contradict itself."""

    def _write(self, results_data, det_scores):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "results.json")
        with open(path, "w") as f:
            json.dump(results_data, f)
        with open(os.path.join(tmpdir, "deterministic_scores.json"), "w") as f:
            json.dump(det_scores, f)
        return path

    def _run(self, path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            analyze(path)
        return buf.getvalue()

    def test_inconclusive_row_not_reported_as_zero_fail(self):
        path = self._write(
            {
                "results": [
                    {"test_id": "t_inc", "details": {"category": "sim"}},
                    {"test_id": "t_ok", "details": {"category": "exec"}},
                ]
            },
            {
                "per_test": [
                    {"test_id": "t_inc", "composite": None, "status": "inconclusive"},
                    {"test_id": "t_ok", "composite": 0.508},
                ]
            },
        )
        summary = self._run(path).split("=== DIMENSION SUMMARY ===")[1]
        sim_row = next(ln for ln in summary.splitlines() if ln.startswith("sim"))
        self.assertIn("INCONCL", sim_row)
        self.assertNotIn("0.000", sim_row)
        self.assertNotIn("FAIL", sim_row)

    def test_analyze_skips_non_object_entries(self):
        """score_execution warns and keeps going on a malformed record.

        Both scripts read the same file through extract_results, so an entry
        that score_execution tolerates must not abort analyze with an
        AttributeError.
        """
        path = self._write(
            {"results": ["oops", {"test_id": "a", "details": {"category": "exec"}}]},
            {"per_test": [{"test_id": "a", "composite": 0.9}]},
        )
        output = self._run(path)
        self.assertIn("a", output)
        self.assertNotIn("No test results found", output)

    def test_analyze_tolerates_explicit_null_semantic_scores(self):
        """`"raw_semantic_scores": null` is a real shape from the judge.

        dict.get hands back that None for a present key, and `.items()` on it
        aborted analyze() at RECOMMENDED ACTIONS — after the summary, per-test
        breakdown and dimension summary had already printed, so the operator
        got a report that looked complete minus its last section.
        """
        path = self._write(
            {
                "results": [
                    {
                        "test_id": "TC-001",
                        "score": 0.9,
                        "suite": "exec",
                        "details": {
                            "category": "exec",
                            "composite_1_5": 4.5,
                            "raw_semantic_scores": None,
                            "overall_feedback": "ok",
                        },
                    }
                ]
            },
            {"per_test": [{"test_id": "TC-001", "composite": 0.9}]},
        )
        output = self._run(path)
        self.assertIn("RECOMMENDED ACTIONS", output)

    def test_analyze_tolerates_explicit_null_category(self):
        """A null category reached an f-string format spec and raised."""
        path = self._write(
            {
                "results": [
                    {
                        "test_id": "TC-002",
                        "suite": "exec",
                        "details": {"category": None, "composite_1_5": 4.0},
                    }
                ]
            },
            {"per_test": [{"test_id": "TC-002", "composite": 0.9}]},
        )
        output = self._run(path)
        self.assertIn("RECOMMENDED ACTIONS", output)
        self.assertIn("exec", output)

    def test_analyze_reads_test_results_alias(self):
        path = self._write(
            {"test_results": [{"test_id": "t1", "details": {"category": "exec"}}]},
            {"per_test": [{"test_id": "t1", "composite": 0.508}]},
        )
        output = self._run(path)
        self.assertNotIn("No test results found", output)
        self.assertIn("0.508", output)


if __name__ == "__main__":
    unittest.main()
