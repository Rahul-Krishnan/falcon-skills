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

# Null-tolerant dict access (same-directory flat import). State files under
# validation are exactly where present-but-null keys show up; a raw
# state.get("steps", {}) returns None for {"steps": null} and crashes.
# The run-shape table (RUN_SHAPE_ACTIVE_STEPS / derive_run_shape) is the
# authoritative statement of which steps run in each documented run shape
# (normal, fix-only, no-improvement); see hone_common's module docstring.
# Both --step and --all consult it via _input_expected below.
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
    """A required non-empty string naming a DIRECTORY, not a file inside it.

    `output_dir` changed meaning in the same change that made it required: it
    held the path to results.json, it now holds the directory containing it.
    An old value passes the non-empty check untouched and then resolves
    `$PRIOR_OUTPUT_DIR/deterministic_scores.json` to
    `.../results.json/deterministic_scores.json`, a path that cannot exist,
    so the baseline reads as absent a phase later with nothing on stderr.
    Here is the only place that can catch the old shape while it still knows
    what the field means, and name the migration in the message.
    """
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
    """A boolean field. `must_be_true` makes the value, not the key, the gate.

    Some of these fields record a check the run is required to have passed
    before it may hand off at all. For those, requiring only that the key is
    present validates the sentence "I checked, and it failed" as readily as
    "I checked, and it passed" -- the read-back becomes a nudge with a schema
    around it. `false_message` says what the false value means, so the error
    names the halt rather than the type.
    """
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
    items: dict | None = None, required: bool = True, non_empty: bool = False
) -> dict:
    spec: dict = {"type": "array", "required": required, "non_empty": non_empty}
    if items is not None:
        spec["items"] = items
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
    """`metadata.scorer_fingerprint` copied out of this round's scores.

    Optional, because every state file written before score_execution.py
    recorded one has no value to copy, and because step 5 already has a
    mechanical guard: step 3a's `check_eval_power.py` reads the fingerprint
    from each round's deterministic_scores.json and returns `not_measurable`
    on a mismatch or an absence, which the power precondition turns into "do
    not auto-revert". What this field adds is survival: after a compaction
    the state file is what the next round re-reads, and a record that names
    its own scorer can say whether its baseline is still comparable without
    re-reading a directory that may have been pruned.
    """
    return _str(required=False, non_empty=True)


