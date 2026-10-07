#!/usr/bin/env python3
"""One-time batch migration: eval_criteria.yaml -> eval_criteria.json.

Converts YAML eval criteria files to the canonical JSON schema format:
  - Flattens quality_criteria/dimensions wrappers
  - Renames semantic_checks -> checks, check/question -> description
  - Maps weight -> importance (CRITICAL/HIGH/MEDIUM/LOW)
  - Fills sparse rubrics with placeholder text
  - Infers test_profile from runner_context/allowed_tools
  - Validates output against the JSON schema

Usage:
    python3 yaml_to_json_criteria.py <path_to_eval_criteria.yaml>
    python3 yaml_to_json_criteria.py --batch <directory>  # convert all under dir
    python3 yaml_to_json_criteria.py --help

Exit codes:
    0: success
    1: conversion errors
    2: usage/file error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

# Import schema validator (same directory)
from validate_criteria_schema import validate_criteria as schema_validate

# ---------------------------------------------------------------------------
# Weight -> Importance mapping
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Category normalization (YAML files use inconsistent names)
# ---------------------------------------------------------------------------

CATEGORY_MAP = {
    "error-handling": "error_handling",
    "error handling": "error_handling",
    "quality": "execution",
    "edge-case": "edge_case",
    "task-completion": "task_completion",
    "tool-usage": "tool_usage",
    "business-impact": "business_impact",
}

VALID_CATEGORIES = {
    "invocation", "execution", "edge_case", "task_completion",
    "error_handling", "tool_usage", "business_impact",
}


def normalize_category(category: str) -> str:
    """Normalize category to valid enum value."""
    normalized = CATEGORY_MAP.get(category, category)
    if normalized not in VALID_CATEGORIES:
        return "execution"  # safe default
    return normalized


def weight_to_importance(weight: float) -> str:
    """Map numeric weight to importance enum."""
    if weight >= 2.5:
        return "CRITICAL"
    if weight >= 1.5:
        return "HIGH"
    if weight >= 0.75:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Rubric filling
# ---------------------------------------------------------------------------

RUBRIC_LEVELS = ["1", "2", "3", "4", "5"]

def fill_rubric(rubric: dict) -> tuple[dict, bool]:
    """Fill missing rubric levels with placeholders.

    Returns (filled_rubric, had_placeholders).
    """
    filled = {}
    had_placeholders = False
    for level in RUBRIC_LEVELS:
        if str(level) in rubric and rubric[str(level)]:
            filled[str(level)] = str(rubric[str(level)])
        else:
            filled[str(level)] = f"[level {level}: interpolate between adjacent levels]"
            had_placeholders = True
    return filled, had_placeholders


# ---------------------------------------------------------------------------
# Test profile inference
# ---------------------------------------------------------------------------

READ_ONLY_TOOLS = {"Read", "Grep", "Glob"}
KE_MARKERS = ("knowledge extraction", "do not invoke")
EH_MARKERS = ("error handling", "validation error", "argument validation")
SEG_MARKERS = ("safety sandbox", "side-effect simulation mode")


def infer_test_profile(tc: dict) -> str:
    """Infer test_profile from runner_context, allowed_tools, and category."""
    runner_ctx = tc.get("runner_context", "").lower()
    category = tc.get("category", "").lower()
    tools = set(tc.get("allowed_tools", []))

    # Error handling
    if category in ("error_handling", "error-handling"):
        return "error_handling"
    if any(marker in runner_ctx for marker in EH_MARKERS):
        return "error_handling"

    # Side effect guarded
    if any(marker in runner_ctx for marker in SEG_MARKERS):
        return "side_effect_guarded"

    # Knowledge extraction
    if any(marker in runner_ctx for marker in KE_MARKERS):
        return "knowledge_extraction"
    if tools and tools <= READ_ONLY_TOOLS:
        return "knowledge_extraction"

    return "execution"


# ---------------------------------------------------------------------------
# Extract checks from YAML structures
# ---------------------------------------------------------------------------

def extract_checks(tc: dict) -> list[dict]:
    """Extract semantic checks from dimensions or quality_criteria layouts."""
    raw_checks: list[dict] = []

    # Try dimensions layout first
    dims = tc.get("dimensions", [])
    if isinstance(dims, list):
        for dim in dims:
            if isinstance(dim, dict):
                for check in dim.get("semantic_checks", []):
                    if isinstance(check, dict):
                        raw_checks.append(check)
                    elif isinstance(check, str):
                        raw_checks.append({"check": check})

    # Fall back to quality_criteria layout
    if not raw_checks:
        qc = tc.get("quality_criteria", {})
        if isinstance(qc, dict):
            for check in qc.get("semantic_checks", []):
                if isinstance(check, dict):
                    raw_checks.append(check)
                elif isinstance(check, str):
                    raw_checks.append({"check": check})

    # Fall back to top-level semantic_checks
    if not raw_checks:
        for check in tc.get("semantic_checks", []):
            if isinstance(check, dict):
                raw_checks.append(check)
            elif isinstance(check, str):
                raw_checks.append({"check": check})

    return raw_checks


def extract_field(tc: dict, field: str) -> list:
    """Extract a field from top-level or quality_criteria."""
    val = tc.get(field, [])
    if val:
        return list(val) if isinstance(val, list) else [val]
    qc = tc.get("quality_criteria", {})
    if isinstance(qc, dict):
        val = qc.get(field, [])
        return list(val) if isinstance(val, list) else [val] if val else []
    return []


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def convert_test_case(tc: dict) -> tuple[dict, list[str]]:
    """Convert a single YAML test case to JSON format.

    Returns (converted_dict, list_of_warnings).
    """
    warnings: list[str] = []
    tc_id = tc.get("id", "unknown")

    # Extract and convert checks
    raw_checks = extract_checks(tc)
    checks: list[dict] = []
    for check in raw_checks:
        description = check.get("check", "") or check.get("question", "")
        if not description:
            warnings.append(f"{tc_id}: check with no description, skipping")
            continue

        weight = float(check.get("weight", 1.0))
        importance = weight_to_importance(weight)

        raw_rubric = check.get("rubric", {})
        if isinstance(raw_rubric, dict):
            rubric, had_placeholders = fill_rubric(raw_rubric)
        else:
            rubric, had_placeholders = fill_rubric({})

        if had_placeholders:
            warnings.append(f"{tc_id}: check has incomplete rubric, placeholders added")

        checks.append({
            "description": str(description),
            "importance": importance,
            "rubric": rubric,
        })

    if not checks:
        warnings.append(f"{tc_id}: no checks found")

    # Build converted test case
    runner_context = tc.get("runner_context", "")
    if not runner_context:
        runner_context = "[NEEDS RUNNER CONTEXT]"
        warnings.append(f"{tc_id}: missing runner_context, placeholder added")

    result = {
        "id": tc.get("id", "unknown"),
        "name": tc.get("name", tc.get("id", "unknown")),
        "category": normalize_category(tc.get("category", "execution")),
        "test_profile": infer_test_profile(tc),
        "prompt": tc.get("prompt", ""),
        "runner_context": runner_context,
        "allowed_tools": tc.get("allowed_tools", []),
        "target_skills": tc.get("target_skills", []),
        "checks": checks,
    }

    # Optional fields
    required_present = extract_field(tc, "required_present")
    if required_present:
        result["required_present"] = required_present

    required_absent = extract_field(tc, "required_absent")
    if required_absent:
        result["required_absent"] = required_absent

    return result, warnings


def convert_file(yaml_path: Path) -> tuple[dict | None, list[str]]:
    """Convert a YAML eval criteria file to JSON format.

    Returns (json_data, warnings). json_data is None on parse error.
    """
    warnings: list[str] = []

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return None, [f"YAML parse error: {e}"]
    except OSError as e:
        return None, [f"Cannot read file: {e}"]

    if not data or not isinstance(data, dict):
        return None, ["Empty or non-dict YAML file"]

    test_cases = data.get("test_cases", [])
    if not test_cases:
        return None, ["No test_cases found"]

    converted_cases: list[dict] = []
    for idx, tc in enumerate(test_cases):
        if not isinstance(tc, dict):
            warnings.append(f"test_cases[{idx}]: not a dict (got {type(tc).__name__}), skipping")
            continue
        converted, tc_warnings = convert_test_case(tc)
        converted_cases.append(converted)
        warnings.extend(tc_warnings)

    result = {
        "project": data.get("project", ""),
        "skill_name": data.get("skill_name", ""),
        "test_cases": converted_cases,
    }

    return result, warnings


def write_json(data: dict, output_path: Path) -> None:
    """Write JSON data to file."""
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert eval_criteria.yaml to eval_criteria.json"
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="Path to eval_criteria.yaml (or directory with --batch)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Convert all eval_criteria.yaml files under the given directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print conversion results without writing files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output summary as JSON",
    )
    args = parser.parse_args()

    if not args.path:
        parser.print_help()
        return 2

    input_path = Path(args.path).expanduser()

    if args.batch:
        if not input_path.is_dir():
            print(f"Error: {input_path} is not a directory", file=sys.stderr)
            return 2
        yaml_files = sorted(input_path.rglob("eval_criteria.yaml"))
    else:
        if not input_path.exists():
            print(f"Error: {input_path} not found", file=sys.stderr)
            return 2
        yaml_files = [input_path]

    total = len(yaml_files)
    converted = 0
    failed = 0
    all_warnings: list[str] = []
    validation_failures: list[str] = []

    for yaml_file in yaml_files:
        json_file = yaml_file.with_name("eval_criteria.json")

        data, warnings = convert_file(yaml_file)
        all_warnings.extend(warnings)

        if data is None:
            failed += 1
            print(f"FAIL: {yaml_file}: {warnings}", file=sys.stderr)
            continue

        if not args.dry_run:
            write_json(data, json_file)

        # Validate against schema (lenient: write file regardless, track failures)
        if not args.dry_run:
            import io
            from validate_criteria_schema import validate_criteria as schema_fn
            buf = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            exit_code = schema_fn(str(json_file), json_output=False)
            sys.stdout = old_stdout
            if exit_code != 0:
                validation_failures.append(str(json_file))
                all_warnings.append(f"{json_file}: schema validation issues (file written anyway)")

        converted += 1
        if not args.json:
            tc_count = len(data.get("test_cases", []))
            action = "Would convert" if args.dry_run else "Converted"
            print(f"  {action}: {yaml_file} ({tc_count} test cases)")

    summary = {
        "total": total,
        "converted": converted,
        "failed": failed,
        "warnings": len(all_warnings),
        "validation_failures": validation_failures,
    }

    if args.json:
        json.dump(summary, sys.stdout, indent=2)
        print()
    else:
        print(f"\nSummary: {converted}/{total} converted, {failed} failed, {len(all_warnings)} warnings")
        if all_warnings:
            print("\nWarnings:")
            for w in all_warnings:
                print(f"  {w}")
        if validation_failures:
            print("\nValidation failures:")
            for vf in validation_failures:
                print(f"  {vf}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
