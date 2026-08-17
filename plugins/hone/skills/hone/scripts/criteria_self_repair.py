#!/usr/bin/env python3
"""Criteria Self-Repair V1: Pattern-table-only deterministic fixes.

Reads eval results, matches failing tests against known criteria bug
patterns, and outputs proposed fixes. Does NOT modify files directly;
the caller applies fixes via Edit tool.

Usage:
    python3 criteria_self_repair.py <results.json> --json

Output (JSON):
    {
        "matched": [{
            "test_id": "TC-008",
            "pattern": "recursive_timeout",
            "fixes": [
                {"field": "allowed_tools", "action": "remove", "values": ["Bash", "Agent"]},
                {"field": "required_absent", "action": "add", "values": ["eval runner", "structural_audit"]},
                {"field": "runner_context", "action": "append", "text": "..."}
            ],
            "confidence": "high",
            "evidence": "timeout_analysis mentions recursive eval, duration >= 600s"
        }],
        "unmatched": [{
            "test_id": "TC-010",
            "score": 0.0,
            "failure_signature": "duration=12s",
            "recommendation": "human_review"
        }],
        "summary": {
            "total_failing": 3,
            "pattern_matched": 2,
            "unmatched": 1
        }
    }
"""

from __future__ import annotations

import argparse
import json
import re
import sys


# === PATTERN TABLE ===
# Each pattern: condition function, fix generator, confidence level.
# Patterns are checked in order; first match wins per test.
# To add a new pattern: define condition + fix functions, add to PATTERNS list.
# Pattern table is append-only during runs. Updates between runs require human review.


def _check_recursive_timeout(test_result: dict) -> bool:
    """Test timed out because it launched a recursive evaluation run."""
    details = test_result.get("details", {})
    timeout_analysis = details.get("timeout_analysis", "")
    duration = test_result.get("duration", 0)
    score = test_result.get("score", 1.0)

    if score > 0.0:
        return False

    # Look for recursive eval patterns in tool calls + timeout
    has_recursive = any(
        kw in timeout_analysis
        for kw in ["run_eval", "skill-eval/", "eval_criteria", "launching eval"]
    )
    is_timeout = (
        duration >= 600
        or "1200s" in timeout_analysis
        or "Duration: 1200" in timeout_analysis
    )

    return has_recursive and is_timeout


def _fix_recursive_timeout(test_result: dict) -> dict:
    """Remove tools that enable recursive execution, add required_absent guards."""
    return {
        "pattern": "recursive_timeout",
        "confidence": "high",
        "evidence": "timeout_analysis shows recursive evaluation launch",
        "fixes": [
            {
                "field": "allowed_tools",
                "action": "remove",
                "values": ["Bash", "Agent", "Write", "Edit"],
                "reason": "Prevents recursive eval launches and workflow state writes",
            },
            {
                "field": "required_absent",
                "action": "add",
                "values": [
                    "structural_audit",
                    "Phase 1",
                    "Phase 2",
                    "eval runner",
                ],
                "reason": "Catches workflow progression that should not occur in error-handling tests",
            },
            {
                "field": "runner_context",
                "action": "append",
                "text": (
                    "\n\n      IMPORTANT: This test verifies error handling at the argument validation stage.\n"
                    "      The correct behavior completes in 1-3 tool calls.\n"
                    "      If the skill proceeds to Phase 1 (structural audit, eval criteria, eval runner),\n"
                    "      that is a FAILURE of the validation gate. Do NOT wait for long-running processes."
                ),
                "reason": "Guides executor to expect early stopping, not full workflow execution",
            },
        ],
    }


def _check_empty_response(test_result: dict) -> bool:
    """Test produced no agent response at all."""
    score = test_result.get("score", 1.0)
    response = test_result.get("agent_response", test_result.get("response", ""))
    details = test_result.get("details", {})
    timeout_analysis = details.get("timeout_analysis", "")

    if score > 0.0:
        return False

    # Empty response without timeout (timeout is handled by recursive_timeout)
    is_empty = not response or len(str(response).strip()) == 0
    is_not_timeout = (
        "1200s" not in timeout_analysis and "Duration: 1200" not in timeout_analysis
    )

    return is_empty and is_not_timeout


def _fix_empty_response(test_result: dict) -> dict:
    """Add runner_context with explicit skill invocation instructions."""
    return {
        "pattern": "empty_response",
        "confidence": "medium",
        "evidence": "Agent produced no response (not a timeout)",
        "fixes": [
            {
                "field": "runner_context",
                "action": "append",
                "text": (
                    "\n\n      After invoking the skill, you MUST produce a text response describing what happened.\n"
                    "      Even if the skill fails or rejects the input, describe the outcome."
                ),
                "reason": "Ensures executor produces scorable output even on error paths",
            }
        ],
    }


