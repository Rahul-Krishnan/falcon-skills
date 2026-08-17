"""Tests for validate_eval_criteria.py.

Run with:
  python3 test_validate_eval_criteria.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PYTHON = sys.executable
SCRIPT = str(Path(__file__).parent / "validate_eval_criteria.py")

# Exit codes. Warnings do not flip the exit code: the Step 5 -> Step 6 gate
# accepts warnings, and the handoff field validation_passed is defined as
# exit 0 — a warnings-only run must satisfy both. Tests that expect a
# warning therefore also assert on the WARNINGS block in stdout.
EXIT_CLEAN = 0
EXIT_WARNINGS = 0
EXIT_ERRORS = 2


def run_validate(content, extra_args=None):
    """Write content to a temp JSON file and run the script against it."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        if isinstance(content, dict):
            json.dump(content, f, indent=2)
        else:
            f.write(content)
        tmp_path = f.name

    cmd = [PYTHON, SCRIPT, tmp_path] + (extra_args or [])
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Helpers: minimal valid JSON fixtures
# ---------------------------------------------------------------------------

def _make_check(description="Did the agent execute the review and produce a clear summary?",
                importance="HIGH"):
    """Build a single valid check dict with full rubric."""
    return {
        "description": description,
        "importance": importance,
        "rubric": {
            "1": "Bad",
            "2": "Poor",
            "3": "OK",
            "4": "Good",
            "5": "Excellent",
        },
    }


def _make_tc(tc_id="tc_001", prompt="Do something useful.", checks=None, **overrides):
    """Build a single valid test case dict."""
    tc = {
        "id": tc_id,
        "name": "Basic test",
        "category": "invocation",
        "test_profile": "execution",
        "prompt": prompt,
        "runner_context": "Simulate a code review assistant.",
        "allowed_tools": ["Read", "Bash", "Grep", "Glob", "Skill"],
        "target_skills": ["/some/skill.md"],
        "checks": checks or [_make_check()],
    }
    tc.update(overrides)
    return tc


VALID_CRITERIA = {
    "test_cases": [_make_tc()]
}


