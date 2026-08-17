#!/usr/bin/env python3
"""Validate inter-step handoff data in hone workflow state files.

Deterministic schema validation for the typed interfaces defined between
hone's workflow steps. Each handoff has a declared schema (field names,
types, required vs optional, enum values, nested objects). This script
checks that the actual data in the workflow state file matches.

Usage:
    validate_handoff.py STATE_FILE --handoff artifact_context [--json]
    validate_handoff.py STATE_FILE --step phase1_structural_audit [--json]
    validate_handoff.py STATE_FILE --all [--json]

Exit codes:
    0 = all validated handoffs pass
    1 = one or more validation failures
    2 = usage error (bad args, file not found, invalid handoff name)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Schema DSL
# ---------------------------------------------------------------------------
# Each schema entry is a dict describing expected fields.
# Field spec keys:
#   type: "string" | "number" | "boolean" | "enum" | "object" | "array"
#   required: bool (default True)
#   non_empty: bool (for strings: must be non-empty; for arrays: must have items)
#   values: list[str] (for enum type)
#   fields: dict (for object type: nested field specs)
#   items: dict (for array type: schema for each element)
#   min_value: number (for number type: inclusive minimum)
#   max_value: number (for number type: inclusive maximum)


def _str(required: bool = True, non_empty: bool = False) -> dict:
    return {"type": "string", "required": required, "non_empty": non_empty}


def _num(
    required: bool = True,
    min_value: float | None = None,
    max_value: float | None = None,
    allow_null: bool = False,
) -> dict:
    spec: dict = {"type": "number", "required": required}
    if min_value is not None:
        spec["min_value"] = min_value
    if max_value is not None:
        spec["max_value"] = max_value
    if allow_null:
        spec["allow_null"] = True
    return spec


def _bool(required: bool = True) -> dict:
    return {"type": "boolean", "required": required}


def _enum(
    values: list[str], required: bool = True, allow_null: bool = False
) -> dict:
    return {
        "type": "enum",
        "required": required,
        "values": values,
        "allow_null": allow_null,
    }


def _obj(fields: dict, required: bool = True) -> dict:
    return {"type": "object", "required": required, "fields": fields}


def _arr(
    items: dict | None = None, required: bool = True, non_empty: bool = False
) -> dict:
    spec: dict = {"type": "array", "required": required, "non_empty": non_empty}
    if items is not None:
        spec["items"] = items
    return spec


# ---------------------------------------------------------------------------
# Handoff schemas (mirrors the SKILL.md typed interfaces)
# ---------------------------------------------------------------------------

HANDOFF_SCHEMAS: dict[str, dict] = {
    # Step 1 -> Step 2
    "artifact_context": {
        "fields": {
            "artifact_content": _str(non_empty=True),
            "artifact_path": _str(non_empty=True),
            "edit_path": _str(non_empty=True),
            "artifact_type": _enum(["skill", "command", "hook", "script"]),
            "artifact_name": _str(non_empty=True),
            "scope_intent": _obj(
                {
                    "complexity_tier": _enum(["lightweight", "standard", "complex"]),
                    "primary_dimension": _enum(
                        [
                            "correctness",
                            "instruction_clarity",
                            "orchestration",
                        ]
                    ),
                    "line_count": _num(min_value=0),
                }
            ),
        },
    },
    # Step 2 -> Phase 2 structural findings
    # transitions/handoffs are optional: structural_audit.py computes gate and
    # handoff coverage as document-wide aggregates, not per-transition, so the
    # script cannot emit honest per-item entries. The model MAY enrich the
    # handoff with them (validated against the item schemas when present).
    # The has_*/*_needed booleans ARE emitted deterministically by the script.
    "structural_audit": {
        "fields": {
            "structural_score": _num(min_value=0.0, max_value=1.0),
            "transitions": _arr(
                required=False,
                items={
                    "type": "object",
                    "fields": {
                        "from": _str(),
                        "to": _str(),
                        "gate_type": _enum(
                            [
                                "checklist",
                                "rubric",
                                "crucible",
                                "interaction_schema",
                                "none",
                            ]
                        ),
                        "status": _enum(["gated", "ungated"]),
                    },
                }
            ),
            "handoffs": _arr(
                required=False,
                items={
                    "type": "object",
                    "fields": {
                        "from": _str(),
                        "to": _str(),
                        "has_interface": _bool(),
                        "has_validation": _bool(),
                    },
                }
            ),
            "has_state_persistence": _bool(),
            "state_persistence_needed": _bool(),
            "has_anti_laziness_check": _bool(),
            "anti_laziness_needed": _bool(),
            "has_research_depth_enforcement": _bool(),
            "research_depth_needed": _bool(),
            "has_complexity_aware_analysis": _bool(),
            "complexity_aware_needed": _bool(),
            "findings": _arr(items={"type": "string"}),
        },
    },
    # Step 3 -> Step 3.5 or Step 4
    "routing_decision": {
        "fields": {
            "has_existing_criteria": _bool(),
            "criteria_path": _str(non_empty=True),
            "criteria_valid": _bool(),
            "route": _enum(["reuse", "regenerate", "fix_only"]),
        },
    },
    # Step 3.5 -> Step 4 or Step 5
    "criteria_audit": {
        "fields": {
            "criteria_existed": _bool(),
            "backup_path": _str(),
            "audit_ran": _bool(),
            "fixable_applied": _num(min_value=0),
            "warnings": _arr(items={"type": "string"}),
            "should_regenerate": _bool(),
            "criteria_deleted": _bool(),
            "classification_results": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "test_id": _str(non_empty=True),
                        "classification": _enum(
                            [
                                "behavioral",
                                "keyword",
                                "mixed",
                            ]
                        ),
                        "evidence": _str(),
                    },
                },
                required=False,
            ),
        },
    },
    # Step 4 -> Step 5
    "generated_criteria": {
        "fields": {
            "criteria_path": _str(non_empty=True),
            "test_count": _num(min_value=3),
            "validation_passed": _bool(),
            "dimensions": _arr(items={"type": "string"}, non_empty=True),
        },
    },
    # Step 5 -> Step 6
    "judge_results": {
        "fields": {
            "output_dir": _str(non_empty=True),
            "results_path": _str(non_empty=True),
            "completed": _bool(),
            "test_count": _num(min_value=1),
            "method": _enum(["eval_runner", "subagent"]),
        },
    },
    # Step 5.7 -> Step 6 (merged into eval_results)
    "reference_validation": {
        "fields": {
            "total_references": _num(min_value=0),
            "checked": _num(min_value=0),
            "skipped": _num(min_value=0),
            "broken": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "path": _str(non_empty=True),
                        "expanded_path": _str(),
                        "type": _enum(["file", "script", "skill", "command"]),
                        "issue": _enum(
                            [
                                "missing",
                                "not_executable",
                                "syntax_error",
                            ]
                        ),
                        "line_context": _str(),
                    },
                },
            ),
            "warnings": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "path": _str(),
                        "issue": _str(),
                    },
                },
                required=False,
            ),
            "script_test_coverage": _obj(
                {
                    "total_scripts": _num(min_value=0),
                    "scripts_with_tests": _num(min_value=0),
                    "scripts_without_tests": _arr(
                        items={
                            "type": "object",
                            "fields": {
                                "path": _str(non_empty=True),
                                "expected_test": _str(non_empty=True),
                            },
                        },
                    ),
                },
                required=False,
            ),
        },
    },
    # Phase 1 -> Phase 2
    # score_execution.py deliberately emits composite_score: null with grade
    # "INCONCLUSIVE" when no test is conclusive, and per-test status
    # "inconclusive" (score null) / "score_error". The schema must be able to
    # represent that output, or an all-inconclusive run has no legal encoding
    # and the mandatory pre-Phase-2 gate hard-stops with nothing to fix.
    "eval_results": {
        "fields": {
            "output_dir": _str(required=False),
            "results_path": _str(required=False),
            "composite_score": _num(min_value=0.0, max_value=1.0, allow_null=True),
            "grade": _enum(["A", "B", "C", "D", "F", "INCONCLUSIVE"]),
            "per_test": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "test_id": _str(non_empty=True),
                        "score": _num(
                            min_value=0.0, max_value=1.0, allow_null=True
                        ),
                        "status": _enum(
                            ["pass", "fail", "error", "inconclusive", "score_error"]
                        ),
                        "failure_type": _enum(
                            ["criteria_bug", "variance", "real_issue"],
                            required=False,
                        ),
                    },
                },
                non_empty=True,
            ),
            "actionable_failures": _num(min_value=0),
        },
    },
    # P2 Step 1 -> Step 1.7
    "triaged_results": {
        "fields": {
            "actionable_failures": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "test_id": _str(non_empty=True),
                        "score": _num(min_value=0.0, max_value=1.0),
                        "failure_type": _enum(["real_issue"]),
                    },
                },
            ),
            "excluded": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "test_id": _str(non_empty=True),
                        "reason": _enum(
                            [
                                "criteria_bug",
                                "variance",
                                "criteria_repaired",
                            ]
                        ),
                    },
                },
            ),
            "structural_findings": _arr(items={"type": "string"}),
        },
    },
    # Step 1.5 -> Step 2
    "criteria_repair": {
        "fields": {
            "pattern_matched": _num(min_value=0),
            "pattern_verified": _num(min_value=0),
            "pattern_reverted": _num(min_value=0),
            "unmatched": _num(min_value=0),
            "unmatched_test_ids": _arr(items={"type": "string"}),
            "repairs_applied": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "test_id": _str(non_empty=True),
                        "pattern": _str(non_empty=True),
                        "post_fix_score": _num(min_value=0.0, max_value=1.0),
                        "status": _enum(["accepted", "reverted"]),
                    },
                },
            ),
        },
    },
    # Step 1.7 -> Step 2
    "fresh_eyes": {
        "fields": {
            "proposals": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "id": _str(non_empty=True),
                        "section": _str(non_empty=True),
                        "description": _str(non_empty=True),
                        "source_test": _str(non_empty=True),
                        "fix_type": _enum(["structural", "content"]),
                    },
                },
            ),
        },
    },
    # P2 Step 2 -> Step 3
    "improvement_findings": {
        "fields": {
            "findings": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "id": _str(non_empty=True),
                        "fix_type": _enum(
                            [
                                "structural",
                                "content",
                                "criteria",
                                "reference",
                            ]
                        ),
                        "section": _str(non_empty=True),
                        "description": _str(non_empty=True),
                        "source": _str(non_empty=True),
                        "priority": _enum(["HIGH", "MED", "LOW"]),
                        "agreement": _enum(
                            [
                                "both",
                                "different_fix",
                                "single_source",
                                "contradiction",
                            ],
                            required=False,
                            # phase2-improvement.md documents `agreement: null`
                            # (eg when fresh-eyes review is skipped).
                            allow_null=True,
                        ),
                    },
                },
                non_empty=True,
            ),
            "reconciliation_summary": _obj(
                {
                    "total_proposals_main": _num(min_value=0),
                    "total_proposals_fresh": _num(min_value=0),
                    "agreed": _num(min_value=0),
                    "different_fix": _num(min_value=0),
                    "single_source": _num(min_value=0),
                    "contradictions": _num(min_value=0),
                    "skipped_contradictions": _arr(items={"type": "string"}),
                },
                required=False,
            ),
        },
    },
    # P2 Step 3 -> Step 4
    "improvement_plan": {
        "fields": {
            "edits": _arr(
                items={
                    "type": "object",
                    "fields": {
                        "id": _str(non_empty=True),
                        "target_section": _str(non_empty=True),
                        "change": _str(non_empty=True),
                        "approved": _bool(),
                    },
                },
                non_empty=True,
            ),
            "total_approved": _num(min_value=1),
        },
    },
    # P2 Step 4 -> Phase 3
    "applied_edits": {
        "fields": {
            "edit_count": _num(min_value=1),
            "confirmed_on_disk": _bool(),
            "artifact_before_snapshot": _str(non_empty=True),
            "syntax_check_passed": _bool(),
        },
    },
    # P1 Step 10 -> Step 11 (spec artifact generation; supplementary)
    "spec_artifacts": {
        "fields": {
            "evals_path": _str(non_empty=True),
            "grading_path": _str(non_empty=True),
            "timing_path": _str(non_empty=True),
            "benchmark_path": _str(non_empty=True),
            "has_baseline": _bool(),
            "generation_success": _bool(),
        },
    },
    # P2 Step 7 -> Phase 3 (trigger phrase testing)
    "trigger_test": {
        "fields": {
            "accuracy": _num(min_value=0.0, max_value=1.0),
            "should_trigger_pass_rate": _num(min_value=0.0, max_value=1.0),
            "should_not_trigger_pass_rate": _num(min_value=0.0, max_value=1.0),
            "description_improved": _bool(),
            "queries_path": _str(non_empty=True),
        },
    },
    # Hook pre-scan metadata (Step 1 discovery for hooks)
    "hook_metadata": {
        "fields": {
            "event_type": _enum(
                [
                    "Stop",
                    "PostToolUse",
                    "UserPromptSubmit",
                    "PreToolUse",
                    "SessionStart",
                    "Notification",
                ]
            ),
            "has_throttle": _bool(),
            "shebang": _str(),
        },
    },
}

# Which handoffs each step requires (inputs) and produces (outputs).
# Used by --step mode to validate that a completed step has valid outputs.
STEP_CONTRACTS: dict[str, dict[str, list[str]]] = {
    "phase1_structural_audit": {
        "requires": ["artifact_context"],
        "produces": ["structural_audit"],
    },
    "phase1_criteria_audit": {
        "requires": ["routing_decision"],
        "produces": ["criteria_audit"],
    },
    "phase1_evaluate": {
        "requires": [],
        "produces": ["eval_results"],
    },
    "phase1_reference_validation": {
        "requires": ["artifact_context"],
        "produces": ["reference_validation"],
    },
    "phase1_spec_artifacts": {
        "requires": ["eval_results"],
        "produces": ["spec_artifacts"],
    },
    "phase2_trigger_test": {
        "requires": [],
        "produces": ["trigger_test"],
    },
    "phase2_fresh_eyes": {
        "requires": ["eval_results"],
        "produces": ["fresh_eyes"],
    },
    "phase2_improve": {
        "requires": ["eval_results"],
        "produces": ["improvement_findings", "improvement_plan", "applied_edits"],
    },
    "phase3_reevaluate": {
        "requires": ["applied_edits", "eval_results"],
        "produces": [],
    },
}


# ---------------------------------------------------------------------------
# Validation engine
# ---------------------------------------------------------------------------


@dataclass
class ValidationError:
    path: str
    message: str
    severity: str = "error"  # "error" or "warning"


@dataclass
class ValidationResult:
    handoff: str
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    fields_checked: int = 0


def validate_value(
    value: object,
    spec: dict,
    path: str,
    errors: list[ValidationError],
) -> int:
    """Validate a single value against its spec. Returns count of fields checked."""
    checked = 0
    field_type = spec.get("type", "string")

    if field_type == "string":
        checked += 1
        if not isinstance(value, str):
            errors.append(
                ValidationError(
                    path,
                    f"expected string, got {type(value).__name__}",
                )
            )
        elif spec.get("non_empty") and not value.strip():
            errors.append(ValidationError(path, "string must be non-empty"))

    elif field_type == "number":
        checked += 1
        if value is None and spec.get("allow_null"):
            # Mirrors the enum branch: some producers legitimately emit null
            # (eg score_execution.py's inconclusive composite).
            pass
        elif isinstance(value, bool):
            # bool is a subclass of int in Python, but JSON booleans
            # are not numbers. Reject before the int/float check.
            errors.append(
                ValidationError(
                    path,
                    "expected number, got bool",
                )
            )
        elif not isinstance(value, (int, float)):
            errors.append(
                ValidationError(
                    path,
                    f"expected number, got {type(value).__name__}",
                )
            )
        else:
            if "min_value" in spec and value < spec["min_value"]:
                errors.append(
                    ValidationError(
                        path,
                        f"value {value} below minimum {spec['min_value']}",
                    )
                )
            if "max_value" in spec and value > spec["max_value"]:
                errors.append(
                    ValidationError(
                        path,
                        f"value {value} above maximum {spec['max_value']}",
                    )
                )

    elif field_type == "boolean":
        checked += 1
        if not isinstance(value, bool):
            errors.append(
                ValidationError(
                    path,
                    f"expected boolean, got {type(value).__name__}",
                )
            )

    elif field_type == "enum":
        checked += 1
        allowed = spec.get("values", [])
        if value is None and spec.get("allow_null"):
            pass
        elif value not in allowed:
            errors.append(
                ValidationError(
                    path,
                    f"value {value!r} not in allowed values: {allowed}",
                )
            )

    elif field_type == "object":
        checked += 1
        if not isinstance(value, dict):
            errors.append(
                ValidationError(
                    path,
                    f"expected object, got {type(value).__name__}",
                )
            )
        elif "fields" in spec:
            checked += validate_fields(value, spec["fields"], path, errors)

    elif field_type == "array":
        checked += 1
        if not isinstance(value, list):
            errors.append(
                ValidationError(
                    path,
                    f"expected array, got {type(value).__name__}",
                )
            )
        else:
            if spec.get("non_empty") and len(value) == 0:
                errors.append(ValidationError(path, "array must be non-empty"))
            if "items" in spec:
                item_spec = spec["items"]
                for idx, item in enumerate(value):
                    checked += validate_value(
                        item,
                        item_spec,
                        f"{path}[{idx}]",
                        errors,
                    )

    return checked


def validate_fields(
    data: dict,
    field_specs: dict,
    parent_path: str,
    errors: list[ValidationError],
) -> int:
    """Validate all fields in a dict against their specs. Returns fields checked."""
    checked = 0

    for field_name, spec in field_specs.items():
        field_path = f"{parent_path}.{field_name}" if parent_path else field_name
        required = spec.get("required", True)

        if field_name not in data:
            if required:
                errors.append(
                    ValidationError(
                        field_path,
                        "required field missing",
                    )
                )
            continue

        checked += validate_value(data[field_name], spec, field_path, errors)

    return checked


def validate_handoff(
    state: dict,
    handoff_name: str,
) -> ValidationResult:
    """Validate a single handoff's data in the workflow state."""
    if handoff_name not in HANDOFF_SCHEMAS:
        return ValidationResult(
            handoff=handoff_name,
            valid=False,
            errors=[
                ValidationError(
                    handoff_name,
                    f"unknown handoff schema: {handoff_name!r}. "
                    f"Valid names: {sorted(HANDOFF_SCHEMAS.keys())}",
                    severity="error",
                )
            ],
        )

    schema = HANDOFF_SCHEMAS[handoff_name]

    if handoff_name not in state:
        return ValidationResult(
            handoff=handoff_name,
            valid=False,
            errors=[
                ValidationError(
                    handoff_name,
                    f"handoff data not found in state file (key {handoff_name!r} missing)",
                )
            ],
        )

    data = state[handoff_name]
    if not isinstance(data, dict):
        return ValidationResult(
            handoff=handoff_name,
            valid=False,
            errors=[
                ValidationError(
                    handoff_name,
                    f"handoff data must be an object, got {type(data).__name__}",
                )
            ],
        )

    errors: list[ValidationError] = []
    checked = validate_fields(data, schema["fields"], handoff_name, errors)

    real_errors = [e for e in errors if e.severity == "error"]
    warnings = [e for e in errors if e.severity == "warning"]

    return ValidationResult(
        handoff=handoff_name,
        valid=len(real_errors) == 0,
        errors=real_errors,
        warnings=warnings,
        fields_checked=checked,
    )


