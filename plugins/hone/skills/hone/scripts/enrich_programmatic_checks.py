#!/usr/bin/env python3
"""Enrich eval criteria with deterministic required_present checks.

Extracts underscore identifiers from semantic check descriptions, verifies
them against the artifact content, and adds high-confidence anchors as
required_present entries. Conservative by design: only adds identifiers
that exist verbatim in the artifact and are specific enough to avoid
false positives on valid responses.

Usage:
    enrich_programmatic_checks.py --artifact-path PATH --criteria-path PATH [--json] [--max-per-test N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# Underscore identifier pattern: two or more underscore-separated segments
# eg max_rounds, task_completion, progress_gates
IDENTIFIER_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

# Identifiers that are too generic to be useful anchors
EXCLUDED_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "test_case",
        "test_cases",
        "test_type",
        "file_path",
        "file_paths",
        "line_count",
        "test_plan",
        "test_id",
        "required_present",
        "required_absent",
        "checks",
        "importance",
        "allowed_tools",
        "target_skills",
        "runner_context",
    }
)


def extract_identifiers(text: str) -> list[str]:
    """Extract underscore identifiers from text.

    Only matches lowercase identifiers with at least one underscore
    (eg max_rounds, not maxrounds or MaxRounds).
    """
    matches = IDENTIFIER_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for match in matches:
        if match not in seen:
            seen.add(match)
            result.append(match)
    return result


def get_check_texts(test_case: dict) -> list[str]:
    """Extract check description texts from a test case.

    Only reads the 'description' field, NOT rubric values.
    Rubric text contains implementation details that should not become anchors.
    """
    texts: list[str] = []

    for check in test_case.get("checks", []):
        if isinstance(check, dict):
            check_text = check.get("description", "")
            if check_text:
                texts.append(check_text)
        elif isinstance(check, str):
            texts.append(check)

    return texts


def get_existing_present(test_case: dict) -> list[str]:
    """Get existing required_present values from a test case."""
    return list(test_case.get("required_present", []))


def get_existing_absent(test_case: dict) -> list[str]:
    """Get existing required_absent values from a test case."""
    return list(test_case.get("required_absent", []))


def filter_candidates(
    candidates: list[str],
    artifact_content: str,
    existing_absent: list[str],
    existing_present: list[str],
) -> list[dict]:
    """Filter candidates to high-confidence anchors only.

    Returns list of dicts with 'identifier' and 'occurrences' (in artifact).
    """
    artifact_lower = artifact_content.lower()
    absent_set = {val.lower() for val in existing_absent}
    present_set = {val.lower() for val in existing_present}

    result: list[dict] = []
    for candidate in candidates:
        if candidate in EXCLUDED_IDENTIFIERS:
            continue
        if len(candidate) > 30:
            continue
        if candidate in absent_set:
            continue
        if candidate in present_set:
            continue
        occurrences = artifact_lower.count(candidate)
        if occurrences == 0:
            continue
        result.append({"identifier": candidate, "occurrences": occurrences})

    return result


def rank_and_select(candidates: list[dict], max_count: int) -> list[str]:
    """Rank candidates by artifact-specificity and select top N.

    Fewer occurrences = more specific = higher rank.
    """
    sorted_candidates = sorted(candidates, key=lambda item: item["occurrences"])
    return [item["identifier"] for item in sorted_candidates[:max_count]]


def enrich_test_case(
    test_case: dict,
    artifact_content: str,
    max_per_test: int,
) -> dict:
    """Enrich a single test case with required_present checks.

    Returns a dict with enrichment results. Does not modify the test_case
    in place -- the caller handles YAML mutation.
    """
    test_id = test_case.get("id", "unknown")
    # Normalize hyphen/underscore so legacy "error-handling" spellings still
    # hit the guard; the schema enum's canonical spelling is "error_handling".
    category = test_case.get("category", "").lower().replace("-", "_")

    if category == "error_handling":
        return {
            "test_id": test_id,
            "added": [],
            "candidates_found": 0,
            "candidates_filtered": 0,
            "skipped": True,
            "skip_reason": "error_handling category",
        }

    check_texts = get_check_texts(test_case)
    if not check_texts:
        return {
            "test_id": test_id,
            "added": [],
            "candidates_found": 0,
            "candidates_filtered": 0,
            "skipped": True,
            "skip_reason": "no semantic checks",
        }

    combined_text = " ".join(check_texts)
    raw_candidates = extract_identifiers(combined_text)
    existing_absent = get_existing_absent(test_case)
    existing_present = get_existing_present(test_case)

    filtered = filter_candidates(
        raw_candidates, artifact_content, existing_absent, existing_present
    )
    selected = rank_and_select(filtered, max_per_test)

    return {
        "test_id": test_id,
        "added": selected,
        "candidates_found": len(raw_candidates),
        "candidates_filtered": len(filtered),
        "skipped": False,
    }


def strip_stale_present(test_case: dict, artifact_content: str) -> list[str]:
    """Remove enrichment-shaped required_present entries no longer in artifact.

    Phase 3 relies on re-running this script after edits: a Phase 2 rename
    would otherwise leave the old identifier in required_present, score
    MISSING deterministically on every re-eval, and trigger auto-revert of a
    correct improvement. Only identifier-shaped entries (the only shape
    enrichment ever adds) are candidates; hand-written phrases are left alone
    because they assert on the agent response, not the artifact text.

    Mutates test_case in place. Returns the removed entries.
    """
    existing = test_case.get("required_present", [])
    if not isinstance(existing, list):
        return []

    artifact_lower = artifact_content.lower()
    kept: list[str] = []
    removed: list[str] = []
    for entry in existing:
        if (
            isinstance(entry, str)
            and IDENTIFIER_RE.fullmatch(entry)
            and entry not in artifact_lower
        ):
            removed.append(entry)
        else:
            kept.append(entry)

    if removed:
        test_case["required_present"] = kept
    return removed


def apply_enrichment(test_case: dict, identifiers: list[str]) -> None:
    """Add required_present entries to a test case dict (mutates in place)."""
    if not identifiers:
        return

    existing = test_case.get("required_present", [])
    if not isinstance(existing, list):
        existing = []
    test_case["required_present"] = existing + identifiers


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich eval criteria with deterministic required_present checks"
    )
    parser.add_argument(
        "--artifact-path",
        required=True,
        help="Path to the artifact file (SKILL.md, command .md, etc.)",
    )
    parser.add_argument(
        "--criteria-path",
        required=True,
        help="Path to eval_criteria.json to enrich",
    )
    parser.add_argument(
        "--max-per-test",
        type=int,
        default=5,
        help="Maximum required_present entries per test case (default: 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON report to stdout",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be added without writing the criteria file",
    )
    args = parser.parse_args()

    artifact_path = Path(args.artifact_path)
    criteria_path = Path(args.criteria_path)

    if not artifact_path.exists():
        print(f"ERROR: Artifact not found: {artifact_path}", file=sys.stderr)
        sys.exit(1)

    if not criteria_path.exists():
        print(f"ERROR: Criteria not found: {criteria_path}", file=sys.stderr)
        sys.exit(1)

    try:
        artifact_content = artifact_path.read_text()
    except OSError as exc:
        print(f"ERROR: Cannot read artifact: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(criteria_path) as criteria_file:
            criteria_data = json.load(criteria_file)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: Cannot read criteria: {exc}", file=sys.stderr)
        sys.exit(1)

    if not criteria_data or "test_cases" not in criteria_data:
        print("ERROR: No test_cases in criteria file", file=sys.stderr)
        sys.exit(1)

    test_cases = criteria_data["test_cases"]
    report_per_test: list[dict] = []
    modified_ids: list[str] = []
    skipped_ids: list[str] = []
    total_added = 0
    total_removed = 0

    for test_case in test_cases:
        # Strip stale identifiers first so a renamed identifier can be
        # re-added under its new name in the same pass. Same population as
        # additions: error-handling tests are never enriched, so their
        # entries are never enrichment-owned.
        removed: list[str] = []
        if test_case.get("category", "").lower().replace("-", "_") != "error_handling":
            removed = strip_stale_present(test_case, artifact_content)

        result = enrich_test_case(test_case, artifact_content, args.max_per_test)
        result["removed"] = removed
        report_per_test.append(result)

        if removed:
            total_removed += len(removed)
            if result["test_id"] not in modified_ids:
                modified_ids.append(result["test_id"])

        if result["skipped"]:
            if result["test_id"] not in modified_ids:
                skipped_ids.append(result["test_id"])
            continue

        if result["added"]:
            apply_enrichment(test_case, result["added"])
            if result["test_id"] not in modified_ids:
                modified_ids.append(result["test_id"])
            total_added += len(result["added"])

    if total_added == 0 and total_removed == 0:
        report = {
            "enriched_count": 0,
            "test_cases_modified": [],
            "test_cases_skipped": skipped_ids,
            "per_test": report_per_test,
            "total_checks_added": 0,
            "total_checks_removed": 0,
        }
        if args.json:
            json.dump(report, sys.stdout, indent=2)
            print()
        else:
            print("Nothing to enrich: no high-confidence anchors found")
        sys.exit(2)

    if args.dry_run:
        report = {
            "enriched_count": len(modified_ids),
            "test_cases_modified": modified_ids,
            "test_cases_skipped": skipped_ids,
            "per_test": report_per_test,
            "total_checks_added": total_added,
            "total_checks_removed": total_removed,
            "dry_run": True,
            "backup_path": None,
        }
        if args.json:
            json.dump(report, sys.stdout, indent=2)
            print()
        else:
            print(
                f"DRY RUN: would add {total_added} and remove {total_removed} "
                "check(s); no file written"
            )
        sys.exit(0)

    # Always back up before overwriting. The caller used to be told to run `cp`
    # by hand, which made the safety property depend on the model remembering a
    # separate step. Fail closed if the backup cannot be written.
    backup_path = f"{criteria_path}.pre-enrich"
    try:
        with open(criteria_path) as original:
            original_text = original.read()
        with open(backup_path, "w") as backup_file:
            backup_file.write(original_text)
    except OSError as exc:
        print(f"ERROR: Cannot write backup {backup_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(criteria_path, "w") as criteria_file:
            json.dump(criteria_data, criteria_file, indent=2)
            criteria_file.write("\n")
    except OSError as exc:
        print(f"ERROR: Cannot write criteria: {exc}", file=sys.stderr)
        sys.exit(1)

    report = {
        "enriched_count": len(modified_ids),
        "test_cases_modified": modified_ids,
        "test_cases_skipped": skipped_ids,
        "per_test": report_per_test,
        "total_checks_added": total_added,
        "total_checks_removed": total_removed,
        "dry_run": False,
        "backup_path": backup_path,
    }

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(
            f"Enriched {len(modified_ids)} test cases: "
            f"+{total_added} checks, -{total_removed} stale"
        )
        for entry in report_per_test:
            if entry["added"] or entry.get("removed"):
                print(
                    f"  {entry['test_id']}: +{entry['added']} "
                    f"-{entry.get('removed', [])}"
                )

    sys.exit(0)


if __name__ == "__main__":
    main()
