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
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Use shared null-safe access and run-shape rules for both --step and --all.
from hone_common import RUN_SHAPE_ACTIVE_STEPS, derive_run_shape
from hone_common import get as null_safe_get


# ---------------------------------------------------------------------------
# Schema DSL
# ---------------------------------------------------------------------------
# Each schema entry is a dict describing expected fields.
# Field spec keys:
#   type: "string" | "number" | "boolean" | "enum" | "object" | "array"
#   required: bool (default True)
#   non_empty: bool (for strings: must be non-empty; for arrays: must have items)
#   must_be_true: bool (for booleans: false is a validation failure, not a value)
#   values: list[str] (for enum type)
#   fields: dict (for object type: nested field specs)
#   items: dict (for array type: schema for each element)
#   min_value: number (for number type: inclusive minimum)
#   max_value: number (for number type: inclusive maximum)


def _str(required: bool = True, non_empty: bool = False,
         migration: str | None = None) -> dict:
    spec: dict = {"type": "string", "required": required, "non_empty": non_empty}
    if migration is not None:
        spec["migration"] = migration
    return spec


def _dir(migration: str) -> dict:
    """Require a directory path. Legacy output_dir values pointed at results.json;
    reject that shape here with migration guidance before baseline lookup fails."""
    spec = _str(required=True, non_empty=True, migration=migration)
    spec["dir_path"] = True
    return spec


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


def _bool(required: bool = True, must_be_true: bool = False,
          false_message: str | None = None) -> dict:
    """Define a boolean field. must_be_true requires a successful check;
    false_message explains the failure instead of reporting a type error."""
    spec: dict = {"type": "boolean", "required": required}
    if must_be_true:
        spec["must_be_true"] = True
        if false_message is not None:
            spec["false_message"] = false_message
    return spec


def _enum(
    values: list[str], required: bool = True, allow_null: bool = False,
    migration: str | None = None,
) -> dict:
    spec: dict = {
        "type": "enum",
        "required": required,
        "values": values,
        "allow_null": allow_null,
    }
    if migration is not None:
        spec["migration"] = migration
    return spec


def _obj(fields: dict, required: bool = True) -> dict:
    return {"type": "object", "required": required, "fields": fields}


def _arr(
    items: dict | None = None, required: bool = True, non_empty: bool = False,
    migration: str | None = None,
) -> dict:
    spec: dict = {"type": "array", "required": required, "non_empty": non_empty}
    if items is not None:
        spec["items"] = items
    if migration is not None:
        spec["migration"] = migration
    return spec


def _baseline() -> dict:
    """A round's scores as re-read by the following round's criteria re-score.

    Optional on both `eval_results` and `round_scores`: it exists only when
    the next round changed the criteria file and re-scored this round's
    trace, and Phase 3 step 5 reads its `per_test` in place of the record's
    own when it does.
    """
    return _obj(
        {
            "composite_score": _num(min_value=0.0, max_value=1.0, allow_null=True),
            "per_test": _arr(non_empty=True),
        },
        required=False,
    )


def _scorer_fingerprint() -> dict:
    """Optional scorer fingerprint copied from this round's metadata for resumptions.
    Legacy states may omit it. check_eval_power independently treats missing or
    mismatched score-file fingerprints as not_measurable, blocking auto-revert."""
    return _str(required=False, non_empty=True)


