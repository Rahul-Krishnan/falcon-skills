#!/usr/bin/env python3
"""Tests for validate_criteria_schema.py.

Run with:
    python3 test_validate_criteria_schema.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "validate_criteria_schema.py")


def run_validate(data: dict | str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Write data to a temp JSON file and run the validator against it."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        if isinstance(data, dict):
            json.dump(data, f, indent=2)
        else:
            f.write(data)
        tmp_path = f.name

    cmd = [sys.executable, SCRIPT, tmp_path] + (extra_args or [])
    return subprocess.run(cmd, capture_output=True, text=True)


VALID_CRITERIA = {
    "project": "test-project",
    "skill_name": "test-skill",
    "test_cases": [
        {
            "id": "tc_001",
            "name": "Basic invocation test",
            "category": "invocation",
            "test_profile": "execution",
            "prompt": "Do something useful.",
            "runner_context": "Simulate a code review assistant.",
            "allowed_tools": ["Read", "Bash", "Grep"],
            "target_skills": ["/some/skill.md"],
            "checks": [
                {
                    "description": "Did the agent execute the review and produce a clear summary?",
                    "importance": "HIGH",
                    "rubric": {
                        "1": "No review attempted",
                        "2": "Partial review, major gaps",
                        "3": "Review done but unclear summary",
                        "4": "Good review with minor gaps",
                        "5": "Complete review with clear summary",
                    },
                }
            ],
        }
    ],
}


class TestValidCriteria(unittest.TestCase):
    def test_valid_criteria_exits_zero(self):
        result = run_validate(VALID_CRITERIA)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("VALID", result.stdout)

    def test_valid_criteria_json_output(self):
        result = run_validate(VALID_CRITERIA, ["--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["test_case_count"], 1)
        self.assertEqual(payload["error_count"], 0)


class TestInvalidCriteria(unittest.TestCase):
    def test_empty_json_file_is_error(self):
        result = run_validate("")
        self.assertNotEqual(result.returncode, 0)

    def test_missing_test_cases_is_error(self):
        result = run_validate({"project": "test"})
        self.assertEqual(result.returncode, 1)

    def test_empty_test_cases_is_error(self):
        result = run_validate({"test_cases": []})
        self.assertEqual(result.returncode, 1)

    def test_null_test_cases_reports_cleanly(self):
        # Null test_cases must produce INVALID, not crash the semantic pass.
        result = run_validate({"test_cases": None})
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("INVALID", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_null_test_cases_json_output_is_parseable(self):
        result = run_validate({"test_cases": None}, ["--json"])
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["test_case_count"], 0)

    def test_non_list_test_cases_reports_cleanly(self):
        result = run_validate({"test_cases": {"tc": 1}})
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_unhashable_id_reports_cleanly(self):
        # Unhashable IDs must produce findings without crashing validation or audit.
        data = {"test_cases": [dict(VALID_CRITERIA["test_cases"][0], id={"k": 1})]}
        result = run_validate(data, ["--json"])
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])

    def test_null_id_reports_cleanly(self):
        # {"id": null} bypassed the "" default and put None in seen_ids.
        data = {"test_cases": [dict(VALID_CRITERIA["test_cases"][0], id=None)]}
        result = run_validate(data)
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_required_field_is_error(self):
        data = {
            "test_cases": [
                {
                    "id": "tc_001",
                    "name": "Test",
                    "category": "invocation",
                    "test_profile": "execution",
                    # missing prompt
                    "runner_context": "Simulate.",
                    "allowed_tools": ["Read"],
                    "checks": [
                        {
                            "description": "Check",
                            "importance": "HIGH",
                            "rubric": {"1": "Bad", "2": "OK", "3": "Good", "4": "Great", "5": "Perfect"},
                        }
                    ],
                }
            ]
        }
        result = run_validate(data, ["--json"])
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])
        error_paths = [e["path"] for e in payload["errors"]]
        self.assertTrue(any("prompt" in p for p in error_paths))

    def test_invalid_category_enum_is_error(self):
        data = {
            "test_cases": [
                {
                    "id": "tc_001",
                    "name": "Test",
                    "category": "nonexistent_category",
                    "test_profile": "execution",
                    "prompt": "Do something.",
                    "runner_context": "Simulate.",
                    "allowed_tools": ["Read"],
                    "checks": [
                        {
                            "description": "Check",
                            "importance": "HIGH",
                            "rubric": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"},
                        }
                    ],
                }
            ]
        }
        result = run_validate(data, ["--json"])
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])

    def test_invalid_importance_enum_is_error(self):
        data = {
            "test_cases": [
                {
                    "id": "tc_001",
                    "name": "Test",
                    "category": "invocation",
                    "test_profile": "execution",
                    "prompt": "Do something.",
                    "runner_context": "Simulate.",
                    "allowed_tools": ["Read"],
                    "checks": [
                        {
                            "description": "Check",
                            "importance": "INVALID",
                            "rubric": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"},
                        }
                    ],
                }
            ]
        }
        result = run_validate(data, ["--json"])
        self.assertEqual(result.returncode, 1)

    def test_incomplete_rubric_is_error(self):
        data = {
            "test_cases": [
                {
                    "id": "tc_001",
                    "name": "Test",
                    "category": "invocation",
                    "test_profile": "execution",
                    "prompt": "Do something.",
                    "runner_context": "Simulate.",
                    "allowed_tools": ["Read"],
                    "checks": [
                        {
                            "description": "Check",
                            "importance": "HIGH",
                            "rubric": {"1": "Bad", "5": "Good"},  # missing 2, 3, 4
                        }
                    ],
                }
            ]
        }
        result = run_validate(data, ["--json"])
        self.assertEqual(result.returncode, 1)

    def test_empty_checks_array_is_error(self):
        data = {
            "test_cases": [
                {
                    "id": "tc_001",
                    "name": "Test",
                    "category": "invocation",
                    "test_profile": "execution",
                    "prompt": "Do something.",
                    "runner_context": "Simulate.",
                    "allowed_tools": ["Read"],
                    "checks": [],
                }
            ]
        }
        result = run_validate(data, ["--json"])
        self.assertEqual(result.returncode, 1)

    def test_duplicate_test_ids_is_error(self):
        tc = {
            "id": "tc_001",
            "name": "Test",
            "category": "invocation",
            "test_profile": "execution",
            "prompt": "Do something.",
            "runner_context": "Simulate.",
            "allowed_tools": ["Read"],
            "checks": [
                {
                    "description": "Check",
                    "importance": "HIGH",
                    "rubric": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"},
                }
            ],
        }
        data = {"test_cases": [tc, tc]}
        result = run_validate(data, ["--json"])
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertTrue(any("duplicate" in e["message"] for e in payload["errors"]))


class TestFileErrors(unittest.TestCase):
    def test_nonexistent_file_exits_2(self):
        cmd = [sys.executable, SCRIPT, "/nonexistent/path/eval_criteria.json"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_invalid_json_exits_2(self):
        result = run_validate("{not valid json")
        self.assertEqual(result.returncode, 2)

    def test_non_object_root_exits_2(self):
        result = run_validate("[1, 2, 3]")
        self.assertEqual(result.returncode, 2)

    def test_directory_path_exits_2_not_traceback(self):
        # Directory inputs must return the documented exit-2 usage error.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cmd = [sys.executable, SCRIPT, tmp_dir, "--json"]
            result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])


class TestOptionalFields(unittest.TestCase):
    def test_optional_required_present_absent(self):
        """required_present and required_absent are optional."""
        data = {
            "test_cases": [
                {
                    "id": "tc_001",
                    "name": "Test",
                    "category": "invocation",
                    "test_profile": "execution",
                    "prompt": "Do something.",
                    "runner_context": "Simulate.",
                    "allowed_tools": ["Read"],
                    "checks": [
                        {
                            "description": "Check",
                            "importance": "HIGH",
                            "rubric": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"},
                        }
                    ],
                    "required_present": ["hello"],
                    "required_absent": ["error"],
                }
            ]
        }
        result = run_validate(data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_optional_target_skills(self):
        """target_skills is optional."""
        data = {
            "test_cases": [
                {
                    "id": "tc_001",
                    "name": "Test",
                    "category": "invocation",
                    "test_profile": "execution",
                    "prompt": "Do something.",
                    "runner_context": "Simulate.",
                    "allowed_tools": ["Read"],
                    "checks": [
                        {
                            "description": "Check",
                            "importance": "HIGH",
                            "rubric": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"},
                        }
                    ],
                }
            ]
        }
        result = run_validate(data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestFailureModeProfile(unittest.TestCase):
    def test_failure_mode_is_valid_test_profile(self):
        """failure_mode must be accepted as a valid test_profile enum value."""
        data = {
            "test_cases": [
                {
                    "id": "tc_fm_001",
                    "name": "Corrupt state file halt",
                    "category": "edge_case",
                    "test_profile": "failure_mode",
                    "prompt": "Run hone on a skill. The state file is corrupt JSON.",
                    "runner_context": "SIMULATION MODE. Inject corrupt state.",
                    "allowed_tools": ["Read", "Bash"],
                    "checks": [
                        {
                            "description": "Did the skill halt on corrupt state?",
                            "importance": "CRITICAL",
                            "rubric": {
                                "1": "Did not halt",
                                "2": "Halted without message",
                                "3": "Halted with vague message",
                                "4": "Halted with file path",
                                "5": "Halted immediately with clear message and file path",
                            },
                        }
                    ],
                }
            ]
        }
        result = run_validate(data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_test_profile_rejected(self):
        data = {
            "test_cases": [
                {
                    "id": "tc_001",
                    "name": "Test",
                    "category": "edge_case",
                    "test_profile": "adversarial_mode",
                    "prompt": "Do something.",
                    "runner_context": "Simulate.",
                    "allowed_tools": ["Read"],
                    "checks": [
                        {
                            "description": "Check",
                            "importance": "HIGH",
                            "rubric": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"},
                        }
                    ],
                }
            ]
        }
        result = run_validate(data, ["--json"])
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
