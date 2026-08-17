#!/usr/bin/env python3
"""Analyze eval runner results.json and produce a structured summary.

Usage:
    python3 analyze_results.py <path_to_results.json>           # human-readable
    python3 analyze_results.py <path_to_results.json> --triage   # JSON triage to stdout

Outputs a concise analysis to stdout that can be consumed by the LLM
for faster Phase 2 analysis (no need to parse raw JSON).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Below this, a test is an actionable quality gap. Mirrors SKILL.md's Phase 1
# exit gate ("any test scored below 0.8").
ACTIONABLE_THRESHOLD = 0.8

# Below this across the whole run, the criteria are suspect rather than the artifact.
CRITERIA_BUG_THRESHOLD = 0.5


def load_deterministic_scores(results_path: str) -> dict[str, float]:
    """Map test_id -> deterministic composite from deterministic_scores.json.

    results.json carries a per-test `score` only when an LLM judge ran. On a
    deterministic-only run those fields are absent, so every consumer that reads
    `score` directly sees 0.0 for every test. Both the triage path and the
    human-readable summary must use this map first.

    Returns an empty dict when the file is missing or unreadable.
    """
    det_scores_path = Path(results_path).parent / "deterministic_scores.json"
    if not det_scores_path.exists():
        return {}
    try:
        with open(det_scores_path) as f:
            det_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    per_test = det_data.get("per_test") or []
    # Inconclusive tests carry composite: null; exclude them so numeric
    # comparisons downstream never see None.
    return {
        test["test_id"]: test["composite"]
        for test in per_test
        if "test_id" in test and isinstance(test.get("composite"), (int, float))
    }


def load_inconclusive_ids(results_path: str) -> set[str]:
    """Set of test_ids marked inconclusive in deterministic_scores.json.

    score_execution.py emits `status: "inconclusive"` with `composite: null`
    for tests with no execution evidence. load_deterministic_scores drops them
    from the score map, which made them indistinguishable from "never scored
    deterministically": on a deterministic-only run they then fell back to
    `score = 0.0, score_source = "llm_judge"`, dragging avg/FAIL counts and
    (on an all-inconclusive run) misrouting triage into criteria_bug.
    """
    det_scores_path = Path(results_path).parent / "deterministic_scores.json"
    if not det_scores_path.exists():
        return set()
    try:
        with open(det_scores_path) as f:
            det_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()

    per_test = det_data.get("per_test") or []
    return {
        test["test_id"]
        for test in per_test
        if "test_id" in test
        and (
            test.get("status") == "inconclusive"
            or not isinstance(test.get("composite"), (int, float))
        )
    }


def _llm_score(result: dict) -> float:
    """LLM judge score with an explicit null treated the same as a missing key.

    The eval_results schema allows score: null for inconclusive/score_error
    tests, and .get's default only covers an absent key, so a raw
    result.get("score", 0.0) returns None and crashes the >= comparisons."""
    score = result.get("score")
    return score if score is not None else 0.0


def classify_failure(score: float, all_scores: list[float]) -> str:
    """Deterministic failure classification.

    Classification priority (evaluated in order, first match wins):
    1. All scores zero -> criteria are broken, not the artifact
    2. This score zero but others passed -> agent variance/misidentification
    3. Score below threshold -> genuine quality gap
    4. Score at or above threshold -> passing

    Args:
        score: the score for this specific test (0.0-1.0, may be from
            deterministic or LLM source depending on caller)
        all_scores: scores for ALL tests in the same run (used for
            criteria_bug detection). May mix score sources when
            deterministic_scores.json is only partially available.

    The actionable threshold matches SKILL.md's Phase 1 exit gate ("any test
    scored below 0.8"). Keeping triage on a lower bar than the gate produced a
    contradiction: a test could be reported as `pass` here while the gate
    counted it as an actionable failure worth a full Phase 2 round.

    Returns: "criteria_bug" | "variance" | "real_issue" | "pass"
    """
    if all(s == 0.0 for s in all_scores):
        return "criteria_bug"
    if all(s < CRITERIA_BUG_THRESHOLD for s in all_scores) and len(all_scores) > 1:
        return "criteria_bug"
    if score == 0.0 and any(s > CRITERIA_BUG_THRESHOLD for s in all_scores):
        return "variance"
    if 0.0 < score < ACTIONABLE_THRESHOLD:
        return "real_issue"
    if score == 0.0:
        return "real_issue"
    return "pass"


def triage(path: str) -> dict:
    """Deterministic triage. Returns JSON-serializable dict."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc), "classifications": [], "summary": {}}

    results = data.get("results", [])
    if not results:
        return {
            "classifications": [],
            "summary": {"criteria_bug": 0, "variance": 0, "real_issue": 0, "pass": 0},
        }

    det_per_test = load_deterministic_scores(path)
    inconclusive_ids = load_inconclusive_ids(path)

    # Inconclusive tests were never scored; they must not contribute a
    # phantom 0.0 to the criteria_bug / variance population checks.
    all_scores = []
    for result in results:
        test_id = result.get("test_id", "unknown")
        if test_id in inconclusive_ids:
            continue
        if test_id in det_per_test:
            all_scores.append(det_per_test[test_id])
        else:
            all_scores.append(_llm_score(result))

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
        if test_id in det_per_test:
            score = det_per_test[test_id]
            source = "deterministic"
        else:
            score = _llm_score(result)
            source = "llm_judge"

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

    results = data.get("results", [])
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
        test_id = result.get("test_id", "unknown")
        if test_id in det_per_test:
            return det_per_test[test_id]
        return _llm_score(result)

    # Inconclusive tests were never scored; keep them out of pass/avg math
    # so "nothing was observed" does not read as a 0.0 failure.
    conclusive = [
        r for r in results if r.get("test_id", "unknown") not in inconclusive_ids
    ]

    score_source = "deterministic" if det_per_test else "llm_judge"
    # PASS/FAIL here must agree with classify_failure's triage band: a test
    # below ACTIONABLE_THRESHOLD is a real_issue, so it must not be reported
    # as passing in the operator summary.
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

    # Triage
    all_zero = denom > 0 and all(score_of(r) == 0.0 for r in conclusive)
    some_zero = any(score_of(r) == 0.0 for r in conclusive) and not all_zero

    if all_zero:
        print("TRIAGE: ALL TESTS SCORED 0.00")
        print(
            "  -> This is almost certainly an eval criteria bug (required_present/required_absent)"
        )
        print("  -> Fix eval criteria, do NOT edit SKILL.md")
        print()
    elif some_zero:
        zero_ids = [r.get("test_id", "?") for r in conclusive if score_of(r) == 0.0]
        print(f"TRIAGE: {len(zero_ids)} test(s) scored 0.00: {', '.join(zero_ids)}")
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
            prog = details.get("programmatic_checks", [])
            failed_prog = [p for p in prog if not p.get("passed", True)]
            if failed_prog:
                print("  FAILED programmatic checks:")
                for p in failed_prog:
                    print(f'    {p.get("id", "?")}: "{p.get("value", "")}" not found')

            # Semantic scores
            raw = details.get("raw_semantic_scores", {})
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
            cat = details.get("category", r.get("suite", "unknown"))
            score = score_of(r)
            composite = details.get("composite_1_5", 0)
            status = "PASS" if score >= ACTIONABLE_THRESHOLD else "FAIL"
            print(f"{cat:<20} {score:>6.3f} {composite:>5.1f} {status:>8}")

    # Actionable recommendations
    print("\n=== RECOMMENDED ACTIONS ===")
    low_dims = []
    for r in results:
        details = r.get("details", {})
        if isinstance(details, dict):
            raw = details.get("raw_semantic_scores", {})
            cat = details.get("category", r.get("suite", "unknown"))
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