# Migration hints for newly required output_dir and power_verdict fields.
# Attach remedies to field errors so older resumed states can be repaired.
# Keep these instructions aligned with phase1-evaluation.md.
OUTPUT_DIR_MIGRATION = (
    "state files written before the power and overfit gates landed either "
    "omit this field or carry the pre-change meaning, the path to "
    "results.json. To migrate, set it to the DIRECTORY holding that round's "
    "results.json and deterministic_scores.json, and leave the file path "
    "itself in results_path"
)
# check_eval_power takes the criteria file positionally and score-file paths
# via --before/--after. Keep this command aligned with SKILL.md resume guidance
# and phase1-evaluation.md; a directory argument exits 2.
POWER_VERDICT_MIGRATION = (
    "state files written before the power and overfit gates landed omit this "
    "field. To migrate, run check_eval_power.py on the EVAL CRITERIA FILE "
    "(its only positional argument; a directory there is a usage error) with "
    "the rounds passed as files: check_eval_power.py <eval_criteria.json> "
    "--artifact-type <type> --before <prior round output_dir>/"
    "deterministic_scores.json --after <this round's output_dir>/"
    "deterministic_scores.json, and record its top-level verdict; a round "
    "with no earlier round to compare against runs the sizing half alone "
    "(check_eval_power.py <eval_criteria.json> --artifact-type <type>, no "
    "--before/--after) and records that verdict instead (powered or "
    "underpowered)"
)
# Older states lack required edited_paths. Recover the executor's written
# paths from its records; a script cannot infer authorship. Keep the recovery
# and unrecoverable-case guidance aligned with phase2-improvement.md Step 6.
EDITED_PATHS_MIGRATION = (
    "state files written before the Step 6a scope guard landed have no "
    "edited_paths, because nothing read it then. To migrate, list every file "
    "this round wrote as an absolute path -- the artifact, a generated "
    "companion validator, anything else; the read-back at Step 6 walked that "
    "same list. If the round's edits cannot be recovered, do NOT invent the "
    "list and do NOT pass --declared-none: re-run Step 5a to take a fresh "
    "snapshot, or halt, because a declaration that is wrong is worse than one "
    "that is missing (the guard answers a missing one with not_measurable and "
    "a halt, and a wrong one with a revert instruction aimed at the wrong "
    "file)"
)


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
            # Phase 3 auto-revert and the recovery paths restore from this
            # backup; a state file without it would pass validation and then
            # have nothing to revert to.
            "original_backup_path": _str(non_empty=True),
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
    # Step 2 -> Phase 2 structural findings. The script emits aggregate coverage
    # and has_*/*_needed booleans. Optional per-item transitions/handoffs may be
    # model-enriched and are validated when present.
    "structural_audit": {
        "fields": {
            # null when no scoring pillar beyond the security scan applies to
            # this artifact type and tier: the audit measured nothing, and a
            # fabricated 1.0 is what that used to report.
            "structural_score": _num(min_value=0.0, max_value=1.0, allow_null=True),
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
            # Enforce the tier-independent floor of two. The doc gate requires
            # three except for lightweight artifacts; this schema cannot see tier.
            "test_count": _num(min_value=2),
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
    # Phase 1 -> Phase 2. Allow null/INCONCLUSIVE composites and inconclusive
    # or score_error tests, matching scorer output. Keep these enums aligned
    # with phase1-evaluation.md.
    "eval_results": {
        "fields": {
            # Directory holding results.json and deterministic_scores.json.
            # Phase 3 step 3a reads it as $PRIOR_OUTPUT_DIR; required here so a
            # missing baseline fails this gate, not check_eval_power a phase
            # later. Inconclusive runs still have an output directory.
            "output_dir": _dir(OUTPUT_DIR_MIGRATION),
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
            # Required power verdict beside the composite. First-round sizing yields
            # powered/underpowered; comparisons yield the other values. Phase 2
            # must not act on a composite without this verdict.
            "power_verdict": _enum(
                ["powered", "underpowered", "improved", "regressed",
                 "inconclusive", "not_measurable"],
                migration=POWER_VERDICT_MIGRATION,
            ),
            "power_p_improved": _num(required=False, min_value=0.0, max_value=1.0),
            "power_discordant": _num(required=False, min_value=0),
            # Written into this record by the next round's criteria re-score
            # (phase3-reevaluation.md); that round's step 5 reads
            # `baseline_adjusted.per_test` in place of `per_test` when present.
            "baseline_original": _baseline(),
            "baseline_adjusted": _baseline(),
            "scorer_fingerprint": _scorer_fingerprint(),
        },
    },
    # Phase 3 step 6 -> next round's baseline. Concrete round_<N>_scores keys
    # resolve to this schema. output_dir prevents reuse of the Phase 1 baseline
    # and double-counting the previous round's gain.
    "round_scores": {
        "fields": {
            "output_dir": _dir(OUTPUT_DIR_MIGRATION),
            "composite_score": _num(min_value=0.0, max_value=1.0, allow_null=True),
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
                    },
                },
                non_empty=True,
            ),
            "power_verdict": _enum(
                ["powered", "underpowered", "improved", "regressed",
                 "inconclusive", "not_measurable"],
                migration=POWER_VERDICT_MIGRATION,
            ),
            "power_p_improved": _num(required=False, min_value=0.0, max_value=1.0),
            "power_discordant": _num(required=False, min_value=0),
            "baseline_original": _baseline(),
            "baseline_adjusted": _baseline(),
            "scorer_fingerprint": _scorer_fingerprint(),
        },
    },
    # P2 Step 1 -> Step 1.7. Inconclusive tests go to excluded[] with reason
    # inconclusive and score null. Keep the Phase 2 handoff reference aligned.
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
                                "inconclusive",
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
            # Read-back must confirm every planned edit before Phase 3 compares
            # scores or auto-reverts. Failed verification halts without this handoff.
            "confirmed_on_disk": _bool(
                must_be_true=True,
                false_message=(
                    "must be true: the post-edit read-back did not confirm "
                    "the edits on disk, so the run halts rather than handing "
                    "off to Phase 3"
                ),
            ),
            "artifact_before_snapshot": _str(non_empty=True),
            "syntax_check_passed": _bool(),
            # Declare this round's written paths for check_scope --verify. Tree diffs
            # cannot distinguish executor writes from concurrent user edits. Declared
            # out-of-scope writes may be reverted; undeclared changes are only reported.
            # At least one path is required because edit_count >= 1; missing attribution
            # otherwise yields not_measurable and halts the guard.
            "edited_paths": _arr(
                items={"type": "string", "non_empty": True},
                non_empty=True,
                migration=EDITED_PATHS_MIGRATION,
            ),
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
    # P2 Step 7 -> Step 8 (trigger phrase testing)
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
            # Accept any non-empty hook-event string. The harness owns this
            # vocabulary; a plugin enum would reject newly added events.
            "event_type": _str(non_empty=True),
            "has_throttle": _bool(),
            "shebang": _str(),
        },
    },
}