# Migration hints. `eval_results.output_dir` went optional -> required and
# `power_verdict` was added as required in the same change, so a state file
# written before it and resumed after it (SKILL.md's resume protocol keeps
# runs alive across sessions) hard-stops at the mandatory pre-Phase-2 gate
# with nothing but "required field missing" to act on. The gate is correct to
# stop -- Phase 2 must not act on a composite with no power verdict beside
# it -- but a hard stop that does not say how to move is a dead end, and the
# validator is the only component that knows both the old shape and the new
# one. These travel with the field specs so the message arrives at the field
# that is missing. references/phase1-evaluation.md carries the same steps in
# prose; keep the two in step.
OUTPUT_DIR_MIGRATION = (
    "state files written before the power and overfit gates landed either "
    "omit this field or carry the pre-change meaning, the path to "
    "results.json. To migrate, set it to the DIRECTORY holding that round's "
    "results.json and deterministic_scores.json, and leave the file path "
    "itself in results_path"
)
# check_eval_power.py's positional argument is the EVAL CRITERIA FILE; the
# round directories reach it through --before/--after, as paths to each
# round's deterministic_scores.json. "Run it over this round's output_dir"
# read as a remedy that takes the directory positionally, and a directory
# there is an explicit exit-2 usage error -- a printed remedy that fails when
# followed literally is worse than none. SKILL.md's resume section and
# references/phase1-evaluation.md carry the same command; keep the three in
# step.
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
    # Step 2 -> Phase 2 structural findings
    # transitions/handoffs are optional: structural_audit.py computes gate and
    # handoff coverage as document-wide aggregates, not per-transition, so the
    # script cannot emit honest per-item entries. The model MAY enrich the
    # handoff with them (validated against the item schemas when present).
    # The has_*/*_needed booleans ARE emitted deterministically by the script.
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
            # Absolute floor 2, not 3: the contract is "at least 3 test
            # cases, 2 for the lightweight complexity tier"
            # (references/phase1-evaluation.md, Step 5 -> Step 6 gate). This
            # schema cannot see the tier, so it enforces the lower bound;
            # the tier-aware floor lives in the doc gate, which carries the
            # lightweight carve-out explicitly.
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
    # Phase 1 -> Phase 2
    # score_execution.py deliberately emits composite_score: null with grade
    # "INCONCLUSIVE" when no test is conclusive, and per-test status
    # "inconclusive" (score null) / "score_error". The schema must be able to
    # represent that output, or an all-inconclusive run has no legal encoding
    # and the mandatory pre-Phase-2 gate hard-stops with nothing to fix.
    # Documented in references/phase1-evaluation.md ("Handoff interface
    # (Phase 1 -> Phase 2)"); keep the enums there in sync with these.
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
            # Steps 6b and 9a (references/phase1-evaluation.md): the power
            # verdict recorded beside the composite. `powered` and
            # `underpowered` are the sizing values a first round writes; the
            # other four come from the before/after comparison. Required,
            # because a composite without it is the number Phase 2 must not
            # act on: an eval_results that omits it fails this gate the same
            # way one carrying a value outside the enum ("pass") does.
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
    # Phase 3 step 6 -> the next round's step 3a and step 5
    # (references/phase3-reevaluation.md). Stored under `round_{N}_scores`,
    # one key per round, so state keys matching ROUND_SCORES_KEY resolve to
    # this schema and --handoff takes the concrete key. Without `output_dir`
    # round N+1 falls back to Phase 1's baseline and credits round N's gain
    # to itself.
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
    # P2 Step 1 -> Step 1.7
    # Inconclusive tests (analyze_results --triage: classification
    # "inconclusive", score null) route to excluded[] with reason
    # "inconclusive" — never actionable_failures. Same widening as
    # eval_results above: without the slot an inconclusive run has no legal
    # encoding and the pre-Phase-2 gate hard-stops. Documented in
    # references/phase2-improvement.md ("Handoff interface (P2 Step 1 ->
    # Step 2)"); update both together.
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
            # True, not merely present. phase2-improvement.md calls the
            # post-edit read-back "a gate, not a nudge" on the strength of
            # this contract, and the step's gate checklist requires that the
            # re-read confirm every planned edit. A run whose read-back did
            # not confirm the edits has nothing to hand Phase 3: Phase 3
            # compares before/after scores and auto-reverts from
            # `artifact_before_snapshot`, both of which assume the edits are
            # on disk. The documented failure path (the stale-write guard)
            # STOPs without emitting this handoff at all, so no legitimate
            # flow records false here.
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
            # The paths this round wrote, which is what the scope guard
            # attributes from. No comparison of two tree states can tell this
            # run's write from the user's editor saving the same file, so
            # `check_scope.py --verify` reads the run's own declaration
            # instead: a declared out-of-scope change is a violation it may
            # revert, an undeclared one belongs to someone else and only gets
            # reported. Required and non-empty, because `edit_count >= 1` is:
            # a run that applied edits and cannot name a single file it wrote
            # has nothing to hand the guard, and the guard's answer for a
            # missing declaration is `not_measurable` -- a halt either way, so
            # it fails here where the message says what is wrong.
            "edited_paths": _arr(
                items={"type": "string", "non_empty": True},
                non_empty=True,
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
            # Not an enum: the hook-event vocabulary belongs to the Claude
            # Code harness, not this plugin. A hard-coded list went stale
            # (SubagentStop, PreCompact, SessionEnd were rejected as
            # invalid, blocking /hone on correct hooks) and every future
            # harness event would need a plugin release. Any non-empty
            # string is accepted; references/artifact-profiles.md documents
            # the vocabulary as open.
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
    # A Phase 3 round's output is its score record, written by step 6 under
    # `round_{N}_scores`. Declaring nothing here was the same class of hole
    # `convergence` was in validate_gates.REQUIRED_STEPS: a mandatory record
    # this script does not check is prose. A round that skipped step 6 passed
    # `--all` clean, and the next round's step 3a then fell back to
    # `eval_results.output_dir` -- Phase 1's baseline -- crediting round N's
    # gain to round N+1. The concrete key carries a round number the schema
    # table cannot name in advance, so the SCHEMA name stands in here and
    # `_validate_round_scores` resolves it to whichever rounds are on disk.
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
    """Whether a required input handoff must exist in this run shape.

    CONTRACT (the prose statement of hone_common's run-shape table, which
    SKILL.md's run shapes defer to): a handoff is required exactly when the
    step that produces it is active in the derived run shape AND actually
    ran. Requiring inputs unconditionally forces the executor of a shape
    that legitimately skips producers (fix-only skips all of Phase 1,
    no-improvement skips Phases 2-3) to fabricate handoff blocks; requiring
    only present keys lets a corrupt state file (no artifact_context, hence
    no original_backup_path for Phase 3 auto-revert) sail through as a
    vacuous ALL PASS. The table threads between the two, and it applies to
    "done" consumers as much as "skipped" ones — a fix-only run's done
    phase2_improve must not demand the eval_results that shape never
    produces.

    For handoffs produced by a tracked step (HANDOFF_PRODUCERS), the
    producer must be active in the shape and "done". The untracked
    producers (artifact_context, routing_decision, from Phase 1's Discover
    and routing steps) run whenever Phase 1 runs at all, so they are
    expected exactly when the shape activates Phase 1.
    """
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
    """`message`, plus the field's migration note when it carries one.

    A field that became required, or changed meaning, after state files were
    already on disk needs the remedy attached to the failure. "required field
    missing" alone is a hard stop at a mandatory gate with no next move; see
    OUTPUT_DIR_MIGRATION and POWER_VERDICT_MIGRATION.
    """
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
            # json.loads parses the nonstandard NaN/Infinity/-Infinity
            # literals by default. Comparisons cannot reject NaN, and the
            # min-only bounds below cannot reject +Infinity (inf >= 0), so
            # downstream range/regression checks would silently bless
            # either. Guarded to floats: ints are always finite, and
            # math.isfinite raises OverflowError on ints too large for
            # float.
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
                        _with_migration("required field missing", spec),
                    )
                )
            continue

        checked += validate_value(data[field_name], spec, field_path, errors)

    return checked


