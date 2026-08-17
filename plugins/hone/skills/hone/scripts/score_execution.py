#!/usr/bin/env python3
"""Deterministic scoring of eval runner execution data.

Weighted geometric mean across dimension scorers, 4 type profiles (skill,
command, hook, script), epsilon floor to prevent zero-collapse.

Usage:
    score_execution.py <results_json> --type skill [--artifact-path PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

# Shared helpers. Reading criteria through raw `dict.get` is why several
# present-but-null fields (required_present, step_index) crashed a whole test
# into a fabricated 0.0; `get(..., expected=T)` treats an explicit JSON null,
# and a wrong-typed value, as absent, as the validators already do.
from hone_common import DIMENSION_FLOOR, extract_results, get as typed_get

EPSILON = 1e-6

# Cues that flip a forbidden-phrase match into a denial. A correct halt message
# often has to name the work it is declining to do ("does NOT run the structural
# audit"); counting that as a violation punishes the exact behavior being asked
# for. Only un-negated occurrences count as present.
NEGATION_CUES = re.compile(
    r"\b(?:not|never|no|without|skip(?:s|ped|ping)?|avoid(?:s|ed|ing)?|"
    r"refrain(?:s|ed|ing)?|neither|nor|won't|doesn't|didn't|will not|"
    r"does not|did not|do not|rather than|instead of)\b",
    re.IGNORECASE,
)

# How far back to look for a negation cue preceding a forbidden-phrase match.
NEGATION_WINDOW = 40


def _has_unnegated_occurrence(phrase: str, text: str) -> bool:
    """True when `phrase` appears in `text` outside a negating context.

    The lookback window stops at the nearest sentence break so a denial in one
    sentence cannot excuse a plain occurrence in the next.
    """
    for match in re.finditer(re.escape(phrase), text, re.IGNORECASE):
        window = text[max(0, match.start() - NEGATION_WINDOW) : match.start()]
        for breaker in (".", "!", "?", ";", "\n"):
            head, sep, tail = window.rpartition(breaker)
            if sep:
                window = tail
        if not NEGATION_CUES.search(window):
            return True
    return False

# Grade thresholds
GRADE_THRESHOLDS = [
    (0.9, "A"),
    (0.75, "B"),
    (0.6, "C"),
    (0.4, "D"),
    (0.0, "F"),
]

# Step marker patterns, case-insensitive. These markers are searched for as
# literals in the execution transcript, so the numeric form must capture its
# heading title: the bare marker "## 1." sent score_workflow_sequence hunting
# for "1.", "2.", "3.", which any ordered list or ascending version number
# satisfies. structural_audit.py keeps the looser form — it only counts step
# transitions. Separators are [ \t], not \s, so the title has to be on the
# same line; \s spans newlines, which let an untitled `## 1.` borrow the next
# heading as its title.
STEP_PATTERNS = [
    re.compile(r"^#{2,4}\s+(?:Step|Phase|Stage)\s+\d+", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^#{2,4}\s+Part\s+[A-Z]", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^#{2,4}[ \t]+\d+\.[ \t]+\S.*$", re.MULTILINE),
]

# Gate/validation keywords
GATE_KEYWORDS = re.compile(
    r"\b(?:gate|checklist|validate|STOP|rubric|interaction schema)\b",
    re.IGNORECASE,
)

# State file path patterns
STATE_FILE_PATTERN = re.compile(
    r"/tmp/workflow-|state.*\.json|workflow.*state", re.IGNORECASE
)

# Voice slop patterns (subset of the full slop detector, for scoring)
EM_DASH_PATTERN = re.compile(r"\s[\u2014\u2013]\s|(?<!\w)--(?!\w)")
STACCATO_PATTERN = re.compile(r"(?:^.{1,15}\.\s*$\n){3,}", re.MULTILINE)
NEGATION_ASSERTION = re.compile(
    r"(?:Not|Isn't|Wasn't|Doesn't|Don't|Can't|Won't)\s+\w+[.,]\s*(?:It's|It is|This is|That's)",
    re.IGNORECASE,
)
BANNED_PHRASES = re.compile(
    r"\b(?:deep dive|leverage|synergize|game-?changer|paradigm shift|holistic|robust solution)\b",
    re.IGNORECASE,
)

# Code block fence pattern for stripping
CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```", re.MULTILINE)

# Patterns for content that should be excluded from voice compliance scoring
# because it's quoted/referenced material, not the executor's own prose
BLOCKQUOTE_PATTERN = re.compile(r"^\s*>+\s.*$", re.MULTILINE)
INLINE_CODE_PATTERN = re.compile(r"`[^`]+`")
# Lines that contain 2+ inline code spans are likely reference material, not prose
BACKTICK_HEAVY_LINE = re.compile(r"^.*`[^`]+`.*`[^`]+`.*$")

# Output section heading pattern (moved to module scope for perf)
HEADING_PATTERN = re.compile(r"^#{2,4}\s+(.+)", re.MULTILINE)

# Box-drawing patterns used by orchestration commands for gate presentations
BOX_DRAWING_PATTERN = re.compile(
    r"^[━═─]{3,}|^[┏┓┗┛┃│┌┐└┘├┤┬┴┼]",
    re.MULTILINE,
)

# Table separator pattern for voice compliance (moved to module scope for perf)
TABLE_SEP_PATTERN = re.compile(r"^\s*\|[\s\-|:]+\|\s*$")

# Dimension weight profiles by artifact type
SKILL_WEIGHTS = {
    "workflow_sequence": 0.187,
    "gate_compliance": 0.151,
    "state_persistence": 0.075,
    "output_structure": 0.116,
    "voice_compliance": 0.076,
    "parallel_efficiency": 0.076,
    "error_handling": 0.076,
    "quality_checks": 0.133,
    "verify_actions": 0.06,
    "research_first": 0.05,
}

COMMAND_WEIGHTS = {
    "workflow_sequence": 0.226,
    "gate_compliance": 0.186,
    "state_persistence": 0.115,
    "output_structure": 0.115,
    "error_handling": 0.115,
    "quality_checks": 0.133,
    "verify_actions": 0.06,
    "research_first": 0.05,
}

HOOK_WEIGHTS = {
    # false_positive_rate computed the same quantity (non-error fraction of
    # Bash calls) and was collapsed into trigger_accuracy at the combined
    # weight (0.30 + 0.25): a no-op under the weighted geometric mean.
    "trigger_accuracy": 0.55,
    "performance": 0.20,
    "output_structure": 0.15,
    "error_handling": 0.10,
}

SCRIPT_WEIGHTS = {
    "correctness": 0.35,
    "output_format": 0.10,
    "performance": 0.15,
    "output_structure": 0.15,
    "error_handling": 0.25,
}

CRITICAL_DIMS = {
    "skill": "workflow_sequence",
    "command": "workflow_sequence",
    "hook": "trigger_accuracy",
    "script": "correctness",
}

# Knowledge extraction tests use Read-only tools and don't invoke the skill.
# Execution-oriented dimensions are inapplicable. The LLM judge handles
# semantic evaluation; deterministic scoring contributes only error_handling
# (which is typically 1.0 for KE tests -- no errors expected).
# voice_compliance was removed: it scored the eval runner agent's prose style,
# which is irrelevant to the quality of the skill under evaluation.
KNOWLEDGE_EXTRACTION_WEIGHTS = {
    "error_handling": 1.0,
}

# Error-handling tests verify graceful early termination on invalid input.
# The skill should STOP before any workflow execution and communicate the error.
# Execution dimensions (workflow_sequence, state_persistence) are inapplicable
# because the correct behavior is to NOT execute the workflow.
ERROR_HANDLING_WEIGHTS = {
    "early_termination": 0.51,
    "user_communication": 0.34,
    "quality_checks": 0.15,
}

# Side-effect-guarded tests have a SAFETY SANDBOX block in runner_context
# that tells the executor to SIMULATE dangerous commands. Execution-oriented
# dimensions (workflow_sequence, state_persistence) are inapplicable because
# the executor was explicitly told NOT to execute those commands.
SIDE_EFFECT_GUARDED_WEIGHTS = {
    "gate_compliance": 0.339,
    "error_handling": 0.418,
    "quality_checks": 0.133,
    "verify_actions": 0.06,
    "research_first": 0.05,
}

# Failure-mode tests inject a specific failure condition (corrupt state, handoff
# validation error, compaction, regression) and verify the skill handles it per
# its documented recovery path. Gate compliance is the primary signal (did the
# failure gate fire?). Execution dimensions (workflow_sequence, parallel_efficiency,
# state_persistence, output_structure) are inapplicable — the test is about
# failure detection and response, not successful workflow execution.
FAILURE_MODE_WEIGHTS = {
    "gate_compliance": 0.51,
    "error_handling": 0.34,
    "quality_checks": 0.15,
}

# Markers in test_input that indicate error-handling tests
EH_MARKERS = ("error handling", "validation error", "argument validation")

# Markers in runner_context that indicate side-effect guard simulation mode
SEG_MARKERS = ("safety sandbox", "side-effect simulation mode")

# Markers in runner_context that indicate failure-mode tests (injected failure condition)
FM_MARKERS = ("failure condition", "failure_condition")

# Read-only tools that indicate knowledge extraction (no side effects)
READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob"})
# Markers in runner_context that explicitly flag knowledge extraction
KE_MARKERS = ("knowledge extraction", "do not invoke")


def map_grade(score: float) -> str:
    """Map a composite score to a letter grade."""
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


def _test_input_dict(test_result: dict) -> dict:
    """Return test_input as a dict, whatever shape the runner wrote.

    eval runner v2+ writes test_input as an object, but older runners and
    hand-merged results files store the bare prompt string. Every consumer below
    reads it with .get(), so a str here used to raise AttributeError and abort
    the entire scoring run. Coerce once, in one place.
    """
    test_input = test_result.get("test_input")
    return test_input if isinstance(test_input, dict) else {}


def _is_knowledge_extraction(test_result: dict) -> bool:
    """Detect knowledge extraction tests vs execution tests.

    Knowledge extraction tests ask the executor to read a file and answer
    questions. They never invoke the skill under test. Execution-oriented
    dimensions (workflow_sequence, state_persistence, output_structure) are
    inapplicable to these tests.

    Detection uses two signals that must agree:
    1. runner_context contains explicit KE markers
    2. Tool usage is read-only (no Skill/Write/Edit/Bash)
    """
    runner_context = _test_input_dict(test_result).get("runner_context", "").lower()
    has_ke_marker = any(marker in runner_context for marker in KE_MARKERS)

    timeline = test_result.get("execution_timeline") or []
    tools_used = {
        _tool_name(entry)
        for entry in timeline
        if entry.get("step_type") == "tool_use"
    }
    is_read_only = tools_used <= READ_ONLY_TOOLS

    return has_ke_marker and is_read_only


def _is_error_handling_test(test_result: dict) -> bool:
    """Detect error-handling tests that verify graceful early termination.

    Error-handling tests give invalid input (missing args, wrong type,
    conflicting flags) and expect the skill to STOP without executing
    the workflow. Execution dimensions are meaningless for these tests.

    Detection uses three signals (any one sufficient):
    1. test category is "error_handling"
    2. required_absent contains 2+ workflow progression keywords
    3. runner_context contains error-handling markers
    """
    test_input = _test_input_dict(test_result)

    # Signal 1: test category (normalize hyphen/underscore; the schema enum's
    # canonical spelling is "error_handling")
    category = test_input.get("category", "").lower().replace("-", "_")
    if category == "error_handling":
        return True

    # Signal 2: required_absent contains workflow progression keywords.
    # required_present/required_absent are top-level test-case fields per the
    # criteria schema (validate_criteria_schema.py) — the canonical location.
    required_absent = test_input.get("required_absent", [])
    workflow_blockers = {
        "generating eval criteria",
        "launching eval runner",
        "proceeding to Phase",
        "running structural audit",
        "running eval runner",
    }
    blocker_count = sum(
        1
        for absent_phrase in required_absent
        if any(kw in absent_phrase for kw in workflow_blockers)
    )
    if blocker_count >= 2:
        return True

    # Signal 3: runner_context mentions error/validation handling
    runner_context = test_input.get("runner_context", "").lower()
    if any(marker in runner_context for marker in EH_MARKERS):
        return True

    return False


def _is_failure_mode(test_result: dict) -> bool:
    """Detect failure-mode tests where a specific failure condition was injected.

    Failure-mode tests inject a failure condition (corrupt state, handoff
    validation error, compaction, regression) and verify the skill detects
    and handles it per its documented recovery path.

    Execution dimensions (workflow_sequence, parallel_efficiency,
    state_persistence, output_structure) are inapplicable — the correct
    behavior is to detect and halt, not complete the workflow.

    Detection: check runner_context for FAILURE CONDITION markers.
    The explicit test_profile field is checked first in _resolve_test_profile
    and takes precedence; this is only the heuristic fallback.
    """
    test_input = _test_input_dict(test_result)
    runner_context = test_input.get("runner_context", "").lower()
    return any(marker in runner_context for marker in FM_MARKERS)


def _is_side_effect_guarded(test_result: dict) -> bool:
    """Detect side-effect-guarded tests where the executor was told to simulate.

    The side_effect_guard.py script injects a SAFETY SANDBOX block into
    runner_context telling the executor to simulate dangerous commands
    (git push, gh pr create, /forge, /ship, etc.) instead of executing them.

    Execution-oriented dimensions (workflow_sequence, state_persistence)
    are inapplicable because the executor was explicitly told NOT to
    execute those commands. Penalizing simulation compliance is inverted.

    Detection: check runner_context for SAFETY SANDBOX markers, or check
    for a declared test_profile field.
    """
    test_input = _test_input_dict(test_result)

    # Signal 1: explicit test_profile field (preferred, typed)
    profile = test_input.get("test_profile", "").lower()
    if profile == "side_effect_guarded":
        return True

    # Signal 2: SAFETY SANDBOX markers in runner_context (fallback, string-matching)
    runner_context = test_input.get("runner_context", "").lower()
    return any(marker in runner_context for marker in SEG_MARKERS)


def compute_composite(
    scores: dict[str, float],
    weights: dict[str, float],
    critical_dim: str,
) -> float:
    """Weighted geometric mean with dimension floor and critical dim cap.

    The floor is DIMENSION_FLOOR rather than EPSILON so a single zeroed
    dimension caps the composite instead of annihilating it. With EPSILON, one
    zeroed dimension at weight 0.51 produced 1e-6 ** 0.51 == 8.7e-4, three
    orders of magnitude below "the executor did nothing", which made failing
    runs impossible to rank against each other. The critical-dimension cap
    below is the mechanism that expresses "the critical dimension failed", and
    it still fires.
    """
    clamped = {dim: max(score, DIMENSION_FLOOR) for dim, score in scores.items()}
    raw = math.prod(clamped[dim] ** weights[dim] for dim in clamped if dim in weights)
    # Both exits round: the capped branch used to return the raw float, so a
    # capped test carried full float noise into deterministic_scores.json and
    # the aggregate while every other path was rounded to 4 places.
    if scores.get(critical_dim, 1.0) < 0.3:
        return round(min(raw, 0.5), 4)
    return round(raw, 4)


def _extract_steps_from_artifact(artifact_content: str) -> list[str]:
    """Find step markers in artifact content, in document order.

    Matches are collected with their offsets across all patterns and sorted
    by position. Iterating pattern-by-pattern instead would group steps by
    heading style ("## Step 1", "## 2.", ...), and a mixed-style artifact
    would hand score_workflow_sequence a mis-ordered expected sequence.
    """
    positioned: list[tuple[int, str]] = []
    for pattern in STEP_PATTERNS:
        for match in pattern.finditer(artifact_content):
            positioned.append((match.start(), match.group().strip()))
    return [text for _pos, text in sorted(positioned)]


def _tool_name(entry: dict) -> str:
    """The tool name on a timeline entry, under either key the runners emit.

    Eval runners store it as `tool_name` or as the `tool` alias. Half the
    scorers honoured both and half read `tool_name` alone, so a runner using
    `tool` zeroed gate_compliance and state_persistence — the critical
    dimension for two profiles — on a fully compliant run. Every scorer reads
    the name through here so the two shapes cannot diverge again.
    """
    return entry.get("tool_name") or entry.get("tool") or ""


def _get_tool_uses(timeline: list[dict]) -> list[dict]:
    """Filter timeline to tool_use entries only."""
    return [entry for entry in timeline if entry.get("step_type") == "tool_use"]


def _get_text_entries(timeline: list[dict]) -> list[dict]:
    """Filter timeline to text entries only."""
    return [entry for entry in timeline if entry.get("step_type") == "text"]


def score_workflow_sequence(
    timeline: list[dict], artifact_content: str
) -> dict[str, float | str]:
    """Score how well the execution follows the artifact's step sequence.

    Known limitation (v1): When the executor reads the artifact being evaluated
    (eg hone-on-hone), step markers appear in timeline content in document order,
    inflating the score. This is a known false-positive source for self-referential
    evaluations. Future fix: filter tool_result content matching the artifact.
    """
    steps = _extract_steps_from_artifact(artifact_content)
    if not steps:
        return {"score": 1.0, "evidence": "No steps detected in artifact, default 1.0"}

    if not timeline:
        return {"score": 0.0, "evidence": "Empty execution timeline"}

    text_content = " ".join(
        entry.get("content", "") for entry in timeline if entry.get("content")
    )

    steps_found_in_order = 0
    last_pos = -1
    for step in steps:
        step_pattern = re.compile(re.escape(step.strip("#").strip()), re.IGNORECASE)
        # Search only past the previous step's position: searching from offset 0
        # finds the *first* occurrence, which breaks ordering semantics whenever
        # a step name is mentioned before it is executed.
        match = step_pattern.search(text_content, last_pos + 1)
        if match:
            steps_found_in_order += 1
            last_pos = match.start()

    total_steps = max(1, len(steps))
    score = steps_found_in_order / total_steps
    return {
        "score": round(score, 4),
        "evidence": f"{steps_found_in_order}/{total_steps} steps in order",
    }


def _extract_gate_events(
    timeline: list[dict], agent_response: str = ""
) -> list[dict]:
    """Extract gate events from state file writes or agent response text.

    Primary: Write/Edit tool calls to state files with a 'gates' array in content.
    Fallback: scan agent_response for inline gate event JSON (captures simulation mode
    where Write content is not stored in tool_input).
    Returns the most complete gates[] array found.
    """
    # Primary: tool_input.content on Write/Edit calls to state files
    tool_uses = _get_tool_uses(timeline)
    for entry in reversed(tool_uses):
        if _tool_name(entry) not in ("Write", "Edit"):
            continue
        # `or {}` / `or ""` (not .get defaults): tool_use entries can carry an
        # explicit tool_input: null, which the default would pass through.
        tool_input = entry.get("tool_input") or {}
        file_path = tool_input.get("file_path") or ""
        if not STATE_FILE_PATTERN.search(file_path):
            continue
        content = tool_input.get("content") or ""
        if not content:
            continue
        try:
            data = json.loads(content)
            if "gates" in data and isinstance(data["gates"], list):
                return data["gates"]
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: scan agent_response for JSON objects containing gate event fields.
    # Captures simulation mode where executors describe but don't write state files.
    if agent_response:
        candidates: list[dict] = []
        # Lookaheads make the key match order-independent: any ordering of
        # step/judge/result inside the {...} blob is accepted.
        for match in re.finditer(
            r'\{(?=[^{}]*"step")(?=[^{}]*"judge")(?=[^{}]*"result")[^{}]*\}',
            agent_response,
        ):
            try:
                obj = json.loads(match.group())
                if "step" in obj and "judge" in obj and "result" in obj:
                    candidates.append(obj)
            except (json.JSONDecodeError, TypeError):
                pass
        if candidates:
            return candidates

    return []


def _is_well_formed_gate(gate: dict) -> bool:
    """A gate event is well-formed when it has the required keys and a valid result."""
    if not all(key in gate for key in ("step", "judge", "result")):
        return False
    return gate.get("result") in ("pass", "fail")


def score_gate_compliance(
    timeline: list[dict], agent_response: str, artifact_content: str = ""
) -> dict[str, float | str]:
    """Score gate compliance by checking for structured gate events in state file.

    Primary: parse gates[] array from state file writes in the timeline.
    Each gate event must have step, judge, and result fields.

    This dimension scores gate *emission and schema validity*, not gate outcome.
    A gate that correctly reports failure is compliant: penalizing it would
    reward executors for hiding failures. A 'fail' event counts as compliant
    when it is terminal (the pipeline halted there, e.g. a Phase 3 regression
    auto-revert or an error halt) or when a later 'pass' for the same step
    records the repair (e.g. a handoff validation retry loop). A 'fail' that is
    followed by unrelated forward progress is still non-compliant.

    Fallback (legacy): keyword counting when no structured gate events are found.
    Legacy score is capped at 0.7 to incentivize migration to structured events.
    """
    # Primary path: structured gate events (timeline writes or inline in agent_response)
    gates = _extract_gate_events(timeline, agent_response)

    if gates:
        total = len(gates)
        well_formed = sum(1 for g in gates if _is_well_formed_gate(g))

        compliant = 0
        expected_fail = 0
        for idx, gate in enumerate(gates):
            if not _is_well_formed_gate(gate):
                continue
            if gate.get("result") == "pass":
                compliant += 1
                continue
            # result == "fail": compliant only when failure is the documented outcome.
            terminal = idx == len(gates) - 1
            repaired = any(
                later.get("step") == gate.get("step")
                and later.get("result") == "pass"
                for later in gates[idx + 1 :]
            )
            if terminal or repaired:
                compliant += 1
                expected_fail += 1

        if compliant == total and well_formed == total:
            evidence = f"All {total} gate(s) compliant with structured events"
            if expected_fail:
                evidence += f" ({expected_fail} expected-fail)"
            return {"score": 1.0, "evidence": evidence}

        malformed = total - well_formed
        noncompliant = total - compliant
        # Malformed gates never reach `compliant` (the loop skips them), so
        # they already count against the score via the denominator. A second
        # well_formed/total factor squared the penalty: 1 good + 1 malformed
        # scored 0.25 instead of 0.5.
        score = compliant / total
        evidence = f"{compliant}/{total} gate(s) compliant"
        if expected_fail:
            evidence += f" ({expected_fail} expected-fail)"
        if malformed:
            evidence += f", {malformed} malformed"
        if noncompliant:
            evidence += f", {noncompliant} non-compliant"
        return {"score": round(score, 4), "evidence": evidence}

    # Legacy fallback: keyword counting, capped at 0.7
    combined = agent_response
    for entry in timeline:
        content = entry.get("content", "")
        if content:
            combined += " " + content

    gate_matches = GATE_KEYWORDS.findall(combined)
    gate_count = len(gate_matches)

    if gate_count == 0:
        return {"score": 0.0, "evidence": "No gate/validation patterns found"}

    steps = _extract_steps_from_artifact(artifact_content) if artifact_content else []
    expected_gates = max(1, len(steps) - 1) if len(steps) >= 2 else 3
    raw_score = min(1.0, gate_count / expected_gates)
    capped_score = min(0.7, raw_score)
    return {
        "score": round(capped_score, 4),
        "evidence": (
            f"{gate_count} keyword gate indicator(s) found"
            " (legacy; structured gate events needed for >0.7)"
        ),
    }


def score_state_persistence(timeline: list[dict]) -> dict[str, float | str]:
    """Score whether workflow state files were written.

    Checks both Write/Edit tool calls and Bash commands that write to
    state file paths (cat/echo/heredoc redirects to workflow-*.json).
    """
    tool_uses = _get_tool_uses(timeline)

    for entry in tool_uses:
        tool_name = _tool_name(entry)
        if tool_name in ("Write", "Edit"):
            # `or {}` / `or ""`: tool_use entries can carry explicit nulls.
            tool_input = entry.get("tool_input") or {}
            file_path = tool_input.get("file_path") or ""
            if STATE_FILE_PATTERN.search(file_path):
                return {"score": 1.0, "evidence": f"State file written: {file_path}"}
        elif tool_name == "Bash":
            # Check for cat/echo/heredoc writes to state files in the command
            command = (entry.get("tool_input") or {}).get("command") or ""
            if STATE_FILE_PATTERN.search(command) and any(
                redirect in command for redirect in (">", "cat >", "tee ", ">>")
            ):
                return {
                    "score": 1.0,
                    "evidence": "State file written via Bash redirect",
                }

    return {"score": 0.0, "evidence": "No state file writes detected"}


def score_output_structure(
    agent_response: str, artifact_content: str
) -> dict[str, float | str]:
    """Score whether the response has expected output sections.

    Detects both markdown headings (## Section) and box-drawing patterns
    (━━━━━) used by orchestration commands for gate presentations.
    """
    heading_pattern = HEADING_PATTERN
    expected_headings = heading_pattern.findall(artifact_content)

    output_keywords = [
        h.strip()
        for h in expected_headings
        if any(
            kw in h.lower()
            for kw in ("output", "summary", "result", "recommendation", "finding")
        )
    ]

    if not output_keywords:
        # Fall back: check if the artifact uses box-drawing for output structure
        has_box_drawing_artifact = bool(BOX_DRAWING_PATTERN.search(artifact_content))
        if has_box_drawing_artifact:
            has_box_drawing_response = bool(BOX_DRAWING_PATTERN.search(agent_response))
            if has_box_drawing_response:
                return {
                    "score": 1.0,
                    "evidence": "Box-drawing output structure detected",
                }
            return {
                "score": 0.5,
                "evidence": "Artifact uses box-drawing but response does not",
            }
        return {"score": 1.0, "evidence": "No expected output sections in artifact"}

    found = 0
    for keyword in output_keywords:
        if re.search(re.escape(keyword), agent_response, re.IGNORECASE):
            found += 1

    total = max(1, len(output_keywords))
    score = found / total
    return {
        "score": round(score, 4),
        "evidence": f"{found}/{total} expected output sections found",
    }


def score_voice_compliance(agent_response: str) -> dict[str, float | str]:
    """Score AI slop pattern prevalence in the response.

    Strips quoted/referenced content before scanning to avoid penalizing
    executors that quote artifact content containing slop patterns (eg
    em dashes in the artifact's own prose).
    """
    if not agent_response or not agent_response.strip():
        return {"score": 0.0, "evidence": "Empty response"}

    # Strip code blocks
    prose = CODE_BLOCK_PATTERN.sub("", agent_response)
    # Strip blockquotes (lines starting with >)
    prose = BLOCKQUOTE_PATTERN.sub("", prose)
    # Strip inline code spans (content between backticks)
    prose = INLINE_CODE_PATTERN.sub("", prose)

    table_sep = TABLE_SEP_PATTERN
    prose_lines = [
        line
        for line in prose.strip().split("\n")
        if line.strip()
        and not table_sep.match(line)
        and not BACKTICK_HEAVY_LINE.match(line)
    ]

    if not prose_lines:
        return {"score": 1.0, "evidence": "Code-only response, no prose to check"}

    # Count violations over the same filtered lines the denominator uses.
    # Scanning the unfiltered prose counts hits on table-separator and
    # backtick-heavy lines that were excluded from line_count, so the
    # violations/lines ratio could exceed 1 and zero out clean responses.
    prose_text = "\n".join(prose_lines)
    violations = 0
    violations += len(EM_DASH_PATTERN.findall(prose_text))
    violations += len(STACCATO_PATTERN.findall(prose_text))
    violations += len(NEGATION_ASSERTION.findall(prose_text))
    violations += len(BANNED_PHRASES.findall(prose_text))

    line_count = max(1, len(prose_lines))
    score = max(0.0, 1.0 - (violations / line_count))
    return {
        "score": round(score, 4),
        "evidence": f"{violations} slop violation(s) in {line_count} prose lines",
    }


def score_parallel_efficiency(timeline: list[dict]) -> dict[str, float | str]:
    """Score whether parallel tool use batches were detected."""
    if not timeline:
        return {"score": 1.0, "evidence": "Empty timeline, default 1.0"}

    tool_uses = _get_tool_uses(timeline)
    if len(tool_uses) < 2:
        return {"score": 1.0, "evidence": "Too few tool uses to judge parallelism"}

    step_index_groups: dict[int, int] = {}
    for entry in tool_uses:
        # A `.get` default only covers an *absent* key. An explicit
        # `"step_index": null` flowed straight into `idx >= 0` and raised
        # TypeError, which the per-test handler recorded as a 0.0 failure for
        # the whole test. The isinstance check also drops a stringified index.
        idx = entry.get("step_index")
        if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0:
            step_index_groups[idx] = step_index_groups.get(idx, 0) + 1

    parallel_batches = sum(1 for count in step_index_groups.values() if count > 1)
    total_groups = max(1, len(step_index_groups))

    if parallel_batches == 0:
        potential = sum(1 for count in step_index_groups.values() if count == 1)
        if potential <= 2:
            return {
                "score": 1.0,
                "evidence": "No parallel opportunities detected",
            }
        score = 0.5
        return {
            "score": score,
            "evidence": f"0 parallel batches out of {total_groups} groups",
        }

    score = min(1.0, parallel_batches / max(1, total_groups))
    return {
        "score": round(score, 4),
        "evidence": f"{parallel_batches} parallel batch(es) in {total_groups} groups",
    }


def score_error_handling(timeline: list[dict]) -> dict[str, float | str]:
    """Score error recovery: errors followed by diagnostic tool calls."""
    error_entries = []
    for index, entry in enumerate(timeline):
        if entry.get("is_error"):
            error_entries.append(index)

    if not error_entries:
        return {"score": 1.0, "evidence": "No errors encountered"}

    recovered = 0
    diagnostic_tools = {"Read", "Bash", "Grep", "Glob"}

    for error_idx in error_entries:
        for follow_idx in range(error_idx + 1, min(error_idx + 4, len(timeline))):
            follow = timeline[follow_idx]
            if (
                follow.get("step_type") == "tool_use"
                and _tool_name(follow) in diagnostic_tools
            ):
                recovered += 1
                break

    total_errors = max(1, len(error_entries))
    score = recovered / total_errors
    return {
        "score": round(score, 4),
        "evidence": f"{recovered}/{total_errors} errors recovered from",
    }


def score_trigger_accuracy(
    timeline: list[dict], test_input: dict | None = None
) -> dict[str, float | str]:
    """Score hook execution as the non-crashing fraction of Bash calls.

    This is a crash-rate proxy, not true trigger/false-positive measurement:
    the timeline carries no stdout and the criteria carry no per-test trigger
    expectations, so "should trigger" and "should not trigger" cases cannot be
    distinguished. Only crashes (is_error=True) are penalized. A previous
    false_positive_rate dimension computed this same quantity and was
    collapsed into this one at their combined weight (see HOOK_WEIGHTS).
    """
    tool_uses = _get_tool_uses(timeline)
    bash_calls = [t for t in tool_uses if _tool_name(t) == "Bash"]

    if not bash_calls:
        return {"score": 1.0, "evidence": "No Bash calls to evaluate"}

    correct = 0
    total = len(bash_calls)
    for bash_call in bash_calls:
        if not bash_call.get("is_error"):
            correct += 1

    score = correct / max(1, total)
    return {
        "score": round(score, 4),
        "evidence": f"{correct}/{total} Bash calls behaved correctly",
    }


def score_performance(
    duration_seconds: float, budget: float = 1.0
) -> dict[str, float | str]:
    """Score time performance against a budget.

    Callers must pass a budget the criteria author declared
    (`performance_budget_seconds` on the test case); the 1.0s default is only
    meaningful for directly measured hook/script execution, never for eval
    wall time.
    """
    if duration_seconds <= budget:
        return {
            "score": 1.0,
            "evidence": f"{duration_seconds:.1f}s <= {budget:.1f}s budget",
        }

    if duration_seconds >= budget * 3:
        return {"score": 0.0, "evidence": f"{duration_seconds:.1f}s >= 3x budget"}

    score = 1.0 - ((duration_seconds - budget) / (budget * 2))
    return {
        "score": round(max(0.0, score), 4),
        "evidence": f"{duration_seconds:.1f}s (budget: {budget:.1f}s)",
    }


def score_correctness(
    timeline: list[dict], expected_output: str | None = None
) -> dict[str, float | str]:
    """Score script correctness from tool call results."""
    tool_uses = _get_tool_uses(timeline)

    if not tool_uses:
        return {"score": 1.0, "evidence": "No tool calls to evaluate"}

    correct = sum(1 for t in tool_uses if not t.get("is_error"))
    total = max(1, len(tool_uses))
    score = correct / total
    return {
        "score": round(score, 4),
        "evidence": f"{correct}/{total} tool calls succeeded",
    }


def score_output_format(agent_response: str) -> dict[str, float | str]:
    """Score whether output has parseable structure."""
    if not agent_response:
        return {"score": 0.0, "evidence": "Empty response"}

    checks = 0
    total_checks = 3

    if re.search(r"```", agent_response):
        checks += 1
    if re.search(r"^#{1,4}\s+", agent_response, re.MULTILINE):
        checks += 1
    if re.search(r"^\s*[-*]\s+", agent_response, re.MULTILINE):
        checks += 1

    score = checks / total_checks
    return {
        "score": round(score, 4),
        "evidence": f"{checks}/{total_checks} format checks passed",
    }


def score_early_termination(timeline: list[dict]) -> dict[str, float | str]:
    """Score whether executor correctly stopped before workflow execution.

    Error-handling tests expect the skill to detect invalid input and stop
    without writing state files or progressing through workflow steps.
    Low tool call count + no state writes = correct early termination.
    """
    tool_uses = _get_tool_uses(timeline)
    tool_count = len(tool_uses)

    # Check for workflow-progression indicators (should be absent)
    # `or {}` / `or ""`: tool_use entries can carry explicit nulls.
    wrote_state = any(
        _tool_name(t) in ("Write", "Edit")
        and STATE_FILE_PATTERN.search((t.get("tool_input") or {}).get("file_path") or "")
        for t in tool_uses
    )

    if wrote_state:
        return {
            "score": 0.0,
            "evidence": "Wrote workflow state (should have stopped early)",
        }

    if tool_count > 15:
        return {
            "score": 0.3,
            "evidence": f"{tool_count} tool calls (expected < 15 for early stop)",
        }

    return {
        "score": 1.0,
        "evidence": f"{tool_count} tool calls, no workflow progression",
    }


def score_user_communication(
    timeline: list[dict], agent_response: str = ""
) -> dict[str, float | str]:
    """Score whether executor communicated the error to the user.

    Error-handling tests expect the skill to either use AskUserQuestion
    (preferred for arg validation) or output explanatory text.

    Three tiers:
    - AskUserQuestion called as tool (tool_use): 1.0
    - AskUserQuestion attempted but unavailable (condition_fired / fallback): 0.9
    - Text output only: 0.7
    - No communication: 0.0

    Simulation mode fallback: when execution_timeline is empty (eval runner in
    SIMULATION MODE), check agent_response for error communication patterns.
    Mirrors the same fallback used in score_gate_compliance for simulation mode.
    """
    tool_uses = _get_tool_uses(timeline)

    # Best: AskUserQuestion called successfully as a tool.
    used_ask_tool = any(_tool_name(t) == "AskUserQuestion" for t in tool_uses)
    if used_ask_tool:
        return {
            "score": 1.0,
            "evidence": "Used AskUserQuestion as tool for error communication",
        }

    # Good: AskUserQuestion attempted but unavailable, or fallback output emitted.
    # Signals: condition_fired step (tool-unavailable gate fired), fallback_text_output /
    # fallback_output step types (eval runner records these when the tool is absent),
    # or ToolSearch with "AskUserQuestion" in content (skill tried to find the tool).
    attempted_ask = any(
        e.get("step_type") in {"condition_fired", "fallback_text_output", "fallback_output"}
        for e in timeline
    ) or any(
        _tool_name(t) == "ToolSearch" and "AskUserQuestion" in (t.get("content") or "")
        for t in tool_uses
    )
    if attempted_ask:
        return {
            "score": 0.9,
            "evidence": "AskUserQuestion attempted but unavailable; used fallback",
        }

    # Acceptable: any text output (step_type "text", "fallback_text_output", "fallback_output")
    text_entries = [
        e for e in timeline
        if e.get("step_type") in {"text", "fallback_text_output", "fallback_output"}
    ]
    has_text = any((entry.get("content") or "").strip() for entry in text_entries)
    if has_text:
        return {
            "score": 0.7,
            "evidence": "Reported error as text (AskUserQuestion preferred)",
        }

    # Simulation mode fallback: execution_timeline is empty when eval runner runs
    # in SIMULATION MODE (no real tool calls). The skill's response text is all we
    # have, so detect communication from agent_response directly.
    if agent_response and agent_response.strip():
        response_lower = agent_response.lower()

        # Best available in sim mode: the skill (correctly) chose to ask the user
        # for missing input. It cannot issue a real AskUserQuestion tool call, so
        # it surfaces the question as text. Treat this as the "attempted" tier:
        # the action is right, only the harness cannot record a tool_use. Without
        # this branch, a correct empty-state clarification scores 0.0 purely
        # because its message ("No uncommitted changes found. Which files should
        # I scan?") contains none of the error keywords below.
        asks_user = (
            "askuserquestion" in response_lower
            or ("ask" in response_lower and "user" in response_lower)
            or (
                "?" in agent_response
                and any(
                    kw in response_lower
                    for kw in (
                        "which file", "which files", "what file",
                        "would you like", "what would you like",
                        "please specify", "please provide",
                        "let me know which", "tell me which", "should i",
                    )
                )
            )
        )
        if asks_user:
            return {
                "score": 0.9,
                "evidence": (
                    "Surfaced AskUserQuestion/clarification in response"
                    " (simulation mode; tool call not recordable)"
                ),
            }

        # Otherwise: any substantive error- or empty-state report scores 0.7.
        # Must contain error-relevant content, not just step narration.
        error_indicators = [
            "file not found", "not found", "no files", "no uncommitted changes",
            "no changes", "empty", "too complex", "stop", "error",
            "cannot", "requires", "verify the path",
            "missing", "invalid", "failed", "cannot proceed", "arguments",
        ]
        has_error_content = any(ind in response_lower for ind in error_indicators)
        if has_error_content and len(agent_response.strip()) > 50:
            return {
                "score": 0.7,
                "evidence": "Reported error/empty-state in agent response (simulation mode; AskUserQuestion preferred)",
            }

    return {"score": 0.0, "evidence": "No error communication to user"}


# Explicit test_profile -> weight map. Checked before heuristic detection.
PROFILE_WEIGHT_MAP = {
    "execution": None,  # use artifact-type default (SKILL_WEIGHTS etc.)
    "knowledge_extraction": KNOWLEDGE_EXTRACTION_WEIGHTS,
    "error_handling": ERROR_HANDLING_WEIGHTS,
    "side_effect_guarded": SIDE_EFFECT_GUARDED_WEIGHTS,
    "failure_mode": FAILURE_MODE_WEIGHTS,
}


def _load_criteria_index(criteria_path: str | None) -> dict[str, dict]:
    """Load eval_criteria.json and return dict keyed by test_id.

    Type-tolerant on every hop, matching hone_common's loaders: main() calls
    this *outside* the try/except that wraps score_from_results, so a
    schema-shaped surprise here killed the whole scoring step with a
    traceback instead of degrading. A top-level list made `data.get` raise
    AttributeError; a string test case passed the `"id" in tc` substring test
    and then raised TypeError on `tc["id"]`.
    """
    if not criteria_path:
        return {}
    try:
        with open(criteria_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        tc["id"]: tc
        for tc in typed_get(data, "test_cases", [], expected=list)
        if isinstance(tc, dict) and isinstance(tc.get("id"), str)
    }


def score_quality_checks(
    agent_response: str,
    timeline: list[dict],
    required_present: list[str],
    required_absent: list[str],
) -> dict[str, float | str]:
    """Score required_present and required_absent assertions from eval_criteria."""
    # required_absent matching is negation-aware: see _has_unnegated_occurrence.
    if not required_present and not required_absent:
        return {"score": 1.0, "evidence": "No quality assertions defined"}

    combined = agent_response
    for entry in timeline:
        content = entry.get("content", "")
        if content:
            combined += " " + content

    checks_total = len(required_present) + len(required_absent)
    checks_passed = 0
    violations: list[str] = []

    for phrase in required_present:
        if re.search(re.escape(phrase), combined, re.IGNORECASE):
            checks_passed += 1
        else:
            violations.append(f"MISSING: '{phrase}'")

    for phrase in required_absent:
        if not _has_unnegated_occurrence(phrase, combined):
            checks_passed += 1
        else:
            violations.append(f"FORBIDDEN present: '{phrase}'")

    score = checks_passed / max(1, checks_total)
    evidence = f"{checks_passed}/{checks_total} quality checks passed"
    if violations:
        evidence += f"; {'; '.join(violations[:3])}"

    return {"score": round(score, 4), "evidence": evidence}


_WRITE_TOOL_NAMES = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_READ_TOOL_NAMES = frozenset({"Read", "Glob", "Grep"})
_WRITE_CONTENT_RE = re.compile(r"^(?:Edit|Write|Update|Overwrite)\b", re.IGNORECASE)
_READ_CONTENT_RE = re.compile(r"^(?:Read|Glob|Grep)\b", re.IGNORECASE)
_VERIFY_CONTENT_RE = re.compile(
    r"\bre-?read\b|\bread.{0,20}back\b|\bconfirm.{0,30}content\b|\bverif",
    re.IGNORECASE,
)
# State/temp paths that are initialization writes, not artifact modifications
_TEMP_PATH_RE = re.compile(r"/tmp/|workflow[-_]state|state\.json", re.IGNORECASE)


def _is_write_entry(entry: dict) -> bool:
    tool = _tool_name(entry)
    if tool in _WRITE_TOOL_NAMES:
        return True
    # `or ""` (not a .get default): tool_use entries commonly carry content: null,
    # and a None here would raise TypeError inside re and zero the whole test.
    return bool(_WRITE_CONTENT_RE.match(entry.get("content") or ""))


def _is_artifact_write_entry(entry: dict) -> bool:
    """Write to an artifact — excludes temp/state-file initialization.

    Real tool_use entries carry their path in tool_input["file_path"];
    simulation-mode entries describe the write in prose under "content".
    Check both so state-file writes are excluded in either shape.
    """
    tool_input = entry.get("tool_input") or {}
    file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
    if file_path:
        # A real path is authoritative: classify on it alone. Content sniffing
        # here would let a Write to SKILL.md whose echoed content merely
        # mentions /tmp/ or state files (exactly what hone's conventions tell
        # skills to document) masquerade as a temp/state write.
        if _TEMP_PATH_RE.search(file_path):
            return False
        return _is_write_entry(entry)
    content = entry.get("content") or ""
    if _TEMP_PATH_RE.search(content):
        return False
    return _is_write_entry(entry)


def _is_read_entry(entry: dict) -> bool:
    tool = _tool_name(entry)
    if tool in _READ_TOOL_NAMES:
        return True
    return bool(_READ_CONTENT_RE.match(entry.get("content") or ""))


def _is_verify_entry(entry: dict) -> bool:
    tool = _tool_name(entry)
    if tool in _READ_TOOL_NAMES or tool == "Bash":
        return True
    return bool(_VERIFY_CONTENT_RE.search(entry.get("content") or ""))


def score_verify_actions(timeline: list[dict]) -> dict[str, float | str]:
    """Score: were consequential writes followed by verification?

    Each Edit/Write should be followed by a read-back or verification
    within the next 3 tool calls. Unchecked writes are the primary source
    of false completion claims (claiming 'done' without closing the loop).
    """
    tool_uses = _get_tool_uses(timeline)
    write_indices = [i for i, e in enumerate(tool_uses) if _is_write_entry(e)]

    if not write_indices:
        return {"score": 1.0, "evidence": "No write actions; verification not required"}

    verified = 0
    for idx in write_indices:
        lookahead = tool_uses[idx + 1 : idx + 4]
        if any(_is_verify_entry(e) for e in lookahead):
            verified += 1

    score = verified / len(write_indices)
    evidence = f"{verified}/{len(write_indices)} writes followed by verification"
    return {"score": round(score, 4), "evidence": evidence}


def score_research_first(timeline: list[dict]) -> dict[str, float | str]:
    """Score: did the executor read/research before making the first artifact write?

    Skills should read reference files and understand the artifact before
    making changes. Writes before any reads suggest uninformed edits.
    Temp/state-file writes (workflow state init) are excluded.
    """
    tool_uses = _get_tool_uses(timeline)

    first_write_idx = next(
        (i for i, e in enumerate(tool_uses) if _is_artifact_write_entry(e)), None
    )
    if first_write_idx is None:
        return {"score": 1.0, "evidence": "No artifact writes; research check not applicable"}

    reads_before = sum(1 for e in tool_uses[:first_write_idx] if _is_read_entry(e))

    if reads_before >= 3:
        score, label = 1.0, "sufficient"
    elif reads_before == 2:
        score, label = 0.85, "adequate"
    elif reads_before == 1:
        score, label = 0.6, "minimal"
    else:
        score, label = 0.0, "none"

    evidence = f"{reads_before} reads before first artifact write ({label})"
    return {"score": score, "evidence": evidence}


def _resolve_test_profile(
    test_result: dict, criteria_index: dict | None = None
) -> str | None:
    """Resolve test profile from explicit field, falling back to heuristics.

    Returns one of: 'knowledge_extraction', 'error_handling',
    'side_effect_guarded', 'failure_mode', 'execution', or None (use artifact-type default).
    """
    test_input = _test_input_dict(test_result)

    # Prefer explicit test_profile field in test_input (set by eval runner v2+)
    profile = test_input.get("test_profile", "")
    if profile in PROFILE_WEIGHT_MAP:
        return profile

    # Fall back to criteria_index lookup (eval runner stores summarized test_input
    # that omits test_profile; the original eval_criteria.json has the authoritative value)
    if criteria_index:
        test_id = test_result.get("test_id", "")
        crit = criteria_index.get(test_id, {})
        profile = crit.get("test_profile", "")
        if profile in PROFILE_WEIGHT_MAP:
            return profile

    # Last resort: heuristic detection (no criteria_index available)
    if _is_knowledge_extraction(test_result):
        return "knowledge_extraction"
    if _is_error_handling_test(test_result):
        return "error_handling"
    if _is_side_effect_guarded(test_result):
        return "side_effect_guarded"
    if _is_failure_mode(test_result):
        return "failure_mode"

    return None


def _renormalize_weights(
    weights: dict[str, float], dimensions: dict
) -> dict[str, float]:
    """Restrict weights to the scored dimensions and rescale them to sum to 1."""
    active = {dim: weights[dim] for dim in dimensions if dim in weights}
    total = sum(active.values())
    if total > 0 and abs(total - 1.0) > 1e-9:
        active = {dim: weight / total for dim, weight in active.items()}
    return active


def _score_single_test(
    test_result: dict,
    artifact_type: str,
    artifact_content: str = "",
    criteria_index: dict[str, dict] | None = None,
) -> dict:
    """Score a single test result across all applicable dimensions.

    A skill/command test with an empty execution timeline is returned with
    `status: "inconclusive"` and `composite: None`; callers must exclude it
    from aggregation rather than average a fabricated number.
    """
    inconclusive = False
    timeline = test_result.get("execution_timeline") or []
    # `or ""`: an explicit `"agent_response": null` is a real shape from a
    # runner whose test crashed, and None crashes every string consumer below.
    agent_response = test_result.get("agent_response") or ""
    test_input = _test_input_dict(test_result)
    # None (not 0.0) when timing is absent: a missing duration must score as
    # unknown, not as an instantaneous run that aces the performance budget.
    duration = test_result.get("duration_seconds")
    partial_scoring = False

    # Resolve test profile: explicit field → criteria_index → heuristics
    profile = _resolve_test_profile(test_result, criteria_index)
    is_ke = profile == "knowledge_extraction"
    is_eh = profile == "error_handling"
    is_seg = profile == "side_effect_guarded"
    is_fm = profile == "failure_mode"

    # Load quality assertions from criteria_index (if provided)
    test_id = test_result.get("test_id", "unknown")
    # typed_get, not dict.get: the criteria schema marks these optional
    # without rejecting null, so an explicit `"required_present": null`
    # reached `len(None)` and recorded a fabricated 0.0 for a passing test.
    # validate_eval_criteria.py reads the same two fields the same way.
    crit = typed_get(criteria_index or {}, test_id, {}, expected=dict)
    required_present = typed_get(crit, "required_present", [], expected=list)
    required_absent = typed_get(crit, "required_absent", [], expected=list)
    # Performance is scored only against a budget the criteria author declared.
    # duration_seconds is the wall time of the whole agentic eval test (tens of
    # seconds), so scoring it against the function's 1.0s default floored the
    # dimension to 0.0 on every timed run. No declared budget -> dimension is
    # skipped and weights renormalize, same as an untimed run.
    perf_budget = typed_get(crit, "performance_budget_seconds", expected=(int, float))

    if artifact_type in ("skill", "command"):
        dimensions: dict[str, dict] = {}

        if is_ke:
            # Knowledge extraction: LLM judge handles semantic evaluation.
            # Deterministic scoring only checks for errors (typically 1.0).
            dimensions["error_handling"] = score_error_handling(timeline)
            active_weights = dict(KNOWLEDGE_EXTRACTION_WEIGHTS)
            # One dimension at weight 1.0, and score_error_handling([]) is a
            # constant 1.0 — the same shape the empty-timeline branch below is
            # marked inconclusive for. Without this an empty answer scored a
            # composite 1.0, which resolve_score then preferred over a judge
            # who had scored the same answer 0.15.
            if not timeline or not agent_response.strip():
                partial_scoring = True
                inconclusive = True
                active_weights = {}
        elif is_eh:
            # Error-handling: score on early termination and user communication.
            # Execution dimensions (workflow, state) are meaningless here
            # because the correct behavior is to NOT execute the workflow.
            dimensions["early_termination"] = score_early_termination(timeline)
            dimensions["user_communication"] = score_user_communication(timeline, agent_response)
            dimensions["quality_checks"] = score_quality_checks(
                agent_response, timeline, required_present, required_absent
            )
            active_weights = dict(ERROR_HANDLING_WEIGHTS)
        elif is_seg:
            # Side-effect guarded: executor was told to simulate dangerous
            # commands. Execution dimensions (workflow_sequence, state_persistence)
            # are inapplicable because the guard explicitly prevents execution.
            # Output structure is also inapplicable in simulation mode.
            # Score only dimensions that survive simulation.
            dimensions["gate_compliance"] = score_gate_compliance(
                timeline, agent_response, artifact_content
            )
            dimensions["error_handling"] = score_error_handling(timeline)
            dimensions["quality_checks"] = score_quality_checks(
                agent_response, timeline, required_present, required_absent
            )
            dimensions["verify_actions"] = score_verify_actions(timeline)
            dimensions["research_first"] = score_research_first(timeline)
            active_weights = dict(SIDE_EFFECT_GUARDED_WEIGHTS)
        elif is_fm:
            # Failure-mode: a specific failure condition was injected (corrupt
            # state, handoff validation error, compaction, regression). Score
            # whether the failure gate fired (gate_compliance) and whether the
            # skill communicated the failure gracefully (error_handling).
            # Execution dimensions are inapplicable — correct behavior is to
            # detect and halt, not to complete the workflow.
            dimensions["gate_compliance"] = score_gate_compliance(
                timeline, agent_response, artifact_content
            )
            dimensions["error_handling"] = score_error_handling(timeline)
            dimensions["quality_checks"] = score_quality_checks(
                agent_response, timeline, required_present, required_absent
            )
            active_weights = dict(FAILURE_MODE_WEIGHTS)
        elif timeline:
            dimensions["workflow_sequence"] = score_workflow_sequence(
                timeline, artifact_content
            )
            dimensions["gate_compliance"] = score_gate_compliance(
                timeline, agent_response, artifact_content
            )
            dimensions["parallel_efficiency"] = score_parallel_efficiency(timeline)
            dimensions["state_persistence"] = score_state_persistence(timeline)
            dimensions["output_structure"] = score_output_structure(
                agent_response, artifact_content
            )
            dimensions["voice_compliance"] = score_voice_compliance(agent_response)
            dimensions["error_handling"] = score_error_handling(timeline)
            dimensions["quality_checks"] = score_quality_checks(
                agent_response, timeline, required_present, required_absent
            )
            dimensions["verify_actions"] = score_verify_actions(timeline)
            dimensions["research_first"] = score_research_first(timeline)

            weights = SKILL_WEIGHTS if artifact_type == "skill" else COMMAND_WEIGHTS
            active_weights = _renormalize_weights(weights, dimensions)
        else:
            # Empty timeline (simulation mode) has no execution evidence:
            # error_handling([]) is a constant 1.0, which used to yield
            # composite 1.0 for arbitrarily bad output. Mark inconclusive;
            # dimensions stay visible but no composite is manufactured.
            partial_scoring = True
            inconclusive = True
            dimensions["voice_compliance"] = score_voice_compliance(agent_response)
            dimensions["error_handling"] = score_error_handling(timeline)
            active_weights = {}

    elif artifact_type == "hook":
        dimensions = {
            "trigger_accuracy": score_trigger_accuracy(timeline, test_input),
            "output_structure": score_output_structure(
                agent_response, artifact_content
            ),
            "error_handling": score_error_handling(timeline),
        }
        # Untimed results (no duration_seconds key) and runs without a
        # declared performance_budget_seconds skip the performance dimension
        # entirely; the remaining weights are renormalized below.
        if duration is not None and perf_budget:
            dimensions["performance"] = score_performance(duration, perf_budget)
        active_weights = _renormalize_weights(HOOK_WEIGHTS, dimensions)

    elif artifact_type == "script":
        dimensions = {
            "correctness": score_correctness(timeline),
            "output_format": score_output_format(agent_response),
            "output_structure": score_output_structure(
                agent_response, artifact_content
            ),
            "error_handling": score_error_handling(timeline),
        }
        if duration is not None and perf_budget:
            dimensions["performance"] = score_performance(duration, perf_budget)
        active_weights = _renormalize_weights(SCRIPT_WEIGHTS, dimensions)

    else:
        return {
            "test_id": test_result.get("test_id", "unknown"),
            "composite": 0.0,
            "dimensions": {},
            "partial_scoring": True,
        }

    scores = {dim: dim_result["score"] for dim, dim_result in dimensions.items()}
    critical_dim = CRITICAL_DIMS.get(artifact_type, "workflow_sequence")
    # KE, EH, SEG, and FM tests have no workflow_sequence, so skip critical dim floor
    if is_ke:
        critical_dim = "error_handling"
    elif is_eh:
        critical_dim = "early_termination"
    elif is_seg or is_fm:
        critical_dim = "gate_compliance"

    if inconclusive:
        composite = None
    elif scores:
        composite = compute_composite(scores, active_weights, critical_dim)
    else:
        composite = 0.0

    if is_ke:
        test_type_label = "knowledge_extraction"
    elif is_eh:
        test_type_label = "error_handling"
    elif is_seg:
        test_type_label = "side_effect_guarded"
    elif is_fm:
        test_type_label = "failure_mode"
    else:
        test_type_label = "execution"

    scored: dict = {
        "test_id": test_result.get("test_id", "unknown"),
        "composite": composite,
        "dimensions": {
            dim: {"score": dim_result["score"], "evidence": dim_result["evidence"]}
            for dim, dim_result in dimensions.items()
        },
        "partial_scoring": partial_scoring,
        "test_type": test_type_label,
    }
    if inconclusive:
        scored["status"] = "inconclusive"
    return scored


def score_from_results(
    results_path: str,
    artifact_type: str,
    artifact_content: str = "",
    criteria_index: dict[str, dict] | None = None,
) -> dict:
    """Score all test results from a results.json file."""
    try:
        with open(results_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "composite_score": 0.0,
            "grade": "F",
            "per_test": [],
            "aggregate_dimensions": {},
            "metadata": {"error": str(exc)},
        }

    # Accept `results` (canonical hone format) or `test_results` (skill-creator
    # alias) — shared with analyze_results via hone_common so the two scripts
    # cannot disagree about which files contain tests. Fail loud on schema
    # mismatch instead of silently returning 0.0/F: a perfect artifact with the
    # wrong top-level key should not look like a catastrophic failure.
    results, results_key_used = extract_results(data)

    if not results:
        top_level_keys = sorted(data.keys()) if isinstance(data, dict) else []
        if results_key_used is not None:
            # Valid schema, but no test entries ran.
            error_reason = "empty_results"
            hint = f"File has '{results_key_used}' key but the array is empty. No tests ran."
        elif top_level_keys:
            # Schema mismatch: file has content but neither recognized key.
            error_reason = "schema_mismatch"
            hint = (
                f"File has top-level keys {top_level_keys} but no 'results' or "
                f"'test_results' array. Expected: "
                f"{{\"results\": [{{\"test_id\", \"agent_response\", \"execution_timeline\", ...}}]}}."
            )
        else:
            error_reason = "empty_file"
            hint = "File is empty or has no top-level keys."
        return {
            "composite_score": 0.0,
            "grade": "F",
            "per_test": [],
            "aggregate_dimensions": {},
            "metadata": {
                "artifact_type": artifact_type,
                "scoring_formula": "weighted_geometric_mean",
                "error": error_reason,
                "found_keys": top_level_keys,
                "hint": hint,
            },
        }

    per_test = []
    any_partial = False

    for test_result in results:
        test_id = test_result.get("test_id", "unknown") if isinstance(test_result, dict) else "unknown"
        try:
            scored = _score_single_test(test_result, artifact_type, artifact_content, criteria_index)
        except Exception as exc:  # noqa: BLE001 - one bad record must not kill the run
            print(
                f"WARNING: could not score test '{test_id}': {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            scored = {
                "test_id": test_id,
                "composite": 0.0,
                "dimensions": {},
                "partial_scoring": True,
                "status": "score_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        per_test.append(scored)
        if scored.get("partial_scoring"):
            any_partial = True

    # Inconclusive tests (empty timeline, no execution evidence) carry
    # composite None and are excluded: averaging them in either direction
    # would turn "nothing was observed" into a grade signal.
    conclusive = [
        t
        for t in per_test
        if t.get("status") != "inconclusive" and t.get("composite") is not None
    ]
    inconclusive_count = len(per_test) - len(conclusive)
    composites = [t["composite"] for t in conclusive]
    if composites:
        overall_composite = round(sum(composites) / len(composites), 4)
    else:
        overall_composite = None

    all_dims: dict[str, list[float]] = {}
    for test in conclusive:
        for dim_name, dim_data in test.get("dimensions", {}).items():
            if dim_name not in all_dims:
                all_dims[dim_name] = []
            all_dims[dim_name].append(dim_data["score"])

    aggregate_dimensions = {
        dim: round(sum(scores) / max(1, len(scores)), 4)
        for dim, scores in all_dims.items()
    }

    critical_dim = CRITICAL_DIMS.get(artifact_type, "workflow_sequence")
    critical_floor_applied = aggregate_dimensions.get(critical_dim, 1.0) < 0.3

    return {
        "composite_score": overall_composite,
        "grade": map_grade(overall_composite)
        if overall_composite is not None
        else "INCONCLUSIVE",
        "per_test": per_test,
        "aggregate_dimensions": aggregate_dimensions,
        "metadata": {
            "artifact_type": artifact_type,
            "critical_dim": critical_dim,
            "critical_floor_applied": critical_floor_applied,
            "scoring_formula": "weighted_geometric_mean",
            "epsilon": EPSILON,
            "partial_scoring": any_partial,
            "inconclusive_tests": inconclusive_count,
            "schema_version": 2,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic scoring of eval runner execution data"
    )
    parser.add_argument("results_json", help="Path to results.json from eval runner")
    parser.add_argument(
        "--type",
        required=True,
        choices=["skill", "command", "hook", "script"],
        help="Artifact type",
    )
    parser.add_argument(
        "--artifact-path",
        default=None,
        help="Path to the artifact file (for content-dependent scoring)",
    )
    parser.add_argument(
        "--criteria-path",
        default=None,
        help="Path to eval_criteria.json (enables required_present/absent scoring)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output JSON (default: human-readable)"
    )
    args = parser.parse_args()

    artifact_content = ""
    if args.artifact_path:
        artifact_path = Path(args.artifact_path)
        if artifact_path.exists():
            try:
                artifact_content = artifact_path.read_text()
            except OSError as exc:
                print(f"WARNING: Could not read artifact: {exc}", file=sys.stderr)

    criteria_index = _load_criteria_index(args.criteria_path)
    try:
        result = score_from_results(args.results_json, args.type, artifact_content, criteria_index)
    except Exception as exc:  # noqa: BLE001 - fatal, but must exit cleanly for the caller
        print(
            f"ERROR: scoring failed for {args.results_json}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    results_dir = Path(args.results_json).parent
    output_path = results_dir / "deterministic_scores.json"
    try:
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
    except OSError as exc:
        print(f"WARNING: Could not write scores: {exc}", file=sys.stderr)

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        if result["composite_score"] is None:
            print(f"Composite Score: n/a ({result['grade']})")
        else:
            print(
                f"Composite Score: {result['composite_score']:.4f} ({result['grade']})"
            )
        print()
        for dim, score in result.get("aggregate_dimensions", {}).items():
            print(f"  {dim:<25s} {score:.4f}")
        if result.get("per_test"):
            print(f"\nPer-test ({len(result['per_test'])} tests):")
            for test in result["per_test"]:
                if test.get("composite") is None:
                    print(f"  {test['test_id']}: inconclusive")
                else:
                    print(f"  {test['test_id']}: {test['composite']:.4f}")
        print(f"\nScores written to: {output_path}")


if __name__ == "__main__":
    main()