# `round_{N}_scores` keys, one per Phase 3 round; all validate against the
# `round_scores` schema. Declared above STEP_CONTRACTS because the schema name
# is what `phase3_reevaluate` produces, and the contract table below names it.
ROUND_SCORES_KEY = re.compile(r"^round_[1-9]\d*_scores$")
ROUND_SCORES_SCHEMA = "round_scores"

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
    # Phase 3 must produce a round_<N>_scores record. Use its schema name here;
    # _validate_round_scores resolves concrete keys and catches missing records
    # before the next round falls back to Phase 1's baseline.
    "phase3_reevaluate": {
        "requires": ["applied_edits", "eval_results"],
        "produces": [ROUND_SCORES_SCHEMA],
    },
}

# Reverse map: handoff name -> the tracked step that produces it (from
# STEP_CONTRACTS "produces"). Handoffs absent here (artifact_context,
# routing_decision) are produced by untracked steps (Step 1 Discover,
# Step 3 criteria routing) that run whenever Phase 1 runs at all.
HANDOFF_PRODUCERS: dict[str, str] = {
    handoff: step
    for step, contract in STEP_CONTRACTS.items()
    for handoff in contract["produces"]
}


def _input_expected(steps: dict, handoff_name: str) -> bool:
    """True when this run shape requires the handoff.

    Tracked producers must be active and done. Untracked Phase 1 producers
    (artifact_context and routing_decision) are expected whenever Phase 1 is
    active. Apply this to done and skipped consumers alike: demand missing
    outputs from steps that ran, but never fabricate inputs from skipped phases."""
    active = RUN_SHAPE_ACTIVE_STEPS[derive_run_shape(steps)]
    producer = HANDOFF_PRODUCERS.get(handoff_name)
    if producer is not None:
        return producer in active and null_safe_get(steps, producer) == "done"
    return "phase1_evaluate" in active


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


