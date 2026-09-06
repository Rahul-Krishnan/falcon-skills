#!/usr/bin/env python3
"""Summarize eval runner results.json for Phase 2 analysis.

Usage:
    python3 analyze_results.py <path_to_results.json>           # human-readable
    python3 analyze_results.py <path_to_results.json> --triage   # JSON triage to stdout
"""

from __future__ import annotations

import argparse
import json
import sys

# Shared score resolution and thresholds keep scoring, triage, and gates aligned.
from hone_common import (
    ACTIONABLE_THRESHOLD,
    CRITERIA_BUG_THRESHOLD,
    DIMENSION_FLOOR,
    at_score_floor,
    extract_results,
    get,
    load_deterministic_scores,
    load_inconclusive_ids,
    resolve_score,
)


def _dict_results(results: list) -> list[dict]:
    """Drop non-dict entries with one warning each, matching score_execution."""
    kept = []
    for entry in results:
        if isinstance(entry, dict):
            kept.append(entry)
        else:
            print(
                f"WARNING: skipping non-object result entry: {type(entry).__name__}",
                file=sys.stderr,
            )
    return kept


def classify_failure(score: float, all_scores: list[float]) -> str:
    """Classify a test using the run's score distribution.

    Priority: all scores at the floor or multiple scores below the criteria-bug
    threshold -> criteria_bug; this score at the floor with another above that
    threshold -> variance; below ACTIONABLE_THRESHOLD -> real_issue; else pass.

    Use at_score_floor: deterministic composites bottom out at DIMENSION_FLOOR
    (0.05), so an exact-zero check misses them. ACTIONABLE_THRESHOLD matches
    the Phase 1 exit gate (0.8).

    score and all_scores use the 0-1 scale and may mix deterministic and judge
    scores when deterministic_scores.json covers only part of the run.
    """
    if all_scores and all(at_score_floor(s) for s in all_scores):
        return "criteria_bug"
    if all(s < CRITERIA_BUG_THRESHOLD for s in all_scores) and len(all_scores) > 1:
        return "criteria_bug"
    if at_score_floor(score) and any(s > CRITERIA_BUG_THRESHOLD for s in all_scores):
        return "variance"
    if score < ACTIONABLE_THRESHOLD:
        return "real_issue"
    return "pass"


