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

           Below the floor the verdict is ADVISORY, because hone's own
           generation minimums (2 cases on the lightweight tier, 3 at the
           Step 3/5 gates, 4 standard) all certify suites under any floor
           this script can enforce: a blocking floor made those tiers
           unrunnable and left padding with near-duplicates as the only
           escape, which is the remedy Step 6b warns against. So the gate
           yields, not the generator. Sizing still reports `underpowered`
           with the floor it enforced and the reason, and exits 0 with
           `blocking` false so the run carries the warning forward. A
           duplicate test case id still exits 1: it breaks the identity the
           next round pairs on. Usage and input errors keep exiting 2.

           The floor counts only the cases compare mode could actually pair.
           A case whose test_profile can never produce a deterministic
           composite never reaches the sign test, so counting it certified
           suites the comparison could never rule on: a six-case set with
           three knowledge_extraction cases sized `powered` and then compared
           three cases and returned `underpowered` on a clean sweep, forever,
           while Step 6b told the agent to add cases it could keep adding from
           the same unscorable profile. See NON_SCORABLE_PROFILES.

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
           cases and reported `underpowered` forever. Pass either the
           deterministic_scores.json or the results.json beside it; the
           deterministic sibling is what gets read. A round with no
           deterministic file at all is `not_measurable`, whichever side it
           is on: the judge scores in results.json are not the measurement
           Phase 2 acts on, so two judge files are not compared either.

           A case that scored in the before round and came back inconclusive
           (composite null) in the after round is also `not_measurable`. Its
           evidence collapsed rather than its score moving, and a sign test
           over the cases that still scored reads that collapse as a clean
           sweep. `inconclusive_after` names the cases.

Thresholds come from the binomial, not from taste. With n discordant votes and
w wins, p = sum(C(n,k) * 0.5**n for k in w..n). At alpha 0.05 that means 5-7
discordant votes need a clean sweep, 8 tolerate a single loss. Five distinct
cases is an eligibility floor, not adequate power: detecting a true 70% win
rate at 80% power needs roughly 37 discordant votes.

`--alpha` is a **per-direction** level, not the rate at which this script
reports something. `improved` and `regressed` are two separate one-sided
tests read against the same alpha, so the chance that either fires on noise
is up to twice it -- 0.0625, not 0.05, for a five-vote clean sweep. Each
individual decision still carries its own one-sided rate, which is why the
borrowed 5-7-8 table is kept as it stands rather than halved; the combined
figure is reported as `two_sided_alpha` so a consumer reading the emitted
JSON is not left to infer it from `alpha`.

Exit codes: 0 when nothing blocks the caller (sizing `powered`, sizing
`underpowered` on the floor alone, and compare `improved`); 1 when a finding
must stop the caller (a sizing criteria defect such as duplicate ids, and any
compare verdict outside `improved`); 2 usage or input error (a missing or
unreadable path, a non-object criteria root, a directory where a round file
was expected, only one of --before/--after). The `blocking` field in the
report carries the same decision, so a consumer reading `--json` never has to
re-derive it from the verdict.

Stdlib only. Read-only: it never writes to the criteria or results files.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# load_deterministic_scores owns the sibling-file rule: given a round's
# results.json it reads deterministic_scores.json beside it, dropping tests
# whose composite is null. load_inconclusive_ids is the other half of that
# file: the ids it dropped. A second copy here is a second copy to drift.
# extract_results and _raw_llm_score own the results.json shape (which key
# carries the entries, which field carries the judge score); the fallback
# below reads through them for the same reason.
from hone_common import (
    _raw_llm_score,
    extract_results,
    load_deterministic_scores,
    load_inconclusive_ids,
)

# Minimum distinct test cases before a verdict is meaningful at all. Below this
# no arrangement of wins can reach p <= 0.05 on a one-sided sign test.
DEFAULT_MIN_STIMULI = 5

# Minimum distinct test_profile values. Five cases that all exercise the same
# profile give arithmetic, not evidence.
DEFAULT_MIN_PROFILES = 2

# Test profiles that can never produce a deterministic composite, so a case
# carrying one can never enter compare mode's sign test.
# phase1-evaluation.md: `test_profile: "knowledge_extraction"` is **always**
# inconclusive deterministically, and `load_deterministic_scores` drops null
# composites. The other inconclusive classes named there (`error_handling`,
# `side_effect_guarded`, `failure_mode` with zero tool calls) depend on what a
# round actually executed and cannot be read off the criteria file, so they are
# deliberately absent: this is the statically knowable subset, not the whole
# truth. A case with no test_profile at all is counted as scorable here, which
# is the optimistic reading -- the scorer can still resolve it to
# knowledge_extraction from the round's own evidence.
NON_SCORABLE_PROFILES = frozenset({"knowledge_extraction"})

