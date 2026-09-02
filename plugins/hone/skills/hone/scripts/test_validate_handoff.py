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

    def test_nan_rejected(self) -> None:
        # json.loads parses the nonstandard NaN literal by default, and
        # NaN < min / NaN > max are both False, so a bounded _num spec
        # silently blessed it (letting eg a Phase 3 regression check pass
        # on all-False comparisons).
        errors: list[ValidationError] = []
        validate_value(
            float("nan"),
            {"type": "number", "min_value": 0.0, "max_value": 1.0},
            "test",
            errors,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("NaN", errors[0].message)

    def test_nan_rejected_without_bounds(self) -> None:
        errors: list[ValidationError] = []
        validate_value(float("nan"), {"type": "number"}, "test", errors)
        self.assertEqual(len(errors), 1)

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

    def test_infinity_rejected_on_min_only_bounds(self) -> None:
        # json.loads parses the nonstandard Infinity literal by default,
        # and inf >= 0 satisfies a min-only bound, so counter fields
        # (actionable_failures, test_count, line_count) blessed +inf.
        errors: list[ValidationError] = []
        validate_value(
            float("inf"), {"type": "number", "min_value": 0}, "test", errors
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("Infinity", errors[0].message)

    def test_negative_infinity_rejected_without_bounds(self) -> None:
        errors: list[ValidationError] = []
        validate_value(float("-inf"), {"type": "number"}, "test", errors)
        self.assertEqual(len(errors), 1)

    def test_huge_int_does_not_crash_finiteness_check(self) -> None:
        # math.isfinite raises OverflowError on ints beyond float range;
        # the finiteness guard applies to floats only (ints are finite).
        errors: list[ValidationError] = []
        validate_value(
            10**400, {"type": "number", "min_value": 0}, "test", errors
        )
        self.assertEqual(len(errors), 0)

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
                "original_backup_path": "/tmp/hone-backup/SKILL.md",
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
                "power_verdict": "powered",
            },
        }
        result = validate_handoff(state, "eval_results")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_eval_results_without_a_power_verdict_fails(self) -> None:
        # Step 9a: a composite without a power verdict is a number, not a
        # result, and the gate is what enforces that.
        state = {
            "eval_results": {
                "composite_score": 0.85,
                "grade": "B",
                "per_test": [{"test_id": "t", "score": 0.85, "status": "pass"}],
                "actionable_failures": 0,
            },
        }
        result = validate_handoff(state, "eval_results")
        self.assertFalse(result.valid)
        self.assertTrue(any(e.path == "eval_results.power_verdict" for e in result.errors))

    def test_eval_results_power_verdict_outside_the_enum_fails(self) -> None:
        state = {
            "eval_results": {
                "composite_score": 0.85,
                "grade": "B",
                "per_test": [{"test_id": "t", "score": 0.85, "status": "pass"}],
                "actionable_failures": 0,
                "power_verdict": "pass",
            },
        }
        self.assertFalse(validate_handoff(state, "eval_results").valid)

    def test_eval_results_score_out_of_range(self) -> None:
        state = {
            "eval_results": {
                "composite_score": 1.5,
                "grade": "A",
                "per_test": [{"test_id": "t", "score": 0.5, "status": "pass"}],
                "actionable_failures": 0,
                "power_verdict": "powered",
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
                "power_verdict": "powered",
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

    def test_hook_metadata_accepts_harness_events_outside_any_list(self) -> None:
        # The hook-event vocabulary is owned by the Claude Code harness; a
        # closed enum went stale and rejected valid hooks (SubagentStop,
        # PreCompact, SessionEnd). Any non-empty string must validate.
        for event in ("SubagentStop", "PreCompact", "SessionEnd", "FutureEvent"):
            state = {
                "hook_metadata": {
                    "event_type": event,
                    "has_throttle": False,
                    "shebang": "#!/bin/bash",
                },
            }
            self.assertTrue(validate_handoff(state, "hook_metadata").valid, event)

    def test_hook_metadata_rejects_empty_event_type(self) -> None:
        state = {
            "hook_metadata": {
                "event_type": "",
                "has_throttle": False,
                "shebang": "#!/bin/bash",
            },
        }
        self.assertFalse(validate_handoff(state, "hook_metadata").valid)

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
                "power_verdict": "powered",
            },
        }
        results = validate_step(state, "phase1_evaluate")
        self.assertTrue(all(r.valid for r in results))

    def test_done_step_missing_outputs(self) -> None:
        state = {"steps": {"phase1_evaluate": "done"}}
        results = validate_step(state, "phase1_evaluate")
        self.assertFalse(all(r.valid for r in results))

    def test_skipped_step_with_valid_inputs_ok(self) -> None:
        state = {
            "steps": {"phase1_structural_audit": "skipped"},
            "artifact_context": {
                "artifact_content": "content",
                "artifact_path": "/path",
                "edit_path": "/path",
                "original_backup_path": "/tmp/hone-backup/SKILL.md",
                "artifact_type": "skill",
                "artifact_name": "test",
                "scope_intent": {
                    "complexity_tier": "standard",
                    "primary_dimension": "correctness",
                    "line_count": 100,
                },
            },
        }
        results = validate_step(state, "phase1_structural_audit")
        self.assertTrue(all(r.valid for r in results))

    def test_skipped_step_missing_required_input_fails(self) -> None:
        # A skipped structural audit with no artifact_context at all (and
        # therefore no original_backup_path for Phase 3 auto-revert) must
        # not produce a vacuous ALL PASS.
        state = {"steps": {"phase1_structural_audit": "skipped"}}
        results = validate_step(state, "phase1_structural_audit")
        self.assertFalse(all(r.valid for r in results))

    def test_skipped_step_without_required_inputs_ok(self) -> None:
        # phase1_evaluate requires no inputs; a skip records an explicit pass.
        state = {"steps": {"phase1_evaluate": "skipped"}}
        results = validate_step(state, "phase1_evaluate")
        self.assertTrue(all(r.valid for r in results))

    def test_no_improvement_run_shape_passes(self) -> None:
        # SKILL.md-sanctioned shape: Phase 1 found nothing to improve, so
        # phase2_improve and phase3_reevaluate are skipped and applied_edits
        # is never produced. Requiring it unconditionally forced the
        # executor to fabricate an applied_edits block.
        state = {
            "steps": {
                "phase1_evaluate": "done",
                "phase2_improve": "skipped",
                "phase3_reevaluate": "skipped",
            },
            "eval_results": {
                "composite_score": 0.9,
                "grade": "A",
                "per_test": [{"test_id": "t", "score": 0.9, "status": "pass"}],
                "actionable_failures": 0,
                "power_verdict": "improved",
            },
        }
        results = validate_step(state, "phase3_reevaluate")
        self.assertTrue(
            all(r.valid for r in results),
            [e.message for r in results for e in r.errors],
        )

    def test_no_improvement_run_still_requires_eval_results(self) -> None:
        # phase1_evaluate ran, so its output is a required input of the
        # skipped phase3_reevaluate and its absence is a real error.
        state = {
            "steps": {
                "phase1_evaluate": "done",
                "phase2_improve": "skipped",
                "phase3_reevaluate": "skipped",
            },
        }
        results = validate_step(state, "phase3_reevaluate")
        self.assertFalse(all(r.valid for r in results))

    def test_fix_only_run_shape_passes(self) -> None:
        # SKILL.md-sanctioned shape: --fix-only marks every Phase 1 step
        # "skipped", so artifact_context / routing_decision / eval_results
        # never exist. That absence is legal for the skipped steps.
        steps = {
            "phase1_structural_audit": "skipped",
            "phase1_criteria_audit": "skipped",
            "phase1_evaluate": "skipped",
            "phase1_spec_artifacts": "skipped",
            "phase1_reference_validation": "skipped",
        }
        state = {"steps": steps}
        for step_name in (
            "phase1_structural_audit",
            "phase1_criteria_audit",
            "phase1_reference_validation",
        ):
            results = validate_step(state, step_name)
            self.assertTrue(
                all(r.valid for r in results),
                (step_name, [e.message for r in results for e in r.errors]),
            )

    def test_skipped_step_with_invalid_present_input_fails(self) -> None:
        # An input that IS present must still validate, run shape aside.
        state = {
            "steps": {
                "phase1_evaluate": "skipped",
                "phase2_improve": "skipped",
                "phase3_reevaluate": "skipped",
            },
            "eval_results": {"grade": "Z"},
        }
        results = validate_step(state, "phase3_reevaluate")
        self.assertFalse(all(r.valid for r in results))

    def test_null_steps_reports_cleanly(self) -> None:
        # {"steps": null} is the present-but-null pitfall; --step mode must
        # emit the clean "step status is None" error, not an AttributeError.
        results = validate_step({"steps": None}, "phase1_evaluate")
        self.assertFalse(all(r.valid for r in results))
        self.assertIn("None", results[0].errors[0].message)

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
                "original_backup_path": "/tmp/hone-backup/SKILL.md",
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


