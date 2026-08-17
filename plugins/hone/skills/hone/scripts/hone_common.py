#!/usr/bin/env python3
"""Shared helpers and constants for hone's standalone scripts.

The scripts in this directory are standalone CLIs with flat same-directory
imports (no package). This module is the single source of truth for logic
that used to be duplicated (and drifted) across them:

  - Null-tolerant dict access (`get`): the eval_results schema allows
    explicit JSON null for many fields, and dict.get's default only covers an
    absent key — a present-but-null value defeated `d.get(k, default)` and
    produced repeated TypeError crashes across consumers.
  - The canonical per-test score fallback chain (`resolve_score`):
    deterministic composite / `score` / `final_score`.
  - The sibling deterministic_scores.json loaders shared by
    analyze_results.py and criteria_self_repair.py.
  - Side-effecting bash command patterns shared by side_effect_guard.py
    (sandboxing) and validate_eval_criteria.py (runner_context hygiene).
  - YAML frontmatter splitting and field extraction shared by
    side_effect_guard.py and structural_audit.py.
  - Pass/acceptance/triage score thresholds. These are AUTHORITATIVE; the
    numbers quoted in references/*.md mirror this module.

Stdlib only. Keep it dependency-free so the plugin ships self-contained.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Score thresholds (authoritative — references/*.md mirror these values)
# ---------------------------------------------------------------------------

# Below this across a whole run, the eval criteria are suspect rather than
# the artifact (analyze_results triage). Per-test, it is also the "failing
# test" cutoff that criteria_self_repair processes.
CRITERIA_BUG_THRESHOLD = 0.5

# Post-fix score a repaired test must reach for the criteria fix to be
# accepted rather than reverted (Phase 2 self-repair verification).
ACCEPTANCE_THRESHOLD = 0.65

# Phase 1 exit gate: any test scoring below this is an actionable quality
# gap worth a Phase 2 improvement round. PASS/FAIL labels in operator
# summaries must use this same bar so triage and reporting never disagree.
ACTIONABLE_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Null-tolerant access
# ---------------------------------------------------------------------------

def get(d: object, key: str, default=None, expected: type | tuple | None = None):
    """dict.get that also treats an explicit JSON null as absent.

    Returns `default` when `d` is not a dict, when `key` is missing, or when
    the stored value is None. The eval_results schema allows null for
    score/details/timeout fields, and a raw `d.get(key, default)` returns
    None in that case, crashing numeric comparisons and method calls
    downstream.

    `expected` optionally extends the same treatment to wrong-typed values:
    when set, a stored value that is not an instance of `expected` also
    returns `default`. Audit-path callers use this because they run on
    schema-invalid files by design — a `runner_context` that arrives as a
    list must not crash `.strip()` before any findings reach stdout.
    """
    if not isinstance(d, dict):
        return default
    value = d.get(key, default)
    if value is None:
        return default
    if expected is not None and not isinstance(value, expected):
        return default
    return value


def _raw_llm_score(result: dict):
    """The result's own LLM score: `score`, else the `final_score` alias.

    An explicit `score: null` means the judge ran and errored; it is
    returned as None (NOT papered over by the `final_score` alias) so
    resolve_score falls through to the deterministic composite. This pins
    the pre-consolidation behavior of criteria_self_repair's
    `result.get("score", result.get("final_score"))`, where a present-but-
    null `score` never consulted `final_score`. The alias applies only when
    the `score` key is absent entirely (skill-creator runners emit
    `final_score` instead of `score`).

    Non-numeric values (a stringified "0.85", a bool, a list) are treated
    exactly like null: returned as None so resolve_score falls through to
    the deterministic composite/default, mirroring the sibling loaders'
    `isinstance(..., (int, float))` filter. Passing them through crashed
    numeric consumers (`round(score, 4)`, threshold comparisons).
    """
    if not isinstance(result, dict):
        return None
    if "score" in result:
        value = result["score"]
    else:
        value = result.get("final_score")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def resolve_score(
    result: dict,
    det_scores: dict[str, float] | None = None,
    default: float = 0.0,
    prefer_deterministic: bool = True,
) -> float:
    """Canonical per-test score fallback chain.

    Sources, in order:
      - the deterministic composite for this test_id in `det_scores`
        (from deterministic_scores.json via load_deterministic_scores)
      - the LLM judge `score` in the result; the `final_score` alias some
        runners emit stands in only when the `score` key is absent. An
        explicit `score: null` (judge ran and errored) skips both and
        falls through to the deterministic composite.
      - `default` (0.0 — a scoreless failing test must not look passing)

    `prefer_deterministic=True` (analyze_results convention) consults the
    deterministic composite first; False (criteria_self_repair convention)
    consults the result's own score first and uses the deterministic
    composite only as a fallback.
    """
    det_scores = det_scores or {}
    det = det_scores.get(get(result, "test_id", "unknown"))
    if prefer_deterministic and det is not None:
        return det
    llm = _raw_llm_score(result)
    if llm is not None:
        return llm
    if det is not None:
        return det
    return default


# ---------------------------------------------------------------------------
# deterministic_scores.json sibling-file loaders
# ---------------------------------------------------------------------------

def _load_det_file(results_path: str) -> dict:
    """Parse deterministic_scores.json next to results.json; {} on any failure.

    "Any failure" includes a file that parses but is not a JSON object
    (e.g. `[]` from truncation or a bad repair) — returning it as-is would
    crash both public loaders on `.get`.
    """
    det_scores_path = Path(results_path).parent / "deterministic_scores.json"
    if not det_scores_path.exists():
        return {}
    try:
        with open(det_scores_path) as f:
            parsed = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _per_test_entries(results_path: str) -> list[dict]:
    """Object entries of deterministic_scores.json's per_test array.

    Tolerates a null/non-list `per_test` and non-object items (an int item
    would make `"test_id" in test` raise TypeError in the loaders below).
    """
    per_test = _load_det_file(results_path).get("per_test")
    if not isinstance(per_test, list):
        return []
    return [test for test in per_test if isinstance(test, dict)]


def load_deterministic_scores(results_path: str) -> dict[str, float]:
    """Map test_id -> deterministic composite from deterministic_scores.json.

    results.json carries a per-test `score` only when an LLM judge ran. On a
    deterministic-only run those fields are absent, so every consumer that
    reads `score` directly sees 0.0 for every test; this sibling file is the
    only score source on such runs.

    Inconclusive tests carry composite: null; they are excluded so numeric
    comparisons downstream never see None. Returns an empty dict when the
    file is missing or unreadable.
    """
    return {
        test["test_id"]: test["composite"]
        for test in _per_test_entries(results_path)
        if "test_id" in test and isinstance(test.get("composite"), (int, float))
    }


def load_inconclusive_ids(results_path: str) -> set[str]:
    """Set of test_ids marked inconclusive in deterministic_scores.json.

    score_execution.py emits `status: "inconclusive"` with `composite: null`
    for tests with no execution evidence. load_deterministic_scores drops
    them from the score map, which made them indistinguishable from "never
    scored deterministically": on a deterministic-only run they then fell
    back to `score = 0.0`, dragging avg/FAIL counts and (on an
    all-inconclusive run) misrouting triage into criteria_bug.
    """
    return {
        test["test_id"]
        for test in _per_test_entries(results_path)
        if "test_id" in test
        and (
            test.get("status") == "inconclusive"
            or not isinstance(test.get("composite"), (int, float))
        )
    }


# ---------------------------------------------------------------------------
# Side-effecting bash command patterns
# ---------------------------------------------------------------------------
# Shared by side_effect_guard.py (which sandboxes these during eval runs and
# attaches simulated responses) and validate_eval_criteria.py (which flags
# them in runner_context as hygiene findings). Each entry: (regex_source,
# human_label). Consumer-specific metadata (simulated responses, compile
# flags, extra patterns like SETUP: blocks) stays in each consumer.

# Source-control / publishing mutations.
GIT_MUTATING_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\bgit\s+push\b", "git push"),
    (r"\bgit\s+push\s+--force\b", "git push --force"),
    (r"\bgh\s+pr\s+create\b", "gh pr create"),
    (r"\bgh\s+pr\s+merge\b", "gh pr merge"),
    (r"\bgit\s+commit\b", "git commit"),
]

# Filesystem-mutating commands — flagged so eval criteria never actually
# create scripts or files during a test run. These are the shapes that
# showed up in SETUP: blocks and caused flaky eval state.
FS_MUTATING_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\bmkdir\s+(-p\s+)?[^\s]+", "mkdir"),
    # [^|\n] keeps the match on a single line: an unrestricted [^|]* spans
    # newlines and false-positives on a bare `echo`/`printf` mention followed
    # by any later ">" (e.g. an "->" arrow) anywhere in the document.
    (r"\bprintf\s+[^|\n]*>[>\s]*[^\s\n]+", "printf > file"),
    (r"\becho\s+[^|\n]*>[>\s]*[^\s\n]+", "echo > file"),
    (r"\bcp\s+[^\s]+\s+[^\s]+", "cp"),
]

# Full ordered set used by side_effect_guard.py (order determines the
# sandbox-context listing order).
BASH_SIDE_EFFECT_PATTERNS: list[tuple[str, str]] = (
    GIT_MUTATING_BASH_PATTERNS + FS_MUTATING_BASH_PATTERNS
)


# ---------------------------------------------------------------------------
# YAML frontmatter extraction
# ---------------------------------------------------------------------------

# \A anchors to the absolute start of file (not just line start) to avoid
# matching horizontal rules or --- inside code blocks mid-document. The
# closing delimiter must be a bare --- line (trailing whitespace ok) or the
# delimiter at EOF — a file ending exactly at `---` previously failed to
# parse in side_effect_guard, silently disabling the allowed-tools filter.
# \r? before each \n accepts CRLF line endings; without it a well-formed
# CRLF document failed to parse, with the same silent-disable consequence.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)

_BLOCK_SCALAR_INDICATOR_RE = re.compile(r"[|>][+-]?\d*\Z")


def match_frontmatter(content: str) -> re.Match | None:
    """Match the leading YAML frontmatter block; group(1) is its inner text.

    Returns None when the document has no frontmatter. Callers that need the
    body offset can use .end(); callers that only need the inner text can
    use split_frontmatter below.
    """
    return _FRONTMATTER_RE.match(content)


def split_frontmatter(content: str) -> str | None:
    """Inner text of the leading YAML frontmatter block, or None."""
    m = match_frontmatter(content)
    return m.group(1) if m else None


def frontmatter_field(frontmatter: str, name: str) -> str | None:
    """Extract a top-level field's value from frontmatter text (no YAML dep).

    Handles the shapes that appear in real artifacts:
      - inline scalar / flow list:  `name: value`  -> "value" (stripped)
      - block scalar:               `name: |` / `name: >` (with chomping /
        indent indicators) followed by indented lines -> dedented lines
        joined by newlines
      - bare key + indented block:  `name:` followed by an indented block
        (e.g. a `- item` list) -> dedented block lines joined by newlines

    Returns None when the field is absent, or when the key is present with
    neither an inline value nor an indented block.
    """
    field_re = re.compile(
        rf"^{re.escape(name)}:[ \t]*(.*)$", re.MULTILINE | re.IGNORECASE
    )
    m = field_re.search(frontmatter)
    if m is None:
        return None
    rest = m.group(1).strip()
    is_block_scalar = bool(rest) and bool(_BLOCK_SCALAR_INDICATOR_RE.fullmatch(rest))
    if rest and not is_block_scalar:
        return rest

    # Collect the indented block following the field line; blank lines are
    # allowed inside, and the block ends at the first non-indented line.
    block_lines: list[str] = []
    for line in frontmatter[m.end():].split("\n")[1:]:
        if line.strip() == "":
            block_lines.append("")
            continue
        if line[0] in " \t":
            block_lines.append(line)
            continue
        break
    while block_lines and block_lines[-1] == "":
        block_lines.pop()
    if not block_lines:
        return None

    indent = min(
        len(line) - len(line.lstrip()) for line in block_lines if line.strip()
    )
    return "\n".join(line[indent:] if line.strip() else "" for line in block_lines)
