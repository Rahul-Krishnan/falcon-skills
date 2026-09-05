#!/usr/bin/env python3
"""Shared helpers and constants for hone's standalone scripts.

The scripts in this directory are standalone CLIs with flat same-directory
imports (no package). This module is the single source of truth for logic
that used to be duplicated (and drifted) across them:

  - Null-tolerant dict access (`get`): the eval_results schema allows
    explicit JSON null for many fields, and dict.get's default only covers an
    absent key — a present-but-null value defeated `d.get(k, default)` and
    produced repeated TypeError crashes across consumers.
  - The canonical per-test score fallback chain (`resolve_score`):
    deterministic composite / `score` / `final_score`.
  - The sibling deterministic_scores.json loaders shared by
    analyze_results.py and criteria_self_repair.py.
  - Side-effecting bash command patterns shared by side_effect_guard.py
    (sandboxing) and validate_eval_criteria.py (runner_context hygiene).
  - The delegation-shaped slash-invocation regex shared by
    side_effect_guard.py (fail-closed sandboxing) and
    validate_eval_criteria.py (missing_skill_tool audit).
  - YAML frontmatter splitting and field extraction shared by
    side_effect_guard.py and structural_audit.py.
  - Pass/acceptance/triage score thresholds. These are AUTHORITATIVE; the
    numbers quoted in references/*.md mirror this module.
  - The run-shape table (RUN_SHAPE_ACTIVE_STEPS + derive_run_shape /
    derive_gate_mode). This is AUTHORITATIVE and stated once: a hone run
    has one of three documented shapes, each derived from the state file's
    steps{} map, and the shape decides which steps (hence which handoffs
    and which gate events) the run is expected to produce:

      normal          phase1_evaluate ran        all steps active
      fix-only        phase1_evaluate "skipped"  Phase 2/3 steps only
                      (SKILL.md's --fix-only entry marks every Phase 1
                      step skipped in one write; no other documented
                      shape skips phase1_evaluate)
      no-improvement  phase2_improve "skipped"   Phase 1 steps only
                      (Phase 1 found nothing to improve; Phases 2 and 3
                      never ran)

    validate_handoff.py consults the table for --step and --all (a handoff
    is required exactly when its producing step is active in the shape and
    actually ran), and validate_gates.py derives its expected-event mode
    from the same map (plus "error-halt" when non-done, non-skipped steps
    remain). SKILL.md and references/*.md defer to this table; do not
    restate the shape rules in prose.

Stdlib only. Keep it dependency-free so the plugin ships self-contained.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Score thresholds (authoritative — references/*.md mirror these values)
# ---------------------------------------------------------------------------

# Below this across a whole run, the eval criteria are suspect rather than
# the artifact (analyze_results triage). Per-test, it is also the "failing
# test" cutoff that criteria_self_repair processes.
CRITERIA_BUG_THRESHOLD = 0.5

# Post-fix score a repaired test must reach for the criteria fix to be
# accepted rather than reverted (Phase 2 self-repair verification).
ACCEPTANCE_THRESHOLD = 0.65

# Phase 1 exit gate: any test scoring below this is an actionable quality
# gap worth a Phase 2 improvement round. PASS/FAIL labels in operator
# summaries must use this same bar so triage and reporting never disagree.
ACTIONABLE_THRESHOLD = 0.8

# Floor applied to each dimension inside score_execution's weighted geometric
# mean, so also the smallest composite a deterministic run can produce: with
# weights summing to 1, all-floored dimensions give 0.05 ** 1 == 0.05. It
# lives here because analyze_results' triage bands have to agree with it —
# written against an exact 0.0, which the floor makes unreachable, `variance`
# could never be returned on a deterministic-only run.
DIMENSION_FLOOR = 0.05


def at_score_floor(score: float) -> bool:
    """True when a composite sits at (or below) the deterministic floor.

    The floor is the "nothing scored" reading that an exact 0.0 used to carry.
    The tolerance absorbs the float noise in `0.05 ** 1` (0.049999999999999996)
    for callers that skip the round-trip through `round(..., 4)`.
    """
    return isinstance(score, (int, float)) and score <= DIMENSION_FLOOR + 1e-9


# ---------------------------------------------------------------------------
# Run shapes (authoritative — see the module docstring)
# ---------------------------------------------------------------------------
# Step-name vocabulary matches the SKILL.md state-file template and
# validate_handoff.py's STEP_CONTRACTS (which has a test asserting the two
# stay in sync).

PHASE1_STEPS: tuple[str, ...] = (
    "phase1_structural_audit",
    "phase1_criteria_audit",
    "phase1_evaluate",
    "phase1_reference_validation",
    "phase1_spec_artifacts",
)

PHASE23_STEPS: tuple[str, ...] = (
    "phase2_trigger_test",
    "phase2_fresh_eyes",
    "phase2_improve",
    "phase3_reevaluate",
)

# The single declarative statement of which tracked steps run in each
# documented run shape. "Active" means the shape can run the step at all;
# whether it actually ran is the step's own status.
RUN_SHAPE_ACTIVE_STEPS: dict[str, frozenset[str]] = {
    "normal": frozenset(PHASE1_STEPS + PHASE23_STEPS),
    "fix-only": frozenset(PHASE23_STEPS),
    "no-improvement": frozenset(PHASE1_STEPS),
}


# Steps that may follow ANY failing gate without contradicting the claim that
# the run halted there. Only `workflow_exit` qualifies unconditionally: it is
# the stop itself, and SKILL.md mandates it before ANY exit. Anything else
# after a fail is forward progress, and a fail followed by forward progress is
# not a halt. Shared so validate_gates.py's warning and score_execution.py's
# score read the same halt shape.
HALT_SEQUENCE_STEPS: frozenset[str] = frozenset({"workflow_exit"})

# Phase 3's terminal event sequence, in the order the docs actually emit it:
# `phase3_exit` at references/phase3-reevaluation.md step 6, `convergence` at
# step 7, then the mandated exit. SKILL.md's gate table lists the same order.
#
# This tuple exists because the order is the whole argument. An earlier
# revision of this comment asserted `... phase2_to_phase3, convergence,
# phase3_exit, workflow_exit`, reasoning from the order of the names in
# validate_gates.REQUIRED_STEPS -- which is a membership check, not a
# sequence. Under the real order the documented regression auto-revert halt is
# `[phase3_exit:fail, convergence:pass, workflow_exit]`, and a flat
# `HALT_SEQUENCE_STEPS` rejected it: `validate_gates` warned on a correct halt
# and `score_gate_compliance` scored it non-compliant. Worse, it was a
# catch-22, because `convergence` is now a REQUIRED step, so a run cannot drop
# it to restore the halt shape.
PHASE3_HALT_SEQUENCE: tuple[str, ...] = (
    "phase3_exit", "convergence", "workflow_exit",
)

# WHAT A `fail` MEANS, which is the axis every settlement rule below turns on.
# There are exactly two kinds in the gate table, and telling them apart is the
# whole of this section:
#
#   VALIDATION VERDICT -- the gate rejected an INPUT and ordered nothing about
#     the run. `handoff_<name>` is the entire family: SKILL.md's gate table
#     gives it "fail then pass on repair", and the Handoff Validation Protocol
#     repairs the document and re-runs the SAME validator. Nothing that
#     happens between the two attempts was forbidden, so a later `pass` is
#     affirmative evidence the input was fixed, whatever route reached it.
#
#   HALT ORDER -- the `fail` IS the instruction to stop. `convergence:fail` is
#     `escalate` or `capped`, both FORCED halts; `phase3_exit:fail` is the
#     regression auto-revert halt; a failed phase transition is a run that
#     could not enter the next phase; `workflow_exit:fail` is the error halt.
#     Nothing a later round does is evidence about a fail of this kind,
#     because RUNNING a later round is precisely what the fail forbade.
#     Emitting more events past a halt order is the violation, not the excuse
#     for it.
#
# This distinction is the invariant, and stating it once here is what closes
# the laundering class rather than one more door in it. Every previous fix in
# this area restated the same fact for a single step and left the next step
# open: pulling `convergence` out of the flat halt set, then making the halt
# tail slice exclusive of the failing step, then constraining the gap on a
# later `fail`. Each was correct and none generalized, because each keyed on
# the STEP or on the later event's RESULT instead of on what the earlier
# `fail` meant. Keyed on meaning, all three are corollaries: a halt order is
# accounted for by the halt, never by work done after it.
#
# Unrecognized steps read as halt orders. That is the strict default on
# purpose -- a gate added later, whose `fail` nobody has classified, must not
# be settleable by simply running on.
VALIDATION_FAIL_PREFIXES: tuple[str, ...] = ("handoff_",)

# Steps the workflow emits ONCE PER ATTEMPT, where a later attempt at the same
# step can settle an earlier attempt's failure. This is orthogonal to the
# distinction above: it says a retry EXISTS, while `fail_orders_halt` says
# what that retry has to look like to count.
#
# Membership is documented retry semantics, not mere recurrence. Both members
# earn it the same way -- the docs describe a failure, a repair of the failing
# input, and a re-run of the SAME check:
#
#   handoff_<name>  SKILL.md's gate table: "each handoff validation attempt",
#                   result "fail then pass on repair". The retry loop is the
#                   Handoff Validation Protocol.
#   convergence     references/phase3-reevaluation.md step 7's exit-2 branch:
#                   emit `convergence:fail` with `reason: ledger_missing`,
#                   write the ledger, re-run the check ONCE, emit the second
#                   `convergence`. That second event may itself be a `fail`
#                   (the re-run returning escalate or capped), which is the
#                   case a later-`pass`-only test cannot see.
#
# `phase3_exit` is deliberately NOT a member even though Phase 3 re-emits it
# every round. A `phase3_exit:fail` is the regression auto-revert, and the
# reference pairs it with an immediate halt; a later `phase3_exit` is
# therefore a run that ignored the halt. Same for `phase1_to_phase2`,
# `phase2_to_phase3`, `fixonly_entry` and `resume`: they recur, but no doc
# gives any of them a fail-then-retry shape, so admitting them would open a
# settlement path for no legitimate sequence.
#
# This prefix tuple holds the same value as `VALIDATION_FAIL_PREFIXES` today
# and is deliberately not the same constant: one says the docs re-attempt the
# step, the other says its `fail` ordered nothing. `convergence` already
# separates them (documented retry, halt-ordering fail), and a future gate can
# land in either set alone.
REPEATABLE_STEPS: frozenset[str] = frozenset({"convergence"})
REPEATABLE_STEP_PREFIXES: tuple[str, ...] = ("handoff_",)

# The gate event that marks an authorized restart of a halted loop. It is the
# one thing that lets a run legitimately emit events after a halt order (see
# `is_authorized_restart`).
RESUMPTION_STEP = "resume"

# Steps whose `fail` a `resume` may restart. Membership is documented restart
# semantics, not plausibility, on the same principle as `REPEATABLE_STEPS`.
#
# Two gates earn it, and the docs name both.
#
#   convergence    references/phase3-reevaluation.md's "Forced exit with human
#                  gate (--confirm mode only)": a `capped` verdict reaches the
#                  human gate, the human grants more rounds, the run raises
#                  `iteration.target` and the ledger's `max_rounds`, emits
#                  `resume`, and re-enters Phase 2.
#   workflow_exit  SKILL.md's gate table gives `resume` for "resuming a run
#                  from an existing state file (after compaction, across
#                  sessions)". The last event before such a break is the exit,
#                  so a `resume` directly behind one is that documented
#                  restart. `is_halt_tail`'s empty-prefix clause already
#                  treats `workflow_exit:fail` as its own stop.
#
# Every other gate is out, and that is the restriction. `phase3_exit:fail` is
# the regression auto-revert, a failed phase transition is a run that could
# not enter the next phase: no doc follows either with a grant of anything, so
# a `resume` behind one is the executor's own authority, which is what the
# halt rules exist to refuse. Before this set, `is_authorized_restart` applied
# to EVERY failing step, so appending `[<any gate>:fail, workflow_exit,
# resume, ...another whole round...]` laundered any halt at all.
#
# A halted run that really is resumed later still records the resume behind
# its own `workflow_exit`, and that exit is resumable; what is refused is
# reading the resume back onto the earlier gate whose fail ordered the halt.
RESUMABLE_STEPS: frozenset[str] = frozenset({"convergence", "workflow_exit"})


# WHAT THE FAIL DECLARES -- the one thing a flat list of events cannot carry.
#
# `convergence:fail` is three different things wearing one name, and their
# settlement rules are opposites:
#
#   escalate        a FORCED halt. references/phase3-reevaluation.md is
#                   explicit that continuing after it "is a fresh `/hone`
#                   invocation with the findings triaged by hand, not an
#                   extension of this one". Nothing restarts it.
#   capped          a FORCED halt that DOES have a documented restart: the
#                   `--confirm` human gate grants more rounds and the run
#                   records a `resume`.
#   ledger_missing  not a halt at all. Exit 2 is a Phase 2 Step 8 omission,
#                   repaired in place by writing the ledger and re-running the
#                   check once; the re-run's verdict then drives the round,
#                   `in_progress` included.
#
# Four previous revisions of the predicates below tried to tell these apart
# from the SHAPE of what followed, and each was principled and each was
# defeated by an ordering it had not been shown. The reason it kept happening
# is not that the inferences were careless: the honest continuation of a
# `ledger_missing` repair and the laundering of an ignored `escalate` are the
# SAME steps with the SAME results in the SAME order. No predicate over the
# list can separate them, because the difference is not in the list.
#
# It is in the executor, which already knows mechanically which one it has:
# `check_convergence.py` returns `escalate` and `capped` as its own `verdict`,
# and exit 2 is the missing ledger. The vocabulary below is deliberately those
# same words, so declaring is copying a value the run already computed rather
# than composing a new claim. The event says why it halted; this table is what
# that declaration is read against.
#
# PER STEP AND PER EVENT, not per run. One correct run emits several
# halt-ordering fails with different reasons -- the exit-2 repair, then the
# re-run's own `capped` -- so a run-level field could not say which was which,
# and the predicates ask about one failing event at a time.
#
# Keyed by step because `reason` is free-form annotation everywhere else:
# SKILL.md's corrupt-state `workflow_exit` carries `corrupt_state_file` and
# `fixonly_entry` carries "prior evaluation reused". Closing the enum over
# every event would invalidate documented examples and buy nothing. A step
# earns a vocabulary exactly when a predicate reads its declaration, which is
# also the fail-closed shape for a gate added later: no vocabulary means no
# declaration unlocks anything, and adding one can only tighten.
#
# AUTHORITATIVE. references/gate-event-schema.json mirrors this enum in its
# conditional branch (test_hone_common asserts the two agree) and SKILL.md and
# references/phase3-reevaluation.md quote it.
HALT_REASONS: dict[str, frozenset[str]] = {
    "convergence": frozenset({"ledger_missing", "escalate", "capped"}),
}

# WHICH SETTLEMENT A DECLARATION LEAVES REACHABLE. Read the direction
# carefully, because it is the whole safety property of this feature: these
# sets do not GRANT anything. They name the one reason that still reaches a
# settlement the undeclared event already reached, and every other value --
# including every non-answer -- takes settlements away.
#
# `reason` is written by the executor being scored and nothing independent
# corroborates it (see is_settled_by_retry, "WHY THERE IS NO CORROBORATION").
# So the invariant is: A DECLARATION MAY ONLY EVER NARROW, measured against
# the behaviour before `reason` was read at all. Every value, and every
# non-answer, leaves a SUBSET of the settlements that same event already had
# (`fail_is_accounted` tabulates the four rows). Lying can therefore cost an
# executor a settlement it would otherwise have had, and can never buy one.
#
# `capped` is the only halt the docs let a `resume` restart, so it is the only
# reason that keeps `is_authorized_restart` reachable -- a restart every
# undeclared halt used to get unconditionally. `ledger_missing` is the only
# `convergence:fail` the docs re-attempt at all, so it is the only reason that
# keeps `is_settled_by_retry` reachable, under the same blunt in-place rule
# that predicate has always applied. `escalate` appears in neither, which is
# the point of naming it: a run that declares `escalate` has said, in its own
# state file, that it may not resume and has no retry.
#
# BOTH GUARDS MUST BE WRITTEN THE SAME WAY, and the way is
# `declared_halt_reason(step, reason) not in <SET>`. Membership is what makes
# the non-answers fail-closed, because `declared_halt_reason` collapses an
# absent, empty, misspelled, invented, or wrongly-typed value to `None` and
# `None` is in neither set. The tempting alternative -- resolve the reason
# first, then subtract only `if reason is not None` -- reads as the same rule
# and is not: it lets every non-answer past the guard, so silence and typos
# outscore a truthful `escalate`. That inversion shipped once (see
# `is_settled_by_retry`) and three sets of hand-picked cases missed it.
#
# Adding a reason to either set is the other way this invariant breaks, so do
# not add one without an independent check that the claim is true.
RESTART_AUTHORIZING_REASONS: frozenset[str] = frozenset({"capped"})
IN_PLACE_REPAIR_REASONS: frozenset[str] = frozenset({"ledger_missing"})


def step_declares_reason(step: object) -> bool:
    """True when this step's `fail` has a closed `reason` vocabulary.

    Only such a step can unlock anything by declaring, and only such a step is
    penalized for declaring nothing. Everywhere else `reason` is free text and
    is read by nothing.
    """
    return isinstance(step, str) and step in HALT_REASONS


def declared_halt_reason(step: object, reason: object) -> str | None:
    """The reason this failing gate declared, or None if it declared nothing.

    FAIL-CLOSED, AND THE FOUR NON-ANSWERS ARE ALL ONE ANSWER. `None` is
    returned for an absent `reason`, for an empty or whitespace-only string,
    for a string outside this step's vocabulary, and for any non-string
    (number, null, list, dict) -- and every caller treats `None` as "not
    declared", which is the conservative branch in each of them. So an
    executor that writes nothing, writes junk, or writes the wrong type can
    never do better than one that declares truthfully; the most it can do is
    decline the concession that telling the truth would have earned.

    Matching is exact and unnormalized on purpose. Case-folding or stripping
    would make the vocabulary bigger than the enum the schema publishes, and
    every widening of a security predicate's input space is a door. A near
    miss (`"Capped"`, `" capped "`) reads as undeclared here and is reported
    as a schema error by validate_gates.py, which is the pair of outcomes a
    typo should have.
    """
    if not step_declares_reason(step):
        return None
    if not isinstance(reason, str):
        return None
    return reason if reason in HALT_REASONS[step] else None


def fail_orders_halt(step: object) -> bool:
    """True when a `fail` on this step is itself an order to stop the run."""
    return not (
        isinstance(step, str) and step.startswith(VALIDATION_FAIL_PREFIXES)
    )


def is_repeatable_step(step: object) -> bool:
    """True for a step the workflow emits once per attempt (see above)."""
    if not isinstance(step, str):
        return False
    return step in REPEATABLE_STEPS or step.startswith(REPEATABLE_STEP_PREFIXES)


def is_authorized_restart(
    later_gates: object,
    failed_step: object,
    declared_reason: object = None,
) -> bool:
    """True when the run halted on this fail and a recorded `resume` restarted it.

    The one legitimate way a run emits events after a halt order, and it is
    the shape references/phase3-reevaluation.md documents for the `capped`
    human gate:

        convergence:fail          <- reason: "capped"
        workflow_exit:fail        <- the loop stopped; the human is asked here
        resume:pass               <- more rounds granted, restart on record
        phase2_to_phase3:pass
        ...

    Four conditions, and only the first was ever obvious:

    1. everything before the `resume` is a valid halt tail for the failing
       step, so the run really did stop before it restarted;
    2. the failing step has a documented restart at all (`RESUMABLE_STEPS`).
       The predicate used to apply to every step, which turned "append an
       exit and a `resume`" into a universal laundering suffix for any halt;
    3. the `resume` itself PASSED. A `resume` carrying `result: "fail"` is a
       restart that did not happen, and reading it as authorization credited
       the run for the event rather than for what the event said;
    4. where the failing step declares a reason (`HALT_REASONS`), that
       declaration is one the docs give a restart -- `capped`, and only
       `capped`.

    Condition 4 closes what was recorded here as a KNOWN GAP.
    `convergence:fail` is `escalate` OR `capped`, only `capped` reaches the
    `--confirm` human gate, and the reference is explicit that continuing
    after `escalate` "is a fresh `/hone` invocation with the findings triaged
    by hand, not an extension of this one". The two produce identical event
    sequences, so the gap was not closable by looking harder at the tail; it
    needed the event to say which verdict it carried, which is now the
    `reason` field `check_convergence.py` already computes.

    A `convergence:fail` that declares nothing, or declares `escalate` or
    `ledger_missing`, or declares junk, authorizes no restart. That is
    stricter than the behaviour this replaces -- an undeclared halt used to be
    resumable -- and deliberately so: "cannot tell" resolves to "refuse".
    The membership test below is what implements that, since `None` is in no
    set; `is_settled_by_retry` is written the same way for the same reason,
    and the two must stay that way (see RESTART_AUTHORIZING_REASONS).
    `workflow_exit` has no vocabulary and needs no declaration; its `fail` has
    exactly one meaning (the error halt) and SKILL.md's cross-session `resume`
    is documented behind it.

    references/phase3-reevaluation.md puts the `--confirm` gate outside and
    after the FORCED halt: asking the human is what happens once the loop has
    STOPPED. So everything before the `resume` must be a valid halt tail for
    the failing step, and everything after it belongs to the restarted run,
    which is legitimate forward progress because the restart is on record.

    A bare `resume` with no halt in front of it is not enough, deliberately:
    that describes a run that skipped its own exit event and carried straight
    on, which is the shape the halt tail exists to catch. And forward progress
    with no `resume` at all -- the attack this whole section is about -- has no
    restart to point at:

        convergence:fail          <- escalate, a mandated immediate halt
        phase2_to_phase3:pass     <- the run ignored it and did another round
        phase3_exit:pass
        convergence:pass          <- and this used to launder the halt
        workflow_exit:pass

    The empty prefix is a halt tail only for `workflow_exit` itself (see
    `is_halt_tail`), which is exactly right: `workflow_exit:fail` IS the stop,
    so a `resume` may follow it immediately, while any other fail needs its
    exit event recorded first.
    """
    if failed_step not in RESUMABLE_STEPS:
        return False
    if step_declares_reason(failed_step) and (
        declared_halt_reason(failed_step, declared_reason)
        not in RESTART_AUTHORIZING_REASONS
    ):
        return False
    if not isinstance(later_gates, (list, tuple)):
        return False
    for index, event in enumerate(later_gates):
        if isinstance(event, dict) and event.get("step") == RESUMPTION_STEP:
            if event.get("result") != "pass":
                return False
            return is_halt_tail(later_gates[:index], failed_step)
    return False


def is_settled_by_retry(
    later_gates: object,
    failed_step: object,
    declared_reason: object = None,
) -> bool:
    """True when a documented retry of the same step settled this fail.

    One predicate where there were two, because the pair it replaces
    (`repaired`: any later `pass`; `superseded`: a later attempt across a
    constrained gap) split the question along the wrong axis. They keyed on
    the RESULT of the LATER event, so the unconstrained arm quietly covered
    every step and every gap, and a `convergence:fail` ordering an escalate
    halt, a whole extra round, and the next round's `convergence:pass` scored
    1.0 -- the very bypass the constrained arm had just been built to close.

    Two conditions, and the second is where `fail_orders_halt` does its work:

    1. The step has documented retry semantics (`is_repeatable_step`). A step
       the docs never re-attempt has no retry to be settled by; its fail is
       accounted for by the halt (or the authorized restart) or not at all.
    2. The retry has to fit the kind of fail it is settling:

       * AN IMMEDIATE RETRY settles either kind. An empty gap is the in-place
         retry both repair loops perform: the exit-2 ledger repair writes a
         file and re-runs the check, a handoff repair edits the handoff and
         re-runs the validator, and neither emits a gate event in between. The
         re-run's own event may itself be a `fail` (the ledger re-run
         returning escalate or capped), so a correct repair can produce no
         later `pass` at all -- and that next attempt then has to account for
         ITSELF, which is what stops a chain of fails from laundering each
         other. Only the first later attempt is considered, which loses
         nothing: gaps only grow, so if the first is non-empty every later one
         is too.

         FOR A HALT ORDER THE RETRY IS ALSO CONSTRAINED FROM BEHIND: what
         follows it must itself be that halt's tail. An empty gap in front of
         the retry says nothing about what comes after it, and the laundering
         this rule closes simply moved there --
         `[convergence:fail, convergence:pass, phase2_to_phase3, phase3_exit,
         convergence, workflow_exit]` put the extra round AFTER the retry and
         scored clean. Requiring the tail makes the whole window a halt, not
         just its front edge.

         CONSERVATIVE ON PURPOSE, AND A DECLARATION DOES NOT LIFT IT. The
         restriction applies to every halt-ordering retry, declared or not,
         and the cost is real and known: a `ledger_missing` repair whose
         re-run returns `in_progress` and legitimately continues into more
         rounds reads as NOT accounted, costing gate score on a correct run.
         That cost is accepted. It is the cheap error -- a false "not
         accounted" costs score, a false "accounted" credits a run for
         ignoring a halt, which is a scoring bypass.

         WHY THERE IS NO CORROBORATION, and so why the declaration cannot buy
         its way out of this. A revision of this predicate did lift the rule
         for a declared `ledger_missing`, on the reasoning that the exit-2
         repair's re-run drives the round and so owes nothing behind it. The
         reasoning about the repair is right; the conclusion was still a
         bypass, because the honest repair and the laundering of an ignored
         `escalate` emit the same steps with the same results in the same
         order, and `reason` is written by the same executor being scored.
         Lying was then monotonically the best move: on the round-6 laundering
         shape, declaring nothing scored not-accounted, declaring the truth
         (`escalate`) scored not-accounted, and declaring `ledger_missing`
         scored accounted.

         Every candidate corroborator was examined and none survives:

           * THE STATE FILE records nothing about the ledger. It carries
             `steps{}`, `iteration`, `gates`, `applied_edits`, `eval_results`
             and `resumed`, and Phase 2 Step 8 is inside `phase2_improve`, so
             there is no write for a `ledger_missing` claim to contradict.
           * THE LEDGER ITSELF cannot separate the two. The honest repair
             WRITES it, so after any run -- honest or lying -- the file exists
             and parses. The signal is null in the one direction needed. It is
             also executor-written, and its path is not derivable from the
             state file validate_gates.py is handed.
           * THE EXECUTION TIMELINE, which score_execution.py's
             `score_gate_compliance` does receive, is executor-written JSON
             (see
             `_timeline_entries`, which normalizes it precisely because it is
             untrusted), it is absent entirely in SIMULATION MODE where gates
             are quoted in prose, and `validate_gates.py` never sees one at
             all. A check only one of the two consumers can run is not a
             check; they would disagree about the same state file.
           * `phase2_to_phase3` is owned by Phase 2 Step 8, so its absence
             would witness the omission that causes exit 2 -- but gates[] is a
             membership set with no per-round keying, and exit 2 also fires on
             a ledger that Step 8 DID write unparseably. Compatible with both
             honest causes and with the lie.

         The general form, which is the part worth remembering: a
         "contradiction between two things the executor wrote" is not
         corroboration. It catches a liar who is also sloppy enough to record
         the truth elsewhere, and any liar declines that for the price of one
         omitted field. There is no second party anywhere in this pipeline, so
         a declaration is only ever admissible as a REASON TO REFUSE.

         What the declaration is still read for here is exactly that:

           * `reason: "escalate"` or `"capped"` -- a FORCED halt. Neither has
             a documented re-attempt, so no retry settles them at all and this
             predicate refuses outright; such a fail is accounted for by its
             halt tail (or, for `capped`, by an authorized restart) or not at
             all. STRICTER than the blunt rule, which is the only direction a
             declaration is allowed to move.
           * `reason: "ledger_missing"` -- the one `convergence:fail` the
             docs re-attempt, so it is the one reason a retry may settle,
             under the same blunt in-place rule this predicate has always
             applied. It buys nothing against the pre-`reason` baseline; it
             merely declines to forfeit.
           * nothing declared, or something unresolvable (empty, whitespace,
             a near miss, an invented word, a non-string) -- NO retry, the
             same answer `escalate` and `capped` get. "Cannot tell" resolves
             to "refuse", which is how every other guard in this file reads a
             missing answer and how `is_authorized_restart` reads this exact
             one. The guard is written `not in IN_PLACE_REPAIR_REASONS` so
             that `None` fails the membership test, rather than as an
             `is not None` test that lets `None` past.

             THE ALTERNATIVE SHAPE WAS A LIVE DISINCENTIVE, and it is worth
             naming because it survived three rounds of hand-picked cases.
             While this guard only subtracted when the reason RESOLVED, a
             `convergence:fail` that declared `escalate` or `capped`
             truthfully forfeited the retry while an absent, empty, or
             misspelled one kept it. On the ordinary
             `[convergence:fail, convergence:pass, workflow_exit]` shape that
             is 0.8333 gate_compliance for the truth against 1.0 for silence
             or a typo, which inverts the invariant `fail_is_accounted`
             states: truth paid, and the field's entire purpose is that it
             cannot.

             THE COST OF FIXING IT IS REAL AND IS ACCEPTED. An UNDECLARED
             exit-2 in-place repair -- the shape every pre-`reason` state
             file writes -- is no longer accounted, so a correct legacy run
             loses gate score for it. That is the same migration cost, of the
             same kind and for the same reason, as the restart such a run
             already forfeits: a false "not accounted" costs score on a
             correct run, while anything that lets silence beat the truth is
             an incentive against adopting the field at all. The repair is
             one string away from being accounted again, and it is a string
             the executor already holds.
       * A LATER `pass` ACROSS ANY GAP settles a VALIDATION VERDICT only.
         Nothing between the attempts was forbidden, so the `pass` is
         affirmative evidence the input was repaired whatever route reached
         it. Extending the same courtesy to a halt order is the bypass:
         work done past a halt order is the violation, and
         `is_authorized_restart` owns the single case where such work is
         legitimate.
    """
    if not is_repeatable_step(failed_step):
        return False
    if not isinstance(later_gates, (list, tuple)):
        return False
    if step_declares_reason(failed_step) and (
        declared_halt_reason(failed_step, declared_reason)
        not in IN_PLACE_REPAIR_REASONS
    ):
        # Only `ledger_missing` has a documented re-attempt, so only
        # `ledger_missing` reaches a retry. A declared FORCED halt (escalate,
        # capped) forfeits it because the docs re-attempt neither, and an
        # UNRESOLVABLE reason -- absent, empty, junk, wrong type -- forfeits
        # it too. The shape is `not in <set>`, deliberately identical to
        # `is_authorized_restart`, and the `None` that falls out of that
        # membership test is the whole point: see the note on the same shape
        # there.
        return False
    if not fail_orders_halt(failed_step) and any(
        isinstance(later, dict)
        and later.get("step") == failed_step
        and later.get("result") == "pass"
        for later in later_gates
    ):
        return True
    for offset, later in enumerate(later_gates):
        if isinstance(later, dict) and later.get("step") == failed_step:
            if later_gates[:offset]:
                return False
            if fail_orders_halt(failed_step):
                # See "CONSTRAINED FROM BEHIND" above. This is the blunt rule,
                # and it applies to a declared `ledger_missing` exactly as it
                # applies to an undeclared fail: the retry has to be inside
                # the halt, so everything after it is the halt's tail. The
                # declaration does not relax it, because nothing corroborates
                # the declaration.
                return is_halt_tail(later_gates[offset + 1:], failed_step)
            return True
    return False


def fail_is_accounted(
    later_gates: object,
    failed_step: object,
    declared_reason: object = None,
) -> bool:
    """The one predicate for "this failing gate is not a compliance defect".

    Three ways, all of them corollaries of the invariant above -- the run did
    not proceed past the fail on its own authority:

      * it stopped there (`is_halt_tail`);
      * it stopped there and a recorded `resume` restarted it, which for a
        step with a vocabulary means the fail declared `capped`
        (`is_authorized_restart`);
      * a documented retry of the same step settled it, in place for a halt
        order or by a later `pass` for a validation verdict
        (`is_settled_by_retry`).

    `declared_reason` is the failing event's own `reason` field. Callers pass
    `gate.get("reason")`; it defaults to None so a caller that passes nothing
    gets the conservative reading rather than a crash, which is the same
    fail-closed default `declared_halt_reason` applies to the value itself.

    THE DECLARATION IS SUBTRACTIVE, and the baseline it subtracts from is the
    behaviour BEFORE `reason` was read at all -- which is the comparison that
    matters, because that behaviour is what an executor got for free. Reading
    `reason` opens no route it did not already have. For any tail, the ways a
    fail can be accounted are:

        before this feature       halt tail, restart, retry
        declares ledger_missing   halt tail,          retry
        declares capped           halt tail, restart
        declares escalate         halt tail
        declares nothing          halt tail
        declares anything else    halt tail

    TWO GUARANTEES, STATED PRECISELY, because a looser wording of them has
    now been wrong twice.

      1. NOTHING EXCEEDS THE PRE-`reason` BASELINE. Every row is a subset of
         the first, so no value and no non-value reaches a settlement the
         same events did not already reach. The most a declaration does is
         give one up.
      2. NO NON-ANSWER BEATS A VOCABULARY VALUE. The bottom two rows are the
         weakest in the table, so for every word in the vocabulary, saying it
         is worth at least as much as saying nothing or saying junk.

    Together these give the property that matters: ON THE EVENTS AN HONEST
    RUN ACTUALLY EMITS, declaring the truth scores at least as well as any
    alternative it could have written instead, and usually strictly better.
    An honest exit-2 repair emits the adjacent re-run, where `ledger_missing`
    is accounted and every other answer is not. An honest `capped` emits the
    exit and the `resume`, where `capped` is accounted and every other answer
    is not. An honest `escalate` emits its halt tail, where every answer is
    accounted and the truth therefore ties rather than loses.

    WHAT IS NOT CLAIMED, and cannot be while the field is uncorroborated: on
    an ARBITRARY sequence, a false declaration may still read better than a
    true one -- `ledger_missing` on a sequence that ran another round beats
    `escalate` on that same sequence. Reading an executor-written field at
    all forfeits that, and the design accepts it because guarantee 1 caps the
    liar at what those same events scored before this field existed. The lie
    buys back a settlement silence used to get; it opens no new door. The one
    sequence where that cap is load-bearing -- the round-6 laundering shape
    -- is refused under every reading, declared or not.

    Guarantee 2 is the half that is easy to lose, and losing it is worse
    than it sounds: were a non-answer to keep a settlement a truthful
    `escalate` gives up, the field would be a tax on telling the truth and
    the correct move would be to leave it out. That is why the last two rows
    sit at the BOTTOM of the table rather than somewhere in the middle.

    Both guarantees have been broken once each, in the two guards
    below, in opposite directions. Round 1: `ledger_missing` bought a retry
    the same events did not otherwise reach, so lying paid. Round 2: the
    retry guard subtracted only when the reason RESOLVED, so `escalate` and
    `capped` forfeited a retry that silence and typos kept, and truth paid.
    Both are guard-shape bugs, and the shape that is correct in both
    directions is the one written in both guards now: `declared_halt_reason(
    ...) not in <SET>`, where `None` fails the membership test. Any rewrite
    of these guards must reproduce this table exactly, including the two
    bottom rows; a test over the full cross product of the vocabulary against
    the non-answers asserts it (`TestNoDeclarationBeatsTheTruth`).

    `capped` and `ledger_missing` each keep rather than gain what they have.
    Every halt used to be resumable unconditionally and every repeatable step
    used to reach the blunt retry, so those two rows recover a piece of what
    silence used to buy, and every other value and non-value now forfeits it.

    That is the property to preserve when touching the three predicates
    below, and it is the reason none of them may be loosened on the strength
    of a `reason` alone: the field is written by the executor being scored and
    nothing in this pipeline corroborates it (see is_settled_by_retry, "WHY
    THERE IS NO CORROBORATION").

    validate_gates.py and score_execution.py each used to hand-copy the first
    of these and had already drifted once. Both now call nothing but this, so
    they cannot disagree about what a compliant failure looks like.
    """
    return (
        is_halt_tail(later_gates, failed_step)
        or is_authorized_restart(later_gates, failed_step, declared_reason)
        or is_settled_by_retry(later_gates, failed_step, declared_reason)
    )


def halt_tail_vocabulary(failed_step: object) -> frozenset[str]:
    """Steps admissible in the tail behind a fail on `failed_step`.

    The vocabulary is evidence-based rather than flat, and the evidence is
    which gate failed. #14 removed `convergence` from the flat halt set for a
    real reason: at the time nothing emitted it, so an executor that appended
    one turned ANY failed gate into a compliant halt -- a scoring bypass that
    paid for claiming a checkpoint the run never reached. Putting it back into
    the flat set would reopen exactly that bypass. Keying the vocabulary to
    `failed_step` keeps both properties at once:

    * `phase3_exit` fails (the auto-revert halt) -- the run demonstrably
      reached Phase 3 step 6, so the `convergence` at step 7 behind it is an
      event the run really did emit. Admitted.
    * `convergence` itself fails (escalate or capped) -- it is the FAILING
      gate. Only the exit may follow. A repair `convergence:pass` is not
      admitted here and does not need to be: both callers test `repaired`
      (a later `pass` for the same step) separately, and that is the check
      that owns the repair shape.
    * `phase1_to_phase2` or `phase2_to_phase3` fails -- the run halted before
      Phase 3 ran, so a `convergence` behind it was invented and cannot excuse
      the failure. Not admitted, which is #14's fix, unchanged.

    The slice is exclusive of `failed_step` itself, which matters. Phase 3
    re-emits `phase3_exit` and `convergence` on every round, so admitting the
    failing step back into its own tail would score a run that failed
    `phase3_exit`, ignored the mandated immediate halt, and looped through
    another whole round as though it had stopped. Only what the documented
    order places strictly AFTER the failure counts as its halt.
    """
    if failed_step in PHASE3_HALT_SEQUENCE:
        start = PHASE3_HALT_SEQUENCE.index(failed_step)
        return HALT_SEQUENCE_STEPS | frozenset(PHASE3_HALT_SEQUENCE[start + 1:])
    return HALT_SEQUENCE_STEPS


def is_halt_tail(later_gates: object, failed_step: object = None) -> bool:
    """True when everything after a failing gate belongs to that gate's halt.

    The one place the halt shape is defined. Both callers had their own copy
    and the copies had drifted: validate_gates.py accepted a tail of
    `convergence` alone, while score_execution.py additionally required
    `workflow_exit`, so the two disagreed about whether a run had halted even
    though a comment in each claimed they scored the same shape.

    `failed_step` is the step of the gate that failed. It decides the
    empty-tail clause below AND the tail vocabulary, so callers pass it.

    A tail is a halt when:

    * nothing follows AND the failing gate is `workflow_exit` itself -- the
      exit is the last event SKILL.md mandates, so a fail there with nothing
      after it is the halt. A fail on any *other* step with nothing after it
      is a run that stopped emitting gates before reaching its mandated exit,
      which is the "fewer events score better" hole one level up; or
    * `workflow_exit` is present (SKILL.md mandates it before ANY exit, so a
      tail without one is a run that carried on) and every event in the tail
      is admissible for this `failed_step` per `halt_tail_vocabulary`.

    So the documented regression auto-revert halt is
    `[phase3_exit:fail, convergence, workflow_exit]`, and the shorter
    `[phase3_exit:fail, workflow_exit]` is a halt too: the vocabulary says
    what MAY follow, not what must. `workflow_exit` itself may pass or fail: a
    passing exit is the ordinary clean stop, and it is the halt rather than
    progress past it. A step the failing gate proves the run never reached --
    `convergence` behind a failed `phase2_to_phase3` -- is not admissible, so
    appending one still cannot launder that fail into a compliant halt.
    """
    if not isinstance(later_gates, (list, tuple)):
        return False
    if not later_gates:
        return failed_step == "workflow_exit"
    allowed = halt_tail_vocabulary(failed_step)
    saw_exit = False
    for later in later_gates:
        if not isinstance(later, dict):
            return False
        step = later.get("step")
        if step not in allowed:
            return False
        if step == "workflow_exit":
            saw_exit = True
    return saw_exit


def derive_run_shape(steps: object) -> str:
    """Run shape derived from the state file's steps{} map.

    Discriminators (see the module docstring's table): phase1_evaluate
    "skipped" marks fix-only, phase2_improve "skipped" marks
    no-improvement, anything else is normal. Tier-based skips of other
    Phase 1 steps (eg phase1_structural_audit on lightweight artifacts)
    deliberately do not change the shape. A non-dict/absent steps map
    derives "normal" — the strictest shape, so a truncated state file is
    never blessed by accident.
    """
    if not isinstance(steps, dict):
        return "normal"
    if steps.get("phase1_evaluate") == "skipped":
        return "fix-only"
    if steps.get("phase2_improve") == "skipped":
        return "no-improvement"
    return "normal"


def derive_gate_mode(steps: object) -> str | None:
    """validate_gates.py's expected-event mode, derived from steps{}.

    The gate check runs as the last action before any exit (SKILL.md's
    Mechanical Exit Gate), where a compliant run has every step "done" or
    "skipped". Any other status means the run halted mid-flight:
    "error-halt". Otherwise the mode is the run shape. Returns None when
    the steps map is absent or unusable (mode cannot be derived; the
    caller falls back to --mode / "normal").
    """
    if not isinstance(steps, dict) or not steps:
        return None
    if any(status not in ("done", "skipped") for status in steps.values()):
        return "error-halt"
    return derive_run_shape(steps)


# ---------------------------------------------------------------------------
# Null-tolerant access
# ---------------------------------------------------------------------------

def get(d: object, key: str, default=None, expected: type | tuple | None = None):
    """dict.get that also treats an explicit JSON null as absent.

    Returns `default` when `d` is not a dict, when `key` is missing, or when
    the stored value is None. The eval_results schema allows null for
    score/details/timeout fields, and a raw `d.get(key, default)` returns
    None in that case, crashing numeric comparisons and method calls
    downstream.

    `expected` optionally extends the same treatment to wrong-typed values:
    when set, a stored value that is not an instance of `expected` also
    returns `default`. Audit-path callers use this because they run on
    schema-invalid files by design — a `runner_context` that arrives as a
    list must not crash `.strip()` before any findings reach stdout.
    """
    if not isinstance(d, dict):
        return default
    value = d.get(key, default)
    if value is None:
        return default
    if expected is not None and not isinstance(value, expected):
        return default
    return value


# Top-level keys that can carry the per-test array, in precedence order.
# `results` is the canonical hone format; `test_results` is the skill-creator
# alias. Both are real inputs, so every consumer of a results file has to
# accept both — analyze_results read only `results` and silently reported an
# empty run on a file score_execution had just graded.
RESULTS_KEYS: tuple[str, ...] = ("results", "test_results")


def extract_results(data: object) -> tuple[list, str | None]:
    """Split a parsed results file into (test entries, key that carried them).

    The returned key is None when neither alias is present, which callers use
    to tell a schema mismatch ("this file is not a results file") from a valid
    file whose array is empty ("no tests ran"). A present-but-wrong-typed value
    yields an empty list under its own key: the schema was recognized, the
    payload was not usable.
    """
    if not isinstance(data, dict):
        return [], None
    for key in RESULTS_KEYS:
        if key in data:
            value = data.get(key)
            return (value if isinstance(value, list) else []), key
    return [], None


def _raw_llm_score(result: dict):
    """The result's own LLM score: `score`, else the `final_score` alias.

    An explicit `score: null` means the judge ran and errored; it is
    returned as None (NOT papered over by the `final_score` alias) so
    resolve_score falls through to the deterministic composite. This pins
    the pre-consolidation behavior of criteria_self_repair's
    `result.get("score", result.get("final_score"))`, where a present-but-
    null `score` never consulted `final_score`. The alias applies only when
    the `score` key is absent entirely (skill-creator runners emit
    `final_score` instead of `score`).

    Non-numeric values (a stringified "0.85", a bool, a list) are treated
    exactly like null: returned as None so resolve_score falls through to
    the deterministic composite/default, mirroring the sibling loaders'
    `isinstance(..., (int, float))` filter. Passing them through crashed
    numeric consumers (`round(score, 4)`, threshold comparisons).
    """
    if not isinstance(result, dict):
        return None
    if "score" in result:
        value = result["score"]
    else:
        value = result.get("final_score")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def resolve_score(
    result: dict,
    det_scores: dict[str, float] | None = None,
    default: float | None = 0.0,
    prefer_deterministic: bool = True,
) -> float | None:
    """Canonical per-test score fallback chain.

    Sources, in order:
      - the deterministic composite for this test_id in `det_scores`
        (from deterministic_scores.json via load_deterministic_scores)
      - the LLM judge `score` in the result; the `final_score` alias some
        runners emit stands in only when the `score` key is absent. An
        explicit `score: null` (judge ran and errored) skips both and
        falls through to the deterministic composite.
      - `default` (0.0 — a scoreless failing test must not look passing).
        Callers that need to tell "no usable score" from a real 0.0 pass
        `default=None` and filter the Nones out; generate_spec_artifacts does
        this so an unscored test stays out of the average instead of dragging
        it down.

    `prefer_deterministic=True` (analyze_results convention) consults the
    deterministic composite first; False (criteria_self_repair convention)
    consults the result's own score first and uses the deterministic
    composite only as a fallback.
    """
    det_scores = det_scores or {}
    # expected=str: dict.get hashes its key even on an empty dict, so a
    # non-string test_id (list, dict) raised TypeError on every call path.
    det = det_scores.get(get(result, "test_id", "unknown", expected=str))
    if prefer_deterministic and det is not None:
        return det
    llm = _raw_llm_score(result)
    if llm is not None:
        return llm
    if det is not None:
        return det
    return default


# ---------------------------------------------------------------------------
# deterministic_scores.json sibling-file loaders
# ---------------------------------------------------------------------------

def _load_det_file(results_path: str) -> dict:
    """Parse deterministic_scores.json next to results.json; {} on any failure.

    "Any failure" includes a file that parses but is not a JSON object
    (e.g. `[]` from truncation or a bad repair) — returning it as-is would
    crash both public loaders on `.get`.
    """
    det_scores_path = Path(results_path).parent / "deterministic_scores.json"
    if not det_scores_path.exists():
        return {}
    try:
        with open(det_scores_path) as f:
            parsed = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _per_test_entries(results_path: str) -> list[dict]:
    """Object entries of deterministic_scores.json's per_test array.

    Tolerates a null/non-list `per_test` and non-object items (an int item
    would make `"test_id" in test` raise TypeError in the loaders below).
    """
    per_test = _load_det_file(results_path).get("per_test")
    if not isinstance(per_test, list):
        return []
    return [test for test in per_test if isinstance(test, dict)]


def load_deterministic_scores(results_path: str) -> dict[str, float]:
    """Map test_id -> deterministic composite from deterministic_scores.json.

    results.json carries a per-test `score` only when an LLM judge ran. On a
    deterministic-only run those fields are absent, so every consumer that
    reads `score` directly sees 0.0 for every test; this sibling file is the
    only score source on such runs.

    Inconclusive tests carry composite: null; they are excluded so numeric
    comparisons downstream never see None. Returns an empty dict when the
    file is missing or unreadable. test_id must be a string: it becomes a
    dict key here and a set member in load_inconclusive_ids, and an
    unhashable one (list, dict) raised TypeError before any output.
    """
    return {
        test["test_id"]: test["composite"]
        for test in _per_test_entries(results_path)
        if isinstance(test.get("test_id"), str)
        and isinstance(test.get("composite"), (int, float))
    }


def load_inconclusive_ids(results_path: str) -> set[str]:
    """Set of test_ids marked inconclusive in deterministic_scores.json.

    score_execution.py emits `status: "inconclusive"` with `composite: null`
    for tests with no execution evidence, and `status: "score_error"` when the
    scorer itself raised — an internal exception measured nothing either, so
    both statuses belong here. load_deterministic_scores drops
    them from the score map, which made them indistinguishable from "never
    scored deterministically": on a deterministic-only run they then fell
    back to `score = 0.0`, dragging avg/FAIL counts and (on an
    all-inconclusive run) misrouting triage into criteria_bug.
    """
    return {
        test["test_id"]
        for test in _per_test_entries(results_path)
        if isinstance(test.get("test_id"), str)
        and (
            test.get("status") in ("inconclusive", "score_error")
            or not isinstance(test.get("composite"), (int, float))
        )
    }


# ---------------------------------------------------------------------------
# Side-effecting bash command patterns
# ---------------------------------------------------------------------------
# Shared by side_effect_guard.py (which sandboxes these during eval runs and
# attaches simulated responses) and validate_eval_criteria.py (which flags
# them in runner_context as hygiene findings). Each entry: (regex_source,
# human_label). Consumer-specific metadata (simulated responses, compile
# flags, extra patterns like SETUP: blocks) stays in each consumer.

# Source-control / publishing mutations.
GIT_MUTATING_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\bgit\s+push\b", "git push"),
    (r"\bgit\s+push\s+--force\b", "git push --force"),
    (r"\bgh\s+pr\s+create\b", "gh pr create"),
    (r"\bgh\s+pr\s+merge\b", "gh pr merge"),
    # Publishing a draft PR notifies reviewers and starts CI, and `gh pr ready`
    # is how the local pipeline skills do it — more often than `gh pr create`.
    # The sandbox block is a closed enumeration ("do not execute any of the
    # following"), so a publishing command missing from it reads to the
    # executor as permission to run it for real.
    (r"\bgh\s+pr\s+ready\b", "gh pr ready"),
    (r"\bgh\s+pr\s+edit\b", "gh pr edit"),
    (r"\bgh\s+pr\s+comment\b", "gh pr comment"),
    (r"\bgit\s+commit\b", "git commit"),
]

# Filesystem-mutating commands — flagged so eval criteria never actually
# create scripts or files during a test run. These are the shapes that
# showed up in SETUP: blocks and caused flaky eval state.
FS_MUTATING_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\bmkdir\s+(-p\s+)?[^\s]+", "mkdir"),
    # [^|\n] keeps the match on a single line: an unrestricted [^|]* spans
    # newlines and false-positives on a bare `echo`/`printf` mention followed
    # by any later ">" (e.g. an "->" arrow) anywhere in the document.
    (r"\bprintf\s+[^|\n]*>[>\s]*[^\s\n]+", "printf > file"),
    (r"\becho\s+[^|\n]*>[>\s]*[^\s\n]+", "echo > file"),
    (r"\bcp\s+[^\s]+\s+[^\s]+", "cp"),
]

# Destructive commands — the blast-radius group. Unlike the creation shapes
# above, these cannot be undone by deleting a stray file afterwards, so an
# unattended eval of a skill whose job is deletion (a cleanup skill, a branch
# pruner) has to be sandboxed or it removes the operator's real data. The
# guard previously carried no deletion pattern at all, so exactly those skills
# got an empty sandbox.
DESTRUCTIVE_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(?:-[a-zA-Z]+\s+)*[^\s]+", "rm"),
    (r"\btrash\s+(?:-[a-zA-Z]+\s+)*[^\s]+", "trash"),
    (r"\bfind\s+[^\n]*\s-delete\b", "find -delete"),
    # find -exec rm/trash is the same deletion with an extra hop; the -delete
    # pattern above does not cover it because the verb moves into -exec.
    (r"\bfind\s+[^\n]*-exec\s+(?:rm|trash)\b", "find -exec rm"),
    (r"\bmv\s+[^\s]+\s+[^\s]+", "mv"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+branch\s+-D\b", "git branch -D"),
    (r"\bgit\s+checkout\s+\.(?:\s|$)", "git checkout ."),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[fd]", "git clean -fd"),
]

# Network writes. A POST/PUT/PATCH/DELETE from an unattended eval reaches a
# real endpoint (an API, a chat webhook, a package registry) and is not
# recoverable from the local filesystem, so it belongs in the same group as
# deletions rather than with the read-only fetches, which are left alone.
NETWORK_WRITE_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\bcurl\s+[^\n]*-X\s*(?:POST|PUT|PATCH|DELETE)\b", "curl -X POST"),
    (r"\bcurl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|\s-d\s)", "curl --data"),
    (r"\bgh\s+api\s+[^\n]*-(?:X|-method)\s*(?:POST|PUT|PATCH|DELETE)\b", "gh api -X POST"),
    (r"\bwget\s+[^\n]*--post-(?:data|file)\b", "wget --post-data"),
]

# Full ordered set used by side_effect_guard.py (order determines the
# sandbox-context listing order). validate_eval_criteria.py's runner_context
# hygiene check deliberately consumes only FS_MUTATING_BASH_PATTERNS: the two
# groups above describe what the artifact under test may do, not what a
# criteria author accidentally wrote into a SETUP: block.
BASH_SIDE_EFFECT_PATTERNS: list[tuple[str, str]] = (
    GIT_MUTATING_BASH_PATTERNS
    + FS_MUTATING_BASH_PATTERNS
    + DESTRUCTIVE_BASH_PATTERNS
    + NETWORK_WRITE_BASH_PATTERNS
)

# Runner-context header side_effect_guard.py appends when sandboxing side
# effects. validate_eval_criteria.py's hygiene check skips everything after
# this header: the sandbox block itself names the commands it simulates
# ("cp → simulate: ..."), which would otherwise draw a fixable
# runner_context_side_effect finding against the guard's own output on
# every criteria-reuse run.
SANDBOX_HEADER = "SAFETY SANDBOX — side-effect simulation mode"


# ---------------------------------------------------------------------------
# Delegation-shaped slash-invocation detection
# ---------------------------------------------------------------------------
# Shared by side_effect_guard.py (fail-closed sandboxing of unknown
# delegations) and validate_eval_criteria.py (missing_skill_tool audit).
# These were previously two separate regexes that disagreed on identical
# prompts: "Run /forge." and "`/forge`" sandboxed but never drew the
# missing-Skill-tool repair. The pattern matches a delegation-shaped token
# (line start / whitespace / backtick / bracket / paren before the slash, no
# second slash after the name) so file paths like /tmp/x or factor/face
# never fire; the stoplist drops bare filesystem path heads.

DELEGATION_RE = re.compile(
    r"(?:^|[\s`(\[])/([a-z][a-z0-9-]{2,})\b(?!/)", re.MULTILINE
)
DELEGATION_STOPLIST = frozenset(
    {"tmp", "usr", "bin", "etc", "var", "opt", "dev", "home", "private", "users"}
)


def find_slash_invocations(text: str) -> list[str]:
    """Names of delegation-shaped /slash-commands in text.

    Stoplist-filtered, deduplicated, in order of first appearance.
    """
    names: list[str] = []
    for match in DELEGATION_RE.finditer(text):
        name = match.group(1)
        if name not in DELEGATION_STOPLIST and name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# YAML frontmatter extraction
# ---------------------------------------------------------------------------

# \A anchors to the absolute start of file (not just line start) to avoid
# matching horizontal rules or --- inside code blocks mid-document. The
# closing delimiter must be a bare --- line (trailing whitespace ok) or the
# delimiter at EOF — a file ending exactly at `---` previously failed to
# parse in side_effect_guard, silently disabling the allowed-tools filter.
# \r? before each \n accepts CRLF line endings; without it a well-formed
# CRLF document failed to parse, with the same silent-disable consequence.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)

_BLOCK_SCALAR_INDICATOR_RE = re.compile(r"[|>][+-]?\d*\Z")


def match_frontmatter(content: str) -> re.Match | None:
    """Match the leading YAML frontmatter block; group(1) is its inner text.

    Returns None when the document has no frontmatter. Callers that need the
    body offset can use .end(); callers that only need the inner text can
    use split_frontmatter below.
    """
    return _FRONTMATTER_RE.match(content)


def split_frontmatter(content: str) -> str | None:
    """Inner text of the leading YAML frontmatter block, or None."""
    m = match_frontmatter(content)
    return m.group(1) if m else None


def frontmatter_field(frontmatter: str, name: str) -> str | None:
    """Extract a top-level field's value from frontmatter text (no YAML dep).

    Handles the shapes that appear in real artifacts:
      - inline scalar / flow list:  `name: value`  -> "value" (stripped)
      - block scalar:               `name: |` / `name: >` (with chomping /
        indent indicators) followed by indented lines -> dedented lines
        joined by newlines
      - bare key + indented block:  `name:` followed by an indented block
        (e.g. a `- item` list) -> dedented block lines joined by newlines

    Returns None when the field is absent, or when the key is present with
    neither an inline value nor an indented block.
    """
    field_re = re.compile(
        rf"^{re.escape(name)}:[ \t]*(.*)$", re.MULTILINE | re.IGNORECASE
    )
    m = field_re.search(frontmatter)
    if m is None:
        return None
    rest = m.group(1).strip()
    is_block_scalar = bool(rest) and bool(_BLOCK_SCALAR_INDICATOR_RE.fullmatch(rest))
    if rest and not is_block_scalar:
        return rest

    # Collect the indented block following the field line; blank lines are
    # allowed inside, and the block ends at the first non-indented line.
    block_lines: list[str] = []
    for line in frontmatter[m.end():].split("\n")[1:]:
        if line.strip() == "":
            block_lines.append("")
            continue
        if line[0] in " \t":
            block_lines.append(line)
            continue
        break
    while block_lines and block_lines[-1] == "":
        block_lines.pop()
    if not block_lines:
        return None

    indent = min(
        len(line) - len(line.lstrip()) for line in block_lines if line.strip()
    )
    return "\n".join(line[indent:] if line.strip() else "" for line in block_lines)
