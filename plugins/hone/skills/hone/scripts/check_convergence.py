#!/usr/bin/env python3
"""Check convergence and escalation in a hone findings ledger.

Escalate when blocking findings recur, the blocking count stalls, or a finding
moves between files. Resolved findings stop contributing to streaks and
relocations. Repeated fixed-to-open transitions remain suspect within a
trailing window, even after another reported fix. This follows the escalation
contract in trailofbits/skills skill-improver.

The artifact ledger at ~/skill-eval/{name}/findings-ledger.json preserves
findings, rejections, and verdicts across invocations:
  {"artifact": str,
   "max_rounds": int,
   "rounds": [{"round": int,
               "run": str,
               "findings": [{"id": str, "severity": "critical"|"major"|"minor",
                             "file": str, "summary": str,
                             "status": "open"|"fixed"|"rejected"}]}]}

Use one stable run id per invocation; round numbers restart at 1 and
max_rounds budgets that invocation. Missing run ids merge into the current
segment; without any ids, the whole ledger counts as one run and may cap
early. analyze reports this degradation through run_scoping. Repeated round
numbers denote compaction re-appends, so only their latest entry counts.

Signal scopes:
  current run: live findings, open streaks, stall and relocation checks,
               rounds_run, blocking_counts, and the round budget.
  cross-run: fixed-to-open transitions within DEFAULT_REOPEN_WINDOW_ROUNDS.
  history: severity_by_id lookup, total_rounds_logged, and runs_logged.

Within a run, only fixed or rejected closes a finding; omission leaves it
open. Each new run must restate carried findings. Reopen identity combines
id and summary signature because ids such as F1 restart per run; ids unique
to the artifact avoid ambiguity.

Read verdict from --json to control the loop:
  converged: no blocking work remains and no escalation reason applies.
  in_progress: rounds remain; continue.
  capped: budget exhausted with blocking work; use the human confirmation gate.
  escalate: a non-converging pattern requires a decision.

Exit codes: 0 converged; 1 in_progress, capped, or escalate; 2 usage error.
Exit 1 requires reading the verdict, not halting unconditionally.
Stdlib only; read-only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

BLOCKING = ("critical", "major")

# The CLI validates these vocabularies; library analysis normalizes case.
SEVERITIES = ("critical", "major", "minor")
STATUSES = ("open", "fixed", "rejected")

# Escalate after four consecutive open rounds, one above the default budget
# (3). Default budget exhaustion therefore reaches capped and its human gate;
# a longer run escalates once it demonstrates continued failure to converge.
# open_streaks reports shorter streaks without halting.
DEFAULT_RECURRENCE_LIMIT = 4

# Escalate after two fixed-to-open transitions. One reopening may be an
# incomplete fix; two indicate recurrence. The five-round sequence can span
# invocations of the default three-round run. Report counts below this limit.
DEFAULT_REOPEN_LIMIT = 2

# Count reopenings within ten trailing rounds so old failures expire. The
# window spans the five-round alternation needed for escalation across runs.
DEFAULT_REOPEN_WINDOW_ROUNDS = 10

# Require four flat rounds, one above the default budget, so ordinary budget
# exhaustion reports capped. blocking_counts also reports shorter windows.
DEFAULT_STALL_LIMIT = 4

WORD = re.compile(r"[a-z0-9]+")


def _signature(summary: str) -> str:
    """File-independent identity for a finding, used to spot relocation."""
    words = sorted(set(WORD.findall((summary or "").lower())))
    return " ".join(words)


def _reopen_key(finding: dict) -> str:
    """Pair id and summary signature to track reopenings across runs.

    Ids such as F1 restart per run. Pairing them with summary signatures avoids
    counting unrelated findings as repeated failures while retaining cross-run
    tracking. Rewording a summary can undercount reopenings; this is preferable
    to falsely escalating a healthy run.
    """
    signature = _signature(finding.get("summary", ""))
    return f"{finding.get('id') or ''}\u0000{signature}"


def _status(finding: dict) -> str:
    """Normalize status case and whitespace, matching severity handling.

    The CLI rejects unknown values; library analysis remains tolerant.
    """
    return (finding.get("status") or "").strip().lower()


def _severity(finding: dict) -> str:
    """A finding's severity, folded the same way as `_status`."""
    return (finding.get("severity") or "").strip().lower()


