#!/usr/bin/env python3
"""Convergence and escalation check over a hone findings ledger.

Phase 3 loops back to Phase 2 "if rounds remain and score is improving". That
rule cannot see three failure shapes, all of which burn the round budget while
looking like progress:

  recurring    The same finding stays open round after round, or each round
               "fixes" it and the next round finds it again. Both count: the
               first as a consecutive-open streak, the second as repeated
               fixed-to-open transitions.
  stalled      The blocking-finding count stops moving. Work continues, the
               number does not.
  relocated    A finding is closed in one file and an equivalent one opens in
               another. The count looks flat or better; the problem moved.

Any of these means the run needs a decision, not another round, so the honest
action is to stop and say so, but only while the shape is still live: a
finding that was stuck and has since been fixed is history, and a ledger whose
blocking findings are all resolved has converged whatever it went through to
get there. The exception is the alternating shape, where `fixed` is the record
under suspicion. This mirrors the escalation contract in trailofbits/skills
skill-improver.

The ledger also fixes a smaller thing: hone currently re-derives findings every
run. A ledger on disk carries findings, rejections, and verdicts across runs, so
a resumed run restarts its rounds without re-deriving its history.

Because the ledger is cumulative, every signal here has to say which scope it
reads, and the answer is not the same for all of them:

  run-scoped   `stuck` (the consecutive-open streak), the stall window, the
               relocation trail, `rounds_run`, `blocking_counts`, and the
               `max_rounds` budget. All of these are statements about ONE
               invocation failing to converge, and all of them would otherwise
               fire on a new run's FIRST round purely on inherited history --
               a halt before the run does any work, repeating on every future
               invocation because the history that caused it is permanent.
               SKILL.md's Phase 2 Step 8 requires each round to restate every
               still-open finding, so run 2 round 1 reproduces run 1's final
               open set by construction; a cross-run streak or stall window
               reads that mandated restatement as evidence of a stuck loop.
  cross-run    the reopen counter, which is cross-run BY DESIGN (see
               DEFAULT_REOPEN_LIMIT) and bounded by a trailing rounds window
               instead. It cannot fire on a restatement: restating an
               already-open finding records no fixed->open transition, so it
               takes real churn inside the new run to move.
  whole-ledger `severity_by_id` (a lookup, not a signal: the latest severity
               recorded for an id anywhere) and the `total_rounds_logged` /
               `runs_logged` counters, which are reported as history and feed
               no verdict.

This script additionally separates two outcomes hone reports as one. A run that
exhausts its rounds with blocking findings still open is `capped`, not
`converged`, and must not be presented as success.

Ledger shape:
  {"artifact": str,
   "max_rounds": int,
   "rounds": [{"round": int,
               "run": str,            # optional but strongly preferred
               "findings": [{"id": str, "severity": "critical"|"major"|"minor",
                             "file": str, "summary": str,
                             "status": "open"|"fixed"|"rejected"}]}]}

The ledger is per ARTIFACT, not per run: it lives at
`~/skill-eval/{name}/findings-ledger.json` and every invocation appends to it,
while `round` restarts at 1 and `max_rounds` is a per-run budget. So `rounds`
is a cumulative log holding several runs, and the two facts that follow are
what `run` is for. Give every round the same `run` id for the whole
invocation (any stable string: the RUN_ID the workflow state file already
carries is the obvious one). Without it this module falls back to inferring
run boundaries from repeated round numbers, which is weaker; see
`_run_segments`.

Exit codes: 0 converged, 1 anything else (escalate, capped, or in_progress),
2 usage error.

**The exit code is not the verdict.** `in_progress` -- the ordinary mid-run
state, where rounds remain and the loop should continue -- exits 1 alongside
`escalate` and `capped`, because only `converged` is a finished, successful
run. A caller that branches on the exit code alone reads a healthy round as a
failure and halts the loop. Read `verdict` from the `--json` output and branch
on that; treat exit 1 as "not converged yet", not as an error. Exit 2 is the
only genuine failure (missing or unparseable ledger).

Stdlib only. Read-only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

BLOCKING = ("critical", "major")

# Consecutive rounds a finding may stay open before it counts as recurring.
DEFAULT_RECURRENCE_LIMIT = 3

# Times a finding may be recorded fixed and then found open again before it
# counts as recurring. The consecutive-open streak cannot see this shape at
# all -- the round that records the fix zeroes the streak, so a finding that
# alternates open/fixed forever never reaches DEFAULT_RECURRENCE_LIMIT -- and
# it is the first shape the module docstring names. One reopen is an
# incomplete fix; two is a loop that is not converging on this finding.
#
# Reaching 2 needs open->fixed->open->fixed->open, five rounds, and SKILL.md
# templates max_rounds 3. So this ESCALATES only across runs, not inside one:
# the ledger is the artifact's memory across invocations, and a finding
# reopened twice over two runs is exactly the record worth distrusting.
# Lowering the bar to 1 would fire on the ordinary case where round N's fix is
# incomplete and round N+1 finishes it, halting a run that was converging.
# So that the check still pays off inside a single run, `reopen_counts` is
# reported unconditionally: a run can see "reopened once" as information
# without that becoming a halt.
DEFAULT_REOPEN_LIMIT = 2

# Trailing rounds of ledger history within which a fixed-then-open transition
# still counts toward the reopen bar.
#
# The bar above is deliberately cross-run and the ledger is deliberately
# permanent, which together made the counter permanent: one historical
# alternation escalated every future invocation for that artifact forever,
# with `open_blocking` empty and no recovery short of deleting the file. Old
# churn is history, and history is what the rest of this module already
# refuses to escalate on (see the `last_open_ids` note in `analyze`).
#
# Counted in ROUNDS, not runs: run boundaries are inferred (see
# `_run_segments`), and a bound resting on an inference is weaker than what it
# bounds. Ten exceeds the five rounds a full open->fixed->open->fixed->open
# alternation needs, so the documented cross-run escalation still fires -- at
# roughly three templated runs -- and still ages out.
DEFAULT_REOPEN_WINDOW_ROUNDS = 10

# Consecutive rounds the blocking count may hold still before it counts as
# stalled. Two rounds of no movement is noise; three is a pattern.
DEFAULT_STALL_LIMIT = 3

WORD = re.compile(r"[a-z0-9]+")


def _signature(summary: str) -> str:
    """File-independent identity for a finding, used to spot relocation."""
    words = sorted(set(WORD.findall((summary or "").lower())))
    return " ".join(words)


def _open_findings(round_entry: dict) -> list[dict]:
    return [
        f for f in _as_list(round_entry.get("findings"))
        if isinstance(f, dict) and f.get("status") == "open"
    ]


def _blocking(findings: list[dict]) -> list[dict]:
    return [f for f in findings if (f.get("severity") or "").lower() in BLOCKING]


def _as_list(value: object) -> list:
    """A ledger array, or [] when the executor wrote something else there.

    `_as_int` already exists because executors write scalars where numbers
    belong. They do the same where arrays belong: a truncated or hand-edited
    ledger carrying `"rounds": 3` or `"findings": 7` reached a bare `for`
    and raised TypeError out of analyze(), which main() does not catch, so
    the documented contract for a bad ledger (exit 2) produced a traceback
    instead. main() rejects a non-list `rounds` loudly; this keeps a single
    malformed round from taking the whole analysis down with it.
    """
    return value if isinstance(value, list) else []


def _as_int(value: object, default: int | None = None) -> int | None:
    """Ledger numbers, tolerant of the shapes an executor actually writes.

    The ledger is executor-written JSON, and everything else in this module
    tolerates a wrong type rather than raising. `"round": "1"` or a string
    `max_rounds` used to reach a comparison and raise TypeError out of
    `analyze()`, which `main()` does not catch -- the documented contract for
    a bad ledger is exit 2, not a traceback. `bool` is excluded because
    `True` is an int in Python and is never a round number.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _round_number(round_entry: dict) -> int:
    return _as_int(round_entry.get("round"), 0) or 0


