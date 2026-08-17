#!/usr/bin/env python3
"""Tests for criteria_self_repair.py pattern-table-based repair system.

Tests run the script via subprocess, passing a temp results.json and
the --json flag, then parse the JSON output to verify pattern matching.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from criteria_self_repair import match_patterns

SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "criteria_self_repair.py")


def run_script(results_data):
    """Write results_data to a temp file, run the script with --json, return parsed output."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(results_data, tmp)
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            [sys.executable, SCRIPT_PATH, tmp_path, "--json"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Script exited {proc.returncode}.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        return json.loads(proc.stdout)
    finally:
        os.unlink(tmp_path)


class TestRecursiveTimeoutPattern(unittest.TestCase):
    """Tests for the recursive_timeout pattern."""

    def _make_result(self, test_id, score, duration, timeout_analysis):
        return {
            "results": [
                {
                    "test_id": test_id,
                    "score": score,
                    "duration_seconds": duration,
                    "details": {"timeout_analysis": timeout_analysis},
                }
            ]
        }

    def test_matches_run_eval_with_long_duration(self):
        data = self._make_result(
            "TC-008",
            score=0.0,
            duration=1200,
            timeout_analysis="Tool calls: 3. run_eval triggered. Duration: 1200s",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["test_id"], "TC-008")
        self.assertEqual(out["matched"][0]["pattern"], "recursive_timeout")
        self.assertEqual(out["matched"][0]["confidence"], "high")

    def test_matches_eval_criteria_keyword(self):
        data = self._make_result(
            "TC-009",
            score=0.0,
            duration=700,
            timeout_analysis="eval_criteria was invoked recursively",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "recursive_timeout")

    def test_matches_run_eval_keyword(self):
        data = self._make_result(
            "TC-010",
            score=0.0,
            duration=700,
            timeout_analysis="run_eval was called. Duration: 1200s",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "recursive_timeout")

    def test_matches_skill_eval_keyword(self):
        data = self._make_result(
            "TC-011",
            score=0.0,
            duration=700,
            timeout_analysis="skill-eval/ launched. Duration: 1200s",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "recursive_timeout")

    def test_matches_duration_string_in_analysis(self):
        """1200s in analysis text is enough to trigger timeout, even if duration field is 0."""
        data = self._make_result(
            "TC-012",
            score=0.0,
            duration=0,
            timeout_analysis="run_eval was called. Duration: 1200s wall clock",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "recursive_timeout")

    def test_no_match_when_score_positive(self):
        """Score > 0 should not match even with timeout signals."""
        data = self._make_result(
            "TC-013",
            score=0.3,
            duration=1200,
            timeout_analysis="run_eval triggered. Duration: 1200s",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 0)

    def test_no_match_without_recursive_keyword(self):
        """Timeout without recursive keyword should not match recursive_timeout."""
        data = self._make_result(
            "TC-014",
            score=0.0,
            duration=700,
            timeout_analysis="generic timeout with no recursive tool calls",
        )
        out = run_script(data)
        # Should not match recursive_timeout (may fall through to unmatched)
        patterns = [m["pattern"] for m in out["matched"]]
        self.assertNotIn("recursive_timeout", patterns)

    def test_fixes_include_allowed_tools_remove(self):
        data = self._make_result(
            "TC-015",
            score=0.0,
            duration=700,
            timeout_analysis="run_eval called. 1200s elapsed",
        )
        out = run_script(data)
        fixes = out["matched"][0]["fixes"]
        tool_fix = next((f for f in fixes if f["field"] == "allowed_tools"), None)
        self.assertIsNotNone(tool_fix)
        self.assertEqual(tool_fix["action"], "remove")
        self.assertIn("Bash", tool_fix["values"])
        self.assertIn("Agent", tool_fix["values"])

    def test_fixes_include_required_absent_add(self):
        data = self._make_result(
            "TC-016",
            score=0.0,
            duration=700,
            timeout_analysis="eval_criteria invoked. Duration: 1200s",
        )
        out = run_script(data)
        fixes = out["matched"][0]["fixes"]
        absent_fix = next((f for f in fixes if f["field"] == "required_absent"), None)
        self.assertIsNotNone(absent_fix)
        self.assertEqual(absent_fix["action"], "add")
        self.assertIn("structural_audit", absent_fix["values"])
        self.assertIn("eval runner", absent_fix["values"])

    def test_fixes_include_runner_context_append(self):
        data = self._make_result(
            "TC-017",
            score=0.0,
            duration=700,
            timeout_analysis="run_eval. Duration: 1200s",
        )
        out = run_script(data)
        fixes = out["matched"][0]["fixes"]
        ctx_fix = next((f for f in fixes if f["field"] == "runner_context"), None)
        self.assertIsNotNone(ctx_fix)
        self.assertEqual(ctx_fix["action"], "append")
        self.assertIn("text", ctx_fix)
        self.assertIn("Phase 1", ctx_fix["text"])


class TestEmptyResponsePattern(unittest.TestCase):
    """Tests for the empty_response pattern."""

    def _make_result(self, test_id, score, response, timeout_analysis=""):
        return {
            "results": [
                {
                    "test_id": test_id,
                    "score": score,
                    "agent_response": response,
                    "details": {"timeout_analysis": timeout_analysis},
                }
            ]
        }

    def test_matches_empty_string_response(self):
        data = self._make_result("TC-020", score=0.0, response="")
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "empty_response")

    def test_matches_whitespace_only_response(self):
        data = self._make_result("TC-021", score=0.0, response="   \n\t  ")
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "empty_response")

    def test_matches_null_response_via_missing_key(self):
        """When neither agent_response nor response key exists, falls back to ''."""
        data = {
            "results": [
                {
                    "test_id": "TC-022",
                    "score": 0.0,
                    "details": {"timeout_analysis": ""},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "empty_response")

    def test_no_match_when_score_positive(self):
        data = self._make_result("TC-023", score=0.1, response="")
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 0)

    def test_no_match_when_response_has_content(self):
        data = self._make_result(
            "TC-024", score=0.0, response="The skill rejected the input."
        )
        out = run_script(data)
        patterns = [m["pattern"] for m in out["matched"]]
        self.assertNotIn("empty_response", patterns)

    def test_no_match_when_timeout_present(self):
        """Empty response combined with 1200s timeout should NOT trigger empty_response."""
        data = self._make_result(
            "TC-025",
            score=0.0,
            response="",
            timeout_analysis="Duration: 1200s",
        )
        out = run_script(data)
        patterns = [m["pattern"] for m in out["matched"]]
        self.assertNotIn("empty_response", patterns)

    def test_confidence_is_medium(self):
        data = self._make_result("TC-026", score=0.0, response="")
        out = run_script(data)
        self.assertEqual(out["matched"][0]["confidence"], "medium")

    def test_fix_appends_runner_context(self):
        data = self._make_result("TC-027", score=0.0, response="")
        out = run_script(data)
        fixes = out["matched"][0]["fixes"]
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["field"], "runner_context")
        self.assertEqual(fixes[0]["action"], "append")
        self.assertIn("text response", fixes[0]["text"])

    def test_prefers_agent_response_key_over_response(self):
        """Script uses agent_response if available, falls back to response."""
        data = {
            "results": [
                {
                    "test_id": "TC-028",
                    "score": 0.0,
                    "agent_response": "",
                    "response": "this should be ignored",
                    "details": {"timeout_analysis": ""},
                }
            ]
        }
        out = run_script(data)
        # agent_response="" is empty, so it should still match
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "empty_response")