class TestValidateMode(unittest.TestCase):
    """Tests for default (validate-only) mode."""

    def test_valid_criteria_exits_clean(self):
        result = run_validate(VALID_CRITERIA)
        self.assertEqual(result.returncode, EXIT_CLEAN, result.stdout)
        self.assertIn("CLEAN", result.stdout)

    def test_empty_test_cases_is_error(self):
        result = run_validate({"test_cases": []})
        self.assertEqual(result.returncode, EXIT_ERRORS)
        self.assertIn("ERROR", result.stdout)

    def test_missing_test_cases_key_is_error(self):
        result = run_validate({"description": "No test_cases key at all."})
        self.assertEqual(result.returncode, EXIT_ERRORS)
        self.assertIn("ERROR", result.stdout)

    def test_empty_json_file_is_error(self):
        result = run_validate("")
        self.assertEqual(result.returncode, EXIT_ERRORS)
        self.assertIn("ERROR", result.stdout)

    def test_malformed_json_is_error(self):
        result = run_validate("{not valid json at all")
        self.assertEqual(result.returncode, EXIT_ERRORS)
        self.assertIn("ERROR", result.stdout)

    def test_empty_prompt_is_error(self):
        tc = _make_tc(prompt="")
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_ERRORS)
        # Schema validation catches empty prompt with "non-empty" or validate catches "empty prompt"
        self.assertTrue(
            "empty prompt" in result.stdout or "non-empty" in result.stdout,
            f"Expected 'empty prompt' or 'non-empty' in: {result.stdout}"
        )

    def test_missing_checks_is_warning(self):
        tc = _make_tc()
        tc["checks"] = []
        tc["required_present"] = ["done"]
        result = run_validate({"test_cases": [tc]})
        # Schema validation will reject empty checks array
        self.assertIn(result.returncode, (EXIT_WARNINGS, EXIT_ERRORS))

    def test_dangerous_required_absent_word_is_warning(self):
        tc = _make_tc(required_absent=["error"])
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_WARNINGS)
        self.assertIn("WARNINGS", result.stdout)
        self.assertIn("error", result.stdout)
        self.assertIn("required_absent", result.stdout)

    def test_warnings_only_run_exits_zero(self):
        # Contract pin: warnings alone must not fail validation, or the
        # validation_passed handoff (exit 0) disagrees with the gate
        # checklist ("warnings acceptable") and hard-stops a good file.
        tc = _make_tc(required_absent=["error"])
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, 0)
        self.assertIn("WARNINGS", result.stdout)

    def test_all_dangerous_absent_words_produce_warnings(self):
        """Each DANGEROUS_ABSENT_WORDS entry should trigger a warning."""
        dangerous = [
            "error", "warning", "fail", "failed", "bug", "issue", "problem",
            "debug", "exception", "trace", "stack", "crash", "retry",
        ]
        for word in dangerous:
            with self.subTest(word=word):
                tc = _make_tc(required_absent=[word])
                result = run_validate({"test_cases": [tc]})
                self.assertEqual(result.returncode, EXIT_WARNINGS, f"Expected warning for '{word}'")
                self.assertIn("WARNINGS", result.stdout, f"Expected warning for '{word}'")

    def test_required_present_section_header_is_warning(self):
        """Multi-word uppercase string in required_present triggers brittleness warning."""
        tc = _make_tc(required_present=["SUMMARY AND CONCLUSIONS"])
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_WARNINGS)
        self.assertIn("required_present", result.stdout)

    def test_required_present_long_string_is_warning(self):
        """A required_present string > 30 chars triggers brittleness warning."""
        long_string = "a" * 31
        tc = _make_tc(required_present=[long_string])
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_WARNINGS)
        self.assertIn("required_present", result.stdout)

    def test_tool_call_artifact_in_required_present_is_warning(self):
        """File names (e.g. 'script.py') in required_present should warn."""
        tc = _make_tc(required_present=["run_script.py"])
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_WARNINGS)
        self.assertIn("tool call artifact", result.stdout)

    def test_pipeline_command_with_result_expectation_is_warning(self):
        """Prompt invoking a pipeline command + result-expecting semantic check should warn."""
        tc = _make_tc(
            prompt="/forge please build the feature",
            checks=[_make_check("Was the output complete and submitted?")],
        )
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_WARNINGS)
        self.assertIn("pipeline", result.stdout)

    def test_pipeline_command_without_result_expectation_is_clean(self):
        """Pipeline command in prompt is fine if semantic check doesn't expect full execution."""
        tc = _make_tc(
            prompt="/forge please build the feature",
            checks=[_make_check("Did the agent invoke the correct command with valid arguments?")],
        )
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_CLEAN, result.stdout)

    def test_nonexistent_file_is_error(self):
        cmd = [PYTHON, SCRIPT, "/nonexistent/path/eval_criteria.json"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, EXIT_ERRORS)
        self.assertIn("ERROR", result.stdout)