def validate_step(
    state: dict,
    step_name: str,
) -> list[ValidationResult]:
    """Validate all handoffs for a completed step.

    Checks that:
    1. The step is marked 'done' or 'skip' in the state file
    2. All required input handoffs are present and valid
    3. All produced output handoffs are present and valid (unless step was skipped)
    """
    if step_name not in STEP_CONTRACTS:
        return [
            ValidationResult(
                handoff=step_name,
                valid=False,
                errors=[
                    ValidationError(
                        step_name,
                        f"unknown step: {step_name!r}. "
                        f"Valid steps: {sorted(STEP_CONTRACTS.keys())}",
                    )
                ],
            )
        ]

    contract = STEP_CONTRACTS[step_name]
    results: list[ValidationResult] = []

    steps = state.get("steps", {})
    step_status = steps.get(step_name)

    if step_status == "skip":
        # Skipped steps don't need output validation, but inputs should exist
        for handoff_name in contract["requires"]:
            if handoff_name in state:
                results.append(validate_handoff(state, handoff_name))
        if not results:
            results.append(
                ValidationResult(
                    handoff=f"{step_name}(skipped)",
                    valid=True,
                )
            )
        return results

    if step_status != "done":
        results.append(
            ValidationResult(
                handoff=step_name,
                valid=False,
                errors=[
                    ValidationError(
                        f"steps.{step_name}",
                        f"step status is {step_status!r}, expected 'done' or 'skip'",
                    )
                ],
            )
        )
        return results

    # Validate required inputs
    for handoff_name in contract["requires"]:
        results.append(validate_handoff(state, handoff_name))

    # Validate produced outputs
    for handoff_name in contract["produces"]:
        results.append(validate_handoff(state, handoff_name))

    return results


