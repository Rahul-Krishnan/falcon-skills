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

Both anchor tests carry a collision guard, because an anchor that matches the
artifact by accident is not recitation. MIN_ANCHOR_WORDS is the guard on the
word side; MARKDOWN_SYNTAX_CHARS is its twin on the markup side, exempting
generic markdown syntax (`##`, `---`, `|---|`) that every markdown artifact
contains by construction.

THE DENOMINATOR INVARIANT, which is what closes the dilution exploit for
good (see `_carries_content` and the one guard in `check_overfit` that
enforces it): an item yielding no normalised words carries no measurable
content, and no such item is ever counted `outcome`. It is either a lift --
artifact-specific decoration reproduced verbatim, counted in the numerator
and the denominator alike -- or it leaves the scored set entirely, the way a
`required_absent` entry does. It is never a bare denominator seat, so no
quantity of it moves the ratio.

The invariant is stated once, over `_normalize`'s output, and deliberately
not over any decoration SHAPE. Three earlier fixes each matched a shape --
identifier-like anchors, then short verbatim prose, then underscore runs --
and each left the next shape seated in the denominator: `"| --- |"` and
`"- - -"` carry internal whitespace, `"█"` is a single character, and all of
them padded a ratio-1.0 criteria set down to `within_threshold`. Shape rules
have to enumerate decoration and decoration is unbounded; "does this item
contain a word" is a property of the item, so there is no next shape.

`required_absent` lists are exempt by construction. They assert the artifact's
own vocabulary must NOT appear in output, which is the opposite failure mode
and a legitimate use of lifted phrasing.

Exit codes: 0 within threshold, 1 over threshold or not measurable, 2 usage
error. `not_measurable` is the verdict when nothing could be compared -- the
criteria set yields zero classifiable items, or the artifact carries no words
to compare them against. Nothing was measured, so nothing passed.

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
#
# The step rule admits a letter suffix (`Step 6a`) because hone's own workflow
# numbers sub-steps that way, and a check reciting one is as much a recitation
# as `Step 6`. The script rule is case-insensitive for the same reason a
# capitalised `Score_Execution.py` is still the script. The invocation rule
# spells out its verb forms rather than tacking `d|s|ing` onto stems (which
# produced `invokeing` and never matched `called`), and allows one name token
# between the article and `skill` so `calls the /hone skill` is caught
# (only after `the` or with a leading slash, so `calls their skill set` is not).
#
# The script rule is split by extension. `.py` and `.sh` names are scripts
# whatever their case. `.js` is also how JavaScript technologies are spelled
# (`Node.js`, `Next.js`, `vue.js`), and a check that says "a valid Node.js
# project" is an outcome check about the deliverable, not a recitation of a
# bundled script; matching it pushed every JS-oriented suite over the
# threshold with nothing legitimately rewritable. So a `.js` name counts only
# when it is lowercase and is not a well-known runtime or framework.
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

# Minimum non-whitespace characters for a contentless anchor to be TESTED as a
# verbatim lift. This is the markup side's collision guard, the twin of
# MIN_ANCHOR_WORDS: a lone `#` or `█` occurs in almost any artifact by
# accident, and calling that accident recitation inflates the very ratio being
# gated. Below the floor an anchor is not a lift -- and, carrying no words, it
# is not a denominator seat either (the invariant above), so it simply leaves
# the scored set. Counting whitespace-stripped characters, because whitespace
# is not a shape: `"|---|"` and `"| --- |"` are the same anchor and the fix
# that treated them differently is the one this replaces.
MIN_MARKUP_CHARS = 2