class TestToolAccessErrorsPattern(unittest.TestCase):
    """Tests for the tool_access_errors pattern."""

    def _make_result(self, test_id, score, timeout_analysis):
        return {
            "results": [
                {
                    "test_id": test_id,
                    "score": score,
                    "agent_response": "some response",
                    "details": {"timeout_analysis": timeout_analysis},
                }
            ]
        }

    def test_matches_high_error_ratio(self):
        """6 errors out of 10 calls = 60%, above 50% threshold."""
        data = self._make_result(
            "TC-030",
            score=0.0,
            timeout_analysis="Tool calls: 10 (6 errors)",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "tool_access_errors")

    def test_matches_exactly_at_threshold(self):
        """Exactly 51% errors (11 out of 21) should match."""
        data = self._make_result(
            "TC-031",
            score=0.0,
            timeout_analysis="Tool calls: 21 (11 errors)",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "tool_access_errors")

    def test_no_match_below_threshold(self):
        """50% exactly (5 out of 10) should not match (strictly greater than 0.5)."""
        data = self._make_result(
            "TC-032",
            score=0.0,
            timeout_analysis="Tool calls: 10 (5 errors)",
        )
        out = run_script(data)
        patterns = [m["pattern"] for m in out["matched"]]
        self.assertNotIn("tool_access_errors", patterns)

    def test_no_match_when_score_above_0_3(self):
        """Score > 0.3 skips this pattern even with high tool error ratio."""
        data = self._make_result(
            "TC-033",
            score=0.4,
            timeout_analysis="Tool calls: 10 (8 errors)",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 0)

    def test_matches_when_score_exactly_0_3(self):
        """Score == 0.3 is NOT > 0.3, so pattern should still be checked."""
        data = self._make_result(
            "TC-034",
            score=0.3,
            timeout_analysis="Tool calls: 10 (8 errors)",
        )
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "tool_access_errors")

    def test_no_match_without_error_counts_in_analysis(self):
        """If analysis has no parseable error/tool counts, pattern does not match."""
        data = self._make_result(
            "TC-035",
            score=0.0,
            timeout_analysis="Some vague error message without counts",
        )
        out = run_script(data)
        patterns = [m["pattern"] for m in out["matched"]]
        self.assertNotIn("tool_access_errors", patterns)

    def test_fix_adds_ask_user_question(self):
        data = self._make_result(
            "TC-036",
            score=0.0,
            timeout_analysis="Tool calls: 10 (7 errors)",
        )
        out = run_script(data)
        fixes = out["matched"][0]["fixes"]
        tool_fix = next((f for f in fixes if f["field"] == "allowed_tools"), None)
        self.assertIsNotNone(tool_fix)
        self.assertEqual(tool_fix["action"], "add_if_missing")
        self.assertIn("AskUserQuestion", tool_fix["values"])

    def test_fix_appends_runner_context(self):
        data = self._make_result(
            "TC-037",
            score=0.0,
            timeout_analysis="Tool calls: 10 (7 errors)",
        )
        out = run_script(data)
        fixes = out["matched"][0]["fixes"]
        ctx_fix = next((f for f in fixes if f["field"] == "runner_context"), None)
        self.assertIsNotNone(ctx_fix)
        self.assertEqual(ctx_fix["action"], "append")

    def test_confidence_is_medium(self):
        data = self._make_result(
            "TC-038",
            score=0.0,
            timeout_analysis="Tool calls: 10 (7 errors)",
        )
        out = run_script(data)
        self.assertEqual(out["matched"][0]["confidence"], "medium")


