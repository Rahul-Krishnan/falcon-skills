#!/usr/bin/env python3
"""Check eval sizing and compare before/after deterministic scores.

Sizing (default) reports whether enough scorable cases and profiles exist.
The minimum is advisory: lightweight and standard suites may continue as
underpowered. Duplicate ids block because comparisons need unique identities.
Only statically scorable profiles count toward the floor; hook and script
scoring can produce composites for every profile.

Compare (--before/--after) applies exact one-sided sign tests to non-tied
paired cases. Ties reduce the available discordant votes. Read scores from
deterministic_scores.json, directly or beside the supplied results.json.
Missing deterministic files, changed or unknown scorers, and cases that
become inconclusive make the comparison not_measurable.

For n discordant votes and w wins, p = sum(C(n,k) * 0.5**n for k in w..n).
At alpha 0.05, 5-7 votes need a clean sweep; 8 tolerate one loss. Five cases
is an eligibility floor. Detecting a true 70% win rate with 80% power needs
roughly 37 discordant votes; resampling does not increase this power.

Alpha applies separately to improvement and regression. The chance of either
firing under the null is up to twice alpha (0.0625 for a five-vote sweep).
The report includes two_sided_alpha.

Exit codes:
  0: nonblocking sizing verdict or compare improved.
  1: sizing defect (eg duplicate ids) or any other compare verdict.
  2: usage/input error, including only one of --before/--after.
The blocking field records the same decision.

Stdlib only; reads criteria and scores without changing them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# Share sibling-file resolution, inconclusive ids, and raw-result aliases
# with hone_common so sizing and scoring read the same evidence.
from hone_common import (
    _raw_llm_score,
    extract_results,
    load_deterministic_scores,
    load_inconclusive_ids,
)

# Minimum distinct test cases before a verdict is meaningful at all. Below this
# no arrangement of wins can reach p <= 0.05 on a one-sided sign test.
DEFAULT_MIN_STIMULI = 5

# Minimum profile diversity for scorable skill/command cases.
DEFAULT_MIN_PROFILES = 2

# Profiles statically known to produce no deterministic composite. Other
# inconclusive cases depend on execution evidence and cannot be excluded here.
# An absent profile counts optimistically as scorable.
NON_SCORABLE_PROFILES = frozenset({"knowledge_extraction"})

# Hooks and scripts score every profile, so NON_SCORABLE_PROFILES does not
# apply. Profiles may change their critical-dimension cap but remain pairable.
# The caller supplies artifact type; unset assumes skill/command scoring.
ALWAYS_SCORABLE_ARTIFACT_TYPES = frozenset({"hook", "script"})

# Every type score_execution.py scores, i.e. the values `--artifact-type`
# accepts and `metadata.artifact_type` in deterministic_scores.json can carry.
ARTIFACT_TYPES = ("skill", "command", "hook", "script")

ALPHA = 0.05

# Treat movements within half the Phase 3 regression threshold (0.1) as ties.
TIE_EPSILON = 0.05

# Round deltas before classification so floating-point representations of
# the same nominal change receive the same verdict. Report that rounded value.
DELTA_DECIMALS = 4

# Known scorer profiles. Unknown values fall through to scorer heuristics
# and, without execution evidence, the execution default.
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
    """Return the first non-null key value as a string id, preserving numeric 0."""
    for key in keys:
        raw = entry.get(key)
        if raw is not None:
            return str(raw)
    return None


def _case_id(case: dict) -> str | None:
    """The case's id as the string compare mode pairs on, or None if absent."""
    return _id_of(case, ("id",))


def _profile_of(case: dict) -> str:
    """Resolve the profile using the scorer's criteria-only rules.

    Use a known test_profile, else the error_handling category, else the artifact
    default. Other categories do not imply distinct scoring profiles.
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
    # Apply both the alpha-derived floor and --min-stimuli.
    alpha_floor = min_discordant_for_alpha(alpha)
    floor = max(min_stimuli, alpha_floor)

    cases = [c for c in (criteria.get("test_cases") or []) if isinstance(c, dict)]
    # Stringify ids for consistent pairing and sorting, including numeric 0.
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
    # Hooks and scripts measure the same dimensions across profiles; profile
    # diversity would suggest a remedy that changes no measured property.
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
        # Low case counts are advisory; duplicate ids block comparison.
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
    """Extract per-test scores from scoring or raw-result payloads.

    Accept per_test as a list, an id-to-score map, or an id-to-record map.
    Normalize mappings to entries for shared type checks. Raw results use
    hone_common's extract_results and _raw_llm_score for key precedence and aliases.
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
        # Prefer the composite; separate lookups preserve fallback for explicit nulls.
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
    """Return ids _load_round marked inconclusive; hand-built payloads may omit them."""
    ids = payload.get("inconclusive") if isinstance(payload, dict) else None
    if not isinstance(ids, (list, set, tuple)):
        return set()
    return {str(test_id) for test_id in ids}