# Characters that carry markdown structure rather than content. A bare-markup
# anchor that is a run of a single one of them (`##`, `---`, ```` ``` ````) is
# markdown syntax, not the artifact's vocabulary: it occurs in every markdown
# artifact by construction, so matching it against the artifact says nothing
# about whether the artifact was recited. This is the markup-side twin of
# MIN_ANCHOR_WORDS, which exists on the word side for the same reason -- a
# single common token collides with almost any artifact by accident, and
# flagging the collision inflates the very ratio being gated. Without it the
# rule flagged `"##"`, which phase1-evaluation.md recommends as the minimal
# structural check, so the classifier reported the practice its own docs
# prescribe as recitation. The set covers the characters markdown builds
# tables, rules, arrows, comments and emphasis from, so mixed-character
# structure (`|---|`, `|:--|`, `->`, `<!--`) is exempt as well as a run of
# one. Decoration outside the set (`▓▒░`) is artifact-specific and still
# counts as a lift. Either way the anchor carries no words, so the invariant
# above already keeps it out of the denominator; this set only decides
# whether it is additionally counted as recitation.
MARKDOWN_SYNTAX_CHARS = frozenset("#-*_`>|+~=:<!")

# Every character the set claims must actually be content-free, or the set
# lies about what it exempts: a member that `_normalize` reads as a word (a
# letter or a digit slipped into the set) would make runs of it scorable
# items and reintroduce a shape whose treatment depends on its characters.
# The invariant is checked at import rather than discovered at a gate a phase
# later. test_check_overfit.py asserts the end-to-end property, once per
# character.
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


def _carries_content(text: str) -> bool:
    """Does this item carry anything the classifier can measure?

    One word is enough; none is not. This is the whole of the denominator
    invariant stated at the top of the module, and it is the ONLY test that
    decides whether an item may occupy a denominator seat as `outcome`. It
    looks at `_normalize`'s output and nothing else -- not at which characters
    the item is built from, not at how many of them there are, not at whether
    they are separated by whitespace -- because every previous version of this
    rule was a character-shape test and every one of them was walked around by
    the next shape (`"___"`, then `"| --- |"` and `"- - -"`, then `"█"`).
    """
    return bool(_normalize(text))


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


def _markup_body(stripped: str) -> str:
    """`stripped` with all whitespace removed.

    Whitespace inside decoration is layout, not content: `"|---|"`,
    `"| --- |"` and `"|  ---  |"` are one anchor written three ways. The rule
    they walked around treated them as three shapes and admitted two of them
    to the denominator, so every test below reads this body rather than the
    raw string.
    """
    return "".join(stripped.split())


def _is_generic_markdown(stripped: str) -> bool:
    """Is this contentless anchor markdown syntax rather than artifact wording?

    True when every non-whitespace character is a structural one (`##`,
    `---`, `|---|`, `| --- |`, `->`, `<!--`). See MARKDOWN_SYNTAX_CHARS for
    why those are exempt from the lift test. The earlier test, a run of ONE
    structural character, exempted `---` and then flagged the table separator
    `|---|` built from the same characters, so a hand-written "outputs a
    table" check against any artifact with a table in it was reported as a
    verbatim lift. Non-ASCII decoration (`▓▒░`) shares no character with the
    set and stays a lift.
    """
    body = _markup_body(stripped)
    return bool(body) and set(body) <= MARKDOWN_SYNTAX_CHARS


