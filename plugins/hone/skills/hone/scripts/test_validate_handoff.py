#!/usr/bin/env python3
"""Tests for validate_handoff.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_handoff import (
    HANDOFF_SCHEMAS,
    STEP_CONTRACTS,
    validate_all,
    validate_fields,
    validate_handoff,
    validate_step,
    validate_value,
    ValidationError,
)


class TestValidateValue(unittest.TestCase):
    """Test the core value validation logic."""

    def test_string_valid(self) -> None:
        errors: list[ValidationError] = []
        validate_value("hello", {"type": "string"}, "test", errors)
        self.assertEqual(len(errors), 0)

    def test_string_wrong_type(self) -> None:
        errors: list[ValidationError] = []
        validate_value(42, {"type": "string"}, "test", errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected string", errors[0].message)

    def test_string_non_empty_fails(self) -> None:
        errors: list[ValidationError] = []
        validate_value("", {"type": "string", "non_empty": True}, "test", errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("non-empty", errors[0].message)

    def test_string_non_empty_whitespace(self) -> None:
        errors: list[ValidationError] = []
        validate_value("   ", {"type": "string", "non_empty": True}, "test", errors)
        self.assertEqual(len(errors), 1)

    def test_number_valid(self) -> None:
        errors: list[ValidationError] = []
        validate_value(3.14, {"type": "number"}, "test", errors)
        self.assertEqual(len(errors), 0)

    def test_number_int_valid(self) -> None:
        errors: list[ValidationError] = []
        validate_value(5, {"type": "number"}, "test", errors)
        self.assertEqual(len(errors), 0)

    def test_number_wrong_type(self) -> None:
        errors: list[ValidationError] = []
        validate_value("5", {"type": "number"}, "test", errors)
        self.assertEqual(len(errors), 1)

    def test_number_below_min(self) -> None:
        errors: list[ValidationError] = []
        validate_value(-1, {"type": "number", "min_value": 0}, "test", errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("below minimum", errors[0].message)

    def test_number_above_max(self) -> None:
        errors: list[ValidationError] = []
        validate_value(1.5, {"type": "number", "max_value": 1.0}, "test", errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("above maximum", errors[0].message)

    def test_number_rejects_bool(self) -> None:
        errors: list[ValidationError] = []
        validate_value(True, {"type": "number"}, "test", errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("got bool", errors[0].message)

    def test_number_rejects_false(self) -> None:
        errors: list[ValidationError] = []
        validate_value(False, {"type": "number", "min_value": 0}, "test", errors)
        self.assertEqual(len(errors), 1)

    def test_number_in_range(self) -> None:
        errors: list[ValidationError] = []
        validate_value(
            0.75,
            {"type": "number", "min_value": 0.0, "max_value": 1.0},
            "test",
            errors,
        )
        self.assertEqual(len(errors), 0)

    def test_boolean_valid(self) -> None:
        errors: list[ValidationError] = []
        validate_value(True, {"type": "boolean"}, "test", errors)
        self.assertEqual(len(errors), 0)

    def test_boolean_wrong_type(self) -> None:
        errors: list[ValidationError] = []
        validate_value(1, {"type": "boolean"}, "test", errors)
        self.assertEqual(len(errors), 1)

    def test_enum_valid(self) -> None:
        errors: list[ValidationError] = []
        validate_value(
            "skill",
            {"type": "enum", "values": ["skill", "command"]},
            "test",
            errors,
        )
        self.assertEqual(len(errors), 0)

    def test_enum_invalid(self) -> None:
        errors: list[ValidationError] = []
        validate_value(
            "widget",
            {"type": "enum", "values": ["skill", "command"]},
            "test",
            errors,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("not in allowed values", errors[0].message)

    def test_object_valid(self) -> None:
        errors: list[ValidationError] = []
        validate_value(
            {"name": "test"},
            {
                "type": "object",
                "fields": {"name": {"type": "string", "required": True}},
            },
            "test",
            errors,
        )
        self.assertEqual(len(errors), 0)

    def test_object_wrong_type(self) -> None:
        errors: list[ValidationError] = []
        validate_value("not an object", {"type": "object"}, "test", errors)
        self.assertEqual(len(errors), 1)

    def test_array_valid(self) -> None:
        errors: list[ValidationError] = []
        validate_value(
            ["a", "b"],
            {"type": "array", "items": {"type": "string"}},
            "test",
            errors,
        )
        self.assertEqual(len(errors), 0)

    def test_array_wrong_type(self) -> None:
        errors: list[ValidationError] = []
        validate_value("not an array", {"type": "array"}, "test", errors)
        self.assertEqual(len(errors), 1)

    def test_array_non_empty_fails(self) -> None:
        errors: list[ValidationError] = []
        validate_value([], {"type": "array", "non_empty": True}, "test", errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("non-empty", errors[0].message)

    def test_array_item_validation(self) -> None:
        errors: list[ValidationError] = []
        validate_value(
            ["good", 42],
            {"type": "array", "items": {"type": "string"}},
            "test",
            errors,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("[1]", errors[0].path)

    def test_nested_object_in_array(self) -> None:
        errors: list[ValidationError] = []
        validate_value(
            [{"id": "test", "score": 0.5}],
            {
                "type": "array",
                "items": {
                    "type": "object",
                    "fields": {
                        "id": {"type": "string", "required": True},
                        "score": {
                            "type": "number",
                            "required": True,
                            "min_value": 0.0,
                            "max_value": 1.0,
                        },
                    },
                },
            },
            "test",
            errors,
        )
        self.assertEqual(len(errors), 0)


class TestValidateFields(unittest.TestCase):
    """Test field-level validation with required/optional."""

    def test_required_field_missing(self) -> None:
        errors: list[ValidationError] = []
        validate_fields(
            {},
            {"name": {"type": "string", "required": True}},
            "root",
            errors,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("required field missing", errors[0].message)

    def test_optional_field_missing_ok(self) -> None:
        errors: list[ValidationError] = []
        validate_fields(
            {},
            {"name": {"type": "string", "required": False}},
            "root",
            errors,
        )
        self.assertEqual(len(errors), 0)

    def test_extra_fields_ignored(self) -> None:
        errors: list[ValidationError] = []
        validate_fields(
            {"name": "test", "extra": "ignored"},
            {"name": {"type": "string", "required": True}},
            "root",
            errors,
        )
        self.assertEqual(len(errors), 0)


class TestValidateHandoff(unittest.TestCase):
    """Test full handoff validation against state data."""

    def test_valid_artifact_context(self) -> None:
        state = {
            "artifact_context": {
                "artifact_content": "# My Skill\nDoes stuff",
                "artifact_path": "/home/user/.claude/skills/test/SKILL.md",
                "edit_path": "/home/user/.claude/skills/test/SKILL.md",
                "artifact_type": "skill",
                "artifact_name": "test",
                "scope_intent": {
                    "complexity_tier": "standard",
                    "primary_dimension": "instruction_clarity",
                    "line_count": 200,
                },
            },
        }
        result = validate_handoff(state, "artifact_context")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_invalid_artifact_type(self) -> None:
        state = {
            "artifact_context": {
                "artifact_content": "content",
                "artifact_path": "/path",
                "edit_path": "/path",
                "artifact_type": "widget",
                "artifact_name": "test",
                "scope_intent": {
                    "complexity_tier": "standard",
                    "primary_dimension": "correctness",
                    "line_count": 50,
                },
            },
        }
        result = validate_handoff(state, "artifact_context")
        self.assertFalse(result.valid)
        enum_errors = [
            err for err in result.errors if "not in allowed values" in err.message
        ]
        self.assertGreater(len(enum_errors), 0)

    def test_missing_handoff_key(self) -> None:
        result = validate_handoff({}, "artifact_context")
        self.assertFalse(result.valid)
        self.assertIn("not found", result.errors[0].message)

    def test_unknown_handoff_name(self) -> None:
        result = validate_handoff({}, "nonexistent")
        self.assertFalse(result.valid)
        self.assertIn("unknown handoff schema", result.errors[0].message)

    def test_valid_eval_results(self) -> None:
        state = {
            "eval_results": {
                "composite_score": 0.85,
                "grade": "B",
                "per_test": [
                    {
                        "test_id": "tc1",
                        "score": 0.9,
                        "status": "pass",
                    },
                    {
                        "test_id": "tc2",
                        "score": 0.7,
                        "status": "fail",
                        "failure_type": "real_issue",
                    },
                ],
                "actionable_failures": 1,
            },
        }
        result = validate_handoff(state, "eval_results")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_eval_results_score_out_of_range(self) -> None:
        state = {
            "eval_results": {
                "composite_score": 1.5,
                "grade": "A",
                "per_test": [{"test_id": "t", "score": 0.5, "status": "pass"}],
                "actionable_failures": 0,
            },
        }
        result = validate_handoff(state, "eval_results")
        self.assertFalse(result.valid)

    def test_eval_results_empty_per_test(self) -> None:
        state = {
            "eval_results": {
                "composite_score": 0.0,
                "grade": "F",
                "per_test": [],
                "actionable_failures": 0,
            },
        }
        result = validate_handoff(state, "eval_results")
        self.assertFalse(result.valid)
        non_empty_errors = [err for err in result.errors if "non-empty" in err.message]
        self.assertGreater(len(non_empty_errors), 0)

    def test_valid_routing_decision(self) -> None:
        state = {
            "routing_decision": {
                "has_existing_criteria": True,
                "criteria_path": "/home/user/.claude/skills/test/evals/eval_criteria.yaml",
                "criteria_valid": True,
                "route": "reuse",
            },
        }
        result = validate_handoff(state, "routing_decision")
        self.assertTrue(result.valid)

    def test_valid_hook_metadata(self) -> None:
        state = {
            "hook_metadata": {
                "event_type": "Stop",
                "has_throttle": True,
                "shebang": "#!/bin/bash",
            },
        }
        result = validate_handoff(state, "hook_metadata")
        self.assertTrue(result.valid)

    def test_handoff_data_not_dict(self) -> None:
        state = {"artifact_context": "this should be an object"}
        result = validate_handoff(state, "artifact_context")
        self.assertFalse(result.valid)
        self.assertIn("must be an object", result.errors[0].message)


class TestValidateStep(unittest.TestCase):
    """Test step-level validation (input+output contracts)."""

    def test_done_step_validates_outputs(self) -> None:
        state = {
            "steps": {"phase1_evaluate": "done"},
            "eval_results": {
                "composite_score": 0.8,
                "grade": "B",
                "per_test": [{"test_id": "t", "score": 0.8, "status": "pass"}],
                "actionable_failures": 0,
            },
        }
        results = validate_step(state, "phase1_evaluate")
        self.assertTrue(all(r.valid for r in results))

    def test_done_step_missing_outputs(self) -> None:
        state = {"steps": {"phase1_evaluate": "done"}}
        results = validate_step(state, "phase1_evaluate")
        self.assertFalse(all(r.valid for r in results))

    def test_skipped_step_ok(self) -> None:
        state = {"steps": {"phase1_structural_audit": "skip"}}
        results = validate_step(state, "phase1_structural_audit")
        self.assertTrue(all(r.valid for r in results))

    def test_pending_step_fails(self) -> None:
        state = {"steps": {"phase1_evaluate": "pending"}}
        results = validate_step(state, "phase1_evaluate")
        self.assertFalse(all(r.valid for r in results))
        self.assertIn("pending", results[0].errors[0].message)

    def test_unknown_step(self) -> None:
        results = validate_step({}, "nonexistent_step")
        self.assertFalse(all(r.valid for r in results))

    def test_step_with_required_inputs(self) -> None:
        state = {
            "steps": {"phase1_structural_audit": "done"},
            "artifact_context": {
                "artifact_content": "content",
                "artifact_path": "/path",
                "edit_path": "/path",
                "artifact_type": "skill",
                "artifact_name": "test",
                "scope_intent": {
                    "complexity_tier": "standard",
                    "primary_dimension": "correctness",
                    "line_count": 100,
                },
            },
            "structural_audit": {
                "structural_score": 0.8,
                "transitions": [],
                "handoffs": [],
                "has_state_persistence": True,
                "state_persistence_needed": True,
                "has_anti_laziness_check": True,
                "anti_laziness_needed": True,
                "has_research_depth_enforcement": False,
                "research_depth_needed": False,
                "has_complexity_aware_analysis": False,
                "complexity_aware_needed": False,
                "findings": [],
            },
        }
        results = validate_step(state, "phase1_structural_audit")
        self.assertTrue(
            all(r.valid for r in results),
            f"Failures: {[(r.handoff, [e.message for e in r.errors]) for r in results if not r.valid]}",
        )


class TestValidateAll(unittest.TestCase):
    """Test --all mode."""

    def test_validates_present_handoffs_only(self) -> None:
        state = {
            "hook_metadata": {
                "event_type": "Stop",
                "has_throttle": False,
                "shebang": "#!/bin/bash",
            },
        }
        results = validate_all(state)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].handoff, "hook_metadata")
        self.assertTrue(results[0].valid)

    def test_empty_state_returns_nothing(self) -> None:
        results = validate_all({})
        self.assertEqual(len(results), 0)


class TestSchemaCompleteness(unittest.TestCase):
    """Meta-tests: verify schema definitions are themselves consistent."""

    def test_all_step_contract_handoffs_have_schemas(self) -> None:
        for step_name, contract in STEP_CONTRACTS.items():
            for handoff in contract["requires"] + contract["produces"]:
                self.assertIn(
                    handoff,
                    HANDOFF_SCHEMAS,
                    f"Step {step_name} references handoff {handoff!r} "
                    f"which has no schema definition",
                )

    def test_all_schemas_have_fields(self) -> None:
        for name, schema in HANDOFF_SCHEMAS.items():
            self.assertIn(
                "fields",
                schema,
                f"Schema {name!r} is missing 'fields' key",
            )
            self.assertGreater(
                len(schema["fields"]),
                0,
                f"Schema {name!r} has empty fields",
            )


class TestScriptTestCoverage(unittest.TestCase):
    """Tests for the script_test_coverage optional field in reference_validation."""

    def test_valid_script_test_coverage(self) -> None:
        state = {
            "reference_validation": {
                "total_references": 3,
                "checked": 3,
                "skipped": 0,
                "broken": [],
                "script_test_coverage": {
                    "total_scripts": 6,
                    "scripts_with_tests": 4,
                    "scripts_without_tests": [
                        {
                            "path": "/path/to/helper.py",
                            "expected_test": "/path/to/test_helper.py",
                        },
                        {
                            "path": "/path/to/util.py",
                            "expected_test": "/path/to/test_util.py",
                        },
                    ],
                },
            },
        }
        result = validate_handoff(state, "reference_validation")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_reference_validation_without_script_coverage_still_valid(self) -> None:
        """script_test_coverage is optional -- omitting it should pass."""
        state = {
            "reference_validation": {
                "total_references": 2,
                "checked": 2,
                "skipped": 0,
                "broken": [],
            },
        }
        result = validate_handoff(state, "reference_validation")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_script_test_coverage_missing_required_fields(self) -> None:
        state = {
            "reference_validation": {
                "total_references": 1,
                "checked": 1,
                "skipped": 0,
                "broken": [],
                "script_test_coverage": {
                    "total_scripts": 3,
                    # missing scripts_with_tests and scripts_without_tests
                },
            },
        }
        result = validate_handoff(state, "reference_validation")
        self.assertFalse(result.valid)
        error_paths = [e.path for e in result.errors]
        self.assertTrue(
            any("scripts_with_tests" in p for p in error_paths),
            f"Expected scripts_with_tests error, got: {error_paths}",
        )

    def test_script_test_coverage_validates_array_items(self) -> None:
        """Each entry in scripts_without_tests must have path and expected_test."""
        state = {
            "reference_validation": {
                "total_references": 1,
                "checked": 1,
                "skipped": 0,
                "broken": [],
                "script_test_coverage": {
                    "total_scripts": 1,
                    "scripts_with_tests": 0,
                    "scripts_without_tests": [
                        {"path": "", "expected_test": "/path/test_x.py"},  # empty path
                    ],
                },
            },
        }
        result = validate_handoff(state, "reference_validation")
        self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()
