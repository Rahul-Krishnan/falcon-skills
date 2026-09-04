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

  run-scoped   the still-live open set (which decides the verdict and
               `open_blocking`), `stuck` (the consecutive-open streak), the
               stall window, the relocation trail, `rounds_run`,
               `blocking_counts`, and the `max_rounds` budget. All of these
               are statements about ONE
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

Everything counted in rounds counts ROUNDS, not ledger entries. Phase 2's
compaction-recovery protocol has the executor append rather than overwrite, so
a round redone after a compaction is logged twice; `_dedupe_rounds` keeps the
latest append of each round number so the duplicate does not spend the budget
or flatten the stall window.

A finding is closed only by an explicit `fixed` or `rejected` record. A round
that omits it is an unreported round, which is why the verdict reads the run's
live set (`_live_findings`) rather than the last round's `findings` array.

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
carries is the obvious one). It is the ONLY run boundary this module reads:
without it the whole ledger is one run, `rounds_run` counts every round ever
logged for the artifact, and `capped` therefore arrives early. There is no
fallback inference, because the only candidate signal -- a repeated round
number -- is already spoken for by the compaction re-append the ledger format
mandates; see `_run_segments`. `analyze` reports `run_scoping` so a caller can
tell a run-scoped verdict from a degraded whole-ledger one.

Finding ids are scoped the same way and need the same care. `id` is what the
cross-run reopen counter pairs on, and SKILL.md's Phase 2 Step 8 template
starts each run's findings at `F1`, so ids alone do not identify a finding
across runs. This module pairs `id` WITH the finding's summary signature for
that reason (see `_reopen_key`); ids that are unique per artifact rather than
per run make the pairing exact instead of merely safe.

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

# The closed vocabularies the ledger shape in the module docstring declares.
# `main()` rejects anything outside them; `analyze()` reads them through the
# normalizers below so a case variant is a match rather than a silent miss.
SEVERITIES = ("critical", "major", "minor")
STATUSES = ("open", "fixed", "rejected")

# Consecutive rounds a finding may stay open before it counts as recurring.
#
# Four, which is ONE ABOVE the `max_rounds: 3` that SKILL.md and
# phase2-improvement.md template. That relationship is the point, not the
# number. At three the bar coincided exactly with the budget, so the two
# ordinary ways a run runs out of road -- one finding open every round, and a
# rotating find/fix cycle holding the blocking count flat -- both tripped the
# escalation bars on the same round the budget expired. `reasons` short-
# circuits to `escalate` ahead of `capped`, so both reported `escalate`, and
# `capped` was left needing an oscillating blocking count. That mattered
# because phase3-reevaluation.md routes `capped` to the `--confirm` human gate
# and routes `escalate` deliberately away from it: the gate meant to catch
# "budget ran out with work outstanding" was unreachable in both cases where
# it applied.
#
# Sitting one above the budget separates the two verdicts by meaning rather
# than by coincidence, and costs nothing: a default run halts on round 3
# either way, the label changes from `escalate` to `capped`. Escalation then
# means what it says -- the loop is not converging even with budget to spend.
# It fires on round 4 of a `--rounds 6` run, and on the round after a
# `--confirm` grant of more rounds, which is exactly when "more rounds is the
# wrong remedy" has been demonstrated rather than assumed, and which the same
# reference says gets no second ask.
#
# Nothing is lost below the bar: `open_streaks` reports every consecutive-open
# count unconditionally, the way `reopen_counts` already does below its own
# bar, so a run can see "open for 3 rounds" as information without that being
# a halt.
DEFAULT_RECURRENCE_LIMIT = 4

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
# stalled. Two rounds of no movement is noise; three is a pattern -- but the
# bar sits one above the templated `max_rounds` for the same reason
# DEFAULT_RECURRENCE_LIMIT does, and for the same verdict. A flat count that
# lasts exactly as long as the budget is a run that ran out of rounds
# (`capped`, which reaches the human gate), not a run that proved it was going
# nowhere. `blocking_counts` is reported unconditionally, so the flat window is
# visible whether or not it reached the bar.
DEFAULT_STALL_LIMIT = 4

WORD = re.compile(r"[a-z0-9]+")


def _signature(summary: str) -> str:
    """File-independent identity for a finding, used to spot relocation."""
    words = sorted(set(WORD.findall((summary or "").lower())))
    return " ".join(words)