def _scorer_fingerprint(payload: object) -> tuple[bool, str | None]:
    """Return (known, fingerprint).

    An absent key preserves compatibility with hand-built payloads. Disk-loaded
    rounds always carry the key; None means their scorer did not record a fingerprint.
    """
    if not isinstance(payload, dict) or "scorer_fingerprint" not in payload:
        return False, None
    value = payload["scorer_fingerprint"]
    return True, value if isinstance(value, str) and value else None


def check_compare(before: dict, after: dict, alpha: float,
                  before_source: str = "", after_source: str = "") -> dict:
    """Sign-test paired cases scored by the same deterministic logic.

    Reject judge scores, source mismatches, and changed or unknown fingerprints
    as not_measurable. Re-score an older round before comparing across a scorer
    change; adding cases cannot make different measurements comparable.
    """
    before_scores = _scores_by_id(before)
    after_scores = _scores_by_id(after)
    shared = sorted(set(before_scores) & set(after_scores))

    # A newly inconclusive case lost evidence; do not drop it and rule on survivors.
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
    # New after-only cases must not hide recovery of matching inconclusive ids.
    # Name the extra ids in the diagnostic instead.
    no_baseline = not shared and bool(recovered)
    # Judge scores cannot establish change in the deterministic measurement.
    judge_only = before_source == after_source == "results"
    # Both rounds must use the same scoring logic; see scorer_fingerprint.
    before_known, before_fingerprint = _scorer_fingerprint(before)
    after_known, after_fingerprint = _scorer_fingerprint(after)
    fingerprints_checked = before_known or after_known
    # Missing fingerprints mean unknown scorers; re-score before comparing.
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
        # Recovered cases have matching ids but no measured baseline.
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
        # Zero pairs indicate mismatched inputs, which adding cases cannot repair.
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
        # Name recovered cases as a warning when other cases can still be paired.
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
    """Load a JSON object; reject other root types as usage errors."""
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
    """Require a readable file before resolving its deterministic sibling.

    Reject typos and directories even if their parent contains a score file.
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
    """Resolve the deterministic sibling using hone_common's path rule.

    Check presence separately from loaded scores: an existing file with no
    conclusive cases is still a deterministically scored round.
    """
    return Path(path).parent / "deterministic_scores.json"


def _load_round(path: str) -> tuple[dict, str]:
    """Load round scores with their source, inconclusive ids, and scorer metadata.

    Accept deterministic_scores.json or its results.json sibling. Validate the
    deterministic JSON before using tolerant loaders, so corrupt files produce
    exit 2 rather than an empty score set.

    An existing deterministic file remains authoritative even if every composite
    is null. Preserve those inconclusive ids so check_compare can report lost
    evidence. Never substitute judge scores within such a round.

    Without a deterministic file, load the supplied result file for diagnostics
    and input validation. check_compare marks judge-only rounds not_measurable,
    including when both sides use judge scores.

    Carry metadata.artifact_type so sizing uses the type that produced the scores.
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
            # Always include the key: None identifies an old disk-loaded score file
            # whose scorer is unknown.
            "scorer_fingerprint": metadata.get("scorer_fingerprint"),
        }, "deterministic"
    return _load(path), "results"


def _recorded_artifact_type(*rounds: dict) -> tuple[str, str]:
    """Return the rounds' recorded artifact type and any disagreement warning.

    Return ("", "") if neither records a type, or ("", reason) if they disagree.
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
    """Combine sizing and comparison verdicts, with sizing failures first.

    Underpowered sizing permits neither promotion nor auto-revert, even though
    it is advisory in Phase 1. Preserve comparison.verdict in the nested report
    and warn when sizing hides it. A hidden not_measurable verdict needs input
    repair rather than more cases.
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
        # Size using the artifact type recorded by the scorer.
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
        # Compare mode allows action only on improved. Underpowered permits
        # neither promotion nor auto-revert.
        report = {"mode": "compare", "sizing": sizing, "comparison": comparison,
                  "verdict": verdict, "blocking": verdict != "improved"}

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"VERDICT: {report['verdict']}")
        # This advisory applies only to nonblocking, underpowered sizing.
        # Compare improved permits action and must not receive it.
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