def _with_migration(message: str, spec: dict) -> str:
    """Append the field's migration note, if any, so older states get a repair path."""
    migration = spec.get("migration")
    return f"{message}. Migration: {migration}" if migration else message


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
        elif spec.get("dir_path") and Path(value.strip()).name.endswith(".json"):
            # The pre-change `output_dir`, which named results.json itself.
            # It is a legal non-empty string, so without this it validates
            # clean here and fails silently one phase later, where
            # `$PRIOR_OUTPUT_DIR/deterministic_scores.json` resolves under a
            # file. Reported here, where the migration is still nameable.
            errors.append(
                ValidationError(
                    path,
                    _with_migration(
                        f"expected a directory, got a path to a file: "
                        f"{value.strip()!r}",
                        spec,
                    ),
                )
            )

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
        elif isinstance(value, float) and not math.isfinite(value):
            # Reject nonstandard JSON NaN/Infinity values before range checks.
            # Check floats only: ints are finite and huge ints overflow math.isfinite.
            errors.append(
                ValidationError(
                    path, "NaN and Infinity are not valid numbers"
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
        elif value is False and spec.get("must_be_true"):
            errors.append(
                ValidationError(
                    path,
                    spec.get(
                        "false_message",
                        "must be true; false means the check did not pass, "
                        "which is a halt, not a handoff",
                    ),
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
                # The migration note belongs here too: a resumed state file
                # can carry the key with an empty list as easily as it can
                # omit it, and the remedy is the same either way.
                errors.append(ValidationError(
                    path, _with_migration("array must be non-empty", spec)))
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
                        _with_migration("required field missing", spec),
                    )
                )
            continue

        checked += validate_value(data[field_name], spec, field_path, errors)

    return checked


def _schema_name(handoff_name: str) -> str | None:
    """Resolve a handoff key to its schema, or None. round_<N>_scores keys share
    round_scores; that schema name itself is not a valid state key."""
    if handoff_name in HANDOFF_SCHEMAS and handoff_name != ROUND_SCORES_SCHEMA:
        return handoff_name
    if ROUND_SCORES_KEY.match(handoff_name):
        return ROUND_SCORES_SCHEMA
    return None


def _valid_handoff_names() -> str:
    """List accepted --handoff names, explaining that round_scores requires a
    concrete round_<N>_scores key."""
    return (
        f"{', '.join(sorted(HANDOFF_SCHEMAS))} "
        f"({ROUND_SCORES_SCHEMA} is a schema name, addressed as "
        "round_<N>_scores)"
    )


def _round_scores_keys(state: dict) -> list[str]:
    """Every `round_{N}_scores` key present in `state`, in round order."""
    keys = [k for k in state if isinstance(k, str) and ROUND_SCORES_KEY.match(k)]
    return sorted(keys, key=lambda k: int(k.split("_")[1]))


def _validate_round_scores(state: dict) -> list[ValidationResult]:
    """Validate all round_<N>_scores records and require at least one.
    Both --step and --all use this check. The next round reads output_dir and
    per_test; missing records would reuse Phase 1's baseline and double-count
    gains. Report absence under the generic key because the round is unknown."""
    keys = _round_scores_keys(state)
    if not keys:
        return [
            ValidationResult(
                handoff="round_<N>_scores",
                valid=False,
                errors=[
                    ValidationError(
                        "round_<N>_scores",
                        "required handoff missing: a completed Phase 3 round "
                        "records its scores under round_<N>_scores "
                        "(round_1_scores for the first round). Write the "
                        "record with output_dir, composite_score, per_test "
                        "and power_verdict; the next round reads output_dir "
                        "for its baseline and falls back to "
                        "eval_results.output_dir without it, crediting this "
                        "round's gain to the next one",
                        severity="error",
                    )
                ],
            )
        ]
    return [validate_handoff(state, key) for key in keys]


def validate_handoff(
    state: dict,
    handoff_name: str,
) -> ValidationResult:
    """Validate a single handoff's data in the workflow state."""
    schema_name = _schema_name(handoff_name)
    if schema_name is None:
        return ValidationResult(
            handoff=handoff_name,
            valid=False,
            errors=[
                ValidationError(
                    handoff_name,
                    f"unknown handoff schema: {handoff_name!r}. "
                    f"Valid names: {_valid_handoff_names()}",
                    severity="error",
                )
            ],
        )

    schema = HANDOFF_SCHEMAS[schema_name]

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
    1. The step is marked 'done' or 'skipped' in the state file
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

    # null_safe_get tolerates {"steps": null} and a non-object steps value;
    # the validator most likely to meet malformed state files must report
    # them, not crash on them.
    steps = null_safe_get(state, "steps", {}, expected=dict)
    step_status = null_safe_get(steps, step_name)

    # SKILL.md's Mechanical Exit Gate and reuse path both write "skipped";
    # this is the canonical spelling for a skipped step status.
    if step_status == "skipped":
        # Skipped steps don't need output validation. Required inputs are
        # validated when present, and their absence is an error only when
        # the run shape expects them — see _input_expected for the contract
        # and hone_common's run-shape table for the shapes (fix-only,
        # no-improvement) that legitimately leave inputs absent.
        for handoff_name in contract["requires"]:
            if handoff_name in state or _input_expected(steps, handoff_name):
                results.append(validate_handoff(state, handoff_name))
        if not results:
            # Step has no required inputs (or none expected in this run
            # shape); record an explicit pass.
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
                        f"step status is {step_status!r}, expected 'done' or 'skipped'",
                    )
                ],
            )
        )
        return results

    # Validate required inputs. Same run-shape gate as the skipped branch:
    # a done step's input is demanded only when this run shape produced it
    # (a fix-only run's done phase2_improve has no eval_results to demand),
    # but anything present is validated regardless.
    for handoff_name in contract["requires"]:
        if handoff_name in state or _input_expected(steps, handoff_name):
            results.append(validate_handoff(state, handoff_name))

    # Validate produced outputs: a done step must have produced them, in
    # every run shape (it ran).
    for handoff_name in contract["produces"]:
        if handoff_name == ROUND_SCORES_SCHEMA:
            # Pattern-keyed: one record per round, resolved from the state.
            results.extend(_validate_round_scores(state))
            continue
        results.append(validate_handoff(state, handoff_name))

    if not results:
        # No inputs expected in this run shape and no declared outputs;
        # record an explicit pass so the report is never empty.
        results.append(ValidationResult(handoff=step_name, valid=True))

    return results


