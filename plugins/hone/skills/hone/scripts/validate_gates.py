#!/usr/bin/env python3
"""Validate the gates[] array in a hone workflow state file.

Gate emission is fully verifiable from the state file, so it belongs in a check
rather than in prose. Four separate warnings in SKILL.md ("Common Executor
Mistakes" #7 and #9, the Mechanical Exit Gate paragraph, and the Gate Events
table) all describe constraints this script enforces mechanically.

What it checks:
  1. Schema: every event has step, judge, result; judge is a known value;
     result is exactly "pass" or "fail".
  2. Completeness: the expected event set for the run mode is present.
  3. Fail semantics: a "fail" event is legitimate when it is terminal (the
     pipeline halted there) or when a later "pass" for the same step records
     the repair. A "fail" followed by unrelated forward progress is flagged.

Exit codes: 0 valid, 1 validation failure, 2 usage error.

Stdlib only. Invoke as: python3 <skill-dir>/scripts/validate_gates.py <state> --mode normal
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID_RESULTS = ("pass", "fail")


def _load_valid_judges() -> tuple[str, ...]:
    """Load the judge enum from references/gate-event-schema.json.

    The schema is the single source of truth for judge names. The tuple below
    is a fallback copy of that enum for when the schema file is missing or
    malformed (e.g. the script is vendored without its references directory).
    """
    fallback = (
        "automated",
        "self-check",
        "fresh-subagent",
        "model-judge",
        "opus-judge",
        "crucible-judge",
        "human",
    )
    schema_path = (
        Path(__file__).resolve().parent.parent
        / "references"
        / "gate-event-schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        enum = schema["properties"]["judge"]["enum"]
        if (
            isinstance(enum, list)
            and enum
            and all(isinstance(judge, str) for judge in enum)
        ):
            return tuple(enum)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return fallback


VALID_JUDGES = _load_valid_judges()

# Events required for each run mode. Handoff events (handoff_<name>) are
# emitted per validation attempt and are not part of a fixed expected set.
REQUIRED_STEPS = {
    "normal": ("phase1_to_phase2", "phase2_to_phase3", "phase3_exit", "workflow_exit"),
    "fix-only": ("fixonly_entry", "phase2_to_phase3", "phase3_exit", "workflow_exit"),
    "error-halt": ("workflow_exit",),
    # Phase 1 found nothing to improve, so Phase 2 and Phase 3 never ran.
    "no-improvement": ("phase1_to_phase2", "workflow_exit"),
}


def validate_gates(gates: list, mode: str) -> dict:
    """Return a report dict describing schema, completeness, and fail-semantics."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(gates, list):
        return {
            "valid": False,
            "mode": mode,
            "gate_count": 0,
            "errors": ["gates is not a list"],
            "warnings": [],
            "missing_steps": list(REQUIRED_STEPS.get(mode, ())),
        }

    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            errors.append(f"gates[{index}] is not an object")
            continue
        for key in ("step", "judge", "result"):
            if key not in gate:
                errors.append(f"gates[{index}] missing required key '{key}'")
        result = gate.get("result")
        if "result" in gate and result not in VALID_RESULTS:
            errors.append(
                f"gates[{index}] step '{gate.get('step')}' has result "
                f"'{result}'; only 'pass' or 'fail' are valid"
            )
        judge = gate.get("judge")
        if "judge" in gate and judge not in VALID_JUDGES:
            warnings.append(
                f"gates[{index}] judge '{judge}' is not one of {VALID_JUDGES}"
            )

    # Fail semantics: terminal, or repaired by a later pass for the same step.
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict) or gate.get("result") != "fail":
            continue
        terminal = index == len(gates) - 1
        repaired = any(
            isinstance(later, dict)
            and later.get("step") == gate.get("step")
            and later.get("result") == "pass"
            for later in gates[index + 1 :]
        )
        if not (terminal or repaired):
            warnings.append(
                f"gates[{index}] step '{gate.get('step')}' failed but the run "
                "continued and no later 'pass' for that step was recorded"
            )

    emitted = {g.get("step") for g in gates if isinstance(g, dict)}
    missing = [step for step in REQUIRED_STEPS.get(mode, ()) if step not in emitted]
    for step in missing:
        errors.append(f"missing required gate event '{step}' for mode '{mode}'")

    return {
        "valid": not errors,
        "mode": mode,
        "gate_count": len(gates),
        "errors": errors,
        "warnings": warnings,
        "missing_steps": missing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate gates[] in a hone workflow state file"
    )
    parser.add_argument("state_file", help="Path to /tmp/workflow-${RUN_ID}.json")
    parser.add_argument(
        "--mode",
        choices=sorted(REQUIRED_STEPS),
        default="normal",
        help="Run mode determining the expected event set (default: normal)",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    try:
        with open(args.state_file) as handle:
            state = json.load(handle)
    except FileNotFoundError:
        print(f"ERROR: state file not found: {args.state_file}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"ERROR: state file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"ERROR: cannot read state file: {exc}", file=sys.stderr)
        sys.exit(2)

    report = validate_gates(state.get("gates", []), args.mode)

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        status = "VALID" if report["valid"] else "INVALID"
        print(f"{status}: {report['gate_count']} gate event(s), mode={report['mode']}")
        for error in report["errors"]:
            print(f"  ERROR: {error}")
        for warning in report["warnings"]:
            print(f"  WARNING: {warning}")

    sys.exit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
