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

`required_absent` lists are exempt by construction. They assert the artifact's
own vocabulary must NOT appear in output, which is the opposite failure mode
and a legitimate use of lifted phrasing.

Exit codes: 0 within threshold, 1 over threshold, 2 usage error.

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


def _normalize(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _ngrams(words: list[str], size: int) -> set[tuple[str, ...]]:
    if len(words) < size:
        return set()
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def classify_item(text: str, artifact_ngrams: set, skill_name: str) -> dict:
    """Classify one criteria item and explain the classification."""
    reasons: list[str] = []
    label = "outcome"

    for pattern, why in TECHNIQUE_PATTERNS:
        match = pattern.search(text)
        if match:
            label = "technique"
            reasons.append(f"{why}: '{match.group(0)}'")

    if skill_name and re.search(rf"\b{re.escape(skill_name)}\b", text, re.I):
        label = "technique"
        reasons.append(f"names the artifact under test: '{skill_name}'")

    overlap = _ngrams(_normalize(text), NGRAM_SIZE) & artifact_ngrams
    if overlap:
        label = "vocabulary"
        sample = " ".join(sorted(overlap)[0])
        reasons.append(f"{len(overlap)} phrase(s) lifted from the artifact: '{sample}'")

    return {"text": text[:160], "class": label, "reasons": reasons}


def _collect_items(criteria: dict) -> tuple[list[str], int]:
    """Return (scored item texts, count of exempt required_absent entries)."""
    items: list[str] = []
    exempt = 0
    for case in criteria.get("test_cases") or []:
        if not isinstance(case, dict):
            continue
        exempt += len(case.get("required_absent") or [])
        for present in case.get("required_present") or []:
            if isinstance(present, str):
                items.append(present)
        for check in case.get("checks") or []:
            if not isinstance(check, dict):
                continue
            description = check.get("description")
            if isinstance(description, str):
                items.append(description)
            rubric = check.get("rubric")
            if isinstance(rubric, dict):
                # Only the top band matters: it defines what scoring well means.
                top = max(rubric, key=lambda k: _as_int(k), default=None)
                if top is not None and isinstance(rubric[top], str):
                    items.append(rubric[top])
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

    classified = [classify_item(t, artifact_ngrams, skill_name) for t in items]
    counts = {"outcome": 0, "technique": 0, "vocabulary": 0}
    for item in classified:
        counts[item["class"]] += 1

    total = len(classified)
    overfit = counts["technique"] + counts["vocabulary"]
    ratio = (overfit / total) if total else 0.0
    within = ratio <= max_ratio

    return {
        "verdict": "within_threshold" if within else "overfitted",
        "skill_name": skill_name,
        "items_classified": total,
        "items_exempt_required_absent": exempt,
        "counts": counts,
        "overfit_ratio": round(ratio, 4),
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