class TestAuditMode(unittest.TestCase):
    """Tests for --audit flag."""

    def _audit(self, content, artifact_path=None):
        args = ["--audit"]
        if artifact_path:
            args += ["--artifact-path", artifact_path]
        result = run_validate(content, extra_args=args)
        # The schema validator may print "VALID: ..." or "INVALID: ..." to stdout
        # before the JSON payload. Extract the JSON object from stdout.
        stdout = result.stdout.strip()
        json_start = stdout.find("{")
        if json_start == -1:
            raise ValueError(f"No JSON found in stdout: {stdout!r}")
        payload = json.loads(stdout[json_start:])
        return result.returncode, payload

    def test_valid_criteria_audit_exits_zero(self):
        returncode, payload = self._audit(VALID_CRITERIA)
        self.assertEqual(returncode, EXIT_CLEAN)
        self.assertNotIn("error", payload)
        self.assertTrue(payload["schema_valid"])
        self.assertIsInstance(payload["findings"], list)

    def test_audit_missing_runner_context_still_runs_repair_checks(self):
        """A schema failure must not short-circuit the audit: missing
        runner_context is exactly what check_runner_context_present exists to
        catch, so the audit reports it instead of returning an empty error."""
        tc = _make_tc()
        del tc["runner_context"]
        _, payload = self._audit({"test_cases": [tc]})
        self.assertFalse(payload["schema_valid"])
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("missing_runner_context", issues)

    def test_audit_missing_allowed_tools_still_runs_repair_checks(self):
        """Missing allowed_tools fails the schema but must still reach the
        fixable missing_allowed_tools repair path."""
        tc = _make_tc()
        del tc["allowed_tools"]
        _, payload = self._audit({"test_cases": [tc]})
        self.assertFalse(payload["schema_valid"])
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("missing_allowed_tools", issues)
        self.assertGreaterEqual(payload["fixable_count"], 1)

    def test_audit_wrong_typed_fields_do_not_crash(self):
        """Audit runs on schema-invalid files by design; wrong-typed (not
        just null) fields must degrade to findings, never a traceback that
        leaves the hone executor zero bytes of JSON on stdout."""
        tc = _make_tc()
        tc["runner_context"] = ["not", "a", "string"]
        tc["prompt"] = 42
        tc["allowed_tools"] = "Read"
        tc["required_present"] = ["run_script.py", 7]
        returncode, payload = self._audit({"test_cases": [tc]})
        self.assertEqual(returncode, EXIT_CLEAN)
        self.assertFalse(payload["schema_valid"])
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("missing_runner_context", issues)
        self.assertIn("missing_allowed_tools", issues)

    def test_audit_mixed_type_ids_do_not_crash(self):
        """Two warning-drawing test cases whose ids are an int and a str
        crashed sorted(unfixable_test_ids) with TypeError (int < str),
        exit 1 and no JSON on stdout for the criteria-audit consumer."""
        tc_int = _make_tc(tc_id=2)
        del tc_int["runner_context"]
        tc_str = _make_tc(tc_id="b")
        del tc_str["runner_context"]
        returncode, payload = self._audit({"test_cases": [tc_int, tc_str]})
        self.assertEqual(returncode, EXIT_CLEAN)
        self.assertIn("b", payload["unfixable_test_ids"])
        # The non-string id degrades to "unknown", same as an absent one.
        self.assertIn("unknown", payload["unfixable_test_ids"])

    def test_audit_unhashable_id_does_not_crash(self):
        """A list/dict id crashed unfixable_test_ids.add()."""
        tc = _make_tc(tc_id=["not", "hashable"])
        del tc["runner_context"]
        returncode, payload = self._audit({"test_cases": [tc]})
        self.assertEqual(returncode, EXIT_CLEAN)
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("missing_runner_context", issues)

    def test_audit_mixed_allowed_tools_suggests_strings_only(self):
        """A mixed-type allowed_tools list propagated non-string junk into
        suggested_fix; the auto-repair then wrote a file that still failed
        the pre-launch schema gate (allowed_tools[0]: expected string)."""
        tc = _make_tc(
            prompt="Run /my-skill now.", allowed_tools=[123, "Read"]
        )
        _, payload = self._audit({"test_cases": [tc]})
        fixes = [
            f["suggested_fix"]["value"]
            for f in payload["findings"]
            if f.get("issue") == "missing_skill_tool"
        ]
        self.assertEqual(fixes, [["Read", "Skill"]])

    def test_audit_null_test_cases_returns_error_key(self):
        _, payload = self._audit({"test_cases": None})
        self.assertIn("error", payload)
        self.assertEqual(payload["total_test_cases"], 0)

    def test_audit_reports_missing_skill_tool_when_slash_command_in_prompt(self):
        tc = _make_tc(
            prompt="/some-skill do the thing",
            checks=[_make_check("Did the agent invoke the skill correctly?")],
            allowed_tools=["Read", "Bash"],
        )
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("missing_skill_tool", issues)

    def test_audit_with_artifact_path_reports_missing_target_skills(self):
        tc = _make_tc()
        del tc["target_skills"]
        _, payload = self._audit({"test_cases": [tc]}, artifact_path="/path/to/skill.md")
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("missing_target_skills", issues)
        # The suggested fix should reference the artifact path
        for finding in payload["findings"]:
            if finding["issue"] == "missing_target_skills":
                self.assertEqual(
                    finding["suggested_fix"]["value"], ["/path/to/skill.md"]
                )

    def test_audit_without_artifact_path_does_not_report_missing_target_skills(self):
        tc = _make_tc()
        del tc["target_skills"]
        _, payload = self._audit({"test_cases": [tc]}, artifact_path=None)
        issues = [f["issue"] for f in payload["findings"]]
        self.assertNotIn("missing_target_skills", issues)

    def test_audit_warns_on_keyword_only_semantic_check(self):
        tc = _make_tc(
            checks=[_make_check("Does the output contain the string 'foobar'?")],
        )
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("keyword_only_check", issues)

    def test_audit_does_not_flag_behavioral_semantic_check(self):
        tc = _make_tc(
            prompt="Analyze this code.",
            checks=[_make_check("Did the agent execute the analysis and generate a report?")],
        )
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertNotIn("keyword_only_check", issues)

    def test_audit_warns_on_low_test_count(self):
        """Fewer than 3 test cases triggers a low_test_count warning."""
        tcs = [
            _make_tc(tc_id="tc_001", prompt="Do something."),
            _make_tc(tc_id="tc_002", prompt="Do something else.",
                     checks=[_make_check("Did the agent handle this case?")]),
        ]
        _, payload = self._audit({"test_cases": tcs})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("low_test_count", issues)

    def test_audit_no_low_test_count_warning_with_three_cases(self):
        tcs = [
            _make_tc(tc_id="tc_001", prompt="Task one.",
                     checks=[_make_check("Did the agent execute task one?")]),
            _make_tc(tc_id="tc_002", prompt="Task two.",
                     checks=[_make_check("Did the agent execute task two?")]),
            _make_tc(tc_id="tc_003", prompt="Task three.",
                     checks=[_make_check("Did the agent execute task three?")]),
        ]
        _, payload = self._audit({"test_cases": tcs})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertNotIn("low_test_count", issues)

    def test_audit_empty_test_cases_returns_error_key(self):
        returncode, payload = self._audit({"test_cases": []})
        # Audit mode exits 1 (not 2) for errors via sys.exit(1 if error else 0)
        self.assertEqual(returncode, 1)
        self.assertIn("error", payload)
        self.assertEqual(payload["total_test_cases"], 0)

    def test_audit_output_contains_required_keys(self):
        _, payload = self._audit(VALID_CRITERIA)
        for key in ("findings", "fixable_count", "warning_count",
                    "should_regenerate", "unfixable_test_ids", "total_test_cases"):
            self.assertIn(key, payload, f"Missing key: {key}")

    def test_audit_fixable_count_matches_fixable_findings(self):
        # Use a valid test case with a fixable issue: slash command in prompt
        # but Skill not in allowed_tools
        tc = _make_tc(
            prompt="/some-skill do the thing",
            checks=[_make_check("Did the agent invoke the skill correctly?")],
            allowed_tools=["Read", "Bash"],
        )
        _, payload = self._audit({"test_cases": [tc]})
        self.assertNotIn("error", payload)
        fixable = [f for f in payload["findings"] if f.get("severity") == "fixable"]
        self.assertEqual(payload["fixable_count"], len(fixable))

    def test_audit_should_regenerate_false_when_few_unfixable(self):
        """With a single unfixable test case out of many, should_regenerate is False."""
        # Build 4 good cases + 1 with keyword-only check (unfixable)
        good_tcs = [
            _make_tc(tc_id=f"tc_{i}", prompt=f"Task {i}.",
                     checks=[_make_check(f"Did the agent handle task {i}?")])
            for i in range(1, 5)
        ]
        bad_tc = _make_tc(
            tc_id="tc_bad",
            prompt="Bad task.",
            checks=[_make_check("Does the output contain the string 'foobar'?")],
        )
        _, payload = self._audit({"test_cases": good_tcs + [bad_tc]})
        self.assertFalse(payload["should_regenerate"])