def _open_findings(round_entry: dict) -> list[dict]:
    return [
        f for f in _as_list(round_entry.get("findings"))
        if isinstance(f, dict) and _status(f) == "open"
    ]


def _live_findings(round_entries: list[dict]) -> list[dict]:
    """Return findings whose latest record in this run is open.

    Only fixed or rejected closes a finding. Omission within the run leaves it
    live; a new run must explicitly restate findings it carries forward.
    For findings without ids, use only the final round to avoid double-counting
    unmatchable restatements.
    """
    latest: dict[str, dict] = {}
    for round_entry in round_entries:
        for finding in _as_list(round_entry.get("findings")):
            if isinstance(finding, dict) and finding.get("id"):
                latest[finding["id"]] = finding
    live = [f for f in latest.values() if _status(f) == "open"]
    if round_entries:
        live += [
            f for f in _open_findings(round_entries[-1]) if not f.get("id")
        ]
    return live


def _blocking(findings: list[dict]) -> list[dict]:
    return [f for f in findings if _severity(f) in BLOCKING]


def _as_list(value: object) -> list:
    """Return a list or [] for malformed ledger arrays.

    The CLI rejects invalid rounds; library analysis tolerates malformed entries.
    """
    return value if isinstance(value, list) else []


def _as_int(value: object, default: int | None = None) -> int | None:
    """Coerce ledger numbers, returning default for unsupported values.

    Accept numeric strings but exclude bool, which subclasses int in Python.
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
    """Group rounds by explicit run id, preserving append order.

    A missing id continues the current segment; no ids means one segment.
    Repeated round numbers are compaction re-appends handled by _dedupe_rounds,
    so they cannot also mark run boundaries.

    Missing boundaries may overcount rounds and cap early, which reaches the
    human confirmation gate. Inferring boundaries could reset the budget and
    let a run exceed its cap. analyze reports absent run scoping to callers.
    """
    explicit = any(entry.get("run") is not None for entry in rounds)
    segments: list[list[dict]] = []
    previous_run: object = None
    for entry in rounds:
        run_id = entry.get("run")
        boundary = False
        if explicit and run_id is not None:
            boundary = bool(segments) and run_id != previous_run
            previous_run = run_id
        if boundary or not segments:
            segments.append([])
        segments[-1].append(entry)
    return segments


def _dedupe_rounds(segment: list[dict]) -> list[dict]:
    """Keep the latest append for each valid round number.

    Compaction recovery appends a redone round. Count it once for budgets and
    stall windows. The segment is sorted by round number; preserve entries
    with unusable numbers because they have no identity to deduplicate.
    """
    position_of: dict[int, int] = {}
    kept: list[dict] = []
    for entry in segment:
        number = _round_number(entry)
        if number <= 0:
            kept.append(entry)
        elif number in position_of:
            kept[position_of[number]] = entry
        else:
            position_of[number] = len(kept)
            kept.append(entry)
    return kept


def analyze(ledger: dict, recurrence_limit: int, stall_limit: int,
            reopen_limit: int = DEFAULT_REOPEN_LIMIT,
            reopen_window: int = DEFAULT_REOPEN_WINDOW_ROUNDS) -> dict:
    logged = [r for r in _as_list(ledger.get("rounds")) if isinstance(r, dict)]
    # Sort within each run so restarted round numbers never interleave runs.
    segments = [
        _dedupe_rounds(sorted(seg, key=_round_number))
        for seg in _run_segments(logged)
    ]
    rounds = [entry for segment in segments for entry in segment]
    current_run = segments[-1] if segments else []
    # Report missing run ids: merged runs can overcount rounds and cap early.
    run_scoping = "explicit" if any(
        entry.get("run") is not None for entry in logged
    ) else "absent"
    reasons: list[str] = []

    if not rounds:
        # Keep the full response schema for an empty ledger.
        return {
            "verdict": "in_progress", "rounds_run": 0,
            "total_rounds_logged": 0, "runs_logged": 0,
            "max_rounds": _as_int(ledger.get("max_rounds")), "reasons": [],
            "run_scoping": run_scoping,
            "open_blocking": [], "open_minor_count": 0,
            "recurring": [], "reopened": [], "reopen_counts": {},
            "open_streaks": {},
            "relocations": [], "blocking_counts": [],
        }

    # Use current live findings for verdicts, streaks, and relocations. Explicit
    # closes clear these signals; omitted findings remain open (see _live_findings).
    last_open = _live_findings(current_run)
    last_open_ids = {f.get("id") for f in last_open if f.get("id")}
    last_open_signatures = {
        _signature(f.get("summary", "")) for f in last_open
    } - {""}

    # Track consecutive open rounds and fixed-to-open transitions separately.
    # An omitted finding resets its streak but does not count as a fix.
    streaks: dict[str, int] = {}
    # Record transition positions by id and summary signature so the cross-run
    # window can expire them without combining unrelated reused ids.
    reopen_positions: dict[str, list[int]] = {}
    # Reopen key -> display id.
    reopen_key_id: dict[str, str] = {}
    fixed_since_open: set[str] = set()
    stuck: list[str] = []
    # Latest recorded severity for each id.
    severity_by_id: dict[str, str] = {}
    # Reset streaks at the current run boundary; reopenings retain cross-run history.
    run_start = len(rounds) - len(current_run)
    for position, round_entry in enumerate(rounds):
        if position == run_start:
            streaks.clear()
            del stuck[:]
        statuses: dict[str, str] = {}
        keys: dict[str, str] = {}
        for f in _as_list(round_entry.get("findings")):
            if isinstance(f, dict) and f.get("id"):
                statuses[f["id"]] = _status(f)
                keys[f["id"]] = _reopen_key(f)
                severity_by_id[f["id"]] = _severity(f)
        open_ids = {fid for fid, status in statuses.items() if status == "open"}
        for finding_id in list(streaks):
            if finding_id not in open_ids:
                streaks[finding_id] = 0
        for finding_id in open_ids:
            streaks[finding_id] = streaks.get(finding_id, 0) + 1
            key = keys[finding_id]
            if key in fixed_since_open:
                fixed_since_open.discard(key)
                reopen_positions.setdefault(key, []).append(position)
                reopen_key_id[key] = finding_id
            if streaks[finding_id] >= recurrence_limit and finding_id not in stuck:
                stuck.append(finding_id)
        for finding_id, status in statuses.items():
            if status == "fixed":
                fixed_since_open.add(keys[finding_id])

    def _is_blocking_id(finding_id: str) -> bool:
        return severity_by_id.get(finding_id, "") in BLOCKING

    # Expire transitions outside the trailing rounds window.
    window_start = len(rounds) - max(int(reopen_window or 0), 0)
    # Report the largest count per display id, not the sum: reused ids can refer
    # to unrelated findings with different summary signatures.
    counts_by_key = {
        key: len([p for p in positions if p >= window_start])
        for key, positions in reopen_positions.items()
    }
    reopens: dict[str, int] = {}
    for key, count in counts_by_key.items():
        if not count:
            continue
        finding_id = reopen_key_id.get(key, "")
        reopens[finding_id] = max(reopens.get(finding_id, 0), count)
    reopens = {k: v for k, v in sorted(reopens.items()) if v}
    reopened = sorted(
        finding_id for finding_id, count in reopens.items()
        if count >= reopen_limit
    )

    # Only blocking severities escalate. Streaks require a still-open finding;
    # reopenings remain suspect after another reported fix until the window expires.
    # Report open streaks even below the escalation threshold.
    open_streaks = {
        finding_id: count
        for finding_id, count in sorted(streaks.items())
        if count > 0 and finding_id in last_open_ids and _is_blocking_id(finding_id)
    }

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

    # Check the current run only; inherited counts cannot establish a stall.
    blocking_counts = [len(_blocking(_open_findings(r))) for r in current_run]
    # Require a flat, nonzero window. Rising counts may be newly found work;
    # oscillating counts are covered by recurrence and reopening checks.
    if len(blocking_counts) >= stall_limit:
        window = blocking_counts[-stall_limit:]
        if window[-1] > 0 and len(set(window)) == 1:
            reasons.append(
                f"blocking finding count did not move over the last "
                f"{stall_limit} rounds ({window})"
            )

    # Detect still-live relocations within this run; fixed relocations and prior
    # runs must not keep escalating.
    relocations: list[dict] = []
    # Count a relocation once even when later rounds restate it.
    seen_relocations: set[tuple[str, str, str]] = set()
    # signature -> (origin file, origin finding id, round position of the close)
    closed_signatures: dict[str, tuple[str, str, int]] = {}
    # Track first-open positions to distinguish relocation from concurrent
    # findings with identical wording. The destination must open after the close
    # or in the same round.
    first_open_position: dict[str, int] = {}
    for position, round_entry in enumerate(current_run):
        for finding in _as_list(round_entry.get("findings")):
            if (isinstance(finding, dict) and finding.get("id")
                    and _status(finding) == "open"):
                first_open_position.setdefault(finding["id"], position)
    # Collect closes before testing opens so detection ignores array order,
    # including relocations in the final round.
    for position, round_entry in enumerate(current_run):
        findings = [
            f for f in _as_list(round_entry.get("findings")) if isinstance(f, dict)
        ]
        closed_this_round = {
            _signature(f.get("summary", "")):
                (f.get("file", ""), f.get("id") or "", position)
            for f in findings
            if _status(f) == "fixed" and _signature(f.get("summary", ""))
        }
        origins = {**closed_signatures, **closed_this_round}
        for finding in findings:
            signature = _signature(finding.get("summary", ""))
            if not signature or _status(finding) != "open":
                continue
            # Only blocking findings can trigger relocation escalation.
            if _severity(finding) not in BLOCKING:
                continue
            if signature not in origins or signature not in last_open_signatures:
                continue
            file_path = finding.get("file", "")
            origin, origin_id, closed_at = origins[signature]
            if origin == file_path:
                continue
            destination_id = finding.get("id") or ""
            # A changed file under the same id is a record correction. Missing ids
            # cannot establish relocation.
            if not destination_id or destination_id == origin_id:
                continue
            # A destination already open before the close is a concurrent finding.
            if first_open_position.get(destination_id, position) < closed_at:
                continue
            if (signature, origin, file_path) not in seen_relocations:
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
    # Compare the budget with this run only. The CLI rejects unusable budgets;
    # library callers must inspect max_rounds themselves.
    rounds_exhausted = (
        max_rounds is not None
        and max_rounds >= 1
        and len(current_run) >= max_rounds
    )

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
        # Current-run count for comparison with max_rounds.
        "rounds_run": len(current_run),
        "total_rounds_logged": len(rounds),
        "runs_logged": len(segments),
        "run_scoping": run_scoping,
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
        # Report reopenings below the escalation threshold too.
        "reopen_counts": {k: v for k, v in sorted(reopens.items()) if v},
        # Report each still-open blocking finding's streak, including below threshold.
        "open_streaks": open_streaks,
        "relocations": relocations,
        "blocking_counts": blocking_counts,
    }


def budget_error(ledger: dict) -> str | None:
    """Return the max_rounds error, or None for a usable budget.

    Missing, unparseable, or non-positive budgets disable capping. Reject them,
    including unresolved <max_rounds> placeholders, before CLI analysis.
    """
    if "max_rounds" not in ledger:
        return (
            "ledger has no 'max_rounds'; it is this run's --rounds budget and "
            "'capped' cannot be decided without it"
        )
    raw = ledger["max_rounds"]
    parsed = _as_int(raw)
    if parsed is None:
        return (
            f"ledger 'max_rounds' is not a number: {raw!r}. Resolve the "
            "<max_rounds> placeholder to this run's --rounds budget before "
            "writing the ledger"
        )
    if parsed < 1:
        return f"ledger 'max_rounds' must be at least 1, got {parsed}"
    return None


def limit_errors(recurrence_limit: int, reopen_limit: int,
                 reopen_window: int, stall_limit: int) -> list[str]:
    """Return invalid tuning limits for CLI usage errors (exit 2).

    Recurrence, reopen, and stall limits must be positive to avoid immediate
    false escalation or invalid window indexing. The reopen window may be zero
    but cannot be negative.
    """
    errors: list[str] = []
    for name, value, minimum in (
        ("--recurrence-limit", recurrence_limit, 1),
        ("--reopen-limit", reopen_limit, 1),
        ("--stall-limit", stall_limit, 1),
        ("--reopen-window", reopen_window, 0),
    ):
        if value < minimum:
            errors.append(f"{name} must be at least {minimum}, got {value}")
    return errors


def finding_errors(ledger: dict) -> list[str]:
    """Reject status and severity outside their declared vocabularies.

    Normalize case and surrounding whitespace first. Missing or unknown values
    must fail validation rather than hide blocking findings.
    """
    errors: list[str] = []
    for position, round_entry in enumerate(_as_list(ledger.get("rounds"))):
        if not isinstance(round_entry, dict):
            continue
        label = round_entry.get("round", position)
        for finding in _as_list(round_entry.get("findings")):
            if not isinstance(finding, dict):
                continue
            fid = finding.get("id") or "<no id>"
            if _status(finding) not in STATUSES:
                errors.append(
                    f"round {label} finding {fid}: status "
                    f"{finding.get('status')!r} is not one of {list(STATUSES)}"
                )
            if _severity(finding) not in SEVERITIES:
                errors.append(
                    f"round {label} finding {fid}: severity "
                    f"{finding.get('severity')!r} is not one of "
                    f"{list(SEVERITIES)}"
                )
    return errors


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

    # Validate invocation limits before reading the ledger.
    limit_problems = limit_errors(args.recurrence_limit, args.reopen_limit,
                                  args.reopen_window, args.stall_limit)
    if limit_problems:
        for problem in limit_problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        sys.exit(2)

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
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError is a ValueError, so OSError does not catch it.
        print(f"ERROR: ledger is not decodable text: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        # Map directory and permission errors to exit 2; exit 1 requires a verdict.
        print(f"ERROR: cannot read ledger: {exc}", file=sys.stderr)
        sys.exit(2)

    # Require an object before analyze calls .get().
    if not isinstance(ledger, dict):
        print(
            f"ERROR: ledger root must be a JSON object, got "
            f"{type(ledger).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)

    # The CLI requires a rounds array. Library defaults must not disguise a
    # malformed ledger as an empty run.
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

    budget_problem = budget_error(ledger)
    if budget_problem:
        print(f"ERROR: {budget_problem}", file=sys.stderr)
        sys.exit(2)

    bad_findings = finding_errors(ledger)
    if bad_findings:
        print("ERROR: ledger findings use values outside the documented "
              "vocabulary:", file=sys.stderr)
        for problem in bad_findings:
            print(f"  {problem}", file=sys.stderr)
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
            streak = report["open_streaks"].get(finding["id"])
            age = f" (open {streak} round(s))" if streak else ""
            print(f"  OPEN [{finding['severity']}] {finding['file']}: "
                  f"{finding['summary']}{age}")
        if report["verdict"] == "capped":
            print("  NOTE: capped, NOT converged. Do not report this as success.")

    sys.exit(0 if report["verdict"] == "converged" else 1)


if __name__ == "__main__":
    main()
