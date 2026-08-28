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

This script additionally separates two outcomes hone reports as one. A run that
exhausts its rounds with blocking findings still open is `capped`, not
`converged`, and must not be presented as success.

Ledger shape:
  {"artifact": str,
   "max_rounds": int,
   "rounds": [{"round": int,
               "findings": [{"id": str, "severity": "critical"|"major"|"minor",
                             "file": str, "summary": str,
                             "status": "open"|"fixed"|"rejected"}]}]}

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
DEFAULT_REOPEN_LIMIT = 2

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
        f for f in round_entry.get("findings", [])
        if isinstance(f, dict) and f.get("status") == "open"
    ]


def _blocking(findings: list[dict]) -> list[dict]:
    return [f for f in findings if (f.get("severity") or "").lower() in BLOCKING]


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


def analyze(ledger: dict, recurrence_limit: int, stall_limit: int,
            reopen_limit: int = DEFAULT_REOPEN_LIMIT) -> dict:
    rounds = [r for r in ledger.get("rounds", []) if isinstance(r, dict)]
    rounds.sort(key=lambda r: _as_int(r.get("round"), 0))
    reasons: list[str] = []

    if not rounds:
        # Every key the normal return carries, so a caller reading the --json
        # contract -- `max_rounds` is the documented way to tell `capped` from
        # `in_progress` -- does not hit KeyError on a freshly created ledger.
        return {
            "verdict": "in_progress", "rounds_run": 0,
            "max_rounds": _as_int(ledger.get("max_rounds")), "reasons": [],
            "open_blocking": [], "open_minor_count": 0,
            "recurring": [], "reopened": [], "relocations": [],
            "blocking_counts": [],
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
    reopens: dict[str, int] = {}
    fixed_since_open: set[str] = set()
    stuck: list[str] = []
    reopened: list[str] = []
    for round_entry in rounds:
        statuses = {
            f.get("id"): f.get("status")
            for f in round_entry.get("findings", [])
            if isinstance(f, dict) and f.get("id")
        }
        open_ids = {fid for fid, status in statuses.items() if status == "open"}
        for finding_id in list(streaks):
            if finding_id not in open_ids:
                streaks[finding_id] = 0
        for finding_id in open_ids:
            streaks[finding_id] = streaks.get(finding_id, 0) + 1
            if finding_id in fixed_since_open:
                fixed_since_open.discard(finding_id)
                reopens[finding_id] = reopens.get(finding_id, 0) + 1
            if streaks[finding_id] >= recurrence_limit and finding_id not in stuck:
                stuck.append(finding_id)
            if reopens.get(finding_id, 0) >= reopen_limit and finding_id not in reopened:
                reopened.append(finding_id)
        for finding_id, status in statuses.items():
            if status == "fixed":
                fixed_since_open.add(finding_id)
    # A streak escalates only while the finding is still open: see the note on
    # `last_open_ids` above. `reopened` is deliberately not filtered the same
    # way. A streak that ends in a fix is healed, but the alternating shape is
    # a ledger that has already recorded `fixed` twice and been wrong twice, so
    # a third `fixed` in the latest round is exactly the record this check
    # exists to distrust. Filtering it would delete the check.
    stuck = [finding_id for finding_id in stuck if finding_id in last_open_ids]
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

    # Stalled: blocking count held still across the trailing window.
    blocking_counts = [len(_blocking(_open_findings(r))) for r in rounds]
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
    relocations: list[dict] = []
    # One relocation still open across three rounds is one relocation, not
    # three: the reason line counts entries, so without this it read as
    # "3 finding(s) closed in one file reopened in another".
    seen_relocations: set[tuple[str, str, str]] = set()
    closed_signatures: dict[str, str] = {}
    for round_entry in rounds:
        for finding in round_entry.get("findings", []):
            if not isinstance(finding, dict):
                continue
            signature = _signature(finding.get("summary", ""))
            if not signature:
                continue
            status = finding.get("status")
            file_path = finding.get("file", "")
            if (
                status == "open"
                and signature in closed_signatures
                and signature in last_open_signatures
            ):
                origin = closed_signatures[signature]
                if origin != file_path and (
                    signature, origin, file_path
                ) not in seen_relocations:
                    seen_relocations.add((signature, origin, file_path))
                    relocations.append({
                        "signature_sample": (finding.get("summary") or "")[:100],
                        "from": origin, "to": file_path,
                        "round": round_entry.get("round"),
                    })
            if status == "fixed":
                closed_signatures[signature] = file_path
    if relocations:
        reasons.append(
            f"{len(relocations)} finding(s) closed in one file reopened in another"
        )

    open_blocking = _blocking(last_open)
    open_minor = len(last_open) - len(open_blocking)

    max_rounds = _as_int(ledger.get("max_rounds"))
    rounds_exhausted = bool(max_rounds) and len(rounds) >= max_rounds

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
        "rounds_run": len(rounds),
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

    report = analyze(ledger, args.recurrence_limit, args.stall_limit,
                     args.reopen_limit)
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