class TestRunnerContextHygiene(unittest.TestCase):
    """Tests for check_runner_context_hygiene (audit mode)."""

    def _audit(self, content):
        result = run_validate(content, extra_args=["--audit"])
        stdout = result.stdout.strip()
        json_start = stdout.find("{")
        if json_start == -1:
            raise ValueError(f"No JSON found in stdout: {stdout!r}")
        return result.returncode, json.loads(stdout[json_start:])

    def test_mkdir_in_runner_context_flagged(self):
        tc = _make_tc(
            runner_context="SIMULATION MODE: do not issue real tool calls.\n"
                           "mkdir -p /tmp/test-output",
        )
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("runner_context_side_effect", issues)

    def test_printf_redirect_in_runner_context_flagged(self):
        tc = _make_tc(
            runner_context="SIMULATION MODE: do not issue real tool calls.\n"
                           'printf "hello" > /tmp/output.txt',
        )
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("runner_context_side_effect", issues)

    def test_setup_block_in_runner_context_flagged(self):
        tc = _make_tc(
            runner_context="SIMULATION MODE: do not issue real tool calls.\n"
                           "SETUP: create a fixture directory",
        )
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("runner_context_side_effect", issues)

    def test_missing_simulation_header_flagged(self):
        tc = _make_tc(
            runner_context="Simulate a code review assistant reviewing this diff.",
        )
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertIn("runner_context_missing_simulation_header", issues)

    def test_empty_runner_context_does_not_trigger_hygiene(self):
        """Empty runner_context handled by check_runner_context_present, not hygiene."""
        tc = _make_tc(runner_context="")
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertNotIn("runner_context_side_effect", issues)
        self.assertNotIn("runner_context_missing_simulation_header", issues)

    def test_clean_simulation_runner_context_passes(self):
        tc = _make_tc(
            runner_context=(
                "SIMULATION MODE: do not issue real tool calls.\n"
                "Simulate a code review assistant."
            ),
        )
        _, payload = self._audit({"test_cases": [tc]})
        issues = [f["issue"] for f in payload["findings"]]
        self.assertNotIn("runner_context_side_effect", issues)
        self.assertNotIn("runner_context_missing_simulation_header", issues)


class TestDimensionWeights(unittest.TestCase):
    """Tests for check importance handling in validate mode."""

    def test_single_check_with_high_importance_is_clean(self):
        tc = _make_tc(
            prompt="Analyze this.",
            checks=[_make_check("Did the agent correctly analyze the input?", importance="HIGH")],
        )
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_CLEAN, result.stdout)

    def test_multiple_checks_all_valid(self):
        """Multiple checks each with proper rubric should be clean."""
        tc = _make_tc(
            prompt="Do the thing.",
            checks=[
                _make_check("Did the agent produce a high-quality output?", importance="HIGH"),
                _make_check("Did the agent handle the task safely?", importance="MEDIUM"),
            ],
        )
        result = run_validate({"test_cases": [tc]})
        self.assertEqual(result.returncode, EXIT_CLEAN, result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