def _fix_only_steps(**overrides: str) -> dict:
    """A steps map in SKILL.md's --fix-only entry shape."""
    from hone_common import PHASE1_STEPS, PHASE23_STEPS

    steps = {
        **{step: "skipped" for step in PHASE1_STEPS},
        **{step: "pending" for step in PHASE23_STEPS},
    }
    steps.update(overrides)
    return steps


class TestValidateAll(unittest.TestCase):
    """Test --all mode (run-shape aware; see hone_common's table)."""

    def test_validates_present_handoffs_in_fix_only_shape(self) -> None:
        # The fix-only shape expects no handoffs, so only present ones are
        # validated — but present ones ARE validated.
        state = {
            "steps": _fix_only_steps(),
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

    def test_empty_state_is_not_a_vacuous_pass(self) -> None:
        # A truncated/corrupt state file ({}: no steps, no handoffs)
        # derives the normal shape, whose untracked Phase 1 handoffs
        # (artifact_context, routing_decision) are always expected; their
        # absence must surface as failures, never as ALL PASS over an
        # empty result set.
        results = validate_all({})
        self.assertTrue(results)
        self.assertFalse(all(r.valid for r in results))
        self.assertIn("artifact_context", {r.handoff for r in results})

    def test_fix_only_entry_yields_no_results(self) -> None:
        # SKILL.md's --fix-only entry: all Phase 1 steps skipped, zero
        # handoff blocks. Nothing is expected, so nothing is validated
        # (the CLI maps the empty set to an explicit pass, exit 0).
        self.assertEqual(validate_all({"steps": _fix_only_steps()}), [])

    def test_missing_expected_handoff_is_flagged(self) -> None:
        # Normal shape with phase1_evaluate done but eval_results absent:
        # --all must demand it (checking only present handoffs let a
        # truncated file pass vacuously).
        results = validate_all({"steps": {"phase1_evaluate": "done"}})
        eval_rows = [r for r in results if r.handoff == "eval_results"]
        self.assertTrue(eval_rows)
        self.assertFalse(eval_rows[0].valid)

    def test_fix_only_after_phase2_expects_phase2_outputs(self) -> None:
        # Once a fix-only run's phase2_improve is done, its outputs are
        # expected; their absence fails --all even in the lax shape.
        state = {"steps": _fix_only_steps(phase2_improve="done")}
        failing = {r.handoff for r in validate_all(state) if not r.valid}
        self.assertIn("applied_edits", failing)


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


class TestSpecArtifactsAndTriggerTestSchemas(unittest.TestCase):
    """The two documented handoffs that previously had no schema at all."""

    def test_valid_spec_artifacts(self) -> None:
        state = {
            "spec_artifacts": {
                "evals_path": "/out/evals.json",
                "grading_path": "/out/grading.json",
                "timing_path": "/out/timing.json",
                "benchmark_path": "/out/benchmark.json",
                "has_baseline": True,
                "generation_success": True,
            },
        }
        result = validate_handoff(state, "spec_artifacts")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_spec_artifacts_missing_field_fails(self) -> None:
        state = {
            "spec_artifacts": {
                "evals_path": "/out/evals.json",
                "has_baseline": False,
            },
        }
        result = validate_handoff(state, "spec_artifacts")
        self.assertFalse(result.valid)

    def test_valid_trigger_test(self) -> None:
        state = {
            "trigger_test": {
                "accuracy": 0.9,
                "should_trigger_pass_rate": 1.0,
                "should_not_trigger_pass_rate": 0.8,
                "description_improved": True,
                "queries_path": "/out/trigger_queries.json",
            },
        }
        result = validate_handoff(state, "trigger_test")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_trigger_test_accuracy_out_of_range_fails(self) -> None:
        state = {
            "trigger_test": {
                "accuracy": 1.5,
                "should_trigger_pass_rate": 1.0,
                "should_not_trigger_pass_rate": 0.8,
                "description_improved": False,
                "queries_path": "/out/trigger_queries.json",
            },
        }
        result = validate_handoff(state, "trigger_test")
        self.assertFalse(result.valid)


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


class TestStructuralAuditScriptOutputValidates(unittest.TestCase):
    """structural_audit.py's own --json output must satisfy the handoff schema.

    Regression: the schema used to require transitions/handoffs plus 8 booleans
    the script never emitted, so every audited run hard-stopped at Phase 2
    handoff validation unless the model fabricated the fields.
    """

    def test_audit_output_passes_structural_audit_schema(self) -> None:
        from structural_audit import audit

        content = (
            "## Step 1: Load\nWrite state to /tmp/workflow-x.json\n"
            "**Gate:** - [ ] loaded\n"
            "## Step 2: Report\nDone.\n"
        )
        output = audit(content, "skill", "some-skill", "standard")
        output["artifact_path"] = "/tmp/some-skill/SKILL.md"
        output["artifact_type"] = "skill"

        state = {"structural_audit": output}
        result = validate_handoff(state, "structural_audit")
        self.assertTrue(
            result.valid,
            f"Errors: {[e.message for e in result.errors]}",
        )
        audit_rows = [
            r for r in validate_all(state) if r.handoff == "structural_audit"
        ]
        self.assertTrue(audit_rows)
        self.assertTrue(audit_rows[0].valid)

    def test_null_structural_score_validates(self) -> None:
        """A hook/script whose only scoring pillar is security reports null."""
        from structural_audit import audit

        output = audit("#!/bin/bash\nexit 0\n", "hook", "some-hook", "standard")
        self.assertIsNone(output["structural_score"])
        output["artifact_path"] = "/tmp/some-hook.sh"
        output["artifact_type"] = "hook"

        result = validate_handoff({"structural_audit": output}, "structural_audit")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")


class TestRunShapeTable(unittest.TestCase):
    """Both --step and --all consult hone_common's run-shape table."""

    IMPROVEMENT_HANDOFFS = {
        "improvement_findings": {
            "findings": [
                {
                    "id": "F1",
                    "fix_type": "content",
                    "section": "Usage",
                    "description": "clarify flag",
                    "source": "eval",
                    "priority": "HIGH",
                }
            ],
        },
        "improvement_plan": {
            "edits": [
                {
                    "id": "F1",
                    "target_section": "Usage",
                    "change": "clarified",
                    "approved": True,
                }
            ],
            "total_approved": 1,
        },
        "applied_edits": {
            "edit_count": 1,
            "confirmed_on_disk": True,
            "artifact_before_snapshot": "/tmp/snap.md",
            "syntax_check_passed": True,
        },
    }

    def test_table_covers_the_step_contract_vocabulary(self) -> None:
        # The declarative table and STEP_CONTRACTS must describe the same
        # step vocabulary, and the lax shapes must partition it.
        from hone_common import RUN_SHAPE_ACTIVE_STEPS

        self.assertEqual(
            RUN_SHAPE_ACTIVE_STEPS["normal"], frozenset(STEP_CONTRACTS)
        )
        self.assertEqual(
            RUN_SHAPE_ACTIVE_STEPS["fix-only"]
            | RUN_SHAPE_ACTIVE_STEPS["no-improvement"],
            RUN_SHAPE_ACTIVE_STEPS["normal"],
        )
        self.assertEqual(
            RUN_SHAPE_ACTIVE_STEPS["fix-only"]
            & RUN_SHAPE_ACTIVE_STEPS["no-improvement"],
            frozenset(),
        )

    def test_fix_only_done_improve_step_needs_no_eval_results(self) -> None:
        # Round-3 regression: the skipped-step carve-out did not extend to
        # done steps, so a fix-only run's completed phase2_improve demanded
        # the eval_results that shape never produces, and the executor's
        # only paths forward were fabricating the block or skipping the
        # mandatory gate.
        state = {
            "steps": _fix_only_steps(phase2_improve="done"),
            **self.IMPROVEMENT_HANDOFFS,
        }
        results = validate_step(state, "phase2_improve")
        self.assertTrue(
            all(r.valid for r in results),
            [e.message for r in results for e in r.errors],
        )

    def test_fix_only_done_fresh_eyes_needs_no_eval_results(self) -> None:
        state = {
            "steps": _fix_only_steps(phase2_fresh_eyes="done"),
            "fresh_eyes": {"proposals": []},
        }
        results = validate_step(state, "phase2_fresh_eyes")
        self.assertTrue(
            all(r.valid for r in results),
            [e.message for r in results for e in r.errors],
        )

    def test_fix_only_done_phase3_needs_only_applied_edits(self) -> None:
        state = {
            "steps": _fix_only_steps(
                phase2_improve="done", phase3_reevaluate="done"
            ),
            **self.IMPROVEMENT_HANDOFFS,
        }
        results = validate_step(state, "phase3_reevaluate")
        self.assertTrue(
            all(r.valid for r in results),
            [e.message for r in results for e in r.errors],
        )

    def test_normal_done_improve_step_still_requires_eval_results(self) -> None:
        # In the normal shape phase1_evaluate ran, so its output is a hard
        # requirement of the done phase2_improve; the lax fix-only rule
        # must not leak into runs that did evaluate.
        state = {
            "steps": {"phase1_evaluate": "done", "phase2_improve": "done"},
            **self.IMPROVEMENT_HANDOFFS,
        }
        results = validate_step(state, "phase2_improve")
        self.assertFalse(all(r.valid for r in results))
        failing = {r.handoff for r in results if not r.valid}
        self.assertIn("eval_results", failing)


class TestCliExitCodes(unittest.TestCase):
    """Exit-code contract: 0 pass, 1 validation failure, 2 usage error."""

    def _run(self, state: dict, *args: str):
        import json as _json
        import subprocess
        import tempfile

        script = str(Path(__file__).parent / "validate_handoff.py")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as handle:
            _json.dump(state, handle)
            tmp_path = handle.name
        return subprocess.run(
            [sys.executable, script, tmp_path, *args],
            capture_output=True,
            text=True,
        )

    def test_unknown_handoff_name_is_usage_error(self) -> None:
        # A typo'd name exited 1, indistinguishable from a real state-file
        # failure, misdirecting exit-code consumers into "fix the state
        # file" loops. The docstring contract says 2.
        result = self._run({}, "--handoff", "eval_result")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("unknown handoff name", result.stderr)

    def test_unknown_step_name_is_usage_error(self) -> None:
        result = self._run({}, "--step", "phase1_structual_audit")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("unknown step name", result.stderr)

    def test_known_name_with_missing_data_is_exit_1(self) -> None:
        result = self._run({}, "--handoff", "eval_results")
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)

    def test_known_name_with_valid_data_is_exit_0(self) -> None:
        state = {
            "routing_decision": {
                "has_existing_criteria": True,
                "criteria_path": "/tmp/criteria.json",
                "criteria_valid": True,
                "route": "reuse",
            }
        }
        result = self._run(state, "--handoff", "routing_decision")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_all_on_fix_only_entry_is_exit_0(self) -> None:
        # The documented fix-only entry shape (steps only, zero handoff
        # blocks) deadlocked --all at exit 2 with "no known handoffs
        # found" and nothing fixable.
        result = self._run({"steps": _fix_only_steps()}, "--all")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("fix-only", result.stdout)

    def test_all_on_empty_state_is_exit_1_with_concrete_errors(self) -> None:
        result = self._run({}, "--all")
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("artifact_context", result.stdout)


class TestCliReadErrors(unittest.TestCase):
    """OSError at the CLI entry point must be the exit-2 usage error."""

    def test_directory_path_is_usage_error_not_traceback(self) -> None:
        # IsADirectoryError escaped the JSONDecodeError-only catch as a raw
        # traceback with exit 1, leaving --json consumers with zero
        # parseable bytes.
        import subprocess
        import tempfile

        script = str(Path(__file__).parent / "validate_handoff.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = subprocess.run(
                [sys.executable, script, tmp_dir, "--all", "--json"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("cannot read state file", result.stderr)


if __name__ == "__main__":
    unittest.main()
