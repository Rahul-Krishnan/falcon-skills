#!/usr/bin/env python3
"""Tests for validate_handoff.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_handoff import (
    HANDOFF_SCHEMAS,
    POWER_VERDICT_MIGRATION,
    ROUND_SCORES_SCHEMA,
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

                "output_dir": "/tmp/skill-eval/demo/run-1",                "composite_score": 0.85,
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

                "output_dir": "/tmp/skill-eval/demo/run-1",                "composite_score": 0.85,
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

                "output_dir": "/tmp/skill-eval/demo/run-1",                "composite_score": 0.85,
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

                "output_dir": "/tmp/skill-eval/demo/run-1",                "composite_score": 1.5,
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

                "output_dir": "/tmp/skill-eval/demo/run-1",                "composite_score": 0.0,
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

                "output_dir": "/tmp/skill-eval/demo/run-1",                "composite_score": 0.8,
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

                "output_dir": "/tmp/skill-eval/demo/run-1",                "composite_score": 0.9,
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

    def test_fix_only_done_phase3_needs_no_eval_results(self) -> None:
        # Its own round record is still required (a done step produced its
        # outputs in every shape); the eval_results a fix-only run never
        # writes is not.
        state = {
            "steps": _fix_only_steps(
                phase2_improve="done", phase3_reevaluate="done"
            ),
            **self.IMPROVEMENT_HANDOFFS,
            "round_1_scores": {
                "output_dir": "/tmp/skill-eval/demo/reeval-1",
                "composite_score": 0.82,
                "per_test": [{"test_id": "t", "score": 0.82, "status": "pass"}],
                "power_verdict": "improved",
            },
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


class TestReadBackIsAGate(unittest.TestCase):
    """`applied_edits.confirmed_on_disk` must be true, not merely present.

    Regression: the contract was `_bool()`, which requires the key. A handoff
    recording `confirmed_on_disk: false` validated, so the post-edit read-back
    that phase2-improvement.md calls "a gate, not a nudge" was unenforced --
    a run could report that its edits were not on disk and still hand off to
    Phase 3, which compares before/after scores and auto-reverts against a
    snapshot on the assumption that they are.
    """

    HANDOFF = {
        "edit_count": 1,
        "confirmed_on_disk": True,
        "artifact_before_snapshot": "/tmp/before.md",
        "syntax_check_passed": True,
    }

    def test_confirmed_read_back_validates(self) -> None:
        result = validate_handoff({"applied_edits": dict(self.HANDOFF)}, "applied_edits")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_unconfirmed_read_back_fails(self) -> None:
        handoff = dict(self.HANDOFF, confirmed_on_disk=False)
        result = validate_handoff({"applied_edits": handoff}, "applied_edits")
        self.assertFalse(result.valid)
        self.assertTrue(
            any(
                e.path.endswith("confirmed_on_disk") and "must be true" in e.message
                for e in result.errors
            ),
            [f"{e.path}: {e.message}" for e in result.errors],
        )

    def test_a_missing_key_still_fails(self) -> None:
        handoff = {k: v for k, v in self.HANDOFF.items() if k != "confirmed_on_disk"}
        result = validate_handoff({"applied_edits": handoff}, "applied_edits")
        self.assertFalse(result.valid)

    def test_a_non_boolean_is_still_a_type_error(self) -> None:
        handoff = dict(self.HANDOFF, confirmed_on_disk="yes")
        result = validate_handoff({"applied_edits": handoff}, "applied_edits")
        self.assertFalse(result.valid)
        self.assertTrue(
            any("expected boolean" in e.message for e in result.errors),
            [e.message for e in result.errors],
        )

    def test_the_constraint_is_opt_in(self) -> None:
        """A plain boolean field still records false as a value, not an error.

        Most of these booleans are findings (`has_state_persistence`,
        `criteria_existed`): false is information the next step needs. Only a
        field whose false value means the run may not continue opts in.
        """
        from validate_handoff import _bool

        errors: list = []
        validate_value(False, _bool(), "plain", errors)
        self.assertEqual(errors, [])
        validate_value(False, _bool(must_be_true=True), "gated", errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("must be true", errors[0].message)


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



class TestOutputDirIsLoadBearing(unittest.TestCase):
    """r3-S5. `eval_results.output_dir` is what Phase 3 step 3a reads as
    $PRIOR_OUTPUT_DIR, so an eval_results that omits it, or writes it empty,
    leaves Phase 3 with no baseline and has to fail at this gate rather than
    as check_eval_power's exit 2 a phase later."""

    def _state(self, **overrides):
        block = {
            "output_dir": "/tmp/skill-eval/demo/run-1",
            "composite_score": 0.85,
            "grade": "B",
            "per_test": [{"test_id": "t", "score": 0.85, "status": "pass"}],
            "actionable_failures": 0,
            "power_verdict": "powered",
        }
        block.update(overrides)
        return {"eval_results": {k: v for k, v in block.items() if v is not ...}}

    def test_eval_results_without_output_dir_fails(self) -> None:
        result = validate_handoff(self._state(output_dir=...), "eval_results")
        self.assertFalse(result.valid)
        self.assertTrue(any(e.path == "eval_results.output_dir" for e in result.errors))

    def test_eval_results_with_an_empty_output_dir_fails(self) -> None:
        self.assertFalse(validate_handoff(self._state(output_dir=""), "eval_results").valid)


