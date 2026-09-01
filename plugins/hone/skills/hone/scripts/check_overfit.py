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
literal match anchor, not prose, and the whole entry is the phrase, so it gets
a second, stricter test on top of the n-gram one: an anchor whose words appear
contiguously in the artifact is a lift at MIN_ANCHOR_WORDS words, not at
NGRAM_SIZE. Without it the anchors Step 6 enrichment lifts from the artifact
were invisible here -- classified `outcome`, padding the denominator so that
enriching a set *lowered* its ratio. Covering only identifier- and
markup-shaped anchors left the same hole open one step further out: short
verbatim prose anchors ("gate event", "state file") still diluted the
denominator, so the gate could be cleared by adding more recitation checks.

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

# Word count for a `required_present` anchor to count as "lifted". The bar is
# far lower than NGRAM_SIZE because an anchor is not prose: it is matched
# literally against output, so an anchor reproduced verbatim from the artifact
# asks whether the artifact was recited, whatever its length. One word is
# excluded because a single common token ("OK", "id", "42") collides with
# almost any artifact by accident; two words reproduced in order do not.
MIN_ANCHOR_WORDS = 2

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

# Bare markup (`##`, `---`) carries no words at all, so the word-sequence test
# below can never see it; it is matched as a raw substring instead. Identifier
# anchors (`validate_handoff`, `gate-compliance`) need no special case: they
# normalise to two or more words and the word-sequence test catches them,
# including across a separator swap between the anchor and the artifact.
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


def _contains_phrase(haystack: list[str], phrase: list[str]) -> bool:
    """Does `phrase` appear as a contiguous run inside `haystack`?

    Both sides are normalised word lists, so the test survives the formatting
    the same phrase picks up in prose -- a line break inside it, a separator
    swap between `gate_compliance` and `gate compliance`, a trailing comma.
    A raw substring test misses all three, and an anchor that evades detection
    by a line break is an anchor that dilutes the ratio for free.
    """
    span = len(phrase)
    if not span or span > len(haystack):
        return False
    first = phrase[0]
    for index, word in enumerate(haystack):
        if word == first and haystack[index:index + span] == phrase:
            return True
    return False


def _anchor_lift(stripped: str, artifact_text: str,
                 artifact_words: list[str]) -> str:
    """Why this `required_present` anchor is a verbatim lift, or "" if it is not.

    Two shapes, because one of them has no words to compare. Bare markup
    (`##`) is matched as a raw substring; everything else is matched on its
    normalised words, and only at MIN_ANCHOR_WORDS or more. Below that bar an
    anchor is a common token that collides with almost any artifact by
    accident, and flagging it would inflate the very ratio being gated.
    """
    if not artifact_text:
        return ""
    if MARKUP_ANCHOR.match(stripped):
        if stripped.lower() in artifact_text.lower():
            return f"markup anchor lifted verbatim from the artifact: '{stripped}'"
        return ""
    words = _normalize(stripped)
    if len(words) >= MIN_ANCHOR_WORDS and _contains_phrase(artifact_words, words):
        return (
            f"{len(words)}-word literal anchor lifted verbatim from the "
            f"artifact: '{stripped}'"
        )
    return ""


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
                  artifact_text: str = "", literal_anchor: bool = False,
                  artifact_words: list[str] | None = None) -> dict:
    """Classify one criteria item and explain the classification.

    `literal_anchor` marks a `required_present` entry. Such an entry is
    matched verbatim against output, so the whole entry is the phrase and it
    gets the MIN_ANCHOR_WORDS test *in addition to* the NGRAM_SIZE one below.
    The n-gram rule is not skipped for anchors -- it stays the rule that
    catches a long lifted sentence pasted into `required_present` -- it is
    simply never the rule that fires first on a short one.

    `artifact_words` is the artifact's normalised word list. It is passed in
    so the whole criteria set shares one pass over the artifact; omit it and
    it is derived from `artifact_text`.
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

    if literal_anchor:
        if artifact_words is None:
            artifact_words = _normalize(artifact_text)
        lift = _anchor_lift(text.strip(), artifact_text, artifact_words)
        if lift:
            label = "vocabulary"
            reasons.append(lift)

    overlap = _ngrams(_normalize(text), NGRAM_SIZE) & artifact_ngrams
    if overlap:
        label = "vocabulary"
        sample = " ".join(sorted(overlap)[0])
        reasons.append(f"{len(overlap)} phrase(s) lifted from the artifact: '{sample}'")

    return {"text": text, "class": label, "reasons": reasons}


def _collect_items(criteria: dict) -> tuple[list[dict], int]:
    """Return (scored items, count of exempt `required_absent` entries).

    Each item carries where it came from as well as what it says. Step 6a
    tells the agent to rewrite the flagged items, and a flagged item with no
    test case and no field behind it is not addressable: two cases can carry
    the same check text, and a truncated rubric band names nothing at all.
    `case_id` and `location` are what make a flagged entry findable in the
    criteria file.

    `literal_anchor` travels with the item because `required_present` entries
    are literal match anchors and the rest are prose; they need different lift
    tests. See classify_item.
    """
    items: list[dict] = []
    exempt = 0
    for index, case in enumerate(criteria.get("test_cases") or []):
        if not isinstance(case, dict):
            continue
        case_id = case.get("id") or f"test_cases[{index}]"
        exempt += len(case.get("required_absent") or [])
        for slot, present in enumerate(case.get("required_present") or []):
            if isinstance(present, str):
                items.append({"text": present, "literal_anchor": True,
                              "case_id": case_id,
                              "location": f"required_present[{slot}]"})
        for slot, check in enumerate(case.get("checks") or []):
            if not isinstance(check, dict):
                continue
            description = check.get("description")
            if isinstance(description, str):
                items.append({"text": description, "literal_anchor": False,
                              "case_id": case_id,
                              "location": f"checks[{slot}].description"})
            rubric = check.get("rubric")
            if isinstance(rubric, dict):
                # Only the top band matters: it defines what scoring well means.
                top = max(rubric, key=lambda k: _as_int(k), default=None)
                if top is not None and isinstance(rubric[top], str):
                    items.append({"text": rubric[top], "literal_anchor": False,
                                  "case_id": case_id,
                                  "location": f"checks[{slot}].rubric[{top}]"})
    return items, exempt


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def check_overfit(criteria: dict, artifact_text: str, skill_name: str,
                  max_ratio: float) -> dict:
    artifact_words = _normalize(artifact_text)
    artifact_ngrams = _ngrams(artifact_words, NGRAM_SIZE)
    items, exempt = _collect_items(criteria)

    classified = []
    for item in items:
        entry = classify_item(item["text"], artifact_ngrams, skill_name,
                              artifact_text, item["literal_anchor"],
                              artifact_words)
        entry["case_id"] = item["case_id"]
        entry["location"] = item["location"]
        classified.append(entry)
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
            # Truncate for the terminal only. The JSON carries the item whole,
            # because that is the copy the agent rewrites against.
            text = item["text"]
            if len(text) > 160:
                text = text[:157] + "..."
            print(f"  [{item['class'].upper()}] {item['case_id']} "
                  f"{item['location']}: {text}")
            for reason in item["reasons"]:
                print(f"      {reason}")

    sys.exit(0 if report["verdict"] == "within_threshold" else 1)


if __name__ == "__main__":
    main()
