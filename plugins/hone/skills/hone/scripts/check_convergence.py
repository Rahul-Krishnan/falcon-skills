#!/usr/bin/env python3
"""Convergence and escalation check over a hone findings ledger.

Phase 3 loops back to Phase 2 "if rounds remain and score is improving". That
rule cannot see three failure shapes, all of which burn the round budget while
looking like progress:

  recurring    The same finding reopens round after round. Each round "fixes"
               it and the next round finds it again.
  stalled      The blocking-finding count stops falling. Work continues, the
               number does not move.
  relocated    A finding is closed in one file and an equivalent one opens in
               another. The count looks flat or better; the problem moved.

Any of these means the run needs a decision, not another round, so the honest
action is to stop and say so. This mirrors the escalation contract in
trailofbits/skills skill-improver.

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

# Consecutive rounds the blocking count may fail to fall before it counts as
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


def analyze(ledger: dict, recurrence_limit: int, stall_limit: int) -> dict:
    rounds = [r for r in ledger.get("rounds", []) if isinstance(r, dict)]
    rounds.sort(key=lambda r: r.get("round", 0))
    reasons: list[str] = []

    if not rounds:
        return {
            "verdict": "in_progress", "rounds_run": 0, "reasons": [],
            "open_blocking": [], "open_minor_count": 0,
            "recurring": [], "relocations": [], "blocking_counts": [],
        }

    # Recurring: an id open in `recurrence_limit` consecutive rounds.
    streaks: dict[str, int] = {}
    recurring: list[str] = []
    for round_entry in rounds:
        open_ids = {f.get("id") for f in _open_findings(round_entry) if f.get("id")}
        for finding_id in list(streaks):
            if finding_id not in open_ids:
                streaks[finding_id] = 0
        for finding_id in open_ids:
            streaks[finding_id] = streaks.get(finding_id, 0) + 1
            if streaks[finding_id] >= recurrence_limit and finding_id not in recurring:
                recurring.append(finding_id)
    if recurring:
        reasons.append(
            f"finding(s) {sorted(recurring)} stayed open for "
            f"{recurrence_limit}+ consecutive rounds"
        )

    # Stalled: blocking count failed to fall across the trailing window.
    blocking_counts = [len(_blocking(_open_findings(r))) for r in rounds]
    if len(blocking_counts) >= stall_limit:
        window = blocking_counts[-stall_limit:]
        if window[-1] > 0 and window[-1] >= max(window):
            reasons.append(
                f"blocking finding count did not fall over the last "
                f"{stall_limit} rounds ({window})"
            )

    # Relocated: signature closed in one file reappears in another.
    relocations: list[dict] = []
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
            if status == "open" and signature in closed_signatures:
                origin = closed_signatures[signature]
                if origin != file_path:
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

    last_open = _open_findings(rounds[-1])
    open_blocking = _blocking(last_open)
    open_minor = len(last_open) - len(open_blocking)

    max_rounds = ledger.get("max_rounds")
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
        "recurring": sorted(recurring),
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

    report = analyze(ledger, args.recurrence_limit, args.stall_limit)
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