class TestNoMatchingPatterns(unittest.TestCase):
    """Tests for results that do not match any known pattern."""

    def test_unmatched_low_score_no_signals(self):
        """A failing test with no recognizable signals ends up in unmatched."""
        data = {
            "results": [
                {
                    "test_id": "TC-050",
                    "score": 0.0,
                    "agent_response": "I tried but failed",
                    "details": {"timeout_analysis": "Duration: 45s Tool calls: 2"},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 0)
        self.assertEqual(len(out["unmatched"]), 1)
        self.assertEqual(out["unmatched"][0]["test_id"], "TC-050")
        self.assertEqual(out["unmatched"][0]["recommendation"], "human_review")

    def test_unmatched_captures_duration_in_signature(self):
        data = {
            "results": [
                {
                    "test_id": "TC-051",
                    "score": 0.0,
                    "agent_response": "some response",
                    "details": {"timeout_analysis": "Duration: 12s Tool calls: 3"},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["unmatched"]), 1)
        self.assertEqual(out["unmatched"][0]["failure_signature"], "duration=12s")

    def test_unmatched_captures_tool_calls(self):
        data = {
            "results": [
                {
                    "test_id": "TC-052",
                    "score": 0.0,
                    "agent_response": "some response",
                    "details": {"timeout_analysis": "Duration: 30s Tool calls: 7"},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["unmatched"]), 1)
        self.assertEqual(out["unmatched"][0]["tool_calls"], 7)

    def test_unmatched_falls_back_to_unknown_duration(self):
        data = {
            "results": [
                {
                    "test_id": "TC-053",
                    "score": 0.0,
                    "agent_response": "response",
                    "details": {"timeout_analysis": "no duration info here"},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["unmatched"]), 1)
        self.assertEqual(out["unmatched"][0]["failure_signature"], "unknown_duration")

    def test_passing_tests_are_skipped(self):
        """Tests with score >= 0.5 should not appear in matched or unmatched."""
        data = {
            "results": [
                {
                    "test_id": "TC-054",
                    "score": 0.65,
                    "agent_response": "",
                    "details": {"timeout_analysis": ""},
                },
                {
                    "test_id": "TC-055",
                    "score": 1.0,
                    "agent_response": "",
                    "details": {"timeout_analysis": "run_eval. Duration: 1200s"},
                },
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 0)
        self.assertEqual(len(out["unmatched"]), 0)
        self.assertEqual(out["summary"]["total_failing"], 0)

    def test_score_exactly_0_5_is_skipped(self):
        """Boundary: score == 0.5 is not a failure (threshold is >= 0.5)."""
        data = {
            "results": [
                {
                    "test_id": "TC-056",
                    "score": 0.5,
                    "agent_response": "",
                    "details": {"timeout_analysis": ""},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 0)
        self.assertEqual(len(out["unmatched"]), 0)


class TestSummaryAndMultipleResults(unittest.TestCase):
    """Tests for summary counts and multiple-result inputs."""

    def test_summary_counts_match(self):
        data = {
            "results": [
                # matched: recursive_timeout
                {
                    "test_id": "TC-060",
                    "score": 0.0,
                    "duration_seconds": 700,
                    "agent_response": "x",
                    "details": {"timeout_analysis": "run_eval. Duration: 1200s"},
                },
                # matched: empty_response
                {
                    "test_id": "TC-061",
                    "score": 0.0,
                    "agent_response": "",
                    "details": {"timeout_analysis": ""},
                },
                # unmatched
                {
                    "test_id": "TC-062",
                    "score": 0.0,
                    "agent_response": "partial response",
                    "details": {"timeout_analysis": "Duration: 5s Tool calls: 1"},
                },
                # passing — should be ignored
                {
                    "test_id": "TC-063",
                    "score": 0.9,
                    "agent_response": "good",
                    "details": {"timeout_analysis": ""},
                },
            ]
        }
        out = run_script(data)
        self.assertEqual(out["summary"]["total_failing"], 3)
        self.assertEqual(out["summary"]["pattern_matched"], 2)
        self.assertEqual(out["summary"]["unmatched"], 1)

    def test_all_passing_produces_empty_output(self):
        data = {
            "results": [
                {
                    "test_id": "TC-070",
                    "score": 0.8,
                    "agent_response": "ok",
                    "details": {},
                },
                {
                    "test_id": "TC-071",
                    "score": 1.0,
                    "agent_response": "ok",
                    "details": {},
                },
            ]
        }
        out = run_script(data)
        self.assertEqual(out["matched"], [])
        self.assertEqual(out["unmatched"], [])
        self.assertEqual(out["summary"]["total_failing"], 0)

    def test_empty_results_list(self):
        data = {"results": []}
        out = run_script(data)
        self.assertEqual(out["matched"], [])
        self.assertEqual(out["unmatched"], [])
        self.assertEqual(out["summary"]["total_failing"], 0)

    def test_first_pattern_wins(self):
        """A result that matches recursive_timeout should not also match empty_response."""
        data = {
            "results": [
                {
                    "test_id": "TC-080",
                    "score": 0.0,
                    "duration_seconds": 700,
                    # empty response AND recursive timeout signals
                    "agent_response": "",
                    "details": {"timeout_analysis": "run_eval. Duration: 1200s"},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        # recursive_timeout is first in PATTERNS, so it wins
        self.assertEqual(out["matched"][0]["pattern"], "recursive_timeout")

    def test_test_id_propagated_to_matched(self):
        data = {
            "results": [
                {
                    "test_id": "MY-CUSTOM-ID",
                    "score": 0.0,
                    "agent_response": "",
                    "details": {"timeout_analysis": ""},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(out["matched"][0]["test_id"], "MY-CUSTOM-ID")

    def test_final_score_fallback_reaches_conditions(self):
        """final_score-only results must match patterns like score-keyed ones.

        match_patterns normalizes the score key once, so condition functions
        (which read result['score']) see the final_score fallback instead of
        defaulting to 1.0 and bailing.
        """
        data = {
            "results": [
                {
                    "test_id": "TC-090",
                    "final_score": 0.0,
                    "agent_response": "",
                    "details": {"timeout_analysis": ""},
                }
            ]
        }
        out = run_script(data)
        self.assertEqual(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0]["pattern"], "empty_response")
        self.assertEqual(len(out["unmatched"]), 0)


class TestMatchPatternsErrorHandling(unittest.TestCase):
    """Tests for match_patterns function returning error dicts instead of sys.exit."""

    def test_missing_file_returns_error_dict(self):
        result = match_patterns("/nonexistent/path/results.json")
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])
        self.assertEqual(result["matched"], [])
        self.assertEqual(result["unmatched"], [])
        self.assertEqual(result["summary"]["total_failing"], 0)

    def test_invalid_json_returns_error_dict(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            tmp.write("{not valid json")
            tmp_path = tmp.name

        try:
            result = match_patterns(tmp_path)
            self.assertIn("error", result)
            self.assertIn("invalid JSON", result["error"])
            self.assertEqual(result["matched"], [])
            self.assertEqual(result["summary"]["total_failing"], 0)
        finally:
            os.unlink(tmp_path)

    def test_valid_file_no_error_key(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump({"results": []}, tmp)
            tmp_path = tmp.name

        try:
            result = match_patterns(tmp_path)
            self.assertNotIn("error", result)
        finally:
            os.unlink(tmp_path)


class TestScriptErrorHandling(unittest.TestCase):
    """Tests for script-level error handling (bad input files)."""

    def test_exits_nonzero_on_missing_file(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT_PATH, "/nonexistent/path/results.json", "--json"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not found", proc.stderr)

    def test_exits_nonzero_on_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            tmp.write("{this is not valid json")
            tmp_path = tmp.name

        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT_PATH, tmp_path, "--json"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("invalid JSON", proc.stderr)
        finally:
            os.unlink(tmp_path)

    def test_exits_nonzero_with_no_args(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT_PATH],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage:", proc.stderr)

    def test_output_is_valid_json_with_json_flag(self):
        data = {"results": []}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(data, tmp)
            tmp_path = tmp.name

        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT_PATH, tmp_path, "--json"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0)
            parsed = json.loads(proc.stdout)
            self.assertIn("matched", parsed)
            self.assertIn("unmatched", parsed)
            self.assertIn("summary", parsed)
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
