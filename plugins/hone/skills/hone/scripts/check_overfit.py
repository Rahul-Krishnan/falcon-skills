#!/usr/bin/env python3
"""Classify hone eval criteria as outcome, technique, or vocabulary items.

Phase 1 Step 6 enriches criteria with checks derived from the artifact text.
That is efficient and it is also how an eval stops measuring anything: a check
lifted from the artifact asks whether the artifact was recited, not whether the
task was accomplished. An artifact that grows a section and a matching check
scores better every round while behaving identically.

Three classes, borrowed from the overfitting judge in dotnet/skills
create-skill-test:

  outcome     Did the agent reach a correct result? WHAT, not HOW. Target this.
  technique   Did the agent follow a named internal procedure? Minimize.
  vocabulary  Did the agent echo the artifact's wording? Avoid.

Detection is deterministic, not judged. A check is `vocabulary` when a
distinctive phrase in it appears verbatim in the artifact; `technique` when it
names internal machinery (a bundled script, a numbered phase or step, or the
artifact itself). Everything else is `outcome`.

"Distinctive phrase" is tested two ways, because criteria hold two kinds of
item. Prose (check descriptions, rubric bands) is tested by NGRAM_SIZE-word
overlap, so ordinary English is not flagged. A `required_present` entry is a
literal match anchor, not prose, and the whole entry is the phrase: an
enrichment-shaped anchor (an identifier or bare markup) present verbatim in
the artifact is a lift however short it is. Without that second test the
identifiers Step 6 enrichment lifts from the artifact were invisible here --
classified `outcome`, and padding the denominator so that enriching a set
*lowered* its ratio.

`required_absent` lists are exempt by construction. They assert the artifact's
own vocabulary must NOT appear in output, which is the opposite failure mode
and a legitimate use of lifted phrasing.

Exit codes: 0 within threshold, 1 over threshold or not measurable, 2 usage
error. `not_measurable` is the verdict when the criteria set yields zero
classifiable items -- nothing was measured, so nothing passed.

Stdlib only. Read-only: it reports, it never rewrites criteria.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Word count for a phrase to count as "lifted". Shorter overlaps are ordinary
# English and flagging them would classify every well-written check as
# vocabulary.
NGRAM_SIZE = 6

# Fraction of non-exempt items allowed to be technique or vocabulary before the
# criteria set is considered overfitted.
DEFAULT_MAX_OVERFIT_RATIO = 0.34

# Internal machinery: a bundled script, a numbered workflow position, or an
# explicit instruction to use the artifact. These make a check measure
# adherence rather than outcome.
TECHNIQUE_PATTERNS = (
    (re.compile(r"\b[a-z_][a-z0-9_]*\.(?:py|sh|js)\b"), "names a bundled script"),
    (re.compile(r"\b(?:phase|step|stage)\s+\d+\b", re.I), "names a numbered workflow position"),
    (re.compile(r"\b(?:invoke|call|use|run|dispatch)(?:d|s|ing)?\s+(?:the\s+)?/?[a-z-]*skill\b", re.I),
     "rewards invoking the skill"),
)

WORD = re.compile(r"[a-z0-9]+")

# An anchor is enrichment-shaped when it is a single token that no one would
# write as prose: an underscore/hyphen identifier (`validate_handoff`,
# `gate-compliance`) or bare markup (`##`). enrich_programmatic_checks.py only
# ever emits the first kind -- its IDENTIFIER_RE requires an underscore -- so
# these two shapes cover its entire output. Ordinary short anchors ("OK", "id",
# "42") deliberately do not match: a literal assertion on a common token is not
# a vocabulary lift, and flagging it would inflate the very ratio being gated.
IDENTIFIER_ANCHOR = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)+$", re.I)
MARKUP_ANCHOR = re.compile(r"^[^\w\s]{2,}$")

# The skill-name rule fires on a bare name only when the name is itself
# distinctive (multi-segment, like `temper-rework`: no English sentence
# contains it by accident). A single-word name is matched only in a
# technique-signalling context, because a skill named `commit` or `forge`
# otherwise reclassifies every genuine outcome check that uses the ordinary
# verb, pushing the ratio over the threshold with nothing the author can
# legitimately rewrite.
NAME_CONTEXT_WORDS = r"skill|command|hook|artifact|invoke[ds]?|invoking|ran|run|use[ds]?|using"


def _normalize(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _ngrams(words: list[str], size: int) -> set[tuple[str, ...]]:
    if len(words) < size:
        return set()
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def _names_the_artifact(text: str, skill_name: str) -> bool:
    """Does `text` reference the artifact under test, rather than use a word?"""
    if not skill_name:
        return False
    name = re.escape(skill_name)
    if "-" in skill_name or "_" in skill_name:
        return bool(re.search(rf"\b{name}\b", text, re.I))
    # Single-word name: require a marker that the word is the artifact.
    patterns = (
        rf"/{name}\b",                                   # /forge
        rf"`{name}`",                                    # `forge`
        rf"\b(?:{NAME_CONTEXT_WORDS})\s+(?:the\s+)?{name}\b",   # ran forge
        rf"\b{name}\s+(?:{NAME_CONTEXT_WORDS})\b",       # forge skill
    )
    return any(re.search(p, text, re.I) for p in patterns)


def classify_item(text: str, artifact_ngrams: set, skill_name: str,
                  artifact_text: str = "", literal_anchor: bool = False) -> dict:
    """Classify one criteria item and explain the classification.

    `literal_anchor` marks a `required_present` entry: matched verbatim
    against output, so the whole entry is the phrase and the n-gram rule does
    not apply to it. See the module docstring for why that second test exists.
    """
    reasons: list[str] = []
    label = "outcome"

    for pattern, why in TECHNIQUE_PATTERNS:
        match = pattern.search(text)
        if match:
            label = "technique"
            reasons.append(f"{why}: '{match.group(0)}'")

    if _names_the_artifact(text, skill_name):
        label = "technique"
        reasons.append(f"names the artifact under test: '{skill_name}'")

    stripped = text.strip()
    is_enrichment_shaped = literal_anchor and (
        IDENTIFIER_ANCHOR.match(stripped) or MARKUP_ANCHOR.match(stripped)
    )
    if is_enrichment_shaped and artifact_text and stripped.lower() in artifact_text.lower():
        label = "vocabulary"
        reasons.append(f"literal anchor lifted verbatim from the artifact: '{stripped}'")

    overlap = _ngrams(_normalize(text), NGRAM_SIZE) & artifact_ngrams
    if overlap:
        label = "vocabulary"
        sample = " ".join(sorted(overlap)[0])
        reasons.append(f"{len(overlap)} phrase(s) lifted from the artifact: '{sample}'")

    return {"text": text[:160], "class": label, "reasons": reasons}


def _collect_items(criteria: dict) -> tuple[list[tuple[str, bool]], int]:
    """Return (scored items as (text, is_literal_anchor), exempt count).

    The flag travels with the item because `required_present` entries are
    literal match anchors and the rest are prose; they need different lift
    tests. See classify_item.
    """
    items: list[tuple[str, bool]] = []
    exempt = 0
    for case in criteria.get("test_cases") or []:
        if not isinstance(case, dict):
            continue
        exempt += len(case.get("required_absent") or [])
        for present in case.get("required_present") or []:
            if isinstance(present, str):
                items.append((present, True))
        for check in case.get("checks") or []:
            if not isinstance(check, dict):
                continue
            description = check.get("description")
            if isinstance(description, str):
                items.append((description, False))
            rubric = check.get("rubric")
            if isinstance(rubric, dict):
                # Only the top band matters: it defines what scoring well means.
                top = max(rubric, key=lambda k: _as_int(k), default=None)
                if top is not None and isinstance(rubric[top], str):
                    items.append((rubric[top], False))
    return items, exempt


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def check_overfit(criteria: dict, artifact_text: str, skill_name: str,
                  max_ratio: float) -> dict:
    artifact_ngrams = _ngrams(_normalize(artifact_text), NGRAM_SIZE)
    items, exempt = _collect_items(criteria)

    classified = [
        classify_item(text, artifact_ngrams, skill_name, artifact_text, literal)
        for text, literal in items
    ]
    counts = {"outcome": 0, "technique": 0, "vocabulary": 0}
    for item in classified:
        counts[item["class"]] += 1

    total = len(classified)
    overfit = counts["technique"] + counts["vocabulary"]

    # Zero classifiable items is not a pass. `overfit / total` guarded with an
    # `else 0.0` made an empty ratio indistinguishable from a perfect one, so
    # a criteria set whose cases carry only `required_absent` lists (exempt by
    # construction) or no test cases at all cleared Step 6a's mandatory gate
    # silently, on no evidence. `not_measurable` says what actually happened,
    # and exits non-zero so the gate cannot read it as within threshold.
    if total == 0:
        verdict = "not_measurable"
        ratio = None
    else:
        ratio = round(overfit / total, 4)
        verdict = "within_threshold" if ratio <= max_ratio else "overfitted"

    return {
        "verdict": verdict,
        "skill_name": skill_name,
        "items_classified": total,
        "items_exempt_required_absent": exempt,
        "counts": counts,
        "overfit_ratio": ratio,
        "max_overfit_ratio": max_ratio,
        "flagged": [i for i in classified if i["class"] != "outcome"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify hone eval criteria as outcome/technique/vocabulary"
    )
    parser.add_argument("criteria_file", help="Path to eval_criteria.json")
    parser.add_argument(
        "--artifact",
        help="Path to the artifact under test (default: inferred from criteria)",
    )
    parser.add_argument(
        "--max-ratio",
        type=float,
        default=DEFAULT_MAX_OVERFIT_RATIO,
        help=f"Allowed overfit ratio (default: {DEFAULT_MAX_OVERFIT_RATIO})",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    try:
        with open(args.criteria_file) as handle:
            criteria = json.load(handle)
    except FileNotFoundError:
        print(f"ERROR: criteria file not found: {args.criteria_file}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"ERROR: criteria file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        # A directory or an unreadable path is a usage error with the
        # documented exit 2, not a traceback. Matches check_eval_power._load.
        print(f"ERROR: cannot read criteria file {args.criteria_file}: {exc}",
              file=sys.stderr)
        sys.exit(2)

    # A list- or scalar-rooted criteria file reaches `.get()` below and raises
    # an uncaught AttributeError; the contract here is exit 2.
    if not isinstance(criteria, dict):
        print(
            f"ERROR: criteria file root must be a JSON object, got "
            f"{type(criteria).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)

    skill_name = criteria.get("skill_name") or ""
    artifact_path = args.artifact
    if not artifact_path and skill_name:
        guess = Path.home() / ".claude" / "skills" / skill_name / "SKILL.md"
        artifact_path = str(guess) if guess.exists() else None
    if not artifact_path:
        print(
            "ERROR: no artifact found; pass --artifact with the path to the "
            "SKILL.md (or command/hook/script) the criteria were written for",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        artifact_text = Path(artifact_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"ERROR: cannot read artifact {artifact_path}: {exc}", file=sys.stderr)
        sys.exit(2)

    report = check_overfit(criteria, artifact_text, skill_name, args.max_ratio)
    report["artifact"] = artifact_path

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        counts = report["counts"]
        if report["overfit_ratio"] is None:
            print(f"VERDICT: {report['verdict']} (no classifiable items; "
                  f"nothing was measured)")
        else:
            print(f"VERDICT: {report['verdict']} (ratio {report['overfit_ratio']}, "
                  f"max {report['max_overfit_ratio']})")
        print(f"  {report['items_classified']} item(s): "
              f"{counts['outcome']} outcome, {counts['technique']} technique, "
              f"{counts['vocabulary']} vocabulary "
              f"({report['items_exempt_required_absent']} required_absent exempt)")
        for item in report["flagged"]:
            print(f"  [{item['class'].upper()}] {item['text']}")
            for reason in item["reasons"]:
                print(f"      {reason}")

    sys.exit(0 if report["verdict"] == "within_threshold" else 1)


if __name__ == "__main__":
    main()