def _anchor_lift(stripped: str, artifact_text: str,
                 artifact_words: list[str]) -> str:
    """Why this `required_present` anchor is a verbatim lift, or "" if it is not.

    Two branches, split on the one property that matters: whether the anchor
    carries words at all (`_carries_content`). A contentless anchor (`▓▒░`,
    `▓ ▒ ░`) has nothing to compare word-wise, so it is matched as a raw
    substring, minus the generic markdown syntax `_is_generic_markdown`
    exempts and minus runs shorter than MIN_MARKUP_CHARS. Everything else is
    matched on its normalised words, and only at MIN_ANCHOR_WORDS or more.
    Below either bar an anchor collides with almost any artifact by accident,
    and flagging the collision would inflate the very ratio being gated.

    Whichever branch runs, a contentless anchor that is NOT a lift stays out
    of the denominator: `check_overfit` exempts it rather than seating it as
    `outcome`. That is why widening or narrowing this function cannot reopen
    the dilution exploit -- it decides recitation, never denominator size.
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
    # Single-word name: require a marker that the word is the artifact. The
    # slash form is the skill-invocation spelling, so it must stand alone: a
    # `/forge` that is a segment of a longer path (`~/forge/output.md`,
    # `src/forge/cli`) is a directory the skill happens to share a name with,
    # and matching it there flagged every outcome check that named the
    # skill's own output paths.
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
    """Return (candidate items, exempt `required_absent` count).

    Each item carries where it came from as well as what it says. Step 6a
    tells the agent to rewrite the flagged items, and a flagged item with no
    test case and no field behind it is not addressable: two cases can carry
    the same check text, and a truncated rubric band names nothing at all.
    `case_id` and `location` are what make a flagged entry findable in the
    criteria file.

    `literal_anchor` travels with the item because `required_present` entries
    are literal match anchors and the rest are prose; they need different lift
    tests. See classify_item.

    Nothing is exempted HERE on the strength of its shape any more. This
    function used to drop generic-markdown anchors before classification,
    which meant the exemption and the lift test each had their own idea of
    what "markup" looks like, and an anchor the two disagreed about (`"|
    --- |"`, `"█"`) fell between them into the denominator. Contentless items
    are now carried through classification and settled in one place, by
    `check_overfit`'s single denominator guard.
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
        # A string here is malformed, not a one-anchor list: iterated, each
        # character became an `outcome` item and sixteen of them cleared the
        # gate at 0.0 (Step 6a runs before the Step 8 validate gate).
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
                # Only the top band matters: it defines what scoring well means.
                # Rank numeric keys only: `max` over `_as_int` tied every
                # non-numeric key at -1 and picked by JSON key order, so
                # `{"excellent": ..., "poor": ...}` flipped class on
                # reserialisation. No numeric band, no item.
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
        # THE DENOMINATOR GUARD. The one place an item's membership of the
        # scored set is decided, and the whole enforcement of the invariant at
        # the top of this module: an item with no normalised words carries no
        # measurable content, so it may not sit in the denominator as
        # `outcome`. If classification found recitation in it (decoration
        # lifted verbatim) it stays, counted in numerator and denominator
        # alike; otherwise it leaves the scored set the way a `required_absent`
        # entry does. Either way, adding more of it cannot move the ratio down.
        #
        # Deliberately applied to every item, not only to `required_present`
        # anchors: a check description or rubric band of `"---"` measures
        # exactly as much as an anchor of `"---"` does, which is nothing, and
        # scoping the earlier fixes to anchors is part of why each one left a
        # door open.
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

    # Neither zero classifiable items nor a wordless artifact is a pass.
    #
    # `overfit / total` guarded with an `else 0.0` made an empty ratio
    # indistinguishable from a perfect one, so a criteria set whose cases carry
    # only exempt entries (`required_absent` lists, contentless decoration)
    # or no test cases at all cleared Step 6a's mandatory gate silently, on no
    # evidence. A set padded entirely with `"| --- |"` lands here: every entry
    # is exempt, nothing is classified, and the verdict is `not_measurable`
    # with exit 1 rather than a diluted pass.
    #
    # An empty or wordless artifact is the same hole on the other side of the
    # comparison: `_ngrams` of it is empty and `_anchor_lift` returns "" on its
    # first line, so every item classifies `outcome` and the ratio is 0.0. A
    # `--artifact` pointed at a truncated or empty file therefore cleared the
    # gate while comparing against nothing.
    #
    # `not_measurable` says what actually happened in both cases, and exits
    # non-zero so the gate cannot read it as within threshold.
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
        # Renamed from `items_exempt_generic_markdown`: the exemption is no
        # longer "this looks like markdown" but "this carries no words", which
        # covers generic markdown and every other decoration shape alike.
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

    # A list- or scalar-rooted criteria file reaches `.get()` below and raises
    # an uncaught AttributeError; the contract here is exit 2.
    if not isinstance(criteria, dict):
        print(
            f"ERROR: criteria file root must be a JSON object, got "
            f"{type(criteria).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)

    # `or ""` accepted any truthy value, and a non-string one reached
    # `re.escape` in `_names_the_artifact` as an uncaught TypeError: exit 1
    # with a traceback, which Step 6a reads as `overfitted` -- a gate failure
    # blamed on the criteria rather than the malformed field it came from.
    # Every other malformed-input path here exits 2 and names the field.
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