def _reopen_key(finding: dict) -> str:
    """Cross-run identity for the reopen counter: id AND summary signature.

    The reopen counter is cross-run by design (see DEFAULT_REOPEN_LIMIT), so
    it needs an identity that survives a restart -- and `id` alone does not.
    SKILL.md's Phase 2 Step 8 template starts every run's findings at `F1`,
    and the ledger is per artifact, so run 1's `F1`, run 2's `F1` and run 3's
    `F1` are routinely three unrelated findings. Keyed on the bare id, three
    ordinary runs that each recorded their `F1` fixed and opened a new `F1`
    next time produced `reopen_counts: {"F1": 2}` and `escalate` on run 3's
    FIRST round -- a forced halt, routed away from the `--confirm` gate, with
    no recovery until the trailing window aged out.

    The `run` field fixed the same problem for round numbers. Ids get the
    signature instead of a scope, because the counter has to keep working
    ACROSS runs: scoping the key per run would delete the check. Pairing the
    id with `_signature(summary)` keeps a genuinely recurring finding on one
    key -- Step 8 restates it verbatim, so its signature is stable -- while
    two different findings that merely share an id fall on two keys.

    The residual error is one-directional and it is the safe one. A finding
    whose summary gets REWORDED between runs splits into two keys and its
    reopen count is undercounted, so the check stays silent where it might
    have fired. Missing an escalation costs a round; a false `escalate` is an
    unrecoverable halt on a healthy run, which is what this replaces.
    """
    signature = _signature(finding.get("summary", ""))
    return f"{finding.get('id') or ''}\u0000{signature}"


