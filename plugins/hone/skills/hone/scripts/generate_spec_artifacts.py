#!/usr/bin/env python3
"""Generate Agent Skills open standard eval artifacts from eval runner results.

Converts eval runner results.json + deterministic_scores.json into spec-format:
  - evals.json: test case definitions with assertions
  - grading.json: assertion results with pass/fail and evidence
  - timing.json: duration and token usage
  - benchmark.json: with_skill vs without_skill comparison

Usage:
    python3 generate_spec_artifacts.py <output_dir> \
        --criteria <eval_criteria.json> \
        --timing-ms <duration_ms> \
        --timing-tokens <token_count> \
        [--baseline-dir <baseline_output_dir>] \
        [--json]

Exit codes:
    0: Success
    1: Error (missing files, parse failure)
    2: Usage error
"""

import argparse
import json
import os
import sys

# Shared null-tolerant getter and the authoritative pass threshold (Phase 1
# exit gate; see hone_common.py).
from hone_common import ACTIONABLE_THRESHOLD, get, resolve_score


def load_json(path):
    """Load a JSON file, return None on error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: Could not load {path}: {e}", file=sys.stderr)
        return None


def load_criteria_json(path):
    """Return criteria metadata (id, name, category), or None on read/parse error.

    An empty list means valid criteria with no cases; None must stop generation
    so grading never references missing eval definitions.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"Error: Could not load criteria {path}: {e}", file=sys.stderr)
        return None

    if not isinstance(data, dict):
        print(
            f"Error: criteria {path} is not a JSON object", file=sys.stderr
        )
        return None

    test_cases = []
    for tc in data.get("test_cases", []):
        test_cases.append({
            "id": tc.get("id", ""),
            "name": tc.get("name", tc.get("id", "")),
            "category": tc.get("category", "general"),
        })
    return test_cases


def generate_evals(test_cases, results):
    """Generate evals.json: test case definitions with assertions."""
    evals = []
    results_by_id = {r.get("test_id", ""): r for r in results.get("results", [])}

    for tc in test_cases:
        tc_id = tc.get("id", "")
        result = results_by_id.get(tc_id, {})

        assertions = []
        # Read the shared details.raw_semantic_scores mapping, tolerating nulls.
        details = get(result, "details", {}, expected=dict)
        raw_scores = get(details, "raw_semantic_scores", {}, expected=dict)
        for i, check in enumerate(raw_scores):
            assertions.append(
                {
                    "id": f"{tc_id}-A{i + 1}",
                    "type": "semantic",
                    "description": check,
                    "weight": 1.0,
                    "threshold": 3.0,
                }
            )

        evals.append(
            {
                "id": tc_id,
                "name": tc.get("name", tc_id),
                "category": tc.get("category", "general"),
                "assertions": assertions,
            }
        )

    return {"version": "1.0", "evals": evals}


def generate_grading(results, det_scores):
    """Generate grading.json: assertion results with pass/fail and evidence."""
    assertion_results = []
    results_list = results.get("results", [])
    det_by_id = {}
    if det_scores:
        det_by_id = {t.get("test_id", ""): t for t in det_scores.get("per_test", [])}

    for result in results_list:
        tc_id = result.get("test_id", "")
        score = result.get("score", 0)
        det = det_by_id.get(tc_id, {})
        det_composite = det.get("composite", score)
        # Inconclusive composites are null and cannot be compared numerically.
        if not isinstance(det_composite, (int, float)):
            det_composite = None

        # Use the same typed mapping read as generate_evals.
        details = get(result, "details", {}, expected=dict)
        raw_scores = get(details, "raw_semantic_scores", {}, expected=dict)
        for i, (check, sc_score) in enumerate(raw_scores.items()):
            # Treat null or non-numeric per-check scores as unusable, not passing.
            if not isinstance(sc_score, (int, float)) or isinstance(sc_score, bool):
                sc_score = None
            assertion_results.append(
                {
                    "eval_id": tc_id,
                    "assertion_id": f"{tc_id}-A{i + 1}",
                    "passed": sc_score is not None and sc_score >= 3.0,
                    "score": sc_score,
                    "max_score": 5.0,
                    "evidence": check,
                }
            )

        # Inconclusive tests carry no numeric evidence either way; skip the
        # composite assertion rather than fabricating a pass/fail for it.
        if det_composite is not None:
            assertion_results.append(
                {
                    "eval_id": tc_id,
                    "assertion_id": f"{tc_id}-composite",
                    "passed": det_composite >= ACTIONABLE_THRESHOLD,
                    "score": det_composite,
                    "max_score": 1.0,
                    "evidence": f"LLM judge: {score}, deterministic: {det_composite}",
                }
            )

    total = len(assertion_results)
    passed = sum(1 for a in assertion_results if a["passed"])

    return {
        "version": "1.0",
        "assertion_results": assertion_results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total > 0 else 0,
        },
    }


def generate_timing(duration_ms, tokens_estimate):
    """Generate timing.json: duration and token usage."""
    return {
        "version": "1.0",
        "duration_ms": duration_ms,
        "tokens_estimate": tokens_estimate,
        "tokens_per_second": round(tokens_estimate / (duration_ms / 1000), 1)
        if duration_ms > 0
        else 0,
    }


