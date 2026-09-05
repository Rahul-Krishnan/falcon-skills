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
     `scope_verify` is required when the state file records applied edits
     (applied_edits.edit_count > 0), matching SKILL.md's "mandatory when
     edits were applied"; `resume` is required exactly when the state file
     records `"resumed": true`. Both conditions are read off the state file,
     so neither can be switched off by the caller; --resumed can only turn
     the resume requirement on.
     The one exemption is an error halt that crashed between the edit and
     the verify, and it is guarded on both sides (scope_verify_exempt): it
     reads the steps{}-derived mode and never --mode, and it is refused
     unless gates[] itself shows a halt the run could have died in. An
     exemption a run can claim for itself is an off switch.
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

# Events required for each run mode. Handoff events (handoff_<name>) are
# emitted per validation attempt and are not part of a fixed expected set.
#
# `convergence` sits in the two modes that reach Phase 3. SKILL.md's gate
# table marks it mandatory, and a gate the table calls mandatory that this
# script does not check is prose: nothing stops a run from omitting it.
#
# These are MEMBERSHIP sets, not sequences -- the check below is "was this
# step emitted at all". They are nonetheless listed in the documented emission
# order (`phase3_exit` at Phase 3 step 6, then `convergence` at step 7),
# because a reader who takes the tuple order for the emission order gets the
# halt shape backwards. That misreading is exactly how `hone_common`'s halt
# tail came to reject the documented auto-revert halt. The one authoritative
# statement of the order is `hone_common.PHASE3_HALT_SEQUENCE`.
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
    """Whether this run may omit `scope_verify` after applying edits.

    `error-halt` is the only mode that can, and only when it is a real halt.
    That mode means the run stopped mid-flight, and Phase 2 writes
    `edit_count` at Step 6 while `scope_verify` is emitted at Step 6a, so a
    crash between the two is a legitimate halt that would otherwise be
    reported as a missing required event -- an error, on a run whose whole
    point is that it did not finish. SKILL.md's resume note says the same
    thing ("the derived mode is error-halt, and its only required event is the
    workflow_exit").

    An exemption that a run can claim for itself is not an exemption, it is an
    off switch, and this one was reachable two ways. `--mode error-halt` set
    it straight from a caller flag: main() now passes the *derived* mode here
    and never the override, and when `steps{}` is missing, empty, or not a
    dict -- the cases `derive_gate_mode` cannot derive a mode from -- it
    passes `normal` rather than falling back to the flag, so the flag can no
    longer reach it by either route. The exemption is a claim about `steps{}`,
    and a `steps{}` nobody can read is not evidence for it. The second way
    needed no flag at all -- leave one entry of `steps{}` at `in_progress` and
    `derive_gate_mode` returns `error-halt` on its own. `gates[]` closes that
    one, because a run claiming it died mid-flight has to look like it:

      * it must record an actual failure. Every gate passing is a run that
        finished, whatever `steps{}` says about itself, and a run that
        finished ran Step 6a.
      * it must not record an event only a run past Step 6a could emit. A
        `phase2_to_phase3`, `phase3_exit`, or `convergence` beside the claim
        says the run reached Phase 3, so Step 6a was behind it.

    Both are cheap for an honest halt to satisfy (SKILL.md's halt shape is
    already `<step>:fail` then `workflow_exit:fail`) and expensive for a
    complete run to fake, because faking them means filing itself as a failed
    run. `gates=None` keeps the old mode-only answer for a caller that has no
    gate list to offer.
    """
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
    """Expected event set for a mode, plus the two conditional events.

    Resumption is orthogonal to mode (any mode can be resumed), so it is a
    flag rather than a fifth mode. `scope_verify` is the same shape: SKILL.md
    marks it mandatory "when edits were applied", which is a property of the
    run, not of its mode.

    Both are derived from the state file in main(), never read off a caller
    flag that could switch them off -- an executor allowed to declare "no
    edits applied" or "not a resume" would turn off the checks that catch its
    out-of-scope edits and its unrecorded resumption. `--resumed` survives
    only as a one-way override: it can add the requirement, never remove one
    the state file established.

    `exempt` comes from `scope_verify_exempt`, which is where the one
    exemption and its two guards live. The absence is not ignored even then:
    validate_gates() downgrades it to a warning, so an error halt that skipped
    its scope check is still visible in the report.
    """
    steps = REQUIRED_STEPS.get(mode, ())
    if edits_applied and not exempt:
        steps = steps + ("scope_verify",)
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
    """Return a report dict describing schema, completeness, and fail-semantics.

    `derived_mode` is the run shape read off the state file's `steps{}`, which
    is the only mode the `scope_verify` exemption is allowed to consult.
    `mode` may be a caller's `--mode` override, and an override that could
    switch a safety requirement off is not an override, it is a way around the
    requirement. Leave it None and `mode` is treated as the derived one, which
    is what an in-process caller with no state file is saying. main() never
    leaves it None: a state file it cannot derive a mode from gets `normal`,
    because the alternative is the override reaching the exemption after all.
    """
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
        # `reason` is validated because a settlement predicate KEYS on it
        # (hone_common.HALT_REASONS). No value buys an outcome there -- a
        # declaration only ever rules a settlement out -- but the field is
        # still executor-written, so an arbitrary string must not do better
        # than the truth here either. A declaration outside the closed
        # vocabulary is an error, and it is ALSO read as "not declared" by
        # the predicate, which is the strictest branch there is. Failing both
        # ways is the point: junk cannot beat a vocabulary value here or in
        # `hone_common`, so on the events a correct run emits, declaring the
        # truth scores at least as well as anything else it could write.
        #
        # The type check is not scoped to the vocabulary steps, because
        # gate-event-schema.json declares `reason` a string for every event;
        # the enum check is, because `reason` is free-form annotation
        # elsewhere (SKILL.md's `corrupt_state_file` on a `workflow_exit`,
        # "prior evaluation reused" on a `fixonly_entry`).
        #
        # THE ENUM CHECK IS A DELIBERATE BREAKING CHANGE, not an oversight,
        # and it is deliberately an ERROR rather than a migration-window
        # warning. `reason` was in no `properties` block and was read by
        # nothing before this change, so a free-form value on a failing
        # `convergence` -- `"escalate: f1,f2 open 4 rounds"` -- was legal and
        # now exits this script non-zero at the mechanical exit gate. Three
        # reasons to take that rather than warn:
        #
        #   1. THERE IS NO CORPUS TO MIGRATE. The state file is a per-run
        #      artifact at /tmp/workflow-${RUN_ID}.json (SKILL.md line 230),
        #      written and validated inside the same run. The next run writes
        #      a fresh one under the new rules, so the only file this can
        #      break is an archived one being re-validated by hand, which is
        #      a diagnostic, not a gate on live work.
        #   2. NOTHING EVER TOLD AN EXECUTOR TO WRITE THAT VALUE. The only
        #      `reason` the docs ever mandated on a `convergence:fail` was the
        #      literal `ledger_missing`, which is in the vocabulary and still
        #      validates. The free-form uses the docs DO name sit on
        #      `workflow_exit` and `fixonly_entry`, which this check does not
        #      touch. So the breaking case is legal-but-undocumented rather
        #      than a shape the skill produced.
        #   3. A MIGRATION WINDOW HAS NO WAY TO CLOSE. Nothing in the state
        #      file records a schema version, so a warning added "for a
        #      window" is a warning forever, and a near miss like `"Capped"`
        #      -- a typo, not a legacy value -- would be downgraded along
        #      with it. An error is what makes a typo visible.
        #
        # The absent case stays a WARNING, and the asymmetry with the
        # out-of-vocabulary case is not an inconsistency: absence is what
        # every pre-`reason` state file actually contains, junk is what none
        # of them contain. Neither buys anything either way -- both resolve to
        # `None` and reach the same settlements (`hone_common`'s
        # `declared_halt_reason`) -- so this file is free to report them
        # differently without either one outscoring the truth.
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
                # A WARNING, not an error, and only because state files
                # written before the declaration existed carry no `reason`.
                # They keep validating; what they lose is settlements, not
                # validity, and they lose the same ones every other
                # non-answer loses.
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

    # Fail semantics: terminal, repaired by a later pass for the same step,
    # or superseded by a later attempt at it. "Terminal" means the pipeline halted at that fail: SKILL.md
    # mandates a final workflow_exit event before ANY exit, so a legitimate
    # halt (error halt, regression auto-revert) is followed by the rest of
    # the halt sequence, never by unrelated forward progress. Requiring the
    # literal last index flagged every documented halt shape and invited the
    # executor to append a fabricated repair pass to silence the warning.
    # `workflow_exit` is the whole of that tail: it is the only event SKILL.md
    # mandates after the failure that stopped the run, so the failing step
    # goes to the helper along with the tail. Both files call
    # hone_common.fail_is_accounted, so "the same shape" is now one function
    # rather than two hand-copied conditions that had already drifted apart.
    # It carries two cases the hand-copies missed. An authorized restart: the
    # run halted, recorded `workflow_exit`, and a `resume` records the human
    # granting more rounds, which only a fail declaring `capped` may claim.
    # And a documented in-place retry: the exit-2 ledger repair emits
    # `convergence:fail` and then a second `convergence` that may fail too, so
    # a correct repair has no later `pass` and is not its own halt tail. That
    # retry is reachable only for a fail that declared `"ledger_missing"`, the
    # one failure the docs re-attempt; every other declaration and every
    # non-declaration forfeits it, and the retry that remains still has to sit
    # inside the halt, because `reason` is executor-written and nothing here
    # corroborates it. What the helper deliberately does NOT accept is a later
    # `pass` for a gate whose `fail` was a halt order, which is how a run that
    # ignored an `escalate` and did another round used to score clean.
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
    # `derived_mode` is passed separately, and deliberately: `mode` above may
    # be the caller's --mode, and the scope_verify exemption must never be
    # reachable from a flag. `--mode error-halt` on a completed run that
    # applied edits used to turn the missing scope check into a warning.
    #
    # An underivable mode is passed as "normal" rather than as None, which
    # validate_gates reads as "treat `mode` as derived" and which would hand
    # the exemption straight back to --mode for any state file with a missing,
    # empty, or non-dict steps{}. The run shape used for the expected event
    # set keeps its own fallback above; only the exemption's input is pinned.
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