class TestRoundScoresHaveASchema(unittest.TestCase):
    """r3-S5. Phase 3 step 6 writes `round_{N}_scores`, and the next round
    reads its `output_dir` (step 3a) and `per_test` (step 5); with no schema a
    round could omit `output_dir` and round N+1 fell back to Phase 1's
    baseline, crediting round N's gain to round N+1."""

    RECORD = {
        "output_dir": "/tmp/skill-eval/demo/reeval-1",
        "composite_score": 0.82,
        "per_test": [{"test_id": "t", "score": 0.82, "status": "pass"}],
        "power_verdict": "improved",
        "power_p_improved": 0.0312,
        "power_discordant": 6,
    }

    def test_a_complete_round_record_passes(self) -> None:
        result = validate_handoff({"round_1_scores": self.RECORD}, "round_1_scores")
        self.assertTrue(result.valid, f"Errors: {[e.message for e in result.errors]}")

    def test_a_round_record_without_output_dir_fails(self) -> None:
        record = {k: v for k, v in self.RECORD.items() if k != "output_dir"}
        result = validate_handoff({"round_2_scores": record}, "round_2_scores")
        self.assertFalse(result.valid)
        self.assertTrue(any(e.path == "round_2_scores.output_dir" for e in result.errors))

    def test_a_round_record_without_a_power_verdict_fails(self) -> None:
        record = {k: v for k, v in self.RECORD.items() if k != "power_verdict"}
        self.assertFalse(validate_handoff({"round_1_scores": record}, "round_1_scores").valid)

    def test_the_adjusted_baseline_is_validated_when_present(self) -> None:
        record = dict(self.RECORD)
        record["baseline_original"] = {"composite_score": 0.71, "per_test": [{"test_id": "t"}]}
        record["baseline_adjusted"] = {"composite_score": 0.74, "per_test": [{"test_id": "t"}]}
        self.assertTrue(validate_handoff({"round_1_scores": record}, "round_1_scores").valid)
        record["baseline_adjusted"] = {"composite_score": 0.74}
        self.assertFalse(validate_handoff({"round_1_scores": record}, "round_1_scores").valid)

    def test_eval_results_carries_the_same_adjusted_baseline(self) -> None:
        # Round 1's re-score writes into eval_results, the prior record there.
        block = {
            "output_dir": "/tmp/skill-eval/demo/run-1",
            "composite_score": 0.71,
            "grade": "C",
            "per_test": [{"test_id": "t", "score": 0.71, "status": "fail"}],
            "actionable_failures": 1,
            "power_verdict": "powered",
            "baseline_adjusted": {"composite_score": 0.74},
        }
        result = validate_handoff({"eval_results": block}, "eval_results")
        self.assertFalse(result.valid)
        self.assertTrue(any("baseline_adjusted" in e.path for e in result.errors))

    def test_validate_all_picks_up_every_round_present(self) -> None:
        state = {
            "steps": {},
            "round_1_scores": self.RECORD,
            "round_2_scores": {k: v for k, v in self.RECORD.items() if k != "output_dir"},
        }
        results = {r.handoff: r for r in validate_all(state)}
        self.assertIn("round_1_scores", results)
        self.assertIn("round_2_scores", results)
        self.assertTrue(results["round_1_scores"].valid)
        self.assertFalse(results["round_2_scores"].valid)
        self.assertNotIn("round_scores", results)

    def test_the_template_name_itself_is_not_a_handoff(self) -> None:
        result = validate_handoff({"round_scores": self.RECORD}, "round_scores")
        self.assertFalse(result.valid)
        self.assertIn("unknown handoff schema", result.errors[0].message)

    def test_the_cli_accepts_a_concrete_round_key(self) -> None:
        import json
        import subprocess
        import tempfile

        script = str(Path(__file__).parent / "validate_handoff.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"
            state_path.write_text(json.dumps({"round_3_scores": self.RECORD}))
            result = subprocess.run(
                [sys.executable, script, str(state_path), "--handoff", "round_3_scores"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class TestPreMigrationStateFilesGetAMigrationPath(unittest.TestCase):
    """r4-S1. `eval_results.output_dir` went optional -> required-non-empty and
    `power_verdict` was added as required, so a state file written before this
    change and resumed after it (SKILL.md's resume protocol keeps runs alive
    across sessions) hard-stops at the mandatory pre-Phase-2 gate. The stop is
    correct; a stop that says only "required field missing" is a dead end."""

    PRE_MIGRATION = {
        "results_path": "/tmp/skill-eval/demo/baseline/results.json",
        "composite_score": 0.71,
        "grade": "B",
        "per_test": [{"test_id": "t1", "score": 0.71, "status": "pass"}],
        "actionable_failures": 0,
    }

    def _messages(self, record):
        result = validate_handoff({"eval_results": record}, "eval_results")
        self.assertFalse(result.valid)
        return {e.path: e.message for e in result.errors}

    def test_both_new_required_fields_name_their_migration(self) -> None:
        messages = self._messages(self.PRE_MIGRATION)
        output_dir = messages["eval_results.output_dir"]
        self.assertIn("required field missing", output_dir)
        self.assertIn("Migration:", output_dir)
        self.assertIn("results_path", output_dir)
        verdict = messages["eval_results.power_verdict"]
        self.assertIn("required field missing", verdict)
        self.assertIn("check_eval_power.py", verdict)

    def test_the_old_output_dir_meaning_is_rejected_not_accepted(self) -> None:
        # Pre-change, `output_dir` held the path to results.json. That value is
        # a legal non-empty string, so without the directory check it validated
        # clean here and Phase 3 step 3a then resolved
        # `.../results.json/deterministic_scores.json`, a path that cannot
        # exist, reading the baseline as absent with nothing on stderr.
        record = dict(
            self.PRE_MIGRATION,
            output_dir="/tmp/skill-eval/demo/baseline/results.json",
            power_verdict="powered",
        )
        message = self._messages(record)["eval_results.output_dir"]
        self.assertIn("expected a directory", message)
        self.assertIn("Migration:", message)

    def test_a_directory_value_passes(self) -> None:
        record = dict(
            self.PRE_MIGRATION,
            output_dir="/tmp/skill-eval/demo/baseline",
            power_verdict="powered",
        )
        result = validate_handoff({"eval_results": record}, "eval_results")
        self.assertTrue(result.valid, [e.message for e in result.errors])

    def test_round_scores_carries_the_same_migration(self) -> None:
        record = {
            "output_dir": "/tmp/skill-eval/demo/reeval-1/results.json",
            "composite_score": 0.82,
            "per_test": [{"test_id": "t", "score": 0.82, "status": "pass"}],
            "power_verdict": "improved",
        }
        result = validate_handoff({"round_2_scores": record}, "round_2_scores")
        self.assertFalse(result.valid)
        self.assertIn("expected a directory", result.errors[0].message)


class TestSchemaListingsMatchWhatHandoffAccepts(unittest.TestCase):
    """r4-N3. `--list-schemas` and the unknown-name error both printed
    `round_scores` as a valid `--handoff` name, but `_schema_name` rejects it
    by design, so `--handoff round_scores` exits 2. Only `main()`'s message
    carried the clarifying parenthetical."""

    def _cli(self, *args):
        import json
        import subprocess
        import tempfile

        script = str(Path(__file__).parent / "validate_handoff.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"
            state_path.write_text(json.dumps({}))
            return subprocess.run(
                [sys.executable, script, str(state_path), *args],
                capture_output=True,
                text=True,
            )

    def test_list_schemas_says_how_round_scores_is_addressed(self) -> None:
        out = self._cli("--list-schemas").stdout
        line = next(
            l for l in out.splitlines() if l.strip().startswith(ROUND_SCORES_SCHEMA)
        )
        self.assertIn("round_<N>_scores", line)

    def test_the_unknown_name_error_says_the_same_thing(self) -> None:
        result = self._cli("--handoff", ROUND_SCORES_SCHEMA)
        self.assertEqual(result.returncode, 2)
        self.assertIn("round_<N>_scores", result.stderr)

    def test_every_listed_name_except_the_template_is_addressable(self) -> None:
        for name in sorted(HANDOFF_SCHEMAS):
            if name == ROUND_SCORES_SCHEMA:
                continue
            result = validate_handoff({}, name)
            self.assertNotIn(
                "unknown handoff schema", result.errors[0].message, name
            )


class TestAPhase3RoundMustRecordItsScores(unittest.TestCase):
    """r5-B5. `phase3_reevaluate` declared `produces: []`, and --all validated
    only the `round_*_scores` keys that happened to be present, so a Phase 3
    round that skipped step 6's record passed `validate_handoff.py --all`
    clean. The next round's step 3a then fell back to
    `eval_results.output_dir` -- Phase 1's baseline -- and credited round N's
    gain to round N+1.

    Same standard `convergence` is held to in validate_gates.REQUIRED_STEPS: a
    record the workflow calls mandatory that this script does not check is
    prose."""

    RECORD = {
        "output_dir": "/tmp/skill-eval/demo/reeval-1",
        "composite_score": 0.82,
        "per_test": [{"test_id": "t", "score": 0.82, "status": "pass"}],
        "power_verdict": "improved",
    }

    def _done_phase3(self, **extra) -> dict:
        return {"steps": _fix_only_steps(phase3_reevaluate="done"), **extra}

    def test_the_step_contract_declares_the_round_record(self) -> None:
        self.assertIn(
            ROUND_SCORES_SCHEMA, STEP_CONTRACTS["phase3_reevaluate"]["produces"]
        )

    def test_a_done_round_with_no_record_fails_all(self) -> None:
        results = validate_all(self._done_phase3())
        self.assertTrue(results, "a done Phase 3 round validated nothing at all")
        self.assertFalse(all(r.valid for r in results))
        messages = [e.message for r in results for e in r.errors]
        self.assertTrue(
            any("round_<N>_scores" in m for m in messages), messages
        )

    def test_a_done_round_with_no_record_fails_step_mode(self) -> None:
        results = validate_step(self._done_phase3(), "phase3_reevaluate")
        self.assertFalse(all(r.valid for r in results))

    def test_a_recorded_round_passes(self) -> None:
        state = self._done_phase3(round_1_scores=self.RECORD)
        self.assertTrue(
            all(r.valid for r in validate_all(state)),
            [e.message for r in validate_all(state) for e in r.errors],
        )
        self.assertTrue(
            all(r.valid for r in validate_step(state, "phase3_reevaluate"))
        )

    def test_a_malformed_record_still_fails_on_its_own_fields(self) -> None:
        record = {k: v for k, v in self.RECORD.items() if k != "output_dir"}
        results = validate_all(self._done_phase3(round_2_scores=record))
        self.assertFalse(all(r.valid for r in results))
        self.assertTrue(
            any(e.path == "round_2_scores.output_dir"
                for r in results for e in r.errors)
        )

    def test_a_round_that_never_ran_is_not_demanded(self) -> None:
        # The run-shape gate every other produced handoff gets: a pending or
        # skipped phase3_reevaluate has no record to show.
        for status in ("pending", "skipped"):
            state = {"steps": _fix_only_steps(phase3_reevaluate=status)}
            messages = [
                e.message for r in validate_all(state) for e in r.errors
            ]
            self.assertFalse(
                any("round_<N>_scores" in m for m in messages), status
            )

    def test_every_round_present_is_still_validated(self) -> None:
        state = self._done_phase3(
            round_1_scores=self.RECORD,
            round_2_scores={k: v for k, v in self.RECORD.items()
                            if k != "power_verdict"},
        )
        results = {r.handoff: r for r in validate_all(state)}
        self.assertTrue(results["round_1_scores"].valid)
        self.assertFalse(results["round_2_scores"].valid)


class TestThePowerVerdictRemedyIsRunnable(unittest.TestCase):
    """r5-B3. POWER_VERDICT_MIGRATION told the reader to "run
    check_eval_power.py over this round's output_dir". That script's only
    positional argument is the eval criteria file, and a directory there is an
    explicit exit-2 usage error, so the printed remedy failed when followed
    literally. A remedy that does not run is worse than no remedy: it sends
    the operator looking for a broken script."""

    def test_the_message_names_the_criteria_file_and_the_round_flags(self) -> None:
        self.assertIn("check_eval_power.py", POWER_VERDICT_MIGRATION)
        self.assertIn("EVAL CRITERIA FILE", POWER_VERDICT_MIGRATION)
        self.assertIn("--before", POWER_VERDICT_MIGRATION)
        self.assertIn("--after", POWER_VERDICT_MIGRATION)
        self.assertIn("deterministic_scores.json", POWER_VERDICT_MIGRATION)

    def test_the_message_does_not_pass_a_directory_positionally(self) -> None:
        import re

        self.assertIsNone(
            re.search(r"check_eval_power\.py`?\s+(?:over|on)\s+(?:this|that)",
                      POWER_VERDICT_MIGRATION),
            POWER_VERDICT_MIGRATION,
        )

    def test_the_remedy_the_message_prints_actually_runs(self) -> None:
        # The reproduction, end to end: the command shape the message now
        # describes exits 0 on a real pair of rounds, where the directory the
        # old message named exits 2.
        import json
        import os
        import subprocess
        import sys
        import tempfile

        power = str(Path(__file__).parent / "check_eval_power.py")
        with tempfile.TemporaryDirectory() as tmp:
            criteria = os.path.join(tmp, "eval_criteria.json")
            with open(criteria, "w") as handle:
                json.dump({"test_cases": [
                    {"id": f"t{i}", "test_profile": "execution"}
                    for i in range(6)
                ]}, handle)
            rounds = []
            for name, score in (("r1", 0.5), ("r2", 0.9)):
                os.makedirs(os.path.join(tmp, name))
                path = os.path.join(tmp, name, "deterministic_scores.json")
                with open(path, "w") as handle:
                    json.dump({
                        "per_test": [{"test_id": f"t{i}", "composite": score}
                                     for i in range(6)],
                        # Both rounds scored by the same scorer; without
                        # the fingerprint check_eval_power reports an
                        # unknown scorer rather than a verdict.
                        "metadata": {
                            "artifact_type": "skill",
                            "scorer_fingerprint": "ast1:0000000000000000",
                        },
                    }, handle)
                rounds.append(path)

            good = subprocess.run(
                [sys.executable, power, criteria, "--artifact-type", "skill",
                 "--before", rounds[0], "--after", rounds[1]],
                capture_output=True, text=True,
            )
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertIn("improved", good.stdout)

            sizing_only = subprocess.run(
                [sys.executable, power, criteria, "--artifact-type", "skill"],
                capture_output=True, text=True,
            )
            self.assertEqual(sizing_only.returncode, 0, sizing_only.stderr)

            # What the old wording asked for.
            directory = subprocess.run(
                [sys.executable, power, os.path.dirname(rounds[1])],
                capture_output=True, text=True,
            )
            self.assertEqual(directory.returncode, 2)


class TestTheRoundRecordCanNameItsScorer(unittest.TestCase):
    """`scorer_fingerprint` is optional on both score records.

    Step 5 re-reads the previous round from the state file, not from memory,
    and after a compaction that record is all it has. Optional because every
    state file written before score_execution.py recorded a fingerprint has
    no value to copy, and a required field would hard-stop a resumed run.
    """

    def _round(self, **extra):
        record = {
            "output_dir": "/tmp/hone-run/r1",
            "composite_score": 0.71,
            "per_test": [{"test_id": "t1", "score": 0.71, "status": "pass"}],
            "power_verdict": "improved",
        }
        record.update(extra)
        return {"round_1_scores": record}

    def _validate(self, state):
        from validate_handoff import validate_handoff

        return validate_handoff(state, "round_1_scores")

    def test_a_record_naming_its_scorer_validates(self):
        result = self._validate(
            self._round(scorer_fingerprint="ast1:aaaaaaaaaaaaaaaa")
        )
        self.assertTrue(result.valid, result.errors)

    def test_a_record_without_one_still_validates(self):
        result = self._validate(self._round())
        self.assertTrue(result.valid, result.errors)

    def test_an_empty_fingerprint_is_rejected(self):
        # "" would read as a recorded scorer named nothing; absent is the
        # encoding for "unknown scorer".
        result = self._validate(self._round(scorer_fingerprint=""))
        self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()