def triage(path: str) -> dict:
    """Deterministic triage. Returns JSON-serializable dict."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc), "classifications": [], "summary": {}}

    # Accept the same result layouts as score_execution.
    results, _key = extract_results(data)
    results = _dict_results(results)
    if not results:
        return {
            "classifications": [],
            "summary": {"criteria_bug": 0, "variance": 0, "real_issue": 0, "pass": 0},
        }

    det_per_test = load_deterministic_scores(path)
    inconclusive_ids = load_inconclusive_ids(path)

    # Exclude unscored tests from the criteria-bug and variance population.
    all_scores = []
    for result in results:
        test_id = result.get("test_id", "unknown")
        if test_id in inconclusive_ids:
            continue
        all_scores.append(resolve_score(result, det_per_test))

    classifications = []
    counts: dict[str, int] = {
        "criteria_bug": 0,
        "variance": 0,
        "real_issue": 0,
        "pass": 0,
        "inconclusive": 0,
    }

    for result in results:
        test_id = result.get("test_id", "unknown")
        if test_id in inconclusive_ids:
            classifications.append(
                {
                    "test_id": test_id,
                    "classification": "inconclusive",
                    "score": None,
                    "score_source": "deterministic",
                }
            )
            counts["inconclusive"] += 1
            continue
        source = "deterministic" if test_id in det_per_test else "llm_judge"
        score = resolve_score(result, det_per_test)

        classification = classify_failure(score, all_scores)
        classifications.append(
            {
                "test_id": test_id,
                "classification": classification,
                "score": round(score, 4),
                "score_source": source,
            }
        )
        counts[classification] = counts.get(classification, 0) + 1

    return {"classifications": classifications, "summary": counts}


def analyze(path: str) -> None:
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"ERROR: Failed to read {path}: {e}\n"
            f"  -> check that the eval run wrote results.json to that directory",
            file=sys.stderr,
        )
        sys.exit(2)

    results, _key = extract_results(data)
    results = _dict_results(results)
    summary = data.get("summary", {})
    dims = data.get("dimension_aggregation", {})

    # Overall summary
    total = len(results)
    if total == 0:
        print("=== RESULTS SUMMARY (0 tests) ===")
        print(
            "No test results found. eval runner may have crashed before completing any tests."
        )
        return

    det_per_test = load_deterministic_scores(path)
    inconclusive_ids = load_inconclusive_ids(path)

    def score_of(result: dict) -> float:
        """Deterministic composite when available, else the LLM judge score."""
        return resolve_score(result, det_per_test)

    # Exclude unscored tests from pass rates and averages.
    conclusive = [
        r for r in results if r.get("test_id", "unknown") not in inconclusive_ids
    ]

    score_source = "deterministic" if det_per_test else "llm_judge"
    # Use the same passing threshold as classify_failure.
    passed = sum(1 for r in conclusive if score_of(r) >= ACTIONABLE_THRESHOLD)
    denom = len(conclusive)
    avg_score = sum(score_of(r) for r in conclusive) / denom if denom else 0.0

    print(f"=== RESULTS SUMMARY ({total} tests) ===")
    if inconclusive_ids:
        print(f"Inconclusive (excluded from scoring): {len(results) - denom}/{total}")
    if denom:
        print(f"Passed: {passed}/{denom} ({passed / denom * 100:.0f}%)")
        print(f"Composite: {avg_score:.3f} (source: {score_source})")
    else:
        print("No conclusive tests; nothing to score.")
    print()

    # Use the deterministic floor, matching classify_failure.
    all_zero = denom > 0 and all(at_score_floor(score_of(r)) for r in conclusive)
    some_zero = any(at_score_floor(score_of(r)) for r in conclusive) and not all_zero

    if all_zero:
        print(f"TRIAGE: ALL TESTS AT THE SCORING FLOOR ({DIMENSION_FLOOR:.2f})")
        print(
            "  -> This is almost certainly an eval criteria bug (required_present/required_absent)"
        )
        print("  -> Fix eval criteria, do NOT edit SKILL.md")
        print()
    elif some_zero:
        zero_ids = [
            r.get("test_id", "?") for r in conclusive if at_score_floor(score_of(r))
        ]
        print(
            f"TRIAGE: {len(zero_ids)} test(s) at the scoring floor "
            f"({DIMENSION_FLOOR:.2f}): {', '.join(zero_ids)}"
        )
        print(
            "  -> Likely agent variance (skill misidentification) or eval criteria issue"
        )
        print("  -> Check agent_response for these tests")
        print()

    # Per-test breakdown
    print("=== PER-TEST BREAKDOWN ===")
    for r in results:
        tid = r.get("test_id", "unknown")
        suite = r.get("suite") or r.get("test_profile") or "unknown"
        score = score_of(r)
        details = r.get("details", {})
        tool_errors = r.get("tool_call_error_count", 0)

        if tid in inconclusive_ids:
            status, score_label = "INCONCLUSIVE", "score=n/a"
        else:
            status = "PASS" if score >= ACTIONABLE_THRESHOLD else "FAIL"
            score_label = f"score={score:.3f}"
        print(f"\n--- {tid} [{suite}] {status} ({score_label}) ---")

        if isinstance(details, dict):
            # Programmatic checks
            prog = get(details, "programmatic_checks", [])
            failed_prog = [p for p in prog if not p.get("passed", True)]
            if failed_prog:
                print("  FAILED programmatic checks:")
                for p in failed_prog:
                    print(f'    {p.get("id", "?")}: "{p.get("value", "")}" not found')

            # Semantic scores
            raw = get(details, "raw_semantic_scores", {}, expected=dict)
            if raw:
                print("  Semantic scores:")
                for q, s in raw.items():
                    indicator = "LOW" if s <= 3.0 else "OK" if s <= 4.0 else "GOOD"
                    print(f"    [{indicator}] {s}/5 - {q[:80]}")

            # Overall feedback (truncated)
            feedback = details.get("overall_feedback", "")
            if feedback:
                truncated = feedback[:300]
                suffix = "..." if len(feedback) > 300 else ""
                print(f"  Judge feedback: {truncated}{suffix}")

        if tool_errors:
            print(f"  Tool call errors: {tool_errors}")

    # Dimension summary
    print("\n=== DIMENSION SUMMARY ===")
    print(f"{'Dimension':<20} {'Score':>6} {'1-5':>5} {'Status':>8}")
    print("-" * 45)

    for r in results:
        details = r.get("details", {})
        if isinstance(details, dict):
            cat = get(details, "category", r.get("suite", "unknown"), expected=str)
            composite = get(details, "composite_1_5", 0)
            # Show unscored tests as inconclusive, matching the per-test breakdown.
            if r.get("test_id", "unknown") in inconclusive_ids:
                print(f"{cat:<20} {'n/a':>6} {composite:>5.1f} {'INCONCL':>8}")
                continue
            score = score_of(r)
            status = "PASS" if score >= ACTIONABLE_THRESHOLD else "FAIL"
            print(f"{cat:<20} {score:>6.3f} {composite:>5.1f} {status:>8}")

    # Actionable recommendations
    print("\n=== RECOMMENDED ACTIONS ===")
    low_dims = []
    for r in results:
        details = r.get("details", {})
        if isinstance(details, dict):
            # A judge may return raw_semantic_scores: null; typed get makes .items() safe.
            raw = get(details, "raw_semantic_scores", {}, expected=dict)
            cat = get(details, "category", r.get("suite", "unknown"), expected=str)
            for q, s in raw.items():
                if s <= 3.0:
                    low_dims.append((cat, q, s))

    if low_dims:
        print("Low-scoring semantic checks (<=3.0/5, target for SKILL.md improvement):")
        for cat, q, s in low_dims:
            print(f"  [{cat}] {s}/5: {q}")
    else:
        print(
            "No semantic scores <=3.0. Skill is performing well across all dimensions."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze eval runner results.json")
    parser.add_argument("results_json", help="Path to results.json")
    parser.add_argument(
        "--triage",
        action="store_true",
        help="Output deterministic failure triage as JSON to stdout",
    )
    args = parser.parse_args()

    if args.triage:
        result = triage(args.results_json)
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        analyze(args.results_json)