def _run_segments(rounds: list[dict]) -> list[list[dict]]:
    """The ledger's rounds grouped into runs, in append order.

    The ledger is per artifact and permanent, `round` restarts at 1 every
    invocation, and `max_rounds` is a per-run budget. Two bugs followed from
    reading the cumulative array as one run:

      * sorting the whole array by `round` interleaved runs. Run 1's rounds
        1-3 followed by run 2's rounds 1-2 sorted to [1, 1, 2, 2, 3], so the
        "latest round" was run 1's last one. A run with an open blocking
        finding reported `converged` and exited clean.
      * `len(rounds) >= max_rounds` measured every round ever logged against a
        per-run budget, so a 4-round ledger with `max_rounds: 3` returned
        `capped` on the second run's FIRST round, halting before any work.

    An explicit `run` id is the authoritative boundary: consecutive rounds
    sharing an id are one run. When no round carries one, the only evidence
    left is a REPEATED round number, which is what a restart looks like from
    here.

    That heuristic is deliberately narrower than "the round number went
    down". A lone out-of-order append -- `[{"round": 2}, {"round": 1}]` -- is
    one run whose entries need sorting, not two runs, and it is precisely the
    shape the within-segment sort and `_as_int` exist to absorb. A decreasing
    number would split it and lose a round; a repeated number would not.

    The heuristic's real limit is an executor that never restarts its
    numbering, counting 1..N across every invocation: it leaves no boundary
    evidence at all and reads as a single run, which is the old behaviour.
    Nothing can recover a boundary that was never recorded, which is why the
    module docstring asks for `run` rather than treating it as optional
    polish.
    """
    segments: list[list[dict]] = []
    seen_numbers: set[int] = set()
    previous_run: object = None
    for entry in rounds:
        run_id = entry.get("run")
        if run_id is not None:
            boundary = bool(segments) and run_id != previous_run
            previous_run = run_id
        else:
            boundary = _round_number(entry) in seen_numbers
        if boundary or not segments:
            segments.append([])
            seen_numbers = set()
        segments[-1].append(entry)
        seen_numbers.add(_round_number(entry))
    return segments