def _schema_name(handoff_name: str) -> str | None:
    """The HANDOFF_SCHEMAS entry `handoff_name` validates against, or None.

    Most handoffs are their own schema name. The per-round score records are
    keyed `round_1_scores`, `round_2_scores`, ... and share one schema, so
    the concrete key is what --handoff and the state file carry and the
    pattern is what resolves it. `round_scores` itself is not a state key.
    """
    if handoff_name in HANDOFF_SCHEMAS and handoff_name != ROUND_SCORES_SCHEMA:
        return handoff_name
    if ROUND_SCORES_KEY.match(handoff_name):
        return ROUND_SCORES_SCHEMA
    return None


def _valid_handoff_names() -> str:
    """The names `--handoff` actually accepts, as a message fragment.

    `round_scores` is a schema name, not a state key: `_schema_name` rejects
    it by design, so `--handoff round_scores` exits 2. Every place that lists
    valid names has to say so, or it advertises an argument that does not
    work. `main()` carried the parenthetical and the two listings below did
    not; they all go through here now.
    """
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
    """Validate Phase 3's per-round records, demanding at least one.

    The single place `round_{N}_scores` is checked, for both --step and --all,
    because the two used to disagree: --all validated whichever round keys
    happened to be present and --step demanded none at all, so a Phase 3 round
    that never wrote its record was invisible to both. The record is what the
    NEXT round reads (`output_dir` at step 3a, `per_test` at step 5); without
    it that round silently re-baselines on Phase 1's numbers and reports this
    round's gain as its own.

    Missing is reported against the generic key name, since which round number
    is missing is exactly what the state file does not say.
    """
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
    """Validate every handoff that is present or expected by the run shape.

    Consults the same run-shape table as --step (via _input_expected): a
    handoff whose producing step is active in the derived shape and marked
    "done" is validated even when absent, so a truncated state file cannot
    vacuously ALL PASS; a handoff the shape never produces (all of Phase 1
    on a fix-only run) is validated only when present. A fix-only run at
    Phase 2 entry therefore legitimately yields zero results.
    """
    steps = null_safe_get(state, "steps", {}, expected=dict)
    results: list[ValidationResult] = []
    for handoff_name in HANDOFF_SCHEMAS:
        if handoff_name == ROUND_SCORES_SCHEMA:
            continue  # pattern-keyed; the concrete keys are collected below
        if handoff_name in state or _input_expected(steps, handoff_name):
            results.append(validate_handoff(state, handoff_name))
    # Phase 3 writes one record per round under a key the schema table cannot
    # name in advance. Same run-shape gate as every other produced handoff
    # (`phase3_reevaluate` is its producer in STEP_CONTRACTS, so
    # `_input_expected` resolves it): validate whichever rounds are present,
    # and report the absence when the shape ran Phase 3 to "done" and wrote
    # none. Validating only what was present is what let a round skip its
    # record and still pass `--all`.
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