def validate_all(state: dict) -> list[ValidationResult]:
    """Validate every handoff present in the state file."""
    results: list[ValidationResult] = []
    for handoff_name in HANDOFF_SCHEMAS:
        if handoff_name in state:
            results.append(validate_handoff(state, handoff_name))
    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_text(results: list[ValidationResult]) -> str:
    """Human-readable output."""
    lines: list[str] = []
    all_valid = all(r.valid for r in results)

    for result in results:
        status = "PASS" if result.valid else "FAIL"
        lines.append(
            f"[{status}] {result.handoff} ({result.fields_checked} fields checked)"
        )
        for err in result.errors:
            lines.append(f"  ERROR: {err.path}: {err.message}")
        for warn in result.warnings:
            lines.append(f"  WARN:  {warn.path}: {warn.message}")

    lines.append("")
    if all_valid:
        lines.append(f"Result: ALL PASS ({len(results)} handoffs validated)")
    else:
        fail_count = sum(1 for r in results if not r.valid)
        lines.append(f"Result: {fail_count} FAIL, {len(results) - fail_count} PASS")

    return "\n".join(lines)


def format_json(results: list[ValidationResult]) -> str:
    """JSON output for programmatic consumption."""
    output = {
        "valid": all(r.valid for r in results),
        "handoffs_checked": len(results),
        "results": [
            {
                "handoff": r.handoff,
                "valid": r.valid,
                "fields_checked": r.fields_checked,
                "errors": [asdict(e) for e in r.errors],
                "warnings": [asdict(e) for e in r.warnings],
            }
            for r in results
        ],
    }
    return json.dumps(output, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate hone workflow handoff interfaces",
    )
    parser.add_argument(
        "state_file",
        type=str,
        help="Path to the workflow state JSON file",
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--handoff",
        type=str,
        help="Validate a specific handoff by name",
    )
    mode.add_argument(
        "--step",
        type=str,
        help="Validate all handoffs for a completed step",
    )
    mode.add_argument(
        "--all",
        action="store_true",
        help="Validate all handoffs present in the state file",
    )
    mode.add_argument(
        "--list-schemas",
        action="store_true",
        help="List all known handoff schema names and exit",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of human-readable text",
    )

    args = parser.parse_args()

    if args.list_schemas:
        for name in sorted(HANDOFF_SCHEMAS.keys()):
            schema = HANDOFF_SCHEMAS[name]
            field_count = len(schema.get("fields", {}))
            print(f"  {name} ({field_count} fields)")
        print("\nStep contracts:")
        for step_name, contract in sorted(STEP_CONTRACTS.items()):
            requires = ", ".join(contract["requires"]) or "(none)"
            produces = ", ".join(contract["produces"]) or "(none)"
            print(f"  {step_name}: requires [{requires}] -> produces [{produces}]")
        return 0

    state_path = Path(args.state_file)
    if not state_path.exists():
        print(f"Error: state file not found: {state_path}", file=sys.stderr)
        return 2

    try:
        state = json.loads(state_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in state file: {exc}", file=sys.stderr)
        return 2

    if not isinstance(state, dict):
        print("Error: state file root must be a JSON object", file=sys.stderr)
        return 2

    if args.handoff:
        results = [validate_handoff(state, args.handoff)]
    elif args.step:
        results = validate_step(state, args.step)
    elif args.all:
        results = validate_all(state)
        if not results:
            # An empty/truncated/corrupt state file has no known handoffs;
            # `all()` over an empty list would report a vacuous ALL PASS.
            print(
                "Error: no known handoffs found in state file; "
                "nothing to validate",
                file=sys.stderr,
            )
            return 2
    else:
        parser.print_help()
        return 2

    if args.json:
        print(format_json(results))
    else:
        print(format_text(results))

    return 0 if all(r.valid for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