def _check_tool_access_errors(test_result: dict) -> bool:
    """Test failed because executor tried tools not in allowed_tools."""
    details = test_result.get("details", {})
    timeout_analysis = details.get("timeout_analysis", "")
    score = test_result.get("score", 1.0)

    if score > 0.3:
        return False

    # Look for high tool_call_errors relative to total calls
    error_match = re.search(r"(\d+) errors?\)", timeout_analysis)
    total_match = re.search(r"Tool calls: (\d+)", timeout_analysis)

    if error_match and total_match:
        errors = int(error_match.group(1))
        total = int(total_match.group(1))
        # More than 50% tool calls errored
        return total > 0 and errors / total > 0.5

    return False


def _fix_tool_access_errors(test_result: dict) -> dict:
    """Add AskUserQuestion to allowed_tools if missing (common for error-handling tests)."""
    return {
        "pattern": "tool_access_errors",
        "confidence": "medium",
        "evidence": ">50% tool calls failed (likely disallowed tools)",
        "fixes": [
            {
                "field": "allowed_tools",
                "action": "add_if_missing",
                "values": ["AskUserQuestion"],
                "reason": "Error-handling tests often need AskUserQuestion for the skill to report errors",
            },
            {
                "field": "runner_context",
                "action": "append",
                "text": (
                    "\n\n      Note: If the skill needs to ask for clarification or report an error,\n"
                    "      it may use AskUserQuestion."
                ),
                "reason": "Guides executor to use available error-reporting tools",
            },
        ],
    }


# Pattern table: checked in order, first match wins.
# Each entry: (condition_fn, fix_fn)
PATTERNS = [
    (_check_recursive_timeout, _fix_recursive_timeout),
    (_check_empty_response, _fix_empty_response),
    (_check_tool_access_errors, _fix_tool_access_errors),
]


def match_patterns(results_path: str) -> dict:
    """Match failing tests against pattern table. Return matched + unmatched."""
    try:
        with open(results_path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {
            "error": f"results file not found: {results_path}",
            "matched": [],
            "unmatched": [],
            "summary": {"total_failing": 0, "pattern_matched": 0, "unmatched": 0},
        }
    except json.JSONDecodeError as exc:
        return {
            "error": f"invalid JSON in {results_path}: {exc}",
            "matched": [],
            "unmatched": [],
            "summary": {"total_failing": 0, "pattern_matched": 0, "unmatched": 0},
        }

    results = data.get("results", [])
    matched = []
    unmatched = []

    for result in results:
        score = result.get("score", result.get("final_score", 1.0))
        # Normalize once: condition functions read result["score"] directly,
        # so a final_score-only result must not default to 1.0 there.
        result.setdefault("score", score)
        test_id = result.get("test_id", "unknown")

        # Only process failures (score < 0.5, since 0.65 is the acceptance threshold)
        if score >= 0.5:
            continue

        # Check each pattern
        pattern_matched = False
        for condition_fn, fix_fn in PATTERNS:
            if condition_fn(result):
                fix = fix_fn(result)
                fix["test_id"] = test_id
                matched.append(fix)
                pattern_matched = True
                break

        if not pattern_matched:
            # Build failure signature for human review
            details = result.get("details", {})
            timeout_analysis = details.get("timeout_analysis", "")
            duration_match = re.search(r"Duration: (\d+)s", timeout_analysis)
            tool_match = re.search(r"Tool calls: (\d+)", timeout_analysis)

            unmatched.append(
                {
                    "test_id": test_id,
                    "score": score,
                    "failure_signature": f"duration={duration_match.group(1)}s"
                    if duration_match
                    else "unknown_duration",
                    "tool_calls": int(tool_match.group(1)) if tool_match else 0,
                    "recommendation": "human_review",
                }
            )

    return {
        "matched": matched,
        "unmatched": unmatched,
        "summary": {
            "total_failing": len(matched) + len(unmatched),
            "pattern_matched": len(matched),
            "unmatched": len(unmatched),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Criteria Self-Repair: match failing tests against known patterns"
    )
    parser.add_argument("results_json", help="Path to results.json from eval runner")
    parser.add_argument(
        "--json", action="store_true", help="Output JSON (default: human-readable)"
    )
    args = parser.parse_args()

    result = match_patterns(args.results_json)

    if result.get("error"):
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        # Human-readable output
        print("=== Criteria Self-Repair V1 ===")
        print(f"Failing tests: {result['summary']['total_failing']}")
        print(f"Pattern matched: {result['summary']['pattern_matched']}")
        print(f"Unmatched (human review): {result['summary']['unmatched']}")
        print()

        for m in result["matched"]:
            print(f"  {m['test_id']}: {m['pattern']} (confidence: {m['confidence']})")
            for fix in m["fixes"]:
                print(
                    f"    {fix['field']}: {fix['action']} {fix.get('values', fix.get('text', '')[:60])}"
                )
            print()

        for u in result["unmatched"]:
            print(f"  {u['test_id']}: UNMATCHED ({u['failure_signature']})")
            print(f"    -> {u['recommendation']}")
            print()


if __name__ == "__main__":
    main()
