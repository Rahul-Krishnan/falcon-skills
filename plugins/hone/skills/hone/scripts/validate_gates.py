#!/usr/bin/env python3
"""Validate gate schema, required events, and failure handling in workflow state.

Validate types and enums against gate-event-schema.json, including explicit
nulls on optional fields. Derive the event set from steps{}; warn when --mode
contradicts it. Applied edits require scope_verify; resumed=true requires
resume, and --resumed may only add that requirement.

A scope-check exemption requires a derived error-halt and gate evidence of
a crash before verification. Failure handling uses hone_common.fail_is_accounted.

Exit codes: 0 valid, 1 validation failure, 2 usage error. Stdlib only.
Usage: python3 <skill-dir>/scripts/validate_gates.py <state> --json"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hone_common import HALT_REASONS, derive_gate_mode, fail_is_accounted

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

# Required event membership by mode, excluding per-attempt handoff events.
# Modes reaching Phase 3 require convergence. Tuple order follows the docs
# for readability; hone_common.PHASE3_HALT_SEQUENCE defines halt ordering.
REQUIRED_STEPS = {
    "normal": (
        "phase1_to_phase2", "phase2_to_phase3", "phase3_exit",
        "convergence", "workflow_exit",
    ),
    "fix-only": (
        "fixonly_entry", "phase2_to_phase3", "phase3_exit",
        "convergence", "workflow_exit",
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


# Gate events that only a run which got past Phase 2 Step 6a can have emitted.
# The `scope_verify` exemption is a claim that the run died between Step 6 and
# Step 6a, and any one of these in the same gates[] contradicts it.
PAST_PHASE2 = ("phase2_to_phase3", "phase3_exit", "convergence")


def scope_verify_exempt(mode: str, gates: list | None = None) -> bool:
    """True when a derived error-halt may omit scope_verify after edits.

    A crash can occur between Phase 2 edit recording and scope verification.
    Require a failing gate and no event from after verification. Derive mode
    from steps{}, never --mode; unusable steps default to normal in main().
    This blocks completed runs from claiming exemption via a flag or stale status.
    For callers without a gate list, gates=None retains the mode-only check."""
    if mode != "error-halt":
        return False
    if gates is None:
        return True
    events = [g for g in gates if isinstance(g, dict)]
    if not any(g.get("result") == "fail" for g in events):
        return False
    return not any(g.get("step") in PAST_PHASE2 for g in events)


def _expected_steps(
    mode: str,
    resumed: bool = False,
    edits_applied: bool = False,
    exempt: bool = False,
) -> tuple[str, ...]:
    """Return required events for mode, resumption, and applied edits.
    main derives the latter two from state; --resumed can only add a requirement.
    A scope_verify exemption still produces a warning when the event is absent."""
    steps = REQUIRED_STEPS.get(mode, ())
    if edits_applied and not exempt:
        steps = steps + ("scope_verify",)
    if resumed:
        steps = steps + ("resume",)
    return steps


def derive_resumed(state: object) -> bool:
    """Read the resumed flag set by SKILL.md alongside the resume event. This lets
    the mandatory exit check require resume without relying on CLI flags."""
    return isinstance(state, dict) and state.get("resumed") is True


def derive_edits_applied(state: object) -> bool:
    """Whether the run applied at least one Phase 2 edit, per the state file.

    Reads `applied_edits.edit_count`, the field validate_handoff.py already
    requires of the phase2_apply handoff.
    """
    if not isinstance(state, dict):
        return False
    applied = state.get("applied_edits")
    if not isinstance(applied, dict):
        return False
    count = applied.get("edit_count")
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return False
    return count > 0


def validate_gates(
    gates: list,
    mode: str,
    resumed: bool = False,
    edits_applied: bool = False,
    derived_mode: str | None = None,
) -> dict:
    """Report schema, completeness, and failure-handling findings.

    Only derived_mode may grant a scope-check exemption. None treats mode as
    derived for in-process callers; main always passes a value and uses normal
    when steps{} is unusable, preventing --mode from granting the exemption."""
    errors: list[str] = []
    warnings: list[str] = []
    exempt = scope_verify_exempt(
        mode if derived_mode is None else derived_mode,
        # An unusable gates[] is not evidence of a halt, so it earns no
        # exemption: the empty list has no failing event and fails the guard.
        gates if isinstance(gates, list) else [],
    )

    if not isinstance(gates, list):
        return {
            "valid": False,
            "mode": mode,
            "resumed": resumed,
            "edits_applied": edits_applied,
            "gate_count": 0,
            "errors": ["gates is not a list"],
            "warnings": [],
            "missing_steps": list(
                _expected_steps(mode, resumed, edits_applied, exempt)
            ),
        }

    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            errors.append(f"gates[{index}] is not an object")
            continue
        for key in ("step", "judge", "result"):
            if key not in gate:
                errors.append(f"gates[{index}] missing required key '{key}'")
        # Validate optional fields when present, including nulls. Enum membership
        # already checks judge/result types; other fields need explicit type checks.
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
        # Require string reasons everywhere and closed vocabulary values on failing
        # steps listed in HALT_REASONS. Unknown values are errors and also lose retry
        # and restart credit. Other steps retain free-form annotations.
        #
        # Absent reasons warn for legacy states; invalid values error. There is no
        # schema version to bound a migration window, and warning on invalid values
        # would hide typos indefinitely. Previously documented ledger_missing remains
        # valid; formerly legal free-form convergence reasons now require migration.
        if "reason" in gate and not isinstance(gate["reason"], str):
            errors.append(
                f"gates[{index}] reason must be a string, got "
                f"{type(gate['reason']).__name__}"
            )
        if (
            gate.get("result") == "fail"
            and isinstance(step, str)
            and step in HALT_REASONS
        ):
            vocabulary = tuple(sorted(HALT_REASONS[step]))
            reason = gate.get("reason")
            if "reason" not in gate:
                # Warn for missing legacy reasons; they remain valid but lose
                # retry and restart credit.
                warnings.append(
                    f"gates[{index}] step '{step}' failed without declaring a "
                    f"reason; one of {vocabulary} is expected, and an "
                    "undeclared halt is settled conservatively (no restart, "
                    "no in-place retry)"
                )
            elif isinstance(reason, str) and reason not in HALT_REASONS[step]:
                errors.append(
                    f"gates[{index}] step '{step}' reason {reason!r} is not "
                    f"one of {vocabulary}"
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

    # Share failure handling with the scorer. A halt, authorized restart, or
    # documented retry may account for failure; later progress alone cannot.
    # Convergence reasons restrict these options; they never corroborate a claim.
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict) or gate.get("result") != "fail":
            continue
        if not fail_is_accounted(
            gates[index + 1 :], gate.get("step"), gate.get("reason")
        ):
            warnings.append(
                f"gates[{index}] step '{gate.get('step')}' failed but the run "
                "continued: no halt tail behind it, no recorded 'resume' after "
                "a halt, and no in-place retry of that step"
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
        for step in _expected_steps(mode, resumed, edits_applied, exempt)
        if step not in emitted
    ]
    for step in missing:
        if step == "resume":
            errors.append(
                "missing required gate event 'resume': the run resumed from a "
                "state file but never recorded that it did"
            )
        elif step == "scope_verify" and mode == "error-halt":
            # The exemption was claimed and refused. Say which of the two
            # guards refused it, or the message reads as the plain
            # missing-event error the error-halt mode is supposed to be spared.
            if derived_mode is not None and derived_mode != "error-halt":
                errors.append(
                    "missing required gate event 'scope_verify': the run "
                    "applied edits and never verified their scope. Only an "
                    "error halt is excused this, and the halt has to be "
                    "visible in the state file's steps{}, which derives "
                    f"'{derived_mode}' here (a steps{{}} that is missing, "
                    "empty, or unreadable derives no halt and counts as "
                    "'normal'). --mode does not confer the exemption."
                )
            else:
                errors.append(
                    "missing required gate event 'scope_verify': the run "
                    "applied edits and never verified their scope. An error "
                    "halt is excused this only when its gates[] show a halt it "
                    "could have died in -- at least one failing event, and "
                    f"none of {list(PAST_PHASE2)}, which only a run already "
                    "past Phase 2 Step 6a can emit. These do not."
                )
        else:
            errors.append(f"missing required gate event '{step}' for mode '{mode}'")

    # The error-halt exemption from _expected_steps, recorded rather than
    # dropped: the run applied edits and never verified their scope, which is
    # expected of a crash between Phase 2 Step 6 and Step 6a but is still the
    # one thing a reader of this report wants to know about those edits.
    if edits_applied and exempt and "scope_verify" not in emitted:
        warnings.append(
            "edits were applied but no 'scope_verify' event was recorded; the "
            "run halted on an error before Phase 2 Step 6a, so the scope of "
            "those edits is unverified"
        )

    return {
        "valid": not errors,
        "mode": mode,
        "resumed": resumed,
        "edits_applied": edits_applied,
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
            "override and the derived mode draws a warning. It cannot reach "
            "the scope_verify requirement: that exemption reads the derived "
            "mode only, so --mode error-halt cannot switch the check off."
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

    edits_applied = derive_edits_applied(state)
    resumed = derive_resumed(state) or args.resumed
    # Keep derived_mode separate from --mode. Use normal when steps{} cannot
    # yield a mode; None would let the override grant a scope-check exemption.
    report = validate_gates(
        state.get("gates", []), mode, resumed, edits_applied,
        derived_mode=derived_mode if derived_mode is not None else "normal",
    )

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