def validate_all(state: dict) -> list[ValidationResult]:
    """Validate present handoffs and missing outputs from active, done producers.
    Use the same run-shape rules as --step. A fix-only run at Phase 2 entry
    may validly have no handoffs yet."""
    steps = null_safe_get(state, "steps", {}, expected=dict)
    results: list[ValidationResult] = []
    for handoff_name in HANDOFF_SCHEMAS:
        if handoff_name == ROUND_SCORES_SCHEMA:
            continue  # pattern-keyed; the concrete keys are collected below
        if handoff_name in state or _input_expected(steps, handoff_name):
            results.append(validate_handoff(state, handoff_name))
    # Validate all present round records; require at least one when Phase 3 ran.
    # The schema names the producer, while concrete state keys name each round.
    if _round_scores_keys(state) or _input_expected(steps, ROUND_SCORES_SCHEMA):
        results.extend(_validate_round_scores(state))
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
            # This listing is read as the --handoff menu, and `round_scores`
            # is the one entry that is not one: passing it exits 2.
            addressing = (
                "; addressed as round_<N>_scores"
                if name == ROUND_SCORES_SCHEMA
                else ""
            )
            print(f"  {name} ({field_count} fields{addressing})")
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
    except OSError as exc:
        # IsADirectoryError/PermissionError are usage errors (exit 2), not
        # raw tracebacks that leave --json consumers with unparseable output.
        print(f"Error: cannot read state file: {exc}", file=sys.stderr)
        return 2

    if not isinstance(state, dict):
        print("Error: state file root must be a JSON object", file=sys.stderr)
        return 2

    # Unknown --handoff/--step names are usage errors (exit 2), per the
    # docstring contract: SKILL.md and the phase references treat exit 1 as
    # "fix the state file, re-validate", which misdirects an exit-code
    # consumer when the failure is a one-character typo in the name.
    if args.handoff:
        if _schema_name(args.handoff) is None:
            print(
                f"Error: unknown handoff name: {args.handoff!r}. "
                f"Valid names: {_valid_handoff_names()}",
                file=sys.stderr,
            )
            return 2
        results = [validate_handoff(state, args.handoff)]
    elif args.step:
        if args.step not in STEP_CONTRACTS:
            print(
                f"Error: unknown step name: {args.step!r}. "
                f"Valid steps: {', '.join(sorted(STEP_CONTRACTS))}",
                file=sys.stderr,
            )
            return 2
        results = validate_step(state, args.step)
    elif args.all:
        results = validate_all(state)
        if not results:
            steps = null_safe_get(state, "steps", {}, expected=dict)
            if derive_run_shape(steps) == "fix-only":
                # Documented fix-only entry shape (SKILL.md marks all
                # Phase 1 steps skipped; zero handoff blocks exist yet).
                # Nothing is expected, so nothing to validate is a pass,
                # not a corrupt file.
                results = [
                    ValidationResult(
                        handoff="(fix-only: no handoffs expected yet)",
                        valid=True,
                    )
                ]
            else:
                # Defensive: outside the fix-only shape, validate_all
                # always expects at least artifact_context, so an empty
                # result set means the expectations machinery is broken —
                # never report a vacuous ALL PASS.
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