# Artifact types for which NON_SCORABLE_PROFILES does not apply.
# `knowledge_extraction` is always inconclusive only on the skill and command
# scoring paths. score_execution.py's `hook` and `script` branches score the
# same dimensions (trigger_accuracy / output_structure / correctness, off the
# run's own evidence) for every profile, so a hook or script case carrying
# that profile does produce a composite and does pair in compare mode.
# Excluding it there reported a fully pairable suite `underpowered` and
# attached a warning ("they never pair in compare mode") that was false for
# that artifact type. The profile does move the critical-dimension cap on
# those paths (`_score_single_test` swaps `critical_dim` to `error_handling`
# for knowledge_extraction on every type), so the case is capped under a
# different rule than its neighbours; it still scores, which is what the
# floor counts. The criteria file carries no artifact type of its own, so the
# caller supplies it with `--artifact-type`; unset keeps the conservative
# skill/command reading.
ALWAYS_SCORABLE_ARTIFACT_TYPES = frozenset({"hook", "script"})

# Every type score_execution.py scores, i.e. the values `--artifact-type`
# accepts and `metadata.artifact_type` in deterministic_scores.json can carry.
ARTIFACT_TYPES = ("skill", "command", "hook", "script")

ALPHA = 0.05

# Score movement below this is treated as a tie rather than a win or a loss.
# Matches the 0.1 regression threshold hone already uses in Phase 3, halved so
# that a movement large enough to count here is comfortably inside the noise
# band that triggers resampling there.
TIE_EPSILON = 0.05

# Decimal places a score movement is rounded to before it is classified. The
# composites are written with a handful of decimals, and `0.85 - 0.80` is
# 0.04999999999999993 while `0.55 - 0.50` is 0.050000000000000044, so an
# unrounded `abs(delta) <= TIE_EPSILON` called the same nominal 0.05 movement
# a tie in one suite and a win in another, and the round's verdict flipped
# with it. The rounded delta is also the one written to `movements`, so the
# number a reader sees is the number the decision was taken on.
DELTA_DECIMALS = 4

# Profiles score_execution.py's `_resolve_test_profile` can return, i.e. the
# keys of its PROFILE_WEIGHT_MAP. Anything else in `test_profile` is not
# honoured by the scorer, which falls through to its heuristics and, absent
# execution evidence, to the artifact-type default -- named `execution` here.
KNOWN_PROFILES = frozenset({
    "execution",
    "knowledge_extraction",
    "error_handling",
    "side_effect_guarded",
    "failure_mode",
})
DEFAULT_PROFILE = "execution"


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


def _id_of(entry: dict, keys: tuple[str, ...]) -> str | None:
    """The first of `keys` carrying a non-null value, as the string ids pair on.

    "Non-null", not "truthy": an integer id `0` is an id, and an `or` chain
    over the key names drops it. `_case_id` had that fixed and `_scores_by_id`
    reintroduced it on the results.json fallback path, so `"test_id": 0` was
    counted by sizing and silently unpaired by the comparison, missing from
    `paired_cases` and from both unpaired lists. One resolver, so the two
    cannot disagree about what an id is again.
    """
    for key in keys:
        raw = entry.get(key)
        if raw is not None:
            return str(raw)
    return None


def _case_id(case: dict) -> str | None:
    """The case's id as the string compare mode pairs on, or None if absent."""
    return _id_of(case, ("id",))


def _profile_of(case: dict) -> str:
    """The case's profile, under the name the scorer resolves it by.

    Mirrors the part of score_execution.py's `_resolve_test_profile` that can
    be read off the criteria file: an explicit `test_profile` the scorer
    knows, else `category: error_handling` (the one category its heuristics
    map to a profile), else the artifact-type default. Falling back to
    `category` wholesale, as this did, counted a required enum with a
    different value set as profile diversity: five cases across five
    categories and no `test_profile` reported five profiles and silenced the
    min-profiles warning, while the scorer weighted four of them identically.
    The same five with `test_profile: execution` filled in fired it. The
    warning flipped on whether an optional field was filled in, not on what
    the scorer would resolve.
    """
    profile = case.get("test_profile")
    if isinstance(profile, str) and profile in KNOWN_PROFILES:
        return profile
    category = case.get("category")
    if isinstance(category, str) and category.lower().replace("-", "_") == "error_handling":
        return "error_handling"
    return DEFAULT_PROFILE


