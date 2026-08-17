#!/usr/bin/env python3
"""Validate eval_criteria.json files against the canonical schema.

Uses the same validation DSL as validate_handoff.py to enforce structural
contracts on eval criteria before the eval runner launches.

Usage:
    validate_criteria_schema.py <path_to_eval_criteria.json>           # validate
    validate_criteria_schema.py <path_to_eval_criteria.json> --json    # JSON output
    validate_criteria_schema.py --help

Exit codes:
    0 = valid
    1 = validation errors
    2 = usage error (bad args, file not found, parse error)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Null-tolerant dict access: a present-but-null test_cases key defeats a
# raw data.get("test_cases", []) and would crash the semantic pass below.
from hone_common import get as null_safe_get

# Import validation DSL and engine from validate_handoff.py (same directory)
from validate_handoff import (
    ValidationError,
    ValidationResult,
    _arr,
    _bool,
    _enum,
    _num,
    _obj,
    _str,
    validate_fields,
)

# ---------------------------------------------------------------------------
# Importance-to-weight mapping (used by eval runner judge, not by scoring)
# ---------------------------------------------------------------------------

IMPORTANCE_WEIGHTS = {
    "CRITICAL": 3.0,
    "HIGH": 2.0,
    "MEDIUM": 1.0,
    "LOW": 0.5,
}

# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------

RUBRIC_SCHEMA = {
    "1": _str(non_empty=True),
    "2": _str(non_empty=True),
    "3": _str(non_empty=True),
    "4": _str(non_empty=True),
    "5": _str(non_empty=True),
}

CHECK_SCHEMA = {
    "type": "object",
    "fields": {
        "description": _str(non_empty=True),
        "importance": _enum(["CRITICAL", "HIGH", "MEDIUM", "LOW"]),
        "rubric": _obj(RUBRIC_SCHEMA),
    },
}

TEST_CASE_SCHEMA = {
    "type": "object",
    "fields": {
        "id": _str(non_empty=True),
        "name": _str(non_empty=True),
        "category": _enum([
            "invocation",
            "execution",
            "edge_case",
            "task_completion",
            "error_handling",
            "tool_usage",
            "business_impact",
        ]),
        # Optional: phase1-evaluation.md only instructs generating this field
        # for failure-mode cases (item 8), and score_execution.py falls back
        # to heuristic profile detection when it is absent. Requiring it made
        # every doc-shaped item 1-7 test case fail the pre-launch gate.
        "test_profile": _enum(
            [
                "execution",
                "knowledge_extraction",
                "error_handling",
                "side_effect_guarded",
                "failure_mode",
            ],
            required=False,
        ),
        "prompt": _str(non_empty=True),
        "runner_context": _str(non_empty=True),
        "allowed_tools": _arr(items={"type": "string"}, non_empty=True),
        "target_skills": _arr(items={"type": "string"}, required=False),
        "checks": _arr(items=CHECK_SCHEMA, non_empty=True),
        "required_present": _arr(items={"type": "string"}, required=False),
        "required_absent": _arr(items={"type": "string"}, required=False),
    },
}

CRITERIA_SCHEMA = {
    "project": _str(required=False),
    "skill_name": _str(required=False),
    "test_cases": _arr(items=TEST_CASE_SCHEMA, non_empty=True),
}


def validate_criteria(
    criteria_path: str, json_output: bool = False, output_stream=None
) -> int:
    """Validate an eval_criteria.json file. Returns exit code.

    output_stream: stream for the human-readable VALID/INVALID summary
    (defaults to stdout). Callers with a JSON-on-stdout contract (eg the
    --audit mode of validate_eval_criteria.py) pass sys.stderr so the
    summary cannot corrupt their stdout payload.
    """
    out = output_stream or sys.stdout
    path = Path(criteria_path)
    if not path.exists():
        msg = f"File not found: {criteria_path}"
        if json_output:
            json.dump({"valid": False, "error": msg, "errors": []}, sys.stdout, indent=2)
            print()
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    try:
        with open(path) as file_handle:
            data = json.load(file_handle)
    except json.JSONDecodeError as parse_error:
        msg = f"Invalid JSON in {criteria_path}: {parse_error}"
        if json_output:
            json.dump({"valid": False, "error": msg, "errors": []}, sys.stdout, indent=2)
            print()
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    if not isinstance(data, dict):
        msg = "Top-level value must be a JSON object"
        if json_output:
            json.dump({"valid": False, "error": msg, "errors": []}, sys.stdout, indent=2)
            print()
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    errors: list[ValidationError] = []
    fields_checked = validate_fields(data, CRITERIA_SCHEMA, "", errors)

    # Additional semantic checks beyond schema validation. The list guard
    # matters: validate_fields above records a clean type error for a null
    # or non-list test_cases, and this pass must then degrade to "no test
    # cases" instead of dying on enumerate(None) with a raw traceback
    # (which also leaves --json consumers with unparseable output).
    test_cases = null_safe_get(data, "test_cases", [], expected=list)
    seen_ids: set[str] = set()
    for idx, test_case in enumerate(test_cases):
        if not isinstance(test_case, dict):
            continue
        test_id = test_case.get("id", "")
        if test_id in seen_ids:
            errors.append(ValidationError(
                f"test_cases[{idx}].id",
                f"duplicate test case id: {test_id!r}",
            ))
        seen_ids.add(test_id)

    is_valid = len(errors) == 0

    if json_output:
        result = {
            "valid": is_valid,
            "fields_checked": fields_checked,
            "error_count": len(errors),
            "errors": [
                {"path": err.path, "message": err.message}
                for err in errors
            ],
            "test_case_count": len(test_cases),
        }
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        if is_valid:
            print(
                f"VALID: {len(test_cases)} test cases, "
                f"{fields_checked} fields checked",
                file=out,
            )
        else:
            print(f"INVALID: {len(errors)} error(s)", file=out)
            for err in errors:
                print(f"  {err.path}: {err.message}", file=out)

    return 0 if is_valid else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate eval_criteria.json against the canonical schema"
    )
    parser.add_argument("criteria_path", help="Path to eval_criteria.json")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output validation result as JSON",
    )
    args = parser.parse_args()
    sys.exit(validate_criteria(args.criteria_path, json_output=args.json))


if __name__ == "__main__":
    main()