def generate_benchmark(results, det_scores, baseline_results, baseline_det):
    """Generate a with_skill vs without_skill benchmark.

    Compute deltas only between like metrics: deterministic composites or judge
    averages. Missing baseline scores and mixed scorers cannot establish benefit.
    """

    def compute_summary(res, det):
        if not res:
            return None
        results_list = res.get("results", [])
        # Use the type-tolerant score resolver; exclude missing scores from the average.
        scores = [
            s
            for s in (
                resolve_score(r, default=None, prefer_deterministic=False)
                for r in results_list
            )
            if s is not None
        ]
        # Zero is a valid failing composite; fall back only when the score is absent.
        det_composite = det.get("composite_score") if det else None
        avg_score = round(sum(scores) / len(scores), 4) if scores else None
        if det_composite is not None:
            composite, metric = det_composite, "deterministic"
        elif avg_score is not None:
            composite, metric = avg_score, "llm_avg"
        else:
            composite, metric = None, None
        return {
            "composite": composite,
            "composite_metric": metric,
            "test_count": len(results_list),
            "pass_count": sum(1 for s in scores if s >= ACTIONABLE_THRESHOLD),
            "avg_score": avg_score,
        }

    with_skill = compute_summary(results, det_scores)
    without_skill = (
        compute_summary(baseline_results, baseline_det) if baseline_results else None
    )

    delta = None
    if with_skill and without_skill:
        both_avg = (
            with_skill["avg_score"] is not None
            and without_skill["avg_score"] is not None
        )
        if (
            with_skill["composite_metric"] is not None
            and with_skill["composite_metric"] == without_skill["composite_metric"]
        ):
            composite_delta = round(
                with_skill["composite"] - without_skill["composite"], 4
            )
            metric = with_skill["composite_metric"]
        elif both_avg:
            composite_delta = round(
                with_skill["avg_score"] - without_skill["avg_score"], 4
            )
            metric = "llm_avg"
        else:
            composite_delta = None
            metric = None
            print(
                "Warning: with-skill and baseline runs share no common score "
                "metric; composite delta omitted",
                file=sys.stderr,
            )
        delta = {
            "composite_delta": composite_delta,
            "metric": metric,
            # Compare pass counts only when both rounds have judge scores.
            "pass_count_delta": (
                with_skill["pass_count"] - without_skill["pass_count"]
            )
            if both_avg
            else None,
            "avg_score_delta": round(
                with_skill["avg_score"] - without_skill["avg_score"], 4
            )
            if both_avg
            else None,
        }

    return {
        "version": "1.0",
        "run_summary": {
            "with_skill": with_skill,
            "without_skill": without_skill,
            "delta": delta,
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate Agent Skills spec eval artifacts"
    )
    parser.add_argument("output_dir", help="Directory containing results.json")
    parser.add_argument("--criteria", required=True, help="Path to eval_criteria.json")
    parser.add_argument(
        "--timing-ms", type=int, default=0, help="Eval duration in milliseconds"
    )
    parser.add_argument(
        "--timing-tokens", type=int, default=0, help="Estimated token count"
    )
    parser.add_argument(
        "--baseline-dir", help="Directory containing baseline results.json"
    )
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")

    args = parser.parse_args()

    # Load inputs
    results_path = os.path.join(args.output_dir, "results.json")
    det_path = os.path.join(args.output_dir, "deterministic_scores.json")

    results = load_json(results_path)
    if not results:
        print(f"Error: Cannot load {results_path}", file=sys.stderr)
        sys.exit(1)

    det_scores = load_json(det_path)

    # Stop on unusable criteria rather than generating undefined eval references.
    test_cases = load_criteria_json(args.criteria)
    if test_cases is None:
        print(
            f"Error: Cannot load criteria {args.criteria}; no artifacts written",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load baseline if provided
    baseline_results = None
    baseline_det = None
    if args.baseline_dir:
        baseline_results = load_json(os.path.join(args.baseline_dir, "results.json"))
        baseline_det = load_json(
            os.path.join(args.baseline_dir, "deterministic_scores.json")
        )

    # Generate artifacts
    evals = generate_evals(test_cases, results)
    grading = generate_grading(results, det_scores)
    timing = generate_timing(args.timing_ms, args.timing_tokens)
    benchmark = generate_benchmark(results, det_scores, baseline_results, baseline_det)

    # Write artifacts
    artifacts = {
        "evals.json": evals,
        "grading.json": grading,
        "timing.json": timing,
        "benchmark.json": benchmark,
    }

    for filename, data in artifacts.items():
        path = os.path.join(args.output_dir, filename)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    # Output summary
    summary = {
        "generated": list(artifacts.keys()),
        "output_dir": args.output_dir,
        "has_baseline": baseline_results is not None,
        "grading_pass_rate": grading["summary"]["pass_rate"],
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Generated {len(artifacts)} spec artifacts in {args.output_dir}")
        print(f"  Grading pass rate: {grading['summary']['pass_rate']:.1%}")
        if baseline_results:
            delta = benchmark["run_summary"]["delta"]
            if delta and delta["composite_delta"] is not None:
                print(
                    f"  Baseline delta: {delta['composite_delta']:+.4f} "
                    f"({delta['metric']})"
                )
            else:
                print("  Baseline delta: unavailable (no common metric)")


if __name__ == "__main__":
    main()
