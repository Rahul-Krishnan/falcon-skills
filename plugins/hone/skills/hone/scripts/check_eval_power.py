#!/usr/bin/env python3
"""Power check for hone eval criteria and before/after comparisons.

score_execution.py answers "what did this round score". It does not answer
"is that score strong enough to act on". Those are different questions, and
conflating them lets a three-case criteria set promote or revert an artifact on
noise. Phase 3 already controls variance (median-of-three resampling); this
script controls power, which resampling cannot buy.

Two modes:

  sizing   (default) -- Is the criteria set large and varied enough to return a
           verdict at all? Below the floor the correct answer is "underpowered",
           which is neither a pass nor a regression.

  compare  (--before/--after) -- Given two rounds of scores, run an exact
           one-sided sign test over the discordant (non-tied) test cases. Ties
           are not discarded: they hold the discordant count down, which is the
           point. A round that moves two cases and ties six has not shown
           anything, and reporting it as an improvement is the failure mode.

           The scores compared are the deterministic composites from
           `deterministic_scores.json`, because those are the numbers Phase 2
           acts on (phase1-evaluation.md Step 9: "Phase 2 decisions use the
           deterministic score. The LLM judge score is a reference signal
           only."). Pointing this at results.json instead qualified a number
           nobody decides on, and on a deterministic-only run results.json
           carries no per-test `score` at all, so every comparison paired zero
           cases and reported `underpowered` forever. A round directory is
           accepted either way: pass the results.json and the sibling
           deterministic_scores.json is read instead when it exists.

Thresholds come from the binomial, not from taste. With n discordant votes and
w wins, p = sum(C(n,k) * 0.5**n for k in w..n). At alpha 0.05 that means 5-7
discordant votes need a clean sweep, 8 tolerate a single loss. Five distinct
cases is an eligibility floor, not adequate power: detecting a true 70% win
rate at 80% power needs roughly 37 discordant votes.

Exit codes: 0 powered, 1 underpowered or not significant, 2 usage error.

Stdlib only. Read-only: it never writes to the criteria or results files.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

# load_deterministic_scores owns the sibling-file rule: given a round's
# results.json it reads deterministic_scores.json beside it, dropping tests
# whose composite is null. A second copy here is a second copy to drift.
from hone_common import load_deterministic_scores

# Minimum distinct test cases before a verdict is meaningful at all. Below this
# no arrangement of wins can reach p <= 0.05 on a one-sided sign test.
DEFAULT_MIN_STIMULI = 5

# Minimum distinct test_profile values. Five cases that all exercise the same
# profile give arithmetic, not evidence.
DEFAULT_MIN_PROFILES = 2

ALPHA = 0.05

# Score movement below this is treated as a tie rather than a win or a loss.
# Matches the 0.1 regression threshold hone already uses in Phase 3, halved so
# that a movement large enough to count here is comfortably inside the noise
# band that triggers resampling there.
TIE_EPSILON = 0.05


def sign_test_p(wins: int, discordant: int) -> float:
    """Exact one-sided binomial p for `wins` successes in `discordant` trials."""
    if discordant <= 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(wins, discordant + 1))
    return tail / (2 ** discordant)


def min_discordant_for_alpha(alpha: float = ALPHA) -> int:
    """Smallest discordant count where a clean sweep reaches significance."""
    n = 1
    while n < 64:
        if sign_test_p(n, n) <= alpha:
            return n
        n += 1
    return n


def check_sizing(criteria: dict, min_stimuli: int, min_profiles: int,
                 alpha: float = ALPHA) -> dict:
    """Assess whether a criteria set is large and varied enough to rule."""
    # The floor honours alpha, not --min-stimuli alone: 5 cases at alpha 0.01
    # need 7 discordant votes, so no arrangement of wins can ever clear it.
    # min_discordant_for_significance reported that; the verdict ignored it.
    alpha_floor = min_discordant_for_alpha(alpha)
    floor = max(min_stimuli, alpha_floor)

    cases = criteria.get("test_cases") or []
    ids = [c.get("id") for c in cases if isinstance(c, dict) and c.get("id")]
    distinct_ids = sorted(set(ids))
    profiles = sorted(
        {c.get("test_profile") or c.get("category") or "(unset)"
         for c in cases if isinstance(c, dict)}
    )

    errors: list[str] = []
    warnings: list[str] = []

    if len(ids) != len(distinct_ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        errors.append(
            f"duplicate test case ids {duplicates}; ids are the comparison "
            "identity across rounds, so duplicates make pairing ambiguous"
        )
    if len(distinct_ids) < floor:
        errors.append(
            f"{len(distinct_ids)} distinct test case(s), floor is {floor} "
            f"(max of --min-stimuli {min_stimuli} and {alpha_floor} required by "
            f"alpha {alpha}); no arrangement of wins reaches p<={alpha} below "
            "the floor"
        )
    if len(profiles) < min_profiles:
        warnings.append(
            f"{len(profiles)} distinct test profile(s) {profiles}, "
            f"recommended minimum is {min_profiles}; cases that all exercise "
            "one profile measure one property repeatedly"
        )

    powered = not errors
    return {
        "mode": "sizing",
        "verdict": "powered" if powered else "underpowered",
        "distinct_cases": len(distinct_ids),
        "min_stimuli": min_stimuli,
        "effective_floor": floor,
        "profiles": profiles,
        "min_discordant_for_significance": min_discordant_for_alpha(alpha),
        "errors": errors,
        "warnings": warnings,
    }


def _scores_by_id(results: dict) -> dict[str, float]:
    """Extract per-test composite scores from a results or scoring payload.

    Accepts the shapes hone actually produces. `score_from_results` emits
    `per_test` as a **list** of records carrying `test_id` and `composite`,
    and that scoring payload is exactly what `--before`/`--after` are pointed
    at, so the list has to reach the entry loop below. Treating `per_test` as
    a mapping only, as this did, sent every hone scoring payload down the
    raw-results branch, which found no matching key and returned `{}`: zero
    paired cases and an `underpowered` verdict on every comparison.

    A `per_test` mapping is still accepted (id -> score, or id -> record) but
    is normalised into entries so it shares the loop's type guards. The old
    mapping branch called `float()` on the record itself whenever the record
    carried no `"score"` key -- which is every hone record, since hone names
    that field `"composite"` -- and raised an uncaught TypeError.
    """
    if isinstance(results, list):
        entries = results
    else:
        per_test = results.get("per_test")
        if isinstance(per_test, dict):
            entries = [
                {"test_id": key, **value}
                if isinstance(value, dict)
                else {"test_id": key, "score": value}
                for key, value in per_test.items()
            ]
        else:
            entries = (
                per_test
                or results.get("test_results")
                or results.get("results")
                or results.get("tests")
                or []
            )
    if not isinstance(entries, list):
        return {}

    scores: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        test_id = entry.get("test_id") or entry.get("id") or entry.get("name")
        # Composite first (Phase 2 decides on it), and two separate lookups:
        # a `get` default misses `"score": null`, which hone_common emits when
        # the judge errored, silently dropping the pair.
        raw = entry.get("composite")
        if raw is None:
            raw = entry.get("score")
        if test_id is None or raw is None:
            continue
        try:
            scores[str(test_id)] = float(raw)
        except (TypeError, ValueError):
            continue
    return scores


def check_compare(before: dict, after: dict, alpha: float) -> dict:
    """Sign-test the after-round against the before-round, per test case."""
    before_scores = _scores_by_id(before)
    after_scores = _scores_by_id(after)
    shared = sorted(set(before_scores) & set(after_scores))

    wins = losses = ties = 0
    movements = []
    for test_id in shared:
        delta = after_scores[test_id] - before_scores[test_id]
        if abs(delta) <= TIE_EPSILON:
            ties += 1
            outcome = "tie"
        elif delta > 0:
            wins += 1
            outcome = "win"
        else:
            losses += 1
            outcome = "loss"
        movements.append(
            {"test_id": test_id, "delta": round(delta, 4), "outcome": outcome}
        )

    discordant = wins + losses
    p_improve = sign_test_p(wins, discordant)
    p_regress = sign_test_p(losses, discordant)

    if discordant < min_discordant_for_alpha(alpha):
        verdict = "underpowered"
    elif p_improve <= alpha:
        verdict = "improved"
    elif p_regress <= alpha:
        verdict = "regressed"
    else:
        verdict = "inconclusive"

    return {
        "mode": "compare",
        "verdict": verdict,
        "paired_cases": len(shared),
        "unpaired_before": sorted(set(before_scores) - set(after_scores)),
        "unpaired_after": sorted(set(after_scores) - set(before_scores)),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "discordant": discordant,
        "min_discordant_for_significance": min_discordant_for_alpha(alpha),
        "p_improved": round(p_improve, 5),
        "p_regressed": round(p_regress, 5),
        "alpha": alpha,
        "movements": movements,
        "errors": [],
        "warnings": (
            []
            if discordant >= min_discordant_for_alpha(alpha)
            else [
                f"{discordant} discordant case(s) with {ties} tie(s); "
                "ties hold the discordant count down and no verdict is "
                "reachable until the count clears the floor"
            ]
        ),
    }


def _load(path: str) -> dict:
    """Load a JSON object, rejecting any other root shape as a usage error.

    Every caller immediately does `.get()` on the result, so a list- or
    scalar-rooted file (a criteria file holding a bare array of test cases, a
    truncated write) raised an uncaught AttributeError traceback instead of
    the documented exit 2. Same tolerance `_load_criteria_index` in
    score_execution.py was hardened for.
    """
    try:
        with open(path) as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(loaded, dict):
        print(
            f"ERROR: {path} root must be a JSON object, got "
            f"{type(loaded).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)
    return loaded


def _load_round(path: str) -> dict:
    """Load one round's scores, preferring the deterministic composites.

    `path` may be either a round's `deterministic_scores.json` or the
    `results.json` beside it; `load_deterministic_scores` resolves both to the
    deterministic file, which is what Phase 2 decides on. Falling back to
    `_load(path)` keeps the older shapes working (a results.json from a run
    that had an LLM judge, or any payload already carrying per-test scores),
    and keeps the exit-2 contract for a missing or non-object file.
    """
    deterministic = load_deterministic_scores(path)
    if deterministic:
        return {
            "per_test": [
                {"test_id": test_id, "composite": composite}
                for test_id, composite in deterministic.items()
            ]
        }
    return _load(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Power check for hone eval criteria and round comparisons"
    )
    parser.add_argument("criteria_file", help="Path to eval_criteria.json")
    parser.add_argument(
        "--before",
        help="Prior round deterministic_scores.json, or the results.json "
             "beside it (compare mode)",
    )
    parser.add_argument(
        "--after",
        help="Current round deterministic_scores.json, or the results.json "
             "beside it (compare mode)",
    )
    parser.add_argument(
        "--min-stimuli",
        type=int,
        default=DEFAULT_MIN_STIMULI,
        help=f"Distinct test case floor (default: {DEFAULT_MIN_STIMULI})",
    )
    parser.add_argument(
        "--min-profiles",
        type=int,
        default=DEFAULT_MIN_PROFILES,
        help=f"Distinct test profile floor (default: {DEFAULT_MIN_PROFILES})",
    )
    parser.add_argument("--alpha", type=float, default=ALPHA, help="Significance level")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    if bool(args.before) != bool(args.after):
        print(
            "ERROR: compare mode needs both --before and --after",
            file=sys.stderr,
        )
        sys.exit(2)

    criteria = _load(args.criteria_file)
    sizing = check_sizing(criteria, args.min_stimuli, args.min_profiles, args.alpha)

    report = sizing
    if args.before:
        comparison = check_compare(
            _load_round(args.before), _load_round(args.after), args.alpha
        )
        report = {"sizing": sizing, "comparison": comparison,
                  "verdict": comparison["verdict"]
                  if sizing["verdict"] == "powered" else "underpowered"}

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"VERDICT: {report['verdict']}")
        for section in (sizing, report.get("comparison")):
            if not section:
                continue
            if section["mode"] == "sizing":
                print(f"  sizing: {section['distinct_cases']} distinct case(s), "
                      f"floor {section['effective_floor']}, profiles {section['profiles']}")
            else:
                print(f"  compare: {section['wins']}W/{section['losses']}L/"
                      f"{section['ties']}T over {section['paired_cases']} paired, "
                      f"p_improved={section['p_improved']}")
            for error in section["errors"]:
                print(f"  ERROR: {error}")
            for warning in section["warnings"]:
                print(f"  WARNING: {warning}")

    sys.exit(0 if report["verdict"] in ("powered", "improved") else 1)


if __name__ == "__main__":
    main()
