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
  - The delegation-shaped slash-invocation regex shared by
    side_effect_guard.py (fail-closed sandboxing) and
    validate_eval_criteria.py (missing_skill_tool audit).
  - YAML frontmatter splitting and field extraction shared by
    side_effect_guard.py and structural_audit.py.
  - Pass/acceptance/triage score thresholds. These are AUTHORITATIVE; the
    numbers quoted in references/*.md mirror this module.
  - The run-shape table (RUN_SHAPE_ACTIVE_STEPS + derive_run_shape /
    derive_gate_mode). This is AUTHORITATIVE and stated once: a hone run
    has one of three documented shapes, each derived from the state file's
    steps{} map, and the shape decides which steps (hence which handoffs
    and which gate events) the run is expected to produce:

      normal          phase1_evaluate ran        all steps active
      fix-only        phase1_evaluate "skipped"  Phase 2/3 steps only
                      (SKILL.md's --fix-only entry marks every Phase 1
                      step skipped in one write; no other documented
                      shape skips phase1_evaluate)
      no-improvement  phase2_improve "skipped"   Phase 1 steps only
                      (Phase 1 found nothing to improve; Phases 2 and 3
                      never ran)

    validate_handoff.py consults the table for --step and --all (a handoff
    is required exactly when its producing step is active in the shape and
    actually ran), and validate_gates.py derives its expected-event mode
    from the same map (plus "error-halt" when non-done, non-skipped steps
    remain). SKILL.md and references/*.md defer to this table; do not
    restate the shape rules in prose.

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

# Floor applied to each dimension inside score_execution's weighted geometric
# mean, so also the smallest composite a deterministic run can produce: with
# weights summing to 1, all-floored dimensions give 0.05 ** 1 == 0.05. It
# lives here because analyze_results' triage bands have to agree with it —
# written against an exact 0.0, which the floor makes unreachable, `variance`
# could never be returned on a deterministic-only run.
DIMENSION_FLOOR = 0.05


def at_score_floor(score: float) -> bool:
    """True when a composite sits at (or below) the deterministic floor.

    The floor is the "nothing scored" reading that an exact 0.0 used to carry.
    The tolerance absorbs the float noise in `0.05 ** 1` (0.049999999999999996)
    for callers that skip the round-trip through `round(..., 4)`.
    """
    return isinstance(score, (int, float)) and score <= DIMENSION_FLOOR + 1e-9


# ---------------------------------------------------------------------------
# Run shapes (authoritative — see the module docstring)
# ---------------------------------------------------------------------------
# Step-name vocabulary matches the SKILL.md state-file template and
# validate_handoff.py's STEP_CONTRACTS (which has a test asserting the two
# stay in sync).

PHASE1_STEPS: tuple[str, ...] = (
    "phase1_structural_audit",
    "phase1_criteria_audit",
    "phase1_evaluate",
    "phase1_reference_validation",
    "phase1_spec_artifacts",
)

PHASE23_STEPS: tuple[str, ...] = (
    "phase2_trigger_test",
    "phase2_fresh_eyes",
    "phase2_improve",
    "phase3_reevaluate",
)

# The single declarative statement of which tracked steps run in each
# documented run shape. "Active" means the shape can run the step at all;
# whether it actually ran is the step's own status.
RUN_SHAPE_ACTIVE_STEPS: dict[str, frozenset[str]] = {
    "normal": frozenset(PHASE1_STEPS + PHASE23_STEPS),
    "fix-only": frozenset(PHASE23_STEPS),
    "no-improvement": frozenset(PHASE1_STEPS),
}


# Steps that may legitimately follow a failing gate without contradicting the
# claim that the run halted there. Only `workflow_exit` qualifies: it is the
# stop itself, and SKILL.md mandates it before ANY exit. Anything else after a
# fail is forward progress, and a fail followed by forward progress is not a
# halt. Shared so validate_gates.py's warning and score_execution.py's score
# read the same halt shape.
#
# `convergence` used to sit in this set as "the check the failure capped".
# hone never emits it. It has no row in SKILL.md's Gate Events table, which is
# the closed vocabulary of emitted events, and no Phase 3 step appends it --
# the phase goes straight from the `phase3_exit` append (step 4) to the
# mechanical exit gate (step 5), which emits `workflow_exit`. The word appears
# in references/phase3-reevaluation.md only for the user-specified score
# target, which is not a gate event. Keeping it here meant an executor that
# invented the event turned ANY failed gate into a compliant halt: a scoring
# bypass that paid for emitting a step that does not exist.
HALT_SEQUENCE_STEPS: frozenset[str] = frozenset({"workflow_exit"})


def is_halt_tail(later_gates: object, failed_step: object = None) -> bool:
    """True when everything after a failing gate belongs to that gate's halt.

    The one place the halt shape is defined. Both callers had their own copy
    and the copies had drifted: validate_gates.py accepted a tail of
    `convergence` alone, while score_execution.py additionally required
    `workflow_exit`, so the two disagreed about whether a run had halted even
    though a comment in each claimed they scored the same shape.

    `failed_step` is the step of the gate that failed, and it decides the
    empty-tail clause below, so callers pass it.

    A tail is a halt when:

    * nothing follows AND the failing gate is `workflow_exit` itself -- the
      exit is the last event SKILL.md mandates, so a fail there with nothing
      after it is the halt. A fail on any *other* step with nothing after it
      is a run that stopped emitting gates before reaching its mandated exit,
      which is the "fewer events score better" hole one level up; or
    * `workflow_exit` is present (SKILL.md mandates it before ANY exit, so a
      tail without one is a run that carried on) and every event in the tail
      is a halt-sequence step -- which, since `convergence` left that set,
      means the tail is the exit event and nothing else.

    So the documented regression auto-revert halt is exactly
    `[phase3_exit:fail, workflow_exit]`. `workflow_exit` itself may pass or
    fail: a passing exit is the ordinary clean stop, and it is the halt rather
    than progress past it. An event hone never emits -- `convergence` being
    the one this used to admit -- is not a halt-sequence step, so appending
    one cannot turn a failed gate into a compliant halt.
    """
    if not isinstance(later_gates, (list, tuple)):
        return False
    if not later_gates:
        return failed_step == "workflow_exit"
    saw_exit = False
    for later in later_gates:
        if not isinstance(later, dict):
            return False
        step = later.get("step")
        if step not in HALT_SEQUENCE_STEPS:
            return False
        if step == "workflow_exit":
            saw_exit = True
    return saw_exit


def derive_run_shape(steps: object) -> str:
    """Run shape derived from the state file's steps{} map.

    Discriminators (see the module docstring's table): phase1_evaluate
    "skipped" marks fix-only, phase2_improve "skipped" marks
    no-improvement, anything else is normal. Tier-based skips of other
    Phase 1 steps (eg phase1_structural_audit on lightweight artifacts)
    deliberately do not change the shape. A non-dict/absent steps map
    derives "normal" — the strictest shape, so a truncated state file is
    never blessed by accident.
    """
    if not isinstance(steps, dict):
        return "normal"
    if steps.get("phase1_evaluate") == "skipped":
        return "fix-only"
    if steps.get("phase2_improve") == "skipped":
        return "no-improvement"
    return "normal"


def derive_gate_mode(steps: object) -> str | None:
    """validate_gates.py's expected-event mode, derived from steps{}.

    The gate check runs as the last action before any exit (SKILL.md's
    Mechanical Exit Gate), where a compliant run has every step "done" or
    "skipped". Any other status means the run halted mid-flight:
    "error-halt". Otherwise the mode is the run shape. Returns None when
    the steps map is absent or unusable (mode cannot be derived; the
    caller falls back to --mode / "normal").
    """
    if not isinstance(steps, dict) or not steps:
        return None
    if any(status not in ("done", "skipped") for status in steps.values()):
        return "error-halt"
    return derive_run_shape(steps)


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


# Top-level keys that can carry the per-test array, in precedence order.
# `results` is the canonical hone format; `test_results` is the skill-creator
# alias. Both are real inputs, so every consumer of a results file has to
# accept both — analyze_results read only `results` and silently reported an
# empty run on a file score_execution had just graded.
RESULTS_KEYS: tuple[str, ...] = ("results", "test_results")


def extract_results(data: object) -> tuple[list, str | None]:
    """Split a parsed results file into (test entries, key that carried them).

    The returned key is None when neither alias is present, which callers use
    to tell a schema mismatch ("this file is not a results file") from a valid
    file whose array is empty ("no tests ran"). A present-but-wrong-typed value
    yields an empty list under its own key: the schema was recognized, the
    payload was not usable.
    """
    if not isinstance(data, dict):
        return [], None
    for key in RESULTS_KEYS:
        if key in data:
            value = data.get(key)
            return (value if isinstance(value, list) else []), key
    return [], None


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
    default: float | None = 0.0,
    prefer_deterministic: bool = True,
) -> float | None:
    """Canonical per-test score fallback chain.

    Sources, in order:
      - the deterministic composite for this test_id in `det_scores`
        (from deterministic_scores.json via load_deterministic_scores)
      - the LLM judge `score` in the result; the `final_score` alias some
        runners emit stands in only when the `score` key is absent. An
        explicit `score: null` (judge ran and errored) skips both and
        falls through to the deterministic composite.
      - `default` (0.0 — a scoreless failing test must not look passing).
        Callers that need to tell "no usable score" from a real 0.0 pass
        `default=None` and filter the Nones out; generate_spec_artifacts does
        this so an unscored test stays out of the average instead of dragging
        it down.

    `prefer_deterministic=True` (analyze_results convention) consults the
    deterministic composite first; False (criteria_self_repair convention)
    consults the result's own score first and uses the deterministic
    composite only as a fallback.
    """
    det_scores = det_scores or {}
    # expected=str: dict.get hashes its key even on an empty dict, so a
    # non-string test_id (list, dict) raised TypeError on every call path.
    det = det_scores.get(get(result, "test_id", "unknown", expected=str))
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
    file is missing or unreadable. test_id must be a string: it becomes a
    dict key here and a set member in load_inconclusive_ids, and an
    unhashable one (list, dict) raised TypeError before any output.
    """
    return {
        test["test_id"]: test["composite"]
        for test in _per_test_entries(results_path)
        if isinstance(test.get("test_id"), str)
        and isinstance(test.get("composite"), (int, float))
    }


def load_inconclusive_ids(results_path: str) -> set[str]:
    """Set of test_ids marked inconclusive in deterministic_scores.json.

    score_execution.py emits `status: "inconclusive"` with `composite: null`
    for tests with no execution evidence, and `status: "score_error"` when the
    scorer itself raised — an internal exception measured nothing either, so
    both statuses belong here. load_deterministic_scores drops
    them from the score map, which made them indistinguishable from "never
    scored deterministically": on a deterministic-only run they then fell
    back to `score = 0.0`, dragging avg/FAIL counts and (on an
    all-inconclusive run) misrouting triage into criteria_bug.
    """
    return {
        test["test_id"]
        for test in _per_test_entries(results_path)
        if isinstance(test.get("test_id"), str)
        and (
            test.get("status") in ("inconclusive", "score_error")
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
    # Publishing a draft PR notifies reviewers and starts CI, and `gh pr ready`
    # is how the local pipeline skills do it — more often than `gh pr create`.
    # The sandbox block is a closed enumeration ("do not execute any of the
    # following"), so a publishing command missing from it reads to the
    # executor as permission to run it for real.
    (r"\bgh\s+pr\s+ready\b", "gh pr ready"),
    (r"\bgh\s+pr\s+edit\b", "gh pr edit"),
    (r"\bgh\s+pr\s+comment\b", "gh pr comment"),
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

# Destructive commands — the blast-radius group. Unlike the creation shapes
# above, these cannot be undone by deleting a stray file afterwards, so an
# unattended eval of a skill whose job is deletion (a cleanup skill, a branch
# pruner) has to be sandboxed or it removes the operator's real data. The
# guard previously carried no deletion pattern at all, so exactly those skills
# got an empty sandbox.
DESTRUCTIVE_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(?:-[a-zA-Z]+\s+)*[^\s]+", "rm"),
    (r"\btrash\s+(?:-[a-zA-Z]+\s+)*[^\s]+", "trash"),
    (r"\bfind\s+[^\n]*\s-delete\b", "find -delete"),
    # find -exec rm/trash is the same deletion with an extra hop; the -delete
    # pattern above does not cover it because the verb moves into -exec.
    (r"\bfind\s+[^\n]*-exec\s+(?:rm|trash)\b", "find -exec rm"),
    (r"\bmv\s+[^\s]+\s+[^\s]+", "mv"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+branch\s+-D\b", "git branch -D"),
    (r"\bgit\s+checkout\s+\.(?:\s|$)", "git checkout ."),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[fd]", "git clean -fd"),
]

# Network writes. A POST/PUT/PATCH/DELETE from an unattended eval reaches a
# real endpoint (an API, a chat webhook, a package registry) and is not
# recoverable from the local filesystem, so it belongs in the same group as
# deletions rather than with the read-only fetches, which are left alone.
NETWORK_WRITE_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\bcurl\s+[^\n]*-X\s*(?:POST|PUT|PATCH|DELETE)\b", "curl -X POST"),
    (r"\bcurl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|\s-d\s)", "curl --data"),
    (r"\bgh\s+api\s+[^\n]*-(?:X|-method)\s*(?:POST|PUT|PATCH|DELETE)\b", "gh api -X POST"),
    (r"\bwget\s+[^\n]*--post-(?:data|file)\b", "wget --post-data"),
]

# Full ordered set used by side_effect_guard.py (order determines the
# sandbox-context listing order). validate_eval_criteria.py's runner_context
# hygiene check deliberately consumes only FS_MUTATING_BASH_PATTERNS: the two
# groups above describe what the artifact under test may do, not what a
# criteria author accidentally wrote into a SETUP: block.
BASH_SIDE_EFFECT_PATTERNS: list[tuple[str, str]] = (
    GIT_MUTATING_BASH_PATTERNS
    + FS_MUTATING_BASH_PATTERNS
    + DESTRUCTIVE_BASH_PATTERNS
    + NETWORK_WRITE_BASH_PATTERNS
)

# Runner-context header side_effect_guard.py appends when sandboxing side
# effects. validate_eval_criteria.py's hygiene check skips everything after
# this header: the sandbox block itself names the commands it simulates
# ("cp → simulate: ..."), which would otherwise draw a fixable
# runner_context_side_effect finding against the guard's own output on
# every criteria-reuse run.
SANDBOX_HEADER = "SAFETY SANDBOX — side-effect simulation mode"


# ---------------------------------------------------------------------------
# Delegation-shaped slash-invocation detection
# ---------------------------------------------------------------------------
# Shared by side_effect_guard.py (fail-closed sandboxing of unknown
# delegations) and validate_eval_criteria.py (missing_skill_tool audit).
# These were previously two separate regexes that disagreed on identical
# prompts: "Run /forge." and "`/forge`" sandboxed but never drew the
# missing-Skill-tool repair. The pattern matches a delegation-shaped token
# (line start / whitespace / backtick / bracket / paren before the slash, no
# second slash after the name) so file paths like /tmp/x or factor/face
# never fire; the stoplist drops bare filesystem path heads.

DELEGATION_RE = re.compile(
    r"(?:^|[\s`(\[])/([a-z][a-z0-9-]{2,})\b(?!/)", re.MULTILINE
)
DELEGATION_STOPLIST = frozenset(
    {"tmp", "usr", "bin", "etc", "var", "opt", "dev", "home", "private", "users"}
)


def find_slash_invocations(text: str) -> list[str]:
    """Names of delegation-shaped /slash-commands in text.

    Stoplist-filtered, deduplicated, in order of first appearance.
    """
    names: list[str] = []
    for match in DELEGATION_RE.finditer(text):
        name = match.group(1)
        if name not in DELEGATION_STOPLIST and name not in names:
            names.append(name)
    return names


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