def _status(finding: dict) -> str:
    """A finding's status, folded to the vocabulary `STATUSES` declares.

    Every read of `status` goes through here. The four call sites used to
    compare `f.get("status") == "open"` (or `== "fixed"`) directly while
    `_blocking` normalized severity with `.lower()`, so the two halves of the
    same predicate disagreed about a ledger written with `"status": "Open"`:
    the finding was blocking but not open, `open_blocking` came back empty,
    and the run reported `converged` with blocking work outstanding. `main()`
    rejects an unrecognized status outright, so this only has to fold the
    recognized shapes; it stays tolerant because `analyze()` is a library
    call that never exits.
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
    """Findings still open at the end of `round_entries`.

    The verdict used to read the LAST round's open set alone, which made the
    absence of a finding from that round a close -- the exact reading every
    escalation signal in this module explicitly refuses ("Only an explicit
    `fixed` record counts as a close. A finding simply absent from a round is
    an unreported round, not a fix"). The verdict is the one place that
    reading is unsafe: a round that forgets to restate a still-open
    `critical`, which is what SKILL.md Phase 2 Step 8's "restate every
    still-live finding" rule exists to prevent and the likeliest executor
    slip, returned `verdict: converged`, `open_blocking: []`, exit 0. A run
    reported as success with a critical finding still live.

    So a finding is live when its LATEST record in the run says `open`. Only
    an explicit `fixed` or `rejected` closes it, which is the same rule the
    signals use, stated once and now shared with the verdict.

    Scoped to ONE RUN, like the streak, the stall window and the relocation
    trail, and for the same reason: the ledger is permanent, so carrying an
    unreported finding across runs would make it permanent too, and a run
    could never converge again short of deleting the file. That is the
    failure mode this module has already had to undo twice. Within a run the
    restatement is mandated and its omission is a slip; across runs the
    executor's restatement in round 1 is what carries the finding over, and
    the ledger's own history is not a substitute for it.

    A finding with no id carries no identity to track across rounds, so only
    those in the final round are read -- an earlier one cannot be matched to
    a later close, and counting every one of them would double-count the
    restatements.
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

    An explicit `run` id is the authoritative boundary, and it is the ONLY
    boundary: consecutive rounds sharing an id are one run, and a ledger whose
    rounds carry no id at all is read as one run.

    WHY THERE IS NO FALLBACK INFERENCE. A repeated round number used to infer
    a boundary here. But `_dedupe_rounds` reads the same signal as the
    opposite fact -- Phase 2's compaction-recovery protocol has the executor
    APPEND a redone round, so a repeat is a re-append -- and this function
    runs first, so on any `run`-less ledger the dedupe path was unreachable.
    `max_rounds: 3` with rounds `[1, 2, 2, 3]` and no `run` returned
    `in_progress` with `rounds_run: 2`; the identical ledger with a `run` id
    returned `capped` with `rounds_run: 3`. One compaction silently bought a
    run an unbounded extra budget.

    One signal cannot carry two meanings, so the repeat carries the one the
    ledger format actually mandates -- the compaction re-append -- and the run
    boundary carries the one the format actually records, the `run` id.

    The error direction is why it resolves this way and not the other. Missing
    a boundary over-counts the current run's rounds, which can only bring
    `capped` on earlier; `capped` is a FORCED halt that reaches the `--confirm`
    human gate (references/phase3-reevaluation.md), so the failure is visible
    and a human can grant more rounds. Inventing a boundary resets the per-run
    budget mid-run, which disables `capped` entirely: the loop runs past
    `max_rounds` with nothing to stop it and no gate to notice. A conservative
    early halt beats a silent unbounded loop.

    The cost is real and is the old second bug, now scoped to `run`-less
    ledgers only: a 4-round `run`-less ledger with `max_rounds: 3` reports
    `capped` on the second run's first round. `analyze` reports
    `run_scoping: "absent"` so that degradation is legible in the output
    rather than silent, and the module docstring asks for `run` on every
    round.

    A ledger where only SOME rounds carry an id is treated the same way: an
    entry with no id continues the current segment. Merging errs toward the
    over-count, which is the recoverable direction.
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
    """One entry per round number, keeping the latest append.

    Phase 2's compaction-recovery protocol tells the executor to APPEND and
    never overwrite, so a round interrupted mid-way and redone appears twice.
    Everything scoped to the current run is a statement about ROUNDS, and
    counting ENTRIES made a re-appended round look like an extra round: a
    `[1, 2, 2]` ledger with `max_rounds: 3` returned `capped` -- a forced halt
    reported as "budget exhausted" -- on round 2 of 3, and `blocking_counts`
    fed the stall window the same round's count twice, which is a flat
    stretch the run never had.

    The segment arrives sorted by round number, so duplicates are adjacent and
    the LAST of them is the most recent append, which is the one that
    supersedes. An entry whose round number is unusable (`_round_number`
    returns 0 for an absent or unparseable one) is left alone rather than
    folded in with every other such entry: they carry no identity to
    deduplicate on, and collapsing them would delete rounds.
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
    # Order WITHIN a run, never across runs: the global sort is what
    # interleaved them. See `_run_segments`.
    segments = [
        _dedupe_rounds(sorted(seg, key=_round_number))
        for seg in _run_segments(logged)
    ]
    rounds = [entry for segment in segments for entry in segment]
    current_run = segments[-1] if segments else []
    # Whether run boundaries were READ off the ledger or merely assumed. See
    # `_run_segments`: without a `run` id the whole ledger reads as one run,
    # so `rounds_run` may over-count and `capped` may arrive early. Reported
    # so a caller can tell a scoped verdict from a degraded one.
    run_scoping = "explicit" if any(
        entry.get("run") is not None for entry in logged
    ) else "absent"
    reasons: list[str] = []

    if not rounds:
        # Every key the normal return carries, so a caller reading the --json
        # contract -- `max_rounds` is the documented way to tell `capped` from
        # `in_progress` -- does not hit KeyError on a freshly created ledger.
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

    # The run's still-live open set decides both which escalation reasons hold
    # and what the verdict is. Every reason below is derived from the ledger's
    # whole history, and history does not un-happen, so a reason recorded once
    # used to hold forever: a finding open in rounds 1-3 and fixed in rounds
    # 4-5 kept returning `escalate` with an empty `open_blocking`, and
    # `escalate` means "halt and report a failing convergence gate". A run
    # that fixed what was stuck converged; it did not fail to converge. The
    # stall check already self-heals through its trailing window; this brings
    # the streak and the relocation checks into line with it.
    #
    # "Live" is `_live_findings`, not the final round's open array. A close
    # has to be recorded (`fixed` or `rejected`); an omission is an unreported
    # round. The signals and the verdict now read the same rule, which they
    # did not: the signals said so in a comment while the verdict, one screen
    # down, treated an omission as a fix.
    last_open = _live_findings(current_run)
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
    # the ordered history so the window below can age them out. Keyed on
    # `_reopen_key` (id AND summary signature), not on the bare id: the
    # counter is cross-run and ids restart per run.
    reopen_positions: dict[str, list[int]] = {}
    # id -> the reopen key it was last recorded `fixed` under, so a reopen is
    # reported against the id a reader recognizes.
    reopen_key_id: dict[str, str] = {}
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

    # Only transitions inside the trailing window count. See
    # DEFAULT_REOPEN_WINDOW_ROUNDS: without this the counter never expired and
    # one historical alternation escalated the artifact forever.
    window_start = len(rounds) - max(int(reopen_window or 0), 0)
    # Counted per reopen key, then reported per id: the key is what makes the
    # count correct, the id is what a reader and the `reopened` list use. Two
    # distinct findings sharing an id keep their own counts and the id shows
    # the larger of them, rather than their sum -- summing would rebuild the
    # exact false escalation the key exists to prevent.
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
    # Every still-open blocking finding's consecutive-open count, whether or
    # not it reached `recurrence_limit`. The bar sits one above the templated
    # round budget (see DEFAULT_RECURRENCE_LIMIT), so a default run can end
    # `capped` with a finding that has been open every round and no `reasons`
    # entry naming it. This is where that shows up, and it is the same
    # treatment `reopen_counts` already gets below its own bar: report the
    # signal, let the verdict be the verdict.
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
    # signature -> (origin file, origin finding id, round position of the close)
    closed_signatures: dict[str, tuple[str, str, int]] = {}
    # First position in this run at which each id was recorded open. This is
    # the disambiguator the signature alone cannot supply. Keying relocation
    # on `_signature(summary)` by itself read two DISTINCT findings that
    # happen to share wording -- "no stated exit condition" in SKILL.md and
    # the same sentence in reference.md, which is routine for hone findings --
    # as one finding that moved, the moment the first of them was fixed.
    # `escalate` has no counting bar (one hit halts) and phase3-reevaluation.md
    # routes it deliberately away from the `--confirm` human gate, so that
    # false positive killed the run outright.
    #
    # A relocation is a finding closed HERE and an equivalent one opening
    # THERE, so the destination has to be new: a finding already open at its
    # destination before the close did not move, it was simply already there.
    first_open_position: dict[str, int] = {}
    for position, round_entry in enumerate(current_run):
        for finding in _as_list(round_entry.get("findings")):
            if (isinstance(finding, dict) and finding.get("id")
                    and _status(finding) == "open"):
                first_open_position.setdefault(finding["id"], position)
    # Two passes per round, because one pass populated `closed_signatures` in
    # the same iteration that tested against it: whether a relocation was seen
    # depended on the order of findings WITHIN the round's array. A `fixed`
    # entry for file A listed after the reopened entry for file B was missed
    # that round, and missed permanently when that was the final round. Rounds
    # restate live findings so it usually self-corrected, but not on the last
    # one. Collecting the round's closes first makes detection independent of
    # array order and catches the same-round relocation either way round.
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
            # Relocation is an escalation reason, so it is severity-filtered
            # like the others: a minor finding that moved file is not a
            # non-converging loop.
            if _severity(finding) not in BLOCKING:
                continue
            if signature not in origins or signature not in last_open_signatures:
                continue
            file_path = finding.get("file", "")
            origin, origin_id, closed_at = origins[signature]
            if origin == file_path:
                continue
            destination_id = finding.get("id") or ""
            # Same id restated with a different file is a correction to
            # the record, not a finding chased across the artifact. A finding
            # with no id offers no evidence either way, and `escalate` is a
            # forced halt, so silence beats a guess.
            if not destination_id or destination_id == origin_id:
                continue
            # Open at its destination BEFORE the close: two concurrent
            # findings that share wording, not one that moved.
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
    # `max_rounds` is the budget for ONE invocation, so it is measured against
    # the current run's rounds, not against every round the artifact has ever
    # logged. Counting the whole ledger returned `capped` on a second run's
    # first round: an immediate `fail` gate and a halt before any work.
    # `bool(max_rounds)` conflated three different things -- a budget of zero,
    # an absent `max_rounds`, and one this module could not parse -- and read
    # all three as "no cap", which silently disables the `capped` verdict for
    # the whole run. `main()` refuses to run at all in those cases (see
    # `budget_error`), because "your ledger is malformed" and "you have rounds
    # left" are different answers and only one of them is true. `analyze()` is
    # a library call that cannot exit, so it reports `max_rounds: null` and
    # leaves the caller to notice; it is deliberately NOT the place that
    # decides a missing budget is permissive.
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
        # Rounds in the CURRENT run, which is what `max_rounds` budgets and
        # what a caller reporting "round N of M" means. The cumulative figures
        # are reported alongside rather than folded in; see the module
        # docstring for which signals read which scope.
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
        # Every finding that has come back at least once, whether or not it
        # reached the escalation bar. Below the bar this is the only place the
        # reopen signal is visible at all.
        "reopen_counts": {k: v for k, v in sorted(reopens.items()) if v},
        # Consecutive rounds each still-open blocking finding has been open,
        # reported below the escalation bar as well as at it.
        "open_streaks": open_streaks,
        "relocations": relocations,
        "blocking_counts": blocking_counts,
    }


def budget_error(ledger: dict) -> str | None:
    """Why this ledger's `max_rounds` cannot be used as a budget, or None.

    An unusable budget must not be silently permissive. `capped` is a FORCED
    halt and the only verdict that routes to the `--confirm` human gate, so a
    `max_rounds` this module cannot read disables the one check that stops a
    run reporting `in_progress` forever.

    The silent shape is new. SKILL.md and phase2-improvement.md used to
    template a concrete `"max_rounds": 3`, and the docs warn at length about
    that going stale. They now template the placeholder `<max_rounds>`, and an
    executor that copies it literally writes `"max_rounds": "<max_rounds>"` --
    valid JSON, an unparseable int, no diagnostic anywhere. That is the
    failure this rejects, alongside an absent budget and a non-positive one.
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
    """Why these tuning limits cannot be used, or an empty list.

    Same contract as `budget_error`: a limit this module cannot use is a
    usage error (exit 2), not something to run with quietly.

    Both failure shapes were reachable from the command line. `--stall-limit
    -1` made `blocking_counts[-(-1):]` empty, so `window[-1]` raised an
    IndexError that `main()` does not catch -- the process died with a
    traceback and exit 1, the code this module's docstring defines as "not
    converged yet, read the verdict from --json", with no JSON to read. Exit 2
    is supposed to be the only genuine failure, which is what the OSError and
    UnicodeDecodeError arms above already guard.

    And zero is worse than useless rather than merely odd: `blocking_counts
    [-0:]` is the WHOLE list and `len(...) >= 0` is always true, so
    `--stall-limit 0` escalates on the first round with any blocking finding
    open. `--recurrence-limit 0` and `--reopen-limit 0` flag every finding on
    its first round the same way. A negative `--reopen-window` is absorbed by
    a `max(..., 0)` downstream, but "every reopen has aged out" is not what
    the caller asked for, so it is rejected here rather than reinterpreted.
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
    """Findings whose `status` or `severity` is outside the closed vocabulary.

    Both fields decide whether a finding blocks convergence, and both used to
    fail open: an omitted or misspelled `severity` read as non-blocking, and a
    `status` of `"Open"` read as not-open. Either one alone turns a ledger
    with blocking work outstanding into `verdict: converged`, exit 0, and an
    empty `open_blocking` -- a run reporting success while the findings that
    should have stopped it sit in the file. Every other malformed shape here
    exits 2 loudly; these two did not, which made them the dangerous ones.

    Case and surrounding whitespace are folded rather than rejected, so
    `"Open"` is accepted as `open` (see `_status`). Only a value that is not a
    member of the vocabulary at all is an error.
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

    # Before the file is opened: these are usage errors in the invocation
    # itself, knowable without a ledger.
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
        # A ValueError, not an OSError, so it needs its own arm. A binary file
        # handed to `--ledger` is the same class of usage error as a directory.
        print(f"ERROR: ledger is not decodable text: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        # IsADirectoryError (passing `~/skill-eval/{name}/` instead of
        # `.../findings-ledger.json`) and PermissionError used to escape as a
        # traceback, and Python exits 1 on an uncaught exception -- the code
        # this module's docstring and both reference docs define as "not
        # converged yet, read the verdict from --json", with no JSON to read.
        # Caught after FileNotFoundError, which is an OSError subclass with a
        # more specific message. Same guard as validate_gates.main.
        print(f"ERROR: cannot read ledger: {exc}", file=sys.stderr)
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