def check_sizing(criteria: dict, min_stimuli: int, min_profiles: int,
                 alpha: float = ALPHA, artifact_type: str = "") -> dict:
    """Assess whether a criteria set is large and varied enough to rule.

    `artifact_type` decides whether NON_SCORABLE_PROFILES applies at all; see
    ALWAYS_SCORABLE_ARTIFACT_TYPES.
    """
    # The floor honours alpha, not --min-stimuli alone: 5 cases at alpha 0.01
    # need 7 discordant votes, so no arrangement of wins can ever clear it.
    # min_discordant_for_significance reported that; the verdict ignored it.
    alpha_floor = min_discordant_for_alpha(alpha)
    floor = max(min_stimuli, alpha_floor)

    cases = [c for c in (criteria.get("test_cases") or []) if isinstance(c, dict)]
    # Ids are compared as strings, the way `_scores_by_id` keys them, so an
    # integer id is a case rather than a falsy value to drop (0 vanished) or a
    # sort-time TypeError against a string neighbour.
    ids = [_case_id(c) for c in cases if _case_id(c) is not None]
    distinct_ids = sorted(set(ids))

    profile_scoped = artifact_type not in ALWAYS_SCORABLE_ARTIFACT_TYPES
    unscorable = NON_SCORABLE_PROFILES if profile_scoped else frozenset()
    scorable = [c for c in cases if _profile_of(c) not in unscorable]
    scorable_ids = sorted({_case_id(c) for c in scorable if _case_id(c) is not None})
    excluded_ids = sorted(set(distinct_ids) - set(scorable_ids))
    # Diversity is measured over the same subset the floor counts. Two profiles
    # of which only one can ever be scored is one profile of evidence.
    profiles = sorted({_profile_of(c) for c in scorable})

    errors: list[str] = []
    # `advisories` is the under-floor finding: real, reported, and carried
    # forward rather than halting the run. `errors` is what still blocks.
    advisories: list[str] = []
    warnings: list[str] = []

    if len(ids) != len(distinct_ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        errors.append(
            f"duplicate test case ids {duplicates}; ids are the comparison "
            "identity across rounds, so duplicates make pairing ambiguous"
        )
    if len(scorable_ids) < floor:
        advisories.append(
            f"{len(scorable_ids)} deterministically scorable test case(s) of "
            f"{len(distinct_ids)} distinct, floor is {floor} "
            f"(max of --min-stimuli {min_stimuli} and {alpha_floor} required by "
            f"alpha {alpha}); no arrangement of wins reaches p<={alpha} below "
            "the floor. Advisory: the run continues and carries the "
            "`underpowered` verdict, which justifies neither a promotion nor "
            "a revert. Add cases that discriminate a different property; do "
            "not pad the suite with near-duplicates to clear the floor"
        )
    if excluded_ids:
        warnings.append(
            f"{len(excluded_ids)} case(s) {excluded_ids} carry a profile that "
            f"is always inconclusive deterministically "
            f"{sorted(unscorable)}; they never pair in compare "
            "mode, so adding more of them cannot clear the floor"
        )
    # The hook and script scoring paths score the same dimensions for every
    # profile (the profile only moves the critical-dimension cap there), so
    # profile diversity says nothing about what they measure; the warning
    # pointed at a remedy (vary test_profile) that changes no measured
    # property for those types.
    if profile_scoped and len(profiles) < min_profiles:
        warnings.append(
            f"{len(profiles)} distinct scorable test profile(s) {profiles}, "
            f"recommended minimum is {min_profiles}; cases that all exercise "
            "one profile measure one property repeatedly"
        )

    powered = not errors and not advisories
    return {
        "mode": "sizing",
        "verdict": "powered" if powered else "underpowered",
        # Whether the caller must stop. An under-floor suite is `underpowered`
        # and NOT blocking: Step 6b reports it and Phase 1 carries on. A
        # duplicate-id suite is `underpowered` and blocking, because the
        # comparison identity the next round pairs on is broken.
        "blocking": bool(errors),
        "artifact_type": artifact_type,
        "distinct_cases": len(distinct_ids),
        "scorable_cases": len(scorable_ids),
        "excluded_cases": excluded_ids,
        "min_stimuli": min_stimuli,
        "effective_floor": floor,
        "profiles": profiles,
        "min_discordant_for_significance": min_discordant_for_alpha(alpha),
        "errors": errors,
        "advisories": advisories,
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

    A raw results.json is read through `hone_common.extract_results`, which
    owns the key precedence (`results` before `test_results`), and its judge
    score through `_raw_llm_score`, which owns the `final_score` alias. A
    private key list here had the precedence reversed, carried a `tests`
    alias no producer emits, and missed the alias, so a skill-creator-shaped
    file (`test_results` + `final_score`) yielded `{}` and the report said
    "0 scored" about a round it had in hand.
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
        elif isinstance(per_test, list):
            entries = per_test
        else:
            entries, _key = extract_results(results)
    if not isinstance(entries, list):
        return {}

    scores: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        test_id = _id_of(entry, ("test_id", "id", "name"))
        # Composite first (Phase 2 decides on it), and two separate lookups:
        # a `get` default misses `"score": null`, which hone_common emits when
        # the judge errored, silently dropping the pair.
        raw = entry.get("composite")
        if raw is None:
            raw = _raw_llm_score(entry)
        if test_id is None or raw is None:
            continue
        try:
            scores[test_id] = float(raw)
        except (TypeError, ValueError):
            continue
    return scores


def _inconclusive_ids(payload: dict) -> set[str]:
    """Ids `_load_round` recorded as inconclusive for this side, if any.

    A caller that built the payload by hand (a results.json shape, or a bare
    per_test mapping) carries none, and the compare then behaves as before.
    """
    ids = payload.get("inconclusive") if isinstance(payload, dict) else None
    if not isinstance(ids, (list, set, tuple)):
        return set()
    return {str(test_id) for test_id in ids}


def _scorer_fingerprint(payload: object) -> tuple[bool, str | None]:
    """`(known, fingerprint)` for one side of the comparison.

    `known` is False when the payload carries no `scorer_fingerprint` key at
    all, which is a caller that built the round by hand rather than through
    `_load_round`; those keep the pre-fingerprint behaviour, the same way an
    unset `before_source` does. A payload that came off disk always has the
    key, and a `None` value there means the file was written by a scorer old
    enough not to record one.
    """
    if not isinstance(payload, dict) or "scorer_fingerprint" not in payload:
        return False, None
    value = payload["scorer_fingerprint"]
    return True, value if isinstance(value, str) and value else None


def check_compare(before: dict, after: dict, alpha: float,
                  before_source: str = "", after_source: str = "") -> dict:
    """Sign-test the after-round against the before-round, per test case.

    `before_source` and `after_source` name the scorer each side's numbers
    came from (see `_score_source`). They are compared, not just recorded: the
    deterministic composite and the LLM judge score are both 0-1 and neither
    is a rescaling of the other, so a round whose deterministic file was
    pruned falls back to the judge and the sign test manufactures wins out of
    the scorer swap alone. That is a `not_measurable` comparison, not a
    verdict.

    The same reasoning one level down: two rounds can both be deterministic
    and still not be the same measurement, because the deterministic scorer
    is code that changes. `metadata.scorer_fingerprint` (score_execution.py)
    identifies the scoring logic that produced each side, and a mismatch --
    or an absence, which means an unidentified scorer rather than an
    unchanged one -- is `not_measurable` for the same reason a judge/
    deterministic swap is. The remedy is to re-score the older round, not to
    add test cases.
    """
    before_scores = _scores_by_id(before)
    after_scores = _scores_by_id(after)
    shared = sorted(set(before_scores) & set(after_scores))

    # A case that scored in one round and came back inconclusive in the other
    # is not an unpaired id: the criteria set did not change, the evidence
    # collapsed. `load_deterministic_scores` drops it, so it used to vanish
    # from the pairing and the sign test ruled on the survivors.
    before_inconclusive = _inconclusive_ids(before)
    after_inconclusive = _inconclusive_ids(after)
    collapsed = sorted(set(before_scores) & after_inconclusive)
    recovered = sorted(before_inconclusive & set(after_scores))

    wins = losses = ties = 0
    movements = []
    for test_id in shared:
        delta = round(after_scores[test_id] - before_scores[test_id], DELTA_DECIMALS)
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
            {"test_id": test_id, "delta": delta, "outcome": outcome}
        )

    discordant = wins + losses
    p_improve = sign_test_p(wins, discordant)
    p_regress = sign_test_p(losses, discordant)
    floor = min_discordant_for_alpha(alpha)

    # Unpaired means the id is absent from the other round. A collapsed or
    # recovered case is present there with a null composite, and is reported
    # under its own key rather than as a missing id.
    unpaired_before = sorted(set(before_scores) - set(after_scores) - after_inconclusive)
    unpaired_after = sorted(set(after_scores) - set(before_scores) - before_inconclusive)
    errors: list[str] = []
    warnings: list[str] = []

    scorers_disagree = (
        before_source and after_source and before_source != after_source
    )
    # `unpaired_after` deliberately does NOT gate this. It used to, and one
    # genuinely new case in the after round was then enough to hand a
    # collapsed baseline back to the generic mismatch message below ("check
    # that both paths name rounds of the same criteria set"), the precise
    # misdirection this branch exists to prevent, since the ids in `recovered`
    # do match. New after-only ids are named in the message instead.
    no_baseline = not shared and bool(recovered)
    # Two judge files agree on a scorer, not on the measurement Phase 2 acts
    # on; this used to reach `improved` exit 0 without naming the scorer.
    judge_only = before_source == after_source == "results"
    # Which scoring code produced each side. A round is comparable to another
    # only if the same scoring logic produced both; otherwise every movement
    # here is a mix of the artifact and the measurement, and Phase 3 reads it
    # as the artifact alone. See score_execution.py's `scorer_fingerprint`.
    before_known, before_fingerprint = _scorer_fingerprint(before)
    after_known, after_fingerprint = _scorer_fingerprint(after)
    fingerprints_checked = before_known or after_known
    # Absence is not agreement. 144 stored baselines predate the field, and
    # reading "no fingerprint on either side" as "same scorer" is exactly the
    # assumption that let a merged scorer change auto-revert working edits.
    # An unknown scorer is re-scored, not assumed.
    scorer_unknown = fingerprints_checked and not (
        before_fingerprint and after_fingerprint
    )
    scorer_changed = bool(
        fingerprints_checked
        and before_fingerprint
        and after_fingerprint
        and before_fingerprint != after_fingerprint
    )
    if scorers_disagree:
        verdict = "not_measurable"
        errors.append(
            f"--before scores came from the {before_source} scorer and "
            f"--after from the {after_source} scorer; both are 0-1 but they "
            "are not the same measurement, so any win or loss here could be "
            "the scorer swap rather than the round. Re-run deterministic "
            "scoring on the round that is missing it, or point both flags at "
            "rounds scored the same way"
        )
    elif judge_only:
        verdict = "not_measurable"
        errors.append(
            "neither round has a deterministic_scores.json, so both sides "
            "fell back to the LLM judge scores in results.json. Phase 2 "
            "decides on the deterministic composite, not the judge, and this "
            "comparison qualifies nothing it acts on. Run score_execution.py "
            "on both rounds and point --before/--after at the "
            "deterministic_scores.json files it writes"
        )
    elif scorer_changed:
        verdict = "not_measurable"
        errors.append(
            f"--before was scored by scorer {before_fingerprint} and --after "
            f"by {after_fingerprint}; the scoring code changed between the "
            "two rounds, so a win or a loss here can be the scorer rather "
            "than the round. Re-score the before round's results.json with "
            "the current scorer (score_execution.py, same --type, "
            "--artifact-path pointing at the artifact that round ran "
            "against) and compare again"
        )
    elif scorer_unknown:
        verdict = "not_measurable"
        missing = [
            side
            for side, fingerprint in (
                ("--before", before_fingerprint),
                ("--after", after_fingerprint),
            )
            if not fingerprint
        ]
        errors.append(
            f"{' and '.join(missing)} records no metadata.scorer_fingerprint, "
            "so the scorer that produced those numbers is unknown and cannot "
            "be shown to match the other round's. Absent is not unchanged: a "
            "scorer change landing outside a hone run leaves the older side "
            "looking untouched. Re-score that round's results.json with the "
            "current scorer (score_execution.py, same --type, --artifact-path "
            "pointing at the artifact that round ran against) and compare "
            "again"
        )
    elif collapsed:
        # A partial evidence loss looks like a clean sweep over the survivors;
        # Step 9a's input remedy applies, not Step 6b's.
        verdict = "not_measurable"
        errors.append(
            f"{len(collapsed)} case(s) {collapsed} scored in --before and "
            f"came back inconclusive in --after ({len(shared)} still paired). "
            "Their execution evidence collapsed rather than their scores "
            "moving, and a verdict over the survivors would read that loss "
            "as an improvement. Re-run the after round, or find out why "
            "those cases produced no scorable evidence, before comparing"
        )
    elif no_baseline:
        # Mirror image of `collapsed`: the ids match, the baseline produced no
        # evidence. The mismatch message below sent the agent to check paths.
        verdict = "not_measurable"
        also_new = (
            f" A further {len(unpaired_after)} case(s) {unpaired_after} are "
            "new in --after and have no baseline either." if unpaired_after else ""
        )
        errors.append(
            f"0 paired test case(s): all {len(recovered)} case(s) the before "
            f"round knows and --after scored {recovered} were inconclusive in "
            f"--before, so there is no baseline to compare against.{also_new} "
            "Those test ids match; the before round produced no scorable "
            "evidence. Re-run the before round, or treat this round as the "
            "new baseline"
        )
    elif not shared:
        # Zero pairs is a pairing failure, and reporting it as `underpowered`
        # sent the agent to Step 6b's remedy ("add cases that discriminate a
        # different property"), which cannot fix a test-id mismatch or an
        # absent deterministic_scores.json.
        verdict = "not_measurable"
        errors.append(
            f"0 paired test case(s): no test id is present in both rounds "
            f"({len(before_scores)} scored before, {len(after_scores)} after). "
            "Nothing was compared, so this is not a tie-heavy round. Check "
            "that both paths name rounds of the same criteria set, and that "
            "each round's deterministic_scores.json exists and carries "
            "per-test composites"
        )
    elif discordant < floor:
        verdict = "underpowered"
        warnings.append(
            f"{discordant} discordant case(s) with {ties} tie(s); "
            "ties hold the discordant count down and no verdict is "
            "reachable until the count clears the floor"
        )
    elif p_improve <= alpha:
        verdict = "improved"
    elif p_regress <= alpha:
        verdict = "regressed"
    else:
        verdict = "inconclusive"

    if recovered and not no_baseline:
        # No before-score means no verdict can be manufactured; named so an
        # unstable suite is visible. When nothing paired at all the error
        # above already names them.
        warnings.append(
            f"{len(recovered)} case(s) {recovered} were inconclusive in "
            "--before and scored in --after; they have no baseline and were "
            "not paired"
        )

    return {
        "mode": "compare",
        "verdict": verdict,
        "paired_cases": len(shared),
        "unpaired_before": unpaired_before,
        "unpaired_after": unpaired_after,
        "inconclusive_after": collapsed,
        "inconclusive_before": sorted(before_inconclusive),
        "before_score_source": before_source,
        "after_score_source": after_source,
        "before_scorer_fingerprint": before_fingerprint,
        "after_scorer_fingerprint": after_fingerprint,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "discordant": discordant,
        "min_discordant_for_significance": floor,
        "p_improved": round(p_improve, 5),
        "p_regressed": round(p_regress, 5),
        "alpha": alpha,
        # `alpha` governs one direction; either verdict firing on noise is up
        # to twice it, so `alpha` alone is half the rate a consumer wants.
        "two_sided_alpha": min(1.0, round(2 * alpha, 6)),
        "movements": movements,
        "errors": errors,
        "warnings": warnings,
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


def _require_path(path: str) -> None:
    """Exit 2 on a path that is not a readable file.

    `load_deterministic_scores` only ever looks at `<parent>/
    deterministic_scores.json`, so it does not care whether the path it was
    handed exists: `--before r1/reslts.json` read `r1/deterministic_scores.json`
    and produced a confident verdict off a typo. The documented contract for a
    path that is not there is exit 2, and nothing downstream can restore it
    once the fallback has succeeded.

    A directory is the same failure wearing a plausible shape, and the docs
    used to invite it by calling `--before` "the previous round's output
    directory". `--before r1` resolves to `<parent-of-r1>/
    deterministic_scores.json`: either it does not exist and the run dies on an
    unhelpful "Is a directory", or it does and both flags silently read the
    same file, tying every case and reporting `underpowered` forever.
    """
    target = Path(path)
    if not target.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    if not target.is_file():
        print(
            f"ERROR: {path} is a directory; pass the round's "
            "deterministic_scores.json (or the results.json beside it), not "
            "the output directory that holds it",
            file=sys.stderr,
        )
        sys.exit(2)


def _deterministic_file(path: str) -> Path:
    """The `deterministic_scores.json` that `load_deterministic_scores` resolves.

    Mirrors hone_common's sibling-file rule rather than importing it, because
    hone_common exposes the loaded scores and this needs the file's
    *presence*: a deterministic file that exists and scored nothing is a
    deterministically scored round, not an unscored one.
    """
    return Path(path).parent / "deterministic_scores.json"


def _load_round(path: str) -> tuple[dict, str]:
    """Load one round's scores and name the scorer they came from.

    `path` may be either a round's `deterministic_scores.json` or the
    `results.json` beside it; `load_deterministic_scores` resolves both to the
    deterministic file, which is what Phase 2 decides on. The payload also
    carries the ids that file marked inconclusive, because a case that scored
    last round and produced no evidence this round is a finding, not an
    absent id, and `check_compare` refuses to rule over the survivors.

    Falling back to `_load(path)` when there is no deterministic file keeps
    the exit-2 contract for a missing or non-object file and lets the report
    say what it did find (how many judge scores, under which ids). It does
    not make the judge comparable: `check_compare` returns `not_measurable`
    whenever either side, or both, came from `results`. Two judge files with
    matching sources used to sail through the scorer-swap guard and reach
    `improved` exit 0, with a text report that never said which scorer it
    had read.

    The source is returned from here rather than inferred separately, because
    inferring it from `load_deterministic_scores(path)` being truthy conflated
    two different rounds. A round whose every composite came back null -- the
    signature of a catastrophic regression, per score_execution.py's
    inconclusive paths -- has a deterministic file that scored nothing, and the
    truthiness test called that "results". Paired against a normal round it
    reported a scorer swap that never happened, burying the real finding under
    "re-run deterministic scoring on the round that is missing it".

    Such a round also does not fall back to the judge. The deterministic file
    is present and is the scorer of record; falling through to results.json
    would swap scorers *within* one side, which is the exact substitution
    `check_compare` refuses to rule on. It returns no scores and every id as
    inconclusive instead, so the comparison names the collapse.

    The deterministic file is parsed with `_load` first, because hone_common's
    loaders swallow a JSONDecodeError or a non-object root to `{}`, and `{}`
    is indistinguishable from a round that scored nothing: a truncated
    deterministic file came back as a zero-pair criteria mismatch that never
    named the file, sending the agent to re-check test ids. Invalid JSON is
    the documented exit 2 everywhere else and is here too.

    The payload also carries `metadata.artifact_type` when the scorer wrote
    one, so compare mode can size the criteria under the type that actually
    scored the rounds rather than a flag the caller may have left off.
    """
    _require_path(path)
    deterministic_path = _deterministic_file(path)
    metadata: dict = {}
    if deterministic_path.is_file():
        loaded = _load(str(deterministic_path))
        if isinstance(loaded.get("metadata"), dict):
            metadata = loaded["metadata"]
    deterministic = load_deterministic_scores(path)
    if deterministic or deterministic_path.is_file():
        return {
            "per_test": [
                {"test_id": test_id, "composite": composite}
                for test_id, composite in deterministic.items()
            ],
            "inconclusive": sorted(load_inconclusive_ids(path)),
            "artifact_type": metadata.get("artifact_type"),
            # Always set, None included: `check_compare` reads the key's
            # *presence* as "this side came off disk", so a round scored
            # before score_execution.py recorded a fingerprint is an
            # unknown scorer rather than an opted-out caller.
            "scorer_fingerprint": metadata.get("scorer_fingerprint"),
        }, "deterministic"
    return _load(path), "results"


def _recorded_artifact_type(*rounds: dict) -> tuple[str, str]:
    """The artifact type the rounds were scored under, and a caveat if unclear.

    score_execution.py writes `metadata.artifact_type` into every
    deterministic_scores.json (`--type`), and that is the type whose scoring
    path produced the composites being compared. Returns ("", "") when neither
    round recorded one, and ("", reason) when the two rounds disagree, which
    is a pair no single sizing reading fits.
    """
    recorded = sorted({
        r.get("artifact_type") for r in rounds
        if isinstance(r, dict) and r.get("artifact_type") in ARTIFACT_TYPES
    })
    if not recorded:
        return "", ""
    if len(recorded) > 1:
        return "", (
            f"the two rounds record different artifact types {recorded} in "
            "metadata.artifact_type; they were scored on different paths and "
            "the sizing reading falls back to --artifact-type"
        )
    return recorded[0], ""


def _combined_verdict(sizing: dict, comparison: dict) -> str:
    """The verdict a caller acts on, given both halves of the report.

    A sizing failure outranks whatever the comparison computed, in both
    directions. phase1-evaluation.md Step 9a: an `underpowered` run is
    "neither a pass nor a regression ... never let it justify a promotion or a
    revert", and the sign test is direction-blind under the null, so a suite
    too small to promote on is equally too small to revert on.

    This is what keeps "advisory" from meaning "ignored". Sizing no longer
    halts Phase 1 below the floor, so under-floor suites now reach Phase 3 as
    the common path rather than the exception; the override is what stops the
    extra traffic from landing on Phase 3's auto-revert. An `underpowered`
    sizing therefore still suppresses the comparison in BOTH directions: the
    combined verdict never reads `improved` (no promotion) and never reads
    `regressed` (no auto-revert), and phase3-reevaluation.md step 5 keys its
    auto-revert precondition off exactly that.

    What the override must not do is happen quietly. The nested
    `comparison.verdict` is left intact for a human reading the JSON, and
    anything the override hides is stated as a warning rather than being
    absorbed into a bare "underpowered".

    `not_measurable` gets its own warning because it is the one hidden verdict
    whose remedy differs. Step 9a records the surface verdict, and the
    `underpowered` remedy is "add test cases that discriminate a different
    property" -- which cannot fix a test-id mismatch or an absent
    deterministic file, the input problems phase1-evaluation.md Step 9a says to
    fix instead. Without the warning that distinction survived only in the
    nested JSON nobody reads.
    """
    if sizing["verdict"] == "powered":
        return comparison["verdict"]
    if comparison["verdict"] == "not_measurable":
        comparison["warnings"].append(
            "the comparison was also not_measurable: nothing was compared, "
            "for an input reason named in comparison.errors. Adding test "
            "cases, the underpowered remedy, does not fix that -- fix the "
            "inputs as well as the criteria set"
        )
    elif comparison["verdict"] != "underpowered":
        comparison["warnings"].append(
            f"nominal comparison verdict '{comparison['verdict']}' is "
            "suppressed because sizing failed; fix the criteria set and "
            "re-run before acting on it either way"
        )
    return "underpowered"


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
    parser.add_argument(
        "--alpha",
        type=float,
        default=ALPHA,
        help="Per-direction significance level; `improved` and `regressed` "
             "are read against it separately, so the combined rate is up to "
             f"twice it (reported as two_sided_alpha) (default: {ALPHA})",
    )
    parser.add_argument(
        "--artifact-type",
        choices=ARTIFACT_TYPES,
        default="",
        help="Artifact the criteria were written for. Hooks and scripts score "
             "every profile deterministically, so the always-inconclusive "
             "profile exclusion does not apply to them. In compare mode the "
             "type recorded in the rounds' deterministic_scores.json "
             "(metadata.artifact_type) is used and this flag is checked "
             "against it (default: the conservative skill/command reading)",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    if bool(args.before) != bool(args.after):
        print(
            "ERROR: compare mode needs both --before and --after",
            file=sys.stderr,
        )
        sys.exit(2)

    criteria = _load(args.criteria_file)

    if not args.before:
        report = check_sizing(criteria, args.min_stimuli, args.min_profiles,
                              args.alpha, args.artifact_type)
    else:
        before_round, before_source = _load_round(args.before)
        after_round, after_source = _load_round(args.after)
        # Size under the type that scored the rounds: trusting the flag alone
        # buried a hook suite's genuine `improved` under `underpowered`.
        recorded_type, type_caveat = _recorded_artifact_type(before_round, after_round)
        artifact_type = recorded_type or args.artifact_type
        sizing = check_sizing(criteria, args.min_stimuli, args.min_profiles,
                              args.alpha, artifact_type)
        if type_caveat:
            sizing["warnings"].append(type_caveat)
        elif recorded_type and args.artifact_type and args.artifact_type != recorded_type:
            sizing["warnings"].append(
                f"--artifact-type {args.artifact_type} disagrees with the "
                f"{recorded_type} recorded in both rounds' "
                "metadata.artifact_type; the rounds were scored as a "
                f"{recorded_type} and are sized as one"
            )
        comparison = check_compare(
            before_round, after_round, args.alpha, before_source, after_source,
        )
        # A top-level `mode`, like the sizing report's, so a consumer can tell
        # the two shapes apart without probing for a `comparison` key.
        verdict = _combined_verdict(sizing, comparison)
        # Compare mode keeps its original exit semantics: only `improved` is a
        # result a caller may act on. `underpowered` here is non-zero on
        # purpose -- Phase 3 must neither promote nor revert on it, and a zero
        # exit would read as the clean pass the sizing half just denied.
        report = {"mode": "compare", "sizing": sizing, "comparison": comparison,
                  "verdict": verdict, "blocking": verdict != "improved"}

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"VERDICT: {report['verdict']}")
        # Sizing mode only. The advisory describes a verdict the run carries
        # forward without acting on it, which is what a non-blocking sizing
        # verdict (`underpowered`, no errors) is. In compare mode `blocking`
        # means something else entirely -- it is `verdict != "improved"` --
        # so the one compare verdict reaching this branch was `improved`, and
        # a 6W/0L/0T comparison at p=0.0156 printed "it justifies neither a
        # promotion nor a revert", the exact opposite of Step 9a's rule for
        # `improved` ("Act on it"). The JSON was always right; only this line
        # was wrong.
        if (report["mode"] == "sizing" and not report["blocking"]
                and report["verdict"] != "powered"):
            print("  advisory: not blocking, the run continues carrying this "
                  "verdict; it justifies neither a promotion nor a revert")
        sizing = report["sizing"] if report["mode"] == "compare" else report
        for section in (sizing, report.get("comparison")):
            if not section:
                continue
            if section["mode"] == "sizing":
                print(f"  sizing: {section['scorable_cases']} scorable of "
                      f"{section['distinct_cases']} distinct case(s), "
                      f"floor {section['effective_floor']}, profiles {section['profiles']}")
            else:
                print(f"  compare: {section['wins']}W/{section['losses']}L/"
                      f"{section['ties']}T over {section['paired_cases']} paired, "
                      f"p_improved={section['p_improved']}, scorers "
                      f"{section['before_score_source'] or '?'}/"
                      f"{section['after_score_source'] or '?'}")
                if section["inconclusive_after"]:
                    print(f"  inconclusive after: {section['inconclusive_after']}")
            for error in section["errors"]:
                print(f"  ERROR: {error}")
            for advisory in section.get("advisories", ()):
                print(f"  ADVISORY: {advisory}")
            for warning in section["warnings"]:
                print(f"  WARNING: {warning}")

    sys.exit(1 if report["blocking"] else 0)


if __name__ == "__main__":
    main()