def analyze(ledger: dict, recurrence_limit: int, stall_limit: int,
            reopen_limit: int = DEFAULT_REOPEN_LIMIT,
            reopen_window: int = DEFAULT_REOPEN_WINDOW_ROUNDS) -> dict:
    logged = [r for r in _as_list(ledger.get("rounds")) if isinstance(r, dict)]
    # Order WITHIN a run, never across runs: the global sort is what
    # interleaved them. See `_run_segments`.
    segments = [sorted(seg, key=_round_number) for seg in _run_segments(logged)]
    rounds = [entry for segment in segments for entry in segment]
    current_run = segments[-1] if segments else []
    reasons: list[str] = []

    if not rounds:
        # Every key the normal return carries, so a caller reading the --json
        # contract -- `max_rounds` is the documented way to tell `capped` from
        # `in_progress` -- does not hit KeyError on a freshly created ledger.
        return {
            "verdict": "in_progress", "rounds_run": 0,
            "total_rounds_logged": 0, "runs_logged": 0,
            "max_rounds": _as_int(ledger.get("max_rounds")), "reasons": [],
            "open_blocking": [], "open_minor_count": 0,
            "recurring": [], "reopened": [], "reopen_counts": {},
            "relocations": [], "blocking_counts": [],
        }

    # The final round's open set decides which escalation reasons are still
    # live. Every reason below is derived from the ledger's whole history, and
    # history does not un-happen, so a reason recorded once used to hold
    # forever: a finding open in rounds 1-3 and fixed in rounds 4-5 kept
    # returning `escalate` with an empty `open_blocking`, and `escalate` means
    # "halt and report a failing convergence gate". A run that fixed what was
    # stuck converged; it did not fail to converge. The stall check already
    # self-heals through its trailing window; this brings the streak and the
    # relocation checks into line with it.
    last_open = _open_findings(rounds[-1])
    last_open_ids = {f.get("id") for f in last_open if f.get("id")}
    last_open_signatures = {
        _signature(f.get("summary", "")) for f in last_open
    } - {""}

    # Recurring, two shapes. `stuck` is an id open in `recurrence_limit`
    # consecutive rounds. `reopened` is an id recorded `fixed` and found open
    # again `reopen_limit` times -- the "each round fixes it and the next round
    # finds it again" shape, which the streak alone cannot see because the
    # round that records the fix resets the streak to zero.
    #
    # Only an explicit `fixed` record counts as a close. A finding simply
    # absent from a round is an unreported round, not a fix, and reading it as
    # one would escalate every ledger that lists open findings only.
    streaks: dict[str, int] = {}
    # Each finding's fixed-then-open transitions, recorded by their position in
    # the ordered history so the window below can age them out.
    reopen_positions: dict[str, list[int]] = {}
    fixed_since_open: set[str] = set()
    stuck: list[str] = []
    # The severity last recorded for an id anywhere in the history. Severity
    # can be restated per round, and the latest record is the current one.
    severity_by_id: dict[str, str] = {}
    # Where the current run begins in `rounds`. The single pass below reads
    # the whole ledger, because the reopen counter and `severity_by_id` want
    # it, but the STREAK is reset here: a consecutive-open streak is a
    # statement about one invocation, and carrying it across a restart made
    # `recurrence_limit` fire on the new run's first round. SKILL.md Phase 2
    # Step 8 has every round restate its still-open findings, so run 2 round 1
    # always repeats run 1's final open set; with a cross-run streak, a
    # finding left open at the end of a 3-round run escalated the next run
    # before it did any work, and every run after that, permanently.
    run_start = len(rounds) - len(current_run)
    for position, round_entry in enumerate(rounds):
        if position == run_start:
            streaks.clear()
            del stuck[:]
        statuses: dict[str, str] = {}
        for f in _as_list(round_entry.get("findings")):
            if isinstance(f, dict) and f.get("id"):
                statuses[f["id"]] = f.get("status")
                severity_by_id[f["id"]] = (f.get("severity") or "").lower()
        open_ids = {fid for fid, status in statuses.items() if status == "open"}
        for finding_id in list(streaks):
            if finding_id not in open_ids:
                streaks[finding_id] = 0
        for finding_id in open_ids:
            streaks[finding_id] = streaks.get(finding_id, 0) + 1
            if finding_id in fixed_since_open:
                fixed_since_open.discard(finding_id)
                reopen_positions.setdefault(finding_id, []).append(position)
            if streaks[finding_id] >= recurrence_limit and finding_id not in stuck:
                stuck.append(finding_id)
        for finding_id, status in statuses.items():
            if status == "fixed":
                fixed_since_open.add(finding_id)

    def _is_blocking_id(finding_id: str) -> bool:
        return severity_by_id.get(finding_id, "") in BLOCKING

    # Only transitions inside the trailing window count. See
    # DEFAULT_REOPEN_WINDOW_ROUNDS: without this the counter never expired and
    # one historical alternation escalated the artifact forever.
    window_start = len(rounds) - max(int(reopen_window or 0), 0)
    reopens = {
        finding_id: len([p for p in positions if p >= window_start])
        for finding_id, positions in reopen_positions.items()
    }
    reopens = {k: v for k, v in sorted(reopens.items()) if v}
    reopened = sorted(
        finding_id for finding_id, count in reopens.items()
        if count >= reopen_limit
    )

    # Every escalation reason is filtered to BLOCKING severities. `reasons`
    # short-circuits to `escalate` ahead of the "nothing blocking open ->
    # converged" branch, so an unfiltered reason let a single MINOR finding
    # open three rounds return `escalate` with `open_blocking: []` -- against
    # both the module docstring ("a ledger whose blocking findings are all
    # resolved has converged") and test_minor_findings_do_not_block_convergence.
    # A known nit left open is not a loop that failed to converge.
    #
    # A streak escalates only while the finding is still open: see the note on
    # `last_open_ids` above. `reopened` is deliberately not filtered that same
    # way. A streak that ends in a fix is healed, but the alternating shape is
    # a ledger that has already recorded `fixed` twice and been wrong twice, so
    # a third `fixed` in the latest round is exactly the record this check
    # exists to distrust. Filtering it by `last_open_ids` would delete the
    # check; bounding it by the window above is what keeps it from being
    # permanent.
    stuck = [
        finding_id for finding_id in stuck
        if finding_id in last_open_ids and _is_blocking_id(finding_id)
    ]
    reopened = [
        finding_id for finding_id in reopened if _is_blocking_id(finding_id)
    ]
    recurring = sorted(set(stuck) | set(reopened))
    if stuck:
        reasons.append(
            f"finding(s) {sorted(stuck)} stayed open for "
            f"{recurrence_limit}+ consecutive rounds"
        )
    if reopened:
        reasons.append(
            f"finding(s) {sorted(reopened)} were recorded fixed and found open "
            f"again {reopen_limit}+ times"
        )

    # Stalled: blocking count held still across the current run's trailing
    # window. Current run, not the whole ledger: "the number stopped moving"
    # is a claim about this invocation, and a window that straddles a restart
    # is mostly previous-run history. Four distinct findings, run 1 ending
    # [2, 2] and run 2 round 1 closing both carry-overs while opening two new
    # ones, gave [2, 2, 2] and escalated on `rounds_run: 1` -- on a round that
    # had just closed two findings. `blocking_counts` is reported at the same
    # scope as `rounds_run` for the same reason; `total_rounds_logged` and
    # `runs_logged` are where the cumulative view lives.
    blocking_counts = [len(_blocking(_open_findings(r))) for r in current_run]
    # Flat and non-zero, which is what the docstring means by "the number does
    # not move". `window[-1] >= max(window)` also fired on a *rising* window,
    # so [0, 0, 1] -- two clean rounds and then a newly found blocking finding
    # -- escalated on that finding's first appearance, before the loop had a
    # single round to fix it. A window that rises and then flattens escalates
    # one round later, and an oscillating count is left to the recurrence and
    # reopen checks, which see the individual finding rather than the total.
    if len(blocking_counts) >= stall_limit:
        window = blocking_counts[-stall_limit:]
        if window[-1] > 0 and len(set(window)) == 1:
            reasons.append(
                f"blocking finding count did not move over the last "
                f"{stall_limit} rounds ({window})"
            )

    # Relocated: signature closed in one file reappears in another, and is
    # still open in the latest round (see `last_open_signatures` above -- a
    # relocation that has since been fixed is history, not an escalation).
    #
    # Read over the CURRENT RUN only, for the same reason as the streak and
    # the stall window. "Closed here, opened there" describes a loop chasing
    # one problem around an artifact, which is a within-invocation shape. Over
    # the whole ledger it also became permanent: a signature recorded `fixed`
    # in run 1 and found open elsewhere in run 2 escalated run 2 on round 1,
    # and since Phase 2 Step 8 restates that still-open finding every
    # subsequent run while run 1's `fixed` record never expires, it escalated
    # every run after that too, with no recovery short of deleting the file.
    relocations: list[dict] = []
    # One relocation still open across three rounds is one relocation, not
    # three: the reason line counts entries, so without this it read as
    # "3 finding(s) closed in one file reopened in another".
    seen_relocations: set[tuple[str, str, str]] = set()
    closed_signatures: dict[str, str] = {}
    # Two passes per round, because one pass populated `closed_signatures` in
    # the same iteration that tested against it: whether a relocation was seen
    # depended on the order of findings WITHIN the round's array. A `fixed`
    # entry for file A listed after the reopened entry for file B was missed
    # that round, and missed permanently when that was the final round. Rounds
    # restate live findings so it usually self-corrected, but not on the last
    # one. Collecting the round's closes first makes detection independent of
    # array order and catches the same-round relocation either way round.
    for round_entry in current_run:
        findings = [
            f for f in _as_list(round_entry.get("findings")) if isinstance(f, dict)
        ]
        closed_this_round = {
            _signature(f.get("summary", "")): f.get("file", "")
            for f in findings
            if f.get("status") == "fixed" and _signature(f.get("summary", ""))
        }
        origins = {**closed_signatures, **closed_this_round}
        for finding in findings:
            signature = _signature(finding.get("summary", ""))
            if not signature or finding.get("status") != "open":
                continue
            # Relocation is an escalation reason, so it is severity-filtered
            # like the others: a minor finding that moved file is not a
            # non-converging loop.
            if (finding.get("severity") or "").lower() not in BLOCKING:
                continue
            if signature not in origins or signature not in last_open_signatures:
                continue
            file_path = finding.get("file", "")
            origin = origins[signature]
            if origin != file_path and (
                signature, origin, file_path
            ) not in seen_relocations:
                seen_relocations.add((signature, origin, file_path))
                relocations.append({
                    "signature_sample": (finding.get("summary") or "")[:100],
                    "from": origin, "to": file_path,
                    "round": round_entry.get("round"),
                })
        closed_signatures.update(closed_this_round)
    if relocations:
        reasons.append(
            f"{len(relocations)} finding(s) closed in one file reopened in another"
        )

    open_blocking = _blocking(last_open)
    open_minor = len(last_open) - len(open_blocking)

    max_rounds = _as_int(ledger.get("max_rounds"))
    # `max_rounds` is the budget for ONE invocation, so it is measured against
    # the current run's rounds, not against every round the artifact has ever
    # logged. Counting the whole ledger returned `capped` on a second run's
    # first round: an immediate `fail` gate and a halt before any work.
    rounds_exhausted = bool(max_rounds) and len(current_run) >= max_rounds

    if reasons:
        verdict = "escalate"
    elif not open_blocking:
        verdict = "converged"
    elif rounds_exhausted:
        verdict = "capped"
    else:
        verdict = "in_progress"

    return {
        "verdict": verdict,
        # Rounds in the CURRENT run, which is what `max_rounds` budgets and
        # what a caller reporting "round N of M" means. The cumulative figures
        # are reported alongside rather than folded in; see the module
        # docstring for which signals read which scope.
        "rounds_run": len(current_run),
        "total_rounds_logged": len(rounds),
        "runs_logged": len(segments),
        "max_rounds": max_rounds,
        "reasons": reasons,
        "open_blocking": [
            {"id": f.get("id"), "severity": f.get("severity"),
             "file": f.get("file"), "summary": (f.get("summary") or "")[:120]}
            for f in open_blocking
        ],
        "open_minor_count": open_minor,
        "recurring": recurring,
        "reopened": sorted(reopened),
        # Every finding that has come back at least once, whether or not it
        # reached the escalation bar. Below the bar this is the only place the
        # reopen signal is visible at all.
        "reopen_counts": {k: v for k, v in sorted(reopens.items()) if v},
        "relocations": relocations,
        "blocking_counts": blocking_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect non-convergence in a hone findings ledger"
    )
    parser.add_argument("ledger_file", help="Path to the findings ledger JSON")
    parser.add_argument("--recurrence-limit", type=int, default=DEFAULT_RECURRENCE_LIMIT,
                        help=f"Consecutive open rounds before escalating "
                             f"(default: {DEFAULT_RECURRENCE_LIMIT})")
    parser.add_argument("--reopen-limit", type=int, default=DEFAULT_REOPEN_LIMIT,
                        help=f"Fixed-then-reopened cycles before escalating "
                             f"(default: {DEFAULT_REOPEN_LIMIT})")
    parser.add_argument("--reopen-window", type=int,
                        default=DEFAULT_REOPEN_WINDOW_ROUNDS,
                        help=f"Trailing rounds within which a reopen still "
                             f"counts (default: {DEFAULT_REOPEN_WINDOW_ROUNDS})")
    parser.add_argument("--stall-limit", type=int, default=DEFAULT_STALL_LIMIT,
                        help=f"Rounds without a falling blocking count before "
                             f"escalating (default: {DEFAULT_STALL_LIMIT})")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    try:
        with open(args.ledger_file) as handle:
            ledger = json.load(handle)
    except FileNotFoundError:
        print(f"ERROR: ledger not found: {args.ledger_file}; Phase 2 writes it "
              "on the first round", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"ERROR: ledger is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)

    # A bare array of rounds -- the shape SKILL.md warns against writing --
    # reaches `.get()` in analyze() and raises an uncaught AttributeError; the
    # contract here is exit 2.
    if not isinstance(ledger, dict):
        print(
            f"ERROR: ledger root must be a JSON object, got "
            f"{type(ledger).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)

    # `rounds` carrying a scalar is the same class of mistake as a bare array
    # at the root, and analyze() now tolerates it by reading zero rounds. That
    # is the right default for a library call and the wrong one for the CLI:
    # "no rounds yet" and "your ledger is malformed" are different answers and
    # both would print `in_progress`. Fail loudly here instead.
    # An absent `rounds` is the same class of mistake as a scalar one, and it
    # was the silent one: `{"artifact": "x", "findings": [...]}` -- findings at
    # the top level, exactly the shape SKILL.md warns against -- read as zero
    # rounds and printed `in_progress` with `rounds_run: 0`, indistinguishable
    # from a healthy first round. The convergence check was then disabled for
    # that artifact forever, with nothing to notice. Prose in SKILL.md is not a
    # substitute for the script detecting it.
    if "rounds" not in ledger:
        print(
            "ERROR: ledger has no 'rounds' array; Phase 2 writes one on the "
            "first round. Findings belong inside a round entry, not at the "
            "top level.",
            file=sys.stderr,
        )
        sys.exit(2)

    if not isinstance(ledger["rounds"], list):
        print(
            f"ERROR: ledger 'rounds' must be a JSON array, got "
            f"{type(ledger['rounds']).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)

    report = analyze(ledger, args.recurrence_limit, args.stall_limit,
                     args.reopen_limit, args.reopen_window)
    report["artifact"] = ledger.get("artifact")

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"VERDICT: {report['verdict']} after {report['rounds_run']} round(s)")
        print(f"  blocking counts by round: {report['blocking_counts']}")
        print(f"  open blocking: {len(report['open_blocking'])}, "
              f"open minor: {report['open_minor_count']}")
        for reason in report["reasons"]:
            print(f"  ESCALATE: {reason}")
        for finding in report["open_blocking"]:
            print(f"  OPEN [{finding['severity']}] {finding['file']}: {finding['summary']}")
        if report["verdict"] == "capped":
            print("  NOTE: capped, NOT converged. Do not report this as success.")

    sys.exit(0 if report["verdict"] == "converged" else 1)


if __name__ == "__main__":
    main()
