#!/usr/bin/env python3
"""Tests for generate_spec_artifacts.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate_spec_artifacts import (
    generate_benchmark,
    generate_evals,
    generate_grading,
    load_criteria_json,
)

SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "generate_spec_artifacts.py")

CRITERIA = {
    "test_cases": [
        {"id": "TC-001", "name": "First", "category": "invocation"},
    ]
}


class TestGenerateEvalsNullTolerance(unittest.TestCase):
    """Null semantic-score mappings must be safe in both eval and grading generation."""

    def _evals(self, result: dict) -> dict:
        return generate_evals(
            [{"id": "TC-001", "name": "First", "category": "invocation"}],
            {"results": [result]},
        )

    def test_null_raw_semantic_scores(self) -> None:
        out = self._evals(
            {"test_id": "TC-001", "score": 0.9, "details": {"raw_semantic_scores": None}}
        )
        self.assertEqual(out["evals"][0]["assertions"], [])

    def test_null_details(self) -> None:
        out = self._evals({"test_id": "TC-001", "score": 0.9, "details": None})
        self.assertEqual(out["evals"][0]["assertions"], [])

    def test_wrong_typed_scores_are_treated_as_absent(self) -> None:
        out = self._evals(
            {"test_id": "TC-001", "score": 0.9, "details": {"raw_semantic_scores": []}}
        )
        self.assertEqual(out["evals"][0]["assertions"], [])

    def test_populated_scores_still_produce_assertions(self) -> None:
        out = self._evals(
            {
                "test_id": "TC-001",
                "score": 0.9,
                "details": {"raw_semantic_scores": {"names the gate": 4.0}},
            }
        )
        assertions = out["evals"][0]["assertions"]
        self.assertEqual(len(assertions), 1)
        self.assertEqual(assertions[0]["id"], "TC-001-A1")
        self.assertEqual(assertions[0]["description"], "names the gate")


class TestLoadCriteria(unittest.TestCase):
    def test_unreadable_criteria_returns_none(self) -> None:
        self.assertIsNone(load_criteria_json("/nonexistent/eval_criteria.json"))

    def test_non_object_criteria_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as tmp:
            json.dump([], tmp)
            path = tmp.name
        try:
            self.assertIsNone(load_criteria_json(path))
        finally:
            os.unlink(path)

    def test_criteria_with_no_test_cases_is_empty_not_none(self) -> None:
        # An empty list is a real answer ("this file declares no test cases")
        # and must stay distinguishable from "no usable criteria file".
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as tmp:
            json.dump({"test_cases": []}, tmp)
            path = tmp.name
        try:
            self.assertEqual(load_criteria_json(path), [])
        finally:
            os.unlink(path)


class TestCriteriaLoadFailureExits(unittest.TestCase):
    """Unusable criteria must stop generation before grading references undefined evals."""

    def _run(self, criteria_path: str, out_dir: str) -> subprocess.CompletedProcess:
        with open(os.path.join(out_dir, "results.json"), "w") as f:
            json.dump({"results": [{"test_id": "TC-001", "score": 0.9}]}, f)
        return subprocess.run(
            [sys.executable, SCRIPT_PATH, out_dir, "--criteria", criteria_path, "--json"],
            capture_output=True,
            text=True,
        )

    def test_missing_criteria_exits_1_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as out_dir:
            proc = self._run("/nonexistent/eval_criteria.json", out_dir)
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(os.path.exists(os.path.join(out_dir, "evals.json")))
            self.assertIn("criteria", proc.stderr.lower())

    def test_valid_criteria_exits_0_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as out_dir:
            criteria_path = os.path.join(out_dir, "eval_criteria.json")
            with open(criteria_path, "w") as f:
                json.dump(CRITERIA, f)
            proc = self._run(criteria_path, out_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for name in ("evals.json", "grading.json", "timing.json", "benchmark.json"):
                self.assertTrue(os.path.exists(os.path.join(out_dir, name)), name)
            with open(os.path.join(out_dir, "evals.json")) as f:
                self.assertEqual(len(json.load(f)["evals"]), 1)

    def test_null_raw_semantic_scores_does_not_abort_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as out_dir:
            criteria_path = os.path.join(out_dir, "eval_criteria.json")
            with open(criteria_path, "w") as f:
                json.dump(CRITERIA, f)
            with open(os.path.join(out_dir, "results.json"), "w") as f:
                json.dump(
                    {
                        "results": [
                            {
                                "test_id": "TC-001",
                                "score": 0.9,
                                "details": {"raw_semantic_scores": None},
                            }
                        ]
                    },
                    f,
                )
            proc = subprocess.run(
                [
                    sys.executable,
                    SCRIPT_PATH,
                    out_dir,
                    "--criteria",
                    criteria_path,
                    "--json",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.exists(os.path.join(out_dir, "evals.json")))


class TestGenerateGradingTypeSlop(unittest.TestCase):
    """Malformed per-check scores must not abort generation of the artifact set."""

    def _grading(self, result: dict) -> dict:
        return generate_grading({"results": [result]}, None)

    def test_list_shaped_raw_semantic_scores(self) -> None:
        out = self._grading(
            {
                "test_id": "TC-1",
                "score": 0.9,
                "details": {"raw_semantic_scores": ["check a", "check b"]},
            }
        )
        # No per-check assertions from an unusable payload; the composite
        # assertion is unaffected and still written.
        ids = [a["assertion_id"] for a in out["assertion_results"]]
        self.assertEqual(ids, ["TC-1-composite"])

    def test_null_per_check_score_is_not_a_pass(self) -> None:
        out = self._grading(
            {
                "test_id": "TC-1",
                "score": 0.9,
                "details": {"raw_semantic_scores": {"check a": 4, "check b": None}},
            }
        )
        passed = {a["assertion_id"]: a["passed"] for a in out["assertion_results"]}
        self.assertTrue(passed["TC-1-A1"])
        self.assertFalse(passed["TC-1-A2"])

    def test_stringified_per_check_score_is_not_a_pass(self) -> None:
        out = self._grading(
            {
                "test_id": "TC-1",
                "score": 0.9,
                "details": {"raw_semantic_scores": {"check a": "4"}},
            }
        )
        self.assertFalse(out["assertion_results"][0]["passed"])


class TestBenchmarkScoreTypeSlop(unittest.TestCase):
    """Non-numeric scores must not reach summary arithmetic."""

    def _summary(self, results: list) -> dict:
        out = generate_benchmark({"results": results}, None, None, None)
        return out["run_summary"]["with_skill"]

    def test_stringified_score_is_excluded_from_the_average(self) -> None:
        summary = self._summary([{"test_id": "TC-1", "score": "0.9", "details": {}}])
        self.assertIsNone(summary["avg_score"])
        self.assertEqual(summary["test_count"], 1)
        self.assertEqual(summary["pass_count"], 0)

    def test_numeric_scores_still_average(self) -> None:
        summary = self._summary(
            [
                {"test_id": "TC-1", "score": 0.9, "details": {}},
                {"test_id": "TC-2", "score": 0.5, "details": {}},
            ]
        )
        self.assertEqual(summary["avg_score"], 0.7)
        self.assertEqual(summary["pass_count"], 1)

    def test_missing_score_stays_out_of_the_average(self) -> None:
        summary = self._summary(
            [
                {"test_id": "TC-1", "score": 0.9, "details": {}},
                {"test_id": "TC-2", "details": {}},
            ]
        )
        self.assertEqual(summary["avg_score"], 0.9)


class TestUsageBlockNamesJsonCriteria(unittest.TestCase):
    def test_docstring_does_not_advertise_a_yaml_criteria_file(self) -> None:
        # The whole pipeline writes eval_criteria.json; the usage block said
        # .yaml, which is how a mistyped --criteria path gets there at all.
        import generate_spec_artifacts

        self.assertNotIn("eval_criteria.yaml", generate_spec_artifacts.__doc__)
        self.assertIn("eval_criteria.json", generate_spec_artifacts.__doc__)


if __name__ == "__main__":
    unittest.main()
