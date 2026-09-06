#!/usr/bin/env python3
"""Shared helpers for hone's standalone CLIs. Stdlib only; imports are local.

This module defines null-safe access, score resolution and loaders, side-effect
patterns, slash-invocation detection, frontmatter parsing, and score thresholds.
Reference docs mirror its thresholds and run-shape table.

Run shapes derive from steps{}:
  normal: phase1_evaluate ran; all steps active.
  fix-only: phase1_evaluate skipped; Phase 2/3 active. SKILL.md skips all
    Phase 1 steps at --fix-only entry.
  no-improvement: phase2_improve skipped; Phase 1 active.

validate_handoff requires outputs from active steps that ran. validate_gates
uses the same shape, or error-halt if unfinished steps remain. Keep shape
rules here; SKILL.md and references defer to this table."""

from __future__ import annotations

import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Score thresholds (authoritative — references/*.md mirror these values)
# ---------------------------------------------------------------------------

# A run below this suggests faulty criteria; individual tests below it enter criteria self-repair.
CRITERIA_BUG_THRESHOLD = 0.5

# Minimum repaired-test score required to keep a criteria fix.
ACCEPTANCE_THRESHOLD = 0.65

# Phase 1 tests below this need Phase 2 improvement. Use the same bar for PASS/FAIL reporting.
ACTIONABLE_THRESHOLD = 0.8

# Each dimension is floored before the weighted geometric mean. With weights
# summing to 1, this is also the composite floor. Triage must use this floor
# instead of unreachable 0.0 when detecting variance.
DIMENSION_FLOOR = 0.05


def at_score_floor(score: float) -> bool:
    """True at or below the deterministic floor, allowing float noise from 0.05 ** 1."""
    return isinstance(score, (int, float)) and score <= DIMENSION_FLOOR + 1e-9


# Run shapes. Step names match SKILL.md and validate_handoff.STEP_CONTRACTS.

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

# Active steps may run in this shape; each step's status records whether it ran.
RUN_SHAPE_ACTIVE_STEPS: dict[str, frozenset[str]] = {
    "normal": frozenset(PHASE1_STEPS + PHASE23_STEPS),
    "fix-only": frozenset(PHASE23_STEPS),
    "no-improvement": frozenset(PHASE1_STEPS),
}


# Only workflow_exit may follow every failing gate. It records the required
# stop; other events need step-specific rules shared by validation and scoring.
HALT_SEQUENCE_STEPS: frozenset[str] = frozenset({"workflow_exit"})

# Phase 3 emits phase3_exit, convergence, then workflow_exit (reference steps
# 6 and 7). A regression halt may include convergence after phase3_exit:fail.
# REQUIRED_STEPS checks membership, not this ordering.
PHASE3_HALT_SEQUENCE: tuple[str, ...] = (
    "phase3_exit", "convergence", "workflow_exit",
)

# Handoff failures reject input and allow repair followed by another validation.
# Other failures order a halt: convergence escalate/capped, regression auto-revert,
# failed phase transitions, or workflow_exit errors. Later progress cannot settle
# a halt order. Treat unknown steps as halt orders until explicitly classified.
VALIDATION_FAIL_PREFIXES: tuple[str, ...] = ("handoff_",)

# Only documented retries can settle a failed attempt. Handoffs retry after
# repair; convergence retries once after ledger_missing, and that retry may fail.
# Recurring phase exits, transitions, fixonly_entry, and resume have no retry rule.
# Keep retry prefixes separate from validation-failure prefixes: repeatability
# and permission to continue after failure are different properties.
REPEATABLE_STEPS: frozenset[str] = frozenset({"convergence"})
REPEATABLE_STEP_PREFIXES: tuple[str, ...] = ("handoff_",)

# Event recording an authorized restart; see is_authorized_restart.
RESUMPTION_STEP = "resume"

# Only documented restart paths qualify. A capped convergence may resume after
# the --confirm human gate grants more rounds and updates both round limits.
# workflow_exit permits cross-session resume. A resume after workflow_exit
# does not retroactively authorize an earlier regression or transition failure.
RESUMABLE_STEPS: frozenset[str] = frozenset({"convergence", "workflow_exit"})


# Convergence failure reasons come from check_convergence.py:
#   escalate: halt; further work requires a fresh hone invocation.
#   capped: halt; the --confirm human gate may grant a restart.
#   ledger_missing: repair the ledger and retry once; use the retry verdict.
#
# Event order cannot distinguish these causes. Record the reason per event,
# since one run may encounter several. Other steps retain free-form reasons
# (eg corrupt_state_file). gate-event-schema.json mirrors this enum; tests
# check agreement. SKILL.md and phase3-reevaluation.md also quote it.
HALT_REASONS: dict[str, frozenset[str]] = {
    "convergence": frozenset({"ledger_missing", "escalate", "capped"}),
}

# Executor-written reasons may only restrict the settlements available before
# reason checking: capped retains restart, ledger_missing retains in-place retry,
# and escalate retains neither. No independent source corroborates these claims.
#
# Both guards must test `declared_halt_reason(...) not in <SET>` so absent,
# invalid, empty, or wrongly typed reasons also refuse settlement. Testing
# `reason is not None` first would let silence score better than truthful
# escalate. Adding a permitted reason requires independent corroboration.
RESTART_AUTHORIZING_REASONS: frozenset[str] = frozenset({"capped"})
IN_PLACE_REPAIR_REASONS: frozenset[str] = frozenset({"ledger_missing"})


def step_declares_reason(step: object) -> bool:
    """True when this step has a closed failure-reason vocabulary. Other reasons are free text."""
    return isinstance(step, str) and step in HALT_REASONS


def declared_halt_reason(step: object, reason: object) -> str | None:
    """Return an exact vocabulary match, or None for missing, empty, unknown, or
    non-string reasons. Do not normalize case or whitespace: the schema rejects
    near matches too, and callers must treat None as undeclared."""
    if not step_declares_reason(step):
        return None
    if not isinstance(reason, str):
        return None
    return reason if reason in HALT_REASONS[step] else None


def fail_orders_halt(step: object) -> bool:
    """True when a `fail` on this step is itself an order to stop the run."""
    return not (
        isinstance(step, str) and step.startswith(VALIDATION_FAIL_PREFIXES)
    )


def is_repeatable_step(step: object) -> bool:
    """True for a step the workflow emits once per attempt (see above)."""
    if not isinstance(step, str):
        return False
    return step in REPEATABLE_STEPS or step.startswith(REPEATABLE_STEP_PREFIXES)


def is_authorized_restart(
    later_gates: object,
    failed_step: object,
    declared_reason: object = None,
) -> bool:
    """True when a documented restart follows a valid halt.

    Require a resumable step, a valid halt tail before resume, and resume:pass.
    Convergence additionally requires reason=capped; missing or invalid reasons
    refuse restart. workflow_exit needs no reason and may resume immediately
    because it already records the stop. Other failures need an exit event first.

    The --confirm sequence is convergence:fail (capped), workflow_exit:fail,
    resume:pass, then further rounds. Human approval happens after the halt.
    Neither a bare resume nor later progress without resume qualifies."""
    if failed_step not in RESUMABLE_STEPS:
        return False
    if step_declares_reason(failed_step) and (
        declared_halt_reason(failed_step, declared_reason)
        not in RESTART_AUTHORIZING_REASONS
    ):
        return False
    if not isinstance(later_gates, (list, tuple)):
        return False
    for index, event in enumerate(later_gates):
        if isinstance(event, dict) and event.get("step") == RESUMPTION_STEP:
            if event.get("result") != "pass":
                return False
            return is_halt_tail(later_gates[:index], failed_step)
    return False


def is_settled_by_retry(
    later_gates: object,
    failed_step: object,
    declared_reason: object = None,
) -> bool:
    """True when the first documented retry settles this failure.

    Require a repeatable step. An adjacent retry may pass or fail; a failed retry
    must itself be accounted for. A validation failure also allows a later pass
    across intervening events because it did not order the run to stop.

    A halt-ordering retry must be adjacent and followed only by a valid halt tail.
    For convergence, only ledger_missing permits retry; capped, escalate, and
    undeclared or invalid reasons refuse it. The declaration cannot relax the
    halt-tail rule, so a legitimate ledger repair that returns in_progress and
    continues loses credit. Legacy repairs without reasons also lose credit.
    These conservative errors prevent extra rounds from settling an ignored halt.

    No independent source corroborates ledger_missing: state has no ledger-write
    record; a repaired ledger exists in either case; execution timelines are
    executor-written, absent in simulation, and unavailable to validate_gates;
    phase2_to_phase3 has no per-round key and cannot prove a parseable ledger.
    Another executor-written field cannot authenticate the claim. Reasons may
    only restrict the pre-reason rules, never grant a wider retry path.

    Use membership guards that reject None. Otherwise silence or a typo would
    retain a retry that truthful capped/escalate forfeits."""
    if not is_repeatable_step(failed_step):
        return False
    if not isinstance(later_gates, (list, tuple)):
        return False
    if step_declares_reason(failed_step) and (
        declared_halt_reason(failed_step, declared_reason)
        not in IN_PLACE_REPAIR_REASONS
    ):
        # Only ledger_missing permits retry. Membership rejects missing or invalid
        # reasons too, matching is_authorized_restart.
        return False
    if not fail_orders_halt(failed_step) and any(
        isinstance(later, dict)
        and later.get("step") == failed_step
        and later.get("result") == "pass"
        for later in later_gates
    ):
        return True
    for offset, later in enumerate(later_gates):
        if isinstance(later, dict) and later.get("step") == failed_step:
            if later_gates[:offset]:
                return False
            if fail_orders_halt(failed_step):
                # The retry must remain inside the halt; an uncorroborated reason
                # cannot authorize progress after it.
                return is_halt_tail(later_gates[offset + 1:], failed_step)
            return True
    return False


def fail_is_accounted(
    later_gates: object,
    failed_step: object,
    declared_reason: object = None,
) -> bool:
    """True when a failure was followed by a halt, authorized restart, or valid retry.

    Pass the failing event's reason; None defaults to refusal where a vocabulary
    is required. Both validation and scoring use this predicate.

    For convergence, preserve this table:
      before reason checking: halt tail, restart, retry
      ledger_missing:         halt tail,          retry
      capped:                halt tail, restart
      escalate:              halt tail
      missing or invalid:    halt tail

    Every row is a subset of the pre-reason rules. Missing or invalid reasons
    must never outperform a vocabulary value. On honest event sequences, truth
    therefore scores at least as well as alternatives: repairs get retry credit,
    capped restarts get restart credit, and escalations get halt credit.

    On arbitrary sequences a lie can still outperform truth because reasons are
    executor-written. It cannot exceed the pre-reason baseline, and the extra-round
    bypass remains rejected for every reason. Do not relax predicates based on an
    uncorroborated reason. TestNoDeclarationBeatsTheTruth checks the vocabulary
    against missing and invalid values; preserve both membership guards."""
    return (
        is_halt_tail(later_gates, failed_step)
        or is_authorized_restart(later_gates, failed_step, declared_reason)
        or is_settled_by_retry(later_gates, failed_step, declared_reason)
    )


def halt_tail_vocabulary(failed_step: object) -> frozenset[str]:
    """Return events allowed strictly after failed_step in its halt sequence.

    A failed phase3_exit reached Phase 3 and may emit convergence before exit.
    Failed convergence or earlier phase transitions permit only workflow_exit.
    Exclude the failing step itself so another round cannot count as a halt;
    retry handling belongs to is_settled_by_retry."""
    if failed_step in PHASE3_HALT_SEQUENCE:
        start = PHASE3_HALT_SEQUENCE.index(failed_step)
        return HALT_SEQUENCE_STEPS | frozenset(PHASE3_HALT_SEQUENCE[start + 1:])
    return HALT_SEQUENCE_STEPS


def is_halt_tail(later_gates: object, failed_step: object = None) -> bool:
    """True when the tail records a halt for failed_step.

    An empty tail qualifies only for workflow_exit itself. Otherwise require
    workflow_exit and allow only halt_tail_vocabulary events. The exit may pass
    or fail. A regression halt may include convergence before exit; a failed
    phase transition may not. Validation and scoring share this definition."""
    if not isinstance(later_gates, (list, tuple)):
        return False
    if not later_gates:
        return failed_step == "workflow_exit"
    allowed = halt_tail_vocabulary(failed_step)
    saw_exit = False
    for later in later_gates:
        if not isinstance(later, dict):
            return False
        step = later.get("step")
        if step not in allowed:
            return False
        if step == "workflow_exit":
            saw_exit = True
    return saw_exit


def derive_run_shape(steps: object) -> str:
    """Derive the run shape from steps{} using the module table. Other tier-based
    skips do not change the shape. Missing or non-dict steps default to normal,
    the strictest shape."""
    if not isinstance(steps, dict):
        return "normal"
    if steps.get("phase1_evaluate") == "skipped":
        return "fix-only"
    if steps.get("phase2_improve") == "skipped":
        return "no-improvement"
    return "normal"


def derive_gate_mode(steps: object) -> str | None:
    """Derive the exit-check mode from steps{}. Unfinished steps mean error-halt;
    otherwise use the run shape. Return None for absent or unusable maps so
    the caller falls back to --mode or normal."""
    if not isinstance(steps, dict) or not steps:
        return None
    if any(status not in ("done", "skipped") for status in steps.values()):
        return "error-halt"
    return derive_run_shape(steps)


# ---------------------------------------------------------------------------
# Null-tolerant access
# ---------------------------------------------------------------------------

def get(d: object, key: str, default=None, expected: type | tuple | None = None):
    """Return default for a non-dict, missing key, explicit null, or value outside
    the optional expected type. Unlike dict.get, this handles nullable results
    and malformed audit inputs without crashing downstream consumers."""
    if not isinstance(d, dict):
        return default
    value = d.get(key, default)
    if value is None:
        return default
    if expected is not None and not isinstance(value, expected):
        return default
    return value


# Per-test array keys in precedence order: hone results, then skill-creator test_results.
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
    """Return score, or final_score only when score is absent. Explicit null and
    non-numeric values return None; they must not consult the alias. resolve_score
    then uses its deterministic fallback or default."""
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
    """Resolve a test score from its deterministic composite, LLM score, or default.

    prefer_deterministic=True checks the composite first; False checks the LLM
    score first. final_score aliases an absent score key only; null skips it.
    The default is 0.0. Pass default=None to distinguish unscored tests and
    exclude them from averages, as generate_spec_artifacts does."""
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
    """Parse the sibling deterministic_scores.json; return {} for unreadable files,
    invalid JSON, or non-object roots."""
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
    """Map string test IDs to numeric deterministic composites from the sibling file.
    This is the only score source when no LLM judge ran. Exclude null composites
    and invalid IDs; return {} for a missing or unreadable file."""
    return {
        test["test_id"]: test["composite"]
        for test in _per_test_entries(results_path)
        if isinstance(test.get("test_id"), str)
        and isinstance(test.get("composite"), (int, float))
    }


def load_inconclusive_ids(results_path: str) -> set[str]:
    """Return IDs marked inconclusive or score_error in deterministic_scores.json.
    Both statuses lack measured scores. Keep them separate from missing scores
    so consumers exclude them instead of defaulting to 0.0 and skewing triage."""
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
    # gh pr ready publishes drafts, notifies reviewers, and starts CI.
    # Include it explicitly in the sandbox command list.
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

# Sandbox destructive commands during evals so cleanup skills cannot delete
# real data. These effects cannot be repaired by removing a stray output file.
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

# Sandbox network writes: POST/PUT/PATCH/DELETE can mutate remote services.
# Read-only fetches remain allowed.
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

# Sandbox-context header. The hygiene check skips this block because its
# simulated command examples would otherwise flag the guard's own output.
SANDBOX_HEADER = "SAFETY SANDBOX — side-effect simulation mode"


# Slash-invocation detection shared by sandboxing and missing-Skill-tool audits.
# Match tokens after line starts, whitespace, backticks, brackets, or parentheses;
# exclude a second slash so /tmp/x and factor/face stay out. The stoplist removes
# bare filesystem path heads.

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

# Anchor frontmatter at file start, excluding mid-document rules and code blocks.
# Accept a bare closing --- line, including EOF, trailing whitespace, and CRLF;
# missing valid frontmatter would silently disable allowed-tools filtering.
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
