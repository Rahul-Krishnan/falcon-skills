#!/usr/bin/env python3
"""Validate the gates[] array in a hone workflow state file.

Gate emission is fully verifiable from the state file, so it belongs in a check
rather than in prose. Four separate warnings in SKILL.md ("Common Executor
Mistakes" #7 and #9, the Mechanical Exit Gate paragraph, and the Gate Events
table) all describe constraints this script enforces mechanically.

What it checks:
  1. Schema: every event has step, judge, result; judge is in the
     references/gate-event-schema.json enum; result is exactly "pass" or
     "fail"; step/ts are strings, findings is an array of strings, and
     rubric items carry severity/item/result with the schema's enums and a
     string item. A present key holding null violates the schema exactly
     as a wrong type does (draft-07 rejects null for a declared
     string/array). Violations of the published schema are errors —
     this script is the deterministic compliance check the schema promises,
     so it must not bless a state file the schema rejects.
  2. Completeness: the expected event set for the run mode is present. The
     mode is derived from the state file's steps{} map via hone_common's
     run-shape table (fix-only / no-improvement / normal, plus error-halt
     when non-done, non-skipped steps remain) — the executor does not get
     to pick the gate set it is graded against. --mode is an explicit
     override; a contradiction with the derived mode draws a warning.
     `resume` is required exactly when the state file records
     `"resumed": true`. That condition is read off the state file, so it
     cannot be switched off by the caller; --resumed can only turn the
     resume requirement on.
  3. Fail semantics: a "fail" event is legitimate when it is terminal —
     the pipeline halted there, followed at most by the mandated final
     workflow_exit event(s) — or when a later "pass" for the same step
     records the repair. A "fail" followed by unrelated forward progress
     is flagged.

Exit codes: 0 valid, 1 validation failure, 2 usage error.

Stdlib only. Invoke as: python3 <skill-dir>/scripts/validate_gates.py <state> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hone_common import derive_gate_mode, is_halt_tail

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
    "normal": (
        "phase1_to_phase2", "phase2_to_phase3",
        "phase3_exit", "workflow_exit",
    ),
    "fix-only": (
        "fixonly_entry", "phase2_to_phase3",
        "phase3_exit", "workflow_exit",
    ),
    "error-halt": ("workflow_exit",),
    # Phase 1 found nothing to improve, so Phase 2 and Phase 3 never ran.
    "no-improvement": ("phase1_to_phase2", "workflow_exit"),
}


# Rubric-item enums mirror references/gate-event-schema.json (rubric.items).
RUBRIC_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
RUBRIC_RESULTS = ("pass", "fail", "warn")


def _rubric_errors(index: int, rubric: object) -> list[str]:
    """Schema errors for a gate's optional rubric array.

    gate-event-schema.json requires each rubric item to be an object with
    severity/item/result, severity in RUBRIC_SEVERITIES and result in
    RUBRIC_RESULTS. Only an absent rubric is fine (the field is optional);
    a present null is rejected by the schema like any other non-array, so
    the caller gates on key presence, not `is not None`.
    """
    if not isinstance(rubric, list):
        return [
            f"gates[{index}] rubric must be an array, got "
            f"{type(rubric).__name__}"
        ]
    errors: list[str] = []
    for item_index, item in enumerate(rubric):
        label = f"gates[{index}] rubric[{item_index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} is not an object")
            continue
        for key in ("severity", "item", "result"):
            if key not in item:
                errors.append(f"{label} missing required key '{key}'")
        if "item" in item and not isinstance(item["item"], str):
            errors.append(
                f"{label} item must be a string, got "
                f"{type(item['item']).__name__}"
            )
        if "severity" in item and item["severity"] not in RUBRIC_SEVERITIES:
            errors.append(
                f"{label} severity {item['severity']!r} is not one of "
                f"{RUBRIC_SEVERITIES}"
            )
        if "result" in item and item["result"] not in RUBRIC_RESULTS:
            errors.append(
                f"{label} result {item['result']!r} is not one of "
                f"{RUBRIC_RESULTS}"
            )
    return errors


def _expected_steps(mode: str, resumed: bool = False) -> tuple[str, ...]:
    """Expected event set for a mode, plus the one conditional event.

    Resumption is orthogonal to mode (any mode can be resumed), so it is a
    flag rather than a fifth mode.

    It is derived from the state file in main(), never read off a caller
    flag that could switch it off -- an executor allowed to declare "not a
    resume" would turn off the check that catches its unrecorded
    resumption. `--resumed` survives only as a one-way override: it can add
    the requirement, never remove the one the state file established.
    """
    steps = REQUIRED_STEPS.get(mode, ())
    if resumed:
        steps = steps + ("resume",)
    return steps


def derive_resumed(state: object) -> bool:
    """Whether the run resumed from an existing state file, per the state file.

    Reads `resumed`, which SKILL.md's resume protocol sets alongside the
    `resume` gate event. Deriving it here is what makes the requirement
    enforceable: the exit gate runs `validate_gates.py` with no flags, so a
    resumption recorded only by a caller-supplied `--resumed` was never
    checked at the one point where checking is mandatory. Two records of the
    same fact also means dropping either one is a detectable error rather
    than a silent one.
    """
    return isinstance(state, dict) and state.get("resumed") is True


def validate_gates(gates: list, mode: str, resumed: bool = False) -> dict:
    """Return a report dict describing schema, completeness, and fail-semantics."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(gates, list):
        return {
            "valid": False,
            "mode": mode,
            "resumed": resumed,
            "gate_count": 0,
            "errors": ["gates is not a list"],
            "warnings": [],
            "missing_steps": list(_expected_steps(mode, resumed)),
        }

    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            errors.append(f"gates[{index}] is not an object")
            continue
        for key in ("step", "judge", "result"):
            if key not in gate:
                errors.append(f"gates[{index}] missing required key '{key}'")
        # Enforce the types gate-event-schema.json declares, not just key
        # presence and enum membership: a list-valued step crashed the
        # emitted-set build below, and null step / numeric ts / dict
        # findings were blessed as VALID against the published schema.
        # Optional fields (ts, findings, rubric) gate on key presence, not
        # `is not None`: draft-07 rejects an explicit null for a declared
        # string/array, so a present null is a schema error too. (judge and
        # result need no separate check — enum membership already rejects
        # any non-string.)
        step = gate.get("step")
        if "step" in gate and not isinstance(step, str):
            errors.append(
                f"gates[{index}] step must be a string, got "
                f"{type(step).__name__}"
            )
        if "ts" in gate and not isinstance(gate["ts"], str):
            errors.append(
                f"gates[{index}] ts must be a string, got "
                f"{type(gate['ts']).__name__}"
            )
        if "findings" in gate:
            findings = gate["findings"]
            if not isinstance(findings, list):
                errors.append(
                    f"gates[{index}] findings must be an array, got "
                    f"{type(findings).__name__}"
                )
            else:
                for finding_index, finding in enumerate(findings):
                    if not isinstance(finding, str):
                        errors.append(
                            f"gates[{index}] findings[{finding_index}] is "
                            f"not a string"
                        )
        result = gate.get("result")
        if "result" in gate and result not in VALID_RESULTS:
            errors.append(
                f"gates[{index}] step '{gate.get('step')}' has result "
                f"'{result}'; only 'pass' or 'fail' are valid"
            )
        judge = gate.get("judge")
        if "judge" in gate and judge not in VALID_JUDGES:
            # Error, not warning: unlike hook event types, the judge
            # vocabulary is owned by this plugin's gate-event-schema.json,
            # which declares it as a closed enum.
            errors.append(
                f"gates[{index}] judge '{judge}' is not one of {VALID_JUDGES}"
            )
        if "rubric" in gate:
            errors.extend(_rubric_errors(index, gate["rubric"]))

    # Fail semantics: terminal, or repaired by a later pass for the same
    # step. "Terminal" means the pipeline halted at that fail: SKILL.md
    # mandates a final workflow_exit event before ANY exit, so a legitimate
    # halt (error halt, regression auto-revert) is followed by the rest of
    # the halt sequence, never by unrelated forward progress. Requiring the
    # literal last index flagged every documented halt shape and invited the
    # executor to append a fabricated repair pass to silence the warning.
    # `convergence` joins workflow_exit in that tail: it is the check the
    # failure capped, and Phase 3 emits it after `phase3_exit`, so the failing
    # step goes to the helper along with the tail. Both files call
    # hone_common.is_halt_tail, so "the same shape" is now one function rather
    # than two hand-copied conditions that had already drifted apart.
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict) or gate.get("result") != "fail":
            continue
        terminal = is_halt_tail(gates[index + 1 :], gate.get("step"))
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

    # Non-string steps already drew a schema error above; keep them out of
    # the set (an unhashable list step would crash the build).
    emitted = {
        g.get("step")
        for g in gates
        if isinstance(g, dict) and isinstance(g.get("step"), str)
    }
    missing = [
        step
        for step in _expected_steps(mode, resumed)
        if step not in emitted
    ]
    for step in missing:
        if step == "resume":
            errors.append(
                "missing required gate event 'resume': the run resumed from a "
                "state file but never recorded that it did"
            )
        else:
            errors.append(f"missing required gate event '{step}' for mode '{mode}'")

    return {
        "valid": not errors,
        "mode": mode,
        "resumed": resumed,
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
        default=None,
        help=(
            "Override the run mode determining the expected event set. By "
            "default the mode is derived from the state file's steps{} map "
            "(hone_common.derive_gate_mode); a contradiction between the "
            "override and the derived mode draws a warning."
        ),
    )
    parser.add_argument(
        "--resumed",
        action="store_true",
        help=(
            "Force the 'resume' event requirement on. Normally derived from "
            "the state file's `resumed` field, so passing this is only needed "
            "for a run that resumed without recording it. One-way: it can add "
            "the requirement, never remove one the state file established. "
            "Orthogonal to --mode."
        ),
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

    if not isinstance(state, dict):
        # Same guard as validate_handoff.main: a null/array root (truncation,
        # bad repair) is a usage error, not an AttributeError traceback that
        # masquerades as "gates invalid" with no JSON for the Mechanical
        # Exit Gate consumer.
        print("ERROR: state file root must be a JSON object", file=sys.stderr)
        sys.exit(2)

    # Derive the mode from steps{} rather than trusting the caller-supplied
    # flag: the expected gate set is the one thing the executor must not
    # pick for itself (an incomplete run would simply claim --mode
    # error-halt). --mode stays as an explicit override; a mismatch with
    # the derived mode is warned about, not silently resolved.
    derived_mode = derive_gate_mode(state.get("steps"))
    if args.mode is not None:
        mode = args.mode
    elif derived_mode is not None:
        mode = derived_mode
    else:
        mode = "normal"

    resumed = derive_resumed(state) or args.resumed
    report = validate_gates(state.get("gates", []), mode, resumed)

    if (
        args.mode is not None
        and derived_mode is not None
        and args.mode != derived_mode
    ):
        report["warnings"].append(
            f"--mode '{args.mode}' contradicts the steps{{}}-derived run "
            f"shape '{derived_mode}'; the explicit override was honored"
        )

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
