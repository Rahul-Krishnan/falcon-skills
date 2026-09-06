#!/usr/bin/env python3
"""Classify eval checks as outcome, technique, or vocabulary items.

Checks copied from the artifact can reward recitation without testing task
success. Following dotnet/skills create-skill-test, target outcome checks,
minimize internal-procedure checks, and avoid vocabulary checks.

Classification is deterministic: distinctive verbatim overlap is vocabulary;
named scripts, workflow positions, and artifact invocations are technique;
remaining content is outcome.

Prose uses NGRAM_SIZE-word overlap. Literal required_present anchors also
use contiguous normalized overlap at MIN_ANCHOR_WORDS. Wordless decoration
uses a raw match with whitespace removed, excluding generic markdown syntax
and anchors shorter than MIN_MARKUP_CHARS to avoid accidental matches.

Wordless items never count solely toward the denominator: copied decoration
counts as vocabulary; other wordless items are exempt. This prevents empty
anchors from diluting the overfit ratio regardless of their character shape.
required_absent is exempt because it tests that wording is not repeated.

Exit codes: 0 within threshold; 1 over threshold or not_measurable; 2 usage
error. No classifiable items or no artifact words yields not_measurable.
Stdlib only; reports without rewriting criteria.
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

# Literal anchors use a shorter overlap threshold than prose. Exclude
# single-word matches because common tokens collide by accident.
MIN_ANCHOR_WORDS = 2

# Fraction of non-exempt items allowed to be technique or vocabulary before the
# criteria set is considered overfitted.
DEFAULT_MAX_OVERFIT_RATIO = 0.34

# Detect bundled scripts, numbered positions (including Step 6a), and artifact
# invocations. Explicit verb forms cover calls such as "calls the /hone skill"
# without matching ordinary "skill set" prose. Python and shell names ignore
# case; JavaScript names must be lowercase and pass the framework-name filter.
TECHNIQUE_PATTERNS = (
    (re.compile(r"\b[a-z_][a-z0-9_]*\.(?:py|sh)\b", re.I), "names a bundled script"),
    (re.compile(r"\b(?:phase|step|stage)\s+\d+[a-z]?\b", re.I), "names a numbered workflow position"),
    (re.compile(
        r"\b(?:invok(?:e|ed|es|ing)|call(?:s|ed|ing)?|us(?:e|ed|es|ing)"
        r"|run(?:s|ning)?|ran|dispatch(?:es|ed|ing)?|trigger(?:s|ed|ing)?)"
        r"\s+(?:the\s+(?:/?[a-z][a-z0-9_-]*\s+)?|/[a-z][a-z0-9_-]*\s+)?skill\b",
        re.I,
    ), "rewards invoking the skill"),
)

# Lowercase `.js` names, filtered against JS_TECHNOLOGY_NAMES at match time.
# Case-sensitive: `Node.js` in prose is a proper noun, not a bundled script.
JS_SCRIPT = re.compile(r"\b[a-z_][a-z0-9_]*\.js\b")

# Runtimes and frameworks conventionally spelled with `.js`; the ones a
# deliverable description names ("a valid node.js project"), not every library.
JS_TECHNOLOGY_NAMES = frozenset({
    "node", "next", "nuxt", "vue", "react", "angular", "ember", "svelte",
    "express", "three", "d3", "p5", "chart", "moment", "backbone", "knockout",
    "meteor", "alpine", "preact", "pixi", "babylon", "socket", "hapi", "koa",
    "nest", "gatsby", "remix", "astro", "solid", "qwik", "lit", "stimulus",
})

WORD = re.compile(r"[a-z0-9]+")

# Minimum whitespace-stripped length for a wordless anchor match. Shorter
# anchors risk accidental collisions and are excluded from scoring.
MIN_MARKUP_CHARS = 2

# Generic markdown syntax, including mixed forms such as |---| and <!--,
# is exempt from vocabulary matching. Other wordless decoration may match
# as vocabulary; unmatched wordless items never enter the denominator.
MARKDOWN_SYNTAX_CHARS = frozenset("#-*_`>|+~=:<!")

# Validate at import that syntax characters contain no normalized words.
# Otherwise the exemption could admit content into the denominator.
_CONTENTFUL_SYNTAX_CHARS = sorted(
    char for char in MARKDOWN_SYNTAX_CHARS if WORD.findall(char.lower())
)
if _CONTENTFUL_SYNTAX_CHARS:
    raise AssertionError(
        "MARKDOWN_SYNTAX_CHARS members must carry no normalised words, so "
        "that anchors built from them leave the scored set rather than "
        f"padding the denominator: {_CONTENTFUL_SYNTAX_CHARS} do. Drop them "
        "from the set."
    )

# Match distinctive multi-segment names directly. Single-word names such as
# commit or forge need invocation context to avoid flagging ordinary verbs.
NAME_CONTEXT_WORDS = r"skill|command|hook|artifact|invoke[ds]?|invoking|ran|run|use[ds]?|using"


def _normalize(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _carries_content(text: str) -> bool:
    """Return whether normalization yields any words.

    Only word-bearing items may count as outcome in the denominator.
    """
    return bool(_normalize(text))


def _ngrams(words: list[str], size: int) -> set[tuple[str, ...]]:
    if len(words) < size:
        return set()
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def _contains_phrase(haystack: list[str], phrase: list[str]) -> bool:
    """Match contiguous normalized words, tolerating line breaks and separators."""
    span = len(phrase)
    if not span or span > len(haystack):
        return False
    first = phrase[0]
    for index, word in enumerate(haystack):
        if word == first and haystack[index:index + span] == phrase:
            return True
    return False


def _markup_body(stripped: str) -> str:
    """Remove whitespace so layout variants of decoration compare equally."""
    return "".join(stripped.split())


def _is_generic_markdown(stripped: str) -> bool:
    """Return whether all non-whitespace characters are markdown syntax.

    Mixed forms such as |---| and <!-- are exempt too. Non-ASCII decoration
    outside MARKDOWN_SYNTAX_CHARS remains eligible for a vocabulary match.
    """
    body = _markup_body(stripped)
    return bool(body) and set(body) <= MARKDOWN_SYNTAX_CHARS


def _anchor_lift(stripped: str, artifact_text: str,
                 artifact_words: list[str]) -> str:
    """Explain a required_present anchor's verbatim match, or return "".

    Word-bearing anchors require MIN_ANCHOR_WORDS contiguous normalized words.
    Wordless anchors use whitespace-stripped substring matches, excluding generic
    markdown and strings below MIN_MARKUP_CHARS. Unmatched wordless anchors are
    exempted by check_overfit and cannot dilute the denominator.
    """
    if not artifact_text:
        return ""
    if not _carries_content(stripped):
        if _is_generic_markdown(stripped):
            return ""
        if len(_markup_body(stripped)) < MIN_MARKUP_CHARS:
            return ""
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


def _names_a_js_script(text: str) -> str:
    """The first lowercase `.js` script named in `text`, or "" if none is.

    See JS_SCRIPT and JS_TECHNOLOGY_NAMES for why this is not a third entry
    in TECHNIQUE_PATTERNS.
    """
    for match in JS_SCRIPT.finditer(text):
        stem = match.group(0)[:-3]
        if stem not in JS_TECHNOLOGY_NAMES:
            return match.group(0)
    return ""


def _names_the_artifact(text: str, skill_name: str) -> bool:
    """Does `text` reference the artifact under test, rather than use a word?"""
    if not skill_name:
        return False
    name = re.escape(skill_name)
    if "-" in skill_name or "_" in skill_name:
        return bool(re.search(rf"\b{name}\b", text, re.I))
    # Require standalone invocation syntax for single-word names; directory
    # segments such as ~/forge/output.md do not identify the skill.
    patterns = (
        rf"(?<![\w.~/])/{name}\b(?![\w./-])",            # /forge, not ~/forge/x
        rf"`{name}`",                                    # `forge`
        rf"\b(?:{NAME_CONTEXT_WORDS})\s+(?:the\s+)?{name}\b",   # ran forge
        rf"\b{name}\s+(?:{NAME_CONTEXT_WORDS})\b",       # forge skill
    )
    return any(re.search(p, text, re.I) for p in patterns)


def classify_item(text: str, artifact_ngrams: set, skill_name: str,
                  artifact_text: str = "", literal_anchor: bool = False,
                  artifact_words: list[str] | None = None) -> dict:
    """Classify a criteria item and explain why.

    literal_anchor enables the shorter required_present overlap test in addition
    to the prose n-gram test. Pass artifact_words to reuse normalization across
    items; otherwise derive it from artifact_text.
    """
    reasons: list[str] = []
    label = "outcome"

    for pattern, why in TECHNIQUE_PATTERNS:
        match = pattern.search(text)
        if match:
            label = "technique"
            reasons.append(f"{why}: '{match.group(0)}'")

    js_script = _names_a_js_script(text)
    if js_script:
        label = "technique"
        reasons.append(f"names a bundled script: '{js_script}'")

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
    """Return candidate items and the exempt required_absent count.

    Each item carries case_id and location for targeted rewriting, plus
    literal_anchor to select the appropriate match rule. Keep wordless items
    until check_overfit applies its shared denominator guard.
    """
    items: list[dict] = []
    exempt = 0
    for index, case in enumerate(criteria.get("test_cases") or []):
        if not isinstance(case, dict):
            continue
        # An integer id 0 is an id, not a missing one; it is stringified so
        # the flagged entry names the case the way compare mode pairs it.
        case_id = str(case["id"]) if case.get("id") is not None else f"test_cases[{index}]"
        absent = case.get("required_absent")
        exempt += len(absent) if isinstance(absent, list) else 0
        # Require a list: iterating a string would score individual characters.
        present_list = case.get("required_present")
        if not isinstance(present_list, list):
            present_list = []
        for slot, present in enumerate(present_list):
            if not isinstance(present, str):
                continue
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
                # Use only the highest numeric rubric band, which defines scoring well.
                # Without numeric bands, skip the item rather than depend on JSON key order.
                numeric = [k for k in rubric if _as_int(k) >= 0]
                top = max(numeric, key=_as_int, default=None)
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
    exempt_contentless = 0
    for item in items:
        entry = classify_item(item["text"], artifact_ngrams, skill_name,
                              artifact_text, item["literal_anchor"],
                              artifact_words)
        # Apply the word-content invariant to every item, including descriptions and
        # rubrics. Copied decoration counts in both parts of the ratio; other wordless
        # items are exempt and cannot lower it.
        if entry["class"] == "outcome" and not _carries_content(item["text"]):
            exempt_contentless += 1
            continue
        entry["case_id"] = item["case_id"]
        entry["location"] = item["location"]
        classified.append(entry)
    counts = {"outcome": 0, "technique": 0, "vocabulary": 0}
    for item in classified:
        counts[item["class"]] += 1

    total = len(classified)
    overfit = counts["technique"] + counts["vocabulary"]

    # No classifiable items or no artifact words means not_measurable.
    # An empty ratio must not clear the gate as a perfect result.
    if not artifact_words:
        verdict = "not_measurable"
        ratio = None
        reason = ("the artifact carries no words, so every lift test compared "
                  "against nothing")
    elif total == 0:
        verdict = "not_measurable"
        ratio = None
        reason = "no classifiable items; nothing was measured"
    else:
        ratio = round(overfit / total, 4)
        verdict = "within_threshold" if ratio <= max_ratio else "overfitted"
        reason = ""

    return {
        "verdict": verdict,
        "reason": reason,
        "skill_name": skill_name,
        "items_classified": total,
        "items_exempt_required_absent": exempt,
        # Count exempt wordless items; copied decoration remains vocabulary.
        "items_exempt_contentless": exempt_contentless,
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
        required=True,
        help="Path to the artifact under test: the SKILL.md (or command/hook/"
             "script file) the criteria were written for, as Step 1 discovery "
             "resolved it",
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

    # Require an object before reading criteria fields.
    if not isinstance(criteria, dict):
        print(
            f"ERROR: criteria file root must be a JSON object, got "
            f"{type(criteria).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Reject non-string names as usage errors before regex matching.
    skill_name = criteria.get("skill_name")
    if skill_name is not None and not isinstance(skill_name, str):
        print(
            f"ERROR: criteria file 'skill_name' must be a string, got "
            f"{type(skill_name).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)
    skill_name = skill_name or ""
    # No guessed default. `~/.claude/skills/<name>/SKILL.md` is the path
    # phase1-evaluation.md says never to hardcode (wrong for plugin installs),
    # and a stale copy there produced a within_threshold verdict against a
    # file that was not the artifact under test, with nothing on stderr.
    artifact_path = args.artifact

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
            print(f"VERDICT: {report['verdict']} ({report['reason']})")
        else:
            print(f"VERDICT: {report['verdict']} (ratio {report['overfit_ratio']}, "
                  f"max {report['max_overfit_ratio']})")
        print(f"  {report['items_classified']} item(s): "
              f"{counts['outcome']} outcome, {counts['technique']} technique, "
              f"{counts['vocabulary']} vocabulary "
              f"({report['items_exempt_required_absent']} required_absent, "
              f"{report['items_exempt_contentless']} contentless exempt)")
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
