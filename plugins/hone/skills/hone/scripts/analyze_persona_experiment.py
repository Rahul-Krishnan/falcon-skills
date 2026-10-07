#!/usr/bin/env python3
"""Compare baseline and persona-injected MSL Judge scores.

Run the eval runner with the same criteria and each persona, then pass the
results.json files here to measure whether scores shift as intended.

Usage:
    python3 analyze_persona_experiment.py <baseline_results.json> <persona_name>:<persona_results.json> ...
"""

import argparse
import json
import sys


def load_results(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def extract_scores(results: dict) -> dict:
    """Extract per-test scores from results.json"""
    scores = {}
    for test in results.get("results", []):
        test_id = test.get("test_id", test.get("id", "unknown"))
        scores[test_id] = {
            "score": test.get("score", 0),
            "semantic_scores": {},
        }
        # Try to extract per-check scores from judge feedback
        feedback = test.get("judge_feedback", {})
        if isinstance(feedback, dict):
            for check, detail in feedback.get("semantic_scores", {}).items():
                if isinstance(detail, dict):
                    scores[test_id]["semantic_scores"][check[:50]] = detail.get(
                        "score", 0
                    )
    return scores


def build_report(baseline_path: str, persona_paths: dict) -> dict:
    """Return a structured comparison of baseline against each persona run."""
    baseline = extract_scores(load_results(baseline_path))
    personas = {}

    for persona_name, persona_path in persona_paths.items():
        persona = extract_scores(load_results(persona_path))
        per_test = []
        deltas = []
        for test_id in baseline:
            if test_id not in persona:
                continue
            baseline_score = baseline[test_id]["score"]
            persona_score = persona[test_id]["score"]
            delta = persona_score - baseline_score
            deltas.append(delta)
            per_test.append(
                {
                    "test_id": test_id,
                    "baseline": baseline_score,
                    "persona": persona_score,
                    "delta": round(delta, 4),
                }
            )
        personas[persona_name] = {
            "results_path": persona_path,
            "per_test": per_test,
            "avg_delta": round(sum(deltas) / len(deltas), 4) if deltas else None,
            "max_abs_delta": round(max(deltas, key=abs), 4) if deltas else None,
            "shifted_over_0_1": sum(1 for d in deltas if abs(d) > 0.1),
            "shifted_over_0_05": sum(1 for d in deltas if abs(d) > 0.05),
            "test_count": len(deltas),
        }

    return {
        "baseline_path": baseline_path,
        "baseline_test_count": len(baseline),
        "personas": personas,
    }


def compare_runs(baseline_path: str, persona_paths: dict) -> None:
    """Compare baseline scores against persona-modified scores."""
    baseline = extract_scores(load_results(baseline_path))

    print(f"\n{'=' * 80}")
    print(f"PERSONA INJECTION EXPERIMENT RESULTS")
    print(f"{'=' * 80}\n")

    print(f"Baseline: {baseline_path}")
    print(f"Tests: {len(baseline)}\n")

    for persona_name, persona_path in persona_paths.items():
        persona = extract_scores(load_results(persona_path))

        print(f"\n--- {persona_name.upper()} ---")
        print(f"{'Test ID':<30} {'Baseline':>10} {'Persona':>10} {'Delta':>10}")
        print(f"{'-' * 60}")

        deltas = []
        for test_id in baseline:
            if test_id in persona:
                b_score = baseline[test_id]["score"]
                p_score = persona[test_id]["score"]
                delta = p_score - b_score
                deltas.append(delta)
                marker = " ***" if abs(delta) > 0.1 else ""
                print(
                    f"{test_id:<30} {b_score:>10.3f} {p_score:>10.3f} {delta:>+10.3f}{marker}"
                )

        if deltas:
            avg_delta = sum(deltas) / len(deltas)
            max_delta = max(deltas, key=abs)
            print(f"\n  Avg delta: {avg_delta:+.3f}")
            print(f"  Max delta: {max_delta:+.3f}")
            print(
                f"  Tests with |delta| > 0.1: {sum(1 for d in deltas if abs(d) > 0.1)}/{len(deltas)}"
            )
            print(
                f"  Tests with |delta| > 0.05: {sum(1 for d in deltas if abs(d) > 0.05)}/{len(deltas)}"
            )

    print(f"\n{'=' * 80}")
    print("INTERPRETATION:")
    print(
        "  - If deltas are mostly < 0.05: personas DON'T shift scores (mechanism is a no-op)"
    )
    print("  - If deltas are 0.05-0.15: personas shift scores moderately (useful)")
    print(
        "  - If deltas are > 0.15: personas shift scores strongly (may need calibration)"
    )
    print(
        "  - If all personas shift in the SAME direction: not independent perspectives"
    )
    print(f"{'=' * 80}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare baseline eval results against persona-injected runs to test "
            "whether persona framing shifts scores"
        ),
        epilog=(
            "Example: python3 analyze_persona_experiment.py baseline.json "
            "pragmatist:prag.json architect:arch.json"
        ),
    )
    parser.add_argument("baseline_results", help="Path to the baseline results.json")
    parser.add_argument(
        "persona_results",
        nargs="+",
        metavar="NAME:PATH",
        help="One or more <persona_name>:<results.json> pairs",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    persona_paths = {}
    for pair in args.persona_results:
        if ":" not in pair:
            print(
                f"ERROR: expected <persona_name>:<results.json>, got '{pair}'",
                file=sys.stderr,
            )
            sys.exit(2)
        name, path = pair.split(":", 1)
        persona_paths[name] = path

    try:
        if args.json:
            json.dump(build_report(args.baseline_results, persona_paths), sys.stdout, indent=2)
            print()
        else:
            compare_runs(args.baseline_results, persona_paths)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: results file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
